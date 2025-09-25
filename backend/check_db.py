import sqlite3, pathlib

db = pathlib.Path(__file__).resolve().parent.parent / "laps.sqlite"
print("db:", db)

conn = sqlite3.connect(db)
cur = conn.cursor()

cur.execute("select count(*) from passes")
print("rows:", cur.fetchone()[0])

for r in cur.execute("select id, host_ts_utc, decoder_id, tag_id, decoder_secs from passes order by id desc limit 5"):
    print(r)

conn.close()
