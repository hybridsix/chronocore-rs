from __future__ import annotations
import json
import sqlite3
from typing import Any, Dict


"""
backend/db_schema.py (ultimate)
-------------------------------
Centralized, idempotent SQLite schema management for CCRS.

Design goals
- Preserve existing behavior:
  * Entrants table shape (including TEXT car number).
  * "Unique tag among ENABLED entrants" guaranteed at the DB level via a partial UNIQUE index.
- Add timing-ready primitives:
  * locations: logical timing points (e.g., 'SF', 'PIT_IN') with human labels.
  * sources: concrete bindings (computer_id, decoder_id, port) -> location_id.
  * passes: durable raw detections (when persistence is ON) referencing sources.
  * events/heats/lap_events/flags: compact race model with room to grow.
- Provide helper views joining labels so UIs can query without manual JOINs.
- Keep schema creation safe to call at every boot (idempotent).
- Allow destructive rebuilds (recreate=True) when starting fresh.

IMPORTANT:
SQLite only enforces FOREIGN KEY constraints when 'PRAGMA foreign_keys=ON' is set
on the connection performing writes. Ensure your runtime DB connections do that.
"""

from pathlib import Path
import sqlite3
from typing import Optional

# Bump when DDL changes in a way worth tracking (for future migrations).
LOCKED_USER_VERSION = 6

# ------------------------
# DDL: Core roster
# ------------------------
ENTRANTS_DDL = """
CREATE TABLE IF NOT EXISTS entrants (
    entrant_id   INTEGER PRIMARY KEY,
    number       TEXT,                      -- car number as TEXT ('004', 'A12', etc.)
    name         TEXT NOT NULL,
    tag          TEXT,                      -- transponder ID, nullable when unassigned
    enabled      INTEGER NOT NULL DEFAULT 1, -- 1 = active in roster, 0 = disabled
    status       TEXT NOT NULL DEFAULT 'ACTIVE',
    organization TEXT,
    spoken_name  TEXT,
    color        TEXT,
    logo         TEXT,
    updated_at   INTEGER                    -- epoch seconds (updated by app)
);
-- Partial UNIQUE: tag must be unique among *enabled* entrants, tags may repeat across disabled rows or be NULL.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entrants_tag_enabled_unique
ON entrants(tag)
WHERE enabled = 1 AND tag IS NOT NULL;
"""

# ------------------------
# DDL: Track topology & bindings
# ------------------------
LOCATIONS_DDL = """
-- Logical timing points on the course. Stable IDs are short, human-labels can change freely.
CREATE TABLE IF NOT EXISTS locations (
    location_id TEXT PRIMARY KEY,           -- e.g., 'SF', 'PIT_IN', 'PIT_OUT', 'X1'
    label       TEXT NOT NULL               -- e.g., 'Start/Finish', 'Pit In'
);
"""

SOURCES_DDL = """
-- Physical input bindings that produce detections; multiple sources may map to the same location.
CREATE TABLE IF NOT EXISTS sources (
    source_id   INTEGER PRIMARY KEY,
    computer_id TEXT NOT NULL,              -- host alias, e.g., 't640-gate-a'
    decoder_id  TEXT NOT NULL,              -- reader identity, e.g., 'ilap-210'
    port        TEXT NOT NULL,              -- 'COM7', '/dev/ttyUSB0', 'udp://0.0.0.0:5001'
    location_id TEXT NOT NULL,              -- FK to locations(location_id)
    FOREIGN KEY (location_id) REFERENCES locations(location_id) ON UPDATE CASCADE ON DELETE RESTRICT,
    UNIQUE(computer_id, decoder_id, port)   -- exact physical tuple is unique
);
CREATE INDEX IF NOT EXISTS idx_sources_location ON sources(location_id);
"""

