from __future__ import annotations
"""
backend/config_loader.py
========================

Unified configuration loader for ChronoCore (CCRS).

Project rule: **Forward-only.** The single source of truth is:
    config/config.yaml

This module loads that file, validates a few basics, and provides convenient
accessors so the rest of the backend doesn’t need to know about YAML details.

Design notes
------------
- No legacy fallbacks, no multi-file merging. If the file is missing or broken,
  we raise a friendly RuntimeError that prints absolute paths for quick fixes.
- Unknown keys in YAML are fine; we pass the whole dict through untouched.
- Helpers return {} or sensible defaults if a section/key is absent.
- Paths returned are absolute (resolved against repo root) unless the YAML
  already provided an absolute path.

Public API
----------
- CONFIG: dict              # eager-loaded result of load_config()
- load_config(path=None)    # (re)load config/config.yaml or a specified path
- get_event() → dict
- get_db_path() → pathlib.Path
- get_scanner_cfg() → dict
- get_publisher_cfg() → dict
- get_log_level(default="INFO") → str
- get_server_bind() → (host:str, port:int)
"""

from pathlib import Path
from typing import Any, Dict, Tuple
import os
import yaml


# ------------------------------------------------------------
# Locations
# ------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]          # repo root
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"    # single authoritative file


# ------------------------------------------------------------
# Loader
# ------------------------------------------------------------
def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        txt = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Config not found: {path}\n"
            f"Current working directory: {Path.cwd()}\n"
            f"Expected unified config at: {DEFAULT_CONFIG}"
        ) from e
    try:
        data = yaml.safe_load(txt) or {}
        if not isinstance(data, dict):
            raise TypeError("Top-level YAML is not a mapping/dict.")
        return data
    except Exception as e:
        raise RuntimeError(
            f"Failed to parse YAML: {path}\n"
            f"Error: {e}"
        ) from e


def load_config(path: str | os.PathLike[str] | None = None) -> Dict[str, Any]:
    """
    Load the unified config file. If `path` is None, use DEFAULT_CONFIG.
    Returns a dict (do not mutate in-place; copy if you need to modify).
    """
    p = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG
    cfg = _read_yaml(p)

    # Minimal structural validation (forward-only; no legacy support)
    # We keep this gentle: warn by raising with actionable messages if obviously wrong.
    # Required top-level sections for a normal install (soft requirement):
    #   - app, client, engine  (scanner/publisher/log are used by the scanner process)
    missing_core = [k for k in ("app", "client", "engine") if k not in cfg]
    if missing_core:
        # Not fatal for every tool (e.g., ilap_logger may only need scanner/publisher),
        # but it's usually a sign of a misconfigured file. Raise with details.
        raise RuntimeError(
            "Unified config is missing core sections: "
            + ", ".join(missing_core)
            + f"\nFile: {p}\nCWD: {Path.cwd()}"
        )

    return cfg


# Eagerly load once for convenience. Importers can use CONFIG directly.
CONFIG: Dict[str, Any] = load_config()


# ------------------------------------------------------------
# Accessors (lightweight, forward-only)
# ------------------------------------------------------------
def get_event() -> Dict[str, Any]:
    """
    Return the event block (engine.event). Empty dict if absent.
    """
    return (CONFIG.get("engine") or {}).get("event", {}) or {}


def get_db_path() -> Path:
    """
    Return absolute path to the SQLite DB from engine.persistence.db_path.
    If relative in YAML, resolve against repo root.
    """
    engine = CONFIG.get("engine") or {}
    persistence = engine.get("persistence") or {}
    db_path = persistence.get("db_path")
    if not db_path:
        # Keep the historically-common default path (forward-only projects also used this)
        return (ROOT / "backend" / "db" / "laps.sqlite").resolve()
    p = Path(db_path)
    return p if p.is_absolute() else (ROOT / p).resolve()


def get_scanner_cfg() -> Dict[str, Any]:
    """
    Return the scanner section (used by backend.ilap_logger). {} if absent.
    """
    return CONFIG.get("scanner") or {}


def get_publisher_cfg() -> Dict[str, Any]:
    """
    Return the publisher section (used by backend.ilap_logger). {} if absent.
    """
    return CONFIG.get("publisher") or {}


def get_log_level(default: str = "INFO") -> str:
    """
    Return scanner log level (log.level), upper-cased, defaulting to INFO.
    """
    lvl = ((CONFIG.get("log") or {}).get("level") or default)
    return str(lvl).upper()


def get_server_bind() -> Tuple[str, int]:
    """
    Return (host, port) from engine.server. Defaults (127.0.0.1, 8000).
    """
    srv = ((CONFIG.get("engine") or {}).get("server") or {})
    host = str(srv.get("host", "127.0.0.1"))
    port = int(srv.get("port", 8000))
    return host, port
