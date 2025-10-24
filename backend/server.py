from __future__ import annotations

"""
ChronoCore RS — backend/server.py (drop-in)
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
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, cast
import yaml
from enum import Enum

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, Response, status, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

## Our Race engine data, config, and db settings##
from .race_engine import ENGINE  
from .db_schema import ensure_schema, tag_conflicts
from .config_loader import get_db_path, get_scanner_cfg, CONFIG


# log = logging.getLogger("uvicorn.error")
# log = logging.getLogger("race")
log = logging.getLogger("ccrs")


# Resolve DB path from config and ensure schema on boot.
DB_PATH = get_db_path()
ensure_schema(DB_PATH, recreate=False, include_passes=True)

# ------------------------------------------------------------
# FastAPI app bootstrap
# ------------------------------------------------------------
app = FastAPI(title="CCRS Backend", version="0.2.1")

# ------------------------------------------------------------
# Simple scan bus state
# ------------------------------------------------------------
last_tag: dict[str, object] = {"tag": None, "seen_at": None}
_listeners: list[asyncio.Queue[str]] = []   # SSE subscribers

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

# ---- Sound assets: user overrides then built-in defaults ----
SOUNDS_ASSETS_DIR = PROJECT_ROOT / "assets" / "sounds"
SOUNDS_CONFIG_DIR = PROJECT_ROOT / "config" / "sounds"
SOUNDS_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
SOUNDS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/assets/sounds", StaticFiles(directory=SOUNDS_ASSETS_DIR, html=False), name="assets_sounds")
app.mount("/config/sounds", StaticFiles(directory=SOUNDS_CONFIG_DIR, html=False), name="config_sounds")


log.info("Serving UI from: %s", STATIC_DIR)  # sanity print at startup





# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
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

# --- Live "seen" counters (pre-race diagnostics) ----------------------------
_SEEN_COUNTS: dict[int, int] = {}   # entrant_id -> read count (live, not persisted)
_SEEN_TOTAL: int = 0                # total distinct entrants seen at least once

    
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
# Endpoints — Race Setup save + Race Control fetch
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

        _CURRENT_ENTRANTS_ENGINE = entrants_engine[:]  # keep for reset

        # Make entrants available to Seen block (and anything else that needs roster)
        _RACE_STATE["entrants"] = entrants_engine[:]

        # Build tag -> entrant map and reset live counters for a fresh session
        _TAG_TO_ENTRANT.clear()
        for e in entrants_engine:  # <- use the engine-ready list we just built
            tag = str(e.get("tag") or "").strip()
            if tag:
                _TAG_TO_ENTRANT[tag] = e
        _SEEN_COUNTS.clear()
        _SEEN_TOTAL = 0


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
            entrants=entrants_engine,
            race_type=race_type,
            session_config=_CURRENT_SESSION,
        )

         # Initialize Seen
        #global _SEEN_COUNTS, _SEEN_TOTAL /// already declared above
        _SEEN_COUNTS = {}
        _SEEN_TOTAL  = sum(1 for e in getattr(ENGINE, "entrants", {}).values() if getattr(e, "enabled", False))

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
def decoders_status_stub():
    # Replace with real decoder detection when available.
    return {"online": 0}

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
# Endpoints — flag / state / reset (lightweight, engine-aware)
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

    # 5) Prefer engine snapshot; augment with local phase/flag/clock for UI
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
    Hybrid snapshot:
      - Use ENGINE.snapshot() when available.
      - PRE/COUNTDOWN: drive time from local _state_clock_block and mirror to top-level.
      - GREEN/WHITE/CHECKERED: never overwrite engine clock; only mirror engine's clock.clock_ms to top-level if absent.
      - Fill missing phase/flag/limit/countdown_from_s via setdefault.
      - Keep local _RACE_STATE in sync with engine flags EXCEPT during COUNTDOWN.
      - Always include 'seen'.
    """
    if hasattr(ENGINE, "snapshot"):
        try:
            snap = ENGINE.snapshot() or {}
            if isinstance(snap, dict):
            # 1) Sync local mirror from engine-driven flags FIRST,
            #    but DO NOT clobber local COUNTDOWN (engine doesn't know about it).
                local_phase_now = str(_RACE_STATE.get("phase") or "").lower()
                if local_phase_now != "countdown":
                    try:
                        f = str(snap.get("flag") or "").upper()
                        if f in ("WHITE", "CHECKERED", "GREEN", "PRE", "YELLOW", "RED", "BLUE"):
                            _RACE_STATE["flag"] = f
                            # Only map flags that have explicit Phase values; leave others as-is
                            phase_map = {
                                "WHITE":     Phase.WHITE.value,
                                "CHECKERED": Phase.CHECKERED.value,
                                "GREEN":     Phase.GREEN.value,
                                "PRE":       Phase.PRE.value,
                            }
                            _RACE_STATE["phase"] = phase_map.get(f, _RACE_STATE["phase"])
                    except Exception:
                        pass
                # 2) Fill gaps without stomping engine values
                snap.setdefault("phase", _RACE_STATE["phase"])
                snap.setdefault("flag",  _RACE_STATE["flag"])
                snap.setdefault("limit", _RACE_STATE["limit"])
                snap.setdefault("countdown_from_s", _RACE_STATE["countdown_from_s"])

                # 3) Decide overlay by phase AFTER sync
                phase_lower = str(snap.get("phase") or _RACE_STATE["phase"] or "").lower()

                if phase_lower in ("pre", "countdown"):
                    # Local countdown/preview is authoritative for UI
                    cb = _state_clock_block()
                    snap["clock"] = cb
                    snap["clock_ms"] = cb.get("clock_ms")
                    snap["countdown_remaining_s"] = cb.get("countdown_remaining_s")
                else:
                    # Racing / finish phases → engine owns time.
                    # If engine provided a clock block but no top-level clock_ms, mirror it for legacy UI.
                    if "clock" in snap and isinstance(snap["clock"], dict) and "clock_ms" not in snap:
                        cm = snap["clock"].get("clock_ms")
                        if isinstance(cm, (int, float)):
                            snap["clock_ms"] = cm
                    # IMPORTANT: Do NOT write our local clock here; preserves freeze at CHECKERED.

                # 4) running: prefer engine; else infer (green/white = running)
                snap.setdefault("running", phase_lower in ("green", "white"))

                # 5) Live 'seen' block for operator UI
                snap["seen"] = _state_seen_block()

            return JSONResponse(snap)
        except Exception:
            pass  # fall through to local scaffold if engine snapshot fails

    # Fallback when no engine snapshot available
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
    _RACE_STATE["frozen_at"] = time.time()   # <— add this line

    if hasattr(ENGINE, "set_flag"):
        try:
            ENGINE.set_flag("CHECKERED")
        except Exception:
            pass
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

    # Best-effort engine reset
    reset_fn = getattr(ENGINE, "reset_session", None)
    if callable(reset_fn):
        try:
            reset_fn()   # should stop running and zero the clock
        except Exception:
            pass
    else:
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

    # Optional: clear in-memory seen counters if you keep them
    try:
        _SEEN_COUNTS = {}
        # Recompute total from the current (kept) roster
        _SEEN_TOTAL  = sum(1 for e in getattr(ENGINE, "entrants", {}).values() if getattr(e, "enabled", False))
        return {"ok": True, "phase": _RACE_STATE["phase"]}
    except Exception:
        pass

    return {"ok": True, "phase": _RACE_STATE["phase"]}


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
    race_type = (_CURRENT_SESSION.get("mode_id", "sprint") if _CURRENT_SESSION else "sprint")
    snap = ENGINE.load(
        race_id=int(_CURRENT_RACE_ID or 0),
        entrants=_CURRENT_ENTRANTS_ENGINE or [],
        race_type=str(race_type),
        session_config=_CURRENT_SESSION,
    )

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



