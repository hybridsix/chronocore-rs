#!/usr/bin/env python3
"""
Add race_type, sim, sim_label, source columns to race_state if they don't exist.
Safe to run multiple times; affects only the given SQLite DB.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "laps.sqlite"

NEW_COLS = [
    ("race_type", "TEXT", None),     # e.g. "sprint", "endurance"
    ("sim",       "INTEGER", 0),     # 0/1
    ("sim_label", "TEXT", None),     # e.g. "SIMULATOR ACTIVE"
    ("source",    "TEXT", None),     # e.g. "sim", "decoder"
]

def ensure_column(conn, table, name, coltype, default):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
        if default is not None:
            conn.execute(f"UPDATE {table} SET {name}=?", (default,))
        conn.commit()
        print(f"Added {table}.{name} ({coltype})")

def main():
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        # make sure race_state exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS race_state (
                race_id INTEGER PRIMARY KEY,
                started_at_utc TEXT,
                clock_ms INTEGER,
                flag TEXT,
                running INTEGER DEFAULT 0
            );""")
        conn.commit()

        for name, coltype, default in NEW_COLS:
            ensure_column(conn, "race_state", name, coltype, default)

        print("Migration complete.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
