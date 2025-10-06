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

from pathlib import Path
from typing import Any, Dict, List, Optional
import aiosqlite
from fastapi import FastAPI, HTTPException, Response, status, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from fastapi.staticfiles import StaticFiles

import logging

from pydantic import BaseModel, Field, ValidationError, field_validator
from typing import Optional as _Optional

import asyncio
import json
import time

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





# CORS: permissive for development. Tighten for production.
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
#STATIC_DIR = UI_DIR if UI_DIR.exists() else Path(__file__).resolve().parent / "static"
#app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
STATIC_DIR = Path(__file__).resolve().parent.parent / "ui"   # => project_root/ui
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

log.info("Serving UI from: %s", STATIC_DIR)  # sanity print at startup



# Resolve DB path from config and ensure schema on boot.
DB_PATH = get_db_path()
ensure_schema(DB_PATH, recreate=False, include_passes=True)

# ------------------------------------------------------------
# Minimal "Engine" adapter used by these endpoints
# ------------------------------------------------------------
class _Engine:
    """
    Very small in-memory session mirror the operator UI interacts with.
    Your real runtime engine can replace this; keep method names the same.
    """
    def __init__(self) -> None:
        self._entrants: Dict[int, Dict[str, Any]] = {}

    def load(self, entrants: List[Dict[str, Any]]) -> Dict[str, Any]:
        # Store by id for constant-time lookups
        self._entrants = {e["id"]: e for e in entrants}
        return {"ok": True, "entrants": list(self._entrants.keys())}

    def assign_tag(self, entrant_id: int, tag: Optional[str]) -> Dict[str, Any]:
        if entrant_id not in self._entrants:
            # We deliberately raise to let the API return 412 Precondition Failed
            raise KeyError("entrant not in active session")
        self._entrants[entrant_id]["tag"] = tag
        return {"ok": True, "entrant_id": entrant_id, "tag": tag}

ENGINE = _Engine()

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
        int(race_id)
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

    # Minimal shape that the runtime uses (pass-through tolerated)
    entrants_engine: List[Dict[str, Any]] = []
    for item in entrants_ui:
        entrants_engine.append({
            "id": item["id"],
            "name": item.get("name"),
            "number": item.get("number"),
            "tag": _normalize_tag(item.get("tag")),
            "enabled": bool(item.get("enabled", True)),
        })

    snapshot = ENGINE.load(entrants_engine)
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
            import sqlite3
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
# Admin Entrants (authoritative DB read/write)
# ------------------------------------------------------------
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator

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
    # CHANGED: optional id enables "create" semantics; id <= 0 also treated as create for back-compat with old id=0 posts
    id: Optional[int] = Field(
        default=None,
        description="entrant_id primary key; None/<=0 means create"
    )

    # KEEP your fields but make intent explicit
    number: Optional[str] = None
    name: str
    tag: Optional[str] = None
    enabled: bool = True
    status: str = "ACTIVE"

    # ---- Normalizers / Coercions ----------------------------------------------------

    @field_validator('number', mode='before')
    @classmethod
    def _coerce_number(cls, v):
        """
        Accept '42' or 42 and store as string; allow None.
        """
        if v is None:
            return None
        return str(v)

    @field_validator('tag', mode='before')
    @classmethod
    def _normalize_tag(cls, v):
        """
        Empty/whitespace-only tags become None so they don't participate in the unique-when-enabled rule.
        """
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator('enabled', mode='before')
    @classmethod
    def _coerce_enabled(cls, v):
        """
        Accept booleans, 0/1, and '0'/'1' strings.
        """
        if isinstance(v, bool):
            return v
        if isinstance(v, (int,)):
            return v == 1
        if isinstance(v, str):
            sv = v.strip().lower()
            if sv in ('1', 'true', 'yes', 'y', 'on'):
                return True
            if sv in ('0', 'false', 'no', 'n', 'off'):
                return False
        # Fallback to Python truthiness, but be explicit:
        return bool(v)

    # Convenience helpers your route can use (optional)
    def is_create(self) -> bool:
        """Return True if this payload should INSERT a new entrant."""
        return self.id is None or (isinstance(self.id, int) and self.id <= 0)

    def normalized_tag(self) -> Optional[str]:
        """Tag after whitespace->None normalization."""
        return self.tag

def _norm_tag(value: _Optional[str]) -> _Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None

@app.get("/admin/entrants")
async def admin_list_entrants():
    """
    Authoritative read of entrants for Operator UI.

    Output normalization rules:
      - 'id'      : always an integer (SQLite primary key).
      - 'number'  : always a string if present, else None.
      - 'enabled' : always a boolean True/False, not 0/1.
      - Other fields are passed through as stored.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT entrant_id AS id, number, name, tag, enabled, status
            FROM entrants
            ORDER BY entrant_id
        """)
        rows = await cur.fetchall()
        await cur.close()

        out = []
        for r in rows:
            out.append({
                # id stays an integer exactly as stored
                "id": int(r["id"]) if r["id"] is not None else None,

                # number always stringified so UI can treat it uniformly
                "number": (str(r["number"]) if r["number"] is not None else None),

                # other fields unchanged
                "name": r["name"],
                "tag": r["tag"],

                # ensure strict boolean for enabled
                "enabled": bool(r["enabled"]),

                "status": r["status"],
            })

        return out


