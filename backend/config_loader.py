# backend/config_loader.py
from __future__ import annotations
"""
Unified configuration loader for ChronoCore (CCRS).

Single source of truth:
    config/config.yaml

Design notes
------------
- Forward-only: no legacy multi-file merging, no normalization from old shapes.
- If the file is missing or broken, we raise a friendly RuntimeError that
  prints absolute paths for quick fixes.
- Unknown keys are fine; we pass the full dict through untouched.
- Helpers return {} or sensible defaults when sections are absent.
- Paths are absolute (resolved against the repo root) unless already absolute.

Public API
----------
- CONFIG: dict                              # eager-loaded contents of config/config.yaml
- load_config(path: str|Path|None = None)   # explicit reload (mainly for tests/tools)
- get_event() -> dict
- get_db_path() -> pathlib.Path
- get_scanner_cfg() -> dict
- get_publisher_cfg() -> dict
- get_log_level(default: str = "INFO") -> str
- get_server_bind() -> tuple[str, int]
"""

import os
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

# ---------- Files & roots ----------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR   = PROJECT_ROOT / "config"
DEFAULT_CFG  = CONFIG_DIR / "config.yaml"


# ---------- I/O helpers ----------
def _load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML mapping from `path`. Human-friendly errors, strict root type."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RuntimeError(
            f"Missing configuration file: {path}\n"
            f"Expected a single-file config with an 'app:' section.\n"
            f"Repo root: {PROJECT_ROOT}"
        )
    except Exception as ex:
        raise RuntimeError(f"Failed to read {path}: {type(ex).__name__}: {ex}")

    try:
        data = yaml.safe_load(text) or {}
    except Exception as ex:
        raise RuntimeError(f"Failed to parse YAML {path}: {type(ex).__name__}: {ex}")

    if not isinstance(data, dict):
        raise RuntimeError(f"Root of {path} must be a mapping/object, not {type(data).__name__}")
    return data


def _resolve_path(p: str | os.PathLike[str]) -> Path:
    """Return absolute path; resolve relative to repo root."""
    pth = Path(p)
    return pth if pth.is_absolute() else (PROJECT_ROOT / pth).resolve()


# ---------- Loader ----------
def load_config(path: str | os.PathLike[str] | None = None) -> Dict[str, Any]:
    """
    Load a single YAML file (default: config/config.yaml), validate required shape,
    and return the raw dict (unmodified).
    """
    cfg_path = _resolve_path(path) if path else DEFAULT_CFG
    cfg = _load_yaml(cfg_path)

    # Minimal structural contract for engine startup:
    try:
        app = cfg["app"]
        engine = app["engine"]
        persistence = engine["persistence"]
        sqlite_path = persistence["sqlite_path"]
        # ensure it looks like a stringy path
        if not isinstance(sqlite_path, (str, os.PathLike)) or not str(sqlite_path).strip():
            raise KeyError("app.engine.persistence.sqlite_path must be a non-empty string")
    except KeyError as ke:
        raise RuntimeError(
            "CONFIG missing required key: app.engine.persistence.sqlite_path\n"
            "Your config must contain a single top-level 'app:' mapping with an "
            "'engine.persistence.sqlite_path' entry. See config/config.yaml template."
        ) from ke

    return cfg


# Eagerly load once for the app
CONFIG: Dict[str, Any] = load_config()


# ---------- Accessors ----------
def get_event() -> Dict[str, Any]:
    """Return event identity (name/date/location) or {}."""
    return (
        CONFIG.get("app", {})
              .get("engine", {})
              .get("event", {})
        or {}
    )


def get_db_path() -> Path:
    """Return absolute filesystem path to the SQLite database."""
    sqlite_path = (
        CONFIG.get("app", {})
              .get("engine", {})
              .get("persistence", {})
              .get("sqlite_path")
    )
    if not sqlite_path:
        # This should be unreachable because load_config already validated it.
        raise RuntimeError("CONFIG missing app.engine.persistence.sqlite_path")
    return _resolve_path(sqlite_path)


def get_scanner_cfg() -> Dict[str, Any]:
    """Return scanner configuration block (mock/serial/udp) or {}."""
    return CONFIG.get("scanner", {}) or {}


def get_publisher_cfg() -> Dict[str, Any]:
    """Return publisher configuration block or {} (mode/http/etc.)."""
    return CONFIG.get("publisher", {}) or {}


def get_log_level(default: str = "INFO") -> str:
    """
    Return scanner log level as 'INFO'/'DEBUG', etc.
    Server logging is controlled separately by Uvicorn / logging config.
    """
    lvl = (CONFIG.get("log", {}) or {}).get("level", default)
    # normalize common variants
    return str(lvl).upper()


def get_server_bind() -> Tuple[str, int]:
    """
    Return (host, port) for server convenience if you want to launch with code.
    We try to read from app.client.engine fixed_host when mode=fixed, else
    default to ('127.0.0.1', 8000). If you prefer explicit host/port, add:
        app: { engine: { server: { host: "...", port: 8000 } } }
    """
    # explicit host/port wins if provided
    server = (CONFIG.get("app", {})
                    .get("engine", {})
                    .get("server", {})) or {}
    host = server.get("host")
    port = server.get("port")
    if isinstance(host, str) and isinstance(port, int):
        return host, port

    # fallback via client.engine
    client_eng = (CONFIG.get("app", {})
                         .get("client", {})
                         .get("engine", {}) or {})
    mode = str(client_eng.get("mode", "localhost")).lower()
    if mode == "fixed":
        fixed = client_eng.get("fixed_host", "127.0.0.1:8000")
    elif mode == "localhost":
        fixed = "127.0.0.1:8000"
    else:
        # auto: most dev cases still fine as localhost default
        fixed = client_eng.get("fixed_host", "127.0.0.1:8000")

    try:
        h, p = fixed.split(":")
        return h, int(p)
    except Exception:
        return "127.0.0.1", 8000
# ---------- End of config_loader.py ----------