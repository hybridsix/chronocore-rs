"""
Seed a couple of entrants into the CCRS SQLite DB.

Why this exists:
- /engine/entrant/assign_tag first verifies the entrant exists in the DB.
- Fresh installs can have an empty DB, so we seed known IDs for testing.
- This is safe to re-run: INSERT OR REPLACE keeps IDs stable.

Usage:
  (.venv) python tools/seed_entrants.py
"""
from __future__ import annotations
import sqlite3, os, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "db" / "laps.sqlite"
DB.parent.mkdir(parents=True, exist_ok=True)

now = int(time.time())
rows = [
    # entrant_id, number, name, tag, enabled, status, updated_at
    (12, "42", "Thunder Lizards", None, 0, "ACTIVE", now),
    (34, "7",  "Circuit Breakers", "1234567", 1, "ACTIVE", now),
]

with sqlite3.connect(DB) as conn:
    cur = conn.cursor()
    # Ensure minimal schema (table may already exist; IF NOT EXISTS is safe)
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS entrants (
        entrant_id   INTEGER PRIMARY KEY,
        number       TEXT,
        name         TEXT NOT NULL,
        tag          TEXT,
        enabled      INTEGER NOT NULL DEFAULT 1,
        status       TEXT NOT NULL DEFAULT 'ACTIVE',
        organization TEXT,
        spoken_name  TEXT,
        color        TEXT,
        logo         TEXT,
        updated_at   INTEGER
    );
    """)
    cur.executemany(
        """INSERT OR REPLACE INTO entrants
           (entrant_id, number, name, tag, enabled, status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
print(f"Seeded entrants into {DB}")
