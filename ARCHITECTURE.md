# Architecture

Design notes and the reasoning behind them, for anyone contributing to this repository.

---

## 1. What this project is

A **Pyrogram UserBot** that runs on a personal Telegram account (a real user session — *not* the Bot API). It adds anti-delete logging, an auto-seen engine, an authorization layer, web-scraping utilities, and a multimodal AI assistant (text, images, voice, video, files) backed by the Google Gemini API.

Because it runs as a real account, **every handler must fail safe**: an unhandled exception in a handler degrades a person's actual Telegram experience, and a mis-sent message goes to a real contact.

---

## 2. Tech stack

| Component | Choice | Notes |
|---|---|---|
| Runtime | Python 3.13 | |
| Telegram | `kurigram==2.2.24` + `TgCrypto==1.2.5` | a maintained Pyrogram fork on a current TL layer; imports as `pyrogram` (§2.1) |
| Storage | `aiosqlite==0.22.1` | single file `data/logger.db` |
| HTTP | `aiohttp==3.13.3` | one shared session for the AI layer |
| Config | `python-dotenv==1.2.1` | all secrets via `.env` |
| Deploy | Docker + Docker Compose | project root bind-mounted for session/DB persistence |

Everything is `asyncio`. **Never introduce a blocking call** (`requests`, `time.sleep`, sync `sqlite3`) into a handler — it stalls the entire update dispatcher.

### 2.1 Why the Telegram library is a fork

The project ran on `pyrogram==2.0.106` (TL **layer 158**, mid-2023) until that layer became the reason a feature could not work at all.

**Telegram refuses to deliver view-once and one-time media to a client announcing an old layer.** A view-once photo and a one-time video note arrive as `messageMediaUnsupported`: no `ttl_seconds`, no `photo`, no `document` — an empty shell. There is nothing to download, so no amount of work inside the TTL guard could have rescued them (§8.2 documents two rounds of trying). One-time voice and video messages shipped in January 2024, months after layer 158 froze. Ordinary countdown-timer media is *not* gated and kept working throughout, which is exactly what made the failure look like a bug in this repo.

`kurigram` is a maintained Pyrogram fork on layer 227. It installs as the `pyrogram` package, so **every import and call in this repo is unchanged**. What the migration actually cost, all of it verified headlessly before the swap:

| Area | Outcome |
|---|---|
| All 23 project modules, both headless suites (83 + 22 checks) | passed unmodified |
| Session file | migrated in place (`ALTER TABLE sessions ADD server_address/port`) — no re-login |
| TgCrypto | still `1.2.5`; the fork accepts it, so encryption stays native |
| §11.1 peer-id patch | **obsolete** — the fork classifies peers with no range table, and the patch now skips itself at INFO |
| §11.2 clock patch | **broke, and was rewritten** — see §11.2, this was the only real blocker |
| `UNMUTED_PERMS` | **broke silently** — `ChatPermissions` grew from 8 to 17 fields (§8.3.1) |

`Message._parse` in the fork also passes `ttl_seconds` through for voice, video notes and video, so the library-gap workaround in §8.2 is no longer the *only* way to see a timer — but the raw handler stays, because it is still the earliest point in the pipeline and still the only place that keeps the metadata a faithful re-upload needs.

Keep the version pinned. The reason for pinning never changed; only the pin did.

---

## 3. Layout

```
main.py                  bootstrap: patches, clock sync, init_db(), Client, idle(), shutdown
config.py                env loading with fail-fast validation
README.md                public-facing docs (Persian)
data/                    runtime state — fully gitignored (§3.1)
  my_account.session     Pyrogram session = full account access
  logger.db              SQLite: message cache, auth, AI memory, switches
  forward_tmp/           .fwd scratch space, one subdirectory per run (§8.5)
plugins/                 Pyrogram smart-plugin root (auto-discovered)
  general.py             auth commands + .ping .weather .del .gold .azan .id .help
  logger.py              anti-delete cache + deletion reporting
  auto_seen.py           auto read-receipts
  ai_assistant.py        .ask / .r / .rp / .sum / .aistop / .ai — streaming assistant
  ai_tools.py            .tr translate, .ocr, .img — one-shot AI tools (§6.3)
  voice_tools.py         .stt speech→text, .tts text→voice (§6.4)
  ttl_guard.py           self-destructing media capture — raw handler, group -1 (§8.2)
  group_admin.py         .kick .ban .mute + global .gban/.gmute — enforcement in group -2 (§8.3)
  bot_cleaner.py         .botclean — bot messages in chats you deleted, group 3 (§8.4)
  link_forward.py        .fwd — forward by message link, protected chats included (§8.5)
utils/
  paths.py               PROJECT_ROOT / DATA_DIR / DB_PATH — single source of truth (§3.1)
  db_manager.py          aiosqlite access + RAM caches + transient state
  ai_engines.py          Gemini transport: SSE, files, images, TTS, key rotation
  ai_context.py          Telegram message → model input (text + media + reply)
  media.py               media inspect/download/mime-map + PCM→WAV/OGG
  helpers.py             respond(), respond_long(), safe_delete(), @safe_handler, flood_seconds()
  net.py                 shared HTTP for the information utils (§7.4)
  pyrogram_patch.py      runtime patches for the pinned library (§11.1, §11.2, §11.4)
  clock.py               SNTP + HTTP-Date clock offset probe (§11.2)
  logging_setup.py       log config, noise throttling, asyncio exception hook (§11.3)
  weather_api.py         Open-Meteo
  gold_api.py            TGJU scraper
  azan_api.py            AlAdhan
  currency_api.py        Wallex→Ramzinex crypto prices + 60s RAM cache
```

Handlers are registered with `@Client.on_message(...)` **on the class**, and Pyrogram's smart-plugin loader (`plugins=dict(root="plugins")`) imports every module under `plugins/`. There are no manual `add_handler` calls. `plugins/` and `utils/` have no `__init__.py` and work as namespace packages.

### 3.1 `data/` and `utils/paths.py`

All runtime state lives in `data/`, and every path to it comes from `utils/paths.py`. Two separate problems are solved here.

**Paths are absolute, anchored to `__file__`.** `DB_PATH` used to be the bare string `'logger.db'`, and the Pyrogram session used the default `workdir='.'` — both resolved against the **current working directory**, not the project. Starting the bot from anywhere else (systemd with a different `WorkingDirectory`, cron, an IDE run config) silently created a *fresh empty* database and a *fresh unauthenticated* session next to wherever it was launched: the bot asked for a login code again and `ALLOWED_USERS` came up empty, with nothing in the logs pointing at the cause. `PROJECT_ROOT = Path(__file__).resolve().parent.parent` removes the entire class of bug. Verified by booting from an unrelated directory — same session, no stray files.

**`paths.py` has no dependencies at all** — not `config`, not `dotenv`, not `pyrogram`, only `pathlib`. It is imported by both `config`-adjacent code and `db_manager`, and `config.py` fails fast when `API_ID` is missing; routing a path constant through it would have made every database test require Telegram credentials. `DATA_DIR` is deliberately **not** env-overridable: `paths` would then have to read `os.environ` before `load_dotenv()` had necessarily run, so a `DATA_DIR` line in `.env` would work or not depending on import order. Docker gets the same flexibility from a bind mount (`./data:/app/data`) with no such trap.

`db_manager.DB_PATH` stays a plain module-level name (assigned from `paths.DB_PATH`) so tests can still repoint it, and `init_db()` creates `Path(DB_PATH).parent` rather than `DATA_DIR` so a repointed test path also gets its directory made. `ensure_data_dir()` is called once in `main()` because the session file needs the directory too, and neither sqlite nor Pyrogram creates a missing parent.

---

## 4. Database schema (`logger.db`)

```sql
messages(message_id INTEGER PRIMARY KEY, chat_id INTEGER, sender_name TEXT,
         text TEXT, timestamp REAL, is_outgoing INTEGER NOT NULL DEFAULT 0,
         media_type TEXT, file_id TEXT, sent_at REAL,
         local_path TEXT, media_meta TEXT)
authorized_users(user_id INTEGER PRIMARY KEY)
auto_seen_chats(chat_id INTEGER PRIMARY KEY)
ai_conversations(id INTEGER PK AUTOINCREMENT, chat_id INTEGER, role TEXT,
                 content TEXT, timestamp REAL)          -- INDEX (chat_id, id)
ai_settings(chat_id INTEGER PRIMARY KEY, model TEXT, persona TEXT)
gbanned_users(user_id INTEGER PRIMARY KEY, reason TEXT NOT NULL DEFAULT '', timestamp REAL NOT NULL)
gmuted_users(user_id INTEGER PRIMARY KEY, reason TEXT NOT NULL DEFAULT '', timestamp REAL NOT NULL)
settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)
```

`settings` holds switches flipped from Telegram (`.botclean on` today). One key/value table rather than a table per switch: a switch is a single row and nothing queries across them, so a new one costs a key name, not a migration — and `CREATE TABLE IF NOT EXISTS` is the entire migration for an older database.

`messages` is a rolling 12-hour cache, pruned inside `save_message()` — the prune and the insert
share **one transaction and one commit**, since two commits per incoming private message meant two
fsyncs on the hot path. `idx_messages_ts` backs the prune's `WHERE timestamp < ?`; without it that
filter was a full table scan on every message.

**Connection pragmas.** Every call opens its own connection through `_connect()`, so per-connection
pragmas are applied in one place: `busy_timeout=5000`, `journal_mode=WAL`, `synchronous=NORMAL`.
WAL is what stops a message being cached concurrently with an AI-history read from producing
`database is locked`; its `-wal`/`-shm` side files land in `data/` and are already gitignored.

`gbanned_users` and `gmuted_users` are two separate tables rather than one table with a `kind` column: ban and mute are independent states and one user can legitimately be in both. Table names come only from the `_GLOBAL_TABLES` map — a table name cannot be an SQL parameter, so building one from input would be injection.

**`timestamp` vs `sent_at`.** `timestamp` is when *we* cached the row, on our own clock, and it is what the 12-hour prune measures — this project corrects a skewed system clock rather than trusting it (§11.2), so the prune must not be driven by server time. `sent_at` is the message's own date from Telegram and exists only for display: the deletion report shows when the message was *sent*, and after downtime that is not when we saw it. Rows written before the column existed fall back to `timestamp`.

**`media_type` / `file_id`.** What lets a deletion report re-send the deleted photo, voice, sticker or video note instead of merely naming it (§5.2). The file id costs one column and no bandwidth, but a file reference can expire — and the message it belongs to is gone, so it cannot be refreshed.

