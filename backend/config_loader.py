from pathlib import Path
import os, yaml

ROOT = Path(__file__).resolve().parents[1]            # repo root
CONFIG_DIR = Path(os.getenv("CC_CONFIG_DIR", ROOT / "config"))

APP_YAML    = CONFIG_DIR / "app.yaml"
MODES_YAML  = CONFIG_DIR / "race_modes.yaml"
EVENT_YAML  = CONFIG_DIR / "event.yaml"
LEGACY_YAML = ROOT / "backend" / "config.yaml"        # temporary fallback

def _load_yaml(p: Path) -> dict:
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        return {"__error__": f"{p.name}: {e}"}

def load_config():
    app   = _load_yaml(APP_YAML) or _load_yaml(LEGACY_YAML)
    modes = _load_yaml(MODES_YAML).get("modes", {})
    event = _load_yaml(EVENT_YAML).get("event", {})
    return {"app": app, "modes": modes, "event": event}

CONFIG = load_config()

def get_mode_cfg(name: str) -> dict:
    return CONFIG["modes"].get(name, {})
