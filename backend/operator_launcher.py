"""
operator_launcher.py — ChronoCoreRS Operator Console launcher

PURPOSE
-------
This script is a small "glue" layer that:
  1) Boots the FastAPI backend (uvicorn) as a local subprocess.
  2) Waits until health checks are green (or times out with a helpful message).
  3) Opens the Operator UI in a pywebview window.

IMPORTANT DESIGN CHOICES
------------------------
• We KEEP the existing SCREENS mapping that points to local HTML files on disk.
  - This is useful for a splash screen or emergency "safe mode."
  - It also documents where the UI files live in the repo.

• BUT we PREFER to load the main Operator page from the backend (http://127.0.0.1:8000/ui/operator/).
  - When the UI is served by the backend, all relative fetch('/...') calls are same-origin,
    which avoids the classic 'Failed to fetch' you saw with file:// origins.
  - If the backend isn't reachable (e.g., you kill it or it fails to start), we auto-fallback
    to the local file path so you still get *something* on screen.

• We expose a tiny JS API (the 'Api' class) so HTML pages can request navigation, open URLs, etc.

SAFETY / DIAGNOSTICS
--------------------
• On startup we log the detected project paths and the backend URL being used.
• The backend is stopped on exit to avoid zombie processes.
• Health checks target /healthz and we also optionally probe /readyz for DB readiness.
"""

from __future__ import annotations

import os
import sys
import json
import time
import subprocess
import urllib.request
import urllib.error
import webbrowser
from pathlib import Path
from typing import Optional
from http.client import HTTPResponse

import webview  # requires: pywebview + a GUI backend (PySide6 recommended)
# These imports are part of the ChronoCore backend and may no-op if not used directly here.
from backend.db_schema import ensure_schema  # idempotent schema bootstrap (safe to import)
from backend.config_loader import CONFIG     # parsed config (YAML), safe to import


# --------------------------------------------------------------------------------------
# Paths & Constants
# --------------------------------------------------------------------------------------

# Resolve project root based on this file's typical location.
# This file usually lives at <repo_root>/operator_launcher.py or <repo_root>/backend/…/operator_launcher.py
THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parent  # default guess
# If we're inside backend/, move up a level to the repo root
if (THIS_FILE.parent.name == "backend") or (THIS_FILE.parent.name == "operator"):
    REPO_ROOT = THIS_FILE.parent.parent

# UI folder (the real source of our Operator HTML files)
UI_DIR = REPO_ROOT / "ui" / "operator"

# Optional: a secondary location (FastAPI may mount <repo_root>/ui/ at /ui)
UI_TOP = REPO_ROOT / "ui"

