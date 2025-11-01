"""
backend/app_results_api.py
--------------------------
FastAPI router exposing frozen results and export utilities.

Responsibilities
----------------
- Serve JSON representations of frozen standings and lap history.
- Provide CSV exports for operators to download race results.
- Share the unified SQLite database location via config_loader.
"""

from fastapi import APIRouter, HTTPException, Response
import sqlite3
import csv
import io
from typing import Dict, Any, List

from .config_loader import get_db_path

router = APIRouter()

# Unified config drives the SQLite location so exports stay in lockstep with the engine.
def _conn() -> sqlite3.Connection:
    return sqlite3.connect(str(get_db_path()))

@router.get("/results/{race_id}")
def get_results(race_id: int) -> Dict[str, Any]:
    """Return frozen standings for the given race_id."""
    with _conn() as cx:
        meta = cx.execute("SELECT race_type, frozen_utc, duration_ms FROM result_meta WHERE race_id=?", (race_id,)).fetchone()
        if not meta:
            raise HTTPException(status_code=404, detail="No frozen results for this race_id")
        race_type, frozen_utc, duration_ms = meta

        rows = cx.execute(
            """SELECT position, entrant_id, number, name, laps, last_ms, best_ms, gap_ms, lap_deficit, pit_count, status
               FROM result_standings
               WHERE race_id=?
               ORDER BY position ASC""",
            (race_id,)
        ).fetchall()

        entrants = []
        for r in rows:
            entrants.append({
                "position": r[0], "entrant_id": r[1], "number": r[2], "name": r[3],
                "laps": r[4], "last_ms": r[5], "best_ms": r[6], "gap_ms": r[7],
                "lap_deficit": r[8], "pit_count": r[9], "status": r[10],
            })

        return {
            "race_id": race_id,
            "race_type": race_type,
            "frozen_utc": frozen_utc,
            "duration_ms": duration_ms,
            "entrants": entrants,
        }

@router.get("/results/{race_id}/laps")
def get_results_laps(race_id: int) -> Dict[str, Any]:
    """Return lap-by-lap breakdown for entrants in the frozen snapshot."""
    with _conn() as cx:
        row = cx.execute("SELECT 1 FROM result_meta WHERE race_id=?", (race_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No frozen results for this race_id")

        laps_map: Dict[str, List[int]] = {}
        for entrant_id, lap_no, lap_ms in cx.execute(
            "SELECT entrant_id, lap_no, lap_ms FROM result_laps WHERE race_id=? ORDER BY entrant_id, lap_no",
            (race_id,)
        ):
            laps_map.setdefault(str(entrant_id), []).append(lap_ms)
        return {"race_id": race_id, "laps": laps_map}

@router.get("/export/results_csv")
def export_results_csv(race_id: int):
    """Generate a CSV export of standings (ordered by position)."""
    with _conn() as cx:
        cur = cx.execute(
            """SELECT position, entrant_id, number, name, laps, last_ms, best_ms, gap_ms, lap_deficit, pit_count, status
               FROM result_standings WHERE race_id=? ORDER BY position""",
            (race_id,)
        )
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Position","Entrant ID","Number","Name","Laps","Last (ms)","Best (ms)","Gap (ms)","Lap Deficit","Pits","Status"])
        w.writerows(cur.fetchall())
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="results_{race_id}.csv"'})

@router.get("/export/laps_csv")
def export_laps_csv(race_id: int):
    """Generate lap history CSV (per entrant, ordered by race position)."""
    with _conn() as cx:
        rows = cx.execute(
            """SELECT s.position, s.entrant_id, s.number, s.name, l.lap_no, l.lap_ms
               FROM result_standings s
               JOIN result_laps l USING (race_id, entrant_id)
               WHERE s.race_id=?
               ORDER BY s.position, l.lap_no""",
            (race_id,)
        ).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Position","Entrant ID","Number","Name","Lap #","Lap (ms)"])
        w.writerows(rows)
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="laps_{race_id}.csv"'})