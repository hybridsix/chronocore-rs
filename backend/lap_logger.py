"""
ChronoCore / CCRS - Pluggable Scanner Publisher
===============================================

Purpose
-------
Read tag scans from an input source (I-Lap serial, I-Lap UDP, or a mock generator),
normalize and de-duplicate them, then publish into the Operator UI's scan bus
*either* via direct in-process call to `publish_tag(tag)` *or* via HTTP
POST to `/ilap/inject?tag=...` on the CCRS server.

Supports:
  • I-Lap serial readers
  • UDP broadcast readers
  • Mock tag generators (for testing)
  • Future pluggable reader classes

Publishing modes:
  • In-process (direct call to server.publish_tag)
  • HTTP POST /ilap/inject (external process or node)

Key behaviors
-------------
- Input sources (select one at runtime via YAML/CLI):
    * ilap.serial : USB/serial text stream using the I-Lap 7-digit mode
    * ilap.udp    : UDP datagrams carrying the same line format as serial
    * mock        : synthetic tags at intervals (for testing UIs end-to-end)

- Parsing / validation:
    * Extract digits-only tag, enforce MIN_TAG_LEN (default 7)
    * Optional normalization/prefix handling point (left as a hook)

- De-duplication & throttling:
    * Windowed duplicate suppression (e.g., 3 s) so the same tag doesn’t spam
    * Optional rate limit (tags/sec) to protect UI/back end

- Publishing modes:
    * In-process: call `server.publish_tag(tag)` if importable
    * HTTP: POST /ilap/inject?tag=... with retry/backoff and a small queue

- Observability (lightweight):
    * Structured log lines with fields: source, raw_line, tag, dedup_suppressed, published, latency_ms
    * Simple counters in process (can be scraped from logs)
    * Clear error messages on disconnects and malformed frames

- Resilience:
    * Serial/UDP reconnect with exponential backoff
    * Continue on malformed frames; no crashes
    * Clean shutdown on SIGINT/SIGTERM

CLI
---
    python -m lap_logger --config /path/to/ccrs.yaml
    # Optional runtime overrides:
    --source ilap.serial|ilap.udp|mock
    --mode inprocess|http
    --min-tag-len 7
    --dup-win 3
    --rate 20
    --http-base http://127.0.0.1:8000
    --http-timeout-ms 500
    --serial-port COM7
    --serial-baud 9600
    --udp-host 0.0.0.0
    --udp-port 5000
"""

from __future__ import annotations
from pathlib import Path
import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import AsyncGenerator, AsyncIterable, AsyncIterator, Optional, Mapping, Any

from abc import ABC, abstractmethod

# Third-party deps present in your repo
import yaml
import httpx
# ----------------------------------------------------------------------
# Heartbeat client for CCRS - keeps /decoders/status truthful in real-time
# ----------------------------------------------------------------------


def _post_json(url: str, payload: dict, timeout: float = 1.0) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Not reading response body is okay; server returns a tiny JSON
        _ = resp.read()


def start_heartbeat_thread(
    base_url: str,
    meta: dict,
    period_s: float = 2.0,
    gate: threading.Event | None = None,
) -> threading.Thread:
    """
    Fire-and-forget heartbeat background loop. Posts to /sensors/meta every period_s.
    Swallows exceptions; if the server is unreachable, it will just retry later.
    """
    url = f"{base_url.rstrip('/')}/sensors/meta"

    def _loop() -> None:
        last_post = 0.0
        while True:
            if gate is None or gate.is_set():
                now = time.time()
                if (now - last_post) >= period_s:
                    try:
                        _post_json(url, meta, timeout=1.0)
                    except Exception:
                        # Avoid chatty logs here; the operator UI is the source of truth
                        pass
                    last_post = now
            time.sleep(0.25)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


# pyserial is used only in serial mode; imported lazily inside the reader
# to keep other modes runnable without the dependency at import-time.

# --- Optional in-process publisher import ------------------------------------
# If we're launched inside the same Python process as FastAPI (e.g., via a
# launcher that starts both), we can publish directly without HTTP.
try:
    from server import publish_tag as _inproc_publish  # type: ignore
except Exception:  # pragma: no cover - failure is fine; we’ll log and fallback
    _inproc_publish = None


