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
import os
import platform
import random
import signal
import sqlite3
import sys
import time
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from backend.race_engine import ENGINE

DEFAULT_RACE_ID = 99

# -------------------- helpers --------------------

def now_ms() -> int:
    return int(time.time() * 1000)

def utc_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

def clear_screen():
    os.system("cls" if platform.system() == "Windows" else "clear")

def format_clock_ms(ms: int) -> str:
    s = ms // 1000
    m = s // 60
    s = s % 60
    return f"{int(m):02d}:{int(s):02d}"

def db_path() -> Path:
    env = os.getenv("DB_PATH")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    return here.parent.parent.parent / "laps.sqlite"   # repo-root fallback

# -------------------- data types --------------------

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
    last_lap_s: Optional[float] = None
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

# -------------------- DB layer --------------------

class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.cur = self.conn.cursor()
        self._ensure_schema()

    def _ensure_schema(self):
        self.cur.executescript(r"""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS races (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  start_ts_utc    INTEGER NOT NULL,
  end_ts_utc      INTEGER,
  created_at_utc  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS entrants (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  car_num         TEXT,
  org             TEXT,
  created_at_utc  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS race_entries (
  race_id         INTEGER NOT NULL,
  entrant_id      INTEGER NOT NULL,
  PRIMARY KEY (race_id, entrant_id)
);

CREATE TABLE IF NOT EXISTS tag_assignments (
  race_id            INTEGER NOT NULL,
  entrant_id         INTEGER NOT NULL,
  tag                TEXT    NOT NULL,
  effective_from_utc INTEGER NOT NULL,
  effective_to_utc   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tag_assign_race_tag_time
  ON tag_assignments(race_id, tag, effective_from_utc, effective_to_utc);

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
""")
        self.conn.commit()

    # ----- race lifecycle -----

    def ensure_race(self, race_id: int, name: str) -> None:
        r = self.cur.execute("SELECT 1 FROM races WHERE id=?", (race_id,)).fetchone()
        if not r:
            now = now_ms()
            self.cur.execute(
                "INSERT INTO races(id,name,start_ts_utc,end_ts_utc,created_at_utc) VALUES(?,?,?,?,?)",
                (race_id, name, now, None, now)
            )
            self.conn.commit()

    def clean_race(self, race_id: int) -> None:
        self.cur.execute("DELETE FROM passes WHERE race_id=?", (race_id,))
        self.cur.execute("DELETE FROM tag_assignments WHERE race_id=?", (race_id,))
        self.cur.execute("DELETE FROM race_entries WHERE race_id=?", (race_id,))
        self.cur.execute("DELETE FROM race_state WHERE race_id=?", (race_id,))
        self.cur.execute("DELETE FROM races WHERE id=?", (race_id,))
        self.conn.commit()

    # ----- entrants + registration + tag assignment -----

    def insert_entrant(self, name: str, car_num: str, org: str) -> int:
        now = now_ms()
        self.cur.execute(
            "INSERT INTO entrants(name,car_num,org,created_at_utc) VALUES(?,?,?,?)",
            (name, car_num, org, now)
        )
        self.conn.commit()
        return int(self.cur.lastrowid)

    def ensure_race_entry(self, race_id: int, entrant_id: int) -> None:
        self.cur.execute(
            "INSERT OR IGNORE INTO race_entries(race_id, entrant_id) VALUES(?,?)",
            (race_id, entrant_id)
        )
        self.conn.commit()

    def assign_tag(self, race_id: int, entrant_id: int, tag: str, start_ms: int) -> None:
        # Close any open assignment for this entrant or tag in this race
        self.cur.execute(
            "UPDATE tag_assignments SET effective_to_utc=? "
            "WHERE race_id=? AND (entrant_id=? OR tag=?) AND effective_to_utc IS NULL",
            (start_ms, race_id, entrant_id, tag)
        )
        # Open a new assignment window
        self.cur.execute(
            "INSERT INTO tag_assignments(race_id, entrant_id, tag, effective_from_utc, effective_to_utc) "
            "VALUES(?,?,?,?,NULL)",
            (race_id, entrant_id, tag, start_ms)
        )
        self.conn.commit()

    # ----- writes -----

    def insert_pass(self, race_id: int, tag: str, ts_utc: int) -> None:
        self.cur.execute(
            "INSERT INTO passes(race_id, tag, ts_utc, source, created_at_utc) VALUES(?,?,?,?,?)",
            (race_id, tag, ts_utc, "sim", now_ms())
        )
        self.conn.commit()

    def upsert_race_state(self, race_id: int, *, started_at_utc, clock_ms, flag, running, race_type, sim, sim_label):
        self.cur.execute(
            "INSERT INTO race_state(race_id,started_at_utc,clock_ms,flag,running,race_type,sim,sim_label,source) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(race_id) DO UPDATE SET started_at_utc=excluded.started_at_utc, clock_ms=excluded.clock_ms, "
            "flag=excluded.flag, running=excluded.running, race_type=excluded.race_type, sim=excluded.sim, "
            "sim_label=excluded.sim_label, source=excluded.source",
            (race_id, started_at_utc, clock_ms, flag, int(running), race_type, int(sim), sim_label, "sim")
        )
        self.conn.commit()
    
    def emit_pass(tag: str, ts_ns: int | None = None):
        ENGINE.ingest_pass(tag, ts_ns)

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
                sys.stdout.write(" " * max(len(self._last_drawn), len(line)))
                sys.stdout.write("\rEnter command > ")
                sys.stdout.write(self.buffer)
        sys.stdout.flush()
        self._last_drawn = "Enter command > " + self.buffer

    def _backspace(self):
        if self.buffer:
            self.buffer = self.buffer[:-1]
            # move back one char, erase, move back again
            sys.stdout.write("\b \b")
            sys.stdout.flush()
            self._last_drawn = ""  # force redraw next time

    def poll(self):
        if platform.system() == "Windows":
            import msvcrt
            got = None
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    sys.stdout.write("\n"); sys.stdout.flush()
                    got, self.buffer = self.buffer.strip(), ""
                    self._last_drawn = ""  # force fresh prompt after processing
                elif ch == "\x08":      # backspace
                    self._backspace()
                elif ch == "\x1b":      # ESC clears
                    self.buffer = ""
                    self.draw_prompt(reset=True)
                elif ch.isprintable():
                    self.buffer += ch
                    sys.stdout.write(ch)
                    sys.stdout.flush()
                    # don’t update _last_drawn here; we’re echoing char-by-char
            return got
        # Non-Windows fallback (blocking)
        try:
            self.draw_prompt(reset=True)
            line = input()
            return line.strip()
        except EOFError:
            return None


