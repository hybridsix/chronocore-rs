#!/usr/bin/env python3
"""
PRS Simulator Feed (entrant-based, new schema)

- Creates a race, entrants (name+car), registers them for the race,
  assigns a TAG to each entrant (time-bounded), and writes passes.
- Race clock is simulated (speed changes don't skew lap deltas).
- On 's' (start): for sprint, does an optional grid release (one crossing each).
- On 'x' (pre): resets to pre-grid (clock=0, flag='pre', running=False).
- On quit: sets sim badge off and (unless --keep) cleans the race rows.

UI prompt stays visible: "Enter command >".
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import random
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# -------------------- defaults --------------------

DEFAULT_DB_PATH = Path("./laps.sqlite")
DEFAULT_RACE_ID = 99

# -------------------- util --------------------

def now_ms() -> int:
    return int(time.time() * 1000)

def clear_screen():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

def format_clock_ms(ms: int) -> str:
    if ms is None:
        return "--:--"
    s = max(0, ms) // 1000
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}"

# -------------------- schema helpers --------------------

SCHEMA_RACE = """
CREATE TABLE IF NOT EXISTS races (
  id              INTEGER PRIMARY KEY,
  name            TEXT,
  race_type       TEXT,
  created_at_utc  INTEGER
);
"""

SCHEMA_ENTRANTS = """
CREATE TABLE IF NOT EXISTS entrants (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  car_num         TEXT,
  org             TEXT
);
"""

SCHEMA_RACE_ENTRIES = """
CREATE TABLE IF NOT EXISTS race_entries (
  id              INTEGER PRIMARY KEY,
  race_id         INTEGER NOT NULL,
  entrant_id      INTEGER NOT NULL,
  created_at_utc  INTEGER NOT NULL,
  UNIQUE(race_id, entrant_id)
);
"""

SCHEMA_TAG_ASSIGNMENTS = """
CREATE TABLE IF NOT EXISTS tag_assignments (
  id                  INTEGER PRIMARY KEY,
  race_id             INTEGER NOT NULL,
  entrant_id          INTEGER NOT NULL,
  tag                 TEXT NOT NULL,
  effective_from_utc  INTEGER NOT NULL,
  effective_to_utc    INTEGER,
  UNIQUE(race_id, tag, effective_from_utc)
);
CREATE INDEX IF NOT EXISTS idx_tag_by_race_tag ON tag_assignments(race_id, tag);
CREATE INDEX IF NOT EXISTS idx_tag_by_time ON tag_assignments(race_id, tag, effective_from_utc);
CREATE UNIQUE INDEX IF NOT EXISTS tag_active_unique
  ON tag_assignments(race_id, tag, effective_from_utc, effective_to_utc);
"""

SCHEMA_PASSES = """
CREATE TABLE IF NOT EXISTS passes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  race_id         INTEGER NOT NULL,
  tag             TEXT NOT NULL,
  ts_utc          INTEGER NOT NULL,
  source          TEXT DEFAULT 'sim',
  device_id       TEXT,
  meta_json       TEXT,
  created_at_utc  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passes_race_tag_ts ON passes(race_id, tag, ts_utc);
"""

SCHEMA_STATE = """
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
"""

# -------------------- DB layer --------------------

class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.cur = self.conn.cursor()
        self._init_schema()

    def _init_schema(self):
        self.cur.executescript(
            SCHEMA_RACE + SCHEMA_ENTRANTS + SCHEMA_RACE_ENTRIES +
            SCHEMA_TAG_ASSIGNMENTS + SCHEMA_PASSES + SCHEMA_STATE
        )
        self.conn.commit()

    # ----- race lifecycle -----

    def ensure_race(self, race_id: int, race_type: str) -> None:
        self.cur.execute("SELECT id FROM races WHERE id=?", (race_id,))
        row = self.cur.fetchone()
        if not row:
            self.cur.execute(
                "INSERT INTO races (id, name, race_type, created_at_utc) VALUES (?, ?, ?, ?)",
                (race_id, f"Race {race_id}", race_type, now_ms())
            )
            self.conn.commit()

    def upsert_race_state(self, race_id: int, *, started_at: Optional[int], clock_ms: int,
                          flag: str, running: bool, race_type: str, sim_label: str = "SIM") -> None:
        self.cur.execute("SELECT race_id FROM race_state WHERE race_id=?", (race_id,))
        exists = self.cur.fetchone() is not None
        if exists:
            self.cur.execute("""
                UPDATE race_state
                   SET started_at_utc=?, clock_ms=?, flag=?, running=?, race_type=?, sim=1, sim_label=?, source='sim'
                 WHERE race_id=?
            """, (started_at, clock_ms, flag, int(running), race_type, sim_label, race_id))
        else:
            self.cur.execute("""
                INSERT INTO race_state (race_id, started_at_utc, clock_ms, flag, running, race_type, sim, sim_label, source)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, 'sim')
            """, (race_id, started_at, clock_ms, flag, int(running), race_type, sim_label))
        self.conn.commit()

    # ----- participants -----

    def insert_entrant(self, *, name: str, car_num: str, org: str) -> int:
        self.cur.execute(
            "INSERT INTO entrants (name, car_num, org) VALUES (?, ?, ?)",
            (name, car_num, org)
        )
        self.conn.commit()
        return int(self.cur.lastrowid)

    def ensure_race_entry(self, race_id: int, entrant_id: int) -> None:
        self.cur.execute(
            "INSERT OR IGNORE INTO race_entries (race_id, entrant_id, created_at_utc) VALUES (?, ?, ?)",
            (race_id, entrant_id, now_ms())
        )
        self.conn.commit()

    def assign_tag(self, race_id: int, entrant_id: int, tag: str, *, start_ms: int) -> None:
        self.cur.execute("""
            INSERT INTO tag_assignments (race_id, entrant_id, tag, effective_from_utc)
            VALUES (?, ?, ?, ?)
        """, (race_id, entrant_id, tag, start_ms))
        self.conn.commit()

    # ----- timing -----

    def insert_pass(self, race_id: int, tag: str, ts_ms: Optional[int] = None, **meta) -> None:
        ts_ms = ts_ms if ts_ms is not None else now_ms()
        self.cur.execute("""
            INSERT INTO passes (race_id, tag, ts_utc, source, meta_json, created_at_utc)
            VALUES (?, ?, ?, 'sim', ?, ?)
        """, (race_id, tag, ts_ms, json.dumps(meta) if meta else None, now_ms()))
        self.conn.commit()

    # ----- cleanup -----

    def delete_race(self, race_id: int) -> None:
        self.cur.execute("DELETE FROM passes WHERE race_id=?", (race_id,))
        self.cur.execute("DELETE FROM tag_assignments WHERE race_id=?", (race_id,))
        self.cur.execute("DELETE FROM race_entries WHERE race_id=?", (race_id,))
        self.cur.execute("DELETE FROM race_state WHERE race_id=?", (race_id,))
        self.cur.execute("DELETE FROM races WHERE id=?", (race_id,))
        self.conn.commit()

# -------------------- model --------------------

@dataclass
class EntrantSim:
    entrant_id: int
    tag: str
    name: str
    car: str
    org: str
    mean_lap: float
    stddev: float = 1.8
    next_pass_t: float = 0.0
    last_lap_s: Optional[float] = None      # duration of most recent completed lap
    last_cross_s: Optional[float] = None    # sim clock time of last line crossing
    best_lap_s: Optional[float] = None
    laps: int = 0

@dataclass
class SimState:
    race_id: int = DEFAULT_RACE_ID
    race_type: str = "sprint"           # 'sprint' | 'endurance' | 'qualifying'
    running: bool = False
    flag: str = "pre"
    speed: float = 1.0
    sim_clock_s: float = 0.0
    started: bool = False
    quit: bool = False
    blue_until_s: float = 0.0
    blue_schedule_s: List[Tuple[float, float]] = field(default_factory=list)

# -------------------- input prompt --------------------

class CommandInput:
    """
    Stable single-line prompt:
      - draw_prompt(reset=True) after a full-screen render
      - echo as you type without duplicating the prompt
    """
    def __init__(self):
        self.buffer = ""
        self._last_drawn = ""

    def draw_prompt(self, reset=False):
        # erase the previous prompt line and redraw
        # (works well after a full-screen render or when resetting)
        sys.stdout.write("\r")
        if reset:
            # write a fresh line at the bottom
            sys.stdout.write("Enter command > ")
            sys.stdout.write(self.buffer)
        else:
            # re-draw only if changed
            line = "Enter command > " + self.buffer
            if line != self._last_drawn:
                # clear current line then redraw
                sys.stdout.write("\033[2K")
                sys.stdout.write(line)
                self._last_drawn = line
        sys.stdout.flush()

    def handle_key(self, ch: str) -> Optional[str]:
        if ch == "\n":
            cmd = self.buffer.strip()
            self.buffer = ""
            self._last_drawn = ""
            return cmd
        elif ch == "\x7f":  # backspace
            self.buffer = self.buffer[:-1]
        else:
            self.buffer += ch
        self.draw_prompt(reset=False)
        return None

# -------------------- simulator --------------------

@dataclass
class Args:
    db: Path
    race_id: int
    race_type: str
    entrants: int
    seed: int
    lap_min: float
    lap_max: float
    lap_jitter: float
    blue_every_mins: float
    blue_duration_sec: float
    blue_at: List[float]
    keep: bool
    keep_on_start: bool

class Simulator:
    def __init__(self, args: Args):
        self.args = args
        random.seed(args.seed)
        self.db = DB(args.db)
        self.state = SimState(race_id=args.race_id, race_type=args.race_type)
        self.epoch_ms = now_ms()
        self.entrants: List[EntrantSim] = []

        # Ensure race + initial state
        self.db.ensure_race(self.state.race_id, self.state.race_type)
        self.db.upsert_race_state(
            self.state.race_id,
            started_at=None,
            clock_ms=0,
            flag=self.state.flag,
            running=False,
            race_type=self.state.race_type,
            sim_label="SIM"
        )

        # Seed entrants
        base_tag = 100000
        for i in range(max(3, min(24, args.entrants))):
            name = f"Racer {i+1}"
            car  = f"{(i % 89) + 11:02d}"
            org  = f"Team {i+1}"
            tag  = str(base_tag + i + 1)
            mean = max(6.0, random.uniform(args.lap_min, args.lap_max))

            eid = self.db.insert_entrant(name=name, car_num=car, org=org)
            self.db.ensure_race_entry(self.state.race_id, eid)
            self.db.assign_tag(self.state.race_id, eid, tag, start_ms=self.epoch_ms)

            self.entrants.append(EntrantSim(
                entrant_id=eid, tag=tag, name=name, car=car, org=org, mean_lap=mean, stddev=args.lap_jitter
            ))

        # Auto-blue schedule (optional)
        if args.blue_every_mins > 0:
            step = args.blue_every_mins * 60.0
            for k in range(1, 1000):
                s = step * k
                self.state.blue_schedule_s.append((s, s + args.blue_duration_sec))
        if args.blue_at:
            for m in args.blue_at:
                s = float(m) * 60.0
                self.state.blue_schedule_s.append((s, s + args.blue_duration_sec))

        self.cmd = CommandInput()
        self._last_hash = ""

    # --- flow control ---

    def set_flag(self, f: str):
        self.state.flag = f
        self.db.upsert_race_state(
            self.state.race_id,
            started_at=self.epoch_ms if self.state.started else None,
            clock_ms=int(self.state.sim_clock_s * 1000),
            flag=f,
            running=self.state.running,
            race_type=self.state.race_type
        )
        self.render(force=True)

    def on_first_green(self):
        # one crossing per entrant to clear the grid (sprint only)
        for e in self.entrants:
            # use current sim time as the pass
            self.db.insert_pass(self.state.race_id, e.tag, self.epoch_ms + int(self.state.sim_clock_s * 1000))
            e.last_cross_s = self.state.sim_clock_s
            e.next_pass_t = self.sample_lap(e)

    def sample_lap(self, e: EntrantSim) -> float:
        # gaussian lap time around mean, clamp to sensible min
        base = random.gauss(e.mean_lap, e.stddev)
        base = max(3.0, base)
        return self.state.sim_clock_s + base

    # --- view / console ---

    def _rows(self):
        rows = []
        for e in self.entrants:
            rows.append((e.name, e.car, e.laps, e.last_lap_s, e.best_lap_s, e.next_pass_t))
        rows.sort(key=lambda r: (-r[2], r[5]))
        return rows

    def _view(self) -> str:
        lines = []
        lines.append(f"PRS Simulator Feed - Race {self.state.race_id}")
        lines.append(f"Race Type: {self.state.race_type}   Speed: {self.state.speed:.2f}x   Flag: {self.state.flag}   Running: {self.state.running}")
        lines.append(f"Clock: {format_clock_ms(int(self.state.sim_clock_s*1000))}   Entrants: {len(self.entrants)}   DB: {self.db.path}")
        lines.append("")
        lines.append("Pos | Car | Racer Name                 | Laps | Last(s) | Best(s)")
        lines.append("----+-----+----------------------------+------+---------+--------")
        for i, (name, car, laps, last, best, _) in enumerate(self._rows(), start=1):
            last_s = f"{last:5.2f}" if last is not None else "  -  "
            best_s = f"{best:5.2f}" if best is not None else "  -  "
            lines.append(f"{i:3d} | {car:>3s} | {name:<26} | {laps:4d} | {last_s:>7} | {best_s:>6}")
        lines.append("")
        lines.append("Commands: s=start  p=pause  t=toggle  g=green  y=yellow  r=red  w=white  b=blue  c=checkered  n=next  +=faster  -=slower  x=pre  q=quit")
        lines.append("")
        return "\n".join(lines)

    def render(self, force=False):
        view = self._view()
        h = hashlib.md5(view.encode()).hexdigest()
        if force or h != self._last_hash:
            clear_screen()
            print(view)
            self._last_hash = h
            # draw the prompt exactly once per full render
            self.cmd.draw_prompt(reset=True)

    # --- tick loop ---

    def write_state(self):
        self.db.upsert_race_state(
            self.state.race_id,
            started_at=self.epoch_ms if self.state.started else None,
            clock_ms=int(self.state.sim_clock_s * 1000),
            flag=self.state.flag,
            running=self.state.running,
            race_type=self.state.race_type
        )

    def tick(self, dt_real: float):
        # update simulated race clock
        if self.state.running:
            self.state.sim_clock_s += dt_real * self.state.speed

        # scheduled passes
        nxt_idx = None
        nxt_t = 1e12
        for idx, e in enumerate(self.entrants):
            if e.next_pass_t and e.next_pass_t < nxt_t:
                nxt_t = e.next_pass_t
                nxt_idx = idx

        if nxt_idx is not None and nxt_t <= self.state.sim_clock_s:
            ent = self.entrants[nxt_idx]
            ts = self.epoch_ms + int(ent.next_pass_t*1000)
            self.db.insert_pass(self.state.race_id, ent.tag, ts)
            # console stats only
            if ent.last_cross_s is None:
                # first crossing since green (after grid release): no prior lap to time
                ent.last_cross_s = nxt_t
            else:
                lap_s = nxt_t - ent.last_cross_s
                ent.last_lap_s = lap_s
                ent.best_lap_s = lap_s if ent.best_lap_s is None else min(ent.best_lap_s, lap_s)
                ent.last_cross_s = nxt_t
            ent.laps += 1
            ent.next_pass_t = self.sample_lap(ent)

        # transient blue
        if self.state.flag == "blue" and self.state.sim_clock_s >= self.state.blue_until_s:
            self.state.flag = "green"

        # scheduled blues
        for start_s, end_s in list(self.state.blue_schedule_s):
            if start_s <= self.state.sim_clock_s <= end_s:
                self.state.flag = "blue"
                self.state.blue_until_s = end_s

        # write state row
        self.write_state()

    # --- command processing ---

    def process_cmd(self, cmd: str):
        c = cmd.lower()
        if not c:
            return

        if c in ("q","quit","exit"):
            self.state.quit = True
            return

        if c in ("s","start"):
            if not self.state.started:
                self.state.started = True
                self.state.running = True
                self.state.flag = "green"
                self.epoch_ms = now_ms()
                self.on_first_green()  # grid release
            else:
                self.state.running = True
            self.render(force=True)
            return

        if c in ("p","pause"):
            self.state.running = False
            self.render(force=True)
            return

        if c in ("t","toggle"):
            self.state.running = not self.state.running
            self.render(force=True)
            return

        if c in ("g","green"):   self.set_flag("green");   return
        if c in ("y","yellow"):  self.set_flag("yellow");  return
        if c in ("r","red"):     self.set_flag("red");     return
        if c in ("w","white"):   self.set_flag("white");   return
        if c in ("b","blue"):
            self.state.flag = "blue"
            self.state.blue_until_s = self.state.sim_clock_s + self.args.blue_duration_sec
            self.render(force=True)
            return
        if c in ("c","checkered","chk"):
            self.set_flag("checkered"); return

        if c in ("+","plus","faster"):
            self.state.speed = min(8.0, self.state.speed * 1.25)
            self.render(force=True); return
        if c in ("-","minus","slower"):
            self.state.speed = max(0.1, self.state.speed / 1.25)
            self.render(force=True); return

        if c in ("n","next"):
            if not self.state.running:
                # fire one pass for the earliest scheduled entrant
                ent = min(self.entrants, key=lambda x: x.next_pass_t if x.next_pass_t>0 else 9e9)
                if ent and ent.next_pass_t>0:
                    ts = self.epoch_ms + int(max(ent.next_pass_t, self.state.sim_clock_s)*1000)
                    self.db.insert_pass(self.state.race_id, ent.tag, ts)
                    ent.laps += 1
                    ent.next_pass_t = self.sample_lap(ent)
                    self.render(force=True)
            return

        if c in ("x","pre","reset"):
            # reset to pre-grid, keep entrants/assignments
            self.state.running = False
            self.state.flag = "pre"
            self.state.started = False
            self.state.sim_clock_s = 0.0
            self.state.blue_until_s = 0.0
            self.db.upsert_race_state(
                self.state.race_id,
                started_at=None,
                clock_ms=0,
                flag=self.state.flag,
                running=False,
                race_type=self.state.race_type
            )
            self.render(force=True)
            return

        # otherwise, ignore unknown command (prompt stays)

    def run(self):
        self.render(force=True)
        last = time.perf_counter()
        while not self.state.quit:
            now = time.perf_counter()
            dt = now - last
            last = now

            # non-blocking read from stdin (very simple)
            import select
            if select.select([sys.stdin], [], [], 0)[0]:
                ch = sys.stdin.read(1)
                cmd = self.cmd.handle_key(ch)
                if cmd is not None:
                    self.process_cmd(cmd)

            self.tick(dt)
            time.sleep(0.05)  # ~20 Hz sim tick

        # on quit
        if not self.args.keep:
            self.db.delete_race(self.state.race_id)

# -------------------- args / entrypoint --------------------

def parse_args() -> Args:
    p = argparse.ArgumentParser(description="PRS Simulator Feed")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite path (laps.sqlite)")
    p.add_argument("--race-id", type=int, default=DEFAULT_RACE_ID)
    p.add_argument("--race-type", type=str, default="sprint", choices=["sprint","endurance","qualifying"])
    p.add_argument("--entrants", type=int, default=10)
    p.add_argument("--lap-min", type=float, default=8.0)
    p.add_argument("--lap-max", type=float, default=14.0)
    p.add_argument("--lap-jitter", type=float, default=1.8)
    p.add_argument("--blue-every-mins", type=float, default=0.0, dest="blue_every_mins")
    p.add_argument("--blue-at", type=float, nargs="*", default=[], help="Specific minutes to throw blue flag")
    p.add_argument("--blue-duration-sec", type=float, default=10.0)
    p.add_argument("--keep", action="store_true", help="Keep DB rows on quit")
    p.add_argument("--keep-on-start", action="store_true")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    return Args(**vars(args))

def main():
    args = parse_args()
    sim = Simulator(args)
    def handle_sig(sig, frame): sim.state.quit = True
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    sim.run()

if __name__ == "__main__":
    main()
