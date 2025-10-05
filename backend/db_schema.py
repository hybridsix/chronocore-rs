from __future__ import annotations

"""
backend/db_schema.py (drop-in)
------------------------------
Centralized SQLite schema management for CCRS.

Key changes in this revision:
- Enforce "unique tag among ENABLED entrants" **at the database level** via a UNIQUE partial index:
    CREATE UNIQUE INDEX idx_entrants_tag_enabled_unique ON entrants(tag)
    WHERE enabled=1 AND tag IS NOT NULL;
- ensure_schema() is idempotent and *repairs* older DBs that might have had a non-unique index.
- tag_conflicts() checks only enabled entrants and excludes the incumbent id when provided.
"""

from pathlib import Path
import sqlite3
from typing import Optional

LOCKED_USER_VERSION = 2  # bump if table columns change

# ------------------------
# DDL
# ------------------------
ENTRANTS_DDL = """
CREATE TABLE IF NOT EXISTS entrants (
    entrant_id   INTEGER PRIMARY KEY,
    number       TEXT,              -- race number (string allows '004', 'A12')
    name         TEXT NOT NULL,
    tag          TEXT,              -- transponder ID, nullable when unassigned
    enabled      INTEGER NOT NULL DEFAULT 1,   -- 1 = active in roster, 0 = disabled
    status       TEXT NOT NULL DEFAULT 'ACTIVE',
    organization TEXT,
    spoken_name  TEXT,
    color        TEXT,
    logo         TEXT,
    updated_at   INTEGER            -- epoch seconds
);
"""

PASSES_DDL = """
CREATE TABLE IF NOT EXISTS passes (
    pass_id     INTEGER PRIMARY KEY,
    ts_ms       INTEGER NOT NULL,
    tag         TEXT NOT NULL,
    device_id   TEXT,
    source      TEXT DEFAULT 'track',
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_passes_ts ON passes(ts_ms);
"""

PARTIAL_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_entrants_tag_enabled_unique
ON entrants(tag)
WHERE enabled = 1 AND tag IS NOT NULL;
"""

# ------------------------
# Helpers
# ------------------------
def _exec_script(conn: sqlite3.Connection, script: str) -> None:
    cur = conn.cursor()
    cur.executescript(script)
    conn.commit()

def _drop_everything(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS passes")
    cur.execute("DROP TABLE IF EXISTS entrants")
    cur.execute("DROP INDEX IF EXISTS idx_passes_ts")
    cur.execute("DROP INDEX IF EXISTS idx_entrants_tag_enabled_unique")
    conn.commit()

def ensure_schema(db_path: str | Path, recreate: bool = False, include_passes: bool = True) -> None:
    """
    Create the database (and parent folder) if needed, and enforce our schema.
    This function is idempotent and safe to call at every boot.
    - If 'recreate' is True, this will drop and rebuild the schema (destructive).
    - Always (re)creates the UNIQUE partial index for enabled tag uniqueness.
    """
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        if recreate:
            _drop_everything(conn)

        _exec_script(conn, ENTRANTS_DDL)
        if include_passes:
            _exec_script(conn, PASSES_DDL)

        # Backfill/repair the partial UNIQUE index (older builds might have had a non-unique).
        cur = conn.cursor()
        cur.execute("DROP INDEX IF EXISTS idx_entrants_tag_enabled_unique")
        conn.commit()
        _exec_script(conn, PARTIAL_UNIQUE_INDEX)

        # Optional: record user_version for future lightweight migrations.
        cur.execute(f"PRAGMA user_version = {LOCKED_USER_VERSION}")
        conn.commit()
    finally:
        conn.close()

def tag_conflicts(conn: sqlite3.Connection, tag: str, incumbent_entrant_id: Optional[int] = None) -> bool:
    """
    Return True if 'tag' already belongs to a *different* ENABLED entrant.
    - 'incumbent_entrant_id': the row being edited; exclude it from the check.
    - 'tag' should already be normalized (whitespace trimmed).
    """
    cur = conn.cursor()
    if incumbent_entrant_id is None:
        cur.execute("SELECT entrant_id FROM entrants WHERE enabled=1 AND tag=? LIMIT 1", (tag,))
    else:
        cur.execute(
            "SELECT entrant_id FROM entrants WHERE enabled=1 AND tag=? AND entrant_id != ? LIMIT 1",
            (tag, incumbent_entrant_id),
        )
    return cur.fetchone() is not None
