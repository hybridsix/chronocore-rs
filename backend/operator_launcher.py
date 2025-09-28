import pathlib
import webview
import webbrowser  # <-- needed for open_spectator()

# Project root (prs_starter/)
ROOT = pathlib.Path(__file__).resolve().parents[1]
UI   = ROOT / "ui" / "operator"

SCREENS = {
    "home": UI / "index.html",
    "race_control": UI / "race_control.html",
    "entrants": UI / "entrants.html",
    "stats": UI / "stats.html",
    "settings": UI / "settings.html",
    "about": UI / "about.html",   # optional; create this when ready
}

class Api:
    def __init__(self):
        self.window = None  # set after window is created

    def goto(self, screen_id: str) -> bool:
        """
        Navigate to a different screen.
        Called from JS with: window.pywebview.api.goto('race_control')
        """
        path = SCREENS.get(screen_id)
        if not path or not self.window:
            return False
        self.window.load_url(path.as_uri())
        return True

    def open_spectator(self) -> bool:
        """
        Launch the spectator UI in the user's default browser.
        Adjust the URL if your spectator route differs.
        """
        url = "http://localhost:8000/ui"  # e.g. "/ui" or "/ui/spectator.html?race_id=1"
        try:
            webbrowser.open(url, new=2)  # open in new tab/window
            return True
        except Exception:
            return False

def main():
    start_url = SCREENS["home"].as_uri()

    api = Api()  # create API object first
    window = webview.create_window(
        title="PRS — Operator Console",
        url=start_url,
        width=1280,
        height=800,
        resizable=True,
        confirm_close=True,
        js_api=api,  # expose API to JS
    )
    api.window = window  # attach the window back to the API

    webview.start(gui="qt", http_server=False, debug=False)

if __name__ == "__main__":
    main()

