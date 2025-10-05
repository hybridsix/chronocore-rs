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
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from .db_schema import ensure_schema, tag_conflicts
from .config_loader import get_db_path

# ------------------------------------------------------------
# FastAPI app bootstrap
# ------------------------------------------------------------
app = FastAPI(title="CCRS Backend", version="0.2.1")

# CORS: permissive for development. Tighten for production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static UI bundle if present
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

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
from pydantic import BaseModel, Field, ValidationError
from typing import Optional as _Optional

class EntrantIn(BaseModel):
    """
    Authoritative entrant record for the database.
    Notes:
      - 'id' is the DB primary key (entrant_id).
      - 'tag': empty/whitespace becomes NULL; conflict rules apply only when enabled=1 and tag IS NOT NULL.
    """
    id: int = Field(..., description="entrant_id primary key")
    number: _Optional[str] = None
    name: str
    tag: _Optional[str] = None
    enabled: bool = True
    status: str = "ACTIVE"

def _norm_tag(value: _Optional[str]) -> _Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None

@app.get("/admin/entrants")
async def admin_list_entrants():
    """
    Return all entrants from the database. This is the source of truth for IDs/tags/enabled flags.
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
        return [dict(r) for r in rows]

@app.post("/admin/entrants")
async def admin_upsert_entrants(payload: Dict[str, Any]):
    """
    Upsert a list of entrants into the DB (id = entrant_id).
    Conflict rule: among ENABLED entrants only, 'tag' must be unique. The incumbent row is excluded.
    On conflict, the entire request fails with 409 and details about which id/tag collided.
    """
    entrants = payload.get("entrants")
    if not isinstance(entrants, list):
        raise HTTPException(status_code=400, detail="body must contain 'entrants' as a list")

    # Validate & normalize first (fail fast, before touching the DB)
    entries: list[EntrantIn] = []
    for idx, item in enumerate(entrants):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"entrant at index {idx} must be an object")
        try:
            e = EntrantIn(**item)
        except ValidationError as ve:
            raise HTTPException(status_code=400, detail=f"invalid entrant at index {idx}: {ve.errors()!r}")
        e.tag = _norm_tag(e.tag)
        entries.append(e)

    # Apply within a transaction so it's all-or-nothing
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN")
        try:
            # Enforce conflicts at the app level (DB also has a UNIQUE partial index as a seatbelt)
            import sqlite3
            with sqlite3.connect(DB_PATH) as sconn:
                for e in entries:
                    if e.enabled and e.tag:
                        if tag_conflicts(sconn, e.tag, incumbent_entrant_id=e.id):
                            # Abort the whole batch
                            await db.execute("ROLLBACK")
                            raise HTTPException(
                                status_code=409,
                                detail=f"tag '{e.tag}' already assigned to another enabled entrant (while upserting id={e.id})"
                            )

            # Upsert rows
            for e in entries:
                await db.execute("""
                    INSERT INTO entrants (entrant_id, number, name, tag, enabled, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))
                    ON CONFLICT(entrant_id) DO UPDATE SET
                        number=excluded.number,
                        name=excluded.name,
                        tag=excluded.tag,
                        enabled=excluded.enabled,
                        status=excluded.status,
                        updated_at=strftime('%s','now')
                """, (e.id, e.number, e.name, e.tag, 1 if e.enabled else 0, e.status))

            await db.commit()
        except HTTPException:
            # already rolled back for conflict; re-raise
            raise
        except Exception as ex:
            await db.execute("ROLLBACK")
            raise HTTPException(status_code=500, detail=f"admin upsert failed: {type(ex).__name__}")
    return {"ok": True, "count": len(entries)}


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
