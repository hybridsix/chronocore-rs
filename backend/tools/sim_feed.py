#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, platform, random, signal, sqlite3, sys, time, threading, queue, hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# ---------- Constants ----------
DEFAULT_RACE_ID = 99
SIM_DECODER_BASE = 900000

# ---------- Helpers ----------
def clear_screen():
    os.system("cls" if platform.system() == "Windows" else "clear")

def utc_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

def format_clock_ms(ms: int) -> str:
    if ms < 0: return "--:--"
    s = ms // 1000
    m = s // 60
    s = s % 60
    return f"{int(m):02d}:{int(s):02d}"

def db_path() -> Path:
    # 1) Honor DB_PATH env var if set
    env = os.getenv("DB_PATH")
    if env:
        return Path(env)
    # 2) Default to repo-root laps.sqlite
    here = Path(__file__).resolve()
    return (here.parent.parent.parent / "laps.sqlite")

def table_has_column(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())

# ---------- Data ----------
@dataclass
class Entrant:
    tag_id: int
    team: str
    mean_lap: float
    stddev: float = 1.8
    next_pass_t: float = 0.0
    last_lap_s: Optional[float] = None
    best_lap_s: Optional[float] = None
    laps: int = 0

@dataclass
class SimState:
    race_id: int = DEFAULT_RACE_ID
    race_type: str = "sprint"
    running: bool = False
    flag: str = "pre"  # pre, green, yellow, red, white, blue, checkered
    speed: float = 1.0
    sim_clock_s: float = 0.0
    started: bool = False
    quit: bool = False
    blue_until_s: float = 0.0
    blue_schedule_s: List[Tuple[float, float]] = field(default_factory=list)