**`local_path` / `media_meta`.** The answer to that expiry: small media is copied to disk as it arrives (§5.3), and `local_path` is where it landed. `media_meta` is a small JSON blob (`{"d":7,"w":384,"h":384}`) holding duration and dimensions. It is one column rather than three because nothing queries it — it is read back whole, by one caller, only when a file has to be uploaded again. The values matter because re-uploading bytes means Pyrogram's own defaults apply, and those defaults turn a voice into a 0:00 clip and a video note into a 1×1 circle (§8.2). `local_path` is written by a separate `UPDATE` after the download finishes, which is a no-op if the row is already gone — a late download must not resurrect a pruned row.

**Migrations.** `CREATE TABLE IF NOT EXISTS` does nothing to an existing table, so new columns need an explicit idempotent migration in `init_db()` — see the `is_outgoing` block that reads `PRAGMA table_info(messages)` before issuing `ALTER TABLE`. Follow that pattern; never drop and recreate a table, since `logger.db` holds real data. Verified against a database created before these columns existed: the rows survive and the second run is a no-op.

**RAM caches.** `ALLOWED_USERS`, `AUTO_SEEN_CHATS`, `AI_SETTINGS`, `GBANNED_USERS`, `GMUTED_USERS`, `SETTINGS` are module-level, loaded once in `init_db()`, and updated **write-through** (RAM first, then disk). Custom filters read only from RAM so filter evaluation never touches the disk — this matters most for the two global lists, whose filter runs on *every* group message, and for `SETTINGS`, which the bot-chat cleaner reads on every message a bot sends.

---

## 5. Message logging architecture

### 5.1 What gets cached

`cache_private_messages` is bound to `filters.private` — **both directions**. Outgoing messages are cached deliberately, so that if a contact deletes a message *you* sent, you still find out.

Two hard exclusions, both implemented as early `return`s so nothing ever reaches the database:

**Bots.** Checked via `message.chat.type == enums.ChatType.BOT`, *not* `from_user.is_bot`. This is deliberate and the distinction matters: in a message **you send to a bot**, `from_user` is *you*, so `is_bot` is `False` and an `is_bot`-only check silently caches it. `ChatType.BOT` identifies the conversation regardless of direction. A secondary `from_user.is_bot` check stays as a safety net for bot-sent messages in oddly-typed chats.

**Saved Messages.** Identified by `message.chat.id == client.me.id`. Beyond the obvious "don't log your own notepad", this exclusion is **load-bearing**: deletion reports are themselves sent to Saved Messages, so without it every report would be re-cached and the log would feed on itself.

Both guards use safe attribute access (`message.from_user and ...`, `getattr(client, "me", None)`) because `from_user` is `None` for anonymous/service messages.

### 5.2 What gets reported

`catch_deleted_messages` receives only message **IDs** — Telegram does not say who deleted anything. Two filters apply before reporting:

**Non-private deletions are skipped.** Pyrogram's `parse_deleted_messages` (`pyrogram/utils.py`) sets `chat=None` when the update has no `channel_id`, i.e. for every private-chat deletion; channel/supergroup deletions carry a real `chat`. Since only private messages are cached, `if msg.chat is not None: continue` skips channel traffic — and incidentally prevents a supergroup message ID from colliding with a cached DM ID.

**Report wording depends on direction.** Incoming messages name the sender. Outgoing messages use `🗑 پیام خروجی شما حذف شد: (توسط شما یا مخاطب)` because the protocol genuinely cannot distinguish you deleting it from your contact deleting it — the wording states that ambiguity rather than hiding it.

**Every report identifies the account, not just the display name** (`helpers.contact_lines()`, shared with §8.2). A display name is not an identity — two contacts share one, and a person who renames himself afterwards leaves the report pointing at nobody — so the `@username` and the phone number are shown under it, and when the account publishes neither, the numeric id takes their place. Only then: the number is the fallback, not a fourth line on every report.

**The peer is resolved at report time, not stored with the message,** and that is what makes the phone number available in *both* directions. On a message you sent, `from_user` is you, and the `Chat` object Pyrogram builds for the peer carries no phone number at all — so a one-off `get_users()` on the cached `chat_id` is the only route that answers for incoming and outgoing alike. It is also the only source for the peer's *name* on an outgoing report, where the cached sender name is merely `شما`; before this, an outgoing deletion report never said which chat it came from. `_peer_identity()` never raises: the row already holds the numeric chat id, so an unresolvable peer degrades to that number instead of costing the report. Results are cached per chat for an hour — one deletion can produce a hundred reports from one chat, and each must not cost a round trip — but a *failed* lookup is kept for only 60 s, because the usual causes (a `FloodWait`, a network blip) pass in seconds and an hour of caching would answer with a bare number long after the peer was reachable again.

**Deleted media is re-sent, not described.** A cached `file_id` is replayed with `send_cached_media`, so a deleted photo, voice, sticker, GIF or video note comes back as the file itself with the report as its caption.

Three outcomes have to be told apart, and collapsing two of them into "failed" is a real bug rather than a style point: Telegram accepts **no caption on a sticker or a video note**, so for those the media is sent and the report still has to follow separately (`NEEDS_TEXT`) — treating that as failure sends the same media a second time from the local copy. Hence `SENT` / `NEEDS_TEXT` / `FAILED` and the order in `_send_report()`: stored file id first (no upload, and the original is preserved exactly — a video note stays round, a voice keeps its waveform), the pre-downloaded copy second, the plain text report last.

