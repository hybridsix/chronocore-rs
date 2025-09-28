"""
PRS Race Timing — Backend (FastAPI + SQLite)

Run:
    uvicorn server:app --reload

Env:
    DB_PATH=/absolute/path/to/laps.sqlite
"""
from __future__ import annotations
from pathlib import Path
import json
import os
import time
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException
from sqlite3 import Row
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from starlette.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Paths / DB
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
## (Don’t bind a global path at import time; always resolve via get_db_path())

# Static UI
UI_DIR = BASE_DIR / "static"
UI_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Schema (production tables only)
# ---------------------------------------------------------------------------
SQL_SCHEMA = r"""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS races (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    start_ts_utc    INTEGER NOT NULL,   -- epoch ms
    end_ts_utc      INTEGER,            -- nullable (checkered cutoff)
    created_at_utc  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transponders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id         INTEGER NOT NULL,
    tag             TEXT NOT NULL,       -- transponder UID
    org             TEXT,                -- team/org (optional)
    display_name    TEXT,                -- driver/label (optional)
    created_at_utc  INTEGER NOT NULL,
    UNIQUE(race_id, tag),
    FOREIGN KEY(race_id) REFERENCES races(id) ON DELETE CASCADE
);

-- Raw decoder pipeline: every line crossing (server-trusted time)
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

-- Manual pipeline (optional)
CREATE TABLE IF NOT EXISTS laps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id         INTEGER NOT NULL,
    tag             TEXT NOT NULL,
    ts_utc          INTEGER NOT NULL,    -- epoch ms
    source          TEXT DEFAULT 'manual',
    device_id       TEXT,
    meta_json       TEXT,
    created_at_utc  INTEGER NOT NULL,
    FOREIGN KEY(race_id) REFERENCES races(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_laps_race_tag_ts ON laps(race_id, tag, ts_utc);
CREATE INDEX IF NOT EXISTS idx_laps_race_ts ON laps(race_id, ts_utc);

-- Live race status for UIs (clock/flag)
CREATE TABLE IF NOT EXISTS race_state (
  race_id INTEGER PRIMARY KEY,
  started_at_utc TEXT,
  clock_ms INTEGER,
  flag TEXT,
  running INTEGER DEFAULT 0,
  race_type TEXT,       -- "sprint", "endurance", ...
  sim INTEGER,          -- 0/1 badge for simulator
  sim_label TEXT,       -- e.g. "SIMULATOR ACTIVE"
  source TEXT           -- e.g. "sim", "decoder"
);
-- Simple KV store (optional)
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

def get_db_path() -> str:
    # 1) Env var wins
    env = os.getenv("DB_PATH")
    if env:
        return env
    # 2) Repo-root default
    repo_root = Path(__file__).resolve().parent.parent
    return str(repo_root / "laps.sqlite")

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), detect_types=sqlite3.PARSE_DECLTYPES)   
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SQL_SCHEMA)
        conn.commit()
    finally:
        conn.close()

init_db()

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class LapIn(BaseModel):
    race_id: int = Field(..., ge=1)
    tag: str
    ts_client_utc: Optional[int] = Field(None, description="Epoch ms (optional)")
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
    # live clock/flag from race_state (optional)
    flag: Optional[str] = None
    clock_ms: Optional[int] = None
    running: Optional[bool] = None
    race_type: Optional[str] = None
    sim: Optional[bool] = None
    sim_label: Optional[str] = None
    source: Optional[str] = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="PRS Race Timing API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static UI
app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _row_to_race(r: sqlite3.Row) -> Race:
    return Race(
        id=r["id"],
        name=r["name"],
        start_ts_utc=r["start_ts_utc"],
        end_ts_utc=r["end_ts_utc"],
        created_at_utc=r["created_at_utc"],
    )

def _row_to_lap(r: sqlite3.Row) -> Lap:
    meta = None
    if r["meta_json"]:
        try:
            meta = json.loads(r["meta_json"])
        except Exception:
            meta = None
    return Lap(
        id=r["id"],
        race_id=r["race_id"],
        tag=r["tag"],
        ts_utc=r["ts_utc"],
        source=r["source"],
        device_id=r["device_id"],
        meta=meta,
        created_at_utc=r["created_at_utc"],
    )

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/passes")
def get_passes(
    race_id: int = Query(..., ge=1),
    limit: int = Query(200, ge=1, le=1000),
) -> Dict[str, Any]:
    """Debug endpoint: recent raw passes (decoder pipeline)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,race_id,tag,ts_utc,source,device_id,meta_json,created_at_utc "
            "FROM passes WHERE race_id=? ORDER BY id DESC LIMIT ?",
            (race_id, limit),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            if d.get("meta_json"):
                try:
                    d["meta"] = json.loads(d.pop("meta_json"))
                except Exception:
                    d["meta"] = None
            else:
                d["meta"] = None
            items.append(d)
        return {"count": len(items), "items": items}
    finally:
        conn.close()

