import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import serial

INIT_7DIGIT = bytes([1, 37, 13, 10])  # \x01 '%' '\r' '\n'

def repo_root() -> Path:
    # .../ChronoCoreRS
    return Path(__file__).resolve().parents[1]

def db_path() -> Path:
    # Keep local console DB at repo root unless CC_DB points elsewhere
    env = os.getenv("CC_DB_PATH")
    if env:
        return Path(env)
    return repo_root() / "laps.sqlite"

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_ts_utc   TEXT    NOT NULL,   -- ISO8601 UTC when received
    port          TEXT    NOT NULL,   -- e.g., COM3
    decoder_id    INTEGER NOT NULL,
    tag_id        INTEGER NOT NULL,
    decoder_secs  REAL    NOT NULL,   -- secs since decoder clock was reset
    raw_line      TEXT    NOT NULL
);

-- Helpful indexes
CREATE INDEX IF NOT EXISTS idx_passes_tag_time ON passes(tag_id, decoder_secs);
CREATE INDEX IF NOT EXISTS idx_passes_time ON passes(decoder_secs);
"""

def ensure_db() -> sqlite3.Connection:
    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn

def parse_pass_line(txt: str):
    """
    Expected decoded TXT line for a pass:
      "@\\t<decoder_id>\\t<tag_id>\\t<secs.mss>"
    Returns (decoder_id:int, tag_id:int, t_secs:float) or None.
    """
    if not txt.startswith("@"):
        return None
    parts = txt.split("\t")
    if len(parts) < 4:
        return None
    try:
        decoder_id = int(parts[1])
        tag_id = int(parts[2])
        t_secs = float(parts[3])
        return decoder_id, tag_id, t_secs
    except ValueError:
        return None

def run_session(port: str, min_lap: float = 3.0):
    """
    Minimal console race session:
      - sends 7-digit init (resets decoder clock)
      - prints/logs each pass
      - filters laps by min_lap to avoid duplicates
    """
    ser = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1, timeout=0.25)
    ser.rts = False
    time.sleep(0.2)

    conn = ensure_db()
    cur = conn.cursor()

    last_time_by_tag = {}
    lap_counts = defaultdict(int)

    print("Sending 7-digit init (also resets decoder clock to 0.000)…")
    ser.write(INIT_7DIGIT)
    ser.flush()
    print("Waiting for passes… (Ctrl+C to stop)")

    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue

            txt = raw.decode(errors="replace").strip()
            print(f"RAW: {raw!r}  TXT: {txt}")

            # Parse pass packets only (Framed with 0x01 '@' ... '\r\n')
            if raw.startswith(b"\x01@"):
                parsed = parse_pass_line(txt.replace("\x01", ""))
                if not parsed:
                    continue

                decoder_id, tag_id, t_secs = parsed

                ok = False
                prev_t = last_time_by_tag.get(tag_id)
                if prev_t is None or (t_secs - prev_t) >= min_lap:
                    ok = True
                    last_time_by_tag[tag_id] = t_secs
                    lap_counts[tag_id] += 1

                cur.execute(
                    "INSERT INTO passes (host_ts_utc, port, decoder_id, tag_id, decoder_secs, raw_line) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(), port, decoder_id, tag_id, t_secs, txt),
                )
                conn.commit()

                if ok:
                    print(f"LAP {lap_counts[tag_id]:>3}  tag={tag_id}  t={t_secs:.3f}s")
                else:
                    dt = (t_secs - prev_t) if prev_t is not None else 0.0
                    print(f"(dup/too-fast) tag={tag_id}  dt={dt:.3f}s < min_lap={min_lap:.3f}s")

    except KeyboardInterrupt:
        print("\nStopping session…")

    finally:
        conn.commit()
        conn.close()
        ser.close()
        print("Closed serial and DB.")

def main():
    parser = argparse.ArgumentParser(description="PRS – Minimal console race session")
    parser.add_argument("port", help="Serial port (e.g. COM3 or /dev/ttyUSB0)")
    parser.add_argument("--min-lap", type=float, default=3.0, help="Minimum lap seconds (dup filter)")
    args = parser.parse_args()
    run_session(args.port, min_lap=args.min_lap)

if __name__ == "__main__":
    import os
    main()
