# ChronoCore Race Software - Changelog

All notable changes to ChronoCore are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.9.1] - 2026-05-04

### Summary

Three correctness bugs affecting core timing accuracy, the operator control
display, and the lap event feed were found via simulation and fixed. This
release is considered the first production-quality baseline.

---

### Bug Fixes

#### BUG-001 - Lap timing anchor drift on rejected reads (`backend/race_engine.py`)

**Severity:** Critical - laps lost during normal race operation

**Root cause:**  
In `ingest_pass()`, `ent._last_hit_ms` was unconditionally set to
`clock_ms` *before* the `min_lap_dup` and `min_lap_s` threshold checks.
Any rejected read (hardware echo, car parked near a timing loop, brief
signal reflection) advanced the timing anchor forward. The next real
crossing then computed a short delta against the drifted anchor and was
also rejected. Under continuous interference at intervals shorter than
`min_lap_s` (default 5 s) it became impossible for any lap to count,
even at a full normal lap interval.

**Secondary effect:**  
Even a single duplicate burst at the crossing point (e.g., five echoes
within 0.8 s) caused the lap time measurement to be short by the length
of the burst (the anchor moved to the last echo, not the first crossing).

**Simulation result (BUGGY):**  
- Phantom reads every 3 s, real lap at T=30 s: **0 laps counted** (expected 1).  
- 5 dup reads at T=0..0.8 s, real lap at T=30 s: **lap time = 29.2 s** (expected 30.0 s).

**Fix:**  
Removed the unconditional `ent._last_hit_ms = self.clock_ms` assignment
before the guards. The anchor now advances only after both thresholds
pass. Added an explicit `else:` branch to set the anchor on the first
crossing (previously this was the only path that worked correctly).

**Files changed:** `backend/race_engine.py`

---

#### BUG-002 - Operator control poll loop never starts (`ui/js/op_control.js`)

**Severity:** High - operator control view clock, flag, and standings
frozen from page load

**Root cause:**  
`CCRS.makePoller()` (defined in `base.js`) returns a `{start, stop}`
handle object. The call in `startPolling()` discarded the return value
without calling `.start()`, then immediately executed `return` so the
`setInterval` fallback was never reached. Because `base.js` is always
loaded and always exports `makePoller`, the branch was always taken and
polling never began.

**Simulation result (BUGGY):**  
- `handle.start_called = False`, `pollState()` calls = 0.
- Operator control UI receives zero `/race/state` updates.

**Fix:**  
Chained `.start()` to the `makePoller` call:
`CCRS.makePoller(pollState, 1000).start()`

**Files changed:** `ui/js/op_control.js`

---

#### BUG-003 - Lap feed silently drops intermediate laps (`ui/js/race_control.js`)

**Severity:** Medium - live lap feed display incomplete during network
delays or burst-poll periods

**Root cause:**  
`updateLapFeed()` called `appendLapFeedItem(row)` exactly once whenever
`cur > prev`, regardless of the lap-count delta. If a network hiccup,
slow poll, or burst-poll collision caused two or more laps to complete
between snapshots, only the final lap appeared in the feed. The standings
table correctly showed the updated count, but the feed showed a gap.

**Simulation result (BUGGY):**  
- Car A completes laps 1, 2, 3 across two polls. Only laps 1 and 3
  appear in the feed; **Lap 2 silently dropped**.

**Fix:**  
Replaced the single `appendLapFeedItem` call with a `for` loop from
`prev + 1` to `cur` (inclusive), synthesising a row with the correct
lap number for each missed entry. Intermediate entries reuse the
snapshot's `last`/`best` times (only the most recent are available).

**Files changed:** `ui/js/race_control.js`

---

### Added

- `tools/sim_bug1_timing.py` - simulation script demonstrating and verifying the anchor-drift fix (two scenarios: phantom reads, dup burst).
- `tools/sim_bug2_poller.py` - simulation script demonstrating and verifying the poll-loop-not-started fix.
- `tools/sim_bug3_lapfeed.py` - simulation script demonstrating and verifying the intermediate-lap-dropped fix.

### Changed

- Version unified to `0.9.1` across `config/config.yaml` and `backend/server.py` (previously inconsistent `0.9.0-dev` / `0.2.1-alpha`).

---

## [0.2.1-alpha] - prior releases

See git log for changes prior to 0.9.1.
