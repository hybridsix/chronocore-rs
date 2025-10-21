from __future__ import annotations
import time, json, threading, sqlite3, os
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from backend.db_schema import ensure_schema

try:
    # Prefer the merged loader (config/app.yaml + race_modes.yaml + event.yaml)
    from .config_loader import CONFIG
except Exception:
    CONFIG = {}

UTC_MS = lambda: int(time.time() * 1000)

ALLOWED_FLAGS = {"pre","green","yellow","red","blue","white","checkered"}
ALLOWED_STATUS = {"ACTIVE","DISABLED","DNF","DQ"}

# ----------------------------- Data structs -----------------------------
class Entrant:
    __slots__ = ("entrant_id","enabled","status","tag","number","name",
                 "laps","last_s","best_s","pace_buf","pit_open_at_ms",
                 "pit_count","last_pit_s","_last_hit_ms")
    def __init__(self, entrant_id:int, enabled:bool=True, status:str="ACTIVE",
                 tag:Optional[str]=None, number:Optional[str]=None, name:str=""):
        self.entrant_id = int(entrant_id)
        self.enabled    = bool(enabled)
        self.status     = status if status in ALLOWED_STATUS else "ACTIVE"
        self.tag        = (tag or None)
        self.number     = number or None
        self.name       = name or f"Entrant {entrant_id}"

        self.laps: int  = 0
        self.last_s: Optional[float] = None
        self.best_s: Optional[float] = None
        self.pace_buf: List[float]   = []  # last up to 5 laps

        self.pit_open_at_ms: Optional[int] = None
        self.pit_count: int = 0
        self.last_pit_s: Optional[float] = None

        self._last_hit_ms: Optional[int] = None
    def as_snapshot(self, leader_best_s: Optional[float], leader_laps:int) -> Dict:
        # gap_s only meaningful on same-lap cohort; else 0 with lap_deficit>0
        lap_deficit = max(0, leader_laps - self.laps)
        gap_s = 0.0
        if lap_deficit == 0 and leader_best_s is not None and self.best_s is not None:
            gap_s = max(0.0, round(self.best_s - leader_best_s, 3))

        pace_5 = None
        if self.pace_buf:
            pace_5 = round(sum(self.pace_buf) / len(self.pace_buf), 3)

        return {
            "entrant_id": self.entrant_id,
            "enabled": self.enabled,
            "status": self.status,
            "tag": self.tag,
            "number": self.number,
            "name": self.name,
            "laps": self.laps,
            "last": None if self.last_s is None else round(self.last_s, 3),
            "best": None if self.best_s is None else round(self.best_s, 3),
            "pace_5": pace_5,
            "gap_s": gap_s,
            "lap_deficit": lap_deficit,
            "pit_count": self.pit_count,
            "last_pit_s": None if self.last_pit_s is None else round(self.last_pit_s, 3),
        }

# ----------------------------- Journal (optional) -----------------------------
class Journal:
    def __init__(self, db_path:str, enabled:bool, batch_ms:int, batch_max:int, fsync:bool):
        self.enabled = enabled
        self.db_path = db_path
        self.batch_ms = batch_ms
        self.batch_max = batch_max
        self.fsync = fsync
        self._buf = []
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self._ensure_schema()
        self._white_window_begun: bool = False


    def _ensure_schema(self):
        if not self.enabled: return
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS race_events(
            id INTEGER PRIMARY KEY,
            race_id INTEGER NOT NULL,
            ts_utc INTEGER NOT NULL,
            clock_ms INTEGER NOT NULL,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS race_checkpoints(
            id INTEGER PRIMARY KEY,
            race_id INTEGER NOT NULL,
            ts_utc INTEGER NOT NULL,
            clock_ms INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL
        )""")
        con.commit()
        con.close()

    def put(self, row:Tuple[int,int,int,str,dict]):
        # row = (race_id, ts_utc, clock_ms, type, payload_dict)
        if not self.enabled: return
        with self._lock:
            self._buf.append(row)
            now = time.time()
            if len(self._buf) >= self.batch_max or (now - self._last_flush) * 1000 >= self.batch_ms:
                self._flush_locked()
                self._last_flush = now

    def force_flush(self):
        if not self.enabled: return
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self._buf: return
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.executemany("INSERT INTO race_events(race_id,ts_utc,clock_ms,type,payload_json) VALUES(?,?,?,?,?)",
                        [(r, t, c, typ, json.dumps(p)) for (r,t,c,typ,p) in self._buf])
        con.commit()
        if self.fsync:
            con.execute("PRAGMA wal_checkpoint(FULL);")
        con.close()
        self._buf.clear()

    def checkpoint(self, race_id:int, clock_ms:int, snapshot:dict):
        if not self.enabled: return
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("INSERT INTO race_checkpoints(race_id, ts_utc, clock_ms, snapshot_json) VALUES(?,?,?,?)",
                    (race_id, UTC_MS(), clock_ms, json.dumps(snapshot)))
        con.commit()
        if self.fsync:
            con.execute("PRAGMA wal_checkpoint(FULL);")
        con.close()

# ----------------------------- Race Engine -----------------------------
class RaceEngine:
    def __init__(self, config:dict):
        """
        config may be:
          - legacy shape (timing/features/persistence)
          - or merged loader: {"app":{...}, "modes":{...}, "event":{...}}
        """
        self.cfg = config or {}

        # Support both layouts
        app = self.cfg.get("app", self.cfg) or {}
        tcfg = (app.get("timing") or app.get("engine") or {})
        pcfg = (app.get("persistence") or {})
        fcfg = (app.get("features") or {})

        self._modes = self.cfg.get("modes", {})     # from race_modes.yaml
        self._event = self.cfg.get("event", {})     # from event.yaml

        # thresholds / features with sane fallbacks
        self.min_lap_s    = float(tcfg.get("min_lap_s", tcfg.get("min_lap_s_default", 5.0)))
        self.min_lap_dup  = float(tcfg.get("min_lap_s_dup", 1.0))
        self.feature_pits = bool(fcfg.get("pit_timing", True))
        self.feature_auto_prov = bool(fcfg.get("auto_provisional", True))

        # persistence (forward-only: require app.engine.persistence)
        try:
            engine_cfg = app["engine"]
            pcfg = engine_cfg["persistence"]
        except KeyError as e:
            raise RuntimeError("Missing required config: app.engine.persistence") from e

        if "sqlite_path" not in pcfg:
            raise RuntimeError("Missing required config: app.engine.persistence.sqlite_path")

        db_path = Path(pcfg["sqlite_path"])  # explicit, no legacy/default paths
        ensure_schema(db_path, recreate=bool(pcfg.get("recreate_on_boot", False)))

        self.journal = Journal(
            db_path=str(db_path),
            enabled=bool(pcfg.get("enabled", False)),
            batch_ms=int(pcfg.get("batch_ms", 200)),
            batch_max=int(pcfg.get("batch_max", 50)),
            fsync=bool(pcfg.get("fsync", True)),
        )
        self.checkpoint_s = int(pcfg.get("checkpoint_s", 15))


        # state
        self._lock = threading.RLock()
        self.reset()

        # background checkpoint timer
        self._last_checkpoint = time.time()

    # ---------- lifecycle ----------
    def reset(self):
        with self._lock:
            self.flag: str = "pre"
            self.race_id: Optional[int] = None
            self.race_type: Optional[str] = None
            self.clock_ms: int = 0
            self.clock_start_monotonic: Optional[float] = None  # when green started
            self.running: bool = False

            # active limit (from mode): type: "time"|"laps"
            self._limit = None          # {"type":"time"|"laps","value":...}
            self._limit_ms = None       # precalc for time limit
            self._limit_laps = None     # precalc for lap limit
            self._limit_reached = False # guard against repeated triggers
            # white flag / limit tracking for auto white/finish helpers
            self._white_set: bool = False
            self._limit_type: str = "time"
            self._time_limit_s: int = 0
            self._lap_limit: int = 0
            self._white_window_begun: bool = False
            self.soft_end: bool = False
            self._white_window_begun = False
            self._white_set = False



            self.entrants: Dict[int, Entrant] = {}
            self.tag_to_eid: Dict[str, int] = {}
            self._next_provisional_id = 1
            self._provisional_cap = 50

            self._last_update_utc = 0
            self._events_ring: List[dict] = []  # in-memory trace for debug
            self._active_mode: Optional[dict] = None

            self.sim_active: bool = False
            self.sim_banner: str | None = None

    def load(self, race_id:int, entrants:List[dict], race_type:str="sprint", session_config: Optional[dict]=None) -> dict:
        with self._lock:
            self.reset()
            self.race_id = int(race_id)
            self.race_type = str(race_type)

            # apply mode overrides (e.g., min_lap_s, limits) if present
            self._apply_mode_cfg(self.race_type)

            # Capture session-provided limit metadata for white/checkered automation
            limit_cfg = {}
            if isinstance(session_config, dict):
                limit_cfg = session_config.get("limit") or {}
            ltype = str(limit_cfg.get("type", "")).lower()

            if ltype in {"time", "laps"}:
                self._limit_type = ltype
            elif isinstance(self._limit, dict) and self._limit.get("type") in {"time", "laps"}:
                self._limit_type = str(self._limit["type"]).lower()
            else:
                self._limit_type = "time"

            if self._limit_type == "time":
                value = limit_cfg.get("value_s") if isinstance(limit_cfg, dict) else None
                if value is None and isinstance(self._limit, dict) and self._limit.get("type") == "time":
                    value = self._limit.get("value")
                self._time_limit_s = int(float(value)) if value not in (None, "") else 0
                self._lap_limit = 0
            else:
                value = limit_cfg.get("value_laps") if isinstance(limit_cfg, dict) else None
                if value is None and isinstance(self._limit, dict) and self._limit.get("type") == "laps":
                    value = self._limit.get("value")
                self._lap_limit = int(float(value)) if value not in (None, "") else 0
                self._time_limit_s = 0

            # Soft-end (time mode only)
            self.soft_end = bool((session_config or {}).get("limit", {}).get("soft_end", False)) \
                            if self._limit_type == "time" else False

            # Make enforcement + snapshot reflect session_config if provided
            if self._limit_type == "time" and self._time_limit_s > 0:
                self._limit       = {"type": "time", "value": self._time_limit_s}
                self._limit_ms    = int(self._time_limit_s * 1000)
                self._limit_laps  = None
            elif self._limit_type == "laps" and self._lap_limit > 0:
                self._limit       = {"type": "laps", "value": self._lap_limit}
                self._limit_laps  = int(self._lap_limit)
                self._limit_ms    = None
            else:
                self._limit       = None
                self._limit_ms    = None
                self._limit_laps  = None

            # Reset white flag trigger state for a fresh session
            self._white_set = False

            # install entrants
            for e in entrants or []:
                # --- validate/coerce entrant_id ---------------------------------
                raw_id = e.get("entrant_id", None)
                if raw_id is None:
                    raise ValueError("entrant is missing required key 'entrant_id'")
                try:
                    entrant_id = int(raw_id)
                except (TypeError, ValueError):
                    raise ValueError(f"invalid entrant_id: {raw_id!r}")
                # ----------------------------------------------------------------

                ent = Entrant(
                    entrant_id=entrant_id,
                    enabled=bool(e.get("enabled", True)),
                    status=(str(e.get("status", "ACTIVE")).upper()
                            if str(e.get("status", "ACTIVE")).upper() in ALLOWED_STATUS
                            else "ACTIVE"),
                    tag=(str(e.get("tag")).strip() if e.get("tag") else None),
                    number=(str(e.get("number")).strip() if e.get("number") else None),
                    name=str(e.get("name") or f"Entrant {entrant_id}"),
                )
                self.entrants[ent.entrant_id] = ent
                if ent.enabled and ent.tag:
                    self.tag_to_eid[ent.tag] = ent.entrant_id

            self._emit_flag_change("pre")
            return self.snapshot()

    def _apply_mode_cfg(self, mode_name: str):
        """Apply YAML mode overrides at race load/arm time (non-destructive)."""
        m = self._modes.get(mode_name, {})
        if not m:
            self._active_mode = {"name": mode_name, "label": mode_name.title()}
            # clear any previous limit
            self._limit = None
            self._limit_ms = None
            self._limit_laps = None
            self._limit_reached = False
            return

        # thresholds
        if "min_lap_s" in m:
            self.min_lap_s = float(m["min_lap_s"])

        # light snapshot for UIs/exports
        self._active_mode = {
            "name": mode_name,
            "label": m.get("label", mode_name.title()),
            "limit": m.get("limit"),
            "scoring": m.get("scoring"),
        }

        # extract limit for enforcement
        lim = m.get("limit") or {}
        ltype = str(lim.get("type", "")).lower()
        lval = lim.get("value")

        if ltype in {"time", "laps"} and isinstance(lval, (int, float)) and lval > 0:
            self._limit = {"type": ltype, "value": lval}
            if ltype == "time":
                self._limit_ms = int(float(lval) * 1000)
                self._limit_laps = None
            else:
                self._limit_laps = int(lval)
                self._limit_ms = None
            self._limit_reached = False
        else:
            # no valid limit configured
            self._limit = None
            self._limit_ms = None
            self._limit_laps = None
            self._limit_reached = False


    # ---------- flag & clock ----------
    def set_flag(self, flag:str) -> dict:
        f = str(flag).lower()
        if f not in ALLOWED_FLAGS:
            raise ValueError(f"Invalid flag '{flag}'")
        with self._lock:
            prev = (self.flag or "").lower()
            f_upper = str(flag).upper()
            f_lower = f_upper.lower()

            # --- Reset/guard white state on key flag changes ---
            if f_upper == "GREEN":
                # When we (re)start green, allow a fresh auto-white later
                self._white_set = False
            elif f_upper == "CHECKERED":
                # We're done; ensure we won't auto-white post-finish
                self._white_set = True
            # For manual operator colors (YELLOW/RED/BLUE/WHITE), don't touch _white_set here.

            # If we leave GREEN after the white window began and WHITE hasn't fired, block auto-WHITE later
            if (prev == "green"
                and f_lower in {"red", "yellow", "blue"}
                and self._white_window_begun
                and not self._white_set):
                self._white_set = True

            self.flag = f
            if f == "green":
                if not self.running:
                    self.running = True
                    self.clock_start_monotonic = time.perf_counter()
            elif f == "checkered":
                # freeze at current time
                self._update_clock()
                self.running = False
                self.clock_start_monotonic = None
            elif f in {"pre","yellow","red","white","blue"}:
                # no change to running (per our rules)
                pass

            self._emit_flag_change(self.flag)
            return self.snapshot()
        
    # ---------- simulator indicator  ----------
    def set_sim(self, on: bool, label: str | None = None):
        with self._lock:
            self.sim_active = bool(on)
            # Empty string should clear the label
            self.sim_banner = (label if (label and label.strip()) else None)
            self._last_update_utc = UTC_MS()
            return self.snapshot()


    def _emit_flag_change(self, flag:str):
        ev = {"ts_utc": UTC_MS(), "race_clock_ms": self.clock_ms, "event":"flag_change", "flag": flag}
        self._events_ring.append(ev)
        if len(self._events_ring) > 500:
            self._events_ring = self._events_ring[-500:]
        self.journal.put((self.race_id or 0, ev["ts_utc"], self.clock_ms, "flag_change", {"flag":flag}))

    def _update_clock(self):
        if self.running and self.clock_start_monotonic is not None:
            now = time.perf_counter()
            delta_ms = int((now - self.clock_start_monotonic) * 1000)
            self.clock_ms += max(0, delta_ms)

            # Mark when T-60 window begins (only for time-limited ≥ 60s)
            if (self._limit_type == "time" and self._time_limit_s >= 60):
                threshold_ms = int((self._time_limit_s - 60) * 1000)
                if not self._white_window_begun and self.clock_ms >= threshold_ms:
                    self._white_window_begun = True

            self.clock_start_monotonic = now

            # Time-limit enforcement
            if (not self._limit_reached
                and self._limit_ms is not None
                and self.flag != "checkered"
                and self._limit_type == "time"
                and self._time_limit_s > 0
                and not self.soft_end
                and self.clock_ms >= self._limit_ms):
                self._auto_checkered("time_limit")

    # ---------- passes & pits ----------
    def ingest_pass(self, tag:str, ts_ns:Optional[int]=None, source:str="track", device_id:Optional[str]=None) -> dict:
        tag = str(tag).strip()
        src = (source or "track").lower()
        if src not in {"track","pit_in","pit_out"}:
            src = "track"

        with self._lock:
            self._update_clock()

            # route device → source if configured
            if src == "track":
                # auto-route based on device map only for pit_timing
                if self.feature_pits and device_id:
                    devmap = self.cfg.get("pits",{}).get("receivers",{})
                    if device_id in set(devmap.get("pit_in",[])):  src = "pit_in"
                    if device_id in set(devmap.get("pit_out",[])): src = "pit_out"

            eid = self.tag_to_eid.get(tag)
            if eid is None:
                if self.feature_auto_prov:
                    if len([e for e in self.entrants.values() if e.name.startswith("Unknown ")]) >= self._provisional_cap:
                        return {"ok": False, "entrant_id": None, "lap_added": False, "lap_time_s": None,
                                "reason": "provisional_cap"}
                    # make "Unknown ####"
                    suffix = tag[-4:].rjust(4,"0")
                    new_id = self._alloc_provisional_id()
                    ent = Entrant(new_id, enabled=True, status="ACTIVE", tag=tag, number=None, name=f"Unknown {suffix}")
                    self.entrants[new_id] = ent
                    self.tag_to_eid[tag] = new_id
                    eid = new_id
                else:
                    # ignore unknown
                    return {"ok": True, "entrant_id": None, "lap_added": False, "lap_time_s": None, "reason": "unknown_tag"}

            ent = self.entrants.get(eid)
            if not ent or not ent.enabled:
                return {"ok": True, "entrant_id": eid, "lap_added": False, "lap_time_s": None, "reason": "disabled"}

            # journal this raw event
            self.journal.put((self.race_id or 0, UTC_MS(), self.clock_ms, "pass", {
                "tag": tag, "source": src, "device_id": device_id
            }))

            # pit logic
            if self.feature_pits and src in {"pit_in","pit_out"}:
                if src == "pit_in":
                    # start (or restart) a pit window
                    ent.pit_open_at_ms = self.clock_ms
                else:
                    if ent.pit_open_at_ms is not None:
                        dur_ms = max(0, self.clock_ms - ent.pit_open_at_ms)
                        ent.last_pit_s = dur_ms / 1000.0
                        ent.pit_count += 1
                        ent.pit_open_at_ms = None
                return {"ok": True, "entrant_id": eid, "lap_added": False, "lap_time_s": None, "reason": "pit_event"}

            # track (lap) logic — red still counts per your rule. checkered freezes (no increments)
            if self.flag == "checkered":
                return {"ok": True, "entrant_id": eid, "lap_added": False, "lap_time_s": None, "reason": "checkered_freeze"}

            # derive lap time from entrant’s last hit; we measure by engine clock deltas
            prev_mark = ent._last_hit_ms
            ent._last_hit_ms = self.clock_ms

            lap_added = False
            lap_time_s = None
            if prev_mark is not None:
                delta_s = (self.clock_ms - prev_mark) / 1000.0
                # reject duplicates quickly
                if delta_s < self.min_lap_dup:
                    return {"ok": True, "entrant_id": eid, "lap_added": False, "lap_time_s": None, "reason": "dup"}
                if delta_s < self.min_lap_s:
                    return {"ok": True, "entrant_id": eid, "lap_added": False, "lap_time_s": None, "reason": "min_lap"}
                # count the lap
                ent.laps += 1
                ent.last_s = delta_s
                if ent.best_s is None or delta_s < ent.best_s:
                    ent.best_s = delta_s
                ent.pace_buf.append(delta_s)
                if len(ent.pace_buf) > 5:
                    ent.pace_buf = ent.pace_buf[-5:]
                self._maybe_auto_white_lap()
                lap_added = True
                lap_time_s = round(delta_s, 3)
                # Lap-limit enforcement
                if (not self._limit_reached
                    and self._limit_laps is not None
                    and self.flag != "checkered"
                    and ent.laps >= self._limit_laps):
                    self._auto_checkered("lap_limit")
            # else: first crossing sets start mark; no lap yet

            self._last_update_utc = UTC_MS()
            self._maybe_checkpoint()
            return {"ok": True, "entrant_id": eid, "lap_added": lap_added, "lap_time_s": lap_time_s, "reason": None}


    # ---------- auto checkered flag section ----------
    def _auto_checkered(self, reason: str):
        """Finish the race due to limit; assumes caller holds the lock."""
        if self.flag == "checkered" or self._limit_reached:
            return
        # freeze clock
        self._update_clock()
        self.running = False
        self.clock_start_monotonic = None
        self.flag = "checkered"
        self._limit_reached = True
        # emit event for journal/debug
        self._emit_flag_change("checkered")

    # ---------- internal helpers ----------
    def _elapsed_s(self) -> float:
        """Return elapsed seconds since GREEN (0 if not running)."""
        # If you already expose clock_ms from the main clock updater, prefer that path.
        try:
            return max(0.0, float(self.clock_ms) / 1000.0)
        except Exception:
            # Fallback: derive from our monotonic clock if we're currently running.
            if getattr(self, "running", False) and self.clock_start_monotonic is not None:
                return max(0.0, time.perf_counter() - self.clock_start_monotonic)
            return 0.0

    def _leader_laps(self) -> int:
        """Return the current leader's lap count (0 if unknown)."""
        # With standings derived from entrants, grab the highest lap among enabled drivers.
        try:
            laps = [int(ent.laps or 0) for ent in self.entrants.values() if getattr(ent, "enabled", True)]
            return max(laps) if laps else 0
        except Exception:
            # As a final fallback, treat the leader as unknown.
            return 0

    def _maybe_auto_white_time(self) -> None:
        """Time mode: throw WHITE at T-60s (skip if T<60 or soft/free play)."""
        if self._white_set:
            return

        flag = (self.flag or "").lower()  # normalize once
        if flag in ("checkered", "red", "yellow", "blue"):
            # Respect race control; don't force WHITE over operator colors.
            return

        if self._limit_type != "time" or self._time_limit_s <= 0:
            return

        # Skip auto-white in soft-end / free-play
        if bool(getattr(self, "soft_end", False)):
            return

        # Only consider after window begins
        if not self._white_window_begun:
            return

        # Only auto-white if currently GREEN (we entered/remained GREEN through window)
        if flag != "green":
            return

        remaining = self._time_limit_s - self._elapsed_s()
        if 0.0 < remaining <= 60.0:
            try:
                self.set_flag("WHITE")
                self._white_set = True
            except Exception:
                pass

    def _maybe_auto_white_lap(self) -> None:
        """Lap mode: throw WHITE when leader starts the final lap."""
        if self._white_set:
            return
        if self.flag in ("CHECKERED", "RED", "YELLOW", "BLUE"):
            return
        if self._limit_type != "laps" or self._lap_limit <= 0:
            return
        if self.flag not in ("GREEN", "WHITE"):
            return
        leader = self._leader_laps()
        if leader >= max(0, self._lap_limit - 1):
            # Leader is at final lap threshold—signal WHITE exactly once.
            try:
                self.set_flag("WHITE")
                self._white_set = True
            except Exception:
                pass
            
    def _alloc_provisional_id(self) -> int:
        # ensure we don't collide with explicit entrant_ids
        while self._next_provisional_id in self.entrants:
            self._next_provisional_id += 1
        eid = self._next_provisional_id
        self._next_provisional_id += 1
        return eid

    # ---------- roster management ----------
    def update_entrant_enable(self, entrant_id:int, enabled:bool) -> dict:
        with self._lock:
            ent = self.entrants.get(int(entrant_id))
            if not ent: raise KeyError("entrant not found")
            ent.enabled = bool(enabled)
            # rebuild tag map simply
            self._rebuild_tag_index()
            self._last_update_utc = UTC_MS()
            self.journal.put((self.race_id or 0, self._last_update_utc, self.clock_ms, "entrant_enable",
                              {"entrant_id": ent.entrant_id, "enabled": ent.enabled}))
            return self.snapshot()

    def update_entrant_status(self, entrant_id:int, status:str) -> dict:
        s = str(status).upper()
        if s not in ALLOWED_STATUS:
            raise ValueError("invalid status")
        with self._lock:
            ent = self.entrants.get(int(entrant_id))
            if not ent: raise KeyError("entrant not found")
            ent.status = s
            self._last_update_utc = UTC_MS()
            self.journal.put((self.race_id or 0, self._last_update_utc, self.clock_ms, "entrant_status",
                              {"entrant_id": ent.entrant_id, "status": ent.status}))
            return self.snapshot()

    def assign_tag(self, entrant_id:int, tag:Optional[str]) -> dict:
        with self._lock:
            ent = self.entrants.get(int(entrant_id))
            if not ent: raise KeyError("entrant not found")
            ent.tag = (str(tag).strip() if tag else None)
            self._rebuild_tag_index()
            self._last_update_utc = UTC_MS()
            self.journal.put((self.race_id or 0, self._last_update_utc, self.clock_ms, "assign_tag",
                              {"entrant_id": ent.entrant_id, "tag": ent.tag}))
            return self.snapshot()

    def _rebuild_tag_index(self):
        self.tag_to_eid = {}
        for e in self.entrants.values():
            if e.enabled and e.tag:
                self.tag_to_eid[e.tag] = e.entrant_id

    # ---------- snapshot & ordering ----------
    def snapshot(self) -> dict:
        with self._lock:
            # live clock tick (don’t tick if frozen)
            self._update_clock()
            self._maybe_auto_white_time()
            self._last_update_utc = UTC_MS()
            # ordering
            entrants = list(self.entrants.values())
            # sort: laps desc → best asc → last asc → entrant_id asc
            def sort_key(e:Entrant):
                best = e.best_s if e.best_s is not None else 9e9
                last = e.last_s if e.last_s is not None else 9e9
                return (-e.laps, best, last, e.entrant_id)
            entrants.sort(key=sort_key)

            leader_best = entrants[0].best_s if entrants else None
            leader_laps = entrants[0].laps if entrants else 0

            rows = [e.as_snapshot(leader_best, leader_laps) for e in entrants if e.enabled]

            snap = {
                "flag": self.flag,
                "race_id": self.race_id,
                "race_type": self.race_type or "sprint",
                "clock_ms": self.clock_ms,
                "running": self.running,
                "standings": rows,
                "last_update_utc": self._last_update_utc,
                "source": "engine",
                "sim": self.sim_active,
                "sim_label": self.sim_banner,
                "features": {"pit_timing": self.feature_pits}
            }
            # Optional: advertise active mode & event meta (helps UIs/exports)
            if self._active_mode:
                snap["mode"] = self._active_mode
            if isinstance(self._event, dict) and self._event:
                snap["event"] = self._event
            if self._limit:
                lim = {"type": self._limit["type"], "value": self._limit["value"]}
                if self._limit_ms is not None:
                    lim["remaining_ms"] = max(0, self._limit_ms - self.clock_ms)
                snap["limit"] = lim
            return snap

    def _maybe_checkpoint(self):
        now = time.time()
        if (now - self._last_checkpoint) >= self.checkpoint_s:
            self.journal.checkpoint(self.race_id or 0, self.clock_ms, self.snapshot())
            self._last_checkpoint = now

# ----------------------------- singleton + loader -----------------------------
def _load_yaml(path:str) -> dict:
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def make_engine() -> RaceEngine:
    # Prefer merged CONFIG if available (new style)
    if CONFIG:
        return RaceEngine(CONFIG)
    # Legacy fallback: backend/config.yaml only
    here = os.path.dirname(__file__)
    cfg_path = os.path.join(here, "config.yaml")
    cfg = _load_yaml(cfg_path)
    return RaceEngine(cfg)

# Global singleton for FastAPI to import
ENGINE = make_engine()