# ------------------------------------------------------------
# Config model and helpers
# ------------------------------------------------------------

@dataclass
class ScannerSerialCfg:
    port: str
    baud: int
    device_id: Optional[str] = None
    host: Optional[str] = None

@dataclass
class ScannerUdpCfg:
    host: str
    port: int

@dataclass
class ScannerCfg:
    source: str
    serial: ScannerSerialCfg
    udp: ScannerUdpCfg
    min_tag_len: int
    duplicate_window_sec: float
    rate_limit_per_sec: float
    # NEW: how this scanner should be treated by the engine
    #      ("track" → Start/Finish, "pit_in", "pit_out")
    role: str = "track"

@dataclass
class PublisherHttpCfg:
    base_url: str = "http://127.0.0.1:8000"
    timeout_ms: int = 500

@dataclass
class PublisherCfg:
    mode: str = "http"  # "inprocess" | "http"
    http: PublisherHttpCfg = field(default_factory=PublisherHttpCfg)

@dataclass
class RootCfg:
    scanner: "ScannerCfg" = field(default_factory=lambda: ScannerCfg())
    publisher: "PublisherCfg" = field(default_factory=lambda: PublisherCfg())
    log_level: str = "INFO"



def load_config(path: str) -> RootCfg:
    """
    Load YAML config into our dataclasses. Unknown keys are ignored.
    - Adds scanner.role (track | pit_in | pit_out), default 'track'
    - Normalizes a few string-y enums (publisher.mode, log level)
    """
    with open(path, "r", encoding="utf-8") as f:
        raw: Mapping[str, Any] = yaml.safe_load(f) or {}

    def pick(d: Mapping[str, Any] | None, *keys: str, default: Any = None) -> Any:
        cur: Any = d
        for k in keys:
            cur = (cur or {}).get(k, None)  # type: ignore[attr-defined]
        return default if cur is None else cur

    # --- Normalize helpers ----------------------------------------------------
    def _norm_lower(val: Any, default: str) -> str:
        try:
            s = str(val).strip().lower()
            return s or default
        except Exception:
            return default

    def _norm_upper(val: Any, default: str) -> str:
        try:
            s = str(val).strip().upper()
            return s or default
        except Exception:
            return default

    # scanner.role with guard-rail
    role = _norm_lower(pick(raw, "scanner", "role", default="track"), "track")
    if role not in ("track", "pit_in", "pit_out"):
        role = "track"

    cfg = RootCfg(
        scanner=ScannerCfg(
            source=_norm_lower(pick(raw, "scanner", "source", default="mock"), "mock"),
            serial=ScannerSerialCfg(
                port=pick(raw, "scanner", "serial", "port", default="COM7"),
                baud=int(pick(raw, "scanner", "serial", "baud", default=9600)),
                device_id=pick(raw, "scanner", "serial", "device_id"),
                host=pick(raw, "scanner", "serial", "host"),
            ),
            udp=ScannerUdpCfg(
                host=pick(raw, "scanner", "udp", "host", default="0.0.0.0"),
                port=int(pick(raw, "scanner", "udp", "port", default=5000)),
            ),
            min_tag_len=int(pick(raw, "scanner", "min_tag_len", default=7)),
            duplicate_window_sec=float(pick(raw, "scanner", "duplicate_window_sec", default=3)),
            rate_limit_per_sec=float(pick(raw, "scanner", "rate_limit_per_sec", default=0)),
            role=role,
        ),
        publisher=PublisherCfg(
            mode=_norm_lower(pick(raw, "publisher", "mode", default="http"), "http"),
            http=PublisherHttpCfg(
                base_url=pick(raw, "publisher", "http", "base_url", default="http://127.0.0.1:8000"),
                timeout_ms=int(pick(raw, "publisher", "http", "timeout_ms", default=500)),
            ),
        ),
        # Accept both new `log.level` and legacy root `log_level`
        log_level=_norm_upper(pick(raw, "log", "level", default=pick(raw, "log_level", default="INFO")), "INFO"),
    )
    return cfg



# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

class DedupWindow:
    """
    De-duplicate tag events within a sliding window. Simple in-memory cache:
    tag -> last_seen_epoch_seconds.
    """
    def __init__(self, window_sec: float):
        self.window = float(window_sec)
        self._last = {}  # type: dict[str, float]

    def accept(self, tag: str) -> bool:
        now = time.time()
        last = self._last.get(tag, 0.0)
        if now - last < self.window:
            return False
        self._last[tag] = now
        return True