# ------------------------
# DDL: Raw detections (journaling)
# ------------------------
PASSES_DDL = """
-- Durable detection log; only populated when persistence/journaling is enabled during events.
CREATE TABLE IF NOT EXISTS passes (
    pass_id     INTEGER PRIMARY KEY,
    ts_ms       INTEGER NOT NULL,           -- host epoch ms when received
    tag         TEXT NOT NULL,              -- 7-digit (ILap) etc.
    t_secs      REAL,                       -- optional device seconds if provided by decoder
    source_id   INTEGER,                    -- FK to sources(source_id); may be NULL if unknown
    raw         TEXT,                       -- raw packet/line for forensics
    meta_json   TEXT,                       -- optional JSON: channel/antenna/direction/etc.
    FOREIGN KEY (source_id) REFERENCES sources(source_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_passes_ts ON passes(ts_ms);
CREATE INDEX IF NOT EXISTS idx_passes_tag ON passes(tag);
CREATE INDEX IF NOT EXISTS idx_passes_source_ts ON passes(source_id, ts_ms);
"""

# ------------------------
# DDL: Race model (events, heats, laps, flags)
# ------------------------
EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    date_utc    TEXT,                       -- ISO8601 (YYYY-MM-DD or full timestamp)
    config_json TEXT                        -- optional per-event rules/settings
);
"""

HEATS_DDL = """
CREATE TABLE IF NOT EXISTS heats (
    heat_id     INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    order_index INTEGER,                    -- optional display ordering
    config_json TEXT,                       -- per-heat rules (duration, min_lap_ms, etc.)
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_heats_event ON heats(event_id);
"""

LAP_EVENTS_DDL = """
-- Authoritative laps attributed by the RaceEngine.
CREATE TABLE IF NOT EXISTS lap_events (
    lap_id      INTEGER PRIMARY KEY,
    heat_id     INTEGER NOT NULL,
    entrant_id  INTEGER NOT NULL,
    lap_num     INTEGER NOT NULL,           -- 1-based within the heat
    ts_ms       INTEGER NOT NULL,           -- host epoch ms when lap was credited
    source_id   INTEGER,                    -- where this lap was triggered (usually SF)
    inferred    INTEGER NOT NULL DEFAULT 0, -- 0 = real detection, 1 = inferred/predicted
    meta_json   TEXT,                       -- any extras (why inferred, sigma_k, etc.)
    FOREIGN KEY (heat_id)    REFERENCES heats(heat_id)       ON DELETE CASCADE,
    FOREIGN KEY (entrant_id) REFERENCES entrants(entrant_id) ON DELETE CASCADE,
    FOREIGN KEY (source_id)  REFERENCES sources(source_id)   ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_laps_heat_entrant_time ON lap_events(heat_id, entrant_id, ts_ms);
CREATE INDEX IF NOT EXISTS idx_laps_heat_lapnum ON lap_events(heat_id, lap_num);
"""

FLAGS_DDL = """
-- Race control state changes (GREEN/YELLOW/RED/CHECKERED, etc.)
CREATE TABLE IF NOT EXISTS flags (
    flag_id     INTEGER PRIMARY KEY,
    heat_id     INTEGER NOT NULL,
    state       TEXT NOT NULL,              -- 'GREEN' | 'YELLOW' | 'RED' | 'CHECKERED' | ...
    ts_ms       INTEGER NOT NULL,
    actor       TEXT,                       -- who/what set the flag (operator, API, etc.)
    note        TEXT,
    FOREIGN KEY (heat_id) REFERENCES heats(heat_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_flags_heat_time ON flags(heat_id, ts_ms);
"""

# ------------------------
# DDL: Frozen results snapshots
# ------------------------
RESULTS_DDL = """
-- Frozen standings by race, position-sorted for operator exports.
CREATE TABLE IF NOT EXISTS result_standings (
    race_id        INTEGER NOT NULL,
    position       INTEGER NOT NULL,
    entrant_id     INTEGER NOT NULL,
    number         TEXT,
    name           TEXT,
    laps           INTEGER NOT NULL,
    last_ms        INTEGER,
    best_ms        INTEGER,
    gap_ms         INTEGER,
    lap_deficit    INTEGER,
    pit_count      INTEGER DEFAULT 0,
    status         TEXT DEFAULT 'ACTIVE',
    PRIMARY KEY (race_id, position)
);

-- Frozen lap-by-lap history for results exports.
CREATE TABLE IF NOT EXISTS result_laps (
    race_id        INTEGER NOT NULL,
    entrant_id     INTEGER NOT NULL,
    lap_no         INTEGER NOT NULL,     -- 1-based index
    lap_ms         INTEGER NOT NULL,
    pass_ts_ns     INTEGER,              -- optional original pass timestamp (nanoseconds)
    PRIMARY KEY (race_id, entrant_id, lap_no)
);

-- Metadata describing the frozen snapshot (type, duration, capture time).
CREATE TABLE IF NOT EXISTS result_meta (
    race_id        INTEGER PRIMARY KEY,
    race_type      TEXT,
    frozen_utc     TEXT NOT NULL,        -- ISO8601 timestamp when snapshot was frozen
    duration_ms    INTEGER NOT NULL      -- total race duration in milliseconds
);
"""

# ------------------------
# DDL: Convenience views (for UI-friendly reads)
# ------------------------
VIEWS_DDL = """
-- Passes joined to human-friendly labels.
CREATE VIEW IF NOT EXISTS v_passes_enriched AS
SELECT
  p.pass_id,
  p.ts_ms,
  p.tag,
  p.t_secs,
  p.raw,
  p.meta_json,
  s.source_id,
  s.computer_id,
  s.decoder_id,
  s.port,
  s.location_id,
  l.label AS location_label
FROM passes p
LEFT JOIN sources s ON s.source_id = p.source_id
LEFT JOIN locations l ON l.location_id = s.location_id;

-- Lap events joined to labels and roster.
CREATE VIEW IF NOT EXISTS v_lap_events_enriched AS
SELECT
  le.lap_id,
  le.heat_id,
  h.name      AS heat_name,
    le.entrant_id,
    e.number    AS number,
  e.name      AS team_name,
  le.lap_num,
  le.ts_ms,
  le.inferred,
  le.meta_json,
  le.source_id,
  s.computer_id,
  s.decoder_id,
  s.port,
  s.location_id,
  l.label     AS location_label
FROM lap_events le
JOIN heats     h ON h.heat_id = le.heat_id
JOIN entrants  e ON e.entrant_id = le.entrant_id
LEFT JOIN sources   s ON s.source_id = le.source_id
LEFT JOIN locations l ON l.location_id = s.location_id;

-- Heats summary with derived timing/status aggregates.
CREATE VIEW IF NOT EXISTS v_heats_summary AS
SELECT
    h.heat_id,
    h.event_id,
    h.name,
    -- first GREEN/WHITE is a practical "start" (works for qualifying too)
    (SELECT MIN(f.ts_ms)
         FROM flags f
         WHERE f.heat_id = h.heat_id
             AND f.state IN ('GREEN','WHITE')) AS started_ms,
    -- last CHECKERED is the "finish" (NULL if never thrown)
    (SELECT MAX(f.ts_ms)
         FROM flags f
         WHERE f.heat_id = h.heat_id
             AND f.state = 'CHECKERED') AS finished_ms,
    -- the latest flag is the current status (NULL if no flags yet)
    (SELECT f2.state
         FROM flags f2
         WHERE f2.heat_id = h.heat_id
         ORDER BY f2.ts_ms DESC
         LIMIT 1) AS status,
    -- aggregates
    (SELECT COUNT(*)
         FROM lap_events le
         WHERE le.heat_id = h.heat_id) AS laps_count,
    (SELECT COUNT(DISTINCT le.entrant_id)
         FROM lap_events le
         WHERE le.heat_id = h.heat_id) AS entrant_count
FROM heats h;
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
    # Drop views first (they depend on tables)
    cur.execute("DROP VIEW IF EXISTS v_lap_events_enriched")
    cur.execute("DROP VIEW IF EXISTS v_passes_enriched")
    # Then tables (children before parents)
    cur.execute("DROP TABLE IF EXISTS result_meta")
    cur.execute("DROP TABLE IF EXISTS result_laps")
    cur.execute("DROP TABLE IF EXISTS result_standings")
    cur.execute("DROP TABLE IF EXISTS flags")
    cur.execute("DROP TABLE IF EXISTS lap_events")
    cur.execute("DROP TABLE IF EXISTS heats")
    cur.execute("DROP TABLE IF EXISTS events")
    cur.execute("DROP TABLE IF EXISTS passes")
    cur.execute("DROP TABLE IF EXISTS sources")
    cur.execute("DROP TABLE IF EXISTS locations")
    cur.execute("DROP TABLE IF EXISTS entrants")
    # Drop indexes explicitly if desired (mostly harmless if left; SQLite ignores on table drop)
    cur.execute("DROP INDEX IF EXISTS idx_laps_heat_entrant_time")
    cur.execute("DROP INDEX IF EXISTS idx_laps_heat_lapnum")
    cur.execute("DROP INDEX IF EXISTS idx_flags_heat_time")
    cur.execute("DROP INDEX IF EXISTS idx_passes_ts")
    cur.execute("DROP INDEX IF EXISTS idx_passes_tag")
    cur.execute("DROP INDEX IF EXISTS idx_passes_source_ts")
    cur.execute("DROP INDEX IF EXISTS idx_sources_location")
    cur.execute("DROP INDEX IF EXISTS idx_entrants_tag_enabled_unique")
    conn.commit()

def ensure_schema(
    db_path: str | Path,
    recreate: bool = False,
    include_passes: bool = True,
    include_race: bool = True,
) -> None:
    """
    Create the database (and parent folder) if needed, and enforce our schema.
    Safe to call at every boot.
      - recreate=True   : destructive drop & rebuild (fresh start).
      - include_passes  : create the 'passes' table & indexes (journaling).
      - include_race    : create events/heats/lap_events/flags (race timing model).

    NOTE: If your runtime uses FOREIGN KEYS, remember to set PRAGMA foreign_keys=ON
    on *those* connections. This function only defines FKs in DDL.
    """
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(p)
    try:
        if recreate:
            _drop_everything(conn)

        # Core roster and uniqueness rule.
        _exec_script(conn, ENTRANTS_DDL)

        # Track topology & physical bindings.
        _exec_script(conn, LOCATIONS_DDL)
        _exec_script(conn, SOURCES_DDL)

        # Journaling of raw detections (when enabled).
        if include_passes:
            _exec_script(conn, PASSES_DDL)

        # Race model.
        if include_race:
            _exec_script(conn, EVENTS_DDL)
            _exec_script(conn, HEATS_DDL)
            _exec_script(conn, LAP_EVENTS_DDL)
            _exec_script(conn, FLAGS_DDL)
            _exec_script(conn, RESULTS_DDL)

        # Convenience views for UI.
        _exec_script(conn, VIEWS_DDL)

        # Record user_version for lightweight migrations.
        cur = conn.cursor()
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

# --------------------------------------------------------------------
# JSON config helpers (heats/events) - persistence for tiny settings
# --------------------------------------------------------------------
def _get_json(conn: sqlite3.Connection, table: str, id_col: str, row_id: int) -> Dict[str, Any]:
    cur = conn.execute(f"SELECT config_json FROM {table} WHERE {id_col}=?", (row_id,))
    row = cur.fetchone()
    if not row or row[0] is None:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}

def _set_json(conn: sqlite3.Connection, table: str, id_col: str, row_id: int, payload: Dict[str, Any]) -> None:
    conn.execute(
        f"UPDATE {table} SET config_json=? WHERE {id_col}=?",
        (json.dumps(payload), row_id),
    )
    conn.commit()

# ---- Qualifying brake test verdicts (stored on the QUALIFYING heat) ----
def get_brake_flags(conn: sqlite3.Connection, heat_id: int) -> Dict[int, bool]:
    flags = _get_json(conn, "heats", "heat_id", heat_id).get("qual_brake_flags", {})
    return {int(k): bool(v) for k, v in flags.items()}

def set_brake_flag(conn: sqlite3.Connection, heat_id: int, entrant_id: int, brake_ok: bool) -> None:
    cfg = _get_json(conn, "heats", "heat_id", heat_id)
    flags = cfg.get("qual_brake_flags", {})
    flags[str(entrant_id)] = bool(brake_ok)
    cfg["qual_brake_flags"] = flags
    _set_json(conn, "heats", "heat_id", heat_id, cfg)

# ---- Frozen weekend qualifying grid (stored on the EVENT) ----
def get_event_config(conn: sqlite3.Connection, event_id: int) -> Dict[str, Any]:
    return _get_json(conn, "events", "event_id", event_id)

def set_event_config(conn: sqlite3.Connection, event_id: int, cfg: Dict[str, Any]) -> None:
    _set_json(conn, "events", "event_id", event_id, cfg)

