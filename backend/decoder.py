from __future__ import annotations
import threading, time, socket, sys, re
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable

try:
    import serial  # pyserial
except Exception:
    serial = None  # guarded below

from .race_engine import ENGINE
from .config_loader import load_config  # must return a dict with an "app" key

# ---------- small helpers ----------

def now_ms() -> int:
    return int(time.time() * 1000)

def safe_int(x, default=None):
    try: return int(x)
    except: return default

def _log(*a):
    print("[Decoder]", *a, file=sys.stderr, flush=True)

# ---------- config snapshot ----------

@dataclass
class DecoderConfig:
    enabled: bool = False
    # modes:
    #   ilap_serial | ambrc_serial | trackmate_serial | cano_tcp | tcp_line | mock
    type: str = "ilap_serial"

    # serial
    port: Optional[str] = None
    baud: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout_s: float = 0.25

    # tcp
    host: Optional[str] = None
    tcp_port: Optional[int] = None

    # ilap specifics
    ilap_init_7digit: bool = True

    # reconnection / mock
    reconnect_delay_s: float = 2.0
    mock_tag: str = "3000999"
    mock_period_s: float = 6.0

    # (optional) pit receiver hints
    pit_in_receivers: tuple[str, ...] = ()
    pit_out_receivers: tuple[str, ...] = ()

    # optional regex hook (advanced) for serial_line modes
    line_regex: Optional[str] = None  # must capture tag as (?P<tag>...), decoder as (?P<decoder>...) optional

    @classmethod
    def from_app(cls, app_dict: Dict[str, Any]) -> "DecoderConfig":
        dec = (app_dict or {}).get("decoder", {}) or {}
        serial_cfg = dec.get("serial", {}) or {}
        tcp_cfg = dec.get("tcp", {}) or {}
        ilap_cfg = dec.get("ilap", {}) or {}
        routing = (dec.get("routing") or {})  # strings (receiver ids)

        return cls(
            enabled=bool(dec.get("enabled", False)),
            type=str(dec.get("mode") or dec.get("type") or "ilap_serial"),

            # serial
            port=serial_cfg.get("port"),
            baud=int(serial_cfg.get("baud", 9600)),
            bytesize=int(serial_cfg.get("bytesize", 8)),
            parity=str(serial_cfg.get("parity", "N")),
            stopbits=int(serial_cfg.get("stopbits", 1)),
            timeout_s=float(serial_cfg.get("timeout_s", 0.25)),

            # tcp
            host=tcp_cfg.get("host"),
            tcp_port=safe_int(tcp_cfg.get("port")),

            # ilap
            ilap_init_7digit=bool(ilap_cfg.get("init_7digit", True)),

            reconnect_delay_s=float(dec.get("reconnect_delay_s", 2.0)),
            mock_tag=str(dec.get("mock_tag", "3000999")),
            mock_period_s=float(dec.get("mock_period_s", 6.0)),

            pit_in_receivers=tuple(str(x) for x in (routing.get("pit_in_receivers") or [])),
            pit_out_receivers=tuple(str(x) for x in (routing.get("pit_out_receivers") or [])),

            line_regex=str(dec.get("line_regex")) if dec.get("line_regex") else None,
        )

# ---------- base decoder ----------

