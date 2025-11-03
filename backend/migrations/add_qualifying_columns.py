"""
Add grid_index and brake_valid columns to result_standings table.

This migration adds support for persisting qualifying grid positions and brake test results.
"""
import sqlite3
import sys
from pathlib import Path

def migrate(db_path: str):
    """Add grid_index and brake_valid columns to result_standings."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    # Check if columns already exist
    cur.execute("PRAGMA table_info(result_standings)")
    columns = {row[1] for row in cur.fetchall()}
    
    if "grid_index" not in columns:
        print("Adding grid_index column to result_standings...")
        cur.execute("ALTER TABLE result_standings ADD COLUMN grid_index INTEGER")
        print("✓ grid_index column added")
    else:
        print("✓ grid_index column already exists")
    
    if "brake_valid" not in columns:
        print("Adding brake_valid column to result_standings...")
        cur.execute("ALTER TABLE result_standings ADD COLUMN brake_valid INTEGER")
        print("✓ brake_valid column added")
    else:
        print("✓ brake_valid column already exists")
    
    conn.commit()
    conn.close()
    print("\nMigration complete!")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_path = "backend/db/laps.sqlite"
    
    print(f"Migrating database: {db_path}")
    migrate(db_path)
