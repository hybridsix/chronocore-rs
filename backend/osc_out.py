# backend/osc_out.py
# -----------------------------------------------------------------------------
# CCRS → QLC+ (OSC OUT)
# Sends proper OSC frames (UDP unicast) to QLC+ when CCRS changes flags
# or overlay states. Uses python-osc's SimpleUDPClient (sync, tiny, reliable).
# -----------------------------------------------------------------------------

from __future__ import annotations
import time
from typing import Dict, Optional
from pythonosc.udp_client import SimpleUDPClient

class OscLightingOut:
    def __init__(self, cfg: Dict):
        # cfg structure:
        # integrations.lighting.osc_out: { enabled, host, port, send_repeat, addresses{ flag{...}, blackout } }
        self.enabled = bool(cfg and cfg.get("enabled"))
        self.host = (cfg or {}).get("host", "127.0.0.1")
        self.port = int((cfg or {}).get("port", 9000))

        rep = (cfg or {}).get("send_repeat") or {}
        self.repeat_count = int(rep.get("count", 1))        # e.g. 2 -> send twice
        self.repeat_interval = float(rep.get("interval_ms", 0)) / 1000.0

        addrs = ((cfg or {}).get("addresses") or {})
        self.addr_flag: Dict[str, str] = (addrs.get("flag") or {})   # {"green": "/ccrs/flag/green", ...}
        self.addr_blackout: str = addrs.get("blackout", "/ccrs/blackout")

        self._client: Optional[SimpleUDPClient] = None

    def start(self) -> None:
        if not self.enabled:
            return
        if self._client is None:
            self._client = SimpleUDPClient(self.host, self.port)

    def stop(self) -> None:
        self._client = None

    # --------------------- internal helper ---------------------

    def _send(self, path: str, value: float) -> None:
        """Fire-and-forget with optional repeats for UDP resiliency."""
        if not self.enabled or not self._client:
            return
        sends = max(1, self.repeat_count)
        for i in range(sends):
            try:
                self._client.send_message(path, float(value))
            except Exception:
                # Never blow up race control just because lighting is offline.
                pass
            if i + 1 < sends and self.repeat_interval > 0:
                time.sleep(self.repeat_interval)

    # ----------------------- public API -----------------------

    def send_flag(self, name: str, on: bool = True) -> None:
        """Send the path mapped to this flag name; value 1.0 = ON, 0.0 = OFF."""
        path = self.addr_flag.get(name.lower())
        if not path:
            return
        self._send(path, 1.0 if on else 0.0)

    def send_blackout(self, enabled: bool) -> None:
        self._send(self.addr_blackout, 1.0 if enabled else 0.0)