class BaseDecoder:
    def __init__(self, cfg: DecoderConfig):
        self.cfg = cfg
        self._t: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._status = "idle"

    def start(self):
        if self._t and self._t.is_alive():
            return
        self._stop.clear()
        self._t = threading.Thread(target=self._run_loop, name=f"Decoder[{self.__class__.__name__}]", daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t and self._t.is_alive():
            self._t.join(timeout=2.0)

    def is_running(self) -> bool:
        return bool(self._t and self._t.is_alive())

    def status(self) -> Dict[str, Any]:
        return {"type": self.__class__.__name__, "running": self.is_running(), "status": self._status}

    # subclasses implement this
    def _run_loop(self):  # pragma: no cover
        raise NotImplementedError

    # uniform emit to engine
    def emit_pass(self, tag: str, decoder_id: Optional[str] = None):
        ENGINE.ingest_pass(tag=str(tag), device_id=str(decoder_id) if decoder_id else None, source="track")

# ---------- shared serial reader scaffold ----------

class _SerialLineDecoder(BaseDecoder):
    """
    Opens a serial port and feeds lines to _handle_line(str) implemented by subclasses.
    """
    def _run_loop(self):
        if serial is None:
            self._status = "pyserial not installed"
            _log("pyserial not installed; cannot open serial")
            return

        port = self.cfg.port
        self._status = f"connecting {port}@{self.cfg.baud}"
        while not self._stop.is_set():
            try:
                with serial.Serial(
                    port,
                    self.cfg.baud,
                    bytesize=self.cfg.bytesize,
                    parity=self.cfg.parity,
                    stopbits=self.cfg.stopbits,
                    timeout=self.cfg.timeout_s,
                ) as ser:
                    self._status = f"open {port}"
                    self._on_open(ser)

                    while not self._stop.is_set():
                        raw = ser.readline()
                        if not raw:
                            continue
                        try:
                            line = raw.decode(errors="replace").strip()
                        except Exception:
                            continue
                        if self._handle_line(raw, line):
                            # subclasses return True when a valid pass was emitted
                            pass
            except Exception as e:
                self._status = f"error: {e}"
                _log(f"serial error on {port}:", e)
                time.sleep(self.cfg.reconnect_delay_s)
            if not self._stop.is_set():
                time.sleep(0.2)
        self._status = "stopped"

    # hooks
    def _on_open(self, ser):  # pragma: no cover
        pass

    def _handle_line(self, raw: bytes, line: str) -> bool:  # pragma: no cover
        raise NotImplementedError

# ---------- iLAP serial decoder ----------

class ILapSerialDecoder(_SerialLineDecoder):
    """
    iLAP ASCII, framed by SOH (0x01) and lines beginning with '@':
        RAW:  \x01@\t<decoder_id>\t<tag_id>\t<secs.mss>\r\n
        TXT:  "@\t<decoder_id>\t<tag_id>\t<secs.mss>"
    """
    INIT_7DIGIT = bytes([0x01, ord('%'), 0x0D, 0x0A])  # \x01 '%' '\r' '\n'

    def _on_open(self, ser):
        if self.cfg.ilap_init_7digit:
            try:
                ser.write(self.INIT_7DIGIT)
                ser.flush()
            except Exception as e:
                _log("iLAP init_7digit write failed:", e)

    def _handle_line(self, raw: bytes, line: str) -> bool:
        if not raw.startswith(b"\x01@"):
            return False
        txt = line.replace("\x01", "")
        parts = txt.split("\t")
        if len(parts) < 4 or parts[0] != "@":
            return False
        try:
            decoder_id = int(parts[1]); tag_id = int(parts[2]); t_secs = float(parts[3])
        except ValueError:
            return False
        self.emit_pass(tag=str(tag_id), decoder_id=str(decoder_id))
        self._status = f"last tag={tag_id} t={t_secs:.3f}"
        return True

# ---------- AMB/MyLaps (legacy AMBrc-like) serial decoder ----------

class AMBRcSerialDecoder(_SerialLineDecoder):
    """
    Many AMBrc-compatible devices output simple CSV-ish or token lines.
    We accept a few common shapes and a configurable regex escape hatch.

    Supported out-of-the-box (first match wins):
      1) "TAG,<tag_id>" or "PASS,<tag_id>,<secs>"
      2) "decoder=<id> tag=<tag> time=<secs>"
      3) CSV: "<decoder_id>,<tag_id>,<secs>"

    Advanced: set app.decoder.line_regex with named groups (?P<tag>...) and optional (?P<decoder>...)
    """
    _re_keyvals = re.compile(r"(?:^|[ ,])(?P<key>tag|decoder|time)\s*=\s*(?P<val>[^ ,]+)")

    def _handle_line(self, raw: bytes, line: str) -> bool:
        # custom regex?
        if self.cfg.line_regex:
            m = re.search(self.cfg.line_regex, line)
            if m and m.groupdict().get("tag"):
                tag = m.group("tag")
                dec = m.groupdict().get("decoder")
                self.emit_pass(tag=str(tag), decoder_id=str(dec) if dec else None)
                self._status = f"last tag={tag} (regex)"
                return True

        up = line.strip().upper()

        # Shape 1: TAG,<id>  /  PASS,<id>,<secs>
        if up.startswith("TAG,") or up.startswith("PASS,"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                tag = parts[1]
                dec = None
                self.emit_pass(tag=str(tag), decoder_id=dec)
                self._status = f"last tag={tag}"
                return True

        # Shape 2: key=val tokens
        if "=" in line:
            kvs = dict((m.group("key"), m.group("val")) for m in self._re_keyvals.finditer(line))
            if "tag" in kvs:
                self.emit_pass(tag=str(kvs["tag"]), decoder_id=str(kvs.get("decoder")) if kvs.get("decoder") else None)
                self._status = f"last tag={kvs['tag']}"
                return True

        # Shape 3: CSV: <decoder>,<tag>,<secs>
        if "," in line:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                dec = parts[0] if parts[0] else None
                tag = parts[1]
                self.emit_pass(tag=str(tag), decoder_id=str(dec) if dec else None)
                self._status = f"last tag={tag}"
                return True

        return False

# ---------- Trackmate IR serial decoder ----------

class TrackmateSerialDecoder(_SerialLineDecoder):
    """
    Trackmate USB-serial devices generally emit a tag id per line,
    sometimes with an optional station/decoder prefix.

    Accepted:
      - "<tag_id>"
      - "<decoder_id>,<tag_id>"
    """
    def _handle_line(self, raw: bytes, line: str) -> bool:
        s = line.strip()
        if not s:
            return False
        if "," in s:
            parts = [p.strip() for p in s.split(",")]
            if len(parts) >= 2 and parts[1]:
                self.emit_pass(tag=str(parts[1]), decoder_id=str(parts[0]) if parts[0] else None)
                self._status = f"last tag={parts[1]}"
                return True
        else:
            # bare tag
            self.emit_pass(tag=str(s), decoder_id=None)
            self._status = f"last tag={s}"
            return True
        return False

# ---------- CANO-style TCP decoder (one line per pass) ----------

class CANOTcpDecoder(BaseDecoder):
    """
    Minimal TCP line reader for CANO-ish or DIY bridges.
    Each line should contain either "<tag>" or "<decoder>,<tag>".
    """
    def _run_loop(self):
        host = self.cfg.host or "127.0.0.1"
        port = self.cfg.tcp_port or 3000
        self._status = f"connecting tcp://{host}:{port}"
        while not self._stop.is_set():
            try:
                with socket.create_connection((host, port), timeout=5.0) as s:
                    s_file = s.makefile("rb")
                    self._status = f"open tcp://{host}:{port}"
                    while not self._stop.is_set():
                        raw = s_file.readline()
                        if not raw:
                            time.sleep(0.05)
                            continue
                        try:
                            line = raw.decode(errors="replace").strip()
                        except Exception:
                            continue
                        if not line:
                            continue
                        if "," in line:
                            dec, tag = [p.strip() for p in line.split(",", 1)]
                            if tag:
                                self.emit_pass(tag=str(tag), decoder_id=str(dec) if dec else None)
                                self._status = f"last tag={tag}"
                        else:
                            self.emit_pass(tag=str(line), decoder_id=None)
                            self._status = f"last tag={line}"
            except Exception as e:
                self._status = f"error: {e}"
                _log("tcp error:", e)
                time.sleep(self.cfg.reconnect_delay_s)
        self._status = "stopped"

# ---------- Generic TCP (“just a line”) and Mock ----------

class TCPLineDecoder(CANOTcpDecoder):
    """Alias: same behavior; left for backward compatibility."""
    pass

class MockDecoder(BaseDecoder):
    def _run_loop(self):
        tag = self.cfg.mock_tag or "3000999"
        period = max(0.5, float(self.cfg.mock_period_s or 6.0))
        self._status = f"mocking tag={tag} every {period:.1f}s"
        while not self._stop.is_set():
            self.emit_pass(tag=tag, decoder_id="mock")
            for _ in range(int(period * 10)):
                if self._stop.is_set(): break
                time.sleep(0.1)
        self._status = "stopped"

# ---------- factory + manager ----------

DECODER_TYPES: Dict[str, Callable[[DecoderConfig], BaseDecoder]] = {
    "ilap_serial":     ILapSerialDecoder,
    "ambrc_serial":    AMBRcSerialDecoder,
    "trackmate_serial":TrackmateSerialDecoder,
    "cano_tcp":        CANOTcpDecoder,
    "tcp_line":        TCPLineDecoder,
    "mock":            MockDecoder,
}

class DecoderManager:
    def __init__(self):
        self._lock = threading.RLock()
        self.cfg = self._load_cfg()
        self.decoder: Optional[BaseDecoder] = None

    def _load_cfg(self) -> DecoderConfig:
        cfg = load_config()
        app = cfg.get("app", {})
        return DecoderConfig.from_app(app)

    def reload_config(self):
        with self._lock:
            self.cfg = self._load_cfg()
            return self.cfg

    def start(self):
        with self._lock:
            if self.decoder and self.decoder.is_running():
                return self.status()
            cls = DECODER_TYPES.get(self.cfg.type, ILapSerialDecoder)
            self.decoder = cls(self.cfg)
            self.decoder.start()
            return self.status()

    def stop(self):
        with self._lock:
            if self.decoder:
                self.decoder.stop()
            return self.status()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            s = {"enabled": self.cfg.enabled, "type": self.cfg.type, "running": False, "detail": None}
            if self.decoder:
                st = self.decoder.status()
                s.update({"running": st.get("running", False), "detail": st.get("status")})
            return s

# global singleton
DECODER_MANAGER = DecoderManager()
