from __future__ import annotations
import pathlib

"""
ChronoCore RS - backend/server.py (drop-in)
------------------------------------------
This version keeps the original public surface but fixes the following:
1) /engine/entrant/assign_tag
   - Idempotent: assigning the same tag to the same entrant returns 200 OK.
   - Conflict checks only consider ENABLED entrants and exclude the incumbent.
   - On change, writes through to SQLite so DB and Engine stay in sync.

2) /engine/load
   - Rejects malformed payloads with clean 400s (no crashes, no int(None)).
   - Coerces entrant ids to int *before* any mapping.

3) Health probes
   - /healthz: liveness (no DB access).
   - /readyz: readiness (touches SQLite to confirm schema presence).

4) DB path
   - Sourced via config_loader.get_db_path(), which defaults to backend/db/laps.sqlite
     unless overridden in config/app.yaml.
"""

import asyncio
import datetime
import json
import logging
import time
import random
import sqlite3
import pathlib
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, cast
import yaml
from enum import Enum
from datetime import timezone
import datetime as dt

import aiosqlite
from fastapi import APIRouter, Body, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

## Our Race engine data, config, and db settings##
from .race_engine import ENGINE  
from .db_schema import ensure_schema, tag_conflicts, get_event_config
from .config_loader import get_db_path, get_scanner_cfg, CONFIG
from backend.qualifying import qual, qual_brake
from .app_results_api import router as app_results_router
from .osc_out import OscLightingOut
from .osc_in import OscInbound, OscInConfig


# log = logging.getLogger("uvicorn.error")
# log = logging.getLogger("race")
log = logging.getLogger("ccrs")
log.setLevel(logging.INFO)


# Resolve DB path from config and ensure schema on boot.
DB_PATH = get_db_path()
ensure_schema(DB_PATH, recreate=False, include_passes=True)

# --- Journaling config (YAML + env override) ---
import os

def _load_cfg() -> dict:
    p = pathlib.Path("config/config.yaml")
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}

_CFG = _load_cfg()

# Allow env var to override YAML; accepts "1"/"true"/"yes"
_env = os.getenv("CCRS_JOURNALING", "").strip().lower()
_env_val = _env in ("1", "true", "yes")

JOURNALING_ENABLED = _env_val if _env else bool(_CFG.get("journaling", {}).get("enabled", False))
JOURNALING_TABLE   = _CFG.get("journaling", {}).get("table", "passes_journal")

# ------------------------------------------------------------
# FastAPI app bootstrap
# ------------------------------------------------------------
app = FastAPI(title="CCRS Backend", version="0.2.1")

# Register auxiliary routers
app.include_router(qual)
app.include_router(qual_brake)
app.include_router(app_results_router)   # no prefix; paths mount exactly as declared

# ======================================================================
# Heats listing (schema-aware, no required params, stable JSON shape)
# Path: GET /heats
# Returns: {"heats": [ {heat_id, event_id?, name?, status?, started_utc?, finished_utc?, laps_count, entrant_count}, ... ]}
# ======================================================================
#from typing import Any, Dict, List
#import sqlite3
#import pathlib

