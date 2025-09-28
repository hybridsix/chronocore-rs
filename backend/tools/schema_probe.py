import sqlite3
conn = sqlite3.connect(r"..\..\laps.sqlite")
cur = conn.cursor()
for t in ("passes","transponders","race_state"):
    try:
        cur.execute(f"PRAGMA table_info({t})")
        print(t, [c[1] for c in cur.fetchall()])
    except Exception as e:
        print(t, "ERR:", e)
