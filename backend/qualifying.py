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

# Import DB path from config
from backend.config_loader import get_db_path

# --- DB connection helper ---
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn

# ---- import the JSON helpers you added in db_schema.py -------------
from backend.db_schema import (
    get_brake_flags,
    get_event_config,
    set_event_config,
)

# Import ENGINE to call scratch method
from backend.race_engine import ENGINE

qual = APIRouter(prefix="/event", tags=["qualifying"])
qual_brake = APIRouter(prefix="/qual", tags=["qualifying-brake"])

class FreezeBody(BaseModel):
    source_heat_id: int                  # qualifying heat id
    policy: str = "demote"               # "demote" | "use_next_valid" | "exclude"

# ---- core: collect per-entrant lap durations (ms) from a heat -----
def _laps_by_entrant(conn: sqlite3.Connection, heat_id: int) -> Dict[int, List[int]]:
    """Pull lap durations for the qualifying heat from result_laps (must be frozen first)."""
    cur = conn.execute("""
        SELECT entrant_id, lap_ms
        FROM result_laps
        WHERE race_id = ?
        ORDER BY entrant_id, lap_no
    """, (heat_id,))
    times: Dict[int, List[int]] = {}
    for row in cur.fetchall():
        eid = row["entrant_id"]
        lap_ms = int(row["lap_ms"])
        times.setdefault(eid, []).append(lap_ms)
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
        
        if not by_e:
            raise HTTPException(400, 
                "No lap times found for this qualifying heat. "
                "Make sure the race has finished (CHECKERED flag) and results have been persisted."
            )

        # 2) manual brake-test verdicts from the QUAL heat
        flags = get_brake_flags(conn, body.source_heat_id)        # { entrant_id: True/False }

        # 3) Auto-adopt provisional entrants if enabled (CONFIG-DRIVEN BEHAVIOR)
        #    During qualifying, the engine creates "provisional" entrants with negative IDs
        #    (e.g., -1, -2, -3) for unrecognized transponder tags. These exist only in
        #    RaceEngine memory and results tables, NOT in the authoritative entrants table.
        #
        #    Problem: When starting a race later, the system loads enabled entrants from the DB
        #    and uses the frozen grid to assign starting positions by entrant_id. If provisionals
        #    remain as negative IDs, they won't match real entrants → broken grid positions.
        #
        #    Solution: "Auto-adopt" converts provisionals to permanent entrants during freeze:
        #      1. Detect negative entrant_ids in the qualifying results
        #      2. Create real entrant records in the DB (enabled=true, auto-assigned numbers)
        #      3. Update result_standings and result_laps to reference the new positive IDs
        #      4. Update the frozen grid config to use new IDs
        #      5. Return metadata so UI can alert operators to fix "Unknown" names
        #
        #    This ensures grid positions work immediately when the race starts, while giving
        #    operators a chance to update placeholder names/numbers before the green flag.
        
        from backend.config_loader import get_qualifying_config
        qual_cfg = get_qualifying_config()
        auto_adopt = qual_cfg.get("auto_adopt_unknowns", True)
        auto_number_start = qual_cfg.get("auto_number_start", 901)
        
        id_mapping = {}           # Maps provisional (negative) -> permanent (positive) IDs
        adopted_entrants = []     # Metadata for UI notification
        
        if auto_adopt:
            # Find provisional entrants (RaceEngine assigns negative IDs: -1, -2, etc.)
            provisional_ids = [eid for eid in by_e.keys() if eid < 0]
            
            if provisional_ids:
                # Fetch provisional entrant details from result_standings (written by results.persist_results)
                # These contain the "Unknown XXXX" names and transponder tags from the qualifying session
                cur = conn.execute(f"""
                    SELECT entrant_id, name, tag
                    FROM result_standings
                    WHERE race_id = ? AND entrant_id IN ({','.join('?' * len(provisional_ids))})
                """, (body.source_heat_id, *provisional_ids))
                
                provisional_data = {row["entrant_id"]: row for row in cur.fetchall()}
                
                # Determine next available car number (avoid conflicts with existing entrants)
                # Start from auto_number_start (default 901) and increment until we find an unused number
                cur_numbers = conn.execute("SELECT number FROM entrants WHERE number IS NOT NULL").fetchall()
                used_numbers = {int(row["number"]) for row in cur_numbers if row["number"] and row["number"].isdigit()}
                next_number = auto_number_start
                while next_number in used_numbers:
                    next_number += 1
                
                # Create permanent entrant records for each provisional (ENABLED by default)
                # Why enabled? So the frozen grid positions apply immediately when starting the race.
                # The "Unknown XXXX" name and 900+ number serve as clear visual indicators that
                # operators should edit these in the Entrants & Tags page before going green.
                for old_id in sorted(provisional_ids):  # Process in ascending order for deterministic numbering
                    prov = provisional_data.get(old_id)
                    if not prov:
                        continue  # Skip if no matching result_standings row (shouldn't happen)
                    
                    tag = prov["tag"] or None
                    name = prov["name"] or f"Unknown {tag}" if tag else "Unknown"
                    number = str(next_number)
                    
                    # INSERT entrant record (enabled=1 so they appear in race load)
                    cur = conn.execute("""
                        INSERT INTO entrants (name, number, tag, enabled)
                        VALUES (?, ?, ?, 1)
                    """, (name, number, tag))
                    
                    new_id = cur.lastrowid  # SQLite auto-assigned positive ID
                    id_mapping[old_id] = new_id
                    
                    # Track adoption metadata for UI notification
                    adopted_entrants.append({
                        "entrant_id": new_id,
                        "tag": tag,
                        "number": number,
                        "name": name,
                        "old_id": old_id
                    })
                    
                    next_number += 1  # Increment for next provisional
                
                # Update all references from negative -> positive IDs so data links correctly
                
                # Update result_standings: replace provisional IDs with permanent ones
                # This preserves the qualifying results (position, laps, times) under the new ID
                for old_id, new_id in id_mapping.items():
                    conn.execute("""
                        UPDATE result_standings
                        SET entrant_id = ?
                        WHERE race_id = ? AND entrant_id = ?
                    """, (new_id, body.source_heat_id, old_id))
                
                # Update result_laps: same transformation for individual lap records
                for old_id, new_id in id_mapping.items():
                    conn.execute("""
                        UPDATE result_laps
                        SET entrant_id = ?
                        WHERE race_id = ? AND entrant_id = ?
                    """, (new_id, body.source_heat_id, old_id))
                
                # Update the in-memory lap times dict (used for grid ranking below)
                # Pop the old negative key, insert with new positive key
                for old_id, new_id in id_mapping.items():
                    by_e[new_id] = by_e.pop(old_id)
                
                # Update brake test verdicts dict to use new IDs
                # If a provisional had a manual brake verdict, preserve it under the new ID
                for old_id, new_id in id_mapping.items():
                    if old_id in flags:
                        flags[new_id] = flags.pop(old_id)
                
                # Commit the transaction (INSERT entrants + UPDATE standings/laps)
                # If any step fails, entire adoption rolls back
                conn.commit()

        # 4) choose valid best lap per policy
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

        # 5) rank (smaller best_ms is better); apply policy in sort key
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

        # 6) persist on the EVENT
        cfg = get_event_config(conn, event_id) or {}
        cfg["qualifying"] = {
            "source_heat_id": body.source_heat_id,
            "policy": body.policy,
            "grid": rows,
        }
        set_event_config(conn, event_id, cfg)

        # 7) Build response with adoption metadata
        response = {
            "event_id": event_id,
            "qualifying": cfg["qualifying"]
        }
        
        if adopted_entrants:
            response["adopted_count"] = len(adopted_entrants)
            response["adopted_entrants"] = adopted_entrants
            response["message"] = f"Grid frozen successfully. {len(adopted_entrants)} unknown entrant(s) were auto-adopted with temporary names/numbers. Please update them in Entrants & Tags before starting the race."
        
        return response
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

# -------------------------------------------------------------------------
# Brake Test Verdict endpoints
# -------------------------------------------------------------------------
# Store per-entrant brake test verdicts (Pass/Fail) in memory or SQLite
# In-memory storage for active races; optionally persist to race_meta if needed
#
# GET /qual/heat/{heat_id}/brake
#     Returns: { "entrant_id": bool, ... }
#
# POST /qual/heat/{heat_id}/brake
#     Body: { entrant_id: int, brake_ok: bool | None }
#     If brake_ok is None/null, removes the verdict (resets to unknown)
# -------------------------------------------------------------------------

# In-memory storage: race_id -> { entrant_id: bool }
_brake_verdicts_cache: Dict[int, Dict[int, bool]] = {}

class BrakeVerdictBody(BaseModel):
    entrant_id: int
    brake_ok: Optional[bool] = None  # true=Pass, false=Fail, None=remove

@qual_brake.get("/heat/{heat_id}/brake")
def get_brake_verdicts(heat_id: int):
    """Return all brake test verdicts for a heat/race as { entrant_id: bool }"""
    # Use in-memory cache (race_id is used as heat_id for active races)
    verdicts = _brake_verdicts_cache.get(heat_id, {})
    return verdicts

@qual_brake.post("/heat/{heat_id}/brake")
def set_brake_verdict(heat_id: int, body: BrakeVerdictBody):
    """Set or clear a single brake test verdict for an entrant"""
    # Use in-memory cache (race_id is used as heat_id for active races)
    if heat_id not in _brake_verdicts_cache:
        _brake_verdicts_cache[heat_id] = {}
    
    if body.brake_ok is None:
        # Remove the verdict (reset to unknown)
        _brake_verdicts_cache[heat_id].pop(body.entrant_id, None)
    else:
        # Set verdict (true=Pass, false=Fail)
        _brake_verdicts_cache[heat_id][body.entrant_id] = body.brake_ok
    
    # Persist to database so freeze can read it
    conn = get_conn()
    try:
        from backend.db_schema import set_brake_flag
        if body.brake_ok is not None:
            set_brake_flag(conn, heat_id, body.entrant_id, body.brake_ok)
        conn.commit()
    finally:
        conn.close()
    
    return {"heat_id": heat_id, "entrant_id": body.entrant_id, "brake_ok": body.brake_ok}


