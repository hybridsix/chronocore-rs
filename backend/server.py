# backend/server.py
# =============================================================================
# ChronoCore Backend — FastAPI app (engine-first)
#
# This service exposes:
#   • /race/state                 → Authoritative in-memory RaceEngine snapshot
#   • /engine/load                → Initialize a race (roster + tags)
#   • /engine/flag                → Change race flag. Allowed: pre|green|yellow|red|white|checkered|blue
#   • /engine/pass                → Ingest a timing pass (track|pit_in|pit_out)
#   • /engine/entrant/enable      → Enable/disable an entrant for this race
#   • /engine/entrant/status      → Set entrant status (ACTIVE|DISABLED|DNF|DQ)
#   • /engine/entrant/assign_tag  → Bind/unbind a tag for an entrant
#
# Notes:
#   - Engine is the live authority (fast + consistent). DB mirroring is optional.
#   - Spectator/Operator UIs should poll /race/state (low-latency, stable contract).
#   - Static UI is served at /ui (mounts the repo’s ui/ directory).
# =============================================================================

from __future__ import annotations

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
import os
import io, csv
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse
from pydantic import BaseModel

from fastapi.staticfiles import StaticFiles
import sqlite3
import yaml
from pathlib import Path
from .db_schema import ensure_schema, tag_conflicts

# RaceEngine singleton (authoritative in-memory state)
from .race_engine import ENGINE

# For External Read-Only Feed
from fastapi.responses import JSONResponse


# ---------------------------------------------------------------------------
# FastAPI app + CORS + Static
# ---------------------------------------------------------------------------
app = FastAPI(
    title="ChronoCore Backend",
    description="Engine-first race timing API for ChronoCore.",
    version="0.1.0",
)

# Forward-only config: require app.engine.persistence.sqlite_path
ROOT = os.path.dirname(os.path.dirname(__file__))
CONFIG_DIR = os.path.join(ROOT, 'config')


APP_YAML = os.path.join(CONFIG_DIR, 'app.yaml')
with open(APP_YAML, 'r', encoding='utf-8') as _f:
    _cfg = yaml.safe_load(_f)
try:
    _pcfg = _cfg['app']['engine']['persistence']
    _sqlite_path = _pcfg['sqlite_path']
except KeyError as e:
    raise RuntimeError('Missing required config: app.engine.persistence.sqlite_path') from e

DB_PATH = Path(_sqlite_path)
ensure_schema(DB_PATH, recreate=bool(_pcfg.get('recreate_on_boot', False)))


# Allow local development across ports / shells (operator, spectator, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # lock this down in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static UI (served at /ui)
# server.py lives in backend/, so repo root is one level up
ROOT = os.path.dirname(os.path.dirname(__file__))
UI_DIR = os.path.join(ROOT, "ui")
if os.path.isdir(UI_DIR):
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")



# ---- DB helpers -----------------------------------------------------------
def _conn():
    return sqlite3.connect(DB_PATH)

def _row_to_entrant(row):
    (entrant_id, car_number, name, tag, enabled, status,
     organization, spoken_name, color, logo, created_at, updated_at) = row
    return {
        'id': entrant_id,
        'number': car_number,
        'name': name,
        'tag': tag,
        'enabled': bool(enabled),
        'status': status,
        'organization': organization,
        'spoken_name': spoken_name,
        'color': color,
        'logo': logo,
        'created_at': created_at,
        'updated_at': updated_at,
    }

# ---------------------------------------------------------------------------
# Basic health / convenience routes
# ---------------------------------------------------------------------------
@app.get("/")
def root_redirect():
    """
    Convenience: redirect bare root to the UI bundle if present.
    """
    return RedirectResponse(url="/ui/") if os.path.isdir(UI_DIR) else JSONResponse({"ok": True})

@app.get("/health")
def health():
    """
    Simple liveness probe.
    """
    return {"ok": True}

@app.get("/healthz")
def healthz():
    """
    Extended health probe. DB path is informational only (engine-first).
    """
    # The engine's journal (if enabled) writes to backend/db/laps.sqlite
    db_rel = os.path.join("backend", "db", "laps.sqlite")
    return {"ok": True, "db": db_rel}


# ---------------------------------------------------------------------------
# Engine-first API
# ---------------------------------------------------------------------------
@app.get("/race/state")
def race_state():
    """
    Return the authoritative RaceSnapshot (engine-owned, atomic).
    The snapshot includes: flag, race_id, race_type, clock_ms, running,
    standings[], last_update_utc, features, etc.
    """
    try:
        return JSONResponse(ENGINE.snapshot())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/race/current")
def race_current():
    """
    Minimal helper for UIs that auto-follow the active race.
    """
    return {"race_id": ENGINE.race_id}


