"""
Database layer: async SQLite plus RAM caches and transient state.

Language rule: this module produces no strings for display to the user. Log
lines and exception messages are all English; translating into Persian is the
handler layer's job, since it knows how the error should be shown.
"""

import logging
import time
from pathlib import Path

import aiosqlite

from utils import paths

log = logging.getLogger(__name__)

# Database path. The value comes from utils/paths.py (absolute, inside data/)
# but is deliberately kept as a plain module-level name so tests can repoint
# it at a temporary file: db_manager.DB_PATH = "/tmp/test.db"
DB_PATH = paths.DB_PATH

# How long to wait for the write lock to clear (milliseconds).
#
# All handlers run on one event loop, but each opens its own connection, so
# two concurrent writes (caching a message while storing conversation memory,
# say) can collide. Without this value sqlite returns "database is locked"
# immediately instead of waiting a few hundred milliseconds.
BUSY_TIMEOUT_MS = 5000

# Anti-delete cache lifetime (seconds). Messages older than this are pruned on
# every save.
MESSAGE_TTL = 12 * 3600

# --- RAM caches that keep the filters fast ---
ALLOWED_USERS = set()
AUTO_SEEN_CHATS = set()
# Per-chat AI settings: {chat_id: {"model": str|None, "persona": str|None}}
AI_SETTINGS = {}

# --- Global lists (the .gban and .gmute commands) ---
# Both have the shape {user_id: reason}.
#
# Why a dict and not a set? The reason has to be shown in reports and in
# .gbanlist. An absent reason is the empty string rather than None, so readers
# never have to account for None.
#
# Exactly like ALLOWED_USERS, these two caches are read directly inside a
# Pyrogram filter, and that filter runs on *every* group message — so reading
# them must never touch the disk.
GBANNED_USERS = {}
GMUTED_USERS = {}

# Maps a list kind to its table name. Table names come only from this dict and
# are never built from user input; a table name cannot be an SQL parameter, so
# interpolating one from input would be injection.
_GLOBAL_TABLES = {
    "gban": "gbanned_users",
    "gmute": "gmuted_users",
}


def _global_cache(kind):
    """The RAM cache belonging to one kind of global list."""
    if kind == "gban":
        return GBANNED_USERS
    if kind == "gmute":
        return GMUTED_USERS
    raise ValueError(f"unknown global list kind: {kind!r}")


# --- Transient state: deletions we performed ourselves ---
# Shape: {message_id: registration time}
#
# Why message_id alone and not (chat_id, message_id)?
# For private chats Telegram's deletion update (UpdateDeleteMessages) carries
# no peer or chat_id at all, and Pyrogram leaves chat as None in that case
# (pyrogram/utils.py -> parse_deleted_messages). The reason is that private
# message ids are unique within each account's own sequence, so message_id on
# its own is sufficient and safe for identification.
#
# Why no Lock?
# Every Pyrogram handler runs on one event loop and dict operations are atomic
# in Python; a lock would only add complexity.
IGNORE_DELETED_IDS = {}

# Registered ids expire on their own after this many seconds, so that a
# deletion update which never arrives cannot leak memory forever.
IGNORE_TTL = 120


def _connect():
    """
    Open a database connection with the shared settings.

    Every call opens and closes its own connection (the project's current
    pattern), so the per-connection pragmas have to be applied here, in one
    place.
    """
    return aiosqlite.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)


async def _apply_pragmas(db):
    """
    Durability and concurrency pragmas.

    WAL keeps concurrent reads and writes from blocking each other — its main
    effect here is that caching a message while the AI history is being read
    no longer produces "database is locked". WAL's side files (-wal and -shm)
    are created in the same data/ directory and are already gitignored.
    """
    await db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA synchronous = NORMAL")


