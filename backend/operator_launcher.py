import os
import pathlib
import sys
import subprocess
import time
import json
import urllib.request
import urllib.error
import webbrowser
import webview  # requires: pywebview, PySide6, qtpy

# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
ROOT = pathlib.Path(__file__).resolve().parents[1]   # repo root
OP_UI_DIR   = ROOT / "ui" / "operator"

HTTP_HOST = os.environ.get("PRS_UI_HOST", "localhost")
HTTP_PORT = int(os.environ.get("PRS_UI_PORT", "8000"))
def ui(path: str) -> str:
    # path should start with '/'; example: '/ui/operator/entrants.html'
    return f"http://{HTTP_HOST}:{HTTP_PORT}{path}"

SCREENS = {
    "splash":       OP_UI_DIR / "splash.html",
    "home":         OP_UI_DIR / "index.html",
    "race_setup":   OP_UI_DIR / "race_setup.html",
    "race_control": OP_UI_DIR / "race_control.html",
    "entrants":     OP_UI_DIR / "entrants.html",
    "stats":        OP_UI_DIR / "stats.html",
    "settings":     OP_UI_DIR / "settings.html",
    "about":        OP_UI_DIR / "about.html",
}

BACKEND_URL   = "http://127.0.0.1:8000"
HEALTH_URL    = f"{BACKEND_URL}/healthz"
SPECTATOR_URL = f"{BACKEND_URL}/ui/spectator/spectator.html"

_backend_proc: subprocess.Popen | None = None
_splash_win: webview.Window | None = None
_main_win: webview.Window | None = None


# --------------------------------------------------------------------------------------
# Backend lifecycle
# --------------------------------------------------------------------------------------
def _is_backend_up(timeout_seconds: float = 0.0) -> bool:
    deadline = time.time() + max(0.0, timeout_seconds)
    while True:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=1.5) as resp:
                if resp.status == 200:
                    try:
                        data = json.load(resp)
                        if isinstance(data, dict) and data.get("ok") is True:
                            return True
                    except Exception:
                        return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.25)

def _start_backend():
    global _backend_proc
    if _is_backend_up(0.0):
        return
    cmd = [
        sys.executable, "-m", "uvicorn",
        "backend.server:app",
        "--host", "127.0.0.1", "--port", "8000",
        # "--reload",  # enable during local dev if desired
    ]
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    _backend_proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    _is_backend_up(timeout_seconds=10.0)  # wait while splash shows

def _stop_backend():
    global _backend_proc
    if _backend_proc is None:
        return
    try:
        _backend_proc.terminate()
        try:
            _backend_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _backend_proc.kill()
    finally:
        _backend_proc = None


# --------------------------------------------------------------------------------------
# JS API exposed to the webview
# --------------------------------------------------------------------------------------
class Api:
    def __init__(self):
        self.window: webview.Window | None = None

    def goto(self, screen_id: str) -> bool:
        path = SCREENS.get(screen_id)
        if not path or not self.window:
            return False
        self.window.load_url(path.as_uri())
        return True

    def open_spectator(self) -> bool:
        try:
            webbrowser.open(SPECTATOR_URL, new=2)
            return True
        except Exception:
            return False


# --------------------------------------------------------------------------------------
# Bootstrap: runs on a background thread after GUI loop starts
# --------------------------------------------------------------------------------------
def _bootstrap():
    global _main_win, _splash_win
    _start_backend()  # blocks until healthy or timeout

    # Create the main window now that services are up
    api = Api()
    _main_win = webview.create_window(
        title="ChronoCoreRS — Operator Console",
        url=SCREENS["home"].as_uri(),
        width=1920, height=1080,
        resizable=True, 
        confirm_close=False,
        on_top=False,
        js_api=api,
    )
    api.window = _main_win

    # Close the splash
    if _splash_win:
        _splash_win.destroy()
        _splash_win = None


def main():
    global _splash_win
    # Show a small, frameless ChronoCore-branded splash immediately
    _splash_win = webview.create_window(
        title="ChronoCore — Starting…",
        url=SCREENS["splash"].as_uri(),
        width=460, height=280,
        resizable=False, frameless=True, on_top=True,
        confirm_close=False,
    )

    # Start GUI with Qt only (no fallbacks), run bootstrap in the background
    try:
        webview.start(gui="qt", http_server=False, debug=False, func=_bootstrap)
    finally:
        _stop_backend()


if __name__ == "__main__":
    main()