# Backend serving address — adjustable by env if needed.
# We prefer explicit loopback to avoid name resolution snags on some Windows setups.
BACKEND_HOST = os.environ.get("CCRS_UI_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("CCRS_UI_PORT", "8000"))
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"

# Health & readiness endpoints (as implemented in server.py)
HEALTH_URL = f"{BACKEND_URL}/healthz"
READY_URL  = f"{BACKEND_URL}/readyz"

# Optional convenience: spectator page deep-link (used by Api.open_spectator)
SPECTATOR_URL = f"{BACKEND_URL}/ui/spectator/spectator.html"

# Persistent DB path (best-effort: try to read from CONFIG, else fallback)
# This is for logging/visibility; DB creation is handled by the backend.
DB_PATH = None
try:
    # Expecting CONFIG like: {"persistence": {"db_path": "..."}}
    DB_PATH = Path(CONFIG.get("persistence", {}).get("db_path", "")).resolve()
except Exception:
    DB_PATH = Path(REPO_ROOT / "backend" / "db" / "laps.sqlite").resolve()

# Build the SCREENS mapping to LOCAL FILES (these stay as-is — we won’t break them)
# Note: These are used for the splash screen and as a fallback when backend is unreachable.
SCREENS = {
    "splash": (UI_DIR / "splash.html"),      # Small frameless splash while services warm up
    "home":   (UI_DIR / "index.html"),       # Main Operator UI entrypoint
    "about":  (UI_DIR / "about.html"),
    "results": (UI_DIR / "results_exports.html"),
    "entrants": (UI_DIR / "entrants.html"),
    "race_setup": (UI_DIR / "race_setup.html"),
    "race_control": (UI_DIR / "race_control.html"),
    "settings": (UI_DIR / "settings.html"),
}

# Uvicorn app target. Adjust if you've renamed things.
# The canonical app is APP in backend/server.py → "backend.server:APP"
UVICORN_APP = os.environ.get("CCRS_UVICORN_APP", "backend.server:APP")

# Give ourselves a global handle for cleanup & API plumbing
_backend_proc: Optional[subprocess.Popen] = None
_splash_win: Optional[webview.Window] = None
_main_win:   Optional[webview.Window] = None


# --------------------------------------------------------------------------------------
# Small utility helpers
# --------------------------------------------------------------------------------------

def _log(msg: str) -> None:
    """Lightweight stdout logger with a consistent prefix."""
    print(f"[operator_launcher] {msg}", flush=True)


def _http_get(url: str, timeout: float = 0.75) -> Optional[HTTPResponse]:
    """Tiny GET wrapper that returns a response or None on error; never throws."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        return urllib.request.urlopen(req, timeout=timeout)  # nosec - loopback only
    except Exception as e:
        _log(f"HTTP GET failed: {url} :: {e.__class__.__name__}: {e}")
        return None


def _backend_healthy() -> bool:
    """
    Consider the backend 'healthy' if /healthz returns HTTP 200.
    If JSON is present, accept common truthy shapes too.
    """
    resp = _http_get(HEALTH_URL, timeout=0.75)
    if not resp or getattr(resp, "status", 0) != 200:
        return False

    # If we can parse JSON, accept common patterns, but don't require them.
    try:
        raw = resp.read().decode("utf-8") or ""
        data = json.loads(raw)
        # Accept a variety of keys/values people use for health checks
        vals = [
            data.get("ok"),
            data.get("ready"),
            data.get("alive"),
            data.get("healthy"),
            data.get("status"),
        ]
        # True if any is True or "ok"/"ready"/"alive"/"healthy"/"true" (case-insensitive)
        for v in vals:
            if v is True:
                return True
            if isinstance(v, str) and v.strip().lower() in {"ok", "ready", "alive", "healthy", "true"}:
                return True
        # No recognizable keys? still fine—HTTP 200 is enough.
        return True
    except Exception:
        # Not JSON? fine. 200 means healthy.
        return True


def _backend_ready() -> bool:
    """
    Return True if /readyz returns 200 and {db_ready: true} or similar.
    We don't hard-pin the schema of /readyz; we just look for truthy flags.
    """
    resp = _http_get(READY_URL, timeout=0.75)
    if not resp or getattr(resp, "status", 0) != 200:
        return False
    try:
        data = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:
        return False
    # lenient: if any of these keys is True, consider 'ready'
    keys = ("ready", "db_ready", "database", "db_exists")
    return any(bool(data.get(k, False)) for k in keys)


def _serve_url(path: str) -> str:
    """
    Build a fully-qualified URL pointing at the backend's static UI mount.

    Examples:
      _serve_url("/ui/operator/")              -> "http://127.0.0.1:8000/ui/operator/"
      _serve_url("/ui/operator/index.html")    -> "http://127.0.0.1:8000/ui/operator/index.html"
    """
    if not path.startswith("/"):
        path = "/" + path
    return f"{BACKEND_URL}{path}"


def _file_uri(screen_id: str) -> str:
    """Return a file:// URI for a given screen id from SCREENS, raising if missing."""
    path = SCREENS[screen_id]
    return path.resolve().as_uri()


def _get_screen_url(screen_id: str) -> str:
    """
    Smart URL resolver:
      - For 'home' (the main Operator UI), PREFER the backend-served URL so that
        all fetch('/...') calls are same-origin with the API. If the backend isn't
        reachable yet, fall back to the local file:// URI.
      - For all other pages (splash, etc.), default to local files. We can decide later
        if any of these should prefer served URLs too.
    """
    if screen_id == "home":
        if _backend_healthy():
            # Load the directory URL so StaticFiles serves index.html by default.
            return _serve_url("/ui/operator/")
        # Health probe failed — use local file as a fallback so users see *something*.
        return _file_uri("home")

    # Non-home pages default to local files (splash is an example that should come up immediately)
    return _file_uri(screen_id)


# --------------------------------------------------------------------------------------
# Backend process management
# --------------------------------------------------------------------------------------

def _start_backend(timeout_s: float = 15.0) -> None:
    """
    Start the FastAPI/uvicorn backend as a subprocess IF HEALTH CHECKS FAIL.
    - If the backend is already up (another terminal or service), we don't spawn a duplicate.
    - Otherwise, we launch: uvicorn backend.server:APP --host 127.0.0.1 --port 8000 --reload?
      (We don't force --reload here; you can run reload yourself during development.)
    - We then wait until /healthz returns ok OR until timeout.
    """
    global _backend_proc

    # If someone already started the backend, don't fight them.
    if _backend_healthy():
        _log(f"Backend already healthy at {BACKEND_URL} — not spawning a new process.")
        return

    _log(f"Starting backend process for {UVICORN_APP} on {BACKEND_URL} ...")

    # Construct uvicorn command
    uvicorn_cmd = [
        sys.executable, "-m", "uvicorn",
        UVICORN_APP,
        "--host", BACKEND_HOST,
        "--port", str(BACKEND_PORT),
        # For production, omit --reload. For dev, you can uncomment this.
        # "--reload",
        # Make logs friendlier in our console:
        "--log-level", "info",
    ]

    # Launch the backend as a subprocess with a detached group so we can clean up at exit.
    _backend_proc = subprocess.Popen(
        uvicorn_cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True
    )

    # Stream a little output while we wait (helpful for diagnosing startup issues)
    start_ts = time.time()
    line_cache = []  # keep last few lines for error display if we time out
    while time.time() - start_ts < timeout_s:
        # Drain any available lines from uvicorn (non-blocking-ish)
        if _backend_proc.poll() is not None:
            # Process exited early — print what we have and bail
            _log("Backend process exited unexpectedly during startup.")
            for ln in line_cache[-12:]:
                _log(f"> {ln.strip()}")
            break

        # Read a line if available
        if _backend_proc.stdout and not _backend_proc.stdout.closed:
            _backend_proc.stdout.flush()
            try:
                ln = _backend_proc.stdout.readline()
            except Exception:
                ln = ""
            if ln:
                line_cache.append(ln)
                # Show a few key lines to reassure the user
                if "Uvicorn running on" in ln or "Started server process" in ln:
                    _log(ln.strip())

        # Check health every 200ms
        if _backend_healthy():
            _log("Backend /healthz is OK.")
            # Optional: we could also wait for /readyz to succeed before continuing.
            # We'll be lenient — the Operator UI can show its own DB readiness states.
            return

        time.sleep(0.2)

    # If we reach here, we didn't get healthy in time — show context for debugging.
    _log(f"Timed out waiting for backend to become healthy after {timeout_s:.1f}s.")
    if line_cache:
        _log("Recent backend logs:")
        for ln in line_cache[-20:]:
            _log(f"> {ln.rstrip()}")


def _stop_backend() -> None:
    """Terminate the uvicorn subprocess if we started it (no-op if user started it externally)."""
    global _backend_proc
    if _backend_proc is None:
        return
    try:
        _log("Stopping backend subprocess...")
        _backend_proc.terminate()
        try:
            _backend_proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            _log("Backend did not exit promptly; killing...")
            _backend_proc.kill()
    finally:
        _backend_proc = None


# --------------------------------------------------------------------------------------
# pywebview API exposed to the Operator UI (window.pywebview.api)
# --------------------------------------------------------------------------------------

class Api:
    """
    Methods on this class become accessible from JavaScript via window.pywebview.api.<method>().
    Keep these small and safe — they're part of our UI surface area.
    """
    def __init__(self) -> None:
        self.window: Optional[webview.Window] = None

    # --------------------
    # Simple diagnostics
    # --------------------
    def ping(self) -> str:
        """Roundtrip test from JS: await window.pywebview.api.ping()."""
        return "pong"

    def get_origin(self) -> str:
        """Return the current window origin, as seen from Python (for debugging)."""
        try:
            if not self.window:
                return "<no-window>"
            url = self.window.get_current_url()
            return url or "<unknown>"
        except Exception:
            return "<unknown>"


    # --------------------
    # Navigation helpers
    # --------------------
    def goto(self, screen_id: str) -> bool:
        """
        Navigate to a different operator page.
        Logic:
          - If backend is healthy, prefer SERVED URL so fetch('/...') stays same-origin.
          - Else, fall back to file:// so we can still show the requested page.
        """
        if screen_id not in SCREENS:
            return False
        if not self.window:
            return False

        # Prefer served page if possible (specific file, e.g. entrants.html)
        if _backend_healthy():
            served_url = _serve_url(f"/ui/operator/{SCREENS[screen_id].name}")
            self.window.load_url(served_url)
            return True

        # Fallback to file:// path
        self.window.load_url(SCREENS[screen_id].resolve().as_uri())
        return True

    # --------------------
    # External links
    # --------------------
    def open_external(self, url: str) -> bool:
        """Open a URL in the system default browser (used for docs, GitHub, etc.)."""
        try:
            webbrowser.open(url, new=2)
            return True
        except Exception:
            return False

    def open_spectator(self) -> bool:
        """Convenience to open the spectator page in the system browser."""
        return self.open_external(SPECTATOR_URL)


# --------------------------------------------------------------------------------------
# Bootstrap: show splash, start backend, create main window, then remove splash
# --------------------------------------------------------------------------------------

def _bootstrap():
    """
    This function runs on a background thread AFTER the GUI event loop starts.
    We use it to:
      - Start (or attach to) the backend service.
      - Create the main window ONLY after /healthz is OK (so the UI can fetch data).
      - Close the splash.
    """
    global _main_win, _splash_win

    # Visibility: let the console show where our DB file is expected to live
    recreate_on_boot = bool(CONFIG.get("persistence", {}).get("recreate_on_boot", False))
    _log(f"DB: {DB_PATH} (recreate_on_boot={recreate_on_boot})")
    _log(f"Backend target: {BACKEND_URL}")

    # Start or attach to the backend; block until (best-effort) healthy or timeout.
    _start_backend()

    # Create the main window using the BEST URL (served if possible, else file://)
    api = Api()
    _main_win = webview.create_window(
        title="ChronoCoreRS — Operator Console",
        url=_get_screen_url("home"),
        width=1920, height=1080,
        resizable=True,
        confirm_close=False,
        on_top=False,
        js_api=api,
    )
    api.window = _main_win

    # Close the splash if it's still up
    if _splash_win:
        try:
            _splash_win.destroy()
        finally:
            pass  # ignore small timing errors
        _splash_win = None


def main():
    """
    Entry point:
      1) Show a small, frameless splash immediately (loads from file:// for zero dependencies).
      2) Start the GUI loop and run _bootstrap() on a background thread.
      3) On exit, stop the backend if we started it.
    """
    global _splash_win

    # Splash comes from local file so users get instant feedback even if backend is cold.
    splash_uri = _get_screen_url("splash")
    _splash_win = webview.create_window(
        title="ChronoCore — Starting…",
        url=splash_uri,
        width=460, height=280,
        resizable=False, frameless=True, on_top=True,
        confirm_close=False,
    )

    # Start GUI with Qt only (no confusing fallbacks), run bootstrap in the background.
    try:
        # debug=False → production-ish. Flip to True when you want DevTools.
        webview.start(gui="qt", http_server=False, debug=False, func=_bootstrap)
    finally:
        _stop_backend()


if __name__ == "__main__":
    main()