@app.post("/admin/entrants")
async def admin_upsert_entrants(payload: Dict[str, Any]):
    """
    Create or update entrants in the authoritative DB.

    Semantics:
      • If 'id' is None, missing, or ≤ 0  → INSERT (SQLite assigns new entrant_id)
      • If 'id' > 0                       → UPDATE that specific row (upsert)
    Rules:
      • Among ENABLED entrants only, 'tag' must be unique (app-level pre-check + DB index backstop).
      • All operations occur inside a single transaction (atomic batch).
      • Returns 409 on conflict, 400 on bad payload, 200 on success.
    """

    entrants = payload.get("entrants")
    if not isinstance(entrants, list):
        raise HTTPException(status_code=400, detail="body must contain 'entrants' as a list")

    
    # ---------------------------------------------------------------------
    # 1) Validate and normalize every entrant before touching the DB
    # ---------------------------------------------------------------------
    entries: list[EntrantIn] = []
    for idx, item in enumerate(entrants):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"entrant at index {idx} must be an object")
        try:
            e = EntrantIn(**item)
        except ValidationError as ve:
            raise HTTPException(status_code=400, detail=f"invalid entrant at index {idx}: {ve.errors()!r}")
        entries.append(e)

    
    # ---------------------------------------------------------------------
    # 2) Open the DB transaction and enforce unique-tag rule
    # ---------------------------------------------------------------------
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")  # get write lock early for predictability
        try:
            import sqlite3
            with sqlite3.connect(DB_PATH) as sconn:
                for e in entries:
                    # Only enabled entrants with a non-null tag participate in the uniqueness rule
                    if e.enabled and e.tag:
                        if tag_conflicts(sconn, e.tag, incumbent_entrant_id=(e.id if not e.is_create() else None)):
                            await db.execute("ROLLBACK")
                            raise HTTPException(
                                status_code=409,
                                detail=f"tag '{e.tag}' already assigned to another enabled entrant (while upserting id={e.id or 'new'})"
                            )

            # -----------------------------------------------------------------
            # 3) Perform inserts or upserts depending on id semantics
            #    - For CREATE we capture cursor.lastrowid from aiosqlite.
            #    - We also collect (client_idx, new_id) so the UI can learn ids.
            # -----------------------------------------------------------------
            created = 0
            updated = 0
            assigned_ids: list[dict] = []   # e.g. [{"client_idx": 0, "id": 17}]

            for i, e in enumerate(entries):
                # If you have a tag normalizer on the model, use it; otherwise stick with e.tag
                tag_value = e.tag
                # tag_value = e.normalized_tag()

                if e.is_create():
                    # -------- INSERT (let SQLite assign entrant_id) --------
                    # EXACTLY 5 placeholders for the 5 values we pass.
                    # updated_at is set by SQLite, not as a bound parameter.
                    cur = await db.execute(
                        """
                        INSERT INTO entrants (number, name, tag, enabled, status, updated_at)
                        VALUES (?, ?, ?, ?, ?, strftime('%s','now'))
                        """,
                        (e.number, e.name, tag_value, 1 if e.enabled else 0, e.status),
                    )
                    new_id = cur.lastrowid
                    assigned_ids.append({"client_idx": i, "id": new_id})
                    await cur.close()
                    created += 1
                else:
                    # -------- UPSERT by existing id (PRIMARY KEY) --------
                    # 6 placeholders for the 6 values we pass.
                    await db.execute(
                        """
                        INSERT INTO entrants (entrant_id, number, name, tag, enabled, status, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))
                        ON CONFLICT(entrant_id) DO UPDATE SET
                            number     = excluded.number,
                            name       = excluded.name,
                            tag        = excluded.tag,
                            enabled    = excluded.enabled,
                            status     = excluded.status,
                            updated_at = strftime('%s','now')
                        """,
                        (e.id, e.number, tag_value, 1 if e.enabled else 0, e.status),
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
            raise  # already rolled back above
        except sqlite3.IntegrityError as ie:
            # Defensive backstop if DB-level unique index fires first
            await db.execute("ROLLBACK")
            raise HTTPException(status_code=409, detail=f"uniqueness violation: {ie}")
        except Exception as ex:
            await db.execute("ROLLBACK")
            raise HTTPException(status_code=500, detail=f"admin upsert failed: {type(ex).__name__}: {ex}")

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

app.on_event("startup")
async def start_scanner():
    from backend.lap_logger import ScannerService, load_config
    cfg = load_config("./config/ccrs.yaml")
    cfg.publisher.mode = "inprocess"
    app.state.stop_evt = asyncio.Event()
    app.state.task = asyncio.create_task(ScannerService(cfg).run(app.state.stop_evt))


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
