# ============================================================================
# ChronoCore Race Software - OSC Inbound Handler
# ============================================================================
# Purpose:
#   Listens for OSC (Open Sound Control) messages from QLC+ lighting control
#   software, allowing QLC+ Virtual Console buttons to trigger flag changes
#   and other events in CCRS.
#
# Architecture:
#   - Uses ThreadingOSCUDPServer for non-blocking UDP reception
#   - Runs in a daemon thread to avoid blocking FastAPI event loop
#   - Dispatches incoming OSC messages to registered callback handlers
#
# OSC Message Format:
#   QLC+ sends OSC messages as: /path/to/button <float_value>
#   - Float values: 0.0 = OFF/released, 1.0 = ON/pressed
#   - Threshold (default 0.5) determines ON state
#
# Debouncing:
#   When QLC+ switches from one flag button to another:
#   1. Previous button sends OFF (0.0)
#   2. New button sends ON (1.0)
#   The debounce window ignores the OFF if an ON arrived recently,
#   preventing unwanted flag clears during rapid button switches.
#
# Configuration:
#   See config/config.yaml -> integrations.lighting.osc_in
#
# Dependencies:
#   - pythonosc: OSC protocol implementation
#
# ============================================================================

from __future__ import annotations
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer


@dataclass
class OscInConfig:
    """
    Configuration for OSC inbound receiver.
    
    Attributes:
        host: IP to bind to (0.0.0.0 = all interfaces, 127.0.0.1 = localhost only)
        port: UDP port to listen on (must match QLC+ feedback output port)
        flag_prefix: OSC address prefix for flag messages (e.g., /ccrs/flag/)
        path_blackout: OSC address for blackout control
        threshold_on: Float value threshold for ON state (>= this = ON)
        debounce_off_ms: Ignore OFF messages within this window after an ON
    """
    host: str = "0.0.0.0"
    port: int = 9010
    flag_prefix: str = "/ccrs/flag/"
    path_blackout: str = "/ccrs/blackout"
    threshold_on: float = 0.5
    debounce_off_ms: int = 250