class RateLimiter:
    """
    Optional "max N tags per second" limiter to avoid hammering publishers/UI.
    If rate==0, limiter is disabled.
    """
    def __init__(self, rate_per_sec: float):
        self.rate = float(rate_per_sec)
        self._last_pub = 0.0

    async def wait_slot(self):
        if self.rate <= 0:
            return
        now = time.time()
        min_interval = 1.0 / self.rate
        delay = self._last_pub + min_interval - now
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_pub = time.time()


# ------------------------------------------------------------
# Publishers
# ------------------------------------------------------------

class Publisher:
    """
    Polymorphic publisher interface. Concrete implementations:
      - InProcessPublisher
      - HttpPublisher (with retry/queue)
    """
    async def start(self):
        """Optional background tasks."""
        return

    async def stop(self):
        """Graceful shutdown hook."""
        return

    async def publish(self, tag: str):
        raise NotImplementedError


class InProcessPublisher(Publisher):
    """
    Directly calls server.publish_tag(tag) if available in this Python process.
    Raises RuntimeError immediately if not importable, so you get a clear message.
    """
    def __init__(self):
        if not callable(_inproc_publish):
            raise RuntimeError(
                "publisher.mode='inprocess' but server.publish_tag() is not available.\n"
                "If running as a separate process, set publisher.mode='http'.\n"
                "If embedding in FastAPI, ensure lap_logger imports AFTER server defines publish_tag."
            )

    async def publish(self, tag: str):
        _inproc_publish(tag) # type: ignore


class HttpPublisher(Publisher):
    """
    Posts tags to CCRS /ilap/inject over HTTP with:
      - small queue to absorb bursts
      - retry with exponential backoff
      - shared AsyncClient
    """
    def __init__(self, base_url: str, timeout_ms: int = 500, max_queue: int = 256):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_ms / 1000.0
        self._client: Optional[httpx.AsyncClient] = None
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue)
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

        # Observability counters (simple integers; emit in logs)
        self.tags_enqueued = 0
        self.tags_sent = 0
        self.tags_failed = 0
        self.send_attempts = 0

    async def start(self):
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        self._task = asyncio.create_task(self._run_sender())

    async def stop(self):
        self._stopping.set()
        if self._task:
            await self._task
        if self._client:
            await self._client.aclose()

    async def publish(self, tag: str):
        try:
            self._queue.put_nowait(tag)
            self.tags_enqueued += 1
        except asyncio.QueueFull:
            # Backpressure: drop oldest then enqueue (lossy protection).
            _ = self._queue.get_nowait()
            await self._queue.put(tag)

    async def _run_sender(self):
        """
        Worker task: drains the queue and POSTs /ilap/inject?tag=...
        Backoff doubles on each transient error (cap 2s), resets on success.
        """
        assert self._client is not None
        backoff = 0.1
        while not self._stopping.is_set():
            try:
                tag = await asyncio.wait_for(self._queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue

            self.send_attempts += 1
            t0 = time.perf_counter()
            try:
                resp = await self._client.post(
                    "/sensors/inject",
                    role=(cfg.scanner.get("role") or "track").lower(),
                    json={"tag": tag, "source": role},
                )
                if 200 <= resp.status_code < 300:
                    self.tags_sent += 1
                    logging.getLogger("scanner.pub").info(
                        "published",
                        extra={
                            "tag": tag,
                            "publisher": "http",
                            "status": resp.status_code,
                            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                        },
                    )
                    backoff = 0.1  # reset backoff on success
                else:
                    self.tags_failed += 1
                    logging.getLogger("scanner.pub").warning(
                        "http_non_2xx",
                        extra={
                            "tag": tag,
                            "publisher": "http",
                            "status": resp.status_code,
                            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                        },
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 2.0)
                    # requeue for another try
                    await self._queue.put(tag)
            except Exception as e:
                self.tags_failed += 1
                logging.getLogger("scanner.pub").warning(
                    "http_error",
                    extra={"tag": tag, "publisher": "http", "err": str(e)},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 2.0)
                # requeue for another try
                await self._queue.put(tag)


# ------------------------------------------------------------
# Readers (async)
# ------------------------------------------------------------

ILAP_INIT_7DIGIT = bytes([1, 37, 13, 10])  # SOH '%', CR, LF

# -----------------
# Reader interface
# -----------------
class Reader(ABC):
    @abstractmethod
    def tags(self) -> AsyncIterable[str]:
        """
        Return an async-iterable stream of tag strings.

        Concrete implementations typically implement this as an async generator:
            async def tags(self) -> AsyncIterator[str]:
                ...
        """
        raise NotImplementedError


class MockReader(Reader):
    async def tags(self) -> AsyncIterator[str]:
        i = 0
        while True:
            i += 1
            yield f"9{i:06d}"
            await asyncio.sleep(3.0)


class ILapSerialReader(Reader):
    """
    Reads from a serial TTY using I-Lap text protocol. On startup, sends the
    7-digit init sequence which also resets the decoder clock. Lines look like:

        \x01@\t<decoder_id>\t<tag>\t<secs.mss>\r\n

    We parse lines that start with SOH+'@' and yield only the tag field.
    """
    def __init__(self, port: str, baud: int, *, heartbeat_base_url: Optional[str] = None,
                 heartbeat_meta: Optional[Mapping[str, Any]] = None):
        self.port = port
        self.baud = baud
        self._log = logging.getLogger("scanner.serial")
        self._heartbeat_base_url = heartbeat_base_url
        self._heartbeat_meta = dict(heartbeat_meta) if heartbeat_meta else None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_period_s = 2.0
        self._heartbeat_boot_sent = False
        self._heartbeat_gate = threading.Event()

    def _ensure_heartbeat(self) -> None:
        if not self._heartbeat_base_url or not self._heartbeat_meta:
            return
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = start_heartbeat_thread(
                self._heartbeat_base_url,
                dict(self._heartbeat_meta),
                period_s=self._heartbeat_period_s,
                gate=self._heartbeat_gate,
            )
        self._heartbeat_gate.set()
        if not self._heartbeat_boot_sent:
            try:
                _post_json(
                    f"{self._heartbeat_base_url.rstrip('/')}/sensors/meta",
                    dict(self._heartbeat_meta),
                    timeout=1.0,
                )
            except Exception:
                pass
            self._heartbeat_boot_sent = True

    async def tags(self) -> AsyncGenerator[str, None]:
        backoff = 0.2
        while True:
            ser = None
            try:
                import serial  # lazy import
                self._log.info("open_serial", extra={"port": self.port, "baud": self.baud})
                ser = serial.Serial(self.port, self.baud, bytesize=8, parity='N', stopbits=1, timeout=0.25)
                self._ensure_heartbeat()
                try:
                    # Per I-Lap guidance: RTS low/off
                    try:
                        ser.rts = False
                    except Exception:
                        pass
                    await asyncio.sleep(0.2)
                    # Enter 7-digit mode (and reset decoder clock)
                    ser.write(ILAP_INIT_7DIGIT)
                    ser.flush()
                    self._log.info("sent_init_7digit")

                    # Read loop - line oriented
                    while True:
                        try:
                            # Use a thread hop so we don't block the event loop on .readline()
                            raw: bytes = await asyncio.to_thread(ser.readline)
                        except asyncio.CancelledError:
                            raise

                        if not raw:
                            continue

                        # Decode safely; keep original bytes for debugging
                        txt = raw.decode(errors="replace").strip()
                        logging.getLogger("scanner.raw").debug("serial_line", extra={"raw": repr(raw), "txt": txt})

                        if raw.startswith(b"\x01@"):  # SOH '@'
                            tag = _parse_ilap_pass_and_extract_tag(txt.replace("\x01", ""))
                            if tag is not None:
                                yield tag
                finally:
                    if ser is not None:
                        with contextlib.suppress(Exception):
                            ser.close()
                        ser = None
                    self._log.info("serial_closed")
                    self._heartbeat_gate.clear()
                    self._heartbeat_boot_sent = False
                    backoff = 0.2  # reset backoff after a successful session
            except asyncio.CancelledError:
                self._log.info("serial_cancelled")
                raise
            except Exception as e:
                self._log.warning("serial_error", extra={"port": self.port, "err": str(e)})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)  # cap backoff so we keep trying