# ======================= DELETE (hard delete by id) =======================

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



# ===============================
# Neutral sensor ingest endpoint
# ===============================


class SensorPassIn(BaseModel):
    tag: str
    source: Optional[str] = "track"    # e.g. "track", "pit_in", "pit_out"
    device_id: Optional[str] = None    # optional: hardware id

# --- Sensors inject: single pass from any decoder/bridge ---------------------
@app.post("/sensors/inject")
async def sensors_inject(payload: dict):
    tag = str(payload.get("tag") or "").strip()
    source = str(payload.get("source") or "ui").strip()
    if not tag:
        raise HTTPException(status_code=400, detail="Missing 'tag'")

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

    # Always record 'seen' (even if lap is later rejected by min-lap, etc.)
    _note_seen(tag)

    accepted, err = None, None
    try:
        ingest = getattr(ENGINE, "ingest_pass", None)
        if callable(ingest):
            # Most engines expect (tag, source) OR keyword args; use kwargs for clarity
            accepted = bool(ingest(tag=tag, source=source))
        else:
            err = "ENGINE.ingest_pass not found"
    except Exception as ex:
        err = f"{type(ex).__name__}: {ex}"

    log.info(
        "[INJECT] tag=%s phase=%s accepted=%s running=%s err=%s",
        tag, _RACE_STATE.get("phase"), accepted, _engine_running(), err or "-"
    )

    return {
        "ok": err is None,
        "accepted": accepted,
        "phase": _RACE_STATE.get("phase"),
        "flag": _RACE_STATE.get("flag"),
        "engine_running": _engine_running(),
        "race_id": _RACE_STATE.get("race_id"),
        "error": err,
    }