@qual_brake.post("/heat/{heat_id}/scratch")
def scratch_pass(heat_id: int, body: BrakeVerdictBody):
    """
    Scratch the current best lap for an entrant:
    1. Remove the current best lap time from consideration
    2. Revert to previous best lap (if any exists)
    3. Set brake test based on result:
       - If previous lap exists → PASS (they have a valid fallback)
       - If no previous lap → FAIL (nothing valid left)
    
    Body: { entrant_id: int }
    Returns: { entrant_id, scratched_best_s, previous_best_s, brake_ok }
    """
    entrant_id = body.entrant_id
    
    # Call RaceEngine to scratch the best lap
    result = ENGINE.scratch_entrant_best(entrant_id)
    
    if not result.get("ok"):
        error = result.get("error", "unknown_error")
        if error == "entrant_not_found":
            raise HTTPException(404, "Entrant not found in active race")
        elif error == "no_best_lap":
            raise HTTPException(400, "Entrant has no best lap to scratch")
        else:
            raise HTTPException(400, f"Failed to scratch: {error}")
    
    # Determine brake test status based on whether there's a fallback lap
    previous_best_s = result.get("previous_best_s")
    has_fallback = previous_best_s is not None
    
    # If they have a fallback lap → PASS, if not → FAIL
    brake_ok = has_fallback
    
    # Update brake verdict cache
    if heat_id not in _brake_verdicts_cache:
        _brake_verdicts_cache[heat_id] = {}
    _brake_verdicts_cache[heat_id][entrant_id] = brake_ok
    
    # Persist brake test status to database
    conn = get_conn()
    try:
        from backend.db_schema import set_brake_flag
        set_brake_flag(conn, heat_id, entrant_id, brake_ok)
        conn.commit()
    finally:
        conn.close()
    
    return {
        "heat_id": heat_id,
        "entrant_id": entrant_id,
        "scratched_best_s": result.get("scratched_best_s"),
        "previous_best_s": previous_best_s,
        "brake_ok": brake_ok,
    }