async def init_db():
    """
    Create the tables and load the caches at startup.

    On failure the exception propagates: without the database the authorized-
    user list stays empty and the bot comes up with the wrong access rules —
    which is worse than not starting. main.py turns this exception into a
    clear message.
    """
    # sqlite does not create the parent directory and fails on a missing path.
    # DB_PATH's own parent is used rather than the DATA_DIR constant, so that
    # a test repointing DB_PATH at a temporary path also gets its directory.
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    async with _connect() as db:
        await _apply_pragmas(db)

        # Message cache table.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                sender_name TEXT,
                text TEXT,
                timestamp REAL
            )
        ''')
        # Migration: add the is_outgoing column to an older table.
        # CREATE TABLE IF NOT EXISTS does nothing to an existing table, so a
        # new column has to be added explicitly and idempotently, keeping
        # current databases (which hold real records) intact.
        async with db.execute('PRAGMA table_info(messages)') as cursor:
            existing_columns = {row[1] async for row in cursor}
        if 'is_outgoing' not in existing_columns:
            await db.execute(
                'ALTER TABLE messages ADD COLUMN is_outgoing INTEGER NOT NULL DEFAULT 0'
            )
            log.info("migrated table 'messages': added column 'is_outgoing'")

        # Same pattern for the media columns. They hold what is needed to put
        # the *file* back into Saved Messages when a photo, voice, video note
        # or sticker is deleted — a text-only report cannot show what was sent.
        # Both are nullable: a plain text message leaves them NULL.
        for column in ('media_type', 'file_id'):
            if column not in existing_columns:
                await db.execute(f'ALTER TABLE messages ADD COLUMN {column} TEXT')
                log.info("migrated table 'messages': added column '%s'", column)

        # The message's own date, as Telegram reports it. It is kept apart from
        # `timestamp` on purpose: `timestamp` is when *we* cached the row and
        # is what the 12-hour prune measures, so it must stay on our own clock
        # (this project corrects a skewed clock rather than trusting it —
        # ARCHITECTURE.md §11.2). Mixing server time into that column would make the
        # prune behave differently depending on the host's clock. After
        # downtime the two also genuinely differ, and the deletion report wants
        # the moment the message was sent, not the moment we caught up.
        if 'sent_at' not in existing_columns:
            await db.execute('ALTER TABLE messages ADD COLUMN sent_at REAL')
            log.info("migrated table 'messages': added column 'sent_at'")

        # Where a pre-downloaded copy of the media sits, and the handful of
        # numbers a re-upload needs to look like the original (duration and
        # dimensions). They are one JSON column rather than three integer ones
        # because nothing queries them — they are read back whole, by one
        # caller, only when a file has to be uploaded again.
        for column in ('local_path', 'media_meta'):
            if column not in existing_columns:
                await db.execute(f'ALTER TABLE messages ADD COLUMN {column} TEXT')
                log.info("migrated table 'messages': added column '%s'", column)

        # The anti-delete prune filters on timestamp and runs for *every*
        # private message; without an index that is a full table scan each
        # time.
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(timestamp)'
        )

        # Authorization and auto-seen tables.
        await db.execute('CREATE TABLE IF NOT EXISTS authorized_users (user_id INTEGER PRIMARY KEY)')
        await db.execute('CREATE TABLE IF NOT EXISTS auto_seen_chats (chat_id INTEGER PRIMARY KEY)')

        # --- AI tables ---
        # Conversation memory, per chat.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS ai_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_ai_conv ON ai_conversations(chat_id, id)')
        # Per-chat settings (chosen model and persona).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS ai_settings (
                chat_id INTEGER PRIMARY KEY,
                model TEXT,
                persona TEXT
            )
        ''')

        # --- Global member-control lists ---
        # Two separate tables rather than one with a "kind" column: ban and
        # mute are independent states and one user can legitimately be in both.
        for table in _GLOBAL_TABLES.values():
            await db.execute(f'''
                CREATE TABLE IF NOT EXISTS {table} (
                    user_id INTEGER PRIMARY KEY,
                    reason TEXT NOT NULL DEFAULT '',
                    timestamp REAL NOT NULL
                )
            ''')

        await db.commit()

        # Load the lists from the database into the RAM caches.
        async with db.execute('SELECT user_id FROM authorized_users') as cursor:
            async for row in cursor:
                ALLOWED_USERS.add(row[0])

        async with db.execute('SELECT chat_id FROM auto_seen_chats') as cursor:
            async for row in cursor:
                AUTO_SEEN_CHATS.add(row[0])

        async with db.execute('SELECT chat_id, model, persona FROM ai_settings') as cursor:
            async for row in cursor:
                AI_SETTINGS[row[0]] = {"model": row[1], "persona": row[2]}

        for kind, table in _GLOBAL_TABLES.items():
            cache = _global_cache(kind)
            async with db.execute(f'SELECT user_id, reason FROM {table}') as cursor:
                async for row in cursor:
                    cache[row[0]] = row[1] or ""

    log.info(
        "database ready — %d authorized user(s), %d auto-seen chat(s), "
        "%d gbanned, %d gmuted",
        len(ALLOWED_USERS), len(AUTO_SEEN_CHATS), len(GBANNED_USERS), len(GMUTED_USERS),
    )


