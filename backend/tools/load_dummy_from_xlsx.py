# backend/tools/load_dummy_from_xlsx.py
"""
Load dummy passes from an XLSX Sprint sheet into laps.sqlite.

- Reads: Sprint 1 sheet (Lap + one column per team with lap times in seconds)
- Assigns each Team a fake 7-digit transponder UID (e.g., 3000001+)
- Inserts one row per (team, lap) into passes:
    host_ts_utc  -> now + small offset per inserted row
    port         -> "DUMMY"
    decoder_id   -> 210   (typical I-Lap firmware ID range we saw)
    tag_id       -> fake 7-digit UID
    decoder_secs -> cumulative time per team since race start (sum of that team’s lap times)
    raw_line     -> synthetic ASCII line similar to decoder output
- Safe to re-run after clearing (see --clear)
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]   # .../backend
DB   = ROOT.parent / "laps.sqlite"           # project root/laps.sqlite (same DB the app uses)

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_ts_utc   TEXT    NOT NULL,
    port          TEXT    NOT NULL,
    decoder_id    INTEGER NOT NULL,
    tag_id        INTEGER NOT NULL,
    decoder_secs  REAL    NOT NULL,
    raw_line      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passes_tag_time ON passes(tag_id, decoder_secs);
CREATE INDEX IF NOT EXISTS idx_passes_time ON passes(decoder_secs);
"""

def ensure_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn

def load_sprint_sheet(xlsx_path: Path, sheet_name: str = "Sprint 1") -> pd.DataFrame:
    df = pd.read_excel(xlsx_path, sheet_name=sheet_name)
    # Normalize headers and drop rows with no Lap number
    df.columns = [str(c).strip() for c in df.columns]
    df = df[df["Lap"].notna()].copy()
    # Force Lap to int where possible
    try:
        df["Lap"] = df["Lap"].astype(int)
    except Exception:
        pass
    return df

def assign_fake_uids(df: pd.DataFrame, base_uid: int = 3_000_000) -> Dict[str, int]:
    teams = [c for c in df.columns if c != "Lap"]
    return {team: base_uid + i + 1 for i, team in enumerate(teams)}

def main():
    ap = argparse.ArgumentParser(description="Load dummy passes from Sprint sheet into laps.sqlite")
    ap.add_argument("xlsx", type=Path, help="Path to the scoresheet XLSX")
    ap.add_argument("--sheet", default="Sprint 1", help="Sprint sheet name (default: 'Sprint 1')")
    ap.add_argument("--clear", action="store_true", help="Clear existing passes before inserting")
    ap.add_argument("--decoder-id", type=int, default=210, help="Decoder ID to write (default: 210)")
    args = ap.parse_args()

    print(f"DB:   {DB}")
    print(f"XLSX: {args.xlsx} (sheet='{args.sheet}')")

    df = load_sprint_sheet(args.xlsx, args.sheet)
    uids = assign_fake_uids(df)

    print("Teams found:")
    for t, uid in uids.items():
        print(f"  - {t} → {uid}")

    conn = ensure_db()
    cur = conn.cursor()

    if args.clear:
        cur.execute("DELETE FROM passes")
        conn.commit()
        print("Cleared existing passes.")

    # We will insert one pass per (team, lap) with decoder_secs = cumulative time per team.
    # This mimics the real decoder clock logic for each team’s lap sequence.
    now = datetime.now(timezone.utc)
    inserted = 0

    # Build cumulative time per team
    # For each team/column (except 'Lap'), cumulative sum across laps
    teams = [c for c in df.columns if c != "Lap"]
    cumulative: Dict[str, float] = {t: 0.0 for t in teams}

    # Insert rows in lap order (Lap ascending).
    for _, row in df.sort_values("Lap").iterrows():
        lap_no = int(row["Lap"])
        for team in teams:
            lap_time = row.get(team)
            if pd.isna(lap_time):
                # Missing data for that lap/team → skip
                continue

            # Update cumulative time for this team
            cumulative[team] += float(lap_time)
            t_secs = cumulative[team]

            tag_id = uids[team]
            host_ts = now + timedelta(milliseconds=inserted * 20)  # small stagger for deterministic ordering

            # raw_line shaped like decoder output (ASCII, tabs)
            # NOTE: leading \x01 (SOH) is not included here since we’re storing TXT only for readability.
            raw_txt = f"@\t{args.decoder_id}\t{tag_id}\t{t_secs:.3f}"

            cur.execute(
                "INSERT INTO passes (host_ts_utc, port, decoder_id, tag_id, decoder_secs, raw_line) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (host_ts.isoformat(), "DUMMY", args.decoder_id, tag_id, t_secs, raw_txt),
            )
            inserted += 1

    conn.commit()
    conn.close()
    print(f"Inserted {inserted} rows into passes.")
    print("Done.")

if __name__ == "__main__":
    main()
