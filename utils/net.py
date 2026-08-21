"""
Shared HTTP layer for the information modules (weather, gold, prayer times).

Why it exists: all three modules used to repeat the same code, and each of
them had dropped a different part of it — one had no timeout, one did not
check the response status, one interpolated the raw exception text into the
message shown to the user. That behaviour is now defined once, correctly.

Contract:
    * Only HttpError is raised; every network failure, non-2xx status or
      invalid body is converted to that one type, because from the caller's
      side they all mean the same thing: "this source is not answering
      correctly right now".
    * The HttpError message is **English** and meant for the log. Translating
      it into Persian for the user is the calling module's job (their messages
      are specific to each command).
"""

import logging

import aiohttp

log = logging.getLogger(__name__)

# The timeouts are deliberately short: an interactive Telegram command is
# waiting behind these requests, and the user should not sit in front of a
# "⏳ fetching" message for half a minute.
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15, sock_connect=6, sock_read=10)

# Some sites (the gold price source among them) answer aiohttp's default
# User-Agent with a 403; a browser UA solves that.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Ceiling on the body size we read. A huge HTML page or a broken response
# must not fill up the bot's memory.
MAX_BODY_BYTES = 4 * 1024 * 1024

# Size of a single read from the response stream. Only affects how often the
# size cap above is re-checked while the body arrives.
CHUNK_BYTES = 64 * 1024


class HttpError(Exception):
    """
    A failed HTTP request, with an English message suited to the log.

    status is the server's response code when there is one; it stays None for
    network-level failures. Callers usually do not need it and only log it.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status

    def __str__(self):
        return f"[{self.status}] {self.message}" if self.status else self.message


async def _read(url: str, headers: dict | None, timeout, as_json: bool):
    """Shared body of fetch_json and fetch_text."""
    request_headers = {"User-Agent": BROWSER_UA}
    if headers:
        request_headers.update(headers)

    try:
        async with aiohttp.ClientSession(timeout=timeout or DEFAULT_TIMEOUT) as session:
            async with session.get(url, headers=request_headers) as response:
                if response.status != 200:
                    raise HttpError("unexpected HTTP status", status=response.status)

                # Read as bytes so the size cap can be enforced; resp.json()
                # swallows however much body arrives.
                #
                # This must not be `content.read(MAX_BODY_BYTES + 1)`. Despite the
                # name, StreamReader.read(n) with n >= 0 is not "read n bytes": it
                # waits only until the buffer is non-empty and then returns
                # whatever happens to be sitting in it (aiohttp/streams.py). Any
                # body that does not arrive in a single burst is silently
                # truncated, with no error anywhere — the TGJU gold page is
                # ~1.1 MB and came back as ~700 bytes, a different length on every
                # call, so the price regex matched nothing and `.gold` reported
                # "the reference site's markup changed" while the markup was fine.
                # The small JSON sources were hit by the same bug more rarely,
                # surfacing as an occasional "not valid JSON".
                #
                # Iterating also lets the cap abort an oversized body while it is
                # still arriving, instead of after buffering all of it.
                chunks = []
                size = 0
                async for chunk in response.content.iter_chunked(CHUNK_BYTES):
                    size += len(chunk)
                    if size > MAX_BODY_BYTES:
                        raise HttpError(f"response body larger than {MAX_BODY_BYTES} bytes",
                                        status=response.status)
                    chunks.append(chunk)

                raw = b"".join(chunks)

                charset = response.charset or "utf-8"

    except HttpError:
        raise
    except aiohttp.ClientError as e:
        # Covers DNS, refused connections, TLS errors and mid-response drops.
        raise HttpError(f"network failure: {type(e).__name__}: {e}") from e
    except TimeoutError as e:
        # Since Python 3.11 asyncio.TimeoutError is the builtin TimeoutError.
        raise HttpError("request timed out") from e

    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")

    if not as_json:
        return text

    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise HttpError(f"response is not valid JSON: {e}") from e


async def fetch_json(url: str, *, headers: dict | None = None, timeout=None):
    """
    Fetch a URL and parse the JSON body.

    Returns whatever was in the body (usually a dict). The type is
    deliberately not checked here; validating the structure is the job of the
    calling module's parser.
    """
    return await _read(url, headers, timeout, as_json=True)


async def fetch_text(url: str, *, headers: dict | None = None, timeout=None) -> str:
    """Fetch a URL and return the body as text (for pages that serve HTML)."""
    return await _read(url, headers, timeout, as_json=False)
