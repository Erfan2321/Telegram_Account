"""
UserBot entry point.

The order of operations in this file is deliberate — each step depends on
the one before it:

    load_dotenv()                        env vars before any reader
    setup_logging()                      so later failures are *logged*, not printed
    import config                        fail-fast configuration validation
    patch_peer_id_ranges()               before Client() is built (§11.1)
    patch_tolerant_photo_parse()         before Client() is built (§11.4)
    install_asyncio_exception_handler()  before any background task
    measure_offset()                     before app.start() (§11.2)
    init_db()                            before the first update arrives
    Client(...) / app.start()

Language rule: every log line in this file is English. Nothing here is sent
to Telegram; it all lands on a terminal or in the container log.
"""

import asyncio
import logging
import sys

from dotenv import load_dotenv

# .env must be read *before* logging is configured, so that LOG_LEVEL can be
# set from the file too and not only from the shell environment.
load_dotenv()

from utils.logging_setup import install_asyncio_exception_handler, setup_logging  # noqa: E402

# Installs the noise-throttling filter as well, so it has to run before any
# other log output.
setup_logging()

log = logging.getLogger("userbot")

# config is imported after logging is up, so an incomplete .env produces one
# clear sentence instead of a raw traceback.
try:
    import config
except Exception as config_error:  # ConfigError, or anything else while reading the env
    log.critical("configuration error: %s", config_error)
    log.critical("fix .env and start again (see .env.example for the full list)")
    raise SystemExit(2) from None

from pyrogram import Client, idle  # noqa: E402

from utils import paths  # noqa: E402
from utils.ai_engines import close_session  # noqa: E402
from utils.clock import clock_sync_worker, measure_offset  # noqa: E402
from utils.db_manager import init_db  # noqa: E402
from utils.pyrogram_patch import (  # noqa: E402
    apply_time_offset,
    get_time_offset,
    patch_peer_id_ranges,
    patch_tolerant_photo_parse,
)

# The last offset we warned about. The measurement repeats every half hour and
# without this memory a permanently wrong clock would warn forever; only a
# *change* of state is worth a warning.
_last_logged_offset = None


def _apply_clock_offset(offset: float) -> None:
    """
    Decide what to do with a measured clock offset.

    Below the threshold nothing is applied, so msg_id is never adjusted
    without cause. A positive value means the system clock is behind.
    """
    global _last_logged_offset

    if abs(offset) < config.CLOCK_SYNC_THRESHOLD:
        # Any correction applied earlier has to be lifted: the host clock may
        # have been fixed in the meantime, and keeping a stale offset would
        # recreate exactly the problem it was meant to solve.
        if get_time_offset() != 0.0:
            apply_time_offset(0.0)
            log.info("system clock is back in sync (offset %.2fs); correction removed", offset)
        else:
            log.info("system clock is accurate (offset %.2fs)", offset)
        _last_logged_offset = None
        return

    direction = "behind" if offset > 0 else "ahead"
    message = ("system clock is %.1fs %s; msg_id corrected. "
               "For a permanent fix, sync the host clock with NTP.")

    if _last_logged_offset is None or abs(offset - _last_logged_offset) > 1.0:
        log.warning(message, abs(offset), direction)
    else:
        # Unchanged since last time; record it at debug level only.
        log.debug(message, abs(offset), direction)

    apply_time_offset(offset)
    _last_logged_offset = offset


async def _sync_clock_once() -> None:
    """Initial clock measurement. Must happen before app.start()."""
    try:
        offset = await measure_offset(timeout=config.CLOCK_SYNC_TIMEOUT)
    except Exception as e:
        # Measuring the clock must never keep the bot from starting.
        log.info("clock sync skipped (%s); using the system clock", type(e).__name__)
        return

    if offset is None:
        log.info("no time source answered; using the system clock")
        return

    _apply_clock_offset(offset)


