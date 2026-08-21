"""
Measuring how far the system clock is from real time (SNTP).

Why it is needed: MTProto derives msg_id from the client clock, and a clock
that runs behind earns a BadMsgNotification[16] from the server, which
effectively stops the session working. Inside Docker the clock cannot be set
either (it belongs to the host kernel), so the only option available to the
program is to *know* the offset and hand it to utils/pyrogram_patch.py.

This module deliberately has nothing to do with pyrogram; it is a small SNTP
client and is testable standalone. Like the other utils it never raises: worst
case it returns None and the program carries on with the system clock.

There are two measurement paths, tried in this order:

  1. SNTP over UDP/123 — accurate (sub-millisecond), but many networks block
     outbound UDP.
  2. The Date header of an HTTPS response — accurate only to the second, but
     it gets through any network that allows HTTPS. That is ample here:
     Telegram tolerates roughly 30 s and our correction threshold is 5 s.
"""

import asyncio
import logging
import struct
import time
from email.utils import parsedate_to_datetime

import aiohttp

log = logging.getLogger(__name__)

# Several servers on different networks: if one is blocked, the others answer.
NTP_SERVERS = ("time.google.com", "time.cloudflare.com", "pool.ntp.org")
NTP_PORT = 123

# The second path: lightweight URLs where only the Date header is needed (HEAD).
HTTP_TIME_URLS = (
    "https://www.google.com/generate_204",
    "https://www.cloudflare.com/",
    "https://telegram.org/",
)

# Distance from the NTP epoch (1900) to the Unix epoch (1970).
_NTP_EPOCH_DELTA = 2_208_988_800

# Request packet: LI=0, VN=3, Mode=3 (client) followed by 47 zero bytes.
_REQUEST = b"\x1b" + 47 * b"\0"

# Two answers further apart than this (seconds) are not considered to agree.
# The real offset is the same for every server, so a large spread means one of
# the answers cannot be trusted.
AGREEMENT_TOLERANCE = 2.0

# The HTTP path needs a wider tolerance: the Date header is only second-
# granular, so two perfectly correct servers can still differ by a second.
HTTP_AGREEMENT_TOLERANCE = 3.0

DEFAULT_TIMEOUT = 5.0


class _SntpProtocol(asyncio.DatagramProtocol):
    """One UDP round trip; the result is placed in a future."""

    def __init__(self, future: asyncio.Future):
        self._future = future
        self._sent_at = 0.0

    def connection_made(self, transport):
        # The send time is read right next to sendto itself, so the RTT is right.
        self._sent_at = time.time()
        transport.sendto(_REQUEST)

    def datagram_received(self, data, _addr):
        if not self._future.done():
            self._future.set_result((self._sent_at, data, time.time()))

    def error_received(self, exc):
        if not self._future.done():
            self._future.set_exception(exc)

    def connection_lost(self, exc):
        # If the transport closes before an answer arrives, do not wait out
        # the timeout. When the future is already done (answer received or
        # cancelled) this branch has no effect.
        if not self._future.done():
            self._future.set_exception(exc or ConnectionError("UDP transport closed"))


def _parse_offset(sent_at: float, packet: bytes, received_at: float) -> float:
    """
    Compute the offset from the server's reply.

    The standard NTP formula:
        offset = ((T1 - T0) + (T2 - T3)) / 2
    where T0/T3 are the local send and receive times and T1/T2 the server's
    receive and send times. Halving cancels out the round-trip delay.
    """
    if len(packet) < 48:
        raise ValueError(f"NTP reply is truncated ({len(packet)} bytes)")

    mode = packet[0] & 0b111
    stratum = packet[1]

    if mode != 4:
        raise ValueError(f"NTP reply is not a server response (mode={mode})")

    # Stratum 0 marks a Kiss-of-Death packet and 16 or above means the server
    # is not synchronised itself; either way its time is not authoritative.
    if not 1 <= stratum <= 15:
        raise ValueError(f"NTP server is not synchronised (stratum={stratum})")

    recv_int, recv_frac, tx_int, tx_frac = struct.unpack("!4I", packet[32:48])

    if tx_int == 0:
        raise ValueError("NTP reply has an empty transmit timestamp")

    server_received = recv_int + recv_frac / 2 ** 32 - _NTP_EPOCH_DELTA
    server_sent = tx_int + tx_frac / 2 ** 32 - _NTP_EPOCH_DELTA

    return ((server_received - sent_at) + (server_sent - received_at)) / 2


