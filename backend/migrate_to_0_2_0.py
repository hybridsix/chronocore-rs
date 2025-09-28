#!/usr/bin/env python3
"""
SQLite migration to PRS schema v0.2.0 (tag-based, no bib; transponders has org/display_name)
- Makes a timestamped backup first
- Migrates laps: bib -> tag (if needed)
- Migrates transponders: team/bib -> org/display_name (if needed)
- Creates useful indexes
"""

import os, shutil, sqlite3, time, sys

DB_PATH = os.environ.get("PRS_DB", "laps.sqlite")

def table_exists(conn, name):
    r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None

def column_names(conn, table):
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.OperationalError:
        return []

def backup(db_path):
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = f"{db_path}.{ts}.bak"
    shutil.copy2(db_path, backup_path)
    print(f"[backup] {backup_path}")
    return backup_path

def migrate_laps(conn):
    cols = column_names(conn, "laps")
    if not cols:
        print("[laps] no table -> skipping")
        return
    if "tag" in cols and "bib" not in cols:
        print("[laps] already tag-based -> OK")
        return

    print("[laps] migrating bib -> tag")
    conn.execute("ALTER TABLE laps RENAME TO laps_old;")
    conn.execute("""
        CREATE TABLE laps (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          race_id INTEGER NOT NULL,
          tag TEXT NOT NULL,
          ts_utc INTEGER NOT NULL,
          source TEXT DEFAULT 'manual',
          device_id TEXT,
          meta_json TEXT,
          created_at_utc INTEGER NOT NULL,
          FOREIGN KEY(race_id) REFERENCES races(id) ON DELETE CASCADE
        );
    """)
    # If old table had bib, map it to tag. If it already had tag, keep tag.
    old_cols = column_names(conn, "laps_old")
    if "bib" in old_cols:
        conn.execute("""
            INSERT INTO laps (id,race_id,tag,ts_utc,source,device_id,meta_json,created_at_utc)
            SELECT id,race_id,bib,ts_utc,source,device_id,meta_json,created_at_utc FROM laps_old;
        """)
    else:
        conn.execute("""
            INSERT INTO laps (id,race_id,tag,ts_utc,source,device_id,meta_json,created_at_utc)
            SELECT id,race_id,tag,ts_utc,source,device_id,meta_json,created_at_utc FROM laps_old;
        """)
    conn.execute("DROP TABLE laps_old;")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_laps_race_tag_ts ON laps(race_id, tag, ts_utc);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_laps_race_ts ON laps(race_id, ts_utc);")
    print("[laps] done")

def migrate_transponders(conn):
    if not table_exists(conn, "transponders"):
        print("[transponders] no table -> creating new")
        conn.execute("""
            CREATE TABLE transponders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              race_id INTEGER NOT NULL,
              tag TEXT NOT NULL,
              org TEXT,
              display_name TEXT,
              created_at_utc INTEGER NOT NULL,
              UNIQUE(race_id, tag),
              FOREIGN KEY(race_id) REFERENCES races(id) ON DELETE CASCADE
            );
        """)
        return

    cols = column_names(conn, "transponders")
    want = {"race_id","tag","org","display_name","created_at_utc"}
    if want.issubset(set(cols)) and "team" not in cols and "bib" not in cols:
        print("[transponders] already in new shape -> OK")
        return

    print("[transponders] migrating to (org, display_name) model")
    conn.execute("ALTER TABLE transponders RENAME TO transponders_old;")
    conn.execute("""
        CREATE TABLE transponders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          race_id INTEGER NOT NULL,
          tag TEXT NOT NULL,
          org TEXT,
          display_name TEXT,
          created_at_utc INTEGER NOT NULL,
          UNIQUE(race_id, tag),
          FOREIGN KEY(race_id) REFERENCES races(id) ON DELETE CASCADE
        );
    """)

    old_cols = column_names(conn, "transponders_old")
    # Best-effort mapping
    if "team" in old_cols and "display_name" in old_cols:
        conn.execute("""
            INSERT INTO transponders (race_id, tag, org, display_name, created_at_utc)
            SELECT race_id, tag, team, display_name, COALESCE(created_at_utc, strftime('%s','now')*1000)
            FROM transponders_old;
        """)
    elif "team" in old_cols:
        conn.execute("""
            INSERT INTO transponders (race_id, tag, org, display_name, created_at_utc)
            SELECT race_id, tag, team, NULL, COALESCE(created_at_utc, strftime('%s','now')*1000)
            FROM transponders_old;
        """)
    else:
        # If there was a bib column, drop it; org/display_name unknown
        conn.execute("""
            INSERT INTO transponders (race_id, tag, org, display_name, created_at_utc)
            SELECT race_id, tag, NULL, NULL, COALESCE(created_at_utc, strftime('%s','now')*1000)
            FROM transponders_old;
        """)

    conn.execute("DROP TABLE transponders_old;")
    print("[transponders] done")

def ensure_passes_indexes(conn):
    if not table_exists(conn, "passes"):
        print("[passes] table not found -> skipping index creation")
        return
    conn.execute("CREATE INDEX IF NOT EXISTS idx_passes_race_tag_ts ON passes(race_id, tag, ts_utc);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_passes_race_ts ON passes(race_id, ts_utc);")

def main():
    if not os.path.exists(DB_PATH):
        print(f"[error] {DB_PATH} not found")
        sys.exit(1)

    backup(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys=OFF;")
        conn.execute("BEGIN;")
        migrate_laps(conn)
        migrate_transponders(conn)
        ensure_passes_indexes(conn)
        conn.execute("COMMIT;")
        print("[ok] migration complete")
    except Exception as e:
        conn.execute("ROLLBACK;")
        print("[rollback] migration failed:", e)
        print("Your backup is untouched. See the printed error, fix, and re-run.")
        sys.exit(2)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
