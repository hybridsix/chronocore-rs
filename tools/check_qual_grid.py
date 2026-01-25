import sqlite3
import json

con = sqlite3.connect('backend/db/laps.sqlite')
con.row_factory = sqlite3.Row

# Get the latest event config
row = con.execute("SELECT event_id, config_json FROM events ORDER BY event_id DESC LIMIT 1").fetchone()

if row:
    event_id = row['event_id']
    config = json.loads(row['config_json']) if row['config_json'] else {}
    
    qual = config.get('qualifying')
    
    if qual:
        print("=" * 100)
        print(f"QUALIFYING GRID - Event {event_id}")
        print("=" * 100)
        print(f"Source Heat: {qual.get('source_heat_id')}")
        print(f"Policy: {qual.get('policy')}")
        print()
        
        grid = qual.get('grid', [])
        print(f"{'Order':<8} {'Entrant':<10} {'Best_ms':<12} {'Best_s':<10} {'Brake OK'}")
        print("-" * 100)
        
        for entry in grid:
            order = entry.get('order')
            ent_id = entry.get('entrant_id')
            best_ms = entry.get('best_ms')
            best_s = round(best_ms / 1000.0, 3) if best_ms else None
            brake_ok = entry.get('brake_ok')
            
            marker = " ⚠️ TOO SHORT" if best_ms and best_ms < 15000 else ""
            print(f"{order:<8} {ent_id:<10} {best_ms:<12} {best_s:<10} {brake_ok}{marker}")
    else:
        print("No qualifying grid found in event config")
else:
    print("No events found in database")

con.close()