class ILapUDPReader(Reader):
    """
    Receives UDP datagrams that contain I-Lap lines in the same text format as serial.
    Robust to packet boundaries: splits on newlines and processes each line.
    """
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._log = logging.getLogger("scanner.udp")
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1024)
        self._transport = None
        self._protocol = None

    async def tags(self) -> AsyncGenerator[str, None]:
        backoff = 0.2
        loop = asyncio.get_running_loop()
        while True:
            try:
                # Datagram handler that pushes parsed tags into our queue
                class Proto(asyncio.DatagramProtocol):
                    def __init__(self, outer: "ILapUDPReader"):
                        self.outer = outer
                        self._buf = b""

                    def connection_made(self, transport):
                        self.outer._transport = transport
                        self.outer._log.info("udp_listen", extra={"host": self.outer.host, "port": self.outer.port})

                    def datagram_received(self, data, addr):
                        # Accumulate, then split once on newlines
                        self._buf += data
                        chunks = self._buf.split(b"\n")       # list[bytes]
                        self._buf = chunks[-1]                # tail (possibly partial line)
                        for raw_line in chunks[:-1]:          # complete lines only
                            raw = raw_line.rstrip(b"\r")      # handle CRLF safely
                            if not raw:
                                continue
                            try:
                                txt = raw.decode(errors="replace").strip()
                            except Exception:
                                txt = ""
                            logging.getLogger("scanner.raw").debug("udp_line", extra={"raw": repr(raw), "txt": txt})
                            if raw.startswith(b"\x01@"):
                                tag = _parse_ilap_pass_and_extract_tag(txt.replace("\x01", ""))
                                if tag is not None:
                                    try:
                                        self.outer._queue.put_nowait(tag)
                                    except asyncio.QueueFull:
                                        logging.getLogger("scanner.udp").warning("udp_queue_full_drop", extra={"tag": tag})


                    def error_received(self, exc):
                        self.outer._log.warning("udp_error", extra={"err": str(exc)})

                    def connection_lost(self, exc):
                        self.outer._log.info("udp_closed", extra={"err": str(exc) if exc else None})

                # Create endpoint
                transport, protocol = await loop.create_datagram_endpoint(
                    lambda: Proto(self),
                    local_addr=(self.host, self.port),
                )
                self._transport, self._protocol = transport, protocol

                # Drain the queue and yield tags
                while True:
                    try:
                        tag = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                        yield tag
                    except asyncio.TimeoutError:
                        # Keep loop alive so we can catch cancellation/stop
                        pass
            except asyncio.CancelledError:
                self._log.info("udp_cancelled")
                raise
            except Exception as e:
                self._log.warning("udp_bind_error", extra={"host": self.host, "port": self.port, "err": str(e)})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)
            finally:
                if self._transport is not None:
                    self._transport.close()
                    self._transport = None
                backoff = 0.2  # reset after any successful bind session


# ------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------

def _parse_ilap_pass_and_extract_tag(txt: str) -> Optional[str]:
    """
    Parse an I-Lap pass line shaped like: "@\t<decoder_id>\t<tag>\t<secs.mss>"
    Return the raw tag field (string) or None if invalid.

    NOTE: We return the *digits as seen*. Normalization/padding (e.g., stripping
    prefixes) happens later in the pipeline, so the caller can enforce a MIN_TAG_LEN
    after removing non-digits.
    """
    if not txt.startswith("@"):
        return None
    parts = txt.split("\t")
    if len(parts) < 4:
        return None
    # parts[1] = decoder_id, parts[2] = tag, parts[3] = seconds
    tag_field = parts[2].strip()
    if not tag_field:
        return None
    return tag_field