# ---------- DB ----------
class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.cur = self.conn.cursor()
        self._probe_schema()
        self._ensure_schema()

    def _probe_schema(self):
        self.passes_has_race_id = False
        try:
            self.cur.execute("PRAGMA table_info(passes)")
            cols = {r[1] for r in self.cur.fetchall()}
            self.passes_has_race_id = "race_id" in cols
        except sqlite3.OperationalError:
            pass
        # transponders columns
        self.trans_tag_col = "tag_id"
        self.trans_team_col = "team"
        try:
            self.cur.execute("PRAGMA table_info(transponders)")
            cols = {r[1] for r in self.cur.fetchall()}
            if "tag_id" in cols: self.trans_tag_col = "tag_id"
            elif "tag" in cols:  self.trans_tag_col = "tag"
            if "team" in cols:   self.trans_team_col = "team"
            elif "name" in cols: self.trans_team_col = "name"
        except sqlite3.OperationalError:
            pass

    def _ensure_schema(self):
        self.cur.execute("""
        CREATE TABLE IF NOT EXISTS passes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_ts_utc TEXT NOT NULL,
            port TEXT NOT NULL,
            decoder_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            decoder_secs REAL NOT NULL,
            raw_line TEXT NOT NULL
        )""")
        if not self.passes_has_race_id:
            try:
                self.cur.execute("ALTER TABLE passes ADD COLUMN race_id INTEGER")
                self.passes_has_race_id = True
            except sqlite3.OperationalError:
                pass

        # transponders
        self.cur.execute(f"""
        CREATE TABLE IF NOT EXISTS transponders (
            {self.trans_tag_col} INTEGER PRIMARY KEY,
            {self.trans_team_col} TEXT,
            car_num INTEGER
        )""")

        # race_state
        self.cur.execute("""
        CREATE TABLE IF NOT EXISTS race_state (
            race_id INTEGER PRIMARY KEY,
            running INTEGER NOT NULL,
            flag TEXT NOT NULL,
            clock_ms INTEGER NOT NULL,
            race_type TEXT,
            sim INTEGER,
            sim_label TEXT,
            source TEXT,
            updated_utc TEXT
        )""")
        self.conn.commit()

    def clean_race(self, race_id: int):
        if self.passes_has_race_id:
            self.cur.execute("DELETE FROM passes WHERE race_id=?", (race_id,))
        else:
            sim_decoder = SIM_DECODER_BASE + race_id
            self.cur.execute("DELETE FROM passes WHERE decoder_id=? AND port='SIM'", (sim_decoder,))
        self.cur.execute("DELETE FROM race_state WHERE race_id=?", (race_id,))
        self.conn.commit()

    def upsert_transponder(self, tag_id: int, team: str):
        tag_col = self.trans_tag_col
        team_col = self.trans_team_col
        try:
            self.cur.execute(
                f"INSERT INTO transponders({tag_col},{team_col}) VALUES(?,?) "
                f"ON CONFLICT({tag_col}) DO UPDATE SET {team_col}=excluded.{team_col}",
                (tag_id, team),
            )
        except sqlite3.OperationalError:
            self.cur.execute(
                f"REPLACE INTO transponders({tag_col},{team_col}) VALUES(?,?)", (tag_id, team)
            )
        self.conn.commit()

    def insert_pass(self, race_id: int, decoder_id: int, tag_id: int, decoder_secs: float):
        iso = utc_iso()
        raw = f"SIM {decoder_id} {tag_id} {decoder_secs:.3f}"
        if self.passes_has_race_id:
            self.cur.execute(
                "INSERT INTO passes(host_ts_utc, port, decoder_id, tag_id, decoder_secs, raw_line, race_id) "
                "VALUES(?,?,?,?,?,?,?)",
                (iso, "SIM", decoder_id, tag_id, float(decoder_secs), raw, race_id),
            )
        else:
            self.cur.execute(
                "INSERT INTO passes(host_ts_utc, port, decoder_id, tag_id, decoder_secs, raw_line) "
                "VALUES(?,?,?,?,?,?)",
                (iso, "SIM", decoder_id, tag_id, float(decoder_secs), raw),
            )
        self.conn.commit()

    def update_race_state(self, st: SimState, sim_label: str, sim_on: int = 1):
        self.cur.execute(
            """INSERT INTO race_state(race_id, running, flag, clock_ms, race_type, sim, sim_label, source, updated_utc)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(race_id) DO UPDATE SET
                 running=excluded.running, flag=excluded.flag, clock_ms=excluded.clock_ms,
                 race_type=excluded.race_type, sim=excluded.sim, sim_label=excluded.sim_label,
                 source=excluded.source, updated_utc=excluded.updated_utc""",
            (
                st.race_id,
                1 if st.running else 0,
                st.flag,
                int(round(st.sim_clock_s * 1000)),
                st.race_type,
                int(sim_on),
                sim_label,
                "sim",
                utc_iso(),
            ),
        )
        self.conn.commit()

# ---------- Input (Windows line reader with stable prompt) ----------
class CommandInput:
    def __init__(self):
        self.buffer = ""
        self.ready = None  # last completed command (consumed by poll)
        self.win = (platform.system() == "Windows")

    def start(self):
        # nothing to start; polled from main loop
        pass

    def stop(self):
        pass

    def _print_prompt(self):
        sys.stdout.write("Enter command > " + self.buffer)
        sys.stdout.flush()

    def poll(self) -> Optional[str]:
        if not self.win:
            # Fallback: non-Windows environments still use blocking input()
            # Show a stable prompt, read a line once, return it, otherwise None
            if self.ready is None:
                try:
                    self._print_prompt()
                    line = input()  # user presses Enter here
                    return line.strip()
                except EOFError:
                    return None
            else:
                cmd, self.ready = self.ready, None
                return cmd

        # Windows: non-blocking line assembly with msvcrt
        import msvcrt
        got_cmd = None
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):  # Enter
                sys.stdout.write("\n")
                sys.stdout.flush()
                got_cmd = self.buffer.strip()
                self.buffer = ""
            elif ch == "\x08":  # Backspace
                if self.buffer:
                    self.buffer = self.buffer[:-1]
                    # erase char on screen
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif ch == "\x1b":  # ESC clears the line
                # move cursor to line start after prompt, clear tail
                # Easiest: print CR and redraw prompt
                sys.stdout.write("\r")
                sys.stdout.flush()
                self.buffer = ""
                self._print_prompt()
            else:
                # normal printable
                if ch.isprintable():
                    self.buffer += ch
                    sys.stdout.write(ch)
                    sys.stdout.flush()
        return got_cmd



