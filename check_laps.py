import sqlite3
from pathlib import Path

db_path = Path("backend/db/laps.sqlite")
con = sqlite3.connect(db_path)
con.row_factory = sqlite3.Row

print("=" * 100)
print("PASSES TABLE - First 50 laps")
print("=" * 100)
rows = con.execute("""
    SELECT entrant_id, lap_num, lap_ms, is_valid, created_at 
    FROM passes 
    ORDER BY created_at 
    LIMIT 50
""").fetchall()

print(f"{'Entrant':<10} {'Lap#':<6} {'Lap_ms':<10} {'Lap_s':<10} {'Valid':<6} {'Created_at'}")
print("-" * 100)
for r in rows:
    lap_s = round(r['lap_ms'] / 1000.0, 3) if r['lap_ms'] else 0
    print(f"{r['entrant_id']:<10} {r['lap_num']:<6} {r['lap_ms']:<10} {lap_s:<10.3f} {r['is_valid']:<6} {r['created_at']}")

print("\n" + "=" * 100)
print("BEST LAPS BY ENTRANT")
print("=" * 100)
best = con.execute("""
    SELECT entrant_id, MIN(lap_ms) as best_ms, COUNT(*) as total_laps
    FROM passes 
    WHERE is_valid = 1
    GROUP BY entrant_id
    ORDER BY entrant_id
""").fetchall()

print(f"{'Entrant':<10} {'Best_ms':<10} {'Best_s':<10} {'Total Laps'}")
print("-" * 100)
for r in best:
    best_s = round(r['best_ms'] / 1000.0, 3) if r['best_ms'] else 0
    print(f"{r['entrant_id']:<10} {r['best_ms']:<10} {best_s:<10.3f} {r['total_laps']}")

print("\n" + "=" * 100)
print("SUSPICIOUSLY SHORT LAPS (< 15s)")
print("=" * 100)
short = con.execute("""
    SELECT entrant_id, lap_num, lap_ms, is_valid, created_at
    FROM passes 
    WHERE lap_ms < 15000 AND is_valid = 1
    ORDER BY lap_ms
""").fetchall()

if short:
    print(f"{'Entrant':<10} {'Lap#':<6} {'Lap_ms':<10} {'Lap_s':<10} {'Valid':<6} {'Created_at'}")
    print("-" * 100)
    for r in short:
        lap_s = round(r['lap_ms'] / 1000.0, 3)
        print(f"{r['entrant_id']:<10} {r['lap_num']:<6} {r['lap_ms']:<10} {lap_s:<10.3f} {r['is_valid']:<6} {r['created_at']}")
else:
    print("No short laps found (all laps >= 15s)")

con.close()
