import sqlite3

con = sqlite3.connect('backend/db/laps.sqlite')

print("Tables in database:")
tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
for t in tables:
    print(f"  - {t[0]}")

print("\nPasses table schema:")
cols = con.execute("PRAGMA table_info(passes)").fetchall()
for c in cols:
    print(f"  {c[1]} ({c[2]})")

con.close()