# -------------------- simulator --------------------

class Simulator:
    """
    Entrant-first model (tag can change mid-race; results stick to entrant).
    Console shows: Position | Car Num | Racer Name | Laps | Last | Best
    """
    def __init__(self, args):
        self.args = args
        self.db = DB(db_path())
        self.state = SimState(race_id=args.race_id, race_type=args.race_type)
        random.seed(args.seed)

        # Wipe race rows at start unless user keeps
        if not args.keep_on_start:
            self.db.clean_race(self.state.race_id)

        # Create the race
        self.db.ensure_race(self.state.race_id, name=f"Sim Race {self.state.race_id}")

        # Synthetic entrants: name + car + org + tag assignment
        self.entrants: List[EntrantSim] = []
        n = max(3, min(24, args.synthetic_teams))
        base_tag = 3000000 + 10*self.state.race_id
        self.epoch_ms = now_ms()

        for i in range(n):
            name = f"Racer {i+1}" if not args.teams else args.teams.split(",")[i].strip() if i < len(args.teams.split(",")) else f"Racer {i+1}"
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
        if f == "green" and not self.state.started:
            self.on_first_green()

    def on_first_green(self):
        # Sprint: grid release -> one crossing per entrant at t=0
        if self.args.race_type == "sprint" and self.args.grid_release:
            for e in self.entrants:
                self.db.insert_pass(self.state.race_id, e.tag, self.epoch_ms + int(self.state.sim_clock_s * 1000))
                e.next_pass_t = self.sample_lap(e)
        else:
            for e in self.entrants:
                e.next_pass_t = self.sample_lap(e)
        self.state.started = True

    def sample_lap(self, e: EntrantSim) -> float:
        t = random.gauss(e.mean_lap, e.stddev)
        t = max(self.args.lap_min, min(self.args.lap_max, t))
        return self.state.sim_clock_s + t

    def tick_events(self):
        # Auto-blue while running
        if self.state.running:
            for (s, e) in self.state.blue_schedule_s:
                if s <= self.state.sim_clock_s < e:
                    self.state.blue_until_s = max(self.state.blue_until_s, e)
                    if self.state.flag == "green":
                        self.state.flag = "blue"
                    break
            if self.state.flag == "blue" and self.state.sim_clock_s >= self.state.blue_until_s:
                self.state.flag = "green"

        # Generate passes only during racing flags
        if self.state.running and self.state.flag in ("green", "white", "blue"):
            nxt_idx, nxt_t = None, float("inf")
            for i, ent in enumerate(self.entrants):
                if ent.next_pass_t > 0 and ent.next_pass_t < nxt_t:
                    nxt_idx, nxt_t = i, ent.next_pass_t
            if nxt_idx is not None and nxt_t <= self.state.sim_clock_s:
                ent = self.entrants[nxt_idx]
                ts = self.epoch_ms + int(ent.next_pass_t*1000)
                self.db.insert_pass(self.state.race_id, ent.tag, ts)
                # console stats only
                if ent.last_lap_s is None:
                    ent.last_lap_s = nxt_t
                    ent.best_lap_s = ent.last_lap_s
                else:
                    lap_s = nxt_t - (nxt_t - ent.last_lap_s)
                    ent.last_lap_s = lap_s
                    ent.best_lap_s = min(ent.best_lap_s or lap_s, lap_s)
                ent.laps += 1
                ent.next_pass_t = self.sample_lap(ent)

    # --- console rendering ---

    def _rows(self):
        # Sort by laps desc, then soonest next crossing
        rows = []
        for e in self.entrants:
            rows.append((e.name, e.car, e.laps, e.last_lap_s, e.best_lap_s, e.next_pass_t))
        rows.sort(key=lambda r: (-r[2], r[5]))
        return rows

    def _view(self) -> str:
        lines = []
        lines.append(f"PRS Simulator Feed — Race {self.state.race_id}")
        lines.append(f"Race Type: {self.state.race_type}   Speed: {self.state.speed:.2f}x   Flag: {self.state.flag}   Running: {self.state.running}")
        lines.append(f"Clock: {format_clock_ms(int(self.state.sim_clock_s*1000))}   Entrants: {len(self.entrants)}   DB: {self.db.path}")
        lines.append("")
        lines.append("Pos | Car | Racer Name                 | Laps | Last(s) | Best(s)")
        lines.append("----+-----+----------------------------+------+---------+--------")
        for i, (name, car, laps, last, best, _) in enumerate(self._rows(), start=1):
            last_s = f"{last:5.2f}" if last else "  —  "
            best_s = f"{best:5.2f}" if best else "  —  "
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


    # --- main loop ---

    def run(self):
        self.cmd = CommandInput()
        last_wall = time.perf_counter()

        # initial overlay
        self.db.upsert_race_state(
            self.state.race_id,
            started_at_utc=None,
            clock_ms=int(self.state.sim_clock_s*1000),
            flag=self.state.flag,
            running=self.state.running,
            race_type=self.state.race_type,
            sim=True,
            sim_label="SIMULATOR ACTIVE",
        )
        self.render(force=True)

        render_interval = 0.5
        next_render = 0.0

        while not self.state.quit:
            now = time.perf_counter()
            dt = now - last_wall
            last_wall = now

            if self.state.running:
                self.state.sim_clock_s += dt * max(0.05, self.state.speed)

            self.tick_events()

            started_at = (self.epoch_ms if (self.state.started and self.state.flag != "pre") else None)
            self.db.upsert_race_state(
                self.state.race_id,
                started_at_utc=started_at,
                clock_ms=int(self.state.sim_clock_s*1000),
                flag=self.state.flag,
                running=self.state.running,
                race_type=self.state.race_type,
                sim=True,
                sim_label="SIMULATOR ACTIVE",
            )

            next_render -= dt
            if next_render <= 0:
                self.render()
                next_render = render_interval

            cmd = self.cmd.poll()
            if cmd:
                c = cmd.strip().lower()
                if c in ("s","start"):
                    self.state.running = True
                    if self.state.flag == "pre":
                        self.set_flag("green")
                    self.render(force=True)
                    # ...after processing the command...
                    # if you didn't call render(force=True), at least refresh the prompt:
                    self.cmd.draw_prompt(reset=True)
                elif c in ("p","pause"):
                    self.state.running = False
                elif c in ("t","toggle"):
                    self.state.running = not self.state.running
                    if self.state.running and self.state.flag == "pre":
                        self.set_flag("green")
                elif c in ("g","green"):   self.set_flag("green")
                elif c in ("y","yellow"):  self.set_flag("yellow")
                elif c in ("r","red"):     self.set_flag("red")
                elif c in ("w","white"):   self.set_flag("white")
                elif c in ("b","blue"):
                    self.state.flag = "blue"
                    self.state.blue_until_s = self.state.sim_clock_s + self.args.blue_duration_sec
                elif c in ("c","checkered","chk"):
                    self.set_flag("checkered")
                elif c in ("+","plus","faster"):
                    self.state.speed = min(8.0, self.state.speed * 1.25)
                elif c in ("-","minus","slower"):
                    self.state.speed = max(0.1, self.state.speed / 1.25)
                elif c in ("n","next"):
                    if not self.state.running:
                        # fire one pass for the earliest scheduled entrant
                        ent = min(self.entrants, key=lambda x: x.next_pass_t if x.next_pass_t>0 else 9e9)
                        if ent and ent.next_pass_t>0:
                            ts = self.epoch_ms + int(max(ent.next_pass_t, self.state.sim_clock_s)*1000)
                            self.db.insert_pass(self.state.race_id, ent.tag, ts)
                            ent.laps += 1
                            ent.next_pass_t = self.sample_lap(ent)
                            self.render(force=True)
                elif c in ("x","pre","reset"):
                    # reset to pre-grid, keep entrants/assignments
                    self.state.running = False
                    self.state.flag = "pre"
                    self.state.started = False
                    self.state.sim_clock_s = 0.0
                    self.state.blue_until_s = 0.0
                    self.db.upsert_race_state(
                        self.state.race_id,
                        started_at_utc=None,
                        clock_ms=0,
                        flag="pre",
                        running=False,
                        race_type=self.state.race_type,
                        sim=True,
                        sim_label="SIMULATOR ACTIVE",
                    )
                    self.render(force=True)
                elif c in ("q","quit","exit"):
                    self.state.quit = True

            time.sleep(0.05)

        # shutdown overlay + cleanup
        self.state.running = False
        self.state.flag = "pre"
        self.state.sim_clock_s = 0.0
        self.db.upsert_race_state(
            self.state.race_id,
            started_at_utc=None,
            clock_ms=0,
            flag="pre",
            running=False,
            race_type=self.state.race_type,
            sim=False,
            sim_label="",
        )
        if not self.args.keep_on_exit:
            self.db.clean_race(self.state.race_id)
        clear_screen()
        print("Simulator ended.", "(kept data)" if self.args.keep_on_exit else "(cleaned race)")

