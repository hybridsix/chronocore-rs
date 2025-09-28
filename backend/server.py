"""
PRS Race Timing — Backend API (entrant-based, new schema)

Run:
    uvicorn backend.server:app --reload --host 0.0.0.0 --port 8000

Env:
    DB_PATH = absolute path to laps.sqlite (server and sim must match)

This server:
- Stores a race, entrants (name, car number, org).
- Tracks which TAG is assigned to which entrant, with time windows.
- Writes/reads raw passes by (race_id, tag, ts_utc).
- Builds leaderboard by attributing passes to ENTRANTS (tag can change mid-race).
- Exposes:
    GET /healthz
    GET /passes?race_id=99
    GET /race/state?race_id=99
"""

from __future__ import annotations
import os
import sqlite3
import time
from math import inf
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Paths / DB plumbing
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent          # .../backend
ROOT_DIR = BASE_DIR.parent                          # repo root (contains laps.sqlite)
UI_DIR   = BASE_DIR / "static"                      # static UI at /ui
UI_DIR.mkdir(exist_ok=True)

def get_db_path() -> str:
    """Use DB_PATH if set; else repo-root/laps.sqlite."""
    env = os.getenv("DB_PATH")
    return env if env else str(ROOT_DIR / "laps.sqlite")

async def ensure_schema(db: aiosqlite.Connection) -> None:
    """
    Single source of truth for our schema.
    NOTE: You said it's fine to wipe on schema changes — if you see errors,
    delete laps.sqlite and restart the server to recreate it cleanly.
    """
    await db.executescript(r"""
PRAGMA journal_mode=WAL;

-- Races: the timeboxed competition unit the UI views (per race_id)
CREATE TABLE IF NOT EXISTS races (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  start_ts_utc    INTEGER NOT NULL,   -- epoch ms
  end_ts_utc      INTEGER,
  created_at_utc  INTEGER NOT NULL
);

-- Entrants: identity of a racer (stable across tag changes)
CREATE TABLE IF NOT EXISTS entrants (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,      -- Racer Name (e.g., "Totes Mah Goats")
  car_num         TEXT,               -- Car Number (TEXT so 007 or 75A work)
  org             TEXT,               -- Team/Org (e.g., "Lazy Gecko")
  created_at_utc  INTEGER NOT NULL
);

-- Entrant registered to a race
CREATE TABLE IF NOT EXISTS race_entries (
  race_id         INTEGER NOT NULL,
  entrant_id      INTEGER NOT NULL,
  PRIMARY KEY (race_id, entrant_id),
  FOREIGN KEY(race_id)   REFERENCES races(id)     ON DELETE CASCADE,
  FOREIGN KEY(entrant_id)REFERENCES entrants(id) ON DELETE CASCADE
);

-- Which TAG belongs to which entrant; time-bounded so swaps are tracked
CREATE TABLE IF NOT EXISTS tag_assignments (
  race_id            INTEGER NOT NULL,
  entrant_id         INTEGER NOT NULL,
  tag                TEXT    NOT NULL,
  effective_from_utc INTEGER NOT NULL,  -- inclusive
  effective_to_utc   INTEGER,           -- exclusive; NULL = still active
  FOREIGN KEY(race_id)   REFERENCES races(id)     ON DELETE CASCADE,
  FOREIGN KEY(entrant_id)REFERENCES entrants(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tag_assign_race_tag_time
  ON tag_assignments(race_id, tag, effective_from_utc, effective_to_utc);

-- Raw line crossings (decoder/sim write here)
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
CREATE INDEX IF NOT EXISTS idx_passes_race_ts     ON passes(race_id, ts_utc);

-- Live overlay (flag, clock, running, etc.) pushed by sim/decoder
CREATE TABLE IF NOT EXISTS race_state (
  race_id          INTEGER PRIMARY KEY,
  started_at_utc   INTEGER,
  clock_ms         INTEGER,
  flag             TEXT,
  running          INTEGER DEFAULT 0,
  race_type        TEXT,
  sim              INTEGER,
  sim_label        TEXT,
  source           TEXT
);
""")
    await db.commit()

# ---------------------------------------------------------------------------
# FastAPI app + CORS + Static
# ---------------------------------------------------------------------------