async def _query_server(server: str) -> float:
    """Query one server for the time. The caller applies the timeout."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    transport, _ = await loop.create_datagram_endpoint(
        lambda: _SntpProtocol(future),
        remote_addr=(server, NTP_PORT),
    )

    try:
        sent_at, packet, received_at = await future
    finally:
        transport.close()

    return _parse_offset(sent_at, packet, received_at)


async def _query_http(url: str, session: aiohttp.ClientSession) -> float:
    """
    Read the time from an HTTPS response's Date header.

    HEAD is used because only the header is needed. no-cache is sent as well,
    so an intermediate proxy cannot serve a stale response — and with it a
    stale Date.
    """
    sent_at = time.time()
    async with session.head(url, allow_redirects=False,
                            headers={"Cache-Control": "no-cache"}) as response:
        received_at = time.time()
        raw_date = response.headers.get("Date")

    if not raw_date:
        raise ValueError("response carries no Date header")

    server_time = parsedate_to_datetime(raw_date).timestamp()

    # The Date header is rounded to the second; adding half a second makes the
    # rounding error symmetric around zero instead of one-sided. The local
    # time is taken at the midpoint of the round trip, halving network delay.
    return server_time + 0.5 - (sent_at + received_at) / 2


def _aggregate(offsets: list, tolerance: float, source: str):
    """Pick a final value from several measurements."""
    if not offsets:
        return None

    if len(offsets) == 1:
        # One source beats none, but it is logged explicitly because it cannot
        # be cross-checked.
        log.info("clock offset taken from a single %s source, unverified: %.2fs", source, offsets[0])
        return offsets[0]

    offsets.sort()
    median = offsets[len(offsets) // 2]
    agreeing = [value for value in offsets if abs(value - median) <= tolerance]

    if len(agreeing) >= 2:
        return sum(agreeing) / len(agreeing)

    # The answers disagree: the median is the safest pick, but it warrants a
    # warning.
    log.warning("%s time sources disagree: %s",
                source, [round(value, 2) for value in offsets])
    return median


async def _gather_offsets(coros, labels, source: str) -> list:
    """Run several measurements concurrently and discard the failed ones."""
    results = await asyncio.gather(*coros, return_exceptions=True)

    offsets = []
    for label, result in zip(labels, results):
        if isinstance(result, BaseException):
            log.debug("time source %s (%s) did not answer: %s", label, source, type(result).__name__)
            continue
        offsets.append(result)

    return offsets


async def measure_offset(timeout: float = DEFAULT_TIMEOUT, servers=NTP_SERVERS,
                         http_urls=HTTP_TIME_URLS):
    """
    How far the system clock is from real time, in seconds.

    A positive value means the system clock is *behind* (it needs moving
    forward). If no source answers, None is returned.

    Within each stage every source is queried concurrently, so one slow server
    cannot hold up the whole startup; the timeout bounds each stage, not the
    sum of its sources.
    """
    # --- Stage 1: SNTP (accurate) ---
    if servers:
        offsets = await _gather_offsets(
            [asyncio.wait_for(_query_server(server), timeout) for server in servers],
            servers, "NTP",
        )
        result = _aggregate(offsets, AGREEMENT_TOLERANCE, "NTP")
        if result is not None:
            return result

    if not http_urls:
        return None

    # --- Stage 2: the Date header (for networks that block UDP) ---
    log.debug("no NTP server answered; falling back to the HTTP Date header")
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    # A session dedicated to this call, like the project's other networked
    # utils: this function runs at most once every few dozen minutes.
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        offsets = await _gather_offsets(
            [_query_http(url, session) for url in http_urls],
            http_urls, "HTTP",
        )

    return _aggregate(offsets, HTTP_AGREEMENT_TOLERANCE, "HTTP")


async def clock_sync_worker(interval: float, timeout: float, on_offset) -> None:
    """
    Measure the clock offset periodically.

    Measuring once at startup is not enough: a VM clock drifts again over time
    (especially after the host sleeps or suspends) and brings back exactly the
    same BadMsgNotification.

    on_offset is a synchronous callable invoked with each fresh offset, which
    is how this module avoids taking on any dependency on pyrogram.
    """
    while True:
        await asyncio.sleep(interval)

        try:
            offset = await measure_offset(timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A network outage must not kill the worker; the next round retries.
            log.debug("periodic clock measurement failed: %s", type(e).__name__)
            continue

        if offset is not None:
            on_offset(offset)