@app.get("/laps")
def leaderboard(
    race_id: int = Query(..., ge=1),
    min_lap_seconds: float = Query(3.0, ge=0.0, description="Filter out < this lap time"),
    include_debug: bool = Query(False, description="Include recent raw passes for debugging"),
) -> Dict[str, Any]:
    """
    Build leaderboard from `passes` using ts_utc deltas per tag.

    Returns:
      { count, items?, rows: [{tag, team, laps, last, best}] }
    """
    conn = get_conn()
    try:
        # optional recent items for debugging
        items: List[Dict[str, Any]] = []
        if include_debug:
            rows = conn.execute(
                "SELECT id,race_id,tag,ts_utc,source,device_id,meta_json,created_at_utc "
                "FROM passes WHERE race_id=? ORDER BY id DESC LIMIT 200",
                (race_id,),
            ).fetchall()
            for r in rows:
                d = dict(r)
                if d.get("meta_json"):
                    try:
                        d["meta"] = json.loads(d.pop("meta_json"))
                    except Exception:
                        d["meta"] = None
                else:
                    d["meta"] = None
                items.append(d)

        # map tag → (org, display_name)
        tr = conn.execute(
            "SELECT tag, org, display_name FROM transponders WHERE race_id=?",
            (race_id,),
        ).fetchall()
        tmap = {r["tag"]: {"org": r["org"], "display_name": r["display_name"]} for r in tr}

        # compute per-tag laps from time deltas (ms → s)
        sql = """
        WITH ordered AS (
          SELECT p.tag, p.ts_utc,
                 (p.ts_utc - LAG(p.ts_utc) OVER (PARTITION BY p.tag ORDER BY p.ts_utc)) / 1000.0 AS lap_time
          FROM passes p
          WHERE p.race_id=?
        ),
        good_laps AS (
          SELECT tag, ts_utc, lap_time
          FROM ordered
          WHERE lap_time IS NOT NULL AND lap_time >= ?
        ),
        last_lap AS (
          SELECT g1.tag, g1.lap_time AS last
          FROM good_laps g1
          JOIN (
            SELECT tag, MAX(ts_utc) AS mx
            FROM good_laps GROUP BY tag
          ) m ON m.tag = g1.tag AND m.mx = g1.ts_utc
        ),
        best_lap AS (
          SELECT tag, MIN(lap_time) AS best
          FROM good_laps GROUP BY tag
        ),
        lap_counts AS (
          SELECT tag, COUNT(*) AS laps
          FROM good_laps GROUP BY tag
        )
        SELECT
          t.tag                           AS tag,
          COALESCE(t.display_name, NULL)  AS display_name,
          COALESCE(t.org, NULL)           AS org,
          COALESCE(c.laps, 0)             AS laps,
          COALESCE(l.last, NULL)          AS last,
          COALESCE(b.best, NULL)          AS best
        FROM (SELECT DISTINCT tag FROM passes WHERE race_id=?) tc
        LEFT JOIN transponders t ON t.race_id=? AND t.tag=tc.tag
        LEFT JOIN lap_counts c   ON c.tag=tc.tag
        LEFT JOIN last_lap l     ON l.tag=tc.tag
        LEFT JOIN best_lap b     ON b.tag=tc.tag
        """
        rows = conn.execute(sql, (race_id, float(min_lap_seconds), race_id, race_id)).fetchall()

        # shape + sort
        leaderboard_rows = []
        for r in rows:
            leaderboard_rows.append(
                {
                    "tag": r["tag"],
                    "team": r["org"],
                    "driver": r["display_name"],
                    "laps": int(r["laps"]),
                    "last": None if r["last"] is None else float(r["last"]),
                    "best": None if r["best"] is None else float(r["best"]),
                }
            )
        from math import inf
        leaderboard_rows.sort(
            key=lambda rec: (-rec["laps"], (rec["best"] if rec["best"] is not None else inf), rec["tag"])
        )

        return {
            "count": len(items),
            "items": items if include_debug else [],
            "rows": leaderboard_rows,
        }
    finally:
        conn.close()