class OscInbound:
    """
    OSC message receiver for QLC+ → CCRS communication.
    
    Listens for OSC messages from QLC+ and dispatches them to registered
    callback handlers. Runs in a background thread to avoid blocking the
    main application.
    
    Callbacks:
        on_flag(name: str): Called when a flag button is pressed
            - name: Flag name (green, yellow, red, white, checkered, blue)
        on_blackout(enabled: bool): Called when blackout button state changes
            - enabled: True if blackout is ON, False if OFF
        on_any(addr: str, values: list): Optional diagnostic callback for all messages
            - addr: Full OSC address path
            - values: List of OSC arguments (typically [float])
    
    Thread Safety:
        All callbacks are invoked from the OSC receiver thread. Keep handlers
        lightweight or use queue-based dispatch for heavy operations.
    """

    def __init__(
        self,
        cfg: OscInConfig,
        on_flag: Callable[[str], None],
        on_blackout: Optional[Callable[[bool], None]] = None,
        on_any: Optional[Callable[[str, list], None]] = None,
    ):
        """
        Initialize OSC inbound receiver.
        
        Args:
            cfg: Configuration for network binding and message parsing
            on_flag: Callback for flag state changes (required)
            on_blackout: Callback for blackout state changes (optional)
            on_any: Diagnostic callback for all received messages (optional)
        """
        self.cfg = cfg
        self._on_flag = on_flag
        self._on_blackout = on_blackout
        self._on_any = on_any
        
        # Server lifecycle management
        self._server: Optional[ThreadingOSCUDPServer] = None
        self._thread: Optional[threading.Thread] = None
        
        # Debounce tracking: timestamp of last ON message
        self._last_on_ts = 0.0

    # -------------------------------------------------------------------------
    # Lifecycle Management
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """
        Start the OSC receiver server.
        
        Creates a ThreadingOSCUDPServer bound to the configured host/port and
        starts it in a daemon thread. The server will run until stop() is called
        or the application exits.
        
        This method is idempotent - calling it multiple times has no effect if
        the server is already running.
        """
        # Already running - ignore duplicate start request
        if self._server:
            return
        
        # Set up message dispatcher with routing rules
        disp = Dispatcher()
        disp.set_default_handler(self._handle_default)  # Handles flag messages
        disp.map(self.cfg.path_blackout, self._handle_blackout)  # Specific blackout handler
        
        # Create UDP server bound to configured address
        self._server = ThreadingOSCUDPServer((self.cfg.host, self.cfg.port), disp)
        
        # Run server in daemon thread (won't block app shutdown)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="osc_inbound",
            daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """
        Stop the OSC receiver server and clean up resources.
        
        Shuts down the server gracefully and releases the UDP port. Safe to
        call even if the server isn't running.
        """
        if not self._server:
            return
        
        try:
            # Shut down server event loop and close socket
            self._server.shutdown()
            self._server.server_close()
        finally:
            # Clear references to allow garbage collection
            self._server = None
            self._thread = None

    # -------------------------------------------------------------------------
    # Message Handlers
    # -------------------------------------------------------------------------

    def _handle_default(self, addr: str, *args):
        """
        Default handler for all unmatched OSC messages.
        
        This handler processes flag messages and forwards all messages to the
        diagnostic callback if configured.
        
        Flag Message Format:
            Address: {flag_prefix}{flag_name}
            Example: /ccrs/flag/green → flag_name = "green"
            Value: float (0.0 = OFF, 1.0 = ON)
        
        Debouncing Logic:
            - ON messages: Always processed immediately
            - OFF messages: Ignored if an ON was received within debounce window
            
        This prevents spurious flag clears when switching between buttons in QLC+,
        since QLC+ sends OFF for the old button before sending ON for the new one.
        
        Args:
            addr: Full OSC address path
            *args: OSC message arguments (typically a single float)
        """
        # Forward to diagnostic callback if registered
        if self._on_any:
            try:
                self._on_any(addr, list(args))
            except Exception:
                # Swallow exceptions to prevent one bad callback from breaking others
                pass
        
        # Check if this is a flag message
        if addr.startswith(self.cfg.flag_prefix):
            # Extract flag name from address (e.g., "/ccrs/flag/green" → "green")
            name = addr[len(self.cfg.flag_prefix):].strip().lower()
            
            # Ignore malformed addresses
            if not name:
                return
            
            # Parse float value (default to 0.0 if missing)
            val = float(args[0]) if args else 0.0
            now = time.time()
            
            if val >= self.cfg.threshold_on:
                # ON message: update timestamp and invoke callback
                self._last_on_ts = now
                try:
                    self._on_flag(name)
                except Exception:
                    # Swallow exceptions to prevent callback failures from breaking receiver
                    pass
            else:
                # OFF message: check debounce window
                # Only process OFF if sufficient time has passed since last ON
                elapsed_ms = (now - self._last_on_ts) * 1000.0
                if elapsed_ms >= self.cfg.debounce_off_ms:
                    # If you want to implement "no flag active" state,
                    # you could call a clear/reset callback here.
                    # Currently we just ignore OFF messages within the debounce window.
                    pass

    def _handle_blackout(self, addr: str, *args):
        """
        Handler for blackout control messages.
        
        Blackout is a "hard kill" that can instantly disable all lighting.
        This is typically mapped to an emergency button in QLC+.
        
        Args:
            addr: OSC address (should be path_blackout)
            *args: OSC arguments (float value: 0.0 = OFF, 1.0 = ON)
        """
        # Skip if no callback registered
        if not self._on_blackout:
            return
        
        # Parse float value and convert to boolean via threshold
        val = float(args[0]) if args else 0.0
        try:
            self._on_blackout(val >= self.cfg.threshold_on)
        except Exception:
            # Swallow exceptions to prevent callback failures from breaking receiver
            pass
