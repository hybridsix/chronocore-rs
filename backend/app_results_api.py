"""
backend/app_results_api.py
--------------------------
FastAPI router exposing frozen-results JSON and CSV exports.

Design goals
------------
- Read ONLY from the frozen artifacts written at CHECKERED:
  result_meta, result_standings, result_laps
- No recomputation here; the engine is the source of truth.
- Absolute DB path resolution so API and engine hit the same SQLite file.
- Tolerant laps endpoint: returns {} (not 404) when laps weren’t persisted.
- "Recent results" listing to populate the left rail even if heats/races are empty.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
import pathlib
import sqlite3
import csv
import io
from typing import Dict, Any, List

from .config_loader import get_db_path

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter()


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    """
    Open a SQLite connection to the configured database.
    Always resolve to an absolute path to avoid CWD-related surprises.
    """
    db_abs = str(pathlib.Path(get_db_path()).resolve())
    cx = sqlite3.connect(db_abs)
    return cx


# ---------------------------------------------------------------------------
# Recent frozen results (for left-rail population)
#   GET /results/recent?limit=50
# Returns: {"heats": [{heat_id, name, status, finished_utc, started_utc?, laps_count?, entrant_count?}, ...]}
# ---------------------------------------------------------------------------
@router.get("/results/recent", response_model=None)
def results_recent(limit: int = 50) -> Dict[str, Any]:
    with _conn() as cx:
        cx.row_factory = sqlite3.Row
        rows = cx.execute(
            """
            SELECT race_id, race_type, frozen_utc, duration_ms
            FROM result_meta
            ORDER BY COALESCE(frozen_utc, race_id) DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

        heats: List[Dict[str, Any]] = []
        for r in rows:
            heats.append(
                {
                    # Keep "heat_id" naming for UI compatibility
                    "heat_id": int(r["race_id"]),
                    "name": r["race_type"],
                    "status": "CHECKERED",
                    "finished_utc": r["frozen_utc"],
                    # Not stored for frozen meta today; present for shape compatibility
                    "started_utc": None,
                    "laps_count": None,
                    "entrant_count": None,
                }
            )
        return {"heats": heats}


# ---------------------------------------------------------------------------
# Frozen standings for a race
#   GET /results/{race_id}
# ---------------------------------------------------------------------------
@router.get("/results/{race_id}", response_model=None)
def get_results(race_id: int) -> Dict[str, Any]:
    """Return frozen standings for the given race_id."""
    with _conn() as cx:
        cx.row_factory = sqlite3.Row

        meta = cx.execute(
            "SELECT race_type, frozen_utc, duration_ms FROM result_meta WHERE race_id=?",
            (race_id,),
        ).fetchone()
        if not meta:
            raise HTTPException(status_code=404, detail="No frozen results for this race_id")

        rows = cx.execute(
            """
            SELECT position, entrant_id, number, name, laps, last_ms, best_ms, gap_ms, lap_deficit, pit_count, status
            FROM result_standings
            WHERE race_id=?
            ORDER BY position ASC
            """,
            (race_id,),
        ).fetchall()

        entrants: List[Dict[str, Any]] = []
        for r in rows:
            entrants.append(
                {
                    "position": int(r["position"]),
                    "entrant_id": int(r["entrant_id"]),
                    "number": r["number"],
                    "name": r["name"],
                    "laps": int(r["laps"]),
                    "last_ms": r["last_ms"],
                    "best_ms": r["best_ms"],
                    "gap_ms": r["gap_ms"],
                    "lap_deficit": int(r["lap_deficit"]),
                    "pit_count": int(r["pit_count"]),
                    "status": r["status"],
                }
            )

        return {
            "race_id": race_id,
            "race_type": meta["race_type"],
            "frozen_utc": meta["frozen_utc"],
            "duration_ms": meta["duration_ms"],
            "entrants": entrants,
        }


# ---------------------------------------------------------------------------
# Frozen lap history for a race (tolerant when no laps were saved)
#   GET /results/{race_id}/laps
# Returns: {"race_id": <id>, "laps": { "<entrant_id>": [lap_ms,...], ... }}
# ---------------------------------------------------------------------------
@router.get("/results/{race_id}/laps", response_model=None)
def get_results_laps(race_id: int) -> Dict[str, Any]:
    with _conn() as cx:
        cx.row_factory = sqlite3.Row

        exists = cx.execute(
            "SELECT 1 FROM result_meta WHERE race_id=?",
            (race_id,),
        ).fetchone()
        if not exists:
            # No frozen artifact yet — return empty, not a 404
            return {"race_id": race_id, "laps": {}}

        rows = cx.execute(
            """
            SELECT entrant_id, lap_no, lap_ms
            FROM result_laps
            WHERE race_id=?
            ORDER BY entrant_id, lap_no
            """,
            (race_id,),
        ).fetchall()

        laps_map: Dict[str, List[int]] = {}
        for r in rows:
            k = str(r["entrant_id"])  # keys as strings to keep UI stable
            laps_map.setdefault(k, []).append(int(r["lap_ms"]))

        return {"race_id": race_id, "laps": laps_map}


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------
@router.get("/export/results_csv", response_model=None)
def export_results_csv(race_id: int) -> Response:
    """Download standings CSV (ordered by position)."""
    with _conn() as cx:
        cx.row_factory = sqlite3.Row
        cur = cx.execute(
            """
            SELECT position, entrant_id, number, name, laps, last_ms, best_ms, gap_ms, lap_deficit, pit_count, status
            FROM result_standings
            WHERE race_id=?
            ORDER BY position
            """,
            (race_id,),
        )

        buf = io.StringIO(newline="")
        w = csv.writer(buf)
        w.writerow(
            [
                "Position",
                "Entrant ID",
                "Number",
                "Name",
                "Laps",
                "Last (ms)",
                "Best (ms)",
                "Gap (ms)",
                "Lap Deficit",
                "Pits",
                "Status",
            ]
        )
        for r in cur.fetchall():
            w.writerow(
                [
                    r["position"],
                    r["entrant_id"],
                    r["number"],
                    r["name"],
                    r["laps"],
                    r["last_ms"],
                    r["best_ms"],
                    r["gap_ms"],
                    r["lap_deficit"],
                    r["pit_count"],
                    r["status"],
                ]
            )

        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="results_{race_id}.csv"'},
        )


@router.get("/export/laps_csv", response_model=None)
def export_laps_csv(race_id: int) -> Response:
    """Download per-entrant lap history CSV (ordered by race position, then lap #)."""
    with _conn() as cx:
        cx.row_factory = sqlite3.Row
        rows = cx.execute(
            """
            SELECT s.position, s.entrant_id, s.number, s.name, l.lap_no, l.lap_ms
            FROM result_standings s
            JOIN result_laps l USING (race_id, entrant_id)
            WHERE s.race_id=?
            ORDER BY s.position, l.lap_no
            """,
            (race_id,),
        ).fetchall()

        buf = io.StringIO(newline="")
        w = csv.writer(buf)
        w.writerow(["Position", "Entrant ID", "Number", "Name", "Lap #", "Lap (ms)"])
        for r in rows:
            w.writerow([r["position"], r["entrant_id"], r["number"], r["name"], r["lap_no"], r["lap_ms"]])

        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="laps_{race_id}.csv"'},
        )
