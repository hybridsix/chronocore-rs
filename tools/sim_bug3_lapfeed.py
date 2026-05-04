"""
sim_bug3_lapfeed.py - Simulation for Bug #3: race_control.js lap feed drops laps

Demonstrates that updateLapFeed() calls appendLapFeedItem() exactly once even
when standings show a car completed multiple laps between two polls.  This
happens whenever the poll latency is high enough that more than one lap occurred
during the gap (network hiccup, server busy, burst-poll collisions, etc.).

Run this before and after applying the fix to ui/js/race_control.js.
"""

# ---------------------------------------------------------------------------
# Python model of the JS updateLapFeed logic
# ---------------------------------------------------------------------------

def append_lap_feed_item(row, feed_log):
    """Simulates appendLapFeedItem() - records a feed entry."""
    feed_log.append({
        "entrant": row["name"],
        "lap":     row["laps"],
        "last_s":  row.get("last"),
    })


def update_lap_feed_buggy(standings, last_lap_counts, feed_log):
    """
    Replicates the CURRENT (buggy) updateLapFeed() from race_control.js.

    Bug: only one feed item is emitted even when cur > prev by more than 1.
    """
    for row in standings:
        eid = row.get("entrant_id")
        if eid is None:
            continue
        prev = last_lap_counts.get(eid, 0)
        cur  = int(row.get("laps") or 0)

        if cur > prev:
            append_lap_feed_item(row, feed_log)      # BUG: called once regardless of delta
            last_lap_counts[eid] = cur
        elif eid not in last_lap_counts:
            last_lap_counts[eid] = cur


def update_lap_feed_fixed(standings, last_lap_counts, feed_log):
    """
    FIXED updateLapFeed(): emits one feed item per missed lap.

    For each missed lap number we pass a synthesised row with the correct
    lap counter.  The last entry (cur) carries accurate timing data; earlier
    entries in the burst reuse the same last/best times because the snapshot
    only contains the most recent values.
    """
    for row in standings:
        eid = row.get("entrant_id")
        if eid is None:
            continue
        prev = last_lap_counts.get(eid, 0)
        cur  = int(row.get("laps") or 0)

        if cur > prev:
            for lap_n in range(prev + 1, cur + 1):
                # Synthesise a row with the correct lap number for the feed entry
                entry = dict(row)
                entry["laps"] = lap_n
                append_lap_feed_item(entry, feed_log)
            last_lap_counts[eid] = cur
        elif eid not in last_lap_counts:
            last_lap_counts[eid] = cur


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

def run_scenario(label, update_fn):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    last_lap_counts = {}
    feed_log = []

    # Poll 1: cars visible for the first time, no laps yet
    poll1 = [
        {"entrant_id": 1, "name": "Car A", "laps": 0, "last": None, "best": None},
        {"entrant_id": 2, "name": "Car B", "laps": 0, "last": None, "best": None},
    ]
    update_fn(poll1, last_lap_counts, feed_log)
    print(f"  Poll 1 (all at 0 laps): feed entries added = {len(feed_log)}")

    # Poll 2: normal case - Car A completed exactly 1 lap
    poll2 = [
        {"entrant_id": 1, "name": "Car A", "laps": 1, "last": 32.5, "best": 32.5},
        {"entrant_id": 2, "name": "Car B", "laps": 0, "last": None, "best": None},
    ]
    before = len(feed_log)
    update_fn(poll2, last_lap_counts, feed_log)
    added = len(feed_log) - before
    print(f"  Poll 2 (Car A: 0->1 lap): feed entries added = {added} (expected 1)  {'OK' if added == 1 else 'BUG'}")

    # Poll 3: Car A jumped from 1 to 3 laps (2 laps occurred between polls)
    # Car B also completes 1 lap
    poll3 = [
        {"entrant_id": 1, "name": "Car A", "laps": 3, "last": 31.8, "best": 31.8},
        {"entrant_id": 2, "name": "Car B", "laps": 1, "last": 33.0, "best": 33.0},
    ]
    before = len(feed_log)
    update_fn(poll3, last_lap_counts, feed_log)
    added = len(feed_log) - before
    print(f"  Poll 3 (Car A: 1->3 laps, Car B: 0->1): feed entries added = {added} (expected 3)  {'OK' if added == 3 else 'BUG - ' + str(added) + ' != 3'}")

    # Show full feed log
    print(f"\n  Feed log ({len(feed_log)} total entries):")
    for entry in feed_log:
        t = f"{entry['last_s']:.3f}s" if entry['last_s'] is not None else "    --   "
        print(f"    Lap {entry['lap']:3d}  {entry['entrant']:<10}  last={t}")

    return len(feed_log)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "="*60)
    print("  BUG #3 SIMULATION: race_control.js lap feed drops intermediate laps")
    print("="*60)

    total_buggy = run_scenario("BUGGY  (current race_control.js updateLapFeed)", update_lap_feed_buggy)
    total_fixed = run_scenario("FIXED  (loop over missed laps)", update_lap_feed_fixed)

    print("\n" + "="*60)
    print("  SUMMARY")
    print("="*60)
    print(f"  BUGGY : {total_buggy} total feed entries (expected 4)  <- BUG: Car A Lap 2 silently dropped")
    print(f"  FIXED : {total_fixed} total feed entries (expected 4)  <- {'CORRECT' if total_fixed == 4 else 'unexpected'}")