def _digits_only(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


# ------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------

class ScannerService:
    """
    Wires: Reader → (normalize → de-dup → rate limit) → Publisher.

    This service owns the reader and publisher lifecycles, logs structured events,
    and never raises unhandled exceptions (it runs until cancelled).
    """
    def __init__(self, cfg: RootCfg):
        self.cfg = cfg
        self.log = logging.getLogger("scanner")
        self._rl = RateLimiter(cfg.scanner.rate_limit_per_sec)
        self._dups = DedupWindow(cfg.scanner.duplicate_window_sec)

        # Simple counters for observability (logged periodically)
        self.tags_seen_total = 0
        self.tags_published_total = 0
        self.tags_suppressed_total = 0

        # Build reader
        src = cfg.scanner.source
        if src == "mock":
            self.reader: Reader = MockReader()
        elif src == "ilap.serial":
            heartbeat_meta: dict[str, Any] = {
                "source": "ilap.serial",
                "port": cfg.scanner.serial.port,
                "baud": cfg.scanner.serial.baud,
            }
            if cfg.scanner.serial.device_id:
                heartbeat_meta["device_id"] = cfg.scanner.serial.device_id
            if cfg.scanner.serial.host:
                heartbeat_meta["host"] = cfg.scanner.serial.host

            heartbeat_base_url = cfg.publisher.http.base_url
            self.reader = ILapSerialReader(
                cfg.scanner.serial.port,
                cfg.scanner.serial.baud,
                heartbeat_base_url=heartbeat_base_url,
                heartbeat_meta=heartbeat_meta,
            )
        elif src == "ilap.udp":
            self.reader = ILapUDPReader(cfg.scanner.udp.host, cfg.scanner.udp.port)
        else:
            raise ValueError(f"Unknown scanner.source: {src}")

        # Build publisher
        mode = cfg.publisher.mode
        if mode == "inprocess":
            try:
                self.publisher: Publisher = InProcessPublisher()
            except Exception as e:
                # Clear message then hard-fail so ops sees the misconfiguration quickly.
                raise RuntimeError(
                    "publisher.mode='inprocess' but in-process publish is unavailable. "
                    "Either run inside the FastAPI process (server.publish_tag) or use publisher.mode='http'."
                ) from e
        elif mode == "http":
            self.publisher = HttpPublisher(cfg.publisher.http.base_url, cfg.publisher.http.timeout_ms)
        else:
            raise ValueError(f"Unknown publisher.mode: {mode}")

    async def run(self, stop_evt: asyncio.Event):
        await self.publisher.start()
        self.log.info(
            "scanner_start",
            extra={
                "source": self.cfg.scanner.source,
                "mode": self.cfg.publisher.mode,
                "min_tag_len": self.cfg.scanner.min_tag_len,
                "dup_window_s": self.cfg.scanner.duplicate_window_sec,
                "rate_limit_per_sec": self.cfg.scanner.rate_limit_per_sec,
            },
        )

        hb_task = asyncio.create_task(self._heartbeat(), name="scanner_heartbeat")

        try:
            async for tag_raw in self.reader.tags():
                if stop_evt.is_set():
                    break

                t0 = time.perf_counter()
                self.tags_seen_total += 1

                tag_digits = _digits_only(tag_raw)

                if len(tag_digits) < int(self.cfg.scanner.min_tag_len):
                    logging.getLogger("scanner.parse").debug(
                        "reject_short", extra={"raw_tag": tag_raw, "digits": tag_digits}
                    )
                    continue

                if not self._dups.accept(tag_digits):
                    self.tags_suppressed_total += 1
                    logging.getLogger("scanner.dedup").info(
                        "suppressed",
                        extra={"tag": tag_digits, "window_s": self.cfg.scanner.duplicate_window_sec},
                    )
                    continue

                await self._rl.wait_slot()

                published_ok = True
                try:
                    await self.publisher.publish(tag_digits)
                except Exception as e:
                    published_ok = False
                    logging.getLogger("scanner.pub").warning(
                        "publish_error", extra={"tag": tag_digits, "err": str(e)}
                    )

                logging.getLogger("scanner.event").info(
                    "scan_event",
                    extra={
                        "source": self.cfg.scanner.source,
                        "raw_line": tag_raw if self.cfg.log_level == "DEBUG" else None,
                        "tag": tag_digits,
                        "dedup_suppressed": False,
                        "published": published_ok,
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    },
                )

                if published_ok:
                    self.tags_published_total += 1
        except asyncio.CancelledError:
            stop_evt.set()
            self.log.info("scanner_run_cancelled")
        except Exception:
            self.log.exception("scanner_run_crashed")
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
            except Exception:
                self.log.exception("heartbeat_task_error_on_shutdown")

            try:
                await self.publisher.stop()
            except asyncio.CancelledError:
                pass
            except Exception:
                self.log.exception("scanner_publisher_stop_failed")

            self.log.info(
                "scanner_stop",
                extra={
                    "seen": self.tags_seen_total,
                    "published": self.tags_published_total,
                    "suppressed": self.tags_suppressed_total,
                },
            )

    async def _heartbeat(self):
        """
        Periodic log line so ops can see counters move without scraping metrics.
        """
        pub = self.publisher
        try:
            while True:
                await asyncio.sleep(10)
                payload = {
                    "seen": self.tags_seen_total,
                    "published": self.tags_published_total,
                    "suppressed": self.tags_suppressed_total,
                }
                if isinstance(pub, HttpPublisher):
                    payload.update(
                        {
                            "qsize": pub._queue.qsize(),
                            "send_attempts": pub.send_attempts,
                            "sent": pub.tags_sent,
                            "failed": pub.tags_failed,
                        }
                    )
                logging.getLogger("scanner.hb").info("heartbeat", extra=payload)
        except asyncio.CancelledError:
            return
        except Exception:
            logging.getLogger("scanner.hb").exception("heartbeat_task_crashed")
            raise


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def _apply_overrides(cfg: RootCfg, args: argparse.Namespace) -> RootCfg:
    # Scanner overrides
    if args.source:
        cfg.scanner.source = args.source
    if args.min_tag_len is not None:
        cfg.scanner.min_tag_len = int(args.min_tag_len)
    if args.dup_win is not None:
        cfg.scanner.duplicate_window_sec = float(args.dup_win)
    if args.rate is not None:
        cfg.scanner.rate_limit_per_sec = float(args.rate)
    if args.serial_port:
        cfg.scanner.serial.port = args.serial_port
    if args.serial_baud:
        cfg.scanner.serial.baud = int(args.serial_baud)
    if args.udp_host:
        cfg.scanner.udp.host = args.udp_host
    if args.udp_port:
        cfg.scanner.udp.port = int(args.udp_port)

    # Publisher overrides
    if args.mode:
        cfg.publisher.mode = args.mode
    if args.http_base:
        cfg.publisher.http.base_url = args.http_base
    if args.http_timeout_ms:
        cfg.publisher.http.timeout_ms = int(args.http_timeout_ms)

    # Log level
    if args.log_level:
        cfg.log_level = args.log_level

    return cfg


def _make_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="CCRS Scanner Publisher (I-Lap → publish_tag or HTTP)")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "config.yaml"),
                    help="Path to unified CCRS YAML (default: config/config.yaml)")

    # Scanner overrides
    ap.add_argument("--source", choices=["ilap.serial", "ilap.udp", "mock"])
    ap.add_argument("--min-tag-len", dest="min_tag_len", type=int)
    ap.add_argument("--dup-win", dest="dup_win", type=float, help="Duplicate suppression window seconds")
    ap.add_argument("--rate", type=float, help="Max tags/sec (0 = unlimited)")

    ap.add_argument("--serial-port")
    ap.add_argument("--serial-baud", type=int)
    ap.add_argument("--udp-host")
    ap.add_argument("--udp-port", type=int)

    # Publisher overrides
    ap.add_argument("--mode", choices=["inprocess", "http"])
    ap.add_argument("--http-base")
    ap.add_argument("--http-timeout-ms", type=int)

    # Logging
    ap.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override log level")

    return ap


def main():
    ap = _make_argparser()
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg = _apply_overrides(cfg, args)

    # Configure logging early
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Run service with SIGINT/SIGTERM handling
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_evt = asyncio.Event()

    def _stop():
        try:
            stop_evt.set()
        except Exception:
            pass

    # On Windows, SIGTERM may not be present; guard carefully.
    with contextlib.suppress(Exception):
        loop.add_signal_handler(signal.SIGINT, _stop)
    with contextlib.suppress(Exception):
        loop.add_signal_handler(getattr(signal, "SIGTERM", signal.SIGINT), _stop)

    svc = ScannerService(cfg)
    try:
        loop.run_until_complete(svc.run(stop_evt))
    finally:
        # Ensure we close the loop cleanly
        tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            loop.run_until_complete(asyncio.gather(*tasks))
        loop.close()


if __name__ == "__main__":
    # Enable `python -m lap_logger --config ...`
    main()