@app.post("/engine/load")
async def engine_load(payload: Dict[str, Any]):
    """
    Initialize a race session and roster.
    Enforces duplicate-tag rules (enabled entrants must have unique tags).
    """
    race_id = payload.get("race_id")
    if race_id is None:
        raise HTTPException(status_code=400, detail="race_id required")
    entrants = payload.get("entrants", [])
    race_type = payload.get("race_type", "sprint")

    seen = {}
    for e in entrants:
        if not e.get('enabled', True):
            continue
        t = (e.get('tag') or '').strip()
        if not t:
            continue
        if t in seen and seen[t] != e.get('entrant_id') and seen[t] != e.get('id'):
            raise HTTPException(status_code=409, detail=f"Duplicate tag in payload: {t}")
        seen[t] = e.get('entrant_id') or e.get('id')

    with _conn() as c:
        for e in entrants:
            if not e.get('enabled', True):
                continue
            t = (e.get('tag') or '').strip()
            if not t:
                continue
            incumbent = e.get('entrant_id') or e.get('id')
            if tag_conflicts(c, t, incumbent_entrant_id=incumbent):
                raise HTTPException(status_code=409, detail=f"Tag in use by another enabled entrant: {t}")

    try:
        snap = ENGINE.load(int(race_id), entrants, race_type=str(race_type))
        return JSONResponse(snap)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/engine/flag")
async def engine_flag(payload: Dict[str, Any]):
    """
    Change race flag. Allowed: pre|green|yellow|red|white|checkered|blue

    Behavior:
    - First transition to green starts the race clock.
    - Red still increments laps (your rule).
    - Checkered freezes clock + standings (leader-based classification).
    """
    flag = payload.get("flag")
    if not flag:
        raise HTTPException(status_code=400, detail="flag required")
    try:
        return JSONResponse(ENGINE.set_flag(str(flag)))
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


@app.post("/engine/pass")
async def engine_pass(payload: Dict[str, Any]):
    """
    Ingest a timing pass from track or pit receivers.

    Body:
    {
      "tag": "3000123",
      "ts_ns": 1696000000999999999,   // optional; server will timestamp if missing
      "source": "track|pit_in|pit_out",
      "device_id": "gate-a"           // optional; can be auto-routed via YAML pits.receivers
    }

    Returns a mini result:
    {
      "ok": true,
      "entrant_id": 12,               // null if unknown and auto_provisional=false
      "lap_added": true|false,
      "lap_time_s": 23.481|null,
      "reason": null|"min_lap"|"dup"|"pit_event"|...
    }
    """
    tag = payload.get("tag")
    if not tag:
        raise HTTPException(status_code=400, detail="tag required")

    ts_ns = payload.get("ts_ns")
    source = payload.get("source", "track")
    device_id = payload.get("device_id")

    try:
        res = ENGINE.ingest_pass(str(tag), ts_ns=ts_ns, source=str(source), device_id=device_id)
        return JSONResponse(res)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/engine/entrant/enable")
async def engine_entrant_enable(payload: Dict[str, Any]):
    """
    Enable/disable an entrant for THIS race (roster membership).
    - If disabled, passes for their tag are ignored.
    """
    eid = payload.get("entrant_id")
    if eid is None:
        raise HTTPException(status_code=400, detail="entrant_id required")

    enabled = bool(payload.get("enabled", True))
    try:
        snap = ENGINE.update_entrant_enable(int(eid), enabled)
        return JSONResponse(snap)
    except KeyError:
        raise HTTPException(status_code=404, detail="entrant not found")


@app.post("/engine/entrant/status")
async def engine_entrant_status(payload: Dict[str, Any]):
    """
    Set an entrant's operational status: ACTIVE|DISABLED|DNF|DQ
    - Status is independent from 'enabled' (roster membership).
    """
    eid = payload.get("entrant_id")
    status = payload.get("status")
    if eid is None or not status:
        raise HTTPException(status_code=400, detail="entrant_id and status required")
    try:
        snap = ENGINE.update_entrant_status(int(eid), str(status))
        return JSONResponse(snap)
    except KeyError:
        raise HTTPException(status_code=404, detail="entrant not found")
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


@app.post("/engine/entrant/assign_tag")
async def engine_entrant_assign_tag(payload: Dict[str, Any]):
    """Bind/unbind a tag for an entrant; 409 if tag belongs to another enabled entrant."""
    eid = payload.get("entrant_id")
    if eid is None:
        raise HTTPException(status_code=400, detail="entrant_id required")
    eid = int(eid)
    tag = payload.get("tag", None)
    t = (tag or '').strip()
    if t:
        with _conn() as c:
            if tag_conflicts(c, t, incumbent_entrant_id=eid):
                raise HTTPException(status_code=409, detail="Tag already assigned to another enabled entrant")
    try:
        snap = ENGINE.assign_tag(eid, tag)
        return JSONResponse(snap)
    except KeyError:
        raise HTTPException(status_code=404, detail="entrant not found")

# ---------------------------------------------------------------------------
# Read-only public feed
# ---------------------------------------------------------------------------

@app.get("/race/feed")
def race_feed():
    """Public read-only race snapshot (CORS/ETag friendly)."""
    s = ENGINE.snapshot()
    headers = {
        "Cache-Control": "no-store",
        "ETag": f'W/{s.get("last_update_utc", 0)}'
    }
    return JSONResponse(s, headers=headers)

# ---------------------------------------------------------------------------
# Simulator Active 
# ---------------------------------------------------------------------------