@app.get("/heats", response_model=None)
def list_heats(limit: int = 100) -> Dict[str, Any]:
    db_abs = str(pathlib.Path(get_db_path()).resolve())
    with sqlite3.connect(db_abs) as db:
        db.row_factory = sqlite3.Row

        # Prefer the view; if it doesn't exist yet, return empty (keeps UI happy)
        try:
            rows = db.execute(
                """
                SELECT heat_id, event_id, name, started_ms, finished_ms, status, laps_count, entrant_count
                FROM v_heats_summary
                ORDER BY COALESCE(finished_ms, started_ms, heat_id) DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except sqlite3.OperationalError:
            return {"heats": []}

        def ms_to_iso(ms: Optional[int]) -> Optional[str]:
            if ms is None:
                return None
            return (
                dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "heat_id":        int(r["heat_id"]),
                "event_id":       int(r["event_id"]) if r["event_id"] is not None else None,
                "name":           r["name"],
                "status":         r["status"] or "",
                "started_utc":    ms_to_iso(r["started_ms"]),
                "finished_utc":   ms_to_iso(r["finished_ms"]),
                "laps_count":     int(r["laps_count"] or 0),
                "entrant_count":  int(r["entrant_count"] or 0),
            })
        return {"heats": out}

# ------------------------------------------------------------
# Simple scan bus state
# ------------------------------------------------------------
last_tag: dict[str, object] = {"tag": None, "seen_at": None}
_listeners: list[asyncio.Queue[str]] = []   # SSE subscribers

# ----------------------------------------------------------------------
# Scanner liveness - single source of truth for "decoder online"
# Online = we have heard a heartbeat within N seconds.
# ----------------------------------------------------------------------

_SCANNER_STATUS: Dict[str, Any] = {
    "last_heartbeat": 0.0,  # epoch seconds of the most recent heartbeat
    "meta": None,           # last metadata dict posted by scanner (port, baud, etc.)
}

# Consider the scanner online if we’ve heard from it within this window.
_HEARTBEAT_ONLINE_WINDOW_S: float = 5.0


def _mark_scanner_heartbeat(meta: Optional[Dict[str, Any]] = None) -> None:
    """
    Record a scanner heartbeat and (optionally) remember its metadata.
    Safe to call from /sensors/meta and also at the top of /sensors/inject.
    """
    _SCANNER_STATUS["last_heartbeat"] = time.time()
    if isinstance(meta, dict):
        _SCANNER_STATUS["meta"] = meta

def publish_tag(tag: str) -> float:
    """Push a tag into the bus and wake SSE listeners. Returns seen_at timestamp."""
    ts = time.time()
    last_tag["tag"] = str(tag)
    last_tag["seen_at"] = ts
    # Wake any waiting SSE clients
    for q in list(_listeners):
        try:
            q.put_nowait(str(tag))
        except Exception:
            pass
    return ts

# Single place to forward a pass into the engine.
def _send_pass_to_engine(tag: str, source: str = "SF") -> dict:
    tag = str(tag)
    if hasattr(ENGINE, "ingest_pass"):
        return ENGINE.ingest_pass(tag=tag, source=source)
    # last-resort no-op shape so callers don't explode
    return {"ok": True, "forwarded": False, "reason": "engine has no ingest method"}

# ------------------------------------------------------------
# Tag ingestion helper - For Mock/Test Scanners
# ------------------------------------------------------------
# Deprecated: direct callers should use ENGINE.ingest_pass(...)
# Removed _ingest_tag_to_engine (was unused).
""" async def _ingest_tag_to_engine(tag: str, source: str = "SF") -> dict:

    Look up entrant by tag and forward a pass to the engine.
    Returns a small dict describing what happened (for logging/testing).

    tag = _normalize_tag(tag)
    if not tag:
        return {"ok": False, "reason": "empty_tag"}

    # 1) Resolve tag -> entrant_id (enabled only)
    entrant_id: Optional[int] = None
    async with aiosqlite.connect(DB_PATH) as db:
        row = await _fetch_one(
            db,
            "SELECT entrant_id FROM entrants WHERE enabled=1 AND tag=?",
            (tag,),
        )
        if row:
            entrant_id = int(row[0])

    # 2) Unknown tag policy
    unknown_ok = bool(CONFIG.get("app", {})
                           .get("engine", {})
                           .get("unknown_tags", {})
                           .get("allow", False))

    if entrant_id is None and not unknown_ok:
        # Nothing to do; diagnostics will still show the scan
        return {"ok": True, "resolved": False, "reason": "unknown_tag_blocked"}

    # 3) Forward to the engine (best-effort; support a few method shapes)
    ts = time.time()
    try:
        if hasattr(ENGINE, "ingest_pass"):
            # Preferred: dict-shaped pass event
            ENGINE.ingest_pass({
                "entrant_id": entrant_id,
                "tag": tag,
                "source": source,
                "seen_at": ts,
            })
        elif hasattr(ENGINE, "pass_event"):
            # Alt shape used in some builds
            ENGINE.pass_event(entrant_id, ts, source, tag)
        elif hasattr(ENGINE, "on_tag"):
            # Very old builds that resolve tag inside the engine
            ENGINE.on_tag(tag=tag, seen_at=ts, source=source)
        else:
            return {"ok": False, "reason": "engine_no_ingest_api"}
    except Exception as ex:
        log.error("[ingest] engine forward failed: %s: %s", type(ex).__name__, ex)
        return {"ok": False, "reason": f"engine_error:{type(ex).__name__}"}

    return {"ok": True, "resolved": entrant_id is not None, "entrant_id": entrant_id} """


# ------------------------------------------------------------
# ---- Race Setup → Race Control session contracts -----------
# ------------------------------------------------------------

class EntrantSetup(BaseModel):
    """Minimal entrant shape required by /race/setup."""
    id: int                    # required; no Optional
    name: str
    number: Optional[str | int] = None
    tag: Optional[str] = None
    enabled: bool = True
    status: Optional[str] = None  # normalized to UPPER later

class SessionConfigIn(BaseModel):
    event_label: str
    session_label: str
    mode_id: str
    limit: Dict[str, Any]
    rank_method: str
    min_lap_s: float
    countdown: Dict[str, Any]
    announcements: Dict[str, Any]
    sounds: Dict[str, Any]
    bypass: Dict[str, Any]

class SetupReq(BaseModel):
    race_id: int
    entrants: List[EntrantSetup] = []
    session_config: SessionConfigIn

# IMPORTANT for Pydantic v2: resolve forward refs once models exist
#SetupReq.model_rebuild()

# -----------------------------------------------------------------------------
# Session handoff (Race Setup → Race Control)
# -----------------------------------------------------------------------------
_CURRENT_SESSION: Dict[str, Any] = {}         # last saved session_config from /race/setup
_CURRENT_RACE_ID: int | None = None
_CURRENT_ENTRANTS_ENGINE: List[Dict[str, Any]] = []  # last entrants mapped to ENGINE.load()
_LAST_ENGINE_LOAD: dict | None = None
_COUNTDOWN_TASK: asyncio.Task | None = None

# -----------------------------------------------------------------------------
# OSC Lighting Integration (QLC+)
# -----------------------------------------------------------------------------
_osc_out: Optional[OscLightingOut] = None
_osc_in: Optional[OscInbound] = None

def _get(d: Dict[str, Any], path: str, default=None):
    cur = d
    for part in path.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur

def _sound_url_from_config(key: str, default_file: str) -> str:
    """
    Build URL for sounds per your documented policy:
    /config/sounds/<file> preferred; static mount already falls back to /assets/sounds.
    """
    try:
        from .config_loader import CONFIG
        fname = _get(CONFIG, f"sounds.files.{key}", default_file) or default_file
    except Exception:
        fname = default_file
    return f"/config/sounds/{fname}"

def _derive_for_control(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize exactly what Race Control needs from the session_config we saved.
    Rules we agreed:
      - time-limited → white flag at 60s remaining
      - lap-limited → white flag 0 (Race Control will show 'last lap' UI only)
    """
    limit = cfg.get("limit", {}) or {}
    is_time = (limit.get("type") == "time")
    time_limit_s = int(limit.get("value_s") or 0) if is_time else 0
    soft_end     = bool(limit.get("soft_end", False)) if is_time else False

    cd = cfg.get("countdown", {}) or {}
    countdown_start_s = int(cd.get("start_from_s") or 0) if cd.get("start_enabled") else 0

    white_flag_sound_s = 60 if is_time else 0

    return {
        "mode_id": cfg.get("mode_id", "sprint"),
        "countdown_start_s": countdown_start_s,
        "white_flag_sound_s": white_flag_sound_s,
        "time_limit_s": time_limit_s,    # 0 means "no time limit" (lap-limited)
        "soft_end": soft_end,
        "min_lap_s": float(cfg.get("min_lap_s", 10)),
        "sound_urls": {
            "horn":  _sound_url_from_config("start",      "start_horn.wav"),
            "white": _sound_url_from_config("white_flag", "white_flag.wav"),
        }
    }

async def _auto_go_green(after_s: int) -> None:
    try:
        log.info(f"[COUNTDOWN] Sleeping {after_s}s before GREEN…")
        await asyncio.sleep(max(0, int(after_s)))
        _RACE_STATE["phase"]    = Phase.GREEN.value
        _RACE_STATE["flag"]     = "GREEN"
        _RACE_STATE["start_at"] = time.time()
        _engine_begin_green()  # tell engine we're green
        if hasattr(ENGINE, "set_flag"):
            try: ENGINE.set_flag("GREEN")
            except Exception: pass
        _send_flag_to_lighting("GREEN")  # Sync lighting on countdown green
        log.info(f"[COUNTDOWN] -> GREEN; start_at={_RACE_STATE['start_at']:.3f}")
    except asyncio.CancelledError:
        log.info("[COUNTDOWN] cancelled (end/abort)")
        return


# -----------------------------------------------------------------------------
# Race control in-memory state (scaffold)
# -----------------------------------------------------------------------------
class Phase(str, Enum):
    PRE = "pre"
    COUNTDOWN = "countdown"
    GREEN = "green"
    WHITE = "white"
    CHECKERED = "checkered"

# In-memory race state (kept in sync with UI + engine when possible)
_RACE_STATE = {
    "race_id": None,
    "phase": Phase.PRE.value,       # pre|countdown|green|white|checkered
    "flag": "PRE",                  # PRE|GREEN|YELLOW|RED|WHITE|CHECKERED
    "start_at": None,               # epoch seconds when GREEN actually began
    "countdown_from_s": 0,          # from session_config (0 = none)
    "limit": {"type": "time", "value_s": 0},  # default until /race/setup seeds it
    "created_at": time.time(),
}

def _now() -> float:
    return time.time()

def _elapsed_s() -> float:
    if _RACE_STATE["start_at"] is None:
        return 0.0
    return max(0.0, _now() - float(_RACE_STATE["start_at"]))

def _remaining_s() -> float:
    lim = _RACE_STATE["limit"]
    if not lim or lim.get("type") != "time":
        return float("inf")
    v = int(lim.get("value_s") or 0)
    if v <= 0:
        return float("inf")
    return v - _elapsed_s()

def _state_clock_block() -> dict:
    """
    Compute the clock payload for the UI.

    Rules:
      - COUNTDOWN: show T-minus via negative clock_ms and expose countdown_remaining_s.
      - GREEN:     show positive elapsed clock_ms since start_at.
      - CHECKERED: freeze the clock at the moment we ended (frozen_at - start_at).
      - Else:      clock_ms is None.
    """
    now      = time.time()
    phase    = _RACE_STATE.get("phase")
    start_at = _RACE_STATE.get("start_at")
    frozen   = _RACE_STATE.get("frozen_at")
    anchor   = _RACE_STATE.get("countdown_anchor_s")  # set when start_race enters COUNTDOWN

    clock_ms: int | None = None
    countdown_remaining_s: int | None = None

    if phase == Phase.COUNTDOWN.value and anchor:
        # Remaining whole seconds until auto-GREEN
        rem = int(round(anchor - now))
        countdown_remaining_s = max(0, rem)
        # Negative ms => UI renders as T-minus
        clock_ms = -countdown_remaining_s * 1000

    elif phase == Phase.GREEN.value and start_at:
        clock_ms = int((now - start_at) * 1000)

    elif phase == Phase.CHECKERED.value and start_at and frozen:
        # Freeze at exact elapsed when End was pressed
        clock_ms = int((frozen - start_at) * 1000)

    # Build the block your UI expects
    return {
        "start_at": start_at,
        "phase": phase,
        "clock_ms": clock_ms,
        "countdown_remaining_s": countdown_remaining_s,
    }

# --- Live "Seen" counters (pre-race diagnostics; in-memory only) ------------
# Populated at /race/setup from loaded entrants so we never hit the DB here.
_TAG_TO_ENTRANT: dict[str, dict] = {}   # tag -> entrant row (from setup payload)
_SEEN_COUNTS: dict[int, int] = {}       # entrant_id -> read count
_SEEN_TOTAL: int = 0                    # entrants with reads > 0


def _apply_session_min_lap(session_cfg: dict | None) -> None:
    """Push session-defined min lap time into the engine if provided."""
    if not isinstance(session_cfg, dict):
        return
    value = session_cfg.get("min_lap_s")
    if value is None:
        return
    if isinstance(value, (int, float)):
        ENGINE.min_lap_s = float(value)
        return
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return
        try:
            ENGINE.min_lap_s = float(raw)
        except ValueError:
            pass

# ---- Seen helper: bump count for a tag (single source of truth) ------------
def _note_seen(tag: str) -> None:
    """
    Increment _SEEN_COUNTS for the entrant mapped to this tag,
    only if the entrant exists and is enabled. Uses the live engine map.
    """
    global _SEEN_COUNTS

    eid = None
    try:
        # Prefer the live engine map
        eid = getattr(ENGINE, "tag_to_eid", {}).get(str(tag))
    except Exception:
        eid = None

    if not eid:
        return

    # Only count enabled entrants
    ent = getattr(ENGINE, "entrants", {}).get(eid)
    if ent and getattr(ent, "enabled", True):
        _SEEN_COUNTS[eid] = 1 + _SEEN_COUNTS.get(eid, 0)



def _state_seen_block() -> dict:
    """
    Build a UI-friendly 'seen' block:
      rows: [{entrant_id, tag, number, name, enabled, reads}]
      count/total: numbers for the (seen/total) badge
    Uses entrants already cached in _RACE_STATE by /race/setup.
    """
    rows: list[dict] = []
    try:
        entrants = _RACE_STATE.get("entrants") or []
        for e in entrants:
            eid = int(e.get("entrant_id"))
            rows.append({
                "entrant_id": eid,
                "tag": e.get("tag"),
                "number": e.get("number"),
                "name": e.get("name"),
                "enabled": bool(e.get("enabled", True)),
                "reads": int(_SEEN_COUNTS.get(eid, 0)),
                "grid_index": e.get("grid_index"),
                "brake_valid": e.get("brake_valid"),
            })
        # Sort: enabled first, reads desc, then car number for stable ties
        rows.sort(key=lambda r: (
            0 if r["enabled"] else 1,
            -r["reads"],
            str(r.get("number") or "")
        ))
    except Exception:
        pass
    return {
        "count": sum(1 for r in rows if r["reads"] > 0),
        "total": len(rows),
        "rows": rows,
    }


def _reseed_seen_roster(entrants: Iterable[dict]) -> None:
    """Reset seen counters + tag map from the provided entrant list."""
    global _TAG_TO_ENTRANT, _SEEN_COUNTS, _SEEN_TOTAL

    entrants_list = list(entrants or [])
    _TAG_TO_ENTRANT.clear()
    for item in entrants_list:
        try:
            tag = str(item.get("tag") or "").strip()
        except AttributeError:
            continue
        if tag:
            _TAG_TO_ENTRANT[tag] = item

    _SEEN_COUNTS.clear()
    _SEEN_TOTAL = 0


def _reload_engine_from_cached_session() -> dict | None:
    """Best-effort engine reload using the most recent session cache."""
    global _CURRENT_RACE_ID, _CURRENT_SESSION, _CURRENT_ENTRANTS_ENGINE, _RACE_STATE
    if _CURRENT_RACE_ID is None:
        return None

    race_type = (_CURRENT_SESSION.get("mode_id", "sprint") if _CURRENT_SESSION else "sprint")
    entrants = _CURRENT_ENTRANTS_ENGINE or []

    try:
        snap = ENGINE.load(
            race_id=int(_CURRENT_RACE_ID or 0),
            entrants=entrants,
            race_type=str(race_type),
            session_config=_CURRENT_SESSION,
        )
    except Exception:
        return None

    # Mirror roster/config back into the UI state so summaries stay accurate
    entrants_copy = [dict(e) for e in entrants]
    _CURRENT_ENTRANTS_ENGINE = entrants_copy[:]
    _RACE_STATE["entrants"] = entrants_copy

    if _CURRENT_SESSION:
        _RACE_STATE["limit"] = _CURRENT_SESSION.get("limit") or _RACE_STATE.get("limit")
        cd = (_CURRENT_SESSION.get("countdown") or {})
        _RACE_STATE["countdown_from_s"] = (
            int(cd.get("start_from_s") or 0) if cd.get("start_enabled") else 0
        )
        if "min_lap_s" in _CURRENT_SESSION:
            _RACE_STATE["min_lap_s"] = _CURRENT_SESSION.get("min_lap_s")

    _apply_session_min_lap(_CURRENT_SESSION)
    _reseed_seen_roster(entrants_copy)
    return snap


# ------------------------------------------------------------
# CORS: permissive for development. Tighten for production.
#------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Try <repo>/ui first; fall back to <repo>/backend/static
PROJECT_ROOT = Path(__file__).resolve().parents[1]  # <repo> when server.py is in backend/
UI_DIR = PROJECT_ROOT / "ui"
MODES_PATH = (PROJECT_ROOT / "config" / "race_modes.yaml").resolve()
#STATIC_DIR = UI_DIR if UI_DIR.exists() else Path(__file__).resolve().parent / "static"
#app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

# ---- Sound assets: user overrides then built-in defaults ----
SOUNDS_ASSETS_DIR = PROJECT_ROOT / "assets" / "sounds"
SOUNDS_CONFIG_DIR = PROJECT_ROOT / "config" / "sounds"
SOUNDS_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
SOUNDS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/assets/sounds", StaticFiles(directory=SOUNDS_ASSETS_DIR, html=False), name="assets_sounds")
app.mount("/config/sounds", StaticFiles(directory=SOUNDS_CONFIG_DIR, html=False), name="config_sounds")

STATIC_DIR = Path(__file__).resolve().parent.parent / "ui"   # => project_root/ui
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

log.info("Serving UI from: %s", STATIC_DIR)  # sanity print at startup





# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def _engine_running() -> bool | None:
    for name in ("is_running", "running", "is_green"):
        attr = getattr(ENGINE, name, None)
        try:
            val = attr() if callable(attr) else attr
            if isinstance(val, bool):
                return val
        except Exception:
            pass
    return None

def _grid_map_for_event(conn: sqlite3.Connection, event_id: int) -> Dict[int, int]:
    """Return a map of entrant_id -> order from the event's frozen qualifying grid.

    Reads event config JSON via get_event_config and extracts
    qualifying.grid[].order. Missing data returns an empty dict.
    """
    cfg = get_event_config(conn, event_id) or {}
    grid = (cfg.get("qualifying") or {}).get("grid") or []
    return {int(item["entrant_id"]): int(item["order"]) for item in grid if "entrant_id" in item and "order" in item}

def _brake_map_for_event(conn: sqlite3.Connection, event_id: int) -> dict[int, bool]:
    """
    Read event config and return entrant_id -> brake_ok boolean.
    Expected shape in events.config_json:
      {
        "qualifying": {
          "grid": [
            {"entrant_id": 101, "order": 1, "brake_ok": true},
            ...
          ]
        }
      }
    Missing keys default to True (i.e., treat as passed).
    """
    cfg = get_event_config(conn, event_id) or {}
    grid = (cfg.get("qualifying") or {}).get("grid") or []
    out: dict[int, bool] = {}
    for item in grid:
        try:
            eid = int(item["entrant_id"])
        except Exception:
            continue
        out[eid] = bool(item.get("brake_ok", True))
    return out


def build_standings_payload(
    conn: sqlite3.Connection,
    event_id: int,
    phase: str | None,
    engine_snapshot: dict | None = None,
) -> list[dict]:
    """
    Build the UI 'standings' array. Behavior:
      • PRE/COUNTDOWN → order by frozen grid (qualifying).
      • GREEN/WHITE/CHECKERED → use engine’s live values; if engine
        doesn’t provide an ordered list, derive a reasonable order.

    Each row has:
      { entrant_id, number, name, laps, last, pace, best,
        enabled, grid_index, brake_valid }
    """
    def _to_seconds(raw: object) -> float | None:
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            val = float(raw)
        elif isinstance(raw, str):
            try:
                val = float(raw.strip())
            except ValueError:
                return None
        else:
            return None
        # Heuristic: treat large values as milliseconds.
        return val / 1000.0 if val > 600.0 else val

    # Cache the live engine snapshot (if provided) so we reuse the computed laps/pace data.
    snapshot_rows: dict[int, dict] = {}
    if isinstance(engine_snapshot, dict):
        for item in engine_snapshot.get("standings", []) or []:
            val = item.get("entrant_id") if isinstance(item, dict) else None
            if val is None:
                continue
            try:
                sid = int(val)
            except (TypeError, ValueError):
                continue
            snapshot_rows[sid] = item

    # 1) Snapshot the engine entrants as our baseline “roster”
    ents = getattr(ENGINE, "entrants", {}) or {}
    rows: list[dict] = []
    for eid, ent in ents.items():
        try:
            eid_i = int(getattr(ent, "entrant_id", eid))
        except Exception:
            continue

        snap_row = snapshot_rows.pop(eid_i, None)

        number = getattr(ent, "number", None)
        name = getattr(ent, "name", None)
        enabled = bool(getattr(ent, "enabled", True))

        if snap_row:
            laps = int(snap_row.get("laps") or snap_row.get("total_laps") or 0)
            last = _to_seconds(snap_row.get("last") or snap_row.get("last_s") or snap_row.get("last_ms"))
            pace = _to_seconds(
                snap_row.get("pace_5")
                or snap_row.get("pace")
                or snap_row.get("pace_s")
                or snap_row.get("pace_ms")
            )
            best = _to_seconds(snap_row.get("best") or snap_row.get("best_s") or snap_row.get("best_ms"))
            lap_deficit = snap_row.get("lap_deficit")
        else:
            laps = int(getattr(ent, "laps", getattr(ent, "total_laps", 0)) or 0)
            last = _to_seconds(
                getattr(ent, "last_s", None)
                or getattr(ent, "last_ms", None)
                or getattr(ent, "last", None)
            )
            best = _to_seconds(
                getattr(ent, "best_s", None)
                or getattr(ent, "best_ms", None)
                or getattr(ent, "best", None)
            )
            pace: float | None = None
            buf = getattr(ent, "pace_buf", None)
            if buf:
                try:
                    pace = _to_seconds(sum(buf[-5:]) / len(buf[-5:]))
                except ZeroDivisionError:
                    pace = None
            lap_deficit = None

        rows.append({
            "entrant_id": eid_i,
            "number": (str(number) if number is not None else None),
            "name": (str(name) if name is not None else f"Entrant {eid_i}"),
            "laps": laps,
            "lap_deficit": lap_deficit,
            "last": last,
            "pace": pace,
            "best": best,
            "enabled": enabled,
            # filled below
            "grid_index": None,
            "brake_valid": True,
        })

    # Include any remaining snapshot-only entrants (e.g., provisionals created mid-race).
    for snap_row in snapshot_rows.values():
        if not isinstance(snap_row, dict):
            continue
        value = snap_row.get("entrant_id")
        if value is None:
            continue
        try:
            eid = int(value)
        except (TypeError, ValueError):
            continue
        rows.append({
            "entrant_id": eid,
            "number": (str(snap_row.get("number")) if snap_row.get("number") is not None else None),
            "name": (str(snap_row.get("name")) if snap_row.get("name") else f"Entrant {eid}"),
            "laps": int(snap_row.get("laps") or snap_row.get("total_laps") or 0),
            "lap_deficit": snap_row.get("lap_deficit"),
            "last": _to_seconds(snap_row.get("last") or snap_row.get("last_s") or snap_row.get("last_ms")),
            "pace": _to_seconds(
                snap_row.get("pace_5")
                or snap_row.get("pace")
                or snap_row.get("pace_s")
                or snap_row.get("pace_ms")
            ),
            "best": _to_seconds(snap_row.get("best") or snap_row.get("best_s") or snap_row.get("best_ms")),
            "enabled": bool(snap_row.get("enabled", True)),
            "grid_index": None,
            "brake_valid": True,
        })

    # 2) Grid + brake maps from event config
    grid_map  = _grid_map_for_event(conn, event_id) or {}
    brake_map = _brake_map_for_event(conn, event_id) or {}

    for r in rows:
        eid = r["entrant_id"]
        r["grid_index"] = grid_map.get(eid)
        r["brake_valid"] = brake_map.get(eid, True)

    # Normalize lap deficit after we have the full roster.
    leader_laps = max((int(r.get("laps") or 0) for r in rows), default=0)
    for r in rows:
        deficit_raw = r.get("lap_deficit")
        if deficit_raw is None:
            r["lap_deficit"] = max(0, leader_laps - int(r.get("laps") or 0))
        else:
            try:
                r["lap_deficit"] = max(0, int(deficit_raw))
            except Exception:
                r["lap_deficit"] = max(0, leader_laps - int(r.get("laps") or 0))

    # 3) Ordering policy
    phase_lower = (phase or "").lower()
    if phase_lower in ("pre", "countdown"):
        # Sort strictly by frozen grid. Unknowns go last, stable by number/name.
        rows.sort(key=lambda r: (r["grid_index"] is None, r["grid_index"] or 10**9,
                                 str(r.get("number") or ""), str(r.get("name") or "")))
    else:
        # Racing/finished: prefer laps desc, then pace/best (smallest wins), then grid as gentle tiebreaker
        def _sec(x):
            return _to_seconds(x)

        rows.sort(key=lambda r: (
            -int(r.get("laps") or 0),
            (_sec(r.get("pace")) if _sec(r.get("pace")) is not None else 9e9),
            (_sec(r.get("best")) if _sec(r.get("best")) is not None else 9e9),
            (r["grid_index"] if r["grid_index"] is not None else 10**9),
        ))

        # Stamp 1-based position for convenience (UI still free to compute)
        for i, r in enumerate(rows, start=1):
            r["position"] = i

    return rows


def get_db() -> sqlite3.Connection:
    """Create a SQLite connection with dict-style row access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ms_to_str(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    total_ms = int(ms)
    minutes, rem = divmod(total_ms, 60_000)
    return f"{minutes:02d}:{rem / 1000:06.3f}"


def to_iso_utc(ts_ms: Optional[int]) -> Optional[str]:
    if ts_ms is None:
        return None
    # Refer to the module explicitly (dt.datetime, dt.timezone.utc)
    return dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


JOURNALING_ENABLED = bool(
    ((CONFIG.get("app") or {})
     .get("engine", {})
     .get("persistence", {})
     .get("journal_passes", False))
)


results_router = APIRouter(prefix="/results", tags=["results"])
export_router = APIRouter(prefix="/export", tags=["results-export"])


def _table_info(cx: sqlite3.Connection, table: str) -> Dict[str, bool]:
    """Return a mapping of column names present in a table. Tolerates missing tables."""
    cols: Dict[str, bool] = {}
    try:
        for _cid, name, _coltype, _notnull, _dflt, _pk in cx.execute(f"PRAGMA table_info({table})"):
            cols[str(name)] = True
    except sqlite3.OperationalError:
        pass
    return cols


def _choose_table(cx: sqlite3.Connection) -> str:
    """Prefer 'heats', fallback to 'races'. Raise if neither exists."""
    if _table_info(cx, "heats"):
        return "heats"
    if _table_info(cx, "races"):
        return "races"
    raise RuntimeError("Neither 'heats' nor 'races' table exists.")

# Heats listing (GET /heats); schema-aware and returns a stable {"heats": [...]} payload

@results_router.get("/heats", response_model=None)
def list_heats_old(limit: int = 100) -> Dict[str, Any]:
    """
    Works with either 'heats' or 'races' table.
    Only selects columns that actually exist (avoids OperationalError).
    Returns a dict with "heats": [] so the UI can read a stable shape.
    """

    def _abs_db() -> str:
        # Always resolve to an absolute path to avoid Path/WD issues
        return str(Path(get_db_path()).resolve())

    def _table_exists(db: sqlite3.Connection, name: str) -> bool:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _columns(db: sqlite3.Connection, name: str) -> Dict[str, bool]:
        cols: Dict[str, bool] = {}
        try:
            for _cid, cname, _ctype, _notnull, _dflt, _pk in db.execute(f"PRAGMA table_info({name})"):
                cols[cname] = True
        except sqlite3.OperationalError:
            pass
        return cols

    db_path = _abs_db()
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row

        # Prefer 'heats' if present; else 'races'; else return empty list.
        if _table_exists(db, "heats"):
            table = "heats"
        elif _table_exists(db, "races"):
            table = "races"
        else:
            return {"heats": []}

        cols = _columns(db, table)

        # Identify primary key column and alias to heat_id for UI
        id_col = (
            "heat_id" if "heat_id" in cols else
            ("race_id" if "race_id" in cols else ("id" if "id" in cols else None))
        )
        if not id_col:
            # Table is unusable for listing — return empty instead of 500
            return {"heats": []}

        # Optional columns
        event_col = "event_id" if "event_id" in cols else None

        # Build SELECT list with safe aliases
        fields: List[str] = [f"h.{id_col} AS heat_id"]
        if event_col:
            fields.append(f"h.{event_col} AS event_id")
        if "name" in cols:
            fields.append("h.name")
        # Prefer 'status' if exists; otherwise synthesize an empty string for shape stability
        if "status" in cols:
            fields.append("h.status")
        else:
            fields.append("'' AS status")

        # Time columns: alias common variants to started_utc/finished_utc
        if "started_utc" in cols:
            fields.append("h.started_utc")
        elif "started_at" in cols:
            fields.append("h.started_at AS started_utc")

        if "finished_utc" in cols:
            fields.append("h.finished_utc")
        elif "ended_utc" in cols:
            fields.append("h.ended_utc AS finished_utc")

        select_list = ", ".join(fields)

        # Order newest first using finished/start time when available; else by id
        if any(c in cols for c in ("finished_utc", "started_utc", "ended_utc", "started_at")):
            order_expr = "COALESCE(h.finished_utc, h.started_utc, h.ended_utc, h.started_at) DESC"
        else:
            order_expr = f"h.{id_col} DESC"

        rows = db.execute(
            f"""
            SELECT {select_list}
            FROM {table} h
            ORDER BY {order_expr}
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

        # Aggregate counts if a lap_events table exists (optional)
        aggregates: Dict[int, sqlite3.Row] = {}
        try:
            if _table_exists(db, "lap_events"):
                agg_rows = db.execute(
                    """
                    SELECT
                      heat_id,
                      COUNT(*) AS laps_count,
                      COUNT(DISTINCT entrant_id) AS entrant_count
                    FROM lap_events
                    GROUP BY heat_id
                    """
                ).fetchall()
                aggregates = {int(r["heat_id"]): r for r in agg_rows if r["heat_id"] is not None}
        except sqlite3.OperationalError:
            # If lap_events doesn't exist or has a different schema, just skip aggregates
            aggregates = {}

        out: List[Dict[str, Any]] = []
        for r in rows:
            keys = set(r.keys())
            hid = int(r["heat_id"]) if "heat_id" in keys and r["heat_id"] is not None else None
            agg = aggregates.get(hid) if hid is not None else None
            out.append({
                "heat_id": hid,
                "event_id": (int(r["event_id"]) if "event_id" in keys and r["event_id"] is not None else None),
                "name": r["name"] if "name" in keys else None,
                "status": r["status"] if "status" in keys else "",
                "started_utc": r["started_utc"] if "started_utc" in keys else None,
                "finished_utc": r["finished_utc"] if "finished_utc" in keys else None,
                "laps_count": int(agg["laps_count"]) if agg else 0,
                "entrant_count": int(agg["entrant_count"]) if agg else 0,
            })

        return {"heats": out}



def fetch_laps(db: sqlite3.Connection, heat_id: int) -> List[sqlite3.Row]:
    return db.execute(
        """
        SELECT
          le.heat_id,
          le.entrant_id,
          le.lap_num,
          le.ts_ms,
          le.inferred,
          le.source_id,
          le.meta_json,
          e.number,
          e.name,
          e.tag,
          e.enabled,
          e.status
        FROM lap_events le
        JOIN entrants e ON e.entrant_id = le.entrant_id
        WHERE le.heat_id = ?
        ORDER BY le.entrant_id ASC, le.ts_ms ASC
        """,
        (heat_id,),
    ).fetchall()


def fetch_flags(db: sqlite3.Connection, heat_id: int) -> List[sqlite3.Row]:
    try:
        return db.execute(
            """
            SELECT state, ts_ms
            FROM flags
            WHERE heat_id = ?
            ORDER BY ts_ms ASC
            """,
            (heat_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def latest_flag_at(flags: List[sqlite3.Row], ts_ms: int) -> Optional[str]:
    state = None
    for flag in flags:
        if flag["ts_ms"] <= ts_ms:
            state = flag["state"]
        else:
            break
    return state


def grid_index_map(db: sqlite3.Connection, event_id: int) -> Dict[int, int]:
    try:
        return _grid_map_for_event(db, event_id)
    except Exception:
        return {}


def compute_standings(db: sqlite3.Connection, heat_id: int) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    heat = db.execute(
        "SELECT heat_id, event_id, name, status, started_utc, finished_utc FROM heats WHERE heat_id = ?",
        (heat_id,),
    ).fetchone()
    if not heat:
        raise HTTPException(status_code=404, detail="Heat not found")

    flags = fetch_flags(db, heat_id)
    laps = fetch_laps(db, heat_id)
    if not laps:
        return ({"duration_ms": 0, "fastest_ms": None, "cars_classified": 0}, [])

    roster: Dict[int, Dict[str, Any]] = {}
    for row in laps:
        bucket = roster.setdefault(
            int(row["entrant_id"]),
            {
                "entrant_id": int(row["entrant_id"]),
                "number": row["number"],
                "name": row["name"],
                "enabled": bool(row["enabled"]),
                "status": row["status"],
                "ts": [],
            },
        )
        bucket["ts"].append(int(row["ts_ms"]))

    fastest_ms: Optional[int] = None
    standings: List[Dict[str, Any]] = []
    for entry in roster.values():
        timestamps = entry["ts"]
        if len(timestamps) < 2:
            laps_done = max(0, len(timestamps) - 1)
            standings.append(
                {
                    "entrant_id": entry["entrant_id"],
                    "number": entry["number"],
                    "name": entry["name"],
                    "status": entry["status"],
                    "enabled": entry["enabled"],
                    "laps": laps_done,
                    "last_ms": None,
                    "best_ms": None,
                    "pace_5_ms": None,
                    "grid_index": None,
                    "brake_valid": True,
                    "pit_count": 0,
                }
            )
            continue

        deltas = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
        last_ms = deltas[-1]
        best_ms = min(deltas)
        pace_5_ms = sum(deltas[-5:]) // min(5, len(deltas))
        fastest_ms = best_ms if fastest_ms is None else min(fastest_ms, best_ms)

        standings.append(
            {
                "entrant_id": entry["entrant_id"],
                "number": entry["number"],
                "name": entry["name"],
                "status": entry["status"],
                "enabled": entry["enabled"],
                "laps": len(deltas),
                "last_ms": last_ms,
                "best_ms": best_ms,
                "pace_5_ms": pace_5_ms,
                "grid_index": None,
                "brake_valid": True,
                "pit_count": 0,
            }
        )

    leader_laps = max(row["laps"] for row in standings) if standings else 0
    for row in standings:
        row["lap_deficit"] = leader_laps - row["laps"]

    grid_map = grid_index_map(db, int(heat["event_id"]))
    if grid_map:
        for row in standings:
            row["grid_index"] = grid_map.get(row["entrant_id"])

    def sort_key(row: Dict[str, Any]) -> Tuple[int, int, int, int]:
        laps = int(row.get("laps") or 0)
        pace_candidate = row.get("pace_5_ms")
        if pace_candidate is None:
            pace_candidate = row.get("best_ms")
        pace_val = int(pace_candidate) if pace_candidate is not None else 1_000_000_000
        grid_candidate = row.get("grid_index")
        grid_val = int(grid_candidate) if grid_candidate is not None else 1_000_000
        entrant_id = int(row.get("entrant_id", 0))
        return (-laps, pace_val, grid_val, entrant_id)

    standings.sort(key=sort_key)
    for idx, row in enumerate(standings, start=1):
        row["position"] = idx

    all_ts = [int(row["ts_ms"]) for row in laps]
    totals = {
        "duration_ms": (max(all_ts) - min(all_ts)) if all_ts else 0,
        "fastest_ms": fastest_ms,
        "cars_classified": len(standings),
    }
    return totals, standings


def per_lap_audit(db: sqlite3.Connection, heat_id: int) -> List[Dict[str, Any]]:
    flags = fetch_flags(db, heat_id)
    laps = fetch_laps(db, heat_id)

    last_seen: Dict[int, Optional[int]] = {}
    cumulative: Dict[int, int] = {}
    audit: List[Dict[str, Any]] = []

    for row in laps:
        entrant_id = int(row["entrant_id"])
        ts_ms = int(row["ts_ms"])
        previous = last_seen.get(entrant_id)
        lap_ms = ts_ms - previous if previous is not None else None
        last_seen[entrant_id] = ts_ms
        if lap_ms is not None:
            cumulative[entrant_id] = cumulative.get(entrant_id, 0) + lap_ms

        location_id: Optional[str] = None
        location_label: Optional[str] = None
        if row["meta_json"]:
            try:
                meta = json.loads(row["meta_json"])
                location = meta.get("location") if isinstance(meta, dict) else None
                if isinstance(location, dict):
                    location_id = location.get("id")
                    location_label = location.get("label")
            except Exception:
                pass

        audit.append(
            {
                "entrant_id": entrant_id,
                "number": row["number"],
                "name": row["name"],
                "tag": row["tag"],
                "lap_num": row["lap_num"],
                "lap_ms": lap_ms,
                "cumulative_ms": cumulative.get(entrant_id),
                "ts_ms": ts_ms,
                "ts_utc": to_iso_utc(ts_ms),
                "flag": latest_flag_at(flags, ts_ms) if flags else None,
                "inferred": row["inferred"] or 0,
                "source_id": row["source_id"],
                "location_id": location_id,
                "location_label": location_label,
            }
        )

    return audit


@results_router.get("/{heat_id}/summary")
def heat_summary(heat_id: int) -> Dict[str, Any]:
    with get_db() as db:
        heat = db.execute("SELECT * FROM heats WHERE heat_id = ?", (heat_id,)).fetchone()
        if not heat:
            raise HTTPException(status_code=404, detail="Heat not found")

        totals, standings = compute_standings(db, heat_id)

        policy = None
        try:
            cfg = get_event_config(db, int(heat["event_id"])) or {}
            policy = (cfg.get("qualifying") or {}).get("grid_policy")
        except Exception:
            policy = None

        return {
            "frozen": heat["status"] == "CHECKERED",
            "policy": policy,
            "standings": standings,
            "totals": totals,
        }


@results_router.get("/{heat_id}/laps")
def heat_laps(heat_id: int) -> List[Dict[str, Any]]:
    with get_db() as db:
        exists = db.execute("SELECT 1 FROM heats WHERE heat_id = ?", (heat_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Heat not found")
        return per_lap_audit(db, heat_id)


def csv_stream(rows: Iterable[Iterable[Any]]) -> Iterable[bytes]:
    for row in rows:
        cells: List[str] = []
        for value in row:
            if isinstance(value, str):
                cells.append('"' + value.replace('"', '""') + '"')
            elif value is None:
                cells.append("")
            else:
                cells.append(str(value))
        yield (",".join(cells) + "\r\n").encode("utf-8")


@export_router.get("/standings.json")
def export_standings_json(heat_id: int):
    with get_db() as db:
        heat = db.execute("SELECT * FROM heats WHERE heat_id = ?", (heat_id,)).fetchone()
        if not heat:
            raise HTTPException(status_code=404, detail="Heat not found")
        totals, standings = compute_standings(db, heat_id)
        return JSONResponse(
            {
                "event_id": heat["event_id"],
                "heat_id": heat_id,
                "heat_name": heat["name"],
                "frozen": heat["status"] == "CHECKERED",
                "standings": standings,
                "totals": totals,
            }
        )


@export_router.get("/standings.csv")
def export_standings_csv(heat_id: int):
    with get_db() as db:
        heat = db.execute("SELECT * FROM heats WHERE heat_id = ?", (heat_id,)).fetchone()
        if not heat:
            raise HTTPException(status_code=404, detail="Heat not found")
        totals, standings = compute_standings(db, heat_id)
        audit = per_lap_audit(db, heat_id)

        cumulative_by_entrant: Dict[int, int] = {}
        for row in audit:
            if row.get("cumulative_ms") is not None:
                cumulative_by_entrant[int(row["entrant_id"])] = int(row["cumulative_ms"])

        header = [
            "event_id",
            "heat_id",
            "heat_name",
            "entrant_id",
            "number",
            "name",
            "status",
            "enabled",
            "grid_index",
            "brake_valid",
            "position",
            "laps",
            "lap_deficit",
            "total_ms",
            "total_str",
            "best_ms",
            "best_str",
            "pace5_ms",
            "pace5_str",
        ]

        def gen():
            if heat["status"] != "CHECKERED":
                yield from csv_stream([["# provisional"]])
            yield from csv_stream([header])

            for row in standings:
                total_ms = cumulative_by_entrant.get(int(row["entrant_id"]))
                yield from csv_stream(
                    [
                        [
                            heat["event_id"],
                            heat_id,
                            heat["name"],
                            row.get("entrant_id"),
                            row.get("number"),
                            row.get("name"),
                            row.get("status"),
                            int(bool(row.get("enabled", True))),
                            row.get("grid_index"),
                            1 if row.get("brake_valid") else 0,
                            row.get("position"),
                            row.get("laps"),
                            row.get("lap_deficit"),
                            total_ms if total_ms is not None else "",
                            ms_to_str(total_ms) if total_ms is not None else "",
                            row.get("best_ms"),
                            ms_to_str(row.get("best_ms")),
                            row.get("pace_5_ms"),
                            ms_to_str(row.get("pace_5_ms")),
                        ]
                    ]
                )

        return StreamingResponse(
            gen(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="standings_{heat_id}.csv"'},
        )


@export_router.get("/laps.json")
def export_laps_json(heat_id: int):
    with get_db() as db:
        heat = db.execute("SELECT * FROM heats WHERE heat_id = ?", (heat_id,)).fetchone()
        if not heat:
            raise HTTPException(status_code=404, detail="Heat not found")
        rows = per_lap_audit(db, heat_id)
        return JSONResponse(
            {
                "event_id": heat["event_id"],
                "heat_id": heat_id,
                "heat_name": heat["name"],
                "frozen": heat["status"] == "CHECKERED",
                "laps": rows,
            }
        )


@export_router.get("/laps.csv")
def export_laps_csv(heat_id: int):
    with get_db() as db:
        heat = db.execute("SELECT * FROM heats WHERE heat_id = ?", (heat_id,)).fetchone()
        if not heat:
            raise HTTPException(status_code=404, detail="Heat not found")
        rows = per_lap_audit(db, heat_id)

        header = [
            "event_id",
            "heat_id",
            "heat_name",
            "entrant_id",
            "number",
            "name",
            "tag",
            "lap_num",
            "lap_ms",
            "lap_str",
            "cumulative_ms",
            "cumulative_str",
            "ts_ms",
            "ts_utc",
            "flag",
            "source_id",
            "location_id",
            "location_label",
            "inferred",
        ]

        def gen():
            yield from csv_stream([header])
            for row in rows:
                ts_ms = row.get("ts_ms")
                lap_ms = row.get("lap_ms")
                cumulative_ms = row.get("cumulative_ms")
                yield from csv_stream(
                    [
                        [
                            heat["event_id"],
                            heat_id,
                            heat["name"],
                            row.get("entrant_id"),
                            row.get("number"),
                            row.get("name"),
                            row.get("tag") or "",
                            row.get("lap_num"),
                            lap_ms if lap_ms is not None else "",
                            ms_to_str(lap_ms) if lap_ms is not None else "",
                            cumulative_ms if cumulative_ms is not None else "",
                            ms_to_str(cumulative_ms) if cumulative_ms is not None else "",
                            f"'{ts_ms}" if ts_ms is not None else "",
                            row.get("ts_utc") or "",
                            row.get("flag") or "",
                            row.get("source_id") if row.get("source_id") is not None else "",
                            row.get("location_id") or "",
                            row.get("location_label") or "",
                            row.get("inferred") or 0,
                        ]
                    ]
                )

        return StreamingResponse(
            gen(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="laps_{heat_id}.csv"'},
        )


@export_router.get("/passes.csv")
def export_passes_csv(heat_id: int):
    if not JOURNALING_ENABLED:
        raise HTTPException(status_code=403, detail="Pass journaling is disabled")

    with get_db() as db:
        def table_exists(name: str) -> bool:
            return db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone() is not None

        table_name = JOURNALING_TABLE if table_exists(JOURNALING_TABLE) else (
            "passes" if table_exists("passes") else None
        )
        if not table_name:
            raise HTTPException(status_code=404, detail="Pass journal table not found")

        columns = [row["name"] for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()]
        if not columns:
            raise HTTPException(status_code=404, detail="Unable to read journal columns")

        order_column = "ts_ms" if "ts_ms" in columns else columns[0]

        if "heat_id" in columns:
            rows = db.execute(
                f"SELECT * FROM {table_name} WHERE heat_id = ? ORDER BY {order_column} ASC",
                (heat_id,),
            ).fetchall()
        else:
            rows = db.execute(
                f"SELECT * FROM {table_name} ORDER BY {order_column} ASC",
            ).fetchall()

        def gen():
            yield from csv_stream([columns])
            for row in rows:
                yield from csv_stream([[row[col] for col in columns]])

        return StreamingResponse(
            gen(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="passes_{heat_id}.csv"'},
        )



app.include_router(export_router)


async def _fetch_one(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
    cur = await db.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row

async def _exec(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> None:
    await db.execute(sql, params)
    await db.commit()

def _normalize_tag(raw: Any) -> Optional[str]:
    """
    Normalize incoming tag payload:
      - None or whitespace-only -> None (clears the tag)
      - Else -> stripped string
    """
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None

# Resolve an entrant_id to its tag from the live engine roster (if present)
def _tag_for_entrant_id(eid: int | None) -> str | None:
    try:
        if eid is None:
            return None
        # RaceEngine exposes .entrants dict of Entrant objects
        ent = getattr(ENGINE, "entrants", {}).get(int(eid))
        tag = getattr(ent, "tag", None) if ent else None
        if tag:
            return str(tag).strip()
    except Exception:
        pass
    return None


# Resolve a tag to an entrant's name/number from the live engine roster (if present)
def _entrant_for_tag(tag: str) -> Optional[dict]:
    """
    Best-effort resolve of a tag to an entrant using the live ENGINE roster.
    Returns a tiny {name:number} dict the Diagnostics UI understands,
    or None if we can't resolve.
    """
    try:
        t = (tag or "").strip()
        if not t:
            return None
        # RaceEngine keeps a dict `entrants` of Entrant objects keyed by entrant_id
        ents = getattr(ENGINE, "entrants", {}) or {}
        for ent_id, ent in ents.items():
            ent_tag = getattr(ent, "tag", None)
            if ent_tag and str(ent_tag).strip() == t:
                name = getattr(ent, "name", None)
                number = getattr(ent, "number", None)
                return {"name": str(name) if name is not None else f"Entrant {ent_id}",
                        "number": (str(number) if number is not None else None)}
    except Exception:
        pass
    return None

async def resolve_tag_to_entrant(tag: str) -> dict | None:
    """
    Resolve a transponder tag to an ENABLED entrant from the authoritative DB.
    No engine fallback. Returns {id, number, name} or None.
    """
    t = (str(tag).strip() if tag is not None else "")
    if not t:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT entrant_id AS id, number, name FROM entrants WHERE enabled=1 AND tag=?",
            (t,),
        )
        row = await cur.fetchone()
        await cur.close()

    if row:
        return {
            "id": int(row["id"]),
            "number": (str(row["number"]) if row["number"] is not None else None),
            "name": row["name"],
        }
    return None

# --- Helper: which flags are allowed in a given phase (returns UPPERCASE names) ---
def _allowed_flags_for_phase(phase: str) -> set[str]:
    p = str(phase or "pre").lower()
    return {
        "pre":        {"PRE", "GREEN"},  # operator can arm/start green
        "countdown":  {"PRE"},           # timer flips to GREEN; only abort to PRE from UI
        "green":      {"GREEN", "YELLOW", "RED", "BLUE", "WHITE", "CHECKERED"},
        "white":      {"GREEN", "YELLOW", "RED", "BLUE", "WHITE", "CHECKERED"},
        "checkered":  {"CHECKERED"},     # locked; use End/Reset to change phase
    }.get(p, {"GREEN", "YELLOW", "RED", "BLUE", "WHITE", "CHECKERED"})

def _cancel_task(t: asyncio.Task | None) -> None:
    """Cancel a task if it's still running; ignore if already finished."""
    if t and not t.done():
        t.cancel()

def _engine_begin_green() -> None:
    """
    Tell the engine we're GREEN using whatever hook it exposes.
    All calls are best-effort and safely guarded.
    """
    for attr, args in [
        ("start_race", ()),
        ("begin_race", ()),
        ("go_green", ()),
        ("set_running", (True,)),   # some engines use a plain boolean
        ("set_flag", ("GREEN",)),   # last resort (you already do this elsewhere)
    ]:
        fn = getattr(ENGINE, attr, None)
        if callable(fn):
            try:
                fn(*args)
                return
            except Exception:
                # Try the next option if this one raises
                pass

    
# ------------------------------------------------------------
# Debugging Endpoints
# ------------------------------------------------------------

# --- DEBUG: quick resolver probe (remove later if you want) ------------------
@app.get("/debug/resolve_tag/{tag}")
async def debug_resolve_tag(tag: str):
    """
    Returns {"number": "...", "name": "..."} for an ENABLED entrant with this tag,
    or {} if unknown. This lets us prove the DB lookup works independently of UI.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT number, name FROM entrants WHERE enabled=1 AND tag=? LIMIT 1",
            (str(tag).strip(),)
        )
        row = await cur.fetchone()
        await cur.close()

    if not row:
        return {}
    return {"number": (row["number"] or None), "name": row["name"]}

# --- TEMP DEBUG: hit /debug/resolve?tag=1234567 to see DB mapping ---
@app.get("/debug/resolve")
async def debug_resolve(tag: str):
    """
    Quick sanity check: does this tag resolve to an ENABLED entrant?
    Returns {id, number, name} or {} if not found.
    """
    try:
        ent = await resolve_tag_to_entrant(str(tag).strip())
        return (ent or {})
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"resolve failed: {type(ex).__name__}: {ex}")


# --- DEBUG: peek into the engine so we know what it thinks is loaded ----------
@app.get("/__debug/engine")
def debug_engine():
    """
    Minimal visibility into the engine:
      - how many entrants are loaded (+ a few sample mappings id→tag)
      - current flag/phase we last synced into the engine (if exposed)
    """
    try:
        ents = getattr(ENGINE, "entrants", {}) or {}
        # Sample first few entrants as {id, name, number, tag}
        sample = []
        for eid, ent in list(ents.items())[:5]:
            sample.append({
                "id": int(eid),
                "name": getattr(ent, "name", None),
                "number": getattr(ent, "number", None),
                "tag": getattr(ent, "tag", None),
                "enabled": getattr(ent, "enabled", None),
            })
        # Some engines expose .flag or .state; both are optional
        flag = getattr(ENGINE, "flag", None)
        state = getattr(ENGINE, "state", None)
        return {
            "entrants_count": len(ents),
            "entrants_sample": sample,
            "engine_flag": flag,
            "engine_state": state,
        }
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}

@app.get("/__debug/state")
def debug_state():
    info = {
        "has_snapshot": hasattr(ENGINE, "snapshot"),
        "entrants_in_engine": len(getattr(ENGINE, "entrants", {})) if hasattr(ENGINE, "entrants") else None,
    }
    if info["has_snapshot"]:
        try:
            snap = ENGINE.snapshot()
            info["snapshot_type"] = type(snap).__name__
            info["snapshot_keys"] = list(snap.keys()) if isinstance(snap, dict) else None
            info["snapshot"] = snap
        except Exception as ex:
            info["snapshot_error"] = f"{type(ex).__name__}: {ex}"
    return info

@app.get("/__debug/roster")
def debug_roster():
    """
    Show the engine's in-memory entrant map (id, name, tag, enabled/status if available).
    This is read-only and safe to call anytime.
    """
    ents = getattr(ENGINE, "entrants", {}) or {}
    out = []
    for eid, ent in ents.items():
        try:
            out.append({
                "id": int(getattr(ent, "entrant_id", eid)),
                "name": getattr(ent, "name", None),
                "number": getattr(ent, "number", None),
                "tag": getattr(ent, "tag", None),
                "enabled": getattr(ent, "enabled", None),
                "status": getattr(ent, "status", None),
            })
        except Exception:
            out.append({"id": eid, "repr": repr(ent)})
    return {
        "count": len(ents),
        "entrants": out,
    }

@app.get("/__debug/setup_cache")
def debug_setup_cache():
    """
    Dump the last session_config and the entrants we mapped for ENGINE.load().
    Lets us see if the frontend posted the right roster and if we filtered to enabled-only.
    """
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "race_id": _CURRENT_RACE_ID,
        "entrants_count": len(_CURRENT_ENTRANTS_ENGINE or []),
        "entrants_sample": (_CURRENT_ENTRANTS_ENGINE or [])[:5],
        "session_config": _CURRENT_SESSION or {},
    })

@app.get("/__debug/last_engine_load")
def debug_last_engine_load():
    return _LAST_ENGINE_LOAD or {"note": "no ENGINE.load call recorded yet"}

# ------------------------------------------------------------
# Race Modes YAML (for UI consumption)
# ------------------------------------------------------------
def _load_modes_file() -> dict:
    try:
        data = yaml.safe_load(MODES_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    # tolerate either {"modes": {...}} or a bare mapping
    return data.get("modes", data)

def _save_modes_file(modes: dict) -> None:
    MODES_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODES_PATH.write_text(
        yaml.safe_dump({"modes": modes}, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )

class ModeUpsert(BaseModel):
    id: str
    mode: dict

# ---------------- Race setup: modes ----------------

@app.get("/setup/race_modes")
def get_race_modes():
    """Return built-in modes for Race Setup, read from root/config/race_modes.yaml."""
    return {"modes": _load_modes_file()}

@app.post("/setup/race_modes/save")
def save_race_mode(payload: ModeUpsert):
    """Upsert a single mode in root/config/race_modes.yaml (used by 'Custom → Save Mode')."""
    modes = _load_modes_file()
    modes[payload.id] = payload.mode
    _save_modes_file(modes)
    return {"ok": True, "id": payload.id}



# ------------------------------------------------------------
# Endpoints - Race Setup save + Race Control fetch
# ------------------------------------------------------------


@app.post("/race/setup")
async def race_setup(
    req: SetupReq = Body(
        ...,
        example={
            "race_id": 1,
            "entrants": [
                {"id": 101, "name": "Driver A", "number": "1", "tag": None, "enabled": True, "status": "ACTIVE"}
            ],
            "session_config": {
                "event_label": "Maker Faire Orlando",
                "session_label": "Heat 1",
                "mode_id": "sprint",
                "limit": {"type": "time", "value_s": 900, "soft_end": False},
                "rank_method": "total_laps",
                "min_lap_s": 4.1,
                "countdown": {
                    "start_enabled": True,
                    "start_from_s": 10,
                    "end_enabled": False,
                    "end_from_s": 10,
                    "timeout_s": 0
                },
                "announcements": {
                    "lap_indication": "beep",
                    "rank_enabled": False,
                    "rank_interval_laps": 0,
                    "rank_change_speech": False,
                    "best_lap_speech": False
                },
                "sounds": {
                    "countdown_beep_last_s": 0,
                    "starting_horn": False,
                    "white_flag": {"mode": "auto"},
                    "checkered_horn": False
                },
                "bypass": {"decoder": True, "entrants": False}
            }
        }
    )
):
    """
    Race Setup submits one authoritative session_config + entrants roster.
    We store session_config, map entrants to ENGINE format, and call ENGINE.load().
    """
    try:
        # Cache session and race id
        global _CURRENT_SESSION, _CURRENT_ENTRANTS_ENGINE, _CURRENT_RACE_ID, _RACE_STATE
        _CURRENT_SESSION = req.session_config.model_dump() if hasattr(req.session_config, "model_dump") else dict(req.session_config)
        _CURRENT_RACE_ID = int(req.race_id)

        # Prime live state
        _RACE_STATE["race_id"] = _CURRENT_RACE_ID
        _RACE_STATE["phase"] = Phase.PRE.value
        _RACE_STATE["flag"] = "PRE"
        _RACE_STATE["start_at"] = None

        # countdown_from_s only if enabled
        cd = (_CURRENT_SESSION.get("countdown") or {})
        _RACE_STATE["countdown_from_s"] = int(cd.get("start_from_s") or 0) if cd.get("start_enabled") else 0

        # limit carries through for control UI
        _RACE_STATE["limit"] = (_CURRENT_SESSION.get("limit") or {"type": "time", "value_s": 0})

        # Map entrants → engine shape
        entrants_engine: List[Dict[str, Any]] = []
        for e in (req.entrants or []):
            # support EntrantIn model *or* plain dicts
            get = (lambda k: getattr(e, k, None)) if not isinstance(e, dict) else e.get
            eid = get("id")
            if eid is None:
                raise HTTPException(status_code=400, detail="entrant id is required")
            entrants_engine.append({
                "entrant_id": e.id,  # guaranteed int
                "name": e.name,
                "number": (str(e.number).strip() if e.number is not None else None),
                "tag": _normalize_tag(e.tag),
                "enabled": bool(e.enabled),
                "status": (e.status or "ACTIVE").upper(),
            })

        entrants_copy = [dict(e) for e in entrants_engine]
        _CURRENT_ENTRANTS_ENGINE = entrants_copy[:]  # keep for reset

        # Apply qualifying grid if available for this event
        event_id = CONFIG.get('app', {}).get('engine', {}).get('event', {}).get('id', 1)
        try:
            import sqlite3
            from backend.db_schema import get_event_config
            db_conn = sqlite3.connect(DB_PATH)
            db_conn.row_factory = sqlite3.Row
            event_cfg = get_event_config(db_conn, event_id)
            qual_grid = event_cfg.get("qualifying", {}).get("grid", []) if event_cfg else []
            db_conn.close()
            
            if qual_grid:
                # Create maps for grid order and brake test results
                grid_map = {entry["entrant_id"]: entry["order"] for entry in qual_grid}
                brake_map = {entry["entrant_id"]: entry.get("brake_ok", True) for entry in qual_grid}
                
                # Get brake test policy
                brake_policy = CONFIG.get('app', {}).get('engine', {}).get('qualifying', {}).get('brake_test_policy', 'warn')
                
                # Add grid_index and brake_valid to each entrant
                for e in entrants_copy:
                    eid = e["entrant_id"]
                    if eid in grid_map:
                        e["grid_index"] = grid_map[eid]
                        # brake_valid: True = pass, False/None = fail
                        brake_result = brake_map.get(eid)
                        e["brake_valid"] = True if brake_result is True else False
                
                # Build a map of best lap times from qual_grid for sorting demoted entrants
                best_lap_map = {entry["entrant_id"]: entry.get("best_ms", float('inf')) for entry in qual_grid}
                
                # Sort entrants by grid order, applying brake test policy
                def sort_key(e):
                    grid_pos = e.get("grid_index")
                    brake_ok = e.get("brake_valid", False)
                    
                    if grid_pos is not None:
                        # If brake_test_policy is "demote" and brake test failed/null, demote to back
                        if brake_policy == "demote" and not brake_ok:
                            # Sort demoted entrants by best lap (fastest first)
                            best_ms = best_lap_map.get(e["entrant_id"], float('inf'))
                            return (2, best_ms)  # Demoted entrants sorted by lap time
                        return (0, grid_pos)  # Grid entrants with passing brake test
                    else:
                        return (1, e["entrant_id"])  # Non-grid entrants in middle
                
                entrants_copy.sort(key=sort_key)
                
                log.info(f"Applied qualifying grid with {len(grid_map)} positions to {len(entrants_copy)} entrants")
        except Exception as e:
            log.warning(f"Could not apply qualifying grid: {e}")

        # Make entrants available to Seen block (and anything else that needs roster)
        _RACE_STATE["entrants"] = entrants_copy[:]
        _reseed_seen_roster(_RACE_STATE["entrants"])

  
        # Pick race_type from mode id
        race_type = str(_CURRENT_SESSION.get("mode_id", "sprint"))

        # Call engine with its real signature
        #snap = ENGINE.load(
        #    race_id=_CURRENT_RACE_ID,
        #    entrants=entrants_engine,
        #    race_type=race_type,
        #)

# --- instrumentation: record exactly what we send to the engine ---
        global _LAST_ENGINE_LOAD
        _LAST_ENGINE_LOAD = {
            "race_id": _CURRENT_RACE_ID,
            "race_type": race_type,
            "entrants_len": len(entrants_engine),
            "entrants_sample": entrants_engine[:4],  # first few only
        }

        snap = ENGINE.load(
            race_id=_CURRENT_RACE_ID,
            entrants=entrants_copy,
            race_type=race_type,
            session_config=_CURRENT_SESSION,
        )
        _apply_session_min_lap(_CURRENT_SESSION)

        # Ensure event and heat exist in database
        event_id = CONFIG.get('app', {}).get('engine', {}).get('event', {}).get('id', 1)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                'INSERT OR IGNORE INTO events (event_id, name, config_json) VALUES (?, ?, ?)',
                (event_id, 'Event', '{}')
            )
            await db.execute(
                'INSERT OR IGNORE INTO heats (heat_id, event_id, name, config_json) VALUES (?, ?, ?, ?)',
                (_CURRENT_RACE_ID, event_id, f'{race_type} Race', '{}')
            )
            await db.commit()

        # Capture a minimal echo of what the engine returned (don’t spam huge payloads)
        try:
            if isinstance(snap, dict):
                _LAST_ENGINE_LOAD["engine_return_keys"] = sorted(list(snap.keys()))[:12]
                _LAST_ENGINE_LOAD["engine_flag"] = snap.get("flag")
                _LAST_ENGINE_LOAD["engine_race_id"] = snap.get("race_id")
        except Exception:
            pass




        # Derived shape for Race Control (if you have this helper)
        derived = _derive_for_control(_CURRENT_SESSION) if "_derive_for_control" in globals() else {}

        # Blackout lights when starting a new race control session
        _send_blackout_to_lighting(True)

        return JSONResponse({
            "ok": True,
            "session_id": _CURRENT_RACE_ID,
            "snapshot": snap,
            "derived": derived,
        })

    except HTTPException:
        # let explicit 4xx bubble as-is
        raise
    except TypeError as te:
        # Very helpful when ENGINE.load signature mismatches
        log.exception("ENGINE.load TypeError")
        raise HTTPException(status_code=500, detail=f"ENGINE.load TypeError: {te}")
    except Exception as ex:
        log.exception("race_setup failed")
        raise HTTPException(status_code=500, detail=f"race_setup failed: {type(ex).__name__}: {ex}")




# ------------------------------------------------------------
# Readiness compatibility (UI pings these)
# ------------------------------------------------------------



@app.get("/health")
def health_alias():
    # UI expects /health; you also expose /healthz and /readyz.
    return {"status": "ok", "service": "ccrs-backend"}

@app.get("/admin/entrants/enabled_count")
async def entrants_enabled_count():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM entrants WHERE enabled=1")
        row = await cur.fetchone()
        await cur.close()
        return {"count": int(row[0] if row and row[0] is not None else 0)}

@app.get("/decoders/status")
async def decoders_status():
    now = time.time()
    age = now - float(_SCANNER_STATUS.get("last_heartbeat") or 0.0)
    online = (age >= 0.0) and (age < _HEARTBEAT_ONLINE_WINDOW_S)

    meta = _SCANNER_STATUS.get("meta") or {}
    return JSONResponse({
        "online": 1 if online else 0,
        "age_s": round(age, 3) if age > 0 else None,
        "source": (meta.get("source") or meta.get("type") or "unknown"),
        "port": meta.get("port") or meta.get("device") or None,
        "baud": meta.get("baud") or meta.get("baudrate") or None,
        "device_id": meta.get("device_id") or None,
        "host": meta.get("host") or None,
    })


# ------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------
@app.post("/engine/load")
async def engine_load(payload: Dict[str, Any]):
    """
    Load the current runtime session with the given entrants list.

    Validation rules (hard 400s):
      - payload.race_id must be present and int-able.
      - payload.entrants must be a list.
      - each entrant must be an object with an 'id' that is int-able.
    We coerce id to int **before** any further mapping.
    """
    race_id = payload.get("race_id")
    if race_id is None:
        raise HTTPException(status_code=400, detail="missing required field: race_id")
    try:
        race_id = int(race_id)
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid race_id: {race_id!r}")

    entrants_ui = payload.get("entrants", []) or []
    if not isinstance(entrants_ui, list):
        raise HTTPException(status_code=400, detail="entrants must be a list")

    # Validate + coerce ids in place
    for idx, item in enumerate(entrants_ui):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"entrant at index {idx} must be an object")
        if item.get("id") is None:
            raise HTTPException(status_code=400, detail=f"entrant at index {idx} missing id")
        try:
            item["id"] = int(item["id"])
        except Exception:
            raise HTTPException(status_code=400, detail=f"invalid entrant id at index {idx}: {item.get('id')!r}")

    # Map to the engine's expected shape: entrant_id / number / status
    entrants_engine: List[Dict[str, Any]] = []
    for item in entrants_ui:
        entrants_engine.append({
            "entrant_id": item["id"],
            "name": item.get("name"),
            "number": (str(item.get("number")).strip()
                           if item.get("number") is not None else None),
            "tag": _normalize_tag(item.get("tag")),
            "enabled": bool(item.get("enabled", True)),
            "status": str(item.get("status", "ACTIVE")).upper(),
        })

    # Cache race id for resets
    global _CURRENT_RACE_ID, _CURRENT_ENTRANTS_ENGINE
    _CURRENT_RACE_ID = race_id
    _CURRENT_ENTRANTS_ENGINE = entrants_engine[:]   # keep roster for /race/reset_session

    # Choose race_type: prefer session mode if available, else default
    race_type = _CURRENT_SESSION.get("mode_id", "sprint") if _CURRENT_SESSION else "sprint"

    # Ensure event and heat exist in database for this race
    event_id = CONFIG.get("app", {}).get("engine", {}).get("event", {}).get("id", 1)
    event_name = CONFIG.get("app", {}).get("engine", {}).get("event", {}).get("name", "Unknown Event")
    event_date = CONFIG.get("app", {}).get("engine", {}).get("event", {}).get("date", None)
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Ensure event exists
        await db.execute("""
            INSERT OR IGNORE INTO events (event_id, name, date_utc, config_json)
            VALUES (?, ?, ?, '{}')
        """, (event_id, event_name, event_date))
        
        # Ensure heat exists for this race_id
        await db.execute("""
            INSERT OR IGNORE INTO heats (heat_id, event_id, name, config_json)
            VALUES (?, ?, ?, '{}')
        """, (race_id, event_id, f"{race_type.capitalize()} Race"))
        
        await db.commit()

    snapshot = ENGINE.load(
        race_id=race_id,
        entrants=entrants_engine,
        race_type=str(race_type),
        session_config=_CURRENT_SESSION,
    )
    return JSONResponse(snapshot)



### normalized /tag assignment endpoint
@app.post("/engine/entrant/assign_tag")
async def engine_entrant_assign_tag(payload: Dict[str, Any]):
    """
    Idempotently assign (or clear) a transponder tag for a specific entrant.
    Guarantees:
      - Same tag to same entrant => 200 (no-op, still mirrored to Engine).
      - Conflicts consider ONLY enabled entrants and exclude the incumbent.
      - On change, writes through to DB.
      - If the entrant isn't in the active session, 412 Precondition Failed.
    """
    entrant_id = payload.get("entrant_id")
    if entrant_id is None:
        raise HTTPException(status_code=400, detail="missing required field: entrant_id")
    try:
        entrant_id = int(entrant_id)
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid entrant_id: {payload.get('entrant_id')!r}")

    tag = _normalize_tag(payload.get("tag"))

    async with aiosqlite.connect(DB_PATH) as db:
        # Fetch current row to compute idempotence and to confirm existence
        row = await _fetch_one(db, "SELECT enabled, tag FROM entrants WHERE entrant_id=?", (entrant_id,))
        if not row:
            raise HTTPException(status_code=404, detail=f"entrant {entrant_id} not found")
        enabled_db, tag_db = row[0], row[1]

        # Idempotent fast-path: nothing to change, but keep Engine in sync
        if tag_db == tag:
            try:
                snap = ENGINE.assign_tag(entrant_id, tag)
            except KeyError:
                raise HTTPException(status_code=412, detail="Entrant not in active session; reload roster via /engine/load")
            return JSONResponse(snap or {"ok": True})

        # Conflict check across ENABLED entrants, excluding this entrant
        if tag:
            # Use a short-lived sync connection for the helper (clean and clear).
            with sqlite3.connect(DB_PATH) as sconn:
                if tag_conflicts(sconn, tag, incumbent_entrant_id=entrant_id):
                    raise HTTPException(status_code=409, detail="Tag already assigned to another enabled entrant")

        # Update Engine first so UI reflects the new tag immediately
        try:
            snap = ENGINE.assign_tag(entrant_id, tag)
        except KeyError:
            raise HTTPException(status_code=412, detail="Entrant not in active session; reload roster via /engine/load")

        # Write-through to DB (NULL when clearing)
        await _exec(db,
                    "UPDATE entrants SET tag=?, updated_at=strftime('%s','now') WHERE entrant_id=?",
                    (tag, entrant_id))

    return JSONResponse(snap or {"ok": True})



""" @app.post("/engine/pass")
async def engine_pass(payload: Dict[str, Any]):

    Normalized pass ingest endpoint.
 #   Accepts: { "tag": "1234567", "source": "SF" }
    tag = _normalize_tag(payload.get("tag"))
    source = str(payload.get("source") or "SF")
    if not tag:
        raise HTTPException(status_code=400, detail="missing tag")
    result = await _ingest_tag_to_engine(tag, source=source)
    return JSONResponse(result if result else {"ok": True})
 """

# ------------------------------------------------------------
# OSC Lighting Integration Helpers
# ------------------------------------------------------------
def _handle_flag_from_qlc(name: str):
    """
    Handle flag button press from QLC+ lighting console.
    
    Called via call_soon_threadsafe when QLC+ sends an OSC message.
    Updates the engine flag state to match the lighting console.
    
    RESTRICTIONS (to prevent lighting operator from controlling race timing):
    - Cannot set GREEN unless race is already running (prevents starting race)
    - Cannot set CHECKERED unless race is already running (prevents ending race)
    - CAN change flags during race (e.g., YELLOW ↔ GREEN for cautions)
    - CAN change other informational flags (BLUE, WHITE, RED, PRE)
    
    Args:
        name: Flag name (green, yellow, red, white, checkered, blue)
    """
    try:
        flag_upper = name.upper()
        current_phase = str(_RACE_STATE.get("phase", "pre")).lower()
        
        # Guard: Prevent lighting from starting the race
        if flag_upper == "GREEN" and current_phase in ("pre", "countdown"):
            log.warning("QLC+ attempted to set GREEN during %s - ignoring (race not started)", current_phase)
            return
        
        # Guard: Prevent lighting from ending the race
        if flag_upper == "CHECKERED" and current_phase not in ("green", "white"):
            log.warning("QLC+ attempted to set CHECKERED during %s - ignoring (race not running)", current_phase)
            return
        
        # Allow: Flag changes during active race
        if hasattr(ENGINE, "set_flag"):
            ENGINE.set_flag(flag_upper)
        _RACE_STATE["flag"] = flag_upper
        log.info("Flag changed from QLC+: %s", flag_upper)
    except Exception:
        log.exception("Failed to handle flag from QLC+: %s", name)

def _handle_blackout_from_qlc(enabled: bool):
    """
    Handle blackout button press from QLC+ lighting console.
    
    Called via call_soon_threadsafe when QLC+ sends an OSC message.
    Stores blackout state for UI consumption.
    
    Args:
        enabled: True if blackout is active, False if off
    """
    try:
        _RACE_STATE["blackout"] = enabled
        log.info("Blackout changed from QLC+: %s", enabled)
    except Exception:
        log.exception("Failed to handle blackout from QLC+: %s", enabled)

def _send_flag_to_lighting(flag_name: str):
    """
    Send flag change to OSC lighting system (QLC+).
    
    Called whenever CCRS changes a flag to sync lighting.
    Safe to call even if OSC is disabled - will no-op gracefully.
    
    Args:
        flag_name: Flag name (GREEN, YELLOW, RED, WHITE, CHECKERED, BLUE, etc.)
    """
    if _osc_out:
        try:
            _osc_out.send_flag(flag_name.lower(), on=True)
        except Exception:
            # Never let lighting failures break race control
            log.exception("Failed to send flag to lighting: %s", flag_name)

def _send_blackout_to_lighting(enabled: bool):
    """
    Send blackout state to OSC lighting system (QLC+).
    
    Blackout is a "hard kill" that turns off all lighting.
    Typically used when resetting, aborting, or transitioning out of race control.
    Safe to call even if OSC is disabled - will no-op gracefully.
    
    Args:
        enabled: True to enable blackout (lights off), False to disable
    """
    if _osc_out:
        try:
            _osc_out.send_blackout(enabled)
            _RACE_STATE["blackout"] = enabled
        except Exception:
            # Never let lighting failures break race control
            log.exception("Failed to send blackout to lighting: %s", enabled)

def _send_countdown_to_lighting():
    """
    Send countdown state to lighting system (QLC+).
    
    Called when race enters countdown phase before green flag.
    Currently sends RED flag as a visual indicator that countdown is active.
    
    Future enhancements could include:
    - Custom countdown OSC messages
    - Progressive color changes (red → yellow → green)
    - Synchronization with audio countdown beeps
    - Strobe/flash patterns
    
    Safe to call even if OSC is disabled - will no-op gracefully.
    """
    # For now, use RED flag during countdown as a "staging" indicator
    # This keeps lights on but signals "not yet racing"
    _send_flag_to_lighting("RED")
    log.info("Countdown active - lighting set to RED (staging)")

# ------------------------------------------------------------
# Endpoints - flag / state / reset (lightweight, engine-aware)
# Race Control Endpoints
# ------------------------------------------------------------
# --- Route: POST /engine/flag  (idempotent + GREEN allowed while racing) ---
class FlagReq(BaseModel):
    flag: str

@app.post("/engine/flag")
async def engine_set_flag(req: FlagReq):
    """
    Set the live flag. Calls ENGINE.set_flag(...) if available; otherwise updates local state.
    Also keeps local _RACE_STATE in sync for UI.
    """
    # Normalize inputs/current state
    req_flag  = str(req.flag or "PRE").upper()
    phase_str = str(_RACE_STATE.get("phase") or Phase.PRE.value).lower()
    cur_flag  = str(_RACE_STATE.get("flag")  or "PRE").upper()

    # 1) Idempotent: setting the same flag is a no-op but returns 200
    if req_flag == cur_flag:
        return JSONResponse({
            "ok": True,
            "flag": cur_flag,
            "phase": _RACE_STATE.get("phase"),
            "idempotent": True,
            "clock": _state_clock_block(),
        })

    # 2) Phase rules: allow GREEN while racing; restrict countdown; keep PRE conservative
    allowed = _allowed_flags_for_phase(phase_str)  # UPPERCASE members
    if req_flag not in allowed:
        raise HTTPException(status_code=409, detail=f"flag '{req_flag}' not allowed in phase '{phase_str}'")

    # 3) Try engine first (if it enforces additional rules, surface as 400)
    if hasattr(ENGINE, "set_flag"):
        try:
            ENGINE.set_flag(req_flag)
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))
        except Exception:
            # Don't hard-fail UI if engine refused for non-ValueError reasons
            pass
    else:
        # Basic validation if no engine behind us
        if req_flag not in {"PRE","GREEN","YELLOW","RED","WHITE","BLUE","CHECKERED"}:
            raise HTTPException(status_code=400, detail="invalid flag value")

    # 4) Local sync for UI convenience
    _RACE_STATE["flag"] = req_flag
    if req_flag == "WHITE":
        _RACE_STATE["phase"] = Phase.WHITE.value
    elif req_flag == "CHECKERED":
        _RACE_STATE["phase"] = Phase.CHECKERED.value
    elif req_flag == "GREEN":
        # If GREEN is asserted manually, assume race running; start clock if needed
        if _RACE_STATE.get("start_at") is None:
            _RACE_STATE["start_at"] = _now()
        _RACE_STATE["phase"] = Phase.GREEN.value
    elif req_flag == "PRE":
        _RACE_STATE["phase"] = Phase.PRE.value
        # (intentionally not clearing start_at; abort/reset endpoint handles full reset)

    # 5) Send flag change to lighting system (QLC+)
    _send_flag_to_lighting(req_flag)

    # 6) Prefer engine snapshot; augment with local phase/flag/clock for UI
    if hasattr(ENGINE, "snapshot"):
        try:
            snap = ENGINE.snapshot()
            if isinstance(snap, dict):
                snap["phase"] = _RACE_STATE["phase"]
                snap["flag"]  = _RACE_STATE["flag"]
                clock_block   = _state_clock_block()
                snap["clock"] = clock_block
                snap["clock_ms"] = clock_block.get("clock_ms")
                snap["countdown_remaining_s"] = clock_block.get("countdown_remaining_s")
            return JSONResponse(snap)
        except Exception:
            pass

    return JSONResponse({
        "ok": True,
        "flag": _RACE_STATE["flag"],
        "phase": _RACE_STATE["phase"],
        "clock": _state_clock_block(),
    })

# ------------------------- State: engine snapshot + local --------------------

@app.get("/race/state")
async def race_state():
    """
    Minimal overlay:
      • Prefer ENGINE.snapshot() verbatim.
      • Overlay only phase/flag/limit/countdown_from_s.
      • CLOCK: use local _state_clock_block() only during PRE/COUNTDOWN.
               In racing/finish phases, do NOT overwrite engine clock; at most
               mirror engine clock.clock_ms → top-level clock_ms if absent.
      • Always include 'seen'.
      • Do not mutate _RACE_STATE here.
    """
    if hasattr(ENGINE, "snapshot"):
        try:
            snap = ENGINE.snapshot() or {}
            if isinstance(snap, dict):
                # 1) Fill minimal overlays from local mirror (no stomping)
                snap.setdefault("phase", _RACE_STATE["phase"])
                snap.setdefault("flag",  _RACE_STATE["flag"])
                snap.setdefault("limit", _RACE_STATE["limit"])
                snap.setdefault("countdown_from_s", _RACE_STATE["countdown_from_s"])

                # 2) Clock policy
                phase_lower = str(snap.get("phase") or "").lower()
                if phase_lower in ("pre", "countdown"):
                    cb = _state_clock_block()
                    snap["clock"] = cb
                    snap["clock_ms"] = cb.get("clock_ms")
                    # Convenience field for UIs that read it flat:
                    snap["countdown_remaining_s"] = cb.get("countdown_remaining_s")
                else:
                    # Engine owns time. Only mirror nested clock_ms to top-level if missing.
                    if isinstance(snap.get("clock"), dict) and "clock_ms" not in snap:
                        cm = snap["clock"].get("clock_ms")
                        if isinstance(cm, (int, float)):
                            snap["clock_ms"] = cm

                # 3) Running hint (don’t fight engine if already present)
                snap.setdefault("running", phase_lower in ("green", "white"))

                # 4) Build standings using frozen grid when not racing; live when racing
                try:
                    raw_eid = _CURRENT_RACE_ID
                    if raw_eid not in (None, "", 0, "0"):
                        event_id = int(raw_eid)
                        with sqlite3.connect(DB_PATH) as sconn:
                            snap["standings"] = build_standings_payload(
                                sconn, event_id=event_id, phase=phase_lower, engine_snapshot=snap
                            )
                except Exception as _ex:
                    # Don’t let standings break state responses
                    pass

                # 5) Always attach live 'seen'
                snap["seen"] = _state_seen_block()
                
                # 6) Attach session labels from _CURRENT_SESSION if available
                if _CURRENT_SESSION:
                    snap.setdefault("session_label", _CURRENT_SESSION.get("session_label"))
                    snap.setdefault("event_label", _CURRENT_SESSION.get("event_label"))

            return JSONResponse(snap)
        except Exception:
            pass  # fall through to local scaffold

    # Fallback when engine has no snapshot
    cb = _state_clock_block()
    return JSONResponse({
        "ok": True,
        "race_id": _RACE_STATE["race_id"],
        "phase":   _RACE_STATE["phase"],
        "flag":    _RACE_STATE["flag"],
        "limit":   _RACE_STATE["limit"],
        "countdown_from_s": _RACE_STATE["countdown_from_s"],
        "clock":   cb,
        "clock_ms": cb.get("clock_ms"),
        "countdown_remaining_s": cb.get("countdown_remaining_s"),
        "engine":  "no-snapshot",
        "seen":    _state_seen_block(),
        "running": str(_RACE_STATE["phase"]).lower() in ("green", "white"),
    })


# --------------------------- Start / End / Abort -----------------------------

@app.post("/race/control/start_prep")
def race_start_prep():
    """
    Operator clicked 'Pre-Race Start'.
    Put the session into PRE, no countdown, no clock, no engine flagging.
    """
    if _RACE_STATE["race_id"] is None:
        raise HTTPException(status_code=409, detail="No race loaded (call /race/setup first)")

    _RACE_STATE["phase"]    = Phase.PRE.value
    _RACE_STATE["flag"]     = "PRE"
    _RACE_STATE["start_at"] = None   # ensure clock is parked

    # Do NOT set countdown here. Countdown is only entered by /race/control/start_race.
    # Also do NOT notify the engine; the UI isn’t racing yet.

    return {"ok": True, "phase": _RACE_STATE["phase"]}



# --- START RACE (must be async and unique) -----------------------------------
@app.post("/race/control/start_race")
async def race_start_race():
    if _RACE_STATE["race_id"] is None:
        raise HTTPException(status_code=409, detail="No race loaded (call /race/setup first)")

    global _COUNTDOWN_TASK
    cd = int(_RACE_STATE.get("countdown_from_s") or 0)

    if cd > 0:
        _RACE_STATE["phase"] = Phase.COUNTDOWN.value
        _RACE_STATE["flag"]  = "PRE"

        # >>> REQUIRED for the UI to show T-minus and for /race/state to tick
        _RACE_STATE["countdown_anchor_s"] = time.time() + cd

        # Signal lighting that countdown has started
        _send_countdown_to_lighting()

        _cancel_task(_COUNTDOWN_TASK)
        log.info(f"[START] COUNTDOWN armed for {cd}s; anchor={_RACE_STATE['countdown_anchor_s']:.3f}")
        _COUNTDOWN_TASK = asyncio.get_running_loop().create_task(_auto_go_green(cd))
        return {"ok": True, "phase": _RACE_STATE["phase"], "countdown_from_s": cd}

    # Immediate GREEN path
    _RACE_STATE["phase"]    = Phase.GREEN.value
    _RACE_STATE["flag"]     = "GREEN"
    _RACE_STATE["start_at"] = time.time()
    _engine_begin_green()  # tell engine we're green
    if hasattr(ENGINE, "set_flag"):
        try: ENGINE.set_flag("GREEN")
        except Exception: pass
    _send_flag_to_lighting("GREEN")  # Sync lighting on immediate green
    log.info(f"[START] GREEN immediately; start_at={_RACE_STATE['start_at']:.3f}")
    return {"ok": True, "phase": _RACE_STATE["phase"], "start_at": _RACE_STATE["start_at"]}



#---------------------- End Race      ----------------------
@app.post("/race/control/end_race")
def race_end_race():
    global _COUNTDOWN_TASK
    _cancel_task(_COUNTDOWN_TASK)

    # Operator clicked 'End Race' → CHECKERED; freeze clock.
    if _RACE_STATE["race_id"] is None:
        raise HTTPException(status_code=409, detail="No race loaded (call /race/setup first)")

    _RACE_STATE["phase"]     = Phase.CHECKERED.value
    _RACE_STATE["flag"]      = "CHECKERED"
    _RACE_STATE["frozen_at"] = time.time()   # <- add this line

    if hasattr(ENGINE, "set_flag"):
        try:
            ENGINE.set_flag("CHECKERED")
        except Exception:
            pass
    _send_flag_to_lighting("CHECKERED")  # Sync lighting on race end
    return {"ok": True, "phase": _RACE_STATE["phase"]}

# ---------------------- Abort & Reset route ----------------------
@app.post("/race/control/abort_reset")
async def race_abort_reset():
    """
    Abort & reset to PRE (keep roster intact, clear timing).
    """
    global _COUNTDOWN_TASK
    _cancel_task(_COUNTDOWN_TASK)

    # Local mirror: PRE with no start time
    _RACE_STATE["phase"]    = Phase.PRE.value
    _RACE_STATE["flag"]     = "PRE"
    _RACE_STATE["start_at"] = None
    
    _send_flag_to_lighting("PRE")  # Sync lighting on abort/reset
    _send_blackout_to_lighting(True)  # Kill all lights on abort/reset

    # Best-effort engine reset
    reset_fn = getattr(ENGINE, "reset_session", None)
    reset_ok = False
    if callable(reset_fn):
        try:
            reset_fn()   # should stop running and zero the clock
            reset_ok = True
        except Exception:
            reset_ok = False

    if not reset_ok:
        # Fallback: force-stop the clock and go to PRE
        try:
            if hasattr(ENGINE, "running"):
                ENGINE.running = False
            if hasattr(ENGINE, "clock_start_monotonic"):
                ENGINE.clock_start_monotonic = None
            if hasattr(ENGINE, "clock_ms"):
                ENGINE.clock_ms = 0
        except Exception:
            pass
        try:
            if hasattr(ENGINE, "set_flag"):
                ENGINE.set_flag("PRE")
        except Exception:
            pass

        _reload_engine_from_cached_session()

    # Mirror session config back into local state
    if _CURRENT_SESSION:
        _RACE_STATE["limit"] = _CURRENT_SESSION.get("limit") or _RACE_STATE.get("limit")
        cd = (_CURRENT_SESSION.get("countdown") or {})
        _RACE_STATE["countdown_from_s"] = (
            int(cd.get("start_from_s") or 0) if cd.get("start_enabled") else 0
        )
        if "min_lap_s" in _CURRENT_SESSION:
            _RACE_STATE["min_lap_s"] = _CURRENT_SESSION.get("min_lap_s")

    # Optional: clear in-memory seen counters if you keep them
    _reseed_seen_roster(_RACE_STATE.get("entrants") or [])
    return {"ok": True, "phase": _RACE_STATE["phase"]}


# ---------------------- Open Results (transition from race control) ----------------------
@app.post("/race/control/open_results")
def race_open_results():
    """
    Triggered when user clicks "Open Results & Exports" button.
    Activates blackout (kills all lighting) as we transition out of race control.
    """
    _send_blackout_to_lighting(True)
    log.info("Blackout activated for results transition")
    return {"ok": True, "blackout": True}


# ---------------------- Backward-compatible reset route ----------------------

@app.post("/race/reset_session")
async def reset_session():
    """
    Your original reset route:
      - Prefer engine-native reset if present
      - Else re-load prior roster
      - Always sync local state back to PRE
    """
    global _CURRENT_ENTRANTS_ENGINE, _CURRENT_RACE_ID, _CURRENT_SESSION

    # Local state reset
    _RACE_STATE["phase"] = Phase.PRE.value
    _RACE_STATE["flag"] = "PRE"
    _RACE_STATE["start_at"] = None

    # Engine-native reset if available
    reset_fn = getattr(ENGINE, "reset_session", None)
    if callable(reset_fn):
        snap = reset_fn()
        return JSONResponse({"ok": True, "snapshot": snap})

    # Fallback: re-load with cached roster + mode + race_id
    snap = _reload_engine_from_cached_session()

    # Best-effort set PRE in engine
    set_flag = getattr(ENGINE, "set_flag", None)
    if callable(set_flag):
        try:
            set_flag("PRE")
        except Exception:
            pass

    return JSONResponse({"ok": True, "snapshot": snap})


# ------------------------------------------------------------
# Admin Entrants (authoritative DB read/write)
# ------------------------------------------------------------

# If not already imported somewhere above:
# from backend.db_schema import tag_conflicts  # adjust import path if needed

class EntrantIn(BaseModel):
    """
    Authoritative entrant record for the database.

    Semantics:
      - 'id': None or <= 0  => CREATE (SQLite assigns primary key)
              > 0           => UPDATE that row
      - 'number': coerced to string (accepts int or str)
      - 'tag': empty/whitespace becomes None; conflicts apply only when enabled==True and tag is not None
      - 'enabled': coerced to bool (accepts 1/0, "1"/"0", true/false)
    """
    id: Optional[int] = Field(default=None, description="entrant_id primary key; None/<=0 means create")

    number: Optional[str] = None
    name: str
    tag: Optional[str] = None
    enabled: bool = True
    status: str = "ACTIVE"

    # NEW: extra fields we actually want to persist
    organization: Optional[str] = ""
    spoken_name: Optional[str] = ""
    color: Optional[str] = None

    # ---- Normalizers / Coercions ----------------------------------------------------
    @field_validator('number', mode='before')
    @classmethod
    def _coerce_number(cls, v):
        if v is None:
            return None
        return str(v)

    @field_validator('tag', mode='before')
    @classmethod
    def _normalize_tag(cls, v):
        """
        Empty/whitespace-only tags become None so they don't participate
        in the unique-when-enabled rule.
        """
        if v is None:
            return None
        s = str(v).strip()
        return s or None


    @field_validator('enabled', mode='before')
    @classmethod
    def _coerce_enabled(cls, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v == 1
        if isinstance(v, str):
            sv = v.strip().lower()
            if sv in ('1', 'true', 'yes', 'y', 'on'):
                return True
            if sv in ('0', 'false', 'no', 'n', 'off'):
                return False
        return bool(v)

    def is_create(self) -> bool:
        return self.id is None or (isinstance(self.id, int) and self.id <= 0)


def _norm_tag(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


@app.get("/admin/entrants")
async def admin_list_entrants():
    """
    Authoritative read of entrants for Operator UI.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT
              entrant_id AS id,
              number, name, tag, enabled,
              status, organization, spoken_name, color,
              logo, updated_at
            FROM entrants
            ORDER BY CAST(number AS INTEGER) NULLS LAST, name
        """)
        rows = await cur.fetchall()
        await cur.close()

        out = []
        for r in rows:
            out.append({
                "id": int(r["id"]) if r["id"] is not None else None,
                "number": (str(r["number"]) if r["number"] is not None else None),
                "name": r["name"],
                "tag": r["tag"],
                "enabled": bool(r["enabled"]),
                "status": r["status"],
                "organization": r["organization"],
                "spoken_name": r["spoken_name"],
                "color": r["color"],
                "logo": r["logo"],
                "updated_at": r["updated_at"],
            })
        return out


@app.get("/admin/entrants/export.csv")
async def admin_export_entrants_csv():
    """
    Export all entrants as CSV for download.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT
              entrant_id, number, name, tag, enabled,
              status, organization, spoken_name, color,
              updated_at
            FROM entrants
            ORDER BY CAST(number AS INTEGER) NULLS LAST, name
        """)
        rows = await cur.fetchall()
        await cur.close()

        def gen():
            from datetime import datetime
            
            # Header row
            yield from csv_stream([[
                "entrant_id", "number", "name", "tag", "enabled",
                "status", "organization", "spoken_name", "color", "updated_at"
            ]])
            # Data rows
            for r in rows:
                # Format updated_at as ISO8601 if it's a Unix timestamp
                updated_at_str = ""
                if r["updated_at"]:
                    try:
                        # Assuming updated_at is stored as Unix timestamp (seconds)
                        dt = datetime.fromtimestamp(float(r["updated_at"]))
                        updated_at_str = dt.isoformat()
                    except (ValueError, TypeError):
                        # If it's already a string or invalid, use as-is
                        updated_at_str = str(r["updated_at"])
                
                yield from csv_stream([[
                    r["entrant_id"],
                    r["number"],
                    r["name"],
                    r["tag"] or "",
                    1 if r["enabled"] else 0,
                    r["status"] or "",
                    r["organization"] or "",
                    r["spoken_name"] or "",
                    r["color"] or "",
                    updated_at_str
                ]])

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"entrants_{timestamp}.csv"

        return StreamingResponse(
            gen(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )


@app.post("/admin/entrants")
async def admin_upsert_entrants(payload: Dict[str, Any]):
    """
    Create or update entrants in the authoritative DB.

    Rules:
      • Among ENABLED entrants only, 'tag' must be unique.
      • Atomic batch in a single transaction.
      • Returns 409 on conflict, 400 on bad payload, 200 on success.
    """
    entrants = payload.get("entrants")
    if not isinstance(entrants, list):
        raise HTTPException(status_code=400, detail="body must contain 'entrants' as a list")

    # 1) Validate/normalize
    entries: list[EntrantIn] = []
    for idx, item in enumerate(entrants):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"entrant at index {idx} must be an object")
        try:
            e = EntrantIn(**item)
        except ValidationError as ve:
            raise HTTPException(status_code=400, detail=f"invalid entrant at index {idx}: {ve.errors()!r}")
        entries.append(e)

    # 2) Transaction + uniqueness guard
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # App-level duplicate tag check (enabled & tag not null)
            with sqlite3.connect(DB_PATH) as sconn:
                for e in entries:
                    if e.enabled and e.tag:
                        if tag_conflicts(sconn, e.tag, incumbent_entrant_id=(None if e.is_create() else e.id)):
                            await db.execute("ROLLBACK")
                            raise HTTPException(
                                status_code=409,
                                detail=f"tag '{e.tag}' already assigned to another enabled entrant (while upserting id={e.id or 'new'})"
                            )

            created = 0
            updated = 0
            assigned_ids: list[dict] = []

            for i, e in enumerate(entries):
                tag_value = _norm_tag(e.tag)

                if e.is_create():
                    # -------- INSERT (let SQLite assign entrant_id) --------
                    cur = await db.execute(
                        """
                        INSERT INTO entrants
                          (number, name, tag, enabled, status, organization, spoken_name, color, updated_at)
                        VALUES
                          (?,      ?,    ?,   ?,       ?,      ?,            ?,           ?,     strftime('%s','now'))
                        """,
                        (
                            e.number,
                            e.name,
                            tag_value,
                            1 if e.enabled else 0,
                            e.status,
                            e.organization or "",
                            e.spoken_name or "",
                            e.color,
                        ),
                    )
                    new_id = cur.lastrowid
                    assigned_ids.append({"client_idx": i, "id": new_id})
                    await cur.close()
                    created += 1
                else:
                    # -------- UPSERT by PRIMARY KEY --------
                    await db.execute(
                        """
                        INSERT INTO entrants
                          (entrant_id, number, name, tag, enabled, status, organization, spoken_name, color, updated_at)
                        VALUES
                          (?,          ?,      ?,    ?,   ?,       ?,      ?,            ?,           ?,     strftime('%s','now'))
                        ON CONFLICT(entrant_id) DO UPDATE SET
                          number       = excluded.number,
                          name         = excluded.name,
                          tag          = excluded.tag,
                          enabled      = excluded.enabled,
                          status       = excluded.status,
                          organization = excluded.organization,
                          spoken_name  = excluded.spoken_name,
                          color        = excluded.color,
                          updated_at   = strftime('%s','now')
                        """,
                        (
                            e.id,
                            e.number,
                            e.name,
                            tag_value,
                            1 if e.enabled else 0,
                            e.status,
                            e.organization or "",
                            e.spoken_name or "",
                            e.color,
                        ),
                    )
                    updated += 1

            await db.commit()
            return {
                "ok": True,
                "count": created + updated,
                "created": created,
                "updated": updated,
                "assigned_ids": assigned_ids,
            }

        except HTTPException:
            raise
        except sqlite3.IntegrityError as ie:
            await db.execute("ROLLBACK")
            raise HTTPException(status_code=409, detail=f"uniqueness violation: {ie}")
        except Exception as ex:
            await db.execute("ROLLBACK")
            raise HTTPException(status_code=500, detail=f"admin upsert failed: {type(ex).__name__}: {ex}")



# ------------------------------------------------------------
# Delete Preflight does this entrant have data linked?
# ------------------------------------------------------------

@app.get("/admin/entrants/{entrant_id}/inuse")
async def entrant_inuse(entrant_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # 1) Identity (404 if missing)
        cur = await db.execute(
            "SELECT entrant_id AS id, number, name FROM entrants WHERE entrant_id=?",
            (entrant_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            raise HTTPException(status_code=404, detail="entrant not found")

        # 2) Safe counters (return 0 if tables/cols not found or any other error)
        async def _count(tbl: str, col: str, val: int) -> int:
            try:
                c = await db.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {col}=?", (val,))
                r = await c.fetchone()
                await c.close()
                return int(r[0] if r and r[0] is not None else 0)
            except Exception as ex:
                # Log to server console; never bubble to client
                print(f"[inuse] count failed for {tbl}.{col}={val}: {type(ex).__name__}: {ex}")
                return 0

        passes_cnt = await _count("passes", "entrant_id", entrant_id)
        laps_cnt   = await _count("lap_events", "entrant_id", entrant_id)

        return {
            "id": row["id"],
            "number": row["number"],
            "name": row["name"],
            "counts": { "passes": passes_cnt, "lap_events": laps_cnt }
        }

# ------------------------------------------------------------
# Delete (hard delete by id)
# ------------------------------------------------------------

@app.post("/admin/entrants/delete")
async def delete_entrant(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    # Accept either { id } or { ids: [...] }
    ids = payload.get("ids")
    if ids is None:
        one = payload.get("id")
        if one is None:
            raise HTTPException(status_code=400, detail="missing 'id' (or 'ids')")
        ids = [one]

    # Coerce to ints and dedupe
    try:
        ids = [int(x) for x in ids]
    except Exception:
        raise HTTPException(status_code=400, detail="ids must be integers")

    if not ids:
        raise HTTPException(status_code=400, detail="no ids provided")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            deleted = []
            for eid in ids:
                await db.execute("DELETE FROM entrants WHERE entrant_id=?", (eid,))
                ch = await (await db.execute("SELECT changes()")).fetchone()
                if ch and ch[0] > 0:
                    deleted.append(eid)
            await db.commit()
        except Exception as ex:
            await db.execute("ROLLBACK")
            raise HTTPException(status_code=500, detail=f"delete failed: {type(ex).__name__}: {ex}")

    if not deleted:
        raise HTTPException(status_code=404, detail="entrant not found")

    return {"deleted": deleted}


# ============================================================
# SETTINGS - Read/Write config.yaml
# ============================================================

@app.get("/settings/config")
async def get_settings_config():
    """
    Return the current config.yaml contents for the settings UI.
    Returns sanitized config object safe for editing.
    """
    try:
        # Read the config file directly (config_loader already loaded it)
        config_path = Path(__file__).parent.parent / "config" / "config.yaml"
        
        if not config_path.exists():
            raise HTTPException(status_code=500, detail="config.yaml not found")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        
        if not config_data:
            raise HTTPException(status_code=500, detail="config.yaml is empty or invalid")
        
        return JSONResponse(config_data)
    
    except yaml.YAMLError as e:
        raise HTTPException(status_code=500, detail=f"YAML parse error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read config: {e}")


@app.post("/settings/config")
async def update_settings_config(request: Request):
    """
    Update config.yaml with provided changes.
    Accepts partial config object and merges with existing config.
    Returns success/error response.
    
    NOTE: Changes require server restart to take effect.
    """
    try:
        # Parse the incoming patch
        patch = await request.json()
        
        if not patch or not isinstance(patch, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")
        
        # Read current config
        config_path = Path(__file__).parent.parent / "config" / "config.yaml"
        
        if not config_path.exists():
            raise HTTPException(status_code=500, detail="config.yaml not found")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        
        if not config_data:
            config_data = {}
        
        # Deep merge the patch into existing config
        def deep_merge(base: dict, overlay: dict):
            """Recursively merge overlay into base."""
            for key, value in overlay.items():
                if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                    deep_merge(base[key], value)
                else:
                    base[key] = value
        
        deep_merge(config_data, patch)
        
        # Write back to file with proper formatting
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(
                config_data,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
                width=80,
                indent=2
            )
        
        return {
            "ok": True,
            "message": "Settings saved. Restart server for changes to take effect.",
            "restart_required": True
        }
    
    except yaml.YAMLError as e:
        raise HTTPException(status_code=500, detail=f"YAML error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update config: {e}")


# ------------------------------------------------------------
# Polling endpoint (UI fallback)
# ------------------------------------------------------------


@app.get("/sensors/peek")
async def sensors_peek():
    """Return the last observed tag. UI de-duplicates via seen_at."""
    return JSONResponse(last_tag)



# ------------------------------------------------------------
# SSE endpoint (preferred by UI)
# ------------------------------------------------------------

@app.get("/sensors/stream")
async def sensors_stream(request: Request):
    """Send exactly one tag event and then end (UI opens per-scan)."""
    q: asyncio.Queue[str] = asyncio.Queue()
    _listeners.append(q)

    async def gen():
        try:
            # If client disconnects or cancels scan, this raises
            tag = await asyncio.wait_for(q.get(), timeout=10.0)
            if str(tag) == "__shutdown__":
                return  # exit immediately on shutdown          
            payload = json.dumps({"tag": str(tag)})
            yield f"event: tag\ndata: {payload}\n\n"
        except asyncio.TimeoutError:
            # No tag within window → end quietly (UI shows timeout)
            return
        finally:
            if q in _listeners:
                _listeners.remove(q)

    # Note: the UI preflights this; returning stream only if used
    return StreamingResponse(gen(), media_type="text/event-stream")



# External publishers via CURL
# ------------------------------------------------
# Ingest tag into engine + publish to simple bus
# -------------------------------------------------

@app.post("/engine/pass")
async def engine_pass(payload: Dict[str, Any]):
    """
    Ingest a single pass into the engine and publish to Diagnostics.

    Accepted fields:
      - tag: string (preferred)
      - entrant_id: int (optional; if provided and tag missing, we resolve tag)
      - source: "track" | "pit_in" | "pit_out" (default "track")
      - device_id: optional device identifier (for pit routing)
    """
    tag = (payload.get("tag") or None)
    entrant_id = payload.get("entrant_id")
    source = str(payload.get("source") or "track").lower()
    device_id = payload.get("device_id")

    # If tag missing but entrant_id present, try to resolve the entrant's tag.
    if not tag and entrant_id is not None:
        tag = _tag_for_entrant_id(entrant_id)

    if not tag:
        raise HTTPException(status_code=400, detail="missing 'tag' and unable to resolve from 'entrant_id'")

    tag = str(tag).strip()

    # 1) Keep the simple bus alive for peek/stream consumers
    publish_tag(tag)

    # 2) Forward into the engine so laps/logic actually happen
    try:
        snap = ENGINE.ingest_pass(tag=tag, source=source, device_id=device_id)
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"ingest failed: {type(ex).__name__}: {ex}")

    # 3) Emit a Diagnostics row, labeled if we can resolve the tag to an ENABLED entrant
    try:
        ent = await resolve_tag_to_entrant(tag)  # uses the DB
        await diag_publish({
            "tag_id": tag,
            "entrant": ({"name": ent["name"], "number": ent["number"]} if ent else None),
            "source": ("Start/Finish" if source in ("sf", "track") else source),
            "rssi": -60 - random.randint(0, 15),
            "time": None,
        })
    except Exception:
        # Never let diagnostics issues break ingest
        pass

    return JSONResponse(snap)



# ------------------------------------------------------------
# Neutral sensor ingest endpoint
# ------------------------------------------------------------


class SensorPassIn(BaseModel):
    tag: str
    source: Optional[str] = "track"    # e.g. "track", "pit_in", "pit_out"
    device_id: Optional[str] = None    # optional: hardware id

# ------------------------------------------------------------
# Sensors inject: single pass from any decoder/bridge
# ------------------------------------------------------------
@app.post("/sensors/inject")
async def sensors_inject(payload: dict):
    """
    Neutral ingest from any scanner bridge (HTTP).
    - Fan-out to the one-shot scan bus (Entrants scan UI expects this).
    - Forward to the RaceEngine (so laps/logic happen).
    - Publish a diagnostics row to the continuous SSE feed.
    """
    _mark_scanner_heartbeat({"via": "inject"})

    tag = str(payload.get("tag") or "").strip()
    source = str(payload.get("source") or "track").strip()   # "track"|"pit_in"|"pit_out"|etc.
    device_id = payload.get("device_id")
    if not tag:
        raise HTTPException(status_code=400, detail="Missing 'tag'")

    # 0) One-shot scan bus: wake any /sensors/stream listeners and update /sensors/peek
    #    (This mirrors what /engine/pass already does.)
    try:
        publish_tag(tag)  # updates last_tag and pushes to _listeners queues
    except Exception:
        pass

    # 1) Always record 'seen' (used by the pre-race "Seen" table)
    _note_seen(tag)

    # 2) Forward into the engine so scoring/state advance
    accepted, err = None, None
    try:
        ingest = getattr(ENGINE, "ingest_pass", None)
        if callable(ingest):
            accepted = bool(ingest(tag=tag, source=source, device_id=device_id))
        else:
            err = "ENGINE.ingest_pass not found"
    except Exception as ex:
        err = f"{type(ex).__name__}: {ex}"

    # 3) Diagnostics feed (continuous SSE → diag page)
    try:
        ent = await resolve_tag_to_entrant(tag)  # optional nice-to-have label
        await diag_publish({
            "tag_id": tag,
            "entrant": ({"name": ent["name"], "number": ent["number"]} if ent else None),
            "source": ("Start/Finish" if source in ("sf", "track") else source),
            "rssi": -60,  # placeholder; real RSSI not available via HTTP bridge
        })
    except Exception:
        # never let diagnostics publication break ingest
        pass

    running = _engine_running()

    log.info(
        "[INJECT] tag=%s phase=%s accepted=%s running=%s err=%s",
        tag,
        _RACE_STATE.get("phase"),
        accepted,
        running,
        err or "-",
    )

    return {
        "ok": err is None,
        "accepted": accepted,
        "phase": _RACE_STATE.get("phase"),
        "flag": _RACE_STATE.get("flag"),
        "race_id": _RACE_STATE.get("race_id"),
        "engine_running": running,
        "error": err,
    }


@app.post("/sensors/meta")
async def sensors_meta(req: Request):
    """
    External scanner heartbeat.
    The logger should POST a small JSON blob like:
      {
        "source": "ilap.serial",  # or "ambrc.serial", etc.
        "port":   "COM3",
        "baud":   9600,
        "device_id": "ilap-210",
        "host":   "race_control"
      }
    We treat *presence* of this call (or any /sensors/inject) as liveness.
    """
    try:
        payload = await req.json()
    except Exception:
        payload = {}

    _mark_scanner_heartbeat(payload if isinstance(payload, dict) else None)
    return JSONResponse({"ok": True, "t": _SCANNER_STATUS["last_heartbeat"]})

# --- Scanner lifecycle -------------------------------------------------------
# Keep a small registry of background tasks we start (scanner, SSE pingers, etc.)
_SCANNER_TASKS: set[asyncio.Task] = set()


def _should_start_inprocess_scanner(cfg: dict) -> bool:
    """
    Only allow in-process serial if publisher.mode == 'inprocess'.
    External logger path (publisher.mode=http) must NOT open the port here.
    """
    try:
        return (cfg.get("publisher", {}).get("mode") or "").strip().lower() == "inprocess"
    except Exception:
        return False


def _track_task(t: asyncio.Task) -> None:
    """Remember a task and auto-untrack when it finishes."""
    _SCANNER_TASKS.add(t)
    t.add_done_callback(lambda tt: _SCANNER_TASKS.discard(tt))


@app.on_event("startup")
async def announce_db_path() -> None:
    """Emit the configured database path once logging is fully initialized."""
    try:
        resolved = Path(get_db_path()).resolve()
        message = f"db_path={resolved}"
        log.info(message)
        logging.getLogger("uvicorn.error").info(message)
    except Exception:
        log.exception("Unable to report db_path during startup")

@app.on_event("startup")
async def start_scanner():
    """
    Start the lap logger/decoder reader if enabled in config.
    (No-op if you start it elsewhere.)
    """

    try:
        scan_cfg = get_scanner_cfg() or {}
        source = str(scan_cfg.get("source", "mock")).lower()
        allow_inprocess = _should_start_inprocess_scanner(CONFIG)

        if allow_inprocess:
            log.info("In-process scanner ENABLED (publisher.mode=inprocess)")
        else:
            log.info(
                "In-process scanner DISABLED (publisher.mode!=inprocess); expecting external logger heartbeats at /sensors/meta"
            )

        if source != "mock" and not allow_inprocess:
            return

        if source != "mock":
            try:
                from backend import config_loader as _config_loader
                from backend.lap_logger import ScannerService

                cfg_path = "./config/config.yaml"
                cfg_dict = _config_loader.load_config(cfg_path)

                # Ensure the lap logger uses the same config view we just loaded.
                _config_loader.CONFIG = cfg_dict

                publisher_cfg = cfg_dict.setdefault("publisher", {})
                if str(publisher_cfg.get("mode", "http")).lower() != "inprocess":
                    publisher_cfg["mode"] = "http"
                    http_cfg = publisher_cfg.setdefault("http", {})
                    http_cfg.setdefault("base_url", "http://127.0.0.1:8000")
                    http_cfg.setdefault("timeout_ms", 500)

                stop_evt = asyncio.Event()
                task = asyncio.get_running_loop().create_task(
                    ScannerService(cfg_dict).run(stop_evt),
                    name="scanner_service",
                )
                _track_task(task)
                app.state.stop_evt = stop_evt
                app.state.task = task
                log.info("ScannerService started (source=%s).", source)
                return
            except Exception:
                log.exception("ScannerService not available; falling back if mock is requested.")

        if source == "mock":
            log.info("Starting in-process MOCK tag generator.")

            async def _mock_task(stop_evt: asyncio.Event):
                tags = ["1234567", "2345678", "3456789", "4567890"]
                while not stop_evt.is_set():
                    tag = random.choice(tags)
                    publish_tag(tag)
                    try:
                        ENGINE.ingest_pass(tag=str(tag), source="track")
                    except Exception:
                        log.exception("Mock ingest failed for tag %s", tag)

                    try:
                        ent = await resolve_tag_to_entrant(tag)
                        await diag_publish({
                            "tag_id": str(tag),
                            "entrant": ({"name": ent["name"], "number": ent["number"]} if ent else None),
                            "source": "Start/Finish",
                            "rssi": -60 - random.randint(0, 15),
                            "time": None,
                        })
                    except Exception:
                        pass

                    await asyncio.sleep(1.5)

            stop_evt = asyncio.Event()
            task = asyncio.get_running_loop().create_task(_mock_task(stop_evt), name="mock_scanner")
            _track_task(task)
            app.state.stop_evt = stop_evt
            app.state.task = task
        else:
            log.info("No ScannerService and not in mock mode; scanner startup skipped.")
    except Exception:
        log.exception("Failed to start scanner")

@app.on_event("startup")
async def start_osc_lighting():
    """
    Initialize OSC lighting integration for QLC+ control.
    
    Sets up bidirectional OSC communication:
    - OUT: CCRS → QLC+ (sends flag cues when race flags change)
    - IN: QLC+ → CCRS (receives feedback when QLC+ buttons are clicked)
    
    Configuration is loaded from config.yaml -> integrations.lighting
    """
    global _osc_out, _osc_in
    
    try:
        # Extract lighting config from main config
        lighting_cfg = ((CONFIG.get("integrations") or {}).get("lighting") or {})
        
        if not lighting_cfg:
            log.info("OSC lighting integration disabled (no config)")
            return
        
        # -------------------------------------------------------------------------
        # OSC OUT: CCRS → QLC+ (send flag cues to lights)
        # -------------------------------------------------------------------------
        osc_out_cfg = lighting_cfg.get("osc_out")
        if osc_out_cfg and osc_out_cfg.get("enabled"):
            _osc_out = OscLightingOut(osc_out_cfg)
            _osc_out.start()
            log.info(
                "OSC OUT enabled: %s:%s (flags → lighting)",
                _osc_out.host,
                _osc_out.port,
            )
        else:
            log.info("OSC OUT disabled")
        
        # -------------------------------------------------------------------------
        # OSC IN: QLC+ → CCRS (receive flag button feedback from lights)
        # -------------------------------------------------------------------------
        in_raw = lighting_cfg.get("osc_in") or {}
        if in_raw.get("enabled"):
            # Build config from YAML
            osc_in_cfg = OscInConfig(
                host=in_raw.get("host", "0.0.0.0"),
                port=int(in_raw.get("port", 9010)),
                flag_prefix=((in_raw.get("paths") or {}).get("flag_prefix", "/ccrs/flag/")),
                path_blackout=((in_raw.get("paths") or {}).get("blackout", "/ccrs/blackout")),
                threshold_on=float(in_raw.get("threshold_on", 0.5)),
                debounce_off_ms=int(in_raw.get("debounce_off_ms", 250)),
            )
            
            # Callback: QLC+ button clicked → update CCRS flag
            def _osc_on_flag(name: str):
                """Thread-safe callback when QLC+ sends a flag button press."""
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(_handle_flag_from_qlc, name)
            
            # Callback: QLC+ blackout button clicked → update CCRS blackout state
            def _osc_on_blackout(enabled: bool):
                """Thread-safe callback when QLC+ sends blackout state change."""
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(_handle_blackout_from_qlc, enabled)
            
            _osc_in = OscInbound(
                cfg=osc_in_cfg,
                on_flag=_osc_on_flag,
                on_blackout=_osc_on_blackout,
                on_any=None,  # Could log all OSC messages for debugging
            )
            _osc_in.start()
            log.info(
                "OSC IN enabled: %s:%s (lighting → flags)",
                osc_in_cfg.host,
                osc_in_cfg.port,
            )
        else:
            log.info("OSC IN disabled")
    
    except Exception:
        log.exception("Failed to initialize OSC lighting integration")

@app.on_event("shutdown")
async def stop_scanner():
    """
    Cancel all background tasks and *absorb* CancelledError, so Starlette doesn't
    treat a clean cancellation as an unhandled error.
    """
    tasks = list(_SCANNER_TASKS)
    if not tasks:
        log.info("No scanner/background tasks to stop.")
        return

    log.info("Stopping %d background task(s)...", len(tasks))
    for t in tasks:
        t.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, asyncio.CancelledError):
            continue
        if isinstance(res, Exception):
            log.warning("Background task ended with exception during shutdown: %r", res)

    _SCANNER_TASKS.clear()
    log.info("All background tasks stopped cleanly.")

@app.on_event("shutdown")
async def stop_osc_lighting():
    """
    Cleanup OSC lighting integration on server shutdown.
    
    Stops both inbound and outbound OSC connections gracefully.
    """
    global _osc_out, _osc_in
    
    try:
        if _osc_in:
            _osc_in.stop()
            log.info("OSC IN stopped")
        if _osc_out:
            _osc_out.stop()
            log.info("OSC OUT stopped")
    except Exception:
        log.exception("Error stopping OSC lighting integration")
    finally:
        _osc_in = None
        _osc_out = None

# ------------------------------------------------------------
# Diagnostics / Live Sensors - SSE stream for diag.html
# ------------------------------------------------------------

    
# In-memory pub/sub state
DIAGNOSTICS_ENABLED: bool = True
DIAGNOSTICS_BUFFER_SIZE: int = 500
_diag_ring = deque(maxlen=DIAGNOSTICS_BUFFER_SIZE)
_diag_subs: set[asyncio.Queue] = set()
_diag_lock = asyncio.Lock()

async def diag_publish(evt: dict) -> None:
    """Publish a detection event to all diagnostics subscribers."""
    if not DIAGNOSTICS_ENABLED:
        return
    if "time" not in evt:
        evt = dict(evt)
        evt["time"] = datetime.datetime.now(datetime.timezone.utc)\
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")
    _diag_ring.append(evt)
    async with _diag_lock:
        dead = []
        for q in _diag_subs:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    dead.append(q)
        for q in dead:
            _diag_subs.discard(q)

@app.get("/diagnostics/stream")
async def diagnostics_stream(request: Request):
    """EventSource stream for diagnostics/live sensors."""
    if not DIAGNOSTICS_ENABLED:
        async def disabled_gen():
            yield b'data: {"type":"status","message":"diagnostics_disabled"}\n\n'
        return StreamingResponse(disabled_gen(), media_type="text/event-stream")

    q = asyncio.Queue(maxsize=1024)
    async with _diag_lock:
        _diag_subs.add(q)

    async def gen():
        try:
            for evt in list(_diag_ring):
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(evt, separators=(',',':'))}\n\n".encode()
            while not await request.is_disconnected():
                evt = await q.get()
                # break cleanly if shutdown was signaled
                if isinstance(evt, dict) and evt.get("type") == "shutdown":
                    break
                yield f"data: {json.dumps(evt, separators=(',',':'))}\n\n".encode()
        finally:
            async with _diag_lock:
                _diag_subs.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

@app.post("/diagnostics/test_fire")
async def diagnostics_test_fire():
    """Inject a dummy detection for testing the diag.html frontend."""
    evt = {
        "tag_id": "1234567",
        "entrant": {"name": "Thunder Lizards", "number": "42"},
        "source": "Start/Finish",
        "rssi": -63,
    }
    await diag_publish(evt)
    return {"ok": True, "sent": evt}

# Minimal runtime stub for frontends (Settings/Diagnostics)
@app.get("/setup/runtime")
async def setup_runtime():
    # Include current race info if available
    race_id = ENGINE.race_id if hasattr(ENGINE, 'race_id') else None
    race_type = ENGINE.race_type if hasattr(ENGINE, 'race_type') else None
    
    return {
        "heat_id": race_id,  # Frontend uses this for brake test storage
        "session_type": race_type,  # "qualifying", "sprint", "endurance"
        "engine": {
            "ingest": {"debounce_ms": 250},
            "diagnostics": {
                "enabled": True,
                "buffer_size": 500,
                "stream": {"transport": "sse"},
                "beep": {"max_per_sec": 5},
            },
        },
        "race": {
            "flags": {"inference_blocklist": ["YELLOW", "RED", "SC"], "post_green_grace_ms": 3000},
            "missed_lap": {"enabled": False, "apply_mode": "propose", "window_laps": 5, "sigma_k": 2.0,
                        "min_gap_ms": 8000, "max_consecutive_inferred": 1, "mark_inferred": True},
        },
        "track": {"locations": {"SF": "Start/Finish", "PIT_IN": "Pit In", "PIT_OUT": "Pit Out"}, "bindings": []},
        "ui": {"operator": {"sound_default_enabled": True, "time_display": "local"}},
        "meta": {"engine_host": "127.0.0.1:8000"},
    }


# Race Runtime config endpoint for UI
@app.get("/race/runtime")
def race_runtime():
    if not _CURRENT_SESSION:
        raise HTTPException(status_code=404, detail="no active session")
    return _derive_for_control(_CURRENT_SESSION)

    
# ------------------------------------------------------------
# Probes
# ------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    """
    Lightweight liveness probe. Returns 200 if the app is up and able to serve.
    Does not touch the database.
    """
    return {"status": "ok", "service": "ccrs-backend"}

@app.get("/readyz")
async def readyz():
    """
    Readiness probe. Verifies DB is reachable and schema is present.
    Returns 200 with basic info if good; 503 if DB check fails.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # succeeds only if 'entrants' exists
            await db.execute("SELECT 1 FROM entrants LIMIT 1")
        return {"status": "ok", "db_path": str(DB_PATH)}
    except Exception as e:
        return Response(
            content='{"status":"degraded","error":"%s"}' % type(e).__name__,
            media_type="application/json",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

