"""
backend/results.py
------------------
Durable persistence helpers for frozen race results.

Responsibilities
----------------
- Accept the authoritative snapshot emitted when the engine throws CHECKERED.
- Persist standings, lap history, and metadata into the `result_*` tables.
- Remain idempotent so repeated freeze attempts do not duplicate rows.
"""

import logging
from pathlib import Path
from datetime import datetime, timezone
import sqlite3
from typing import Any, Dict, List

from .db_schema import ensure_schema
from .config_loader import get_db_path

DB_PATH = get_db_path()
ensure_schema(DB_PATH, recreate=False, include_passes=True)

log = logging.getLogger("ccrs.results")

def persist_results(DB_PATH: str, race_id: int, race_type: str, snapshot: Dict[str, Any], laps_map: Dict[int, List[int]]) -> None:
    """Persist a frozen snapshot into the results tables.

    Parameters
    ----------
    DB_PATH:
        Absolute path to the SQLite database (engine + results share the same file).
    race_id:
        Identifier for the race session being frozen.
    race_type:
        Friendly descriptor ("sprint", "endurance", etc.) stored for exports.
    snapshot:
        Engine-provided snapshot; must include fully ordered `standings` and
        a `clock_ms_frozen` duration stamped at freeze time.
    laps_map:
        Mapping of entrant_id → list of lap durations in milliseconds.

    Returns quietly when results for race_id already exist.
    """
    try:
        resolved = Path(DB_PATH).resolve()
        message = f"persist_results_db={resolved} race_id={race_id}"
        log.info(message)
        logging.getLogger("uvicorn.error").info(message)
    except Exception:
        log.exception("Unable to report persist_results db_path", extra={"race_id": race_id})

    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    standings: List[Dict[str, Any]] = snapshot["standings"]
    duration_ms: int = snapshot["clock_ms_frozen"]  # set by engine at freeze time

    with sqlite3.connect(DB_PATH) as cx:
        cx.execute("PRAGMA journal_mode=WAL;")
        cur = cx.cursor()
        log_ctx = {"race_id": race_id}

        cur.execute(
            "INSERT OR IGNORE INTO result_meta (race_id, race_type, frozen_utc, duration_ms) VALUES (?,?,?,?)",
            (race_id, race_type, now_utc, duration_ms),
        )
        if cur.rowcount == 0:
            log.info("persist_results: already exists; skipping", extra=log_ctx)
            return

        log.info(
            "persist_results_counts",
            extra={
                "race_id": race_id,
                "entrants_with_laps": len(laps_map or {}),
                "total_laps": sum(len(v) for v in (laps_map or {}).values()),
            },
        )

        # Standings
        for pos, e in enumerate(standings, start=1):
            cur.execute(
                """INSERT INTO result_standings
                   (race_id, position, entrant_id, number, name, laps, last_ms, best_ms, gap_ms, lap_deficit, pit_count, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    race_id, pos, e["entrant_id"], e.get("number"), e.get("name"),
                    e["laps"], _ms(e.get("last"), seconds=True), _ms(e.get("best")),
                    _ms(e.get("gap_s"), seconds=True),  # convert to ms if the snapshot has seconds
                    e.get("lap_deficit", 0), e.get("pit_count", 0), e.get("status", "ACTIVE"),
                ),
            )

        # Laps
        for entrant_id, laps in laps_map.items():
            for i, lap_ms in enumerate(laps, start=1):
                cur.execute(
                    "INSERT INTO result_laps (race_id, entrant_id, lap_no, lap_ms) VALUES (?,?,?,?)",
                    (race_id, entrant_id, i, lap_ms),
                )

        cx.commit()


def _ms(v, seconds: bool = False):
    """Normalize numeric values into integer milliseconds."""
    if v is None:
        return None
    return int(round(v * 1000)) if seconds else int(v)