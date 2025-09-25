import csv, sqlite3, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent   # project root (…/prs_starter)
DB    = ROOT / "laps.sqlite"

# allow optional output path: python export_csv.py C:\path\to\file.csv
if len(sys.argv) > 1:
    OUT = pathlib.Path(sys.argv[1]).expanduser().resolve()
else:
    OUT = (pathlib.Path(__file__).resolve().parent / "laps_export.csv").resolve()

print("DB :", DB)
print("OUT:", OUT)

conn = sqlite3.connect(DB)
cur  = conn.cursor()
rows = cur.execute("""
  SELECT id, host_ts_utc, port, decoder_id, tag_id, decoder_secs, raw_line
  FROM passes ORDER BY id
""").fetchall()
conn.close()

# ensure parent dir exists
OUT.parent.mkdir(parents=True, exist_ok=True)

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["id","host_ts_utc","port","decoder_id","tag_id","decoder_secs","raw_line"])
    w.writerows(rows)

print(f"Wrote {len(rows)} rows to:", OUT)
