"""
app_results_api.py
------------------
FastAPI router exposing frozen race results from SQLite.

Read-only endpoints for results rails and exports. On each request we
lightly ensure the results schema exists (and add columns if needed),
then serve JSON/CSV. Tags are always included: we prefer the frozen
snapshot (result_standings.tag) and fall back to entrants.tag if missing.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone, timedelta
import sqlite3
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    # Adjust if your project resolves the DB path differently.
    return Path("backend/db/laps.sqlite")

def _connect() -> sqlite3.Connection:
    cx = sqlite3.connect(str(get_db_path()))
    cx.row_factory = sqlite3.Row
    return cx

def _table_has_col(db: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r["name"] == col for r in db.execute(f"PRAGMA table_info({table})"))


def _ensure_cols(db: sqlite3.Connection, table: str, col_defs: List[Tuple[str, str]]) -> None:
    """
    Idempotently ensure each (name, type) column exists on `table`.
    Uses simple ALTERs so we never blow away data.
    """
    for name, typ in col_defs:
        if not _table_has_col(db, table, name):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def _ensure_result_schema(db: sqlite3.Connection) -> None:
    """
    Make sure results tables exist, and that all expected columns are present.
    This runs on every request and is safe against already-updated DBs.
    """
    # Core tables (no-op if they already exist)
    db.execute("""
        CREATE TABLE IF NOT EXISTS result_meta (
            race_id          INTEGER PRIMARY KEY
            -- other columns may be added via ALTER below
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS result_standings (
            race_id     INTEGER,
            position    INTEGER,
            entrant_id  INTEGER,
            -- rest via ALTERs
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

    # Ensure ALL expected columns on result_meta (both “base” and “new”)
    _ensure_cols(db, "result_meta", [
        ("race_type",        "TEXT"),
        ("frozen_utc",       "INTEGER"),
        ("duration_ms",      "INTEGER"),
        ("clock_ms_frozen",  "INTEGER"),
        ("event_label",      "TEXT"),
        ("session_label",    "TEXT"),
        ("race_mode",        "TEXT"),
        ("frozen_iso_utc",   "TEXT"),
        ("frozen_iso_local", "TEXT"),
    ])

    # Ensure standings columns (including snapshot tag)
    _ensure_cols(db, "result_standings", [
        ("number",      "TEXT"),
        ("name",        "TEXT"),
        ("laps",        "INTEGER"),
        ("last_ms",     "INTEGER"),
        ("best_ms",     "INTEGER"),
        ("gap_ms",      "INTEGER"),
        ("lap_deficit", "INTEGER"),
        ("pit_count",   "INTEGER"),
        ("status",      "TEXT"),
        ("tag",         "TEXT"),
    ])

    db.commit()

# ---------------------------------------------------------------------------
# Time Helpers
# ---------------------------------------------------------------------------

def _to_local_iso(val) -> str | None:
    """
    Return a local-time ISO-8601 with offset (YYYY-MM-DDTHH:MM:SS±HH:MM) from:
      - ms epoch (int/float)  -> assumes milliseconds
      - ISO string ('...Z' or with offset)
      - naive ISO (treated as local wall time)
    """
    if val is None:
        return None

    try:
        # Milliseconds since epoch
        if isinstance(val, (int, float)):
            # treat as ms; if someone stored seconds, it will still parse but be "old"
            dt = datetime.fromtimestamp(float(val) / 1000.0, tz=timezone.utc).astimezone()
            return dt.isoformat(timespec="seconds")

        # ISO strings
        if isinstance(val, str):
            s = val.strip()
            # Handle trailing Z by converting to explicit UTC offset
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                return None
            # Naive -> interpret as local wall time
            if dt.tzinfo is None:
                dt = dt.astimezone()  # treats naive as local, attaches local tz
            else:
                dt = dt.astimezone()  # convert to local tz
            return dt.isoformat(timespec="seconds")

        return None
    except Exception:
        return None




# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/results", tags=["results"])


def _meta_row_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
    # Safe dict from result_meta, including newer optional fields if present.
    keys = set(r.keys())

    def _coerce_int(v):
        # Accept int/float directly
        if isinstance(v, (int, float)):
            return int(v)
        # Accept numeric strings like "1700000123"
        if isinstance(v, str):
            s = v.strip()
            if s.isdigit():
                return int(s)
            # Accept ISO-8601 strings like "2025-11-01T05:28:38Z"
            try:
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                return int(datetime.fromisoformat(s).timestamp() * 1000)
            except Exception:
                return None
        return None

    def get_int(k: str):
        return _coerce_int(r[k]) if k in keys and r[k] is not None else None

    def get_str(k: str):
        return r[k] if k in keys else None

    return {
        "race_id":          int(r["race_id"]),
        "race_type":        r["race_type"],
        "frozen_utc":       get_int("frozen_utc"),     # may be ms if parseable, else None
        "duration_ms":      get_int("duration_ms"),
        "clock_ms_frozen":  get_int("clock_ms_frozen"),
        "event_label":      get_str("event_label"),
        "session_label":    get_str("session_label"),
        "race_mode":        get_str("race_mode"),
        "frozen_iso_utc":   get_str("frozen_iso_utc"),
        "frozen_iso_local": get_str("frozen_iso_local"),
    }


@router.get("/recent")
def list_recent(limit: int = 12) -> Dict[str, Any]:
    """Latest frozen heats for the left rail."""
    with _connect() as db:
        _ensure_result_schema(db)
        rows = db.execute(
            """
            SELECT race_id, race_type, frozen_utc, duration_ms, clock_ms_frozen,
                   event_label, session_label, race_mode, frozen_iso_utc, frozen_iso_local
            FROM result_meta
            ORDER BY
              COALESCE(
                frozen_utc,
                CAST(strftime('%s', frozen_iso_utc) AS INTEGER) * 1000,
                0
              ) DESC,
              race_id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

        heats: List[Dict[str, Any]] = []
        for r in rows:
            meta = _meta_row_to_dict(r)

            # Choose best local time to show:
            local_iso = (
                meta["frozen_iso_local"]
                or _to_local_iso(meta["frozen_utc"])
                or _to_local_iso(meta["frozen_iso_utc"])
)

            heats.append({
                "heat_id":          meta["race_id"],
                "name":             meta["race_type"] or "-",
                "status":           "CHECKERED",
                "finished_utc":     meta["frozen_iso_utc"],     # keep for back-compat
                "started_utc":      None,
                "laps_count":       None,
                "entrant_count":    None,
                "event_label":      meta["event_label"],
                "session_label":    meta["session_label"],
                "race_mode":        meta["race_mode"] or meta["race_type"],
                "frozen_iso_local": local_iso,                   # ensure present
                "display_time":     local_iso,                   # <- rails use this
            })
        return {"heats": heats}


@router.get("/{race_id}")
def get_results(race_id: int) -> Dict[str, Any]:
    """Final standings JSON for one frozen heat, with tags included."""
    with _connect() as db:
        _ensure_result_schema(db)

        meta = db.execute(
            "SELECT * FROM result_meta WHERE race_id = ?",
            (race_id,),
        ).fetchone()
        if not meta:
            raise HTTPException(status_code=404, detail="Heat not found")

        # Prefer the frozen tag, but fall back to the live entrants table.
        st_rows = db.execute(
            """
            SELECT
              rs.position, rs.entrant_id, rs.number, rs.name,
              COALESCE(rs.tag, e.tag) AS tag,
              rs.laps, rs.last_ms, rs.best_ms, rs.gap_ms,
              rs.lap_deficit, rs.pit_count, rs.status
            FROM result_standings rs
            LEFT JOIN entrants e ON e.entrant_id = rs.entrant_id
            WHERE rs.race_id = ?
            ORDER BY rs.position ASC
            """,
            (race_id,),
        ).fetchall()

        standings: List[Dict[str, Any]] = []
        for r in st_rows:
            standings.append({
                "position":     int(r["position"]),
                "entrant_id":   int(r["entrant_id"]),
                "number":       r["number"],
                "name":         r["name"],
                "tag":          r["tag"],  # may be None if neither rs.tag nor entrants.tag is set
                "laps":         int(r["laps"]),
                "last_ms":      None if r["last_ms"] is None else int(r["last_ms"]),
                "best_ms":      None if r["best_ms"] is None else int(r["best_ms"]),
                "gap_ms":       None if r["gap_ms"]  is None else int(r["gap_ms"]),
                "lap_deficit":  int(r["lap_deficit"]) if r["lap_deficit"] is not None else 0,
                "pit_count":    int(r["pit_count"])   if r["pit_count"]   is not None else 0,
                "status":       r["status"] or "ACTIVE",
            })

        md = _meta_row_to_dict(meta)
        return {
            "race_id":          md["race_id"],
            "race_type":        md["race_type"],
            "frozen_utc":       md["frozen_iso_utc"] or None,
            "duration_ms":      md["duration_ms"],
            "event_label":      md["event_label"],
            "session_label":    md["session_label"],
            "race_mode":        md["race_mode"] or md["race_type"],
            "frozen_iso_local": md["frozen_iso_local"],
            "entrants":         standings,
        }


@router.get("/{race_id}/laps")
def get_laps(race_id: int) -> Dict[str, Any]:
    """Per-lap times plus entrant_meta (with tag fallback)."""
    with _connect() as db:
        _ensure_result_schema(db)

        # Laps
        rows = db.execute(
            """
            SELECT entrant_id, lap_no, lap_ms
            FROM result_laps
            WHERE race_id = ?
            ORDER BY entrant_id ASC, lap_no ASC
            """,
            (race_id,),
        ).fetchall()

        # 404 only if no meta either (otherwise return empty laps array)
        meta_exists = db.execute(
            "SELECT 1 FROM result_meta WHERE race_id=?",
            (race_id,),
        ).fetchone()
        if not rows and not meta_exists:
            raise HTTPException(status_code=404, detail="Heat not found")

        laps: Dict[str, List[int]] = {}
        for r in rows:
            eid = str(int(r["entrant_id"]))
            laps.setdefault(eid, []).append(int(r["lap_ms"]))

        # Entrant meta with tag fallback
        emeta_rows = db.execute(
            """
            SELECT
              rs.entrant_id,
              COALESCE(rs.tag, e.tag) AS tag,
              rs.number,
              rs.name
            FROM result_standings rs
            LEFT JOIN entrants e ON e.entrant_id = rs.entrant_id
            WHERE rs.race_id = ?
            """,
            (race_id,),
        ).fetchall()
        entrant_meta = {
            str(int(r["entrant_id"])): {
                "tag":    r["tag"] or None,
                "number": r["number"],
                "name":   r["name"],
            }
            for r in emeta_rows
        }

        return {"race_id": race_id, "laps": laps, "entrant_meta": entrant_meta}


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------

def _csv_stream(rows: List[List[Any]], headers: List[str]) -> StreamingResponse:
    buf = StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(headers)
    for row in rows:
        w.writerow(row)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="export.csv"'},
    )

@router.get("/{race_id}/standings.csv")
def standings_csv(race_id: int) -> StreamingResponse:
    with _connect() as db:
        _ensure_result_schema(db)
        rows = db.execute(
            """
            SELECT
              rs.position, rs.entrant_id, rs.number, rs.name,
              COALESCE(rs.tag, e.tag) AS tag,
              rs.laps, rs.last_ms, rs.best_ms, rs.gap_ms,
              rs.lap_deficit, rs.pit_count, rs.status
            FROM result_standings rs
            LEFT JOIN entrants e ON e.entrant_id = rs.entrant_id
            WHERE rs.race_id = ?
            ORDER BY rs.position ASC
            """,
            (race_id,),
        ).fetchall()

        payload: List[List[Any]] = []
        for r in rows:
            payload.append([
                r["position"],
                r["entrant_id"],
                r["number"],
                r["name"],
                r["tag"],         # always included (snapshot or fallback)
                r["laps"],
                r["last_ms"],
                r["best_ms"],
                r["gap_ms"],
                r["lap_deficit"],
                r["pit_count"],
                r["status"],
            ])

        headers = [
            "position","entrant_id","number","name","tag",
            "laps","last_ms","best_ms","gap_ms","lap_deficit","pit_count","status"
        ]
        return _csv_stream(payload, headers)

@router.get("/{race_id}/laps.csv")
def laps_csv(race_id: int) -> StreamingResponse:
    with _connect() as db:
        _ensure_result_schema(db)
        rows = db.execute(
            """
            SELECT
              rl.entrant_id,
              COALESCE(rs.tag, e.tag) AS tag,
              rs.number,
              rs.name,
              rl.lap_no,
              rl.lap_ms
            FROM result_laps rl
            LEFT JOIN result_standings rs
              ON rs.race_id = rl.race_id AND rs.entrant_id = rl.entrant_id
            LEFT JOIN entrants e
              ON e.entrant_id = rl.entrant_id
            WHERE rl.race_id = ?
            ORDER BY rl.entrant_id ASC, rl.lap_no ASC
            """,
            (race_id,),
        ).fetchall()

        payload: List[List[Any]] = []
        for r in rows:
            payload.append([
                r["entrant_id"],
                r["tag"],      # snapshot or fallback
                r["number"],
                r["name"],
                r["lap_no"],
                r["lap_ms"],
            ])

        headers = ["entrant_id","tag","number","name","lap_no","lap_ms"]
        return _csv_stream(payload, headers)


# ---------------- Admin: delete frozen results ----------------

def _delete_frozen_by_race(db: sqlite3.Connection, race_id: int) -> Dict[str, int]:
    cur = db.cursor()
    cur.execute("BEGIN")
    try:
        c_laps = cur.execute("DELETE FROM result_laps WHERE race_id = ?", (race_id,)).rowcount
        c_st   = cur.execute("DELETE FROM result_standings WHERE race_id = ?", (race_id,)).rowcount
        c_meta = cur.execute("DELETE FROM result_meta WHERE race_id = ?", (race_id,)).rowcount
        db.commit()
        return {"meta": c_meta or 0, "standings": c_st or 0, "laps": c_laps or 0}
    except Exception:
        db.rollback()
        raise

@router.delete("/{race_id}")
def delete_heat(race_id: int, confirm: str | None = None) -> Dict[str, int | str]:
    """
    Delete a single frozen heat (result_meta/standings/laps).
    Safety: require confirm=f"heat-{race_id}".
    """
    with _connect() as db:
        _ensure_result_schema(db)
        exists = db.execute("SELECT 1 FROM result_meta WHERE race_id=?", (race_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Heat not found")

        required = f"heat-{race_id}"
        if confirm != required:
            # Tell the client exactly what to send back
            raise HTTPException(status_code=409, detail=f"Confirmation required. Resend with ?confirm={required}")

        counts = _delete_frozen_by_race(db, race_id)
        result: Dict[str, int | str] = dict(counts)
        result["race_id"] = race_id
        return result

@router.delete("/")
def purge_all_results(confirm: str | None = None) -> Dict[str, int | str]:
    """
    PURGE ALL frozen results. Extremely dangerous.
    Safety: require confirm='PURGE-ALL-RESULTS'.
    """
    required = "PURGE-ALL-RESULTS"
    if confirm != required:
        raise HTTPException(status_code=409, detail=f"Confirmation required. Resend with ?confirm={required}")

    with _connect() as db:
        _ensure_result_schema(db)
        cur = db.cursor()
        cur.execute("BEGIN")
        try:
            c_laps = cur.execute("DELETE FROM result_laps").rowcount
            c_st   = cur.execute("DELETE FROM result_standings").rowcount
            c_meta = cur.execute("DELETE FROM result_meta").rowcount
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"purged": 1, "meta": c_meta or 0, "standings": c_st or 0, "laps": c_laps or 0}
