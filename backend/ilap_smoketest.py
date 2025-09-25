import sys
import time
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
import serial

INIT_7DIGIT = bytes([1, 37, 13, 10])  # chr(001), '%', CR, LF

DB_PATH = Path(__file__).resolve().parent.parent / "laps.sqlite"

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_ts_utc TEXT NOT NULL,        -- ISO8601 (UTC) when we received it
    port TEXT NOT NULL,               -- e.g., COM3
    decoder_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    decoder_secs REAL NOT NULL,       -- seconds since decoder clock reset
    raw_line TEXT NOT NULL            -- full raw ASCII line from decoder (for audit)
);
CREATE INDEX IF NOT EXISTS idx_passes_tag_time ON passes(tag_id, decoder_secs);
CREATE INDEX IF NOT EXISTS idx_passes_time ON passes(decoder_secs);
"""

def iso_utc_now():
    return datetime.now(timezone.utc).isoformat()

def ensure_db():
    need_init = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH)
    if need_init:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    return conn

def parse_pass_line(txt: str):
    """
    Expected TXT: '@\\t<decoder_id>\\t<tag>\\t<secs.mss>'
    The raw line came in as: \\x01@\\t...\\r\\n; we strip control chars already.
    Returns (decoder_id:int, tag_id:int, t_secs:float) or None if not valid.
    """
    # Leading control char stripped by .decode().strip(); ensure it starts with '@'
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

def main():
    if len(sys.argv) < 2:
        print("Usage: python ilap_logger.py <PORT>\n  e.g.  python ilap_logger.py COM3")
        sys.exit(1)

    port = sys.argv[1]
    conn = ensure_db()
    cur = conn.cursor()

    print(f"DB → {DB_PATH}")
    print(f"Opening {port} @ 9600 8N1 (RTS OFF)…")
    ser = serial.Serial(port, 9600, bytesize=8, parity='N', stopbits=1, timeout=2)

    try:
        ser.rts = False
        time.sleep(0.2)

        print("Sending init for 7-digit mode… (also resets decoder clock)")
        ser.write(INIT_7DIGIT); ser.flush()
        print("Waiting for data (Ctrl+C to quit)…")

        # Commit writes in small batches for performance
        pending = 0
        while True:
            raw = ser.readline()
            if not raw:
                continue

            txt = raw.decode(errors="replace").strip()
            # Show everything we see for transparency
            print(f"RAW: {raw!r}  TXT: {txt}")

            if raw.startswith(b"\x01@"):
                parsed = parse_pass_line(txt.replace("\x01", ""))
                if parsed:
                    decoder_id, tag_id, t_secs = parsed
                    print(f"PASS → decoder={decoder_id} tag={tag_id} t={t_secs:.3f}s")

                    cur.execute(
                        "INSERT INTO passes (host_ts_utc, port, decoder_id, tag_id, decoder_secs, raw_line) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (iso_utc_now(), port, decoder_id, tag_id, t_secs, txt)
                    )
                    pending += 1
                    if pending >= 20:
                        conn.commit()
                        pending = 0
    except KeyboardInterrupt:
        print("\nStopping (flushing DB)…")
    finally:
        try:
            conn.commit()
        finally:
            conn.close()
        ser.close()
        print("Closed serial & DB.")

if __name__ == "__main__":
    main()