# ------------------------------------------------------------
# --- Embedded scanner startup: publish via HTTP to our own FastAPI ---
# ------------------------------------------------------------
@app.on_event("startup")
async def start_scanner():
    """
    Try to start the real ScannerService (serial/udp). If not available or
    if scanner.source == 'mock', run an in-process generator that:
      • publishes tags to the simple bus (peek/stream),
      • forwards them into the engine (_send_pass_to_engine),
      • emits Diagnostics events with entrant names when tags are known.
    """
    import asyncio, random  # local imports to avoid surprises during import time

    scan_cfg = get_scanner_cfg() or {}
    source = str(scan_cfg.get("source", "mock")).lower()

    # Attempt the real scanner for non-mock sources
    if source != "mock":
        try:
            from backend.lap_logger import ScannerService, load_config as ll_load

            # Build a config object the lap_logger expects (your existing file path logic)
            cfg_path = "./config/config.yaml"
            cfg = ll_load(cfg_path)

            # Force HTTP publishing into this server so we see /sensors/inject if needed
            cfg.publisher.mode = "http"
            # Set any commonly used fields; harmless if some don’t exist
            try:
                cfg.publisher.http.base_url = "http://127.0.0.1:8000"
                cfg.publisher.http.timeout_ms = 500
            except Exception:
                pass

            app.state.stop_evt = asyncio.Event()
            app.state.task = asyncio.get_running_loop().create_task(ScannerService(cfg).run(app.state.stop_evt))
            log.info("ScannerService started (source=%s).", source)
            return
        except Exception:
            log.exception("ScannerService not available; falling back if mock is requested.")

    # Fallback: lightweight mock generator
    if source == "mock":
        log.info("Starting in-process MOCK tag generator.")
        async def _mock_task(stop_evt: asyncio.Event):
            tags = ["1234567", "2345678", "3456789", "4567890"]
            while not stop_evt.is_set():
                tag = random.choice(tags)

                # Publish for /sensors/peek and /sensors/stream consumers
                publish_tag(tag)

                # Ingest using the new, neutral shape (no ilap-isms)
                try:
                    # Direct call into the engine so laps/logic actually happen
                    snap = ENGINE.ingest_pass(tag=str(tag), source="track")
                except Exception:
                    log.exception("Mock ingest failed for tag %s", tag)
                    snap = None

                # Diagnostics row, resolved from DB if possible
                try:
                    ent = await resolve_tag_to_entrant(tag)  # you added this earlier
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


        app.state.stop_evt = asyncio.Event()
        app.state.task = asyncio.get_running_loop().create_task(_mock_task(app.state.stop_evt))
    else:
        log.info("No ScannerService and not in mock mode; scanner startup skipped.")


## ----------------------------------------------------------
# --- Embedded scanner shutdown: make deterministic/clean ---   
#------------------------------------------------------------

@app.on_event("shutdown")
async def stop_scanner():
    """
    Make shutdown deterministic:
    - cancel countdown
    - wake SSE/stream listeners
    - stop scanner/mock task
    - aggressively cancel any other pending asyncio tasks
    """
    import asyncio
    import contextlib

    # 1) Cancel countdown task (prevents a sleeping asyncio.sleep from pinning shutdown)
    global _COUNTDOWN_TASK
    _cancel_task(_COUNTDOWN_TASK)
    _COUNTDOWN_TASK = None

    # 2) Wake /sensors/stream listeners (so their generators exit immediately)
    #    Assumes you keep a list/set of Queues called `_listeners`
    with contextlib.suppress(Exception):
        for q in list(_listeners):
            q.put_nowait("__shutdown__")

    # 3) Wake /diagnostics/stream subscribers (so they break out cleanly)
    #    Assumes you track `_diag_subs` (set of Queues) and guard with `_diag_lock`
    try:
        async with _diag_lock:
            for q in list(_diag_subs):
                with contextlib.suppress(Exception):
                    q.put_nowait({"type": "shutdown"})
    except Exception:
        pass

    # 4) Stop the scanner/mock task if you keep one in app.state
    #    (Cancel + await so it has a chance to clean up)
    task = getattr(app.state, "task", None)
    if task:
        with contextlib.suppress(Exception):
            task.cancel()
            await task

    # 5) Aggressively cancel ANY remaining asyncio tasks in this loop (except this one)
    #    This handles any stragglers (background pollers, forgotten tasks, etc.)
    loop = asyncio.get_running_loop()
    pending = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task(loop)]
    for t in pending:
        t.cancel()
    # Give them a moment to handle CancelledError
    with contextlib.suppress(Exception):
        await asyncio.gather(*pending, return_exceptions=True)



# ==============================================================================
# Diagnostics / Live Sensors — SSE stream for diag.html
# ==============================================================================

    
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
    return {
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
