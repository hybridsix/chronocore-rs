import sqlite3

con = sqlite3.connect('backend/db/laps.sqlite')

rows = con.execute("""
    SELECT entrant_id, name, number, tag, enabled 
    FROM entrants 
    ORDER BY entrant_id
""").fetchall()

print("All entrants:")
for r in rows:
    print(f"  ID {r[0]}: {r[1]} (#{r[2]}) - Tag: {r[3]} - Enabled: {r[4]}")

con.close()