# --- User authorization ---
async def add_allowed_user(user_id):
    ALLOWED_USERS.add(user_id)  # update RAM first
    async with _connect() as db:
        await db.execute('INSERT OR IGNORE INTO authorized_users (user_id) VALUES (?)', (user_id,))
        await db.commit()


async def remove_allowed_user(user_id):
    ALLOWED_USERS.discard(user_id)  # update RAM first
    async with _connect() as db:
        await db.execute('DELETE FROM authorized_users WHERE user_id = ?', (user_id,))
        await db.commit()


# --- Global lists (gban / gmute) ---
async def add_global_flag(kind, user_id, reason=""):
    """
    Add a user to a global list, or update their reason.

    Write-through like the other caches: RAM first, then disk. If the disk
    write fails the exception propagates and the handler reports it; RAM is
    then ahead of disk, which is less harmful than the reverse (the ban not
    being applied at all during this run).
    """
    table = _GLOBAL_TABLES[kind]
    reason = reason or ""
    _global_cache(kind)[user_id] = reason

    async with _connect() as db:
        await db.execute(
            f'INSERT INTO {table} (user_id, reason, timestamp) VALUES (?, ?, ?) '
            f'ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason',
            (user_id, reason, time.time())
        )
        await db.commit()


async def remove_global_flag(kind, user_id) -> bool:
    """
    Remove a user from a global list.

    True means they really were listed; the handler uses that to distinguish
    "removed" from "was not on the list at all".
    """
    table = _GLOBAL_TABLES[kind]
    existed = _global_cache(kind).pop(user_id, None) is not None

    async with _connect() as db:
        await db.execute(f'DELETE FROM {table} WHERE user_id = ?', (user_id,))
        await db.commit()

    return existed


def get_global_flags(kind) -> dict:
    """Read the whole list from RAM as a copy, so callers cannot mutate the cache."""
    return dict(_global_cache(kind))


# --- Auto-seen ---
async def add_auto_seen(chat_id):
    AUTO_SEEN_CHATS.add(chat_id)
    async with _connect() as db:
        await db.execute('INSERT OR IGNORE INTO auto_seen_chats (chat_id) VALUES (?)', (chat_id,))
        await db.commit()


async def remove_auto_seen(chat_id):
    AUTO_SEEN_CHATS.discard(chat_id)
    async with _connect() as db:
        await db.execute('DELETE FROM auto_seen_chats WHERE chat_id = ?', (chat_id,))
        await db.commit()


# --- Message logger ---
async def save_message(message_id, chat_id, sender_name, text, is_outgoing=False,
                       media_type=None, file_id=None, sent_at=None, media_meta=None):
    """
    Store a message in the anti-delete cache and prune expired records.

    is_outgoing records whether we sent the message or the contact did; the
    deletion alert's wording depends on it.

    media_type and file_id are what let the deletion report re-send the actual
    file. The file id is stored rather than the bytes: downloading every photo
    and voice that arrives would cost bandwidth and disk on every single
    message, while a file id costs one column and is enough to put the media
    back in nearly every case (plugins/logger.py covers the rest).

    The prune and the insert share **one transaction**: they used to commit
    separately, which meant two fsyncs per incoming private message.
    """
    cutoff = time.time() - MESSAGE_TTL

    async with _connect() as db:
        await db.execute('DELETE FROM messages WHERE timestamp < ?', (cutoff,))
        # The columns are named explicitly: the migration changed how many
        # there are, and an insert without column names would be fragile.
        await db.execute(
            'INSERT OR IGNORE INTO messages '
            '(message_id, chat_id, sender_name, text, timestamp, is_outgoing, '
            'media_type, file_id, sent_at, media_meta) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (message_id, chat_id, sender_name, text, time.time(), int(is_outgoing),
             media_type, file_id, sent_at, media_meta)
        )
        await db.commit()


async def get_message(message_id):
    """
    Read one cached message.

    Returns (chat_id, sender_name, text, is_outgoing, timestamp, media_type,
    file_id, sent_at, local_path, media_meta) or None. `sent_at` is the
    message's own date and is what the deletion report shows; `timestamp` is
    when we cached it, and is the fallback for rows written before that column
    existed.
    """
    async with _connect() as db:
        async with db.execute(
            'SELECT chat_id, sender_name, text, is_outgoing, timestamp, '
            'media_type, file_id, sent_at, local_path, media_meta '
            'FROM messages WHERE message_id=?',
            (message_id,)
        ) as cursor:
            return await cursor.fetchone()