class SimPayload(BaseModel):
    sim: bool | None = None
    on: bool | None = None
    label: str | None = None

@app.post("/engine/sim")
def engine_sim(payload: SimPayload):
    """Toggle simulator banner/pill."""
    on = payload.sim if payload.sim is not None else (payload.on if payload.on is not None else False)
    snap = ENGINE.set_sim(on, payload.label)
    return snap




# ---------------------------------------------------------------------------
# Admin Entrants (authoring source of truth) — extended fields pass-through
# ---------------------------------------------------------------------------
@app.get('/admin/entrants')
def admin_entrants_list():
    with _conn() as c:
        cur = c.cursor()
        cur.execute(
            'SELECT entrant_id,car_number,name,tag,enabled,status,organization,spoken_name,color,logo,created_at,updated_at FROM entrants ORDER BY entrant_id'
        )
        rows = cur.fetchall()
    return [ _row_to_entrant(r) for r in rows ]

@app.post('/admin/entrants')
def admin_entrants_upsert(payload: Dict[str, Any]):
    entrants = payload.get('entrants', [])
    seen = {}
    for e in entrants:
        if not e.get('enabled', True):
            continue
        t = (e.get('tag') or '').strip()
        if not t:
            continue
        if t in seen and seen[t] != e.get('id'):
            raise HTTPException(status_code=409, detail=f'Duplicate tag in payload: {t}')
        seen[t] = e.get('id')
    with _conn() as c:
        for e in entrants:
            if not e.get('enabled', True):
                continue
            t = (e.get('tag') or '').strip()
            if not t:
                continue
            if tag_conflicts(c, t, incumbent_entrant_id=e.get('id')):
                raise HTTPException(status_code=409, detail=f'Tag in use by another enabled entrant: {t}')
        cur = c.cursor()
        for e in entrants:
            cur.execute(
                """
                INSERT INTO entrants(
                  entrant_id, car_number, name, tag, enabled, status,
                  organization, spoken_name, color, logo, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?, strftime('%s','now'))
                ON CONFLICT(entrant_id) DO UPDATE SET
                  car_number=excluded.car_number,
                  name=excluded.name,
                  tag=excluded.tag,
                  enabled=excluded.enabled,
                  status=excluded.status,
                  organization=excluded.organization,
                  spoken_name=excluded.spoken_name,
                  color=excluded.color,
                  logo=excluded.logo,
                  updated_at=strftime('%s','now')
                """,
                (
                  e.get('id'),
                  str(e.get('number')) if e.get('number') is not None else None,
                  e.get('name'),
                  (e.get('tag') or None),
                  1 if e.get('enabled', True) else 0,
                  e.get('status', 'ACTIVE'),
                  e.get('organization'),
                  e.get('spoken_name'),
                  e.get('color'),
                  e.get('logo'),
                )
            )
        c.commit()
    return {'ok': True, 'count': len(entrants)}
# ---------------------------------------------------------------------------
# CSV Export Logic
# ---------------------------------------------------------------------------

@app.get("/race/{race_id}/export.csv")
def export_csv(race_id: int):
    """CSV export of current standings (Google Sheets friendly)."""
    snap = ENGINE.snapshot()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["position","entrant_id","car_number","name","laps","best","last","pace_5","status"])
    for i, r in enumerate(snap.get("standings", []), start=1):
        w.writerow([
            i,
            r.get("entrant_id"),
            r.get("car_number",""),
            r.get("name",""),
            r.get("laps",0),
            r.get("best",""),
            r.get("last",""),
            r.get("pace_5",""),
            r.get("status","")
        ])
    return PlainTextResponse(out.getvalue(), media_type="text/csv; charset=utf-8")


# ---------------------------------------------------------------------------
# Error handlers (optional sugar for uniform JSON errors)
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    # Avoid leaking internals; surface message only
    return JSONResponse(status_code=500, content={"error": str(exc)})


# ---------------------------------------------------------------------------
# Dev tip
# ---------------------------------------------------------------------------
# Run locally:
#   uvicorn backend.server:app --reload --port 8000
#
# Visit:
#   http://localhost:8000/ui/            (UI bundle, if present)
#   http://localhost:8000/docs           (Swagger UI)
#   http://localhost:8000/race/state     (live snapshot)
#
# Example sequence (PowerShell):
#   Invoke-RestMethod -Method Post -Uri http://localhost:8000/engine/load `
#     -ContentType "application/json" `
#     -Body (@{ race_id=1; race_type="sprint"; entrants=@(
#         @{ entrant_id=1; enabled=$true; status="ACTIVE"; tag="3000123"; car_number="101"; name="Team A" },
#         @{ entrant_id=2; enabled=$true; status="ACTIVE"; tag="30004583"; car_number="7";   name="Team B" }
#     ) } | ConvertTo-Json -Depth 6)
#
#   Invoke-RestMethod -Method Post -Uri http://localhost:8000/engine/flag `
#     -ContentType "application/json" -Body '{ "flag":"green" }'
#
#   Invoke-RestMethod -Method Post -Uri http://localhost:8000/engine/pass `
#     -ContentType "application/json" -Body '{ "tag":"3000123", "source":"track" }'
