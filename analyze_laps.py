import sqlite3

con = sqlite3.connect('backend/db/laps.sqlite')
con.row_factory = sqlite3.Row

print("=" * 100)
print("RESULT_LAPS - Checking for short laps")
print("=" * 100)

# Check schema first
print("\nResult_laps schema:")
cols = con.execute("PRAGMA table_info(result_laps)").fetchall()
for c in cols:
    print(f"  {c[1]} ({c[2]})")

print("\n" + "=" * 100)
print("ALL LAPS FROM LATEST RACE")
print("=" * 100)

# Get the latest race_id
latest_race = con.execute("SELECT MAX(race_id) as race_id FROM result_laps").fetchone()
if latest_race and latest_race['race_id']:
    race_id = latest_race['race_id']
    print(f"Race ID: {race_id}\n")
    
    rows = con.execute("""
        SELECT race_id, entrant_id, lap_no, lap_ms
        FROM result_laps 
        WHERE race_id = ?
        ORDER BY entrant_id, lap_no
    """, (race_id,)).fetchall()
    
    if rows:
        print(f"{'Entrant':<10} {'Lap#':<6} {'Lap_ms':<10} {'Lap_s':<10}")
        print("-" * 100)
        for r in rows:
            lap_s = round(r['lap_ms'] / 1000.0, 3) if r['lap_ms'] else 0
            print(f"{r['entrant_id']:<10} {r['lap_no']:<6} {r['lap_ms']:<10} {lap_s:<10.3f}")
        
        print("\n" + "=" * 100)
        print("BEST LAPS BY ENTRANT")
        print("=" * 100)
        best = con.execute("""
            SELECT entrant_id, MIN(lap_ms) as best_ms, COUNT(*) as total_laps
            FROM result_laps 
            WHERE race_id = ?
            GROUP BY entrant_id
            ORDER BY best_ms
        """, (race_id,)).fetchall()
        
        print(f"{'Entrant':<10} {'Best_ms':<10} {'Best_s':<10} {'Total Laps'}")
        print("-" * 100)
        for r in best:
            best_s = round(r['best_ms'] / 1000.0, 3) if r['best_ms'] else 0
            marker = " ⚠️ TOO SHORT" if best_s < 15 else ""
            print(f"{r['entrant_id']:<10} {r['best_ms']:<10} {best_s:<10.3f} {r['total_laps']}{marker}")
        
        print("\n" + "=" * 100)
        print("SUSPICIOUSLY SHORT LAPS (< 15s)")
        print("=" * 100)
        short = con.execute("""
            SELECT entrant_id, lap_no, lap_ms
            FROM result_laps 
            WHERE race_id = ? AND lap_ms < 15000
            ORDER BY lap_ms
        """, (race_id,)).fetchall()
        
        if short:
            print(f"{'Entrant':<10} {'Lap#':<6} {'Lap_ms':<10} {'Lap_s':<10}")
            print("-" * 100)
            for r in short:
                lap_s = round(r['lap_ms'] / 1000.0, 3)
                print(f"{r['entrant_id']:<10} {r['lap_no']:<6} {r['lap_ms']:<10} {lap_s:<10.3f}")
        else:
            print("✓ No short laps found (all laps >= 15s)")
    else:
        print("No laps found for this race")
else:
    print("No races found in database")

con.close()
