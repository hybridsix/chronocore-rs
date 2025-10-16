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
import sqlite3
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, cast
import yaml

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

## Our Race engine data, config, and db settings##
from .race_engine import ENGINE  
from .db_schema import ensure_schema, tag_conflicts
from .config_loader import get_db_path


log = logging.getLogger("uvicorn.error")

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

# -----------------------------------------------------------------------------
# Session handoff (Race Setup → Race Control)
# -----------------------------------------------------------------------------
_CURRENT_SESSION: Dict[str, Any] = {}         # last saved session_config from /race/setup
_CURRENT_ENTRANTS_ENGINE: List[Dict[str, Any]] = []  # last entrants mapped to ENGINE.load()

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



# Resolve DB path from config and ensure schema on boot.
DB_PATH = get_db_path()
ensure_schema(DB_PATH, recreate=False, include_passes=True)

# ------------------------------------------------------------
# Minimal "Engine" adapter used by these endpoints
# ------------------------------------------------------------
#class _Engine:
#    """
#    Very small in-memory session mirror the operator UI interacts with.
#    Your real runtime engine can replace this; keep method names the same.
#    """
#   def __init__(self) -> None:
#        self._entrants: Dict[int, Dict[str, Any]] = {}
##
#    def load(self, entrants: List[Dict[str, Any]]) -> Dict[str, Any]:
#        # Store by id for constant-time lookups
#        self._entrants = {e["id"]: e for e in entrants}
#        return {"ok": True, "entrants": list(self._entrants.keys())}

#    def assign_tag(self, entrant_id: int, tag: Optional[str]) -> Dict[str, Any]:
#        if entrant_id not in self._entrants:
#            # We deliberately raise to let the API return 412 Precondition Failed
#            raise KeyError("entrant not in active session")
#        self._entrants[entrant_id]["tag"] = tag
#        return {"ok": True, "entrant_id": entrant_id, "tag": tag}
#
#ENGINE = _Engine()

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
async def race_setup(req: SetupReq):
    """
    Race Setup submits one authoritative session_config + entrants roster.
    We store session_config, map entrants to ENGINE format, and call ENGINE.load().
    """
    global _CURRENT_SESSION, _CURRENT_ENTRANTS_ENGINE, _CURRENT_RACE_ID
    _CURRENT_SESSION = req.session_config.model_dump()
    _CURRENT_RACE_ID = int(req.race_id)

    # Map entrants using validated EntrantIn → engine expects entrant_id/car_number/status/etc.
    entrants_engine: List[Dict[str, Any]] = []
    for e in (req.entrants or []):
        if e.id is None:
            raise HTTPException(status_code=400, detail="entrant id is required")
        entrants_engine.append({
            "entrant_id": int(e.id),
            "name": e.name,
            "car_number": (str(e.number).strip() if e.number is not None else None),
            "tag": (_normalize_tag(e.tag)),
            "enabled": bool(e.enabled),
            "status": (e.status or "ACTIVE").upper(),
        })

    _CURRENT_ENTRANTS_ENGINE = entrants_engine[:]  # keep for reset

    # Use the Setup-selected mode as race_type (e.g., "sprint", "endurance", ...)
    race_type = _CURRENT_SESSION.get("mode_id", "sprint")

    # Call the real engine signature: (race_id, entrants, race_type)
    snap = ENGINE.load(race_id=_CURRENT_RACE_ID, entrants=entrants_engine,
                       race_type=str(_CURRENT_SESSION.get("mode_id", "sprint")))
    
    return JSONResponse({
        "ok": True,
        "session_id": req.race_id,
        "snapshot": snap,
        "derived": _derive_for_control(_CURRENT_SESSION),
    })

