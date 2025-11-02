"""
app_results_api.py
------------------
FastAPI router exposing frozen race results from SQLite.

This module stays read-only: it upgrades historical `result_meta` tables to
include the new human context fields and serves REST responses for UI rails
and exports. Connections are short-lived and created per request to avoid
holding locks on the results database.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException

# ---------------------------------------------------------------------------
# DB helpers (use your existing get_db_path if you have one)
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    # Keep your existing logic if different
    return Path("backend/db/laps.sqlite")

def _connect() -> sqlite3.Connection:
    cx = sqlite3.connect(str(get_db_path()))
    cx.row_factory = sqlite3.Row
    return cx

def _table_has_col(db: sqlite3.Connection, table: str, col: str) -> bool:
    for row in db.execute(f"PRAGMA table_info({table})").fetchall():
        if row["name"] == col:
            return True
    return False

def _ensure_result_schema(db: sqlite3.Connection) -> None:
    # Ensure result tables exist (lightweight, safe if they already do)
    db.execute("""
        CREATE TABLE IF NOT EXISTS result_meta (
            race_id          INTEGER PRIMARY KEY,
            race_type        TEXT,
            frozen_utc       INTEGER,
            duration_ms      INTEGER,
            clock_ms_frozen  INTEGER
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS result_standings (
            race_id     INTEGER,
            position    INTEGER,
            entrant_id  INTEGER,
            number      TEXT,
            name        TEXT,
            laps        INTEGER,
            last_ms     INTEGER,
            best_ms     INTEGER,
            gap_ms      INTEGER,
            lap_deficit INTEGER,
            pit_count   INTEGER,
            status      TEXT,
            PRIMARY KEY(race_id, position)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS result_laps (
            race_id    INTEGER,
            entrant_id INTEGER,
            lap_no     INTEGER,
            lap_ms     INTEGER,
            PRIMARY KEY(race_id, entrant_id, lap_no)
        )
    """)

    # NEW columns on result_meta (idempotent ALTERs) to support richer exports
    new_cols = [
        ("event_label",      "TEXT"),
        ("session_label",    "TEXT"),
        ("race_mode",        "TEXT"),
        ("frozen_iso_utc",   "TEXT"),
        ("frozen_iso_local", "TEXT"),
    ]
    for col, typ in new_cols:
        if not _table_has_col(db, "result_meta", col):
            # Each ALTER executes at most once; safe to run on every request.
            db.execute(f"ALTER TABLE result_meta ADD COLUMN {col} {typ}")

    db.commit()

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/results", tags=["results"])

def _meta_row_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
    # Always include new keys; None if the column was missing on legacy DBs.
    return {
        "race_id":          int(r["race_id"]),
        "race_type":        r["race_type"],
        "frozen_utc":       int(r["frozen_utc"]) if r["frozen_utc"] is not None else None,
        "duration_ms":      int(r["duration_ms"]) if r["duration_ms"] is not None else None,
        "clock_ms_frozen":  int(r["clock_ms_frozen"]) if r["clock_ms_frozen"] is not None else None,
        "event_label":      r["event_label"] if "event_label" in r.keys() else None,
        "session_label":    r["session_label"] if "session_label" in r.keys() else None,
        "race_mode":        r["race_mode"] if "race_mode" in r.keys() else None,
        "frozen_iso_utc":   r["frozen_iso_utc"] if "frozen_iso_utc" in r.keys() else None,
        "frozen_iso_local": r["frozen_iso_local"] if "frozen_iso_local" in r.keys() else None,
    }

@router.get("/recent")
def list_recent(limit: int = 12) -> Dict[str, Any]:
    with _connect() as db:
        _ensure_result_schema(db)
        rows = db.execute(
            """
            SELECT race_id, race_type, frozen_utc, duration_ms, clock_ms_frozen,
                   event_label, session_label, race_mode, frozen_iso_utc, frozen_iso_local
            FROM result_meta
            ORDER BY COALESCE(frozen_utc, 0) DESC, race_id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        heats = []
        for r in rows:
            meta = _meta_row_to_dict(r)
            # Operator results rail expects a compact summary per frozen heat.
            heats.append({
                "heat_id":       meta["race_id"],
                "name":          meta["race_type"] or "-",
                "status":        "CHECKERED",  # frozen set implies finalized
                "finished_utc":  meta["frozen_iso_utc"],   # ISO string (UTC)
                "started_utc":   None,                      # unknown at freeze
                "laps_count":    None,                      # optional aggregate
                "entrant_count": None,                      # optional aggregate
                # NEW rail ornaments
                "event_label":      meta["event_label"],
                "session_label":    meta["session_label"],
                "race_mode":        meta["race_mode"] or meta["race_type"],
                "frozen_iso_local": meta["frozen_iso_local"],
            })
        return {"heats": heats}

@router.get("/{race_id}")
def get_results(race_id: int) -> Dict[str, Any]:
    with _connect() as db:
        _ensure_result_schema(db)

        meta = db.execute(
            """
            SELECT *
            FROM result_meta
            WHERE race_id = ?
            """,
            (race_id,),
        ).fetchone()
        if not meta:
            raise HTTPException(status_code=404, detail="Heat not found")

        st_rows = db.execute(
            """
            SELECT position, entrant_id, number, name, laps, last_ms, best_ms, gap_ms, lap_deficit, pit_count, status
            FROM result_standings
            WHERE race_id = ?
            ORDER BY position ASC
            """,
            (race_id,),
        ).fetchall()

        standings = []
        for r in st_rows:
            # Keep numeric fields as ints; UI formats into s.ms
            standings.append({
                "position":     int(r["position"]),
                "entrant_id":   int(r["entrant_id"]),
                "number":       r["number"],
                "name":         r["name"],
                "laps":         int(r["laps"]),
                "last_ms":      None if r["last_ms"] is None else int(r["last_ms"]),
                "best_ms":      None if r["best_ms"] is None else int(r["best_ms"]),
                "gap_ms":       None if r["gap_ms"]  is None else int(r["gap_ms"]),
                "lap_deficit":  int(r["lap_deficit"]) if r["lap_deficit"] is not None else 0,
                "pit_count":    int(r["pit_count"])   if r["pit_count"]   is not None else 0,
                "status":       r["status"] or "ACTIVE",
            })

        meta_d = _meta_row_to_dict(meta)
        return {
            "race_id":     meta_d["race_id"],
            "race_type":   meta_d["race_type"],
            "frozen_utc":  meta_d["frozen_iso_utc"] or None,   # ISO UTC string if available
            "duration_ms": meta_d["duration_ms"],
            # For completeness, also echo the new meta (UI may show in header later)
            "event_label":      meta_d["event_label"],
            "session_label":    meta_d["session_label"],
            "race_mode":        meta_d["race_mode"] or meta_d["race_type"],
            "frozen_iso_local": meta_d["frozen_iso_local"],
            "entrants":   standings,
        }

@router.get("/{race_id}/laps")
def get_laps(race_id: int) -> Dict[str, Any]:
    with _connect() as db:
        _ensure_result_schema(db)
        # laps as entrant_id → [lap_ms...]
        rows = db.execute(
            """
            SELECT entrant_id, lap_no, lap_ms
            FROM result_laps
            WHERE race_id = ?
            ORDER BY entrant_id ASC, lap_no ASC
            """,
            (race_id,),
        ).fetchall()
        if not rows:
            # Keep behavior: 404 if no heat, empty if no laps? We’ll be strict: 404 only when no meta.
            meta = db.execute("SELECT 1 FROM result_meta WHERE race_id=?", (race_id,)).fetchone()
            if not meta:
                raise HTTPException(status_code=404, detail="Heat not found")
        laps: Dict[str, List[int]] = {}
        for r in rows:
            eid = str(int(r["entrant_id"]))
            laps.setdefault(eid, []).append(int(r["lap_ms"]))
        return {"race_id": race_id, "laps": laps}