@app.post("/laps", response_model=Lap)
def add_lap(lap: LapIn) -> Lap:
    """Insert a manual lap into the `laps` table."""
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    ts_utc = lap.ts_client_utc or now_ms
    conn = get_conn()
    try:
        r = conn.execute("SELECT id FROM races WHERE id=?", (lap.race_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="race_id not found")
        conn.execute(
            "INSERT INTO laps(race_id,tag,ts_utc,source,device_id,meta_json,created_at_utc) VALUES(?,?,?,?,?,?,?)",
            (lap.race_id, lap.tag, ts_utc, lap.source, lap.device_id,
             json.dumps(lap.meta) if lap.meta else None, now_ms),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        row = conn.execute("SELECT * FROM laps WHERE id=?", (new_id,)).fetchone()
        return _row_to_lap(row)
    finally:
        conn.close()

@app.get("/race/state", response_model=RaceState)
def race_state(race_id: int = Query(..., ge=1)) -> RaceState:
    """
    Aggregate standings from `passes` (fallback to `laps` if needed),
    and include live clock/flag if present in `race_state`.
    """
    conn = get_conn()
    try:
        rrow = conn.execute("SELECT * FROM races WHERE id=?", (race_id,)).fetchone()
        if not rrow:
            raise HTTPException(status_code=404, detail="race_id not found")
        race = _row_to_race(rrow)
        cutoff = rrow["end_ts_utc"]

        def cutoff_clause(alias: str) -> str:
            return f" AND {alias}.ts_utc <= ?" if cutoff is not None else ""

        def cutoff_params() -> List[Any]:
            return [cutoff] if cutoff is not None else []

        # Prefer decoder pipeline
        has_passes = conn.execute("SELECT 1 FROM passes WHERE race_id=? LIMIT 1", (race_id,)).fetchone()
        standings: List[EntrantScore] = []
        total_participants = 0
        total_laps = 0
        last_update = None

        if has_passes:
            tmap = {
                r["tag"]: {"org": r["org"], "display_name": r["display_name"]}
                for r in conn.execute(
                    "SELECT tag, org, display_name FROM transponders WHERE race_id=?", (race_id,)
                ).fetchall()
            }
            rows = conn.execute(
                "SELECT p.tag, COUNT(*) AS laps, MAX(p.ts_utc) AS last_ts_utc "
                "FROM passes p WHERE p.race_id=?" + cutoff_clause("p") + " GROUP BY p.tag",
                [race_id, *cutoff_params()],
            ).fetchall()
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
            standings.sort(key=lambda e: (-e.laps, e.last_ts_utc or 2**63 - 1))
            if standings:
                leader = standings[0]
                for i, e in enumerate(standings, start=1):
                    e.position = i
                    e.gap_laps = leader.laps - e.laps
                    if e.gap_laps == 0 and e.last_ts_utc is not None and leader.last_ts_utc is not None:
                        e.gap_time_ms = e.last_ts_utc - leader.last_ts_utc

            total_laps = conn.execute(
                "SELECT COUNT(*) AS c FROM passes p WHERE p.race_id=?" + cutoff_clause("p"),
                [race_id, *cutoff_params()],
            ).fetchone()["c"]
            last_update = conn.execute(
                "SELECT MAX(p.ts_utc) AS m FROM passes p WHERE p.race_id=?" + cutoff_clause("p"),
                [race_id, *cutoff_params()],
            ).fetchone()["m"]
            total_participants = conn.execute(
                "SELECT COUNT(DISTINCT p.tag) AS c FROM passes p WHERE p.race_id=?" + cutoff_clause("p"),
                [race_id, *cutoff_params()],
            ).fetchone()["c"]
        else:
            # Fallback to manual laps
            rows = conn.execute(
                "SELECT l.tag, COUNT(*) AS laps, MAX(l.ts_utc) AS last_ts_utc "
                "FROM laps l WHERE l.race_id=?" + cutoff_clause("l") + " GROUP BY l.tag",
                [race_id, *cutoff_params()],
            ).fetchall()
            for row in rows:
                standings.append(
                    EntrantScore(
                        tag=row["tag"],
                        display_name=None,
                        org=None,
                        laps=row["laps"],
                        last_ts_utc=row["last_ts_utc"],
                    )
                )
            standings.sort(key=lambda e: (-e.laps, e.last_ts_utc or 2**63 - 1))
            if standings:
                leader = standings[0]
                for i, e in enumerate(standings, start=1):
                    e.position = i
                    e.gap_laps = leader.laps - e.laps
                    if e.gap_laps == 0 and e.last_ts_utc is not None and leader.last_ts_utc is not None:
                        e.gap_time_ms = e.last_ts_utc - leader.last_ts_utc
            total_laps = conn.execute(
                "SELECT COUNT(*) AS c FROM laps l WHERE l.race_id=?" + cutoff_clause("l"),
                [race_id, *cutoff_params()],
            ).fetchone()["c"]
            last_update = conn.execute(
                "SELECT MAX(l.ts_utc) AS m FROM laps l WHERE l.race_id=?" + cutoff_clause("l"),
                [race_id, *cutoff_params()],
            ).fetchone()["m"]
            total_participants = conn.execute(
                "SELECT COUNT(DISTINCT l.tag) AS c FROM laps l WHERE l.race_id=?" + cutoff_clause("l"),
                [race_id, *cutoff_params()],
            ).fetchone()["c"]

        # live status (optional)
        rs = conn.execute(
            "SELECT started_at_utc, clock_ms, flag, running, race_type, sim, sim_label, source "
            "FROM race_state WHERE race_id=?",
            (race_id,),
        ).fetchone()

        flag = clock_ms = running = None
        race_type = sim = sim_label = source = None

        if rs:
            flag = rs["flag"]
            clock_ms = rs["clock_ms"]
            running = bool(rs["running"]) if rs["running"] is not None else None
            race_type = rs["race_type"]
            sim = bool(rs["sim"]) if rs["sim"] is not None else None
            sim_label = rs["sim_label"]
            source = rs["source"]

        return RaceState(
            race=_row_to_race(rrow),
            total_participants=total_participants or 0,
            total_laps=total_laps or 0,
            last_update_utc=last_update,
            standings=standings,
            flag=flag,
            clock_ms=clock_ms,
            running=running,
            race_type=race_type,
            sim=sim,
            sim_label=sim_label,
            source=source,
        )
    
    finally:
        conn.close()

# ---------------------------- Admin / Utilities -----------------------------

@app.post("/admin/races", response_model=Race)
def create_race(race: RaceIn) -> Race:
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

@app.get("/admin/schema.sql", response_model=str)
def get_schema_sql() -> str:
    return SQL_SCHEMA

# Health
@app.get("/healthz")
def healthz():
    try:
        db = sqlite3.connect(get_db_path())
        db.execute("SELECT 1")
        return {"ok": True, "db_path": get_db_path()}
    except Exception as e:
        return {"ok": False, "error": str(e), "db_path": get_db_path()}

@app.get("/health")
def health() -> Dict[str, Any]:
    return healthz()