# -------------------- CLI --------------------

def parse_args():
    p = argparse.ArgumentParser(description="PRS Simulator Feed (entrant-based)")
    p.add_argument("--race-id", type=int, default=DEFAULT_RACE_ID)
    p.add_argument("--race-type", choices=["sprint","endurance","qualifying"], default="sprint")
    p.add_argument("--teams", type=str, default="", help="Optional comma list of racer names (used as display names)")
    p.add_argument("--synthetic-teams", type=int, default=12)
    p.add_argument("--grid-release", action="store_true", default=True)
    p.add_argument("--no-grid-release", dest="grid_release", action="store_false")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--lap-min", type=float, default=8.0)
    p.add_argument("--lap-max", type=float, default=22.0)
    p.add_argument("--lap-jitter", type=float, default=1.8)
    p.add_argument("--blue-every-mins", type=float, default=0.0)
    p.add_argument("--blue-at", type=lambda s: [float(x) for x in s.split(",")], default=[])
    p.add_argument("--blue-duration-sec", type=float, default=20.0)
    p.add_argument("--keep", dest="keep_on_exit", action="store_true")
    p.add_argument("--keep-on-start", action="store_true")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()

def main():
    args = parse_args()
    sim = Simulator(args)
    def handle_sig(sig, frame): sim.state.quit = True
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    sim.run()

if __name__ == "__main__":
    main()
