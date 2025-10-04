
# backend/db_schema.py
# Dedicated SQLite schema management for CCRS Race Timing Software.
#
# Public:
#   ensure_schema(db_path: str | Path, recreate: bool = False) -> None
#     - Creates the DB (and parent folder) if missing.
#     - Applies locked schema for entrants (+ optional passes).
#     - Sets PRAGMA user_version=2.
#     - If recreate=True, performs a destructive drop & re-create.
#
#   tag_conflicts(conn, tag: str, incumbent_entrant_id: int | None = None) -> bool
#     - Returns True if 'tag' is already associated with a *different* enabled entrant.
#
# Schema versioning:
#   We rely on SQLite PRAGMA user_version to track migrations. This module writes 2.
#
from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Optional

LOCKED_USER_VERSION = 2

# --- DDL (locked) ---------------------------------------------------------

ENTRANTS_DDL = """
CREATE TABLE IF NOT EXISTS entrants (
    entrant_id   INTEGER PRIMARY KEY,
    car_number   TEXT,
    name         TEXT NOT NULL,
    tag          TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'ACTIVE',
    organization TEXT,
    spoken_name  TEXT,
    color        TEXT,
    logo         TEXT,
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at   INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    CHECK (status IN ('ACTIVE','DISABLED','DNF','DQ')),
    CHECK (enabled IN (0,1))
);
-- Unique among enabled entrants only (partial index)
CREATE INDEX IF NOT EXISTS idx_entrants_tag_enabled_unique
  ON entrants(tag)
  WHERE enabled = 1 AND tag IS NOT NULL;

-- Helpful lookups
CREATE INDEX IF NOT EXISTS idx_entrants_car_number ON entrants(car_number);
CREATE INDEX IF NOT EXISTS idx_entrants_name       ON entrants(name);
"""

# Optional: 'passes' table (journal/audit). Safe to include even if another
# component also creates it (CREATE IF NOT EXISTS).
PASSES_DDL = """
CREATE TABLE IF NOT EXISTS passes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    host_ts_utc  TEXT    NOT NULL,
    port         TEXT    NOT NULL,
    decoder_id   INTEGER NOT NULL,
    tag          TEXT,
    device_id    TEXT,
    source       TEXT,
    entrant_id   INTEGER,  -- nullable; resolved at ingest if known
    decoder_secs REAL,
    raw_line     TEXT,
    FOREIGN KEY (entrant_id) REFERENCES entrants(entrant_id)
);
CREATE INDEX IF NOT EXISTS idx_passes_tag_time  ON passes(tag, decoder_secs);
CREATE INDEX IF NOT EXISTS idx_passes_time      ON passes(decoder_secs);
"""

# --------------------------------------------------------------------------

def _exec_script(conn: sqlite3.Connection, sql: str) -> None:
    conn.executescript(sql)
    conn.commit()

def _drop_everything(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    # Drop only what we own (safe idempotent)
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('entrants','passes')")
    for (name,) in cur.fetchall():
        cur.execute(f"DROP TABLE IF EXISTS {name}")
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_entrants_%' OR name LIKE 'idx_passes_%'")
    for (name,) in cur.fetchall():
        cur.execute(f"DROP INDEX IF EXISTS {name}")
    conn.commit()

def ensure_schema(db_path: str | Path, recreate: bool = False, include_passes: bool = True) -> None:
    """Ensure locked schema exists at db_path. Optionally force re-create."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        if recreate:
            _drop_everything(conn)

        _exec_script(conn, ENTRANTS_DDL)
        if include_passes:
            _exec_script(conn, PASSES_DDL)

        # Record schema version
        cur = conn.cursor()
        cur.execute("PRAGMA user_version")
        current = cur.fetchone()[0]
        if current != LOCKED_USER_VERSION:
            cur.execute(f"PRAGMA user_version = {LOCKED_USER_VERSION}")
            conn.commit()
    finally:
        conn.close()

# ---------------- Duplicate-tag guards ------------------------------------

def tag_conflicts(conn: sqlite3.Connection, tag: Optional[str], incumbent_entrant_id: Optional[int] = None) -> bool:
    """True if 'tag' is already bound to a *different* enabled entrant."""
    if not tag:
        return False
    cur = conn.cursor()
    if incumbent_entrant_id is None:
        cur.execute(
            "SELECT 1 FROM entrants WHERE enabled = 1 AND tag = ? LIMIT 1",
            (tag,),
        )
    else:
        cur.execute(
            "SELECT 1 FROM entrants WHERE enabled = 1 AND tag = ? AND entrant_id <> ? LIMIT 1",
            (tag, incumbent_entrant_id),
        )
    return cur.fetchone() is not None