app = FastAPI(title="PRS Race Timing API", version="0.5.0 (entrant-based)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # loosened for local testing
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")

# ---------------------------------------------------------------------------
# Models used in responses
# ---------------------------------------------------------------------------

class StandingsRow(BaseModel):
    # The fields your UI wants FIRST:
    position: int
    car_number: Optional[str] = None
    name: Optional[str] = None
    laps: int
    last: Optional[float] = None   # seconds
    best: Optional[float] = None   # seconds

    # Useful extras (ignore if not needed in UI):
    entrant_id: int
    org: Optional[str] = None
    team: Optional[str] = None     # alias of org (compat)
    tag: Optional[str] = None      # left None (entrant-based view)

class RaceStateResponse(BaseModel):
    race: Dict[str, Any]
    total_participants: int
    total_laps: int
    last_update_utc: Optional[int]
    standings: List[StandingsRow]
    # overlay for header widgets:
    flag: Optional[str] = None
    clock_ms: Optional[int] = None
    running: Optional[bool] = None
    race_type: Optional[str] = None
    sim: Optional[bool] = None
    sim_label: Optional[str] = None
    source: Optional[str] = None

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    db_path = get_db_path()
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            await db.execute("SELECT 1")
        return {"ok": True, "db_path": db_path}
    except Exception as e:
        return {"ok": False, "error": str(e), "db_path": db_path}

# ---------------------------------------------------------------------------
# Recent raw passes (debug)
# ---------------------------------------------------------------------------

@app.get("/passes")
async def get_passes(
    race_id: int = Query(..., ge=1),
    limit: int = Query(50, ge=1, le=1000),
) -> Dict[str, Any]:
    db_path = get_db_path()
    if not Path(db_path).exists():
        raise HTTPException(status_code=404, detail="Database not found")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        await ensure_schema(db)
        rows: List[Dict[str, Any]] = []
        sql = """
        SELECT p.id, p.race_id, p.tag, p.ts_utc, p.source, p.created_at_utc
        FROM passes p
        WHERE p.race_id = ?
        ORDER BY p.id DESC
        LIMIT ?
        """
        async with db.execute(sql, (race_id, limit)) as cur:
            async for r in cur:
                rows.append(dict(r))
        return {"rows": rows}

# ---------------------------------------------------------------------------
# /race/state — entrant-based leaderboard + live overlay
# ---------------------------------------------------------------------------

@app.get("/race/state", response_model=RaceStateResponse)
async def race_state(
    race_id: int = Query(..., ge=1),
    min_lap_seconds: float = Query(3.0, ge=0.0, description="Ignore laps faster than this")
) -> RaceStateResponse:
    db_path = get_db_path()
    if not Path(db_path).exists():
        raise HTTPException(status_code=404, detail="Database not found")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        await ensure_schema(db)

        # 1) race header
        cur = await db.execute("SELECT * FROM races WHERE id=?", (race_id,))
        race_row = await cur.fetchone()
        await cur.close()
        if not race_row:
            raise HTTPException(status_code=404, detail="race_id not found")
        race = dict(race_row)

        # 2) entrant meta for display (id -> name, car_num, org)
        emeta: Dict[int, Dict[str, Optional[str]]] = {}
        async with db.execute(
            "SELECT e.id, e.name, e.car_num, e.org "
            "FROM entrants e JOIN race_entries re ON re.entrant_id=e.id "
            "WHERE re.race_id=?", (race_id,)
        ) as cur2:
            async for r in cur2:
                emeta[int(r["id"])] = {
                    "name": r["name"], "car": r["car_num"], "org": r["org"]
                }

        # 3) compute laps/last/best for each ENTRANT by attributing passes via tag_assignments
        #    NOTE: SQLite has window functions, so deltas are easy once attributed.
        sql = """
        WITH attributed AS (
          SELECT
            ta.entrant_id,
            p.ts_utc
          FROM passes p
          JOIN tag_assignments ta
            ON ta.race_id = p.race_id
           AND ta.tag     = p.tag
           AND p.ts_utc  >= ta.effective_from_utc
           AND (ta.effective_to_utc IS NULL OR p.ts_utc < ta.effective_to_utc)
          WHERE p.race_id = ?
        ),
        ordered AS (
          SELECT entrant_id, ts_utc,
                 (ts_utc - LAG(ts_utc) OVER (PARTITION BY entrant_id ORDER BY ts_utc)) / 1000.0 AS lap_s
          FROM attributed
        ),
        good AS (
          SELECT entrant_id, ts_utc, lap_s
          FROM ordered
          WHERE lap_s IS NOT NULL AND lap_s >= ?
        ),
        last_lap AS (
          SELECT entrant_id, lap_s AS last
          FROM (
            SELECT entrant_id, lap_s,
                   ROW_NUMBER() OVER (PARTITION BY entrant_id ORDER BY ts_utc DESC) AS rn
            FROM good
          ) q WHERE rn=1
        ),
        best_lap AS (
          SELECT entrant_id, MIN(lap_s) AS best
          FROM good GROUP BY entrant_id
        ),
        lap_counts AS (
          SELECT entrant_id, COUNT(*) AS laps
          FROM good GROUP BY entrant_id
        ),
        all_ids AS (
          SELECT entrant_id FROM lap_counts
          UNION SELECT entrant_id FROM last_lap
          UNION SELECT entrant_id FROM best_lap
        )
        SELECT a.entrant_id,
               COALESCE(lc.laps, 0) AS laps,
               l.last,
               b.best
        FROM all_ids a
        LEFT JOIN lap_counts lc ON lc.entrant_id = a.entrant_id
        LEFT JOIN last_lap   l  ON l.entrant_id  = a.entrant_id
        LEFT JOIN best_lap   b  ON b.entrant_id  = a.entrant_id
        """
        standings_raw: List[Dict[str, Any]] = []
        async with db.execute(sql, (race_id, float(min_lap_seconds))) as cur3:
            async for er in cur3:
                entrant_id = int(er["entrant_id"])
                laps = int(er["laps"] or 0)
                last = None if er["last"] is None else float(er["last"])
                best = None if er["best"] is None else float(er["best"])
                meta = emeta.get(entrant_id, {})
                standings_raw.append({
                    "entrant_id": entrant_id,
                    "name": meta.get("name"),
                    "car_number": meta.get("car"),
                    "org": meta.get("org"),
                    "team": meta.get("org"),
                    "laps": laps,
                    "last": last,
                    "best": best,
                })

        # 4) sort & decorate with positions (Position | Car Num | Racer Name | Laps | Last | Best)
        standings_raw.sort(key=lambda r: (-r["laps"], (r["best"] if r["best"] is not None else inf), r.get("name") or ""))

        standings: List[StandingsRow] = []
        for i, r in enumerate(standings_raw, start=1):
            standings.append(StandingsRow(
                position=i,
                car_number=r["car_number"],
                name=r["name"],
                laps=r["laps"],
                last=r["last"],
                best=r["best"],
                entrant_id=r["entrant_id"],
                org=r["org"],
                team=r["team"],
                tag=None,
            ))

        # 5) totals + last update
        cur4 = await db.execute("SELECT COUNT(DISTINCT entrant_id) FROM race_entries WHERE race_id=?", (race_id,))
        total_participants = int((await cur4.fetchone())[0] or 0)
        await cur4.close()

        cur5 = await db.execute("SELECT SUM(laps) FROM (SELECT entrant_id, COUNT(*) AS laps FROM ("
                                "SELECT ta.entrant_id, p.ts_utc FROM passes p "
                                "JOIN tag_assignments ta ON ta.race_id=p.race_id AND ta.tag=p.tag "
                                "AND p.ts_utc>=ta.effective_from_utc AND (ta.effective_to_utc IS NULL OR p.ts_utc<ta.effective_to_utc) "
                                "WHERE p.race_id=? GROUP BY ta.entrant_id, p.ts_utc) GROUP BY entrant_id)", (race_id,))
        total_laps = int((await cur5.fetchone())[0] or 0)
        await cur5.close()

        cur6 = await db.execute("SELECT MAX(ts_utc) FROM passes WHERE race_id=?", (race_id,))
        last_update = (await cur6.fetchone())[0]
        await cur6.close()

        # 6) live overlay
        cur7 = await db.execute(
            "SELECT started_at_utc, clock_ms, flag, running, race_type, sim, sim_label, source "
            "FROM race_state WHERE race_id=?", (race_id,))
        rs = await cur7.fetchone()
        await cur7.close()
        overlay = dict(zip(
            ["started_at_utc","clock_ms","flag","running","race_type","sim","sim_label","source"],
            rs if rs else [None]*8
        ))

        return RaceStateResponse(
            race=race,
            total_participants=total_participants,
            total_laps=total_laps,
            last_update_utc=int(last_update) if last_update is not None else None,
            standings=standings,
            flag=overlay["flag"],
            clock_ms=overlay["clock_ms"],
            running=bool(overlay["running"]) if overlay["running"] is not None else None,
            race_type=overlay["race_type"],
            sim=bool(overlay["sim"]) if overlay["sim"] is not None else None,
            sim_label=overlay["sim_label"],
            source=overlay["source"],
        )
