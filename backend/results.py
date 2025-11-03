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
from typing import Any, Dict, List, Optional

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

    state = snapshot.get("state")
    if not isinstance(state, dict):
        state = {}

    frozen_ms_raw = snapshot.get("clock_ms_frozen")
    if frozen_ms_raw is None:
        frozen_ms_raw = snapshot.get("clock_ms")

    frozen_ms = 0
    if frozen_ms_raw is not None:
        try:
            frozen_ms = int(frozen_ms_raw)
        except (TypeError, ValueError):
            frozen_ms = 0

    # frozen_ms is the race clock time (elapsed ms), not a wall-clock timestamp
    # Use the current wall-clock time as the freeze timestamp
    freeze_time = datetime.now().astimezone()
    frozen_iso_local = freeze_time.isoformat(timespec="seconds")
    frozen_iso_utc = freeze_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    clock_ms_frozen = frozen_ms if frozen_ms > 0 else 0
    duration_raw = snapshot.get("duration_ms")
    try:
        duration_ms = int(duration_raw) if duration_raw is not None else clock_ms_frozen
    except (TypeError, ValueError):
        duration_ms = clock_ms_frozen

    event_label = snapshot.get("event_label") or state.get("event_label")
    session_label = snapshot.get("session_label") or state.get("session_label")
    race_mode = (
        snapshot.get("race_mode")
        or state.get("race_type")
        or state.get("mode")
        or race_type
    )

    with sqlite3.connect(DB_PATH) as cx:
        cx.execute("PRAGMA journal_mode=WAL;")
        cur = cx.cursor()
        log_ctx = {"race_id": race_id}

        cur.execute("PRAGMA table_info(result_meta)")
        result_meta_cols = {row[1] for row in cur.fetchall()}

        cur.execute("SELECT 1 FROM result_meta WHERE race_id=?", (race_id,))
        already_exists = cur.fetchone() is not None

        extended_cols = {
            "clock_ms_frozen",
            "event_label",
            "session_label",
            "race_mode",
            "frozen_iso_utc",
            "frozen_iso_local",
        }

        if extended_cols.issubset(result_meta_cols):
            frozen_utc_value = frozen_iso_utc or now_utc
            cur.execute(
                """
                INSERT INTO result_meta
                  (race_id, race_type, frozen_utc, duration_ms, clock_ms_frozen,
                   event_label, session_label, race_mode, frozen_iso_utc, frozen_iso_local)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(race_id) DO UPDATE SET
                  race_type        = excluded.race_type,
                  frozen_utc       = excluded.frozen_utc,
                  duration_ms      = excluded.duration_ms,
                  clock_ms_frozen  = excluded.clock_ms_frozen,
                  event_label      = excluded.event_label,
                  session_label    = excluded.session_label,
                  race_mode        = excluded.race_mode,
                  frozen_iso_utc   = excluded.frozen_iso_utc,
                  frozen_iso_local = excluded.frozen_iso_local
                """,
                (
                    race_id,
                    race_type,
                    frozen_utc_value,
                    duration_ms,
                    clock_ms_frozen,
                    event_label,
                    session_label,
                    race_mode,
                    frozen_iso_utc,
                    frozen_iso_local,
                ),
            )
        else:
            cur.execute(
                "INSERT OR IGNORE INTO result_meta (race_id, race_type, frozen_utc, duration_ms) VALUES (?,?,?,?)",
                (race_id, race_type, frozen_iso_utc or now_utc, duration_ms),
            )
            if cur.rowcount == 0:
                already_exists = True

        if already_exists:
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

        # Capture entrant tags before writing standings so we can freeze them with results.
        tags_by_id: Dict[int, Optional[str]] = {}
        try:
            rows = cur.execute("SELECT entrant_id, tag FROM entrants").fetchall()
            tags_by_id = {
                int(row[0]): (row[1] if row[1] else None)
                for row in rows
            }
        except Exception:
            # Some dev/test databases may not have an entrants table; fall back to snapshot tags only.
            tags_by_id = {}

        # Capture brake test flags for this race (stored on heats table as JSON)
        from backend.db_schema import get_brake_flags
        brake_flags_by_id: Dict[int, bool] = {}
        try:
            brake_flags_by_id = get_brake_flags(cx, race_id)
        except Exception:
            # If heats table doesn't exist or race isn't a heat, skip brake tests
            pass

        # Standings
        for pos, e in enumerate(standings, start=1):
            entrant_id = int(e["entrant_id"])
            entrant_tag = e.get("tag") or tags_by_id.get(entrant_id)
            
            # Convert brake_valid: prioritize snapshot, then brake_flags, then NULL
            # Snapshot might have it if passed through; otherwise query from heats table
            brake_val = e.get("brake_valid")
            if brake_val is None and entrant_id in brake_flags_by_id:
                brake_val = brake_flags_by_id[entrant_id]
                
            if brake_val is True:
                brake_int = 1
            elif brake_val is False:
                brake_int = 0
            else:
                brake_int = None
            
            cur.execute(
                """INSERT INTO result_standings
                (race_id, position, entrant_id, number, name, tag, laps, last_ms, best_ms, gap_ms, lap_deficit, pit_count, status, grid_index, brake_valid)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    race_id, pos, entrant_id, e.get("number"), e.get("name"), entrant_tag,
                    e["laps"],
                    _ms(e.get("last"), seconds=True),         # seconds → ms
                    _ms(e.get("best"), seconds=True),         # <-- add seconds=True
                    _ms(e.get("gap_s"), seconds=True),        # seconds → ms
                    e.get("lap_deficit", 0),
                    e.get("pit_count", 0),
                    e.get("status", "ACTIVE"),
                    e.get("grid_index"),                      # qualifying position
                    brake_int,                                 # brake test result
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