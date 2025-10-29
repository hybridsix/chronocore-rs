"""
ChronoCore RS - backend/qualifying.py
-------------------------------------
Qualifying utilities and endpoints for freezing a starting grid from a
qualifying heat. Uses manual Brake Test verdicts (if any) together with
per-entrant lap durations to compute an ordered grid and stores it on
the parent event's config JSON.

Endpoints
- POST /event/{event_id}/qual/freeze
     Body FreezeBody: { source_heat_id: int, policy: "demote"|"use_next_valid"|"exclude" }
     Behavior:
     1) Collect lap durations (ms) per entrant from lap_events for the given heat.
         Durations are computed as deltas between consecutive ts_ms for an entrant.
     2) Load manual brake-test flags via get_brake_flags(heat_id) -> { entrant_id: bool }.
     3) Choose a candidate best_ms per entrant:
         - If brake_ok=True: use fastest lap.
         - If brake_ok=False:
              • policy==use_next_valid → use the next fastest lap if present.
              • policy in {demote, exclude} → keep fastest; sort/filter applies later.
         - If no verdict: use fastest lap.
     4) Rank entrants with sort key (exclude, demote, best_ms) where:
         - exclude := (policy=="exclude" and brake_ok==False)
         - demote  := (policy=="demote"  and brake_ok==False)
         After sort, if policy=="exclude", rows with brake_ok==False are removed.
     5) Persist to event config via set_event_config(event_id, cfg), under:
         cfg["qualifying"] = { source_heat_id, policy, grid: [ { entrant_id, best_ms, brake_ok, order } ] }

Data sources
- heats(event_id) → verify the qualifying heat belongs to the event.
- lap_events(heat_id, entrant_id, ts_ms) → build lap durations per entrant.
- get_brake_flags(conn, heat_id) → JSON-backed map of manual verdicts.

Notes
- brake_ok in grid rows is True only when an explicit PASS was recorded.
  Absent/None verdicts are treated as not-True for this boolean field.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import sqlite3, json
from typing import Dict, List, Optional

# --- if you keep DB connection helpers elsewhere, import your own ---
def get_conn() -> sqlite3.Connection:
    # Replace with your real dependency / connection pool
    conn = sqlite3.connect("data/ccrs.db")
    conn.row_factory = sqlite3.Row
    return conn

# ---- import the JSON helpers you added in db_schema.py -------------
from backend.db_schema import (
    get_brake_flags,
    get_event_config,
    set_event_config,
)

qual = APIRouter(prefix="/event", tags=["qualifying"])

class FreezeBody(BaseModel):
    source_heat_id: int                  # qualifying heat id
    policy: str = "demote"               # "demote" | "use_next_valid" | "exclude"

# ---- core: collect per-entrant lap durations (ms) from a heat -----
def _laps_by_entrant(conn: sqlite3.Connection, heat_id: int) -> Dict[int, List[int]]:
    cur = conn.execute("""
        SELECT entrant_id, ts_ms
        FROM lap_events
        WHERE heat_id = ?
        ORDER BY entrant_id, ts_ms
    """, (heat_id,))
    times: Dict[int, List[int]] = {}
    last_ts: Dict[int, int] = {}
    for row in cur.fetchall():
        eid = row["entrant_id"]
        ts  = int(row["ts_ms"])
        if eid in last_ts:
            times.setdefault(eid, []).append(ts - last_ts[eid])
        last_ts[eid] = ts
    return times

def _event_id_for_heat(conn: sqlite3.Connection, heat_id: int) -> int:
    r = conn.execute("SELECT event_id FROM heats WHERE heat_id=?", (heat_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Heat not found")
    return int(r["event_id"])

@qual.post("/{event_id}/qual/freeze")
def freeze_grid(event_id: int, body: FreezeBody):
    conn = get_conn()
    try:
        # sanity: the qualifying heat must belong to this event
        evt_for_heat = _event_id_for_heat(conn, body.source_heat_id)
        if evt_for_heat != event_id:
            raise HTTPException(400, "Qualifying heat does not belong to this event")

        # 1) lap durations per entrant
        by_e = _laps_by_entrant(conn, body.source_heat_id)        # { entrant_id: [ms, ...] }

        # 2) manual brake-test verdicts from the QUAL heat
        flags = get_brake_flags(conn, body.source_heat_id)        # { entrant_id: True/False }

        # 3) choose valid best lap per policy
        rows = []
        for eid, arr in by_e.items():
            arr.sort()
            verdict = flags.get(eid, None)  # None = not set yet
            best = None

            if verdict is True:
                best = arr[0] if arr else None
            elif verdict is False:
                if body.policy == "use_next_valid" and len(arr) > 1:
                    best = arr[1]
                elif body.policy in ("demote", "exclude"):
                    best = arr[0] if arr else None  # keep for stats; ordering handles demote/exclude
            else:
                best = arr[0] if arr else None

            rows.append({
                "entrant_id": eid,
                "best_ms": best,
                "brake_ok": (verdict is True),
            })

        # 4) rank (smaller best_ms is better); apply policy in sort key
        INF = 10**12
        def key(r):
            best = r["best_ms"] if r["best_ms"] is not None else INF
            demote  = (body.policy == "demote"  and r["brake_ok"] is False)
            exclude = (body.policy == "exclude" and r["brake_ok"] is False)
            return (exclude, demote, best)

        rows.sort(key=key)
        if body.policy == "exclude":
            rows = [r for r in rows if r["brake_ok"] or r["brake_ok"] is None]

        # assign 1-based order
        for i, r in enumerate(rows, 1):
            r["order"] = i

        # 5) persist on the EVENT
        cfg = get_event_config(conn, event_id) or {}
        cfg["qualifying"] = {
            "source_heat_id": body.source_heat_id,
            "policy": body.policy,
            "grid": rows,
        }
        set_event_config(conn, event_id, cfg)

        return {"event_id": event_id, "qualifying": cfg["qualifying"]}
    finally:
        conn.close()

@qual.get("/{event_id}/qual")
def get_frozen_grid(event_id: int):
    conn = get_conn()
    try:
        cfg = get_event_config(conn, event_id) or {}
        q = cfg.get("qualifying")
        if not q:
            return {"event_id": event_id, "qualifying": None}
        return {"event_id": event_id, "qualifying": q}
    finally:
        conn.close()
