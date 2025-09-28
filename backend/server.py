"""
PRS Race Timing — Backend (FastAPI + SQLite)
Focus: server.py, laps.sqlite schema, FastAPI endpoints
Topics covered: /laps, /race/state, dummy loaders, scoring logic

Run locally:
    uvicorn server:app --reload

Requires:
    fastapi, uvicorn, pydantic

Database file:
    laps.sqlite (auto-created on first run)
"""
from __future__ import annotations

import os
import random
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
##from fastapi.staticfiles import StaticFiles
from starlette.staticfiles import StaticFiles

##app = FastAPI(title="PRS Backend", version="0.1.1")

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "laps.sqlite"

# Serve /ui for static files if you want the spectator page from here
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

DB_PATH = os.environ.get("PRS_DB", "laps.sqlite")

###############################################################################
# SQLite bootstrap (schema + helpers)
###############################################################################

SQL_SCHEMA = r"""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS races (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    start_ts_utc    INTEGER NOT NULL,   -- epoch ms
    end_ts_utc      INTEGER,            -- nullable for live race; if set, used as checkered cutoff
    created_at_utc  INTEGER NOT NULL    -- epoch ms
);

-- Identity map for each transponder/tag within a race
CREATE TABLE IF NOT EXISTS transponders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id         INTEGER NOT NULL,
    tag             TEXT NOT NULL,       -- RFID/transponder UID
    org             TEXT,                -- organization/team name (optional)
    display_name    TEXT,                -- driver / label (optional)
    created_at_utc  INTEGER NOT NULL,
    UNIQUE(race_id, tag),
    FOREIGN KEY(race_id) REFERENCES races(id) ON DELETE CASCADE
);

-- Decoder/raw pipeline: every line crossing
CREATE TABLE IF NOT EXISTS passes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id         INTEGER NOT NULL,
    tag             TEXT NOT NULL,
    ts_utc          INTEGER NOT NULL,    -- epoch ms
    source          TEXT DEFAULT 'decoder',
    device_id       TEXT,
    meta_json       TEXT,
    created_at_utc  INTEGER NOT NULL,
    FOREIGN KEY(race_id) REFERENCES races(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_passes_race_tag_ts ON passes(race_id, tag, ts_utc);
CREATE INDEX IF NOT EXISTS idx_passes_race_ts ON passes(race_id, ts_utc);

-- Manual pipeline (optional): same semantics as passes but hand-entered
CREATE TABLE IF NOT EXISTS laps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id         INTEGER NOT NULL,
    tag             TEXT NOT NULL,
    ts_utc          INTEGER NOT NULL,    -- crossing time, epoch ms (server-trusted)
    source          TEXT DEFAULT 'manual',
    device_id       TEXT,                -- kiosk / reader / operator id
    meta_json       TEXT,                -- optional opaque metadata (JSON string)
    created_at_utc  INTEGER NOT NULL,    -- insertion time, epoch ms
    FOREIGN KEY(race_id) REFERENCES races(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_laps_race_tag_ts ON laps(race_id, tag, ts_utc);
CREATE INDEX IF NOT EXISTS idx_laps_race_ts ON laps(race_id, ts_utc);

-- Simple key-value table for misc settings (optional)
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.executescript(SQL_SCHEMA)
        conn.commit()
    finally:
        conn.close()


init_db()

###############################################################################
# Pydantic models (requests / responses)
###############################################################################

class LapIn(BaseModel):
    race_id: int = Field(..., ge=1)
    tag: str
    ts_client_utc: Optional[int] = Field(None, description="Epoch ms, optional client-supplied time")
    source: Optional[str] = Field(default="manual")
    device_id: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

    @validator("tag")
    def strip_tag(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("tag must not be empty")
        return v


class Lap(BaseModel):
    id: int
    race_id: int
    tag: str
    ts_utc: int
    source: Optional[str]
    device_id: Optional[str]
    meta: Optional[Dict[str, Any]]
    created_at_utc: int


class RaceIn(BaseModel):
    name: str
    start_ts_utc: int
    end_ts_utc: Optional[int] = None


class Race(BaseModel):
    id: int
    name: str
    start_ts_utc: int
    end_ts_utc: Optional[int]
    created_at_utc: int


class EntrantScore(BaseModel):
    tag: str
    display_name: Optional[str] = None
    org: Optional[str] = None
    laps: int
    last_ts_utc: Optional[int] = None
    position: Optional[int] = None
    gap_laps: Optional[int] = None
    gap_time_ms: Optional[int] = None


class RaceState(BaseModel):
    race: Race
    total_participants: int
    total_laps: int
    last_update_utc: Optional[int]
    standings: List[EntrantScore]


###############################################################################
# FastAPI app
###############################################################################

app = FastAPI(title="PRS Race Timing API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Static UI mount (spectator.html lives in ./static/)
BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "static"
app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")


# Serve the spectator UI (set PRS_UI_DIR to point at the folder with spectator.html)
UI_DIR = Path(os.environ.get("PRS_UI_DIR", Path(__file__).parent))
#app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")


###############################################################################
# Utility: dict row → typed model helpers
###############################################################################

def _row_to_race(r: sqlite3.Row) -> Race:
    return Race(
        id=r["id"], name=r["name"], start_ts_utc=r["start_ts_utc"], end_ts_utc=r["end_ts_utc"], created_at_utc=r["created_at_utc"]
    )


def _row_to_lap(r: sqlite3.Row) -> Lap:
    import json

    meta = None
    if r["meta_json"]:
        try:
            meta = json.loads(r["meta_json"])
        except Exception:
            meta = None
    return Lap(
        id=r["id"], race_id=r["race_id"], tag=r["tag"], ts_utc=r["ts_utc"], source=r["source"], device_id=r["device_id"], meta=meta, created_at_utc=r["created_at_utc"],
    )


###############################################################################
# Core endpoints
###############################################################################

@app.get("/laps", response_model=List[Lap])
def get_laps(
    race_id: int = Query(..., ge=1),
    tag: Optional[str] = Query(None),
    since_id: Optional[int] = Query(None, description="Return laps where id > since_id"),
    limit: int = Query(200, ge=1, le=2000),
):
    """Fetch recent laps (optionally filtered by tag and since_id).
    Sorted ascending by id for stable incremental consumption.
    """
    import json

    conn = get_conn()
    try:
        # Primary: laps table
        sql = "SELECT * FROM laps WHERE race_id=?"
        params: List[Any] = [race_id]
        if tag:
            sql += " AND tag=?"
            params.append(tag)
        if since_id is not None:
            sql += " AND id>?"
            params.append(since_id)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        if rows:
            return [_row_to_lap(r) for r in rows]

        # Fallback: synthesize Lap objects from passes
        psql = (
            "SELECT id, race_id, tag, ts_utc, source, device_id, meta_json, created_at_utc "
            "FROM passes WHERE race_id=?"
        )
        pparams: List[Any] = [race_id]
        if tag:
            psql += " AND tag=?"
            pparams.append(tag)
        if since_id is not None:
            psql += " AND id>?"
            pparams.append(since_id)
        psql += " ORDER BY id ASC LIMIT ?"
        pparams.append(limit)

        prows = conn.execute(psql, pparams).fetchall()

        synth: List[Lap] = []
        for r in prows:
            meta = None
            if r["meta_json"]:
                try:
                    meta = json.loads(r["meta_json"])
                except Exception:
                    meta = None
            synth.append(
                Lap(
                    id=r["id"],
                    race_id=r["race_id"],
                    tag=r["tag"],
                    ts_utc=r["ts_utc"],
                    source=r["source"],
                    device_id=r["device_id"],
                    meta=meta,
                    created_at_utc=r["created_at_utc"],
                )
            )
        return synth
    finally:
        conn.close()



@app.post("/laps", response_model=Lap)
def add_lap(lap: LapIn):
    """Insert a new lap. Server supplies trusted ts_utc if not provided via ts_client_utc."""
    import json

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    ts_utc = lap.ts_client_utc or now_ms

    conn = get_conn()
    try:
        # Ensure race exists
        r = conn.execute("SELECT id FROM races WHERE id=?", (lap.race_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="race_id not found")

        conn.execute(
            "INSERT INTO laps(race_id,tag,ts_utc,source,device_id,meta_json,created_at_utc) VALUES(?,?,?,?,?,?,?)",
            (
                lap.race_id,
                lap.tag,
                ts_utc,
                lap.source,
                lap.device_id,
                json.dumps(lap.meta) if lap.meta else None,
                now_ms,
            ),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        row = conn.execute("SELECT * FROM laps WHERE id=?", (new_id,)).fetchone()
        return _row_to_lap(row)
    finally:
        conn.close()


@app.get("/race/state", response_model=RaceState)
def race_state(race_id: int = Query(..., ge=1)):
    """Aggregate scoring for a race.

    Source priority:
      1) `passes` + `transponders` (decoder pipeline)
      2) fallback to `laps` (manual pipeline)

    Scoring & tie-breaks:
      - Sort by total laps DESC (counted at or before checkered cutoff)
      - Tie-break by earlier last crossing time (ASC)
      - Gaps: leader reference — lap deficit; same-lap time gap

    Checkered cutoff:
      - If races.end_ts_utc is set, only count crossings with ts_utc <= end_ts_utc.
      - If not set, include all (live mode).
    """
    conn = get_conn()
    try:
        rrow = conn.execute("SELECT * FROM races WHERE id=?", (race_id,)).fetchone()
        if not rrow:
            raise HTTPException(status_code=404, detail="race_id not found")
        race = _row_to_race(rrow)

        cutoff = rrow["end_ts_utc"]  # may be None

        def _where_cutoff(alias: str) -> str:
            return f" AND {alias}.ts_utc <= ?" if cutoff is not None else ""

        def _cutoff_params() -> List[Any]:
            return [cutoff] if cutoff is not None else []

        # Prefer decoder pipeline
        has_passes = conn.execute("SELECT 1 FROM passes WHERE race_id=? LIMIT 1", (race_id,)).fetchone()
        if has_passes:
            # Map tag -> (org, display_name)
            tmap = {
                r["tag"]: {"org": r["org"], "display_name": r["display_name"]}
                for r in conn.execute(
                    "SELECT tag, org, display_name FROM transponders WHERE race_id=?", (race_id,)
                ).fetchall()
            }
            sql = (
                "SELECT p.tag, COUNT(*) AS laps, MAX(p.ts_utc) AS last_ts_utc "
                "FROM passes p WHERE p.race_id=?" + _where_cutoff("p") + " GROUP BY p.tag"
            )
            rows = conn.execute(sql, [race_id, *_cutoff_params()]).fetchall()
            standings: List[EntrantScore] = []
            for row in rows:
                tag = row["tag"]
                info = tmap.get(tag, {})
                standings.append(
                    EntrantScore(
                        tag=tag,
                        display_name=info.get("display_name"),
                        org=info.get("org"),
                        laps=row["laps"],
                        last_ts_utc=row["last_ts_utc"],
                    )
                )
            # Sort and assign
            standings.sort(key=lambda e: (-e.laps, e.last_ts_utc or 2**63 - 1))
            if standings:
                leader = standings[0]
                for i, e in enumerate(standings, start=1):
                    e.position = i
                    e.gap_laps = leader.laps - e.laps
                    if e.gap_laps == 0 and e.last_ts_utc is not None and leader.last_ts_utc is not None:
                        e.gap_time_ms = e.last_ts_utc - leader.last_ts_utc
            total_laps = conn.execute(
                "SELECT COUNT(*) AS c FROM passes p WHERE p.race_id=?" + _where_cutoff("p"),
                [race_id, *_cutoff_params()],
            ).fetchone()["c"]
            last_update = conn.execute(
                "SELECT MAX(p.ts_utc) AS m FROM passes p WHERE p.race_id=?" + _where_cutoff("p"),
                [race_id, *_cutoff_params()],
            ).fetchone()["m"]
            total_participants = conn.execute(
                "SELECT COUNT(DISTINCT p.tag) AS c FROM passes p WHERE p.race_id=?" + _where_cutoff("p"),
                [race_id, *_cutoff_params()],
            ).fetchone()["c"]
            return RaceState(
                race=race,
                total_participants=total_participants or 0,
                total_laps=total_laps or 0,
                last_update_utc=last_update,
                standings=standings,
            )

        # Fallback to manual pipeline
        sql = (
            "SELECT l.tag, COUNT(*) AS laps, MAX(l.ts_utc) AS last_ts_utc "
            "FROM laps l WHERE l.race_id=?" + _where_cutoff("l") + " GROUP BY l.tag"
        )
        rows = conn.execute(sql, [race_id, *_cutoff_params()]).fetchall()
        standings = [
            EntrantScore(tag=row["tag"], display_name=None, org=None, laps=row["laps"], last_ts_utc=row["last_ts_utc"])  # noqa
            for row in rows
        ]
        standings.sort(key=lambda e: (-e.laps, e.last_ts_utc or 2**63 - 1))
        if standings:
            leader = standings[0]
            for i, e in enumerate(standings, start=1):
                e.position = i
                e.gap_laps = leader.laps - e.laps
                if e.gap_laps == 0 and e.last_ts_utc is not None and leader.last_ts_utc is not None:
                    e.gap_time_ms = e.last_ts_utc - leader.last_ts_utc
        total_laps = conn.execute(
            "SELECT COUNT(*) AS c FROM laps l WHERE l.race_id=?" + _where_cutoff("l"),
            [race_id, *_cutoff_params()],
        ).fetchone()["c"]
        last_update = conn.execute(
            "SELECT MAX(l.ts_utc) AS m FROM laps l WHERE l.race_id=?" + _where_cutoff("l"),
            [race_id, *_cutoff_params()],
        ).fetchone()["m"]
        total_participants = conn.execute(
            "SELECT COUNT(DISTINCT l.tag) AS c FROM laps l WHERE l.race_id=?" + _where_cutoff("l"),
            [race_id, *_cutoff_params()],
        ).fetchone()["c"]
        return RaceState(
            race=race,
            total_participants=total_participants or 0,
            total_laps=total_laps or 0,
            last_update_utc=last_update,
            standings=standings,
        )
    finally:
        conn.close()


###############################################################################
# Admin + dummy loaders (for testing / demos)
###############################################################################

@app.post("/admin/races", response_model=Race)
def create_race(race: RaceIn):
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO races(name,start_ts_utc,end_ts_utc,created_at_utc) VALUES(?,?,?,?)",
            (race.name, race.start_ts_utc, race.end_ts_utc, now_ms),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        row = conn.execute("SELECT * FROM races WHERE id=?", (new_id,)).fetchone()
        conn.commit()
        return _row_to_race(row)
    finally:
        conn.close()


class DummyLoadRequest(BaseModel):
    race_id: int
    entrants: int = Field(10, ge=1, le=500)
    min_laps: int = Field(3, ge=0, le=500)
    max_laps: int = Field(12, ge=0, le=2000)
    spread_minutes: int = Field(60, ge=1, le=24*60)


@app.post("/admin/dummy_load")
def dummy_load(req: DummyLoadRequest):
    """Seed transponders and random passes for demo/testing."""
    import json

    conn = get_conn()
    try:
        race = conn.execute("SELECT * FROM races WHERE id=?", (req.race_id,)).fetchone()
        if not race:
            raise HTTPException(status_code=404, detail="race_id not found")
        start_ms = race["start_ts_utc"]
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        # Create transponders
        existing_tags = set(
            r["tag"] for r in conn.execute("SELECT tag FROM transponders WHERE race_id=?", (req.race_id,)).fetchall()
        )
        created: List[str] = []
        for i in range(1, req.entrants + 1):
            tag = f"T{1000 + i}"
            while tag in existing_tags:
                tag = f"T{int(tag[1:]) + 1}"
            existing_tags.add(tag)
            display_name = f"Entrant {i}"
            org = None
            conn.execute(
                "INSERT INTO transponders(race_id,tag,org,display_name,created_at_utc) VALUES(?,?,?,?,?)",
                (req.race_id, tag, org, display_name, now_ms),
            )
            created.append(tag)

        # Random passes per tag
        for tag in created:
            n_laps = random.randint(req.min_laps, req.max_laps)
            spread_ms = req.spread_minutes * 60 * 1000
            base_gap = spread_ms // max(n_laps, 1)
            t = start_ms + random.randint(0, base_gap)
            for _ in range(n_laps):
                jitter = random.randint(-base_gap // 4, base_gap // 4)
                t += base_gap + jitter
                conn.execute(
                    "INSERT INTO passes(race_id,tag,ts_utc,source,device_id,meta_json,created_at_utc) VALUES(?,?,?,?,?,?,?)",
                    (req.race_id, tag, max(t, start_ms), "dummy", "loader", None, now_ms),
                )
        conn.commit()
        return {"status": "ok", "transponders_created": len(created)}
    finally:
        conn.close()


###############################################################################
# Convenience endpoint: expose schema (read-only) for tooling visibility
###############################################################################

@app.get("/admin/schema.sql", response_model=str)
def get_schema_sql():
    return SQL_SCHEMA


###############################################################################
# Simple health check
###############################################################################

@app.get("/healthz")
def healthz():
    return {"ok": True, "db": DB_PATH}

# Alias so both /healthz and /health work
@app.get("/health")
def health():
    return healthz()

    # If healthz() is NOT async in your file, use this instead:
    # return healthz()
#
#@app.get("/health")
#def health():
#    # if your /healthz is async, call it; otherwise just return a simple OK
#    try:
#        return healthz()   # reuse the existing logic
#    except NameError:
#        return {"ok": True}

@app.get("/debug/static-path")
async def debug_static_path():
    return {"static_dir": str(STATIC_DIR), "exists": STATIC_DIR.exists()}


##