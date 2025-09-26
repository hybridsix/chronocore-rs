# backend/tools/migrate_add_car_num.py
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB   = ROOT.parent / "laps.sqlite"

def main():
    print(f"DB: {DB}")
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # Ensure table exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transponders (
            tag_id INTEGER PRIMARY KEY,
            team   TEXT NOT NULL
        )
    """)
    conn.commit()

    # Check for car_num
    cur.execute("PRAGMA table_info(transponders)")
    cols = {row[1] for row in cur.fetchall()}
    if "car_num" not in cols:
        print("Adding column transponders.car_num ...")
        cur.execute("ALTER TABLE transponders ADD COLUMN car_num INTEGER")
        conn.commit()
    else:
        print("Column car_num already present.")

    print("Done.")
    conn.close()

if __name__ == "__main__":
    main()