# ---------- Simulator ----------
class Simulator:
    def __init__(self, args):
        self.args = args
        self.db = DB(db_path())
        self.state = SimState(race_id=args.race_id, race_type=args.race_type)
        self.sim_label = args.sim_label
        self.decoder_id = SIM_DECODER_BASE + self.state.race_id
        self.entrants: List[Entrant] = []
        random.seed(args.seed)

        teams = [t.strip() for t in args.teams.split(",") if t.strip()] if args.teams else [f"Team {i+1}" for i in range(max(3, min(24, args.synthetic_teams)))]
        base_tag = 3000000 + 10 * self.state.race_id
        for i, team in enumerate(teams):
            tag = base_tag + i + 1
            mean = max(6.0, random.uniform(args.lap_min, args.lap_max))
            e = Entrant(tag_id=tag, team=team, mean_lap=mean, stddev=args.lap_jitter)
            e.next_pass_t = 0.0
            self.entrants.append(e)
            self.db.upsert_transponder(tag, team)

        # Auto-blue schedule
        if args.blue_every_mins > 0:
            step = args.blue_every_mins * 60.0
            for k in range(1, 1000):
                s = step * k
                self.state.blue_schedule_s.append((s, s + args.blue_duration_sec))
        if args.blue_at:
            for m in args.blue_at:
                s = float(m) * 60.0
                self.state.blue_schedule_s.append((s, s + args.blue_duration_sec))

        if not args.keep_on_start:
            self.db.clean_race(self.state.race_id)

        # input + render buffers
        self.cmd = CommandInput()
        self.last_render_hash = ""

    # ---- Flags/flow ----
    def set_flag(self, f: str):
        self.state.flag = f
        if f == "green" and not self.state.started:
            self.on_first_green()

    def on_first_green(self):
        if self.args.race_type == "sprint" and self.args.grid_release:
            for e in self.entrants:
                self.db.insert_pass(self.state.race_id, self.decoder_id, e.tag_id, 0.0)
                e.next_pass_t = self.sample_lap(e)
        else:
            for e in self.entrants:
                e.next_pass_t = self.sample_lap(e)
        self.state.started = True

    def sample_lap(self, e: Entrant) -> float:
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

        # Pass generation
        if self.state.running and self.state.flag in ("green", "white", "blue"):
            nxt_idx, nxt_t = None, float("inf")
            for i, entry in enumerate(self.entrants):
                if entry.next_pass_t > 0 and entry.next_pass_t < nxt_t:
                    nxt_t, nxt_idx = entry.next_pass_t, i
            if nxt_idx is not None and nxt_t <= self.state.sim_clock_s:
                e = self.entrants[nxt_idx]
                self.db.insert_pass(self.state.race_id, self.decoder_id, e.tag_id, e.next_pass_t)
                # rough in-console stats
                if e.last_lap_s is None:
                    e.last_lap_s = e.next_pass_t
                    e.best_lap_s = e.last_lap_s
                else:
                    lapse = e.next_pass_t - (e.next_pass_t - e.last_lap_s)
                    e.last_lap_s = lapse
                    e.best_lap_s = min(e.best_lap_s or lapse, lapse)
                e.laps += 1
                e.next_pass_t = self.sample_lap(e)

    # ---- Standings + Render ----
    def standings_snap(self):
        rows = []
        for e in self.entrants:
            rows.append((e.team, e.laps, e.last_lap_s, e.best_lap_s, e.next_pass_t))
        rows.sort(key=lambda r: (-r[1], r[4]))
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def build_view(self) -> str:
        lines = []
        lines.append(f"PRS Simulator Feed — Race {self.state.race_id}")
        lines.append(f"Race Type: {self.state.race_type}   Speed: {self.state.speed:.2f}x   Flag: {self.state.flag}   Running: {self.state.running}")
        lines.append(f"Clock: {format_clock_ms(int(self.state.sim_clock_s*1000))}   Entrants: {len(self.entrants)}   DB: {self.db.path.name}")
        lines.append("")
        lines.append("Pos | Team                        | Laps | Last(s) | Best(s)")
        lines.append("----+-----------------------------+------+---------+--------")
        for i, (team, laps, last, best) in enumerate(self.standings_snap(), start=1):
            last_s = f"{last:5.2f}" if last else "  —  "
            best_s = f"{best:5.2f}" if best else "  —  "
            lines.append(f"{i:3d} | {team:<27} | {laps:4d} | {last_s:>7} | {best_s:>6}")
        lines.append("")
        lines.append("Commands: s=start  p=pause  t=toggle  g=green  y=yellow  r=red  w=white  b=blue  c=checkered  n=next  +=faster  -=slower  x=pre  q=quit")
        lines.append("")  # leave space above the prompt
        return "\n".join(lines)

    def render(self, force=False):
        view = self.build_view()
        h = hashlib.md5(view.encode("utf-8")).hexdigest()
        if force or h != self.last_render_hash:
            clear_screen()
            print(view)
            # prompt will be printed by input thread; we just ensure space above it
            self.last_render_hash = h

    # ---- Run ----
    def run(self):
        # start input thread
        self.cmd.start()

        last_wall = time.perf_counter()
        self.db.update_race_state(self.state, self.sim_label, sim_on=1)
        self.render(force=True)

       # ensure prompt is visible immediately
        sys.stdout.write("Enter command > ")
        sys.stdout.flush()

        # target ~2 fps renders
        render_interval = 0.5
        next_render = 0.0

        while not self.state.quit:
            now = time.perf_counter()
            dt_wall = now - last_wall
            last_wall = now

            if self.state.running:
                self.state.sim_clock_s += dt_wall * max(0.05, self.state.speed)

            self.tick_events()
            self.db.update_race_state(self.state, self.sim_label, sim_on=1)

            # render throttle
            next_render -= dt_wall
            if next_render <= 0:
                self.render()
                next_render = render_interval

            # process commands (line-based)
            cmd = self.cmd.poll()
            if cmd:
                c = cmd.strip().lower()
                if c in ("s","start"):
                    self.state.running = True
                    if self.state.flag == "pre":
                        self.set_flag("green")
                    self.render(force=True)
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
                        e = min(self.entrants, key=lambda x: x.next_pass_t if x.next_pass_t>0 else 9e9)
                        if e and e.next_pass_t>0:
                            self.db.insert_pass(self.state.race_id, self.decoder_id, e.tag_id, max(e.next_pass_t, self.state.sim_clock_s))
                            e.laps += 1
                            e.next_pass_t = self.sample_lap(e)
                elif c in ("x","pre","reset"):
                    self.state.running = False
                    self.state.flag = "pre"
                    self.state.started = False
                    self.state.sim_clock_s = 0.0
                    self.state.blue_until_s = 0.0
                    self.db.update_race_state(self.state, self.sim_label, sim_on=1)
                    self.render(force=True)
                elif c in ("q","quit","exit"):
                    self.state.quit = True
                else:
                    # ignore unknowns; re-render to keep prompt tidy
                    self.render(force=True)

            time.sleep(0.05)

        # shutdown
        self.cmd.stop()
        self.state.running = False
        self.state.flag = "pre"
        self.state.sim_clock_s = 0.0
        self.db.update_race_state(self.state, self.sim_label, sim_on=0)
        if not self.args.keep_on_exit:
            self.db.clean_race(self.state.race_id)
        clear_screen()
        print("Simulator ended.", "(kept data)" if self.args.keep_on_exit else "(cleaned race)")

# ---------- CLI ----------
def parse_args():
    p = argparse.ArgumentParser(description="PRS Simulator Feed (CLI)")
    p.add_argument("--race-id", type=int, default=DEFAULT_RACE_ID)
    p.add_argument("--race-type", choices=["sprint","endurance","qualifying"], default="sprint")
    p.add_argument("--sim-label", default="SIMULATOR ACTIVE")
    p.add_argument("--synthetic", type=int, default=60)
    p.add_argument("--teams", type=str, default="")
    p.add_argument("--synthetic-teams", type=int, default=6)
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

    def handle_sig(sig, frame):
        sim.state.quit = True
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        sim.run()
    finally:
        pass

if __name__ == "__main__":
    main()