@app.get("/race/session")
async def race_session():
    """
    Race Control calls this on load. It gets the exact session_config the operator set,
    plus a derived block with normalized numbers and sound URLs.
    """
    if not _CURRENT_SESSION:
        raise HTTPException(status_code=404, detail="no active session")
    return {
        "session_config": _CURRENT_SESSION,
        "derived": _derive_for_control(_CURRENT_SESSION),
    }



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

    # Map to the engine's expected shape: entrant_id / car_number / status
    entrants_engine: List[Dict[str, Any]] = []
    for item in entrants_ui:
        entrants_engine.append({
            "entrant_id": item["id"],
            "name": item.get("name"),
            "car_number": (str(item.get("number")).strip()
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

    snapshot = ENGINE.load(race_id=race_id, entrants=entrants_engine, race_type=str(race_type))
    return JSONResponse(snapshot)



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

# ------------------------------------------------------------
# Endpoints — flag / state / reset (lightweight, engine-aware)
# ------------------------------------------------------------
class FlagReq(BaseModel):
    flag: str

@app.post("/engine/flag")
async def engine_set_flag(req: FlagReq):
    """
    Set the live flag. Calls ENGINE.set_flag(...) if available; otherwise 501.
    """
    if not hasattr(ENGINE, "set_flag"):
        raise HTTPException(status_code=501, detail="engine does not implement set_flag")
    try:
        snap = ENGINE.set_flag(req.flag)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    return JSONResponse(snap if snap is not None else {"ok": True, "flag": req.flag})

@app.get("/race/state")
async def race_state():
    """
    Return a live snapshot. Calls ENGINE.snapshot() if available; else a stub.
    """
    if hasattr(ENGINE, "snapshot"):
        snap = ENGINE.snapshot()
        return JSONResponse(snap)
    return JSONResponse({"ok": True, "engine": "no-snapshot"})

@app.post("/race/reset_session")
async def reset_session():
    """
    Abort & reset:
      - go straight to PRE
      - clear live seen/lap data
      - keep roster intact
    Prefer an engine-native reset if present; else re-load prior roster.
    """
    # Try engine-native reset if implemented
    reset_fn = getattr(ENGINE, "reset_session", None)
    if callable(reset_fn):
        snap = reset_fn()
        return JSONResponse({"ok": True, "snapshot": snap})

    # Fallback: re-load with the cached roster + mode + race_id
    global _CURRENT_ENTRANTS_ENGINE, _CURRENT_RACE_ID, _CURRENT_SESSION

    race_type = (_CURRENT_SESSION.get("mode_id", "sprint") if _CURRENT_SESSION else "sprint")
    snap = ENGINE.load(
        race_id=int(_CURRENT_RACE_ID or 0),
        entrants=_CURRENT_ENTRANTS_ENGINE or [],
        race_type=str(race_type),
    )

    # Best-effort return to PRE
    set_flag = getattr(ENGINE, "set_flag", None)
    if callable(set_flag):
        try:
            set_flag("pre")
        except Exception:
            pass

    return JSONResponse({"ok": True, "snapshot": snap})





# ------------------------------------------------------------
# Admin Entrants (authoritative DB read/write)
# ------------------------------------------------------------

from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, ValidationError, field_validator
from fastapi import HTTPException
import aiosqlite
import sqlite3

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

# ---- Race Setup → Race Control session contracts ----------------------------

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
    entrants: List[EntrantIn] = []          # <- strong typing: id must be present/valid
    session_config: SessionConfigIn


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

@app.get("/ilap/peek")
async def ilap_peek():
    """Return the last observed tag. UI de-duplicates via seen_at."""
    return JSONResponse(last_tag)


# ------------------------------------------------------------
# SSE endpoint (preferred by UI)
# ------------------------------------------------------------

@app.get("/ilap/stream")
async def ilap_stream(request: Request):
    """Send exactly one tag event and then end (UI opens per-scan)."""
    q: asyncio.Queue[str] = asyncio.Queue()
    _listeners.append(q)

    async def gen():
        try:
            # If client disconnects or cancels scan, this raises
            tag = await asyncio.wait_for(q.get(), timeout=10.0)
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
@app.post("/ilap/inject")
async def ilap_inject(tag: str):
    """Convenience endpoint so external processes can push tags."""
    ts = publish_tag(tag)
    return {"ok": True, "seen_at": ts}
#

# ------------------------------------------------------------
# --- Embedded scanner startup: publish via HTTP to our own FastAPI ---
# ------------------------------------------------------------
@app.on_event("startup")
async def start_scanner():
    """
    If ScannerService is available, use it.
    Otherwise, if scanner.source=='mock', run a tiny in-process tag generator
    so the UI has something to chew on.
    """
    from .config_loader import get_scanner_cfg
    scan_cfg = get_scanner_cfg() or {}
    source = str(scan_cfg.get("source", "mock")).lower()

    try:
        if source != "mock":
            # Try to run the real scanner for serial/udp modes
            from backend.lap_logger import ScannerService
            # ... build cfg as before (your working version) ...
            # (omit here for brevity; keep the version we just fixed)
            return
    except Exception:
        log.exception("ScannerService import failed; falling back if mock is requested.")

    # Fallback: lightweight mock when ScannerService isn't available
    if source == "mock":
        log.info("Starting in-process MOCK tag generator.")
        async def _mock_task(stop_evt: asyncio.Event):
            import random, time
            tags = ["1234567", "2345678", "3456789", "4567890"]
            while not stop_evt.is_set():
                tag = random.choice(tags)
                publish_tag(tag)  # reuse your bus → /ilap/peek & /ilap/stream see this
                # Optional: also feed diagnostics
                try:
                    await diag_publish({
                        "tag_id": tag,
                        "entrant": {"name": f"Unknown {tag}", "number": None},
                        "source": "Start/Finish",
                        "rssi": -60 - random.randint(0, 15),
                        "time": None,
                    })
                except Exception:
                    pass
                await asyncio.sleep(1.5)

        app.state.stop_evt = asyncio.Event()
        app.state.task = asyncio.create_task(_mock_task(app.state.stop_evt))
    else:
        log.info("No ScannerService and not in mock mode; scanner startup skipped.")




@app.on_event("shutdown")
async def stop_scanner():
    """
    Gracefully stop the background scanner on shutdown.
    """
    stop_evt = getattr(app.state, "stop_evt", None)
    task = getattr(app.state, "task", None)
    if stop_evt:
        stop_evt.set()
    if task:
        try:
            await task
        except Exception:
            pass


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