**Both timestamps are shown.** `sent_at` (the message's own date) and the moment the deletion update arrived, plus how long the message survived. Telegram's deletion update carries no timestamp of its own, so "now" is as close to the deletion as anything available.

**Every displayed time goes through `helpers.time_text()`** (§10.11). `datetime.fromtimestamp()` reads the *process's* timezone, which inside the container is UTC — so a message sent at 21:30 in Tehran was reported as 18:00 and the report looked broken, even though the stored timestamps were right all along.

Reports are sent with **`ParseMode.HTML` and `html.escape()`** on all interpolated content. Cached text is arbitrary user input; a stray backtick or asterisk breaks Markdown rendering or makes Telegram reject the whole message. HTML escaping is well-defined and total.

### 5.3 Pre-downloading media (`DELETE_CACHE_MEDIA`)

Re-sending by file id costs nothing on arrival, and fails exactly when it is needed most: the reference has expired and the message that could refresh it is the one that was deleted. So small media is copied to `data/deleted_media/` as it arrives.

**What is skipped, and why each exclusion exists.** Stickers — their files are permanent shared server-side objects, so a file id keeps working long after a photo's reference would have expired, and disk spent on them buys nothing. Anything over `DELETE_CACHE_MAX_MB` (5) — a photo or voice is worth a background download on every private message, a 300 MB video is not. And anything carrying `ttl_seconds`, because the TTL guard has already downloaded it (§8.2) and a second transfer would compete with the one that is racing the clock.

**The download is a background task, deliberately.** The row is already saved by then, the report only needs the file if the message is deleted later, and a multi-megabyte transfer must not hold up the handler that caches the *next* message. `_DOWNLOAD_SLOTS` caps it at three at a time so a burst of media cannot open a dozen simultaneous transfers on the connection that also carries the account's updates. Every failure is swallowed: this is a safety net, and one that breaks what it protects is worse than none.

**The directory is bounded twice.** By age — `CACHE_RETENTION` is `MESSAGE_TTL` exactly, because once the row is pruned there is nothing left to report about — and by total size (`DELETE_CACHE_TOTAL_MB`, 500), oldest first, because twelve hours of photo-sharing can still fill a small disk. Both run on write, since writing is the only thing that makes the directory grow.

### 5.4 The `/del` suppression mechanism

When you delete your own messages with `.del`, the anti-delete logger would otherwise alert you about deletions you just performed.

Since `on_deleted_messages` provides no actor, we bridge the gap with transient state in `utils/db_manager.py`:

```
IGNORE_DELETED_IDS = {message_id: registered_at}    # TTL = IGNORE_TTL (120s)
```

- `mark_deletions_ignored(ids)` — called in `delete_messages_command` **immediately before** `client.delete_messages`. The ordering is mandatory: the deletion update can arrive the instant the call returns, so registering afterwards would race and emit a false alert.
- `consume_ignored_deletion(id)` — called at the top of `catch_deleted_messages`. It **pops** the ID, so suppression is strictly one-shot; if the same message number is deleted again later it alerts normally.

Design notes:
- **Keyed by `message_id` alone**, because private deletions carry no `chat_id` (§5.2). This is safe: private-chat message IDs come from a single per-account sequence, which is exactly why Telegram omits the peer in that update.
- **Registered only for `ChatType.PRIVATE`.** Group IDs live in a different number space and could collide with a cached DM ID, so they are never added.
- **TTL-pruned on every write.** If a deletion update never arrives, entries expire after 120 s instead of leaking forever.
- **No lock.** All handlers run on one asyncio event loop and dict operations are atomic under the GIL; a lock would add await points for no benefit.

Known trade-off: if `delete_messages` fails after IDs are registered, a genuine deletion of those same IDs within the TTL window would be suppressed. Bounded and rare; accepted.

---

## 6. AI assistant

The backend is the **Google Gemini API** (`generativelanguage.googleapis.com`), called directly — the old OpenAI-compatible gateway is gone. `utils/ai_engines.py` is pure transport with **no Pyrogram import**, so it is testable standalone.

- One lazily-created shared `aiohttp.ClientSession`, built inside the running loop and closed in `main.py`'s `finally`.
- `ClientTimeout(total=None, sock_connect=15, sock_read=AI_TIMEOUT)` — a `total` cap would guillotine long generations; `sock_read` catches real stalls.
- Errors sometimes arrive **inside the SSE stream under HTTP 200**, so each frame is checked for an `error` key before reading `candidates`.
- Retries on 429/5xx with capped exponential backoff (`MAX_BACKOFF = 8s`, kept short because commands are interactive). `Retry-After` is honoured only for 429. **A stream is never retried once it has yielded a token** — that would duplicate text into the user's message.
- **Key rotation.** `config.AI_API_KEYS` holds several Google keys; `401/403/429` rotates to the next one (`KEY_ROTATE_STATUS`). `400` deliberately does **not** rotate — that means *our* request is malformed, and rotating would just repeat the failure while hiding the real message. Rotation is capped at one pass over the keys; after that the loop falls back to backoff instead of hammering every key.
- Parts flagged `"thought": true` are dropped — they are the model's internal reasoning, not the answer. (Note `thoughtSignature` on a *text* part is different and must not be filtered.)
- A finish reason in `BLOCKING_FINISH_REASONS` with no text raises instead of returning an empty answer, so a safety block reads as a safety block.

**Model names are verified against the live `ListModels` output, not guessed.** `gemini-3.6-flash` (default), `gemini-3.7-flash` and `gemini-3.1-flash-lite` work on the current free-tier keys. Two aliases are correct but **quota-blocked on the free plan** and return 429 `limit: 0`: `pro` (`gemini-3.1-pro-preview`) and every image model. `plugins/ai_tools.py::_friendly_error` translates that specific 429 into a Persian "needs a paid key" message rather than dumping Google's English wall of text.

### 6.1 Multimodal input

`utils/media.py` + `utils/ai_context.py` turn a Telegram message into model input:

- `inspect_media()` reads the *declared* size and mime **before** downloading; a file over `AI_MEDIA_MAX_MB` is rejected without ever touching the network — checking after download would defeat the purpose.
- `gemini_mime()` maps Telegram's mime to something Google accepts (`audio/opus`→`audio/ogg`, `application/octet-stream`+`.py`→`text/plain`, …) and **raises for anything unsupported** (animated `.tgs` stickers, zip archives) so the user gets a clear Persian message instead of an opaque 400.
- Files ≤ `MAX_INLINE_BYTES` (14 MB) go inline as base64; larger ones are uploaded through the **Files API** (`make_media_part`) which polls until the file is `ACTIVE`. 14 MB is the safe raw ceiling under Google's 20 MB request limit, since base64 inflates by ~33 %.
- `collect()` **never raises on media problems** — it returns the text it did get plus a Persian note in `.notes`, which the handler appends under the answer. A broken attachment degrades the command, it doesn't kill it.
- `download_bytes()` retries **once against a re-read message** when the first attempt fails, because the commonest reason for a failure is a stale file id rather than a broken transfer. A file id carries a `file_reference` the server invalidates over time, and Pyrogram serves `message.reply_to_message` straight out of `client.message_cache` (`client.py:413`, `message.py:1875`) — an object parsed when the message first *arrived*, held for the last thousand messages of the session. Replying to a voice note that had been sitting in the chat therefore reached `download_media` with a reference the server had already dropped, so `.stt` failed on a reply while the same voice transcribed fine once forwarded — the forwarded copy is a new message with a new reference. `messages.getMessages` mints a fresh one and marks nothing as read, and `Message._parse` writes the result back into that same cache, so the next command on the message starts from the repaired object. The retry is skipped on `FloodWait` (retrying only earns a second one) and when the re-read comes back without media. Same idea as `ttl_guard._refetch_media` (§8.2.2), one layer up.

### 6.2 Streaming UI

`plugins/ai_assistant.py` renders the stream through `StreamEditor`: edits throttled to ~1.2 s, `ParseMode.DISABLED` while streaming (partial output routinely has an unbalanced ``` fence), Markdown applied only on the final edit with a plain-text fallback, adaptive `FloodWait` backoff, and rollover into a new message near Telegram's 4096-character cap.

`_render()` must issue **exactly one** edit per call. It previously fell through into a second, header-less `edit_text` for every chunk — doubling the edit rate straight into `FloodWait` and dropping the question header. If you touch that method, keep the single-exit shape.

`_run_stream()` is the shared body of `.ask` / `.r` / `.rp` / `.sum`: stop-flag, anchor message, stream loop, memory write and error rendering live there once. The per-chat stop flag is released in a `finally` — otherwise a failure before the first edit would lock that chat out of AI commands until restart.

### 6.3 One-shot tools (`plugins/ai_tools.py`)

`.tr`, `.ocr` and `.img` use non-streaming `generate()` with `FAST_CONFIG` (`thinkingLevel: low`) — measured at roughly half the latency of the default, and these commands have one right answer rather than an essay. If a user points `AI_MODEL` at a pre-3 model that rejects `thinkingConfig`, `generate()` catches the 400 and retries once without it instead of failing the command.

Tool output is sent with **`ParseMode.DISABLED`**: a translation or OCR dump is arbitrary text, and one stray `*` or `_` either breaks rendering or makes Telegram reject the whole message (§10.6). Long output goes through `respond_long()` rather than `respond()`, which would silently truncate a transcript.

### 6.4 Voice (`plugins/voice_tools.py`)

`.stt` sends the audio with an **English instruction placed before the file**. With a Persian instruction after the audio, the model intermittently appended that instruction to the transcript as if it had been spoken — reproduced and fixed, so keep the order.

It also separates *"you did not reply to audio"* from *"the audio could not be fetched"*. Testing only `collected.media is None` conflated the two, so a failed download answered with "reply to a voice note" and the download error underneath it — a reply that contradicted itself and sent the user round in a circle. The kind check now runs against `collected.spec`, which describes the media without downloading it, and a fetch failure reports whatever `collect()` left in `.notes`. `.ocr` had the same shape and got the same split.

`.tts` gets raw PCM back (`audio/L16;rate=24000`), and the sample rate is parsed from the response mime rather than assumed — a hardcoded rate would silently pitch-shift the voice if Google changed it. `pcm_to_ogg()` converts to OGG/Opus with ffmpeg so it arrives as a **real Telegram voice message**; with no ffmpeg on the host it returns `None` and the command falls back to a WAV sent via `send_audio`. That is why the Dockerfile installs ffmpeg — the feature works either way, only the presentation changes.

---

## 7. Market data (`utils/currency_api.py`)

`get_market_prices()` returns a display-ready Persian block with USDT/BTC/ETH prices in Toman. Like the other `*_api.py` utils it **never raises into a handler** — every failure path returns `❌ خطا در دریافت اطلاعات از صرافی‌ها` (note the plural: there are now two exchanges behind it).

### 7.1 Dual-exchange failover

**Nobitex was dropped: `api.nobitex.ir` no longer resolves (NXDOMAIN) from the deploy host.** It is replaced by a two-source chain declared in `_SOURCES`, tried in order:

| Order | Exchange | Endpoint | Unit |
|---|---|---|---|
| 1 (primary) | Wallex | `GET https://api.wallex.ir/v1/markets` | TMN — **Toman, no conversion** |
| 2 (fallback) | Ramzinex | `GET https://publicapi.ramzinex.com/exchange/api/v1.0/exchange/pairs` | IRR — **Rial, ÷10 required** |

`_fetch_quotes()` walks `_SOURCES`, returning the first source that both responds *and* parses. Adding a third exchange means writing one `_parse_*` function and appending a tuple — nothing else changes.

**The catch is deliberately broad.** Timeouts, DNS failures and 5xx all trigger the fallback, but so do malformed JSON and schema drift, because from the user's side those are the same event: *this exchange is not usable right now*. A failure logs `log.warning` with the exchange name and exception type, and a successful failover logs `سوییچ به صرافی جایگزین` — so a geo-block shows up in the logs rather than as a silent price outage. Only when **every** source fails does `_fetch_quotes()` raise, which `get_market_prices()` converts to `ERROR_TEXT`.

Both attempts share one `aiohttp.ClientSession`; the timeouts in `_TIMEOUT` are **per request**, so a dead primary costs at most ~8 s of connect time before the fallback starts.

### 7.2 Why Ramzinex and not Bitpin

Bitpin (`api.bitpin.ir/v1/mkt/markets/`) was measured and rejected:

- **Payload size: 2.68 MB vs Ramzinex's 575 KB** (1184 markets vs 602). The fallback fires exactly when the network is already degraded, which is the worst moment for a 2.7 MB download. Ramzinex was also ~2× faster in practice (~1.0 s vs ~2.0 s median).
- **Bitpin exposes three sibling `change` fields with inconsistent scaling** — for BTC it simultaneously reported `order_book_info.change = 0.0103` (fraction), `internal_price_info.change = 1.03` (percent) and `price_info.change = 0.8199` (a different percent). Picking the wrong one is a silent 100× error. Ramzinex has exactly one: `financial.last24h.change_percent`.

Bitpin remains a valid third link if one is ever wanted: match `results[]` on `code == "BTC_IRT"`, price at `price`, change at `price_info.change`, unit confirmed by `currency2.code == "IRT"` (Toman).

### 7.3 Expected JSON shapes

Wallex — keyed by symbol, price is a **string**:

```jsonc
{"success": true,
 "result": {"symbols": {"USDTTMN": {"quoteAsset": "TMN",
                                    "stats": {"lastPrice": "189200.0000",
                                              "24h_ch": 0.59}}}}}
```

Ramzinex — a **list**, price is a **raw number**, symbols are **multilingual dicts**:

```jsonc
{"status": 0,
 "data": [{"base_currency_symbol":  {"en": "btc", "fa": "بیت کوین"},
           "quote_currency_symbol": {"en": "irr", "fa": "ریال"},
           "is_delist": 0, "is_temporarily_suspended": 0,
           "financial": {"last24h": {"close": 122749999991.81,
                                     "change_percent": 1.24}}}]}
```

Each parser normalises to `dict[str, Quote]` where `Quote = NamedTuple(toman: float, change: float | None)`, so nothing downstream knows which exchange served the data.

**Units are read from the response, never assumed** — the §7 rule survives the rewrite, since Rial→Toman is a ÷10 conversion and a wrong guess is a silent 10× price error. Wallex is checked via `quoteAsset`, Ramzinex via `quote_currency_symbol.en`; an unrecognised unit raises rather than rendering. Note that Ramzinex genuinely returns **Rial** despite the `-irt`-style naming used elsewhere in the Iranian exchange ecosystem — this was confirmed by cross-checking all three coins against Wallex (<0.3 % apart after ÷10, 10× apart without it), not assumed from documentation.

Other defensive details: values go through `_to_number()`, which accepts both strings and raw numbers; `_symbol_en()` tolerates a flat string in case Ramzinex ever un-nests its symbols; delisted and suspended Ramzinex pairs are skipped; and any zero, negative or missing price raises rather than rendering a partial or zero price.

**Caching.** A module-level 60-second RAM cache (`_cache_text` + `_cache_at`). Two deliberate choices:

- **`time.monotonic()`, not `time.time()`** — a system clock jump (NTP sync) measured against a wall-clock timestamp can leave the cache permanently valid or permanently expired.
- **Only successful results are cached.** Caching the error string would keep returning it for a full TTL even if the network recovered a second later. This matters more now: a cached error would also mask a fallback that had started working.

An `asyncio.Lock` gives single-flight behaviour: concurrent calls on a cold cache produce exactly one HTTP request (verified: 10 concurrent calls → 1 request), and waiters re-check the cache after acquiring the lock.

**Formatting.** Unchanged by the rewrite: thousand separators, and `+x.xx% 🟢` / `-x.xx% 🔴` / `0.00% ⚪` for the 24-hour change. Markdown `**` wraps only the hardcoded header — no API-derived value is ever placed inside a formatting construct, so the output cannot break Telegram's parser. The output carries no exchange name; which source served a price is a log-level concern, not a user-facing one.

**Dependencies.** None added; `aiohttp` was already pinned. Brotli is *not* needed — `curl` without `Accept-Encoding` occasionally gets an unlabelled brotli body from Wallex's CDN, but aiohttp always negotiates encoding and decoded 24/24 responses correctly. The module opens a per-call `ClientSession` (like `gold_api`/`weather_api`) rather than a shared one, so it needs no shutdown hook in `main.py`; the 60 s cache keeps that cost rare.

Wired to `.price` / `.market` in `plugins/general.py` (§8).

### 7.4 `utils/net.py` — the other three information utils

`weather_api`, `gold_api` and `azan_api` share one small HTTP layer. They previously duplicated the
same twelve lines of aiohttp boilerplate and each had dropped a different part of it: `weather_api`
had **no `try/except` at all** (so a DNS failure or a renamed JSON key raised straight into
Pyrogram's dispatcher, violating §10.3), `gold_api` and `azan_api` interpolated the raw exception
into the Persian message the user saw, and none of them set an explicit timeout or URL-encoded the
city name that comes directly from the user's message.

`fetch_json()` / `fetch_text()` now own that: an explicit `ClientTimeout(total=15, sock_connect=6,
sock_read=10)`, a browser `User-Agent` (TGJU answers aiohttp's default with 403), a 4 MB body cap,
and **one** exception type out — `HttpError`, English, for the log. Every caller wraps it and
returns its own Persian constant, so the failure text a user sees is specific to the command
(«دریافت قیمت طلا ممکن نشد…») while the cause stays in the log.

The parsers also validate rather than index blindly: a missing `hourly` section, arrays of differing
length, a plausibility range on the gold price, and `payload["code"] != 200` for AlAdhan (which
returns HTTP 200 for an unknown city and puts the real status in the body). Schema drift degrades to
a Persian error, never to a wrong number or a traceback.

---

## 8. Commands

| Command | Access | Purpose |
|---|---|---|
| `.adduser` / `.deluser` | owner | manage authorized users |
| `.addseen` / `.delseen` | owner | manage auto-seen chats |
| `.ping` `.weather` `.gold` `.azan` `.id` | authorized | utilities |
| `.help` / `.cmds` | authorized | full command list |
| `.price` / `.market` | authorized | live USDT/BTC/ETH prices in Toman (60 s cache) |
| `.del <n>` | authorized | delete last *n* messages, max 200, sent in 100-id batches (§8.4) |
| `.send <count> <user_id>` | authorized | relay the last *count* messages from a PV into the current chat (max 50) |
| `.ttl` / `.ttlguard` | authorized | TTL-media guard status + per-run counters (§8.2) |
| `.kick` | authorized | remove from this group without banning (§8.3) |
| `.ban [dur] [reason]` / `.unban` | authorized | ban in this group, optionally time-limited |
| `.mute [dur]` / `.unmute` | authorized | silence in this supergroup, optionally time-limited |
| `.gban` / `.ungban` | authorized | ban across every managed group + persist (§8.3) |
| `.gmute` / `.ungmute` | authorized | mute across every managed supergroup + persist |
| `.gbanlist` / `.gmutelist` | authorized | show the global lists |
| `.gstatus` / `.gadmin` | authorized | managed-group count, list sizes, enforcement stats |
| `.fakegban` / `.fgban` | authorized | prank — imitates `.gban` and does nothing (§8.3) |
| `.ask <prompt>` | authorized | AI question, streamed; accepts attachments and replies |
| `.r` / `.re` / `.about` | authorized | ask *about the replied message*, including its media |
| `.rp` / `.suggest` | authorized | draft three replies to the replied message |
| `.sum [n]` | authorized | summarize the replied message, or the last *n* messages (max 300) |
| `.tr [lang] <text>` | authorized | translate text / reply / text inside an image |
| `.ocr` | authorized | extract text from an image or PDF |
| `.stt` / `.v2t` | authorized | transcribe voice, audio, video note (optional translation) |
| `.tts [voice] <text>` | authorized | text → voice message (`.tts voices` lists voices) |
| `.img <prompt>` | authorized | generate or edit an image (**needs a paid key**) |
| `.aistop` | authorized | halt an in-flight generation |
| `.ai model/models/persona/reset/status` | owner | AI configuration |
| `.fwd <link> [link …]` / `.forward` | owner | forward linked message(s) here; protected chats are re-uploaded as copies (§8.5) |
| `.botclean on/off` / `.botclean` | owner | delete what bots send into chats you deleted; status (§8.4) |

All AI commands accept a model alias and `extended` as leading words (`.ask pro extended …`, `.sum lite 100`); `_extract_flags()` stops at the first non-flag word and keeps the remaining text — including newlines — intact.

Prefixes are `["/", "."]`. Model and persona changes are owner-only on purpose: `.ask` costs tokens, and letting a third party rewrite the system prompt is a config-injection vector.

**`.price` / `.market` interaction flow.** Both names map to one handler (`market_prices_command`), registered with `filters.command(["price", "market"], ...)`. It is the reference implementation of the §10.3/§10.4 pattern that every handler in `general.py` now follows:

```
status = await respond(message, "⏳ …")   # own message → edit; someone else's → reply
result = await get_market_prices()        # §7; cached, and returns its own error string
await respond(status or message, result)  # status was sent by us, so this edits in place
```

The two-step `respond()` is what makes the command safe for authorized third parties. When *you* run it, the command message is edited twice in place. When an authorized user runs it, their message is **never** touched — `respond()` posts a reply, and because that reply is ours (`outgoing=True`), the second `respond()` edits it rather than spamming a second message. Calling `message.edit_text` here instead would raise `MESSAGE_AUTHOR_REQUIRED`.

No `try/except` in the handler is deliberate: `get_market_prices()` never raises (§7), and `@safe_handler` covers anything unforeseen. The `status or message` fallback only matters if `respond()` ever returns `None`; a Pyrogram `Message` defines no `__bool__`/`__len__`, so a real message is always truthy.

### 8.1 `.send <count> <user_id>`

`send_messages_command` in `plugins/general.py`. Reads the last *count* messages out of the **private chat** with `user_id` and relays them into whatever chat the command was typed in (`message.chat.id`).

```
.send 10 123456789      # last 10 messages from that PV → here
```

**Arguments are strictly positional and both must be integers — `count` first, then `user_id`.** Anything else (wrong arity, non-numeric) prints the usage text and makes **no** API call. `int()` is used rather than `.isdigit()` on purpose: a negative id parses fine and is then rejected with "this isn't a PV" instead of the misleading "bad syntax".

| Constant | Value | Why |
|---|---|---|
| `MAX_SEND_COUNT` | 50 | Telegram accepts 100 ids per forward call; 50 is ample and halves the flood/accidental-bulk-send risk. `count` above this is refused before any network call. |
| `SEND_SCAN_HEADROOM` | 25 | Service and empty messages can't be forwarded, so history is scanned with `limit=count + 25` to backfill the skipped ones. It also bounds the scan — a history full of service messages can't spin forever. |
| `COPY_DELAY` | 0.4 s | Only used on the fallback path (below), where each message is a separate request. |

**Behavioural rules.**

1. **Peer accessibility is checked first.** `client.get_chat(target_id)` runs before any history read, catching `PeerIdInvalid`, `PeerIdNotSupported`, `ValueError` and `KeyError` — the account must have interacted with that user before, or the `access_hash` simply isn't in the session. `ValueError` comes from `utils.get_peer_type` for out-of-range ids (e.g. `0`); note this Pyrogram version has no `PeerIdNotHandled` class. The call also supplies the display name for the summary.
2. **PV only.** `PRIVATE` and `BOT` chat types pass; a group/channel id is rejected explicitly so an unexpected result never looks like "empty history".
3. **Chronological reorder.** `get_chat_history` yields newest-first, so the id list is reversed — the relayed conversation reads oldest→newest, *and* the ids end up ascending, which is the order batch forwarding expects.
4. **Batch forward is the primary path.** `client.forward_messages(dest, source, ids)` is one request for the whole list: minimal FloodWait exposure, and media, captions, entities and album grouping survive untouched — no manual Markdown/HTML parsing anywhere in the relay path.
5. **`ChatForwardsRestricted` → per-message copy.** `_copy_messages_one_by_one()` re-sends each message with `copy_message` (no "forwarded from" header), sleeping `COPY_DELAY` between requests and handling `FloodWait` locally with a single retry. Per-message failures (expired media, etc.) are logged and skipped rather than aborting the batch; the function returns how many actually landed. If *nothing* lands, the user is told the chat is protected.
6. **Self-chat safety.** If the command is run inside the target PV itself, the command message *and* the status message are both in that history — both ids are excluded so the command can't relay itself.
7. **Honest reporting.** Fewer relayed than requested (short history, skipped service messages, partial copy) appends a note stating the shortfall instead of silently under-delivering.
8. **The summary is sent with `ParseMode.DISABLED`.** It interpolates the target's display name, which is arbitrary user text; an `_` or `*` would break Markdown rendering or make Telegram reject the message. The summary needs no formatting, so the parser is switched off entirely (§10.6). The usage/error strings only interpolate integers, so they keep their backticks.

Access is `AUTH_FILTER`, matching every other utility command. **Be aware of what that means:** an authorized third party can pull messages out of *any* of the owner's private chats into a chat they can see. That is a deliberate consequence of the requested access level, not an oversight — narrow the decorator to `filters.me` if that reach is unwanted.

---

### 8.1.1 `.del <n>` — why it is batched

Telegram accepts at most **100 message ids per delete call**, and Pyrogram does not chunk:
`delete_messages()` passes the whole list into a single `messages.DeleteMessages` /
`channels.DeleteMessages`. So `.del 150` did not delete 100 and fail on the rest — the request was
rejected outright and **nothing** was deleted. `_delete_in_batches()` now slices into 100s, and a
failed batch does not abort the remaining ones.

`MAX_DELETE_COUNT = 200` is a new ceiling on top of that. It bounds both the history scan and the
blast radius of a typo, in the same spirit as `MAX_SEND_COUNT`.

`_can_delete_others()` wraps the `get_chat_member` call that decides whether we may delete other
people's messages. It used to be unguarded, so losing access to a group turned `.del` into an
exception; now the failure degrades to "own messages only", which is the safe direction.

---

## 8.2 TTL media guard (`plugins/ttl_guard.py`)

Saves self-destructing and view-once media from private chats before it can expire. Off switch: `TTL_GUARD=false`.

**The actual mechanism, which is not the one usually described.** Telegram delivers the file to the client like any other; `ttl_seconds` on `MessageMediaPhoto`/`MessageMediaDocument` is the only marker, and destruction is the *official client's* job, not the server's. But the part that decides whether this works is timing, not decryption: per [core.telegram.org/api/views](https://core.telegram.org/api/views), the client calls `messages.readMessageContents` when the user opens the media, and **that** call starts the timer — after it, the file reference stops working. So this module never calls `readMessageContents`. It downloads on arrival and the media stays "unviewed" from the sender's side. Values: `0 < ttl < 0x7FFFFFFF` is a timer, `ttl == 0x7FFFFFFF` is view-once.

**It is a raw handler, and that is forced by a library gap.** `Message._parse` passes `ttl_seconds` through for photos (`message.py:670`) and non-round videos (`:712`) — and drops it for **voice, video notes, animations and documents**. From `on_message`, a view-once voice is indistinguishable from an ordinary one (verified: parsing a raw one-time voice and a raw one-time video note yields `ttl_seconds` *absent* on both). On the raw update, `ttl_seconds` sits on the media object itself and every type looks the same. One field therefore covers every kind, and all of them are handled: photo, video, video note, voice, animation, audio and plain document.

**The download does not go through `Message._parse` either.** `inspect_media()` builds the `FileId` straight from the raw `Photo`/`Document` — byte-for-byte the same id Pyrogram's own `Photo._parse`/`Voice._parse`/`VideoNote._parse` produce (asserted in the headless check), so it is the same download path, just reachable without the high-level parser. Two things are gained. The parsed objects throw away exactly what a faithful re-upload needs — a `VideoNote` keeps no `ttl_seconds`, and nothing keeps the waveform — while the raw `DocumentAttribute*` list carries duration, dimensions and `round_message`. And a photo that Pyrogram cannot describe at all (`Photo._parse` raises IndexError when no `PhotoSize`/`PhotoSizeProgressive` is present, §11.4) can still be rescued here.

**Faithful re-upload is the whole fix for video notes.** Pyrogram's senders default duration and dimensions to zero, and `send_video_note` defaults `length=1` — so the previous version saved every round video as a **1×1 pixel, 0-second circle** and every one-time voice as a 0:00 clip. Every sender is now given the original values (`duration`, `width`/`height`, `length`, `performer`/`title`, `supports_streaming`). `ttl_seconds` is deliberately *not* passed to `send_photo`/`send_video`: the rescued copy must not inherit the timer it was rescued from.

**A failed download does not raise — it returns an empty buffer.** `Client.get_file` catches its own exceptions and only logs them (`client.py:1019`), so `handle_download` sees a generator that simply ended and returns a zero-byte `BytesIO`; the old `if buf is None` guard was dead code, and an expired file reference was re-uploaded to Saved Messages as a 0-byte file. Every download therefore checks the byte count against the declared size.

### 8.2.1 The download is a race, and the fast route is how it is won

`_fast_download()` issues `upload.getFile` on the **main session**. Pyrogram's own downloader builds a *brand-new `Session` for every single download* (`client.py:865`) — TCP connect, `Ping`, `InvokeWithLayer(InitConnection(...))`, plus a full `Auth` handshake and `ExportAuthorization`/`ImportAuthorization` when the file lives on another DC — all of it **before the first byte**. On a slow or proxied link that is comfortably a second.

That second is the whole feature. This handler is racing the account's *own other devices*: the moment the phone opens the media it calls `messages.readMessageContents` and the server invalidates the file reference, mid-transfer if necessary. The bigger the file, the longer the transfer, the more likely the race is lost — which is why a 20 KB one-time voice survived while a multi-megabyte video note or view-once photo did not. Removing the session setup is the only latency this project actually controls.

The fast route deliberately refuses two cases and raises `_NeedsSlowPath` for them: a file on another DC, and a `upload.FileCdnRedirect` (its own session, key and hash verification). Pyrogram's downloader implements both, so it stays as the second route rather than being replaced. Files over `MAX_FAST_BYTES` (32 MB) also go the slow way: a large transfer over the main session competes with update delivery, and a race that long is already lost.

Its second benefit is that RPC errors **propagate**. `FILE_REFERENCE_EXPIRED` reads as itself instead of being logged inside the library and dropped.

### 8.2.2 Routes, and why failure is reported rather than logged

`_acquire()` walks: fast → library, then one `messages.GetMessages` re-read (which does not mark anything as read) for a fresh file reference → fast → library, then the high-level `Message._parse` + `download_media` as a last route. The re-read also covers an update that arrived with no payload at all — `photo`/`document` is `flags.0` on both media types and can legitimately be absent. Every route appends its reason to one string.

If they all fail, `_report_failure()` sends a Persian message **to Saved Messages** naming the sender, the kind, the timer and that reason string, and `.ttl` keeps the last eight outcomes. This is not decoration: a rescue happens once and cannot be retried, the media is gone before anyone reads a log, and a missed one is invisible by construction — nothing arrives, which looks exactly like nothing having been sent. `TTL_GUARD_DEBUG=true` attaches the raw media structure to that report, which is the only way a *future* failure is diagnosable rather than guessable. The same flag logs private media the pinned layer cannot describe (`messageMediaUnsupported`), which is the one hypothesis this module cannot test for itself — layer 158 predates one-time voice and video messages (rolled out January 2024), so a format newer than the library would surface exactly there.

**The sender is identified the same way as in a deletion report** — `_sender_block()` prints the display name and then hands off to `helpers.contact_lines()` for the `@username`, the phone number, or the numeric id when neither exists (§5.2). Here the raw `users` map from the update already carries all three, so it costs no round trip. The username used to be glued onto the display name and the numeric id was omitted on the grounds that it was noise; both reports now say the same things in the same order, and a sender with no public username stays identifiable by something he cannot change.

**The upload cascade degrades rather than dropping the media.** A round video Telegram refuses as a video note is re-sent as a square video, and anything at all is re-sent as a document; each step down names itself in the caption, so a degraded rescue never looks like a clean one.

**`group=-1` is load-bearing — do not change it to 0.** `dispatcher.py:255` breaks out of the handler loop after the first handler that matches, i.e. **one handler per group**, and a `RawUpdateHandler` claims every update unconditionally (`:232`). In group 0 it would swallow every update and silently disable the logger, auto-seen and all commands. Groups are iterated in sorted order (`OrderedDict(sorted(...))`, `:177`), so a negative group both isolates this handler and runs it *before* group 0 — which matters because `auto_seen`'s `read_chat_history` lives there and nothing that touches the chat should run before the download. Verified: raw handler alone in group −1, 28 message handlers untouched in group 0.

Other details: re-uploads rather than forwards, because forwarding TTL media either gets rejected or carries the timer along; `send_video_note` is the one send method with no `caption` parameter, so video notes get a separate follow-up message — sent inside its own `try`, so losing the caption cannot send the media down the fallback path and upload it twice; any send that the specific format rejects falls back to `send_document`; a `(chat_id, message_id)` set with a 600 s TTL stops re-delivered updates from saving twice. Failures only log and increment a counter — this handler runs on *every* update and must never raise.

Two details about text that comes from the other party. The report is HTML + `html.escape()` (§10.8), and it is **length-budgeted**: a media caption is capped at 1024 characters and escaping quadruples a `<`, so a long original caption used to make Telegram reject the upload — and with it the rescue. The raw text is cut *before* escaping, since cutting escaped text can split an `&amp;` in half. And `DocumentAttributeFilename` is arbitrary sender-chosen text that ends up as a file name: `_safe_name()` strips directory separators and leading dots, because `a/../../../evil` would otherwise walk straight out of `data/ttl_media/`. The same function decides the buffer's name, which matters beyond the local copy — `send_voice` re-guesses the mime type with `guess_mime_type(voice.name)`, so a name whose extension disagrees with the payload uploads an OGG labelled as an MP3. `_upload_name()` keeps the sender's name only when it agrees with the document's mime type and otherwise synthesises one from it.

**Local copies expire.** With `TTL_GUARD_LOCAL_COPY` on, `data/ttl_media/` used to grow without any ceiling — on a small server the feature slowly filled the disk. `_sweep_local_copies()` deletes files older than `TTL_MEDIA_RETENTION_DAYS` (default 2) and runs *on write*, because writing is the only thing that makes the directory grow. Directories are left alone and per-file errors are swallowed: cleanup must never endanger the rescue that triggered it. The Saved Messages copy is unaffected — that is the durable one.

**Scope note.** This deliberately retains media the sender expected to disappear, and Telegram sends them no notification. That is the point of the feature, but it is why it is a single env flag away from being off.

---

## 8.3 Member control and global actions (`plugins/group_admin.py`)

Three layers: per-group commands, global commands that fan out across every managed group, and a handler that keeps a global listing enforced over time.

### 8.3.1 What the library actually does

The library's method names hide two facts that decide the shape of every command here, plus one that only appeared when the library was swapped (§2.1).

**There is no "kick".** `.kick` is `ban_chat_member` followed immediately by `unban_chat_member`. But `ban_chat_member` branches on peer type (`ban_chat_member.py`): for a **channel/supergroup** it invokes `channels.EditBanned`, while for a **basic group** it invokes `messages.DeleteChatUser` — which is already just a removal, not a ban. Meanwhile `unban_chat_member` and `restrict_chat_member` *only* speak `channels.EditBanned`. So in a basic group the un-ban step is both unnecessary and impossible, and `.kick` skips it. `_SUPERGROUP_ONLY_ACTIONS` and the explicit `ChatType.GROUP` guards in `.unban` / `.mute` / `.unmute` exist for the same reason: a clear Persian "supergroup only" beats an opaque serialization failure.

**Telegram silently reinterprets short and long durations.** Anything under 30 s or over 366 days from now is treated as *forever*. A user typing `.mute 10s` does not mean forever, so `_split_duration_and_reason()` rejects those bounds instead of quietly doing something else. When no duration is given, `until_date` is **not passed at all** (`_until_kwargs()` returns `{}`) so Pyrogram's own `utils.zero_datetime()` default stands.

`MUTED_PERMS` is a bare `ChatPermissions()`: `restrict_chat_member` converts each field with `not permissions.can_*`, so all-`None` becomes all-banned. `UNMUTED_PERMS` is the inverse — every field `True`, so no *user-level* restriction remains.

**`UNMUTED_PERMS` is built from `ChatPermissions`' own signature, not written out.** The field list grew from 8 to 17 (separate audio/photo/video/voice-note rights, reactions, topics), and since `write()` bans everything it finds unset, the old hardcoded list would have left each newly added right *restricted* after `.unmute` — a half-unmute that nothing reports. `can_send_media_messages` is excluded on purpose: it is a deprecated alias that blanks the granular `send_*` rights and logs a warning on every call. Verified after the change: unmute emits `ChatBannedRights` with no restriction set at all, mute sets 19 of 22 (`view_messages` deliberately not among them). That is not "everything allowed": the chat's own `default_banned_rights` still apply, which is exactly what lifting a restriction should mean.

### 8.3.2 Finding the managed groups without N requests

`_scan_managed_groups()` pages through **raw** `messages.GetDialogs` rather than `client.get_dialogs()`. The reason is a lossy parse: `Chat._parse_channel_chat` keeps `is_creator` but **drops `admin_rights`**. Going through the high-level generator would therefore mean one extra `get_chat_member` per group to find out whether we can restrict anyone there — dozens of extra requests fired at the exact moment we are about to fire dozens of restrict requests. On the raw response, `admin_rights` sits right next to each chat.

`_admin_group_from_raw()` accepts a chat when `creator` is set or `admin_rights.ban_users` is true, and rejects: broadcast channels (no members to restrict in the group sense), `left`/`deactivated` chats, and basic groups with `migrated_to` set (superseded by their supergroup, which appears separately). Ids are rebuilt with `utils.get_channel_id(id)` for channels and `-id` for basic groups, matching `utils.get_peer_id`.

Pagination copies `get_dialogs.py`'s cursor logic, plus two guards it lacks: a hard `MAX_DIALOG_PAGES` bound and a `seen` set, so a cursor that fails to advance ends the loop instead of spinning. A `messages.Dialogs` response (rather than `DialogsSlice`) means the full list arrived and the loop stops after one request. The result is cached in RAM for `MANAGED_CACHE_TTL` (300 s) against `time.monotonic()` — `time.time()` would let an NTP jump pin the cache permanently valid or permanently stale, the same reasoning as §7's price cache. `.gstatus` passes `force=True`, since answering a status question from a five-minute cache defeats the question.

### 8.3.3 Global fan-out

`_global_command()` is the shared body of all four global commands. **Order is deliberate: the database write happens before the fan-out.** If the run dies halfway — network, FloodWait, restart — the id is already listed and §8.3.4 finishes the job. The reverse order would leave a half-applied ban with no record of it.

Per-group failures are local: `UserNotParticipant`, `UserAdminInvalid` and `UserCreator` count as *skipped* (there was nothing to do), anything else as *failed*, and neither aborts the loop — one group we lost access to must not cancel the other forty. `FloodWait` sleeps and retries that one group once.

`config.GLOBAL_ACTION_DELAY` (default 350 ms) spaces the requests. This is not politeness: a userbot is a real account, and rapid bulk action against many chats is one of the patterns that gets accounts limited. Progress is edited into the status message every `PROGRESS_EVERY` groups, because a forty-group run is a visibly long wait; failures of that cosmetic edit are swallowed so they cannot break the real work.

### 8.3.4 Enforcement handler — and why it is in group −2

`.gban` only reaches groups the target is in *at that moment*. `enforce_global_flags` closes the gap: a listed user who posts in a managed group gets the action applied there too. `GBAN_ENFORCE=false` turns it off.

**The dispatcher group is load-bearing.** As documented in §8.2, `dispatcher.py` breaks out of the handler loop after the first handler that runs **within a group** (the `break` sits inside `for handler in group`, so later groups still run). Group 0 would mean swallowing group messages ahead of `general`'s commands. Group −1 is already taken by `ttl_guard`'s raw handler, which claims every update unconditionally — and since plugins load alphabetically (`sorted(rglob("*.py"))`), `group_admin` registers *before* `ttl_guard`, so sharing −1 would disable the TTL guard entirely. Hence `GBAN_ENFORCE_GROUP = -2`. Verified after wiring: group −2 holds this handler alone, −1 holds the raw TTL handler alone, and group 0 holds the 40 command handlers.

Other details mirroring existing modules: the membership test lives in a **filter** (`globally_flagged_filter`) so a non-listed sender never enters the handler body — two dict lookups, never disk. `_ENFORCED` is a `(chat_id, user_id)` set with a 900 s TTL, pruned on write like `IGNORE_DELETED_IDS`; without it, every message from a listed user in a group where we are *not* admin would generate a failed request. It is recorded regardless of outcome, since its job is suppressing repetition rather than counting successes, and `_global_command()` clears a user's entries when their status changes so the new state applies immediately. There is deliberately **no `@safe_handler`** here, for the same reason as `auto_seen`: this is not a command, and its error path would post a Persian error into a group we do not administer, once per message.

### 8.3.5 `.fakegban`

A prank module, requested as such. It runs a fake progress animation and reports a plausible group count, and **calls no restriction method and touches no table** — its only API calls are edits to its own message. The group count is read from the (already cached) managed-group list purely so the number looks right. Its entire value is being inert; the docstring says so, and `.help` labels it «شوخی».

### 8.3.6 Access scope

Every command here is `AUTH_FILTER`, matching the other utilities — so **an authorized third party can ban or globally ban people across all of the owner's managed groups**. That follows from the requested access level rather than being an oversight, and it is a much wider reach than `.weather`. Narrow the decorators to `filters.me` if that is unwanted. The one guard that is unconditional is `_guard_self()`: no command will act on the account running it.

---

## 8.4 Bot-chat cleaner (`plugins/bot_cleaner.py`)

A bot you once started keeps the right to message you after you delete the chat, and the only built-in way to stop it is a block — which fills the block list with advertising bots. With `.botclean on` (persisted in `settings`, off by default), a bot message that lands in an otherwise empty chat is deleted **together with the chat entry**, and the bot stays unblocked.

**What "a chat you deleted" means, exactly.** One raw `messages.getHistory` (30 messages) and the chat must hold nothing but the bot's own messages, with: nothing you sent (`out`), no service line, nothing older than `BURST_WINDOW` (10 min) before the newest, and nothing this module saw while the chat was in use. The reasoning is that a bot can only reach someone who wrote to it or granted it access (login widget, mini app, payment), and each leaves a trace — your message or a service line — so a chat with neither was emptied by hand. Telegram's own accounts (`777000`, `@replies`, `@BotFather`, Verification Codes, …) are excluded by id regardless.

`BURST_WINDOW` exists because updates are not handled in order: two ads a second apart can arrive newest-first, and the second must not mistake the first for "older history". It is also why a bot message that sat in the chat from before (downtime, or the switch was off) makes the cleaner leave that chat alone until it is deleted once by hand. That is the deliberate direction to be wrong in — the other direction deletes messages.

**Why the deletion is `messages.deleteHistory`.** With `just_clear` unset it is exactly the official apps' "Delete chat": messages *and* the list entry go. Deleting the messages one by one would leave an empty chat sitting in the list, which is the clutter the feature exists to remove. It deletes everything up to `max_id`, so it is safe only because the read immediately before saw the whole chat (a `MessagesSlice` announcing more than it carries means "keep"): every message up to `max_id` is one of the burst, anything newer has a higher id and is judged by its own handler. `revoke` stays unset, as in the official apps for a bot. The call repeats while the server returns `offset > 0`, bounded by `DELETE_ROUNDS`. The ids are passed to `mark_deletions_ignored()` first, like `.del` — the logger never caches bot chats, so this is belt and braces.

**Only "keep" is cached.** Bots in active use send most bot messages, so a keep verdict is remembered for `KEEP_TTL` (6 h) and costs those messages nothing. A delete verdict is never cached: the read behind it is what proves nothing of yours is in the chat *at that moment*, and that read is the whole safety argument. Your own message in a bot chat, or an incoming service line, sets "keep" with no read at all. A per-bot `asyncio.Lock` makes a burst one read and one delete; handlers queued behind it see their id in `_DELETED` and stop.

**Invalidation without a chat id.** Deleting a chat on the phone arrives as `UpdateDeleteMessages` — ids only, no peer, for every private chat. So the ids of messages seen while a chat was in use go into `_OWNER` (`message_id → bot_id`, bounded), and `forget_deleted` (a `DeletedMessagesHandler` in the same group) drops the bot's verdict when any of them is deleted. The next message is judged afresh, and a remembered message that is still in the history then counts as proof the chat was not cleared. That last rule is not decoration: without it, a bot that deletes your `/start` (some menu bots do), or you deleting one message, would drop the verdict and leave a chat of fresh bot messages that looks exactly like an emptied one. Incoming messages are deliberately *not* tracked before their verdict, since an ad being tracked would protect itself.

**`group=3`.** The watcher matches every message in a bot chat, so it needs a group of its own (§8.2's one-handler-per-group rule); placing it after `logger` (2) means every other handler has seen a message before this one can delete it. There is no `@safe_handler` (§10.5): failures log `bot-clean failed …`, throttled by a `NOISE_RULES` entry, and leave the message alone. `.botclean` keeps per-run counters and the last eight clean-ups — a false positive has to be visible somewhere the user looks, not only in a log.

Known limit: the userbot cannot stop the push notification for the ad; the message is deleted after it has been delivered to your devices.

---

## 8.5 Forward by link (`plugins/link_forward.py`)

`.fwd <link> [link …]` brings the linked message into the chat the command is typed in (inside the same forum topic), then deletes the command message — only the forwarded content remains. With no link and sent as a reply, it reads links from the replied message, hidden hyperlinks (`text_link` entities) included. Owner only (`filters.me`): an authorized user could otherwise pull content out of every private channel the account is in.

**Links.** `t.me` / `telegram.me` / `telegram.dog`, public `/<username>/<id>` (also the `/s/` web form) and private `/c/<id>/<id>`, an optional topic segment, `?single` (that album item only; otherwise the whole album via `get_media_group`, in id order), `?comment=<id>` (resolved through the channel's `linked_chat`), and `tg://privatepost` / `tg://resolve`. A path deeper than topic/message is refused rather than read wrongly, and trailing sentence punctuation does not eat `?single`.

**Private links carry only an id.** The session reaches a chat only through a stored access hash, and a channel that has produced no update since login is not stored. On an unresolvable id `_load_from_dialogs()` pages through raw `messages.GetDialogs` until the channel shows up — `client.invoke` stores every page's chats itself — and the call is retried once. Raw, not `get_dialogs()`, because the high-level generator parses each dialog's top message with its reply: a request per dialog for nothing. Cursor and guards follow §8.3.2.

**The peer must be a channel.** `t.me/<name>/<n>` also resolves for a user or a bot, and the request it turns into (`messages.getMessages`) ignores the peer entirely — it would return message *n* of some private chat of this account. `_fetch()` refuses anything that is not an `InputPeerChannel`, and checks the returned message's chat id as well.

**Protected content.** Telegram refuses to forward from a chat with "Restrict saving content" (`ChatForwardsRestricted`, or the per-message `has_protected_content` flag, checked first to skip a doomed request), but serves the files to every member. So the message is rebuilt: text, polls, locations and contacts through `Message.copy()` (no file involved; source buttons dropped — only bots may send them), a sticker by id first, and every other kind downloaded into a per-run directory under `data/forward_tmp/` and uploaded again with its original duration, dimensions, preview, spoiler, streaming flag, waveform and caption entities (`ParseMode.DISABLED`, so a stray `*` stays a character). A protected album goes back as one `send_media_group`; anything that cannot be grouped goes item by item. The file on disk is named by us (`<id>_<kind><ext from mime>`) because Pyrogram joins the sender's file name onto the directory unchecked — `../../x` would escape — and the extension decides the mime type the senders re-guess; the original name travels only as the upload's `file_name` label. A file above the account's upload ceiling (2000 / 4000 MiB) is refused before downloading. Progress is edited into the command message at most every 4 s with direct, failure-swallowing edits — `respond()` would sleep out a FloodWait inside the transfer loop it is reporting on.

**Echo guard.** A copy is a new message of ours. If its text or caption reads `.fwd <link>`, it matches this very command, and a post linking to itself would forward itself forever. Forwards are excluded by the filter (`~filters.forwarded`); copies register `(chat, text)` in `_ECHOES` *before* sending and the handler consumes the entry — the one-shot pattern of `IGNORE_DELETED_IDS` (§5.4), for the same reason: the update can arrive before the send call returns.

**Failures are per link.** A `LinkError` carries an English reason for the log and a Persian `.text` for the user (keeping the §10.2 language split without a second `MediaError`); other RPC errors are reported with their name, and a `FloodWait` stops the batch and marks the rest as not done. On any failure the command message is replaced by a per-link report instead of being deleted; the scratch directory is removed in a `finally` either way, and stale ones left by a killed run are swept after a day.

---

## 9. Running

```bash
cp .env.example .env          # then fill in API_ID, API_HASH, AI_API_KEYS
./venv/bin/python main.py     # local
docker compose up --build     # container
```

Required env: `API_ID`, `API_HASH`. Optional: `LOG_LEVEL`, `AI_API_KEYS`, `AI_MODEL`, `AI_IMAGE_MODEL`, `AI_TTS_MODEL`, `AI_TTS_VOICE`, `AI_TIMEOUT`, `AI_MAX_TURNS`, `AI_MAX_CONCURRENCY`, `AI_MEDIA_MAX_MB`, `CLOCK_SYNC`, `CLOCK_SYNC_INTERVAL`, `CLOCK_SYNC_TIMEOUT` (§11.2), `TTL_GUARD`, `TTL_GUARD_LOCAL_COPY`, `TTL_GUARD_DEBUG`, `TTL_MEDIA_RETENTION_DAYS` (§8.2), `DELETE_CACHE_MEDIA`, `DELETE_CACHE_MAX_MB`, `DELETE_CACHE_TOTAL_MB` (§5.3), `DISPLAY_TIMEZONE` (§10.11), `GBAN_ENFORCE`, `GLOBAL_ACTION_DELAY_MS` (§8.3).

**Optional values never fail the boot.** `_optional_int`/`_optional_bool` log an English warning and
fall back to the default, because a typo in `AI_TIMEOUT` should not take the bot down. They also
*clamp*: `AI_MAX_CONCURRENCY=0` used to build an `asyncio.Semaphore(0)` and park every AI command
forever with no error anywhere, so the floor of 1 is load-bearing rather than cosmetic. Only
`API_ID`/`API_HASH` are fail-fast, and they raise `ConfigError`, which `main.py` turns into a
one-line message instead of a traceback.

**Exit codes** (they matter for systemd/Docker restart policies): `2` configuration, `3` cannot
create `data/`, `4` database init, `5` Telegram client start, `1` anything unhandled.

**Two env traps, both hit in practice and both now guarded in `config.py`:**

- The legacy `AI_API_KEY` (singular) in existing `.env` files still holds the *old gateway's* key. Config reads only `AI_API_KEYS` (plural); reading the singular one sent an invalid key to Google and took the entire assistant offline with `API key not valid`.
- Likewise `AI_MODEL=gpt-5-mini` survives in old `.env` files and produces a 404 from Gemini for every request. `_model_name()` ignores any value that is not a Google model name or a known alias, logs a warning, and falls back to the default.

`ffmpeg` is optional (see §6.4) and is installed in the Docker image.

Startup blocks for up to `CLOCK_SYNC_TIMEOUT` (+ the same again if the HTTP fallback is used) before the client starts, because the offset must be known *before* the first MTProto message is built. On a network that blocks UDP/123 this is ~8 s of dead time per start; set `CLOCK_SYNC=false` to skip it entirely once the host clock is known-good.

`data/` (session + `logger.db`) is gitignored except for `data/.gitkeep`, and persists via the compose bind-mount `./data:/app/data`. **`logger.db` contains real private messages and `my_account.session` is full account access — never commit either.**

Compose used to mount the whole project (`./:/app`), which overlaid the image's code with the host's and made `build` largely decorative. Now that all mutable state is confined to one directory, only that directory is mounted; code ships in the image, so **code changes need `docker compose up --build`**.

---

## 10. Coding standards

1. **Async only.** No blocking I/O in handlers.

2. **Language: Persian goes to Telegram, English goes to the log.** Every string a user reads in a
   chat is Persian; every `log.*` call, every exception message, and every startup diagnostic is
   English. The split exists because logs get pasted into terminals and issue trackers where
   right-to-left text reorders itself and becomes unreadable, while the bot's users are Persian
   speakers. `utils/helpers.py` is where the two meet, and it is the only file that should contain
   much of both.

   The one deliberate exception is `MediaError` (`utils/media.py`): its message *is* the user-facing
   text — `ai_context.collect()` puts it straight into `.notes` and the handler prints it verbatim.
   Its docstring says so. Don't add a second exception without the same justification.

   Technical detail may still reach the user, wrapped in a Persian sentence — `safe_handler` shows
   `❌ خطای غیرمنتظره…` plus an escaped `KeyError: …`. That is intentional: the sentence tells the
   user what happened, the detail makes the bug reportable.

3. **Utils return display-ready Persian strings and never raise into handlers.** `weather_api`,
   `gold_api`, `azan_api` and `currency_api` all follow this; keep it. Note the contract is about
   *raising*, not about hiding: the Persian return value says something went wrong, and the English
   cause goes to the log. Never interpolate a raw exception into the returned string — it is English
   noise inside a Persian sentence and its punctuation can break Telegram's Markdown parser.

4. **`respond()` from `utils/helpers.py`, never `message.edit_text`.** `AUTH_FILTER` admits
   authorized third parties whose messages are *incoming*, and you cannot edit another user's
   message — `respond()` picks edit vs reply automatically. It also **never raises**: it clamps
   over-long and empty text, retries once without a parser when Telegram rejects the formatting,
   falls back to a reply when the anchor is gone, caps `FloodWait`, and returns `None` if nothing
   worked. That total guarantee is required, because `safe_handler` calls `respond()` to *report*
   errors — an exception escaping there would be an error inside the error path.

5. **Wrap command handlers in `@safe_handler`** (below the `@Client.on_message` decorator) so
   failures log a traceback and show a short Persian error instead of dying inside Pyrogram's
   dispatcher. **Do not put it on handlers that run on every message** — `logger`, `auto_seen`, `bot_cleaner.watch_bot_chats` and
   `group_admin.enforce_global_flags` deliberately omit it, because its error message would be
   posted into the other party's chat once per message. Those four carry their own `try/except`
   that only logs.

6. **`FloodWait.value` is not guaranteed to be a number.** `RPCError.__init__` does
   `try: int(value) except: self.value = value`, and `FloodWait().value` is `None` — so
   `await asyncio.sleep(e.value)` can raise `TypeError` *inside* an exception handler. Always go
   through `helpers.flood_seconds()`, and always compare against a `FLOOD_WAIT_LIMIT` before
   sleeping: an unbounded sleep on a multi-thousand-second wait parks the handler for hours.

7. **Guard optional attributes.** `message.from_user` is `None` for anonymous and service messages.

8. **Escape interpolated user content.** Prefer HTML + `html.escape()` over Markdown for anything
   containing arbitrary text. When the text is long *and* escaped, budget the length too —
   `html.escape` quadruples `<`, so 4096 chars of `<` becomes 16384 and Telegram rejects the whole
   message (§5.2).

9. **Comments are Persian, identifiers English.** Explain *why*, not *what*.

10. **New dependency ⇒ pin it** in `requirements.txt`.

11. **Never format a user-facing time with `datetime.now()` or
    `datetime.fromtimestamp()`.** Both read the process's timezone, and the
    container runs on UTC — that is how every reported time was off by exactly
    Tehran's +3:30. `helpers.time_text()` (and `local_time()`) apply
    `DISPLAY_TIMEZONE` explicitly, so the output is right whether the bot runs
    in Docker or straight from a shell, and an unknown zone degrades to the
    system clock with one warning instead of raising. `TZ` in
    `docker-compose.yml` and `tzdata` in the image exist for the *log* lines,
    which the logging module stamps itself; the Telegram-facing times do not
    depend on either.

11. **Schema change ⇒ idempotent migration** in `init_db()`.

### Testing

There is no test suite in-repo; verify headlessly before shipping:

- Handlers are plain async functions — call `plugins.<mod>.<func>` directly with a fake `message` exposing `chat`, `command`, `from_user`, `outgoing`, `edit_text`, `reply_text`, plus `reply_to_message` and the media attributes (`photo`, `voice`, …) for the AI commands. The fake client needs `download_media(in_memory=True)`, `get_chat_history()` and the `send_photo/send_voice/send_audio` used by `.img`/`.tts`.
- Media fixtures need no libraries: `.tts` output is raw PCM you can wrap with `pcm_to_wav()` for an audio fixture, and a valid PNG can be built with `zlib` + `struct` alone. Round-tripping `.tts` → `.stt` exercises both directions against the real API.
- `@Client.on_message` on the class returns the function unchanged and attaches `.handlers`; assert that attribute to confirm registration without connecting.
- Point `db_manager.DB_PATH` at a temp file. **Never test against the real `data/logger.db`.**
- For a boot check, run `main.py` as a subprocess with stdout to a **file** (a pipe blocks and orphans the process, which then reparents to `launchd` and stays connected to Telegram), then send `SIGINT`.

---

## 11. Runtime resilience (`utils/pyrogram_patch.py`, `utils/clock.py`, `utils/logging_setup.py`)

Four problems that live *below* the handler layer, where `@safe_handler` cannot reach: an old library had aged out of Telegram's current id space, one unparsable photo can drop an entire update, MTProto refuses to work with a wrong clock, and an unstable network produces more log lines than signal. The first is now fixed upstream (§2.1) and its patch skips itself; it is kept because it only ever *widens* a range and would cost nothing if a future pin regressed. All of them are addressed at boot in `main.py`, in this order:

```
patch_peer_id_ranges()               # §11.1 — before Client()
patch_tolerant_photo_parse()         # §11.4 — before Client()
install_asyncio_exception_handler()  # §11.3
_sync_clock_once()                   # §11.2 — before app.start()
```

### 11.1 Peer id ranges — a dropped-update bug, not just log noise

**Fixed upstream since the move to kurigram (§2.1); `patch_peer_id_ranges()` now logs one INFO line and returns False.** Kept for the reasons above, and documented because the failure mode is worth recognising. `pyrogram/utils.py` used to hardcode the valid id ranges, assuming channel ids fit in 2³¹:

```python
MIN_CHANNEL_ID = -1002147483647     # ceiling too low
MIN_CHAT_ID    = -2147483647        # ceiling too low
```

Telegram has since expanded that space, so `get_peer_type(-1003785028914)` raises `ValueError: Peer id invalid`. **This silently loses messages.** `Client.handle_updates` calls `resolve_peer(get_channel_id(...))` for any channel message whose peer is not already in `my_account.session`, and it catches only `ChannelPrivate` — the `ValueError` escapes the background task, and that update never reaches the dispatcher. The visible symptom is `Task exception was never retrieved … KeyError('ID not found: …')` chained to `ValueError`, with **zero project frames** in the traceback, which is exactly why it reads as harmless library noise.

`patch_peer_id_ranges()` widens both floors to TDLib's `DialogId` ranges — every peer kind is below 10¹², matching Pyrogram's own untouched `MAX_USER_ID = 999999999999`:

| kind | range after patch |
|---|---|
| basic group | `[-999999999999, -1]` |
| channel / supergroup | `[-1999999999999, -1000000000001]` |

The two intervals stay disjoint and the check order in `get_peer_type` is untouched, so **every previously-valid id resolves exactly as before**; only ids that used to raise now resolve. Genuinely invalid ids (`0`, `-1000000000000`, `-2·10¹²`) still raise. Nothing else in Pyrogram consults these constants — `fetch_peers` and `sqlite_storage.get_peer_by_id` classify from the raw TL type and the stored `type` column — so widening `get_peer_type` is the whole fix.

The patch only ever *widens*: if a future version ships a broader range, it is left alone and the function returns `False`.

### 11.2 Clock offset

`msg_id` is derived straight from the client clock, so a slow clock earns `BadMsgNotification[16]` ("msg_id is too low") and the session stops working. The library has **no** time correction at all — `session/internals/msg_id.py` reads `time.time()` directly — and inside Docker the clock belongs to the host kernel and cannot be set from the container. So the offset is measured and applied **only to msg_id generation**; the system clock and everything else in the process are untouched.

**How it is applied is the lesson from the library swap.** The patch used to be a line-for-line copy of upstream's `MsgId.__new__` with the offset added, which silently coupled it to that function's internals: 2.0.106 keeps `offset`/`last_time` class attributes, the fork computes from a single `_last_msg_id`. The old copy raised `AttributeError` on *every outgoing message* — a total outage caused by a patch whose only job was to add a number. It now replaces the **`time` module reference inside that one module** (`_ShiftedClock`) instead of reimplementing the algorithm: both versions read the clock through `time.time()` there, so the patch no longer knows or cares how msg_id is built. Verified on both libraries: the offset shifts msg_id by exactly the requested seconds and ids stay strictly increasing.

One asymmetry comes with the fork's monotonic watermark: raising the offset takes effect at once, lowering it only once real time passes the highest id already sent. In the case the reversal exists for — the host's own clock being fixed — the system clock jumps forward by the same amount the offset drops, so it is a non-event. Forcing the watermark down is deliberately *not* done: it would emit a msg_id below one already sent, which is the very error this patch prevents.

`utils/clock.py` is deliberately Pyrogram-free (testable standalone, like `ai_engines`) and never raises: worst case it returns `None` and the bot runs on the system clock. It tries two transports in order:

| Order | Transport | Accuracy | Why |
|---|---|---|---|
| 1 | SNTP, UDP/123 | sub-ms | correct answer when it works |
| 2 | HTTP `Date` header (HEAD) | ~±0.5 s | many networks block outbound UDP — measured: UDP/123 times out on the dev host while all three HTTPS probes agree within 0.3 s |

±0.5 s is ample: Telegram tolerates roughly ±30 s and `CLOCK_SYNC_THRESHOLD` is 5 s. Servers within each stage are queried **concurrently**, so the timeout is per stage, not per server.

Defensive details, all of which exist because a wrong offset breaks a working bot just as thoroughly as no offset:

- **Cross-checking.** With ≥2 responses, values more than `AGREEMENT_TOLERANCE` from the median are discarded and the rest averaged; a lone response is used but logged as unverified. The HTTP stage gets a wider tolerance (3 s) because `Date` is second-granular. A stale cached `Date` from one proxy therefore cannot win on its own — and `Cache-Control: no-cache` is sent anyway.
- **The offset is absolute, not cumulative.** Every measurement uses raw `time.time()`, so each result already means "system vs. truth"; `apply_time_offset()` assigns rather than accumulates.
- **It is also reversible.** If a later measurement lands under the threshold the offset is reset to `0.0` — otherwise a host that got its NTP fixed would keep our stale correction and re-create the exact bug.
- **Re-measured every `CLOCK_SYNC_INTERVAL`** (default 30 min), because VM clocks drift after the host sleeps. Repeat detections of an unchanged offset drop to `DEBUG`; only a *change* of state warns.
- **`MsgId.time_offset`** is named to avoid colliding with upstream's `MsgId.offset`, the intra-second counter in the older implementation.

### 11.3 Noise throttling

Nothing is discarded outright; repetition is. Each known-noise pattern is shown **once per window** and the suppressed count is appended to the next line that gets through, so a real outage still shows up — as one line instead of two hundred. Rules live in `NOISE_RULES` and carry an English `note` that turns the library's message into something actionable (the `BadMsgNotification` line, for instance, gains a pointer to `CLOCK_SYNC`).

Three rules match **our own** log lines rather than Pyrogram's — `plugins.auto_seen` on
`"auto-seen failed"`, `plugins.group_admin` on `"global-list enforcement failed"` and
`plugins.bot_cleaner` on `"bot-clean failed"`. All three handlers run on every message and
deliberately have no `@safe_handler` (§10.5), so a log line is their only output and an
unreachable chat would otherwise produce one per message. **The needle is a literal substring of
the emitting `log.warning`,** so changing any of those messages silently disables its rule;
nothing in the boot test catches that. A comment sits at both ends saying so.

The filter is installed on the **root handler, not on loggers**, because a logger's filters do not apply to records from its children — `pyrogram.session.session` records would bypass a filter attached to `pyrogram`. It throttles on `time.monotonic()`: a project whose central problem is an untrustworthy wall clock must not build its rate limiter on one. Matched records lose their traceback (these failures are known and one line says everything) and are re-levelled per rule; **anything unmatched passes through completely untouched**.

Covered: dropped sockets and reconnects (`asyncio`, `pyrogram.connection`), request retries, `CHANNEL_PRIVATE` from `Message._parse` fetching a reply in a chat we have been removed from, unresolvable peers, and repeated `.addseen` failures for a chat that went out of reach.

`install_asyncio_exception_handler()` covers the other channel: Pyrogram dispatches every update through `loop.create_task`, so an escaped exception surfaces as `Task exception was never retrieved` with a full traceback and no project frames. The handler is `loop.set_exception_handler` — an official asyncio hook, **not** a monkeypatch — and collapses only a fixed set of known cases (peer-resolution failures, `OSError`/`TimeoutError`, a few Pyrogram RPC errors matched **by class name** so this module stays decoupled from `pyrogram.errors`). `KeyError` and `ValueError` are matched on message text, never on type, since suppressing those wholesale would hide real bugs. Everything else goes to `loop.default_exception_handler` exactly as before.

### 11.4 Photos Pyrogram cannot describe

`types.Photo._parse` keeps only `PhotoSize` and `PhotoSizeProgressive` entries and then takes the largest with `main = photos[-1]`, so a photo carrying neither — one that arrives with just a stripped preview, say — makes that an `IndexError` (reproduced directly against the pinned version).

The failure does not stay inside the photo. `Dispatcher.handler_worker` parses every update **before** it runs any handler, and that parse sits inside the *same* `try` block as the handler loop (`dispatcher.py:205-260`), so one unparsable photo drops the whole update: the TTL guard, the anti-delete logger and every command miss it, and the only trace is a library-level traceback with no project frames — the same signature as §11.1. Web-page preview photos reach the same parser (`web_page.py:140`), so this is not limited to timed media.

`patch_tolerant_photo_parse()` turns the crash into "no photo object", which the rest of Pyrogram already handles (`Photo._parse` returns `None` for a `PhotoEmpty`). It wraps the original rather than reimplementing it, catches **only** `IndexError`, and is idempotent via a `_tolerant` marker on the replacement. It pairs with §8.2: the TTL guard builds its file id from the raw media, so it can still rescue a photo the high-level parser gave up on.
