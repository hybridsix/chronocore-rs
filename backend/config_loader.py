from __future__ import annotations
"""
backend/config_loader.py (drop-in)
----------------------------------
Loads configuration and exposes get_db_path().

Resolution order for DB path:
  1) app.engine.persistence.db_path (absolute or relative to repo root unless app.paths.root_base is set)
  2) legacy default: <repo_root>/backend/db/laps.sqlite
"""

from pathlib import Path
import os, yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = Path(os.getenv("CC_CONFIG_DIR", ROOT / "config"))

APP_YAML    = CONFIG_DIR / "app.yaml"
MODES_YAML  = CONFIG_DIR / "race_modes.yaml"
EVENT_YAML  = CONFIG_DIR / "event.yaml"

def _load_yaml(p: Path) -> dict:
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        # Keep callers resilient if a bad YAML sneaks in.
        return {}

def load_config() -> dict:
    doc = _load_yaml(APP_YAML)
    app_cfg = doc.get("app", doc)  # allow flat legacy format too

    # For very old configs, synthesize the persistence block so get_db_path() can fall back.
    app_cfg.setdefault("engine", {}).setdefault("persistence", {})

    modes = (_load_yaml(MODES_YAML) or {}).get("modes", {})
    event = (_load_yaml(EVENT_YAML) or {}).get("event", {})
    return {"app": app_cfg, "modes": modes, "event": event}

CONFIG = load_config()

def get_mode_cfg(name: str) -> dict:
    return CONFIG["modes"].get(name, {})

def get_db_path() -> Path:
    """
    Returns the SQLite file path.
    Accepts absolute or relative 'db_path' values. Relative paths resolve against repo root
    unless 'app.paths.root_base' is specified.
    """
    app_cfg = CONFIG.get("app", {})
    db_path = (
        app_cfg.get("engine", {})
               .get("persistence", {})
               .get("db_path")
    )
    if db_path:
        p = Path(db_path)
        if not p.is_absolute():
            root_base = (app_cfg.get("paths", {}) or {}).get("root_base")
            p = (Path(root_base) / p) if root_base else (ROOT / p)
        return p

    # Legacy default used in earlier repos
    return ROOT / "backend" / "db" / "laps.sqlite"