async def _shutdown(app, clock_task) -> None:
    """
    Clean shutdown.

    Each step is guarded separately: if one teardown fails the others still
    have to run — otherwise a minor error closing the aiohttp session would
    also prevent the Telegram client from being stopped.
    """
    if clock_task is not None:
        clock_task.cancel()
        # Wait for the task to actually finish, or closing the loop prints
        # "Task was destroyed but it is pending".
        try:
            await clock_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("clock worker ended with %s", type(e).__name__)

    # The shared aiohttp session has to be closed, otherwise exiting prints
    # an "Unclosed client session" warning.
    try:
        await close_session()
    except Exception as e:
        log.warning("could not close the AI HTTP session: %s", e)

    try:
        await app.stop()
    except Exception as e:
        log.warning("could not stop the Telegram client cleanly: %s", e)


async def main():
    # 0. Pyrogram patches — before Client() is built.
    #    Widens the valid chat-id ranges; without it, updates from channels
    #    with newer ids are dropped with a ValueError and never reach the
    #    handlers at all.
    patch_peer_id_ranges()

    #    Same class of problem, different cause: a photo that Pyrogram cannot
    #    describe raises inside the dispatcher's own parse step and takes the
    #    whole update with it (§11.4).
    patch_tolerant_photo_parse()

    # Collapse known Pyrogram background-task failures into a single line.
    install_asyncio_exception_handler(asyncio.get_running_loop())

    if config.CLOCK_SYNC:
        await _sync_clock_once()

    # data/ has to exist before the database and the session file; neither of
    # them creates its own parent directory.
    try:
        paths.ensure_data_dir()
    except OSError as e:
        log.critical("cannot create the data directory %s: %s", paths.DATA_DIR, e)
        raise SystemExit(3) from None

    log.info("preparing the database and caches…")
    # 1. Prepare the database first, inside this loop.
    try:
        await init_db()
    except Exception as e:
        # Without the database the authorized-user list stays empty and the
        # bot comes up with the wrong access rules — worse than not starting.
        log.critical("database initialisation failed (%s): %s", paths.DB_PATH, e)
        raise SystemExit(4) from None

    log.info("starting the userbot…")

    # 2. The client is built inside this function and inside this same loop.
    #    workdir decides where the session file lives: data/my_account.session
    #    Without it Pyrogram writes the session into the current working
    #    directory, so starting the bot from a different path means a fresh
    #    login and another verification code.
    app = Client(
        name=paths.SESSION_NAME,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        workdir=paths.DATA_DIR,
        plugins=dict(root="plugins")
    )

    # 3. Start the client.
    #    Failure here usually means a wrong api_id/api_hash, a corrupt session
    #    or no network. A raw traceback identifies none of those, so a hint
    #    goes with it.
    try:
        await app.start()
    except Exception as e:
        log.critical("could not start the Telegram client: %s: %s", type(e).__name__, e)
        log.critical(
            "check API_ID/API_HASH in .env and network access. If the session "
            "file is corrupt, delete %s and log in again.",
            paths.DATA_DIR / f"{paths.SESSION_NAME}.session",
        )
        await close_session()
        raise SystemExit(5) from None

    # 4. Periodic clock monitoring (when enabled) — VM clocks drift over time.
    clock_task = None
    if config.CLOCK_SYNC:
        clock_task = asyncio.create_task(
            clock_sync_worker(
                interval=config.CLOCK_SYNC_INTERVAL,
                timeout=config.CLOCK_SYNC_TIMEOUT,
                on_offset=_apply_clock_offset,
            )
        )

    try:
        # Fetch the account details as confirmation.
        me = await app.get_me()
        log.info("signed in as %s (id %s)", me.first_name, me.id)
        log.info("default AI model: %s", config.AI_MODEL)
        log.info("userbot is online 🚀")

        # 5. Keep the program alive in this same loop.
        await idle()
    finally:
        # 6. Clean shutdown — even if something went wrong above.
        await _shutdown(app, clock_task)
        log.info("userbot stopped cleanly")


if __name__ == "__main__":
    # Run the whole bot as a single unit.
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down at user request")
    except Exception:
        # Last safety net: without it an unexpected error only leaves a raw
        # traceback on stderr, which gets lost among the other lines in the
        # container log.
        #
        # SystemExit derives from BaseException rather than Exception, so the
        # deliberate exits above (codes 2-5) pass straight through here and
        # reach the OS with their exit code intact.
        log.critical("userbot terminated with an unhandled error", exc_info=True)
        sys.exit(1)
