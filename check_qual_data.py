import sqlite3

con = sqlite3.connect('backend/db/laps.sqlite')
con.row_factory = sqlite3.Row

# Check the qualifying heat (1762616239)
qual_heat_id = 1762616239

print("=" * 100)
print(f"QUALIFYING RACE DATA - Heat {qual_heat_id}")
print("=" * 100)

rows = con.execute("""
    SELECT entrant_id, lap_no, lap_ms
    FROM result_laps 
    WHERE race_id = ?
    ORDER BY entrant_id, lap_no
""", (qual_heat_id,)).fetchall()

if rows:
    # Group by entrant
    by_entrant = {}
    for r in rows:
        eid = r['entrant_id']
        if eid not in by_entrant:
            by_entrant[eid] = []
        by_entrant[eid].append(r['lap_ms'])
    
    print(f"\n{'Entrant':<10} {'All Laps (ms)':<50} {'Best':<10} {'Best_s'}")
    print("-" * 100)
    
    results = []
    for eid, laps in sorted(by_entrant.items()):
        best_ms = min(laps)
        best_s = round(best_ms / 1000.0, 3)
        laps_str = ', '.join([str(ms) for ms in laps[:10]])  # First 10 laps
        if len(laps) > 10:
            laps_str += f", ... ({len(laps)} total)"
        print(f"{eid:<10} {laps_str:<50} {best_ms:<10} {best_s}")
        results.append((eid, best_ms, best_s))
    
    print("\n" + "=" * 100)
    print("SORTED BY BEST LAP (how qualifying SHOULD be sorted)")
    print("=" * 100)
    print(f"{'Pos':<6} {'Entrant':<10} {'Best_ms':<10} {'Best_s'}")
    print("-" * 100)
    
    results.sort(key=lambda x: x[1])  # Sort by best_ms
    for i, (eid, best_ms, best_s) in enumerate(results, 1):
        print(f"{i:<6} {eid:<10} {best_ms:<10} {best_s}")
    
    print("\n" + "=" * 100)
    print("ACTUAL QUALIFYING GRID ORDER (from events config)")
    print("=" * 100)
    print("Order  Entrant    Best_ms      Best_s")
    print("-" * 100)
    print("1      4          20610        20.61")
    print("2      3          20619        20.619")
    print("3      15         20901        20.901")
    print("4      5          21345        21.345")
    print("5      6          21362        21.362")
    print("6      14         22070        22.07")
    print("7      7          22644        22.644")
    print("8      12         23168        23.168")
    print("9      9          23445        23.445")
    print("10     10         26223        26.223")
    print("11     8          27760        27.76")
    print("12     2          29063        29.063")
    print("13     11         22744        22.744  (brake fail - demoted)")
    
else:
    print("No laps found for this heat")

con.close()