async def set_local_media_path(message_id, path):
    """
    Record where the pre-downloaded copy of a message's media was written.

    Separate from save_message() because the download finishes *after* the row
    is written: the message is cached the moment it arrives, and the file lands
    seconds later. The UPDATE is a no-op when the row is already gone (pruned,
    or the message was deleted while the download ran), which is exactly the
    behaviour wanted — no row should be resurrected by a late download.
    """
    async with _connect() as db:
        await db.execute(
            'UPDATE messages SET local_path=? WHERE message_id=?',
            (str(path), message_id)
        )
        await db.commit()


# --- Deliberate deletions (the .del command) ---
def _prune_ignored_ids(now=None):
    """Drop ids older than IGNORE_TTL."""
    now = now if now is not None else time.time()
    stale = [mid for mid, ts in IGNORE_DELETED_IDS.items() if now - ts > IGNORE_TTL]
    for mid in stale:
        IGNORE_DELETED_IDS.pop(mid, None)


def mark_deletions_ignored(message_ids):
    """
    Mark ids as "deleted deliberately by us".

    Must be called *before* client.delete_messages, so that the ids are
    already registered by the time Telegram's deletion update arrives.
    """
    now = time.time()
    _prune_ignored_ids(now)
    for mid in message_ids:
        IGNORE_DELETED_IDS[mid] = now


def consume_ignored_deletion(message_id) -> bool:
    """
    Did we perform this deletion ourselves?

    The id is popped, making the suppression strictly one-shot: if the same
    message number is deleted again later, it is not ignored by mistake.
    """
    _prune_ignored_ids()
    return IGNORE_DELETED_IDS.pop(message_id, None) is not None


# ==========================================================
#                      AI functions
# ==========================================================

# --- Per-chat settings (model and persona) ---
def get_ai_settings(chat_id):
    """Read a chat's settings from the RAM cache (never touches disk)."""
    return AI_SETTINGS.get(chat_id, {"model": None, "persona": None})


async def _upsert_ai_setting(chat_id, field, value):
    """Write one settings field; RAM first, then the database (write-through)."""
    current = AI_SETTINGS.setdefault(chat_id, {"model": None, "persona": None})
    current[field] = value

    async with _connect() as db:
        await db.execute(
            'INSERT INTO ai_settings (chat_id, model, persona) VALUES (?, ?, ?) '
            'ON CONFLICT(chat_id) DO UPDATE SET model=excluded.model, persona=excluded.persona',
            (chat_id, current["model"], current["persona"])
        )
        await db.commit()


async def set_ai_model(chat_id, model):
    """Set a chat-specific model."""
    await _upsert_ai_setting(chat_id, "model", model)


async def set_ai_persona(chat_id, persona):
    """Set a chat's persona (an extra system prompt)."""
    await _upsert_ai_setting(chat_id, "persona", persona)


# --- Conversation memory ---
async def add_ai_message(chat_id, role, content, max_turns):
    """
    Append a message to the conversation memory and prune older ones.

    Only the last max_turns messages per chat are kept.
    """
    async with _connect() as db:
        await db.execute(
            'INSERT INTO ai_conversations (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)',
            (chat_id, role, content, time.time())
        )
        # Prune: anything outside this chat's last N records is deleted.
        await db.execute(
            'DELETE FROM ai_conversations WHERE chat_id = ? AND id NOT IN '
            '(SELECT id FROM ai_conversations WHERE chat_id = ? ORDER BY id DESC LIMIT ?)',
            (chat_id, chat_id, max_turns)
        )
        await db.commit()


async def get_ai_history(chat_id, max_turns):
    """
    Read a chat's conversation memory in chronological order (oldest first).

    The result has the shape [{"role": ..., "content": ...}, ...] so it can be
    sent to the API directly.
    """
    async with _connect() as db:
        async with db.execute(
            'SELECT role, content FROM ai_conversations WHERE chat_id = ? ORDER BY id DESC LIMIT ?',
            (chat_id, max_turns)
        ) as cursor:
            rows = await cursor.fetchall()

    # Read with DESC, so reverse it to get the chronological order back.
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


async def clear_ai_history(chat_id):
    """Clear a chat's conversation memory. Returns how many records were deleted."""
    async with _connect() as db:
        cursor = await db.execute('DELETE FROM ai_conversations WHERE chat_id = ?', (chat_id,))
        await db.commit()
        return cursor.rowcount


async def count_ai_history(chat_id):
    """Count the messages currently held in a chat's memory."""
    async with _connect() as db:
        async with db.execute(
            'SELECT COUNT(*) FROM ai_conversations WHERE chat_id = ?', (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
