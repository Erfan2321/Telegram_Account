"""
Fixed project paths — the single source of truth for where state lives.

Why a separate module rather than config.py?
    config.py performs fail-fast validation on import (a missing API_ID
    raises). db_manager only needs a path, not the whole configuration; if it
    imported config, testing the database would be tied to having Telegram
    credentials set.

    So this module deliberately has no dependencies at all — not config, not
    dotenv, not pyrogram. Only pathlib.

Why an absolute path and not a relative 'logger.db'?
    A relative path resolves against the current working directory, not the
    project. Starting the bot from another directory (systemd, cron, an IDE
    with a different working directory) created a fresh, empty database, and
    the message cache and the authorized-user list silently looked empty.
    Anchoring to __file__ removes that entire class of bug.
"""

from pathlib import Path

# Project root: one directory above utils/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# All runtime state collects here: the database and the Telegram session file.
# Keeping state out of the source tree means one directory to back up, one
# path to mount in Docker, and a root that holds nothing but code.
#
# The contents are fully gitignored: the session file grants complete access
# to the Telegram account, and logger.db holds real private messages.
DATA_DIR = PROJECT_ROOT / "data"

# SQLite database (message cache, authorized users, AI conversation memory).
DB_PATH = DATA_DIR / "logger.db"

# Local copy of timed media (only when TTL_GUARD_LOCAL_COPY is on).
# Deliberately inside data/ so the same gitignore rules cover it — its
# contents are precisely what the sender wanted not to persist.
TTL_MEDIA_DIR = DATA_DIR / "ttl_media"

# Media pre-downloaded for the anti-delete report (DELETE_CACHE_MEDIA). Same
# reasoning for the location: it holds other people's private media, so it
# belongs under the directory the gitignore rules already cover.
DELETED_MEDIA_DIR = DATA_DIR / "deleted_media"

# Pyrogram session name. Pyrogram builds the real filename itself from
# workdir + name: data/my_account.session
SESSION_NAME = "my_account"


def ensure_data_dir() -> Path:
    """
    Create the data directory if it does not exist.

    Neither sqlite nor Pyrogram creates a missing parent directory when
    opening a file — both just fail — so this has to be called before either
    of them. The operation is idempotent and safe to call repeatedly.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
