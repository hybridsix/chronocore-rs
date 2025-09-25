import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime
from datetime import timezone
from pathlib import Path

import serial


INIT_7DIGIT = bytes([1, 37, 13, 10])  # \x01 '%' '\r' '\n'


def db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "laps.sqlite"


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
    """Open DB and ensure required tables/indexes exist."""
    db = db_path()
    new = not db.exists()
    conn = sqlite3.connect(db)
    if new:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    else:
        # Even if file exists, make sure schema is present (safe to re-run)
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
    # Open serial
    ser = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1, timeout=0.25)
    ser.rts = False
    time.sleep(0.2)

    # DB
    conn = ensure_db()
    cur = conn.cursor()

    # In-memory lap state (very simple)
    last_time_by_tag = {}
    lap_counts = defaultdict(int)

    # Reset decoder clock / enable 7-digit mode
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
            # Show raw lines for transparency while testing
            print(f"RAW: {raw!r}  TXT: {txt}")

            # Parse pass packets only
            if raw.startswith(b"\x01@"):
                parsed = parse_pass_line(txt.replace("\x01", ""))
                if not parsed:
                    continue

                decoder_id, tag_id, t_secs = parsed

                # Simple min-lap duplication filter
                ok = False
                prev_t = last_time_by_tag.get(tag_id)
                if prev_t is None or (t_secs - prev_t) >= min_lap:
                    ok = True
                    last_time_by_tag[tag_id] = t_secs
                    lap_counts[tag_id] += 1

                # Persist every pass regardless (so we have full audit)
                cur.execute(
                    "INSERT INTO passes (host_ts_utc, port, decoder_id, tag_id, decoder_secs, raw_line) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(), port, decoder_id, tag_id, t_secs, txt),
                )

                # Commit in small batches for safety during testing
                conn.commit()

                if ok:
                    print(f"LAP {lap_counts[tag_id]:>3}  tag={tag_id}  t={t_secs:.3f}s")
                else:
                    print(f"(dup/too-fast) tag={tag_id}  dt={(t_secs - prev_t):.3f}s < min_lap={min_lap:.3f}s")

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
    main()
