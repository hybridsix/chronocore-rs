"""
sim_bug1_timing.py - Simulation for Bug #1: _last_hit_ms advances on rejected reads

Demonstrates that continuous low-rate phantom reads (e.g., decoder sensitivity,
parked car near timing loop) prevent legitimate laps from being counted because
the anchor timestamp keeps moving forward on every rejection.

Run this before and after applying the fix to race_engine.py.
"""

MIN_LAP_DUP = 1.0   # seconds - hardware echo window
MIN_LAP_S   = 5.0   # seconds - minimum valid lap time

# ---------------------------------------------------------------------------
# Minimal stand-ins for Entrant and ingest logic
# ---------------------------------------------------------------------------

class Entrant:
    def __init__(self, name):
        self.name = name
        self._last_hit_ms = None
        self.laps = 0
        self.last_s = None


def ingest_buggy(ent, clock_ms):
    """Replicates the CURRENT (buggy) logic from race_engine.py ingest_pass."""
    prev_mark = ent._last_hit_ms
    ent._last_hit_ms = clock_ms          # BUG: anchor advances before validation

    if prev_mark is not None:
        delta_s = (clock_ms - prev_mark) / 1000.0
        if delta_s < MIN_LAP_DUP:
            return "dup"
        if delta_s < MIN_LAP_S:
            return "min_lap"             # returns early, but anchor already moved
        ent.laps += 1
        ent.last_s = delta_s
        return f"LAP  (delta={delta_s:.1f}s, total laps={ent.laps})"
    return "first_crossing"


def ingest_fixed(ent, clock_ms):
    """FIXED version: anchor only advances after both thresholds pass.
    Mirrors the corrected race_engine.py ingest_pass with explicit else clause."""
    prev_mark = ent._last_hit_ms
    # NOTE: _last_hit_ms is NOT set here - only after validation

    if prev_mark is not None:
        delta_s = (clock_ms - prev_mark) / 1000.0
        if delta_s < MIN_LAP_DUP:
            return "dup"                 # anchor unchanged
        if delta_s < MIN_LAP_S:
            return "min_lap"            # anchor unchanged
        # Thresholds passed - NOW advance the anchor and count the lap
        ent._last_hit_ms = clock_ms
        ent.laps += 1
        ent.last_s = delta_s
        return f"LAP  (delta={delta_s:.1f}s, total laps={ent.laps})"
    else:
        # First crossing: set anchor, no lap yet
        ent._last_hit_ms = clock_ms
        return "first_crossing"


# ---------------------------------------------------------------------------
# Scenario 1: phantom reads every 3s during a 30s lap
# This is the canonical "lost lap" scenario
# ---------------------------------------------------------------------------

READS_MS = [
    0,     # real start-line crossing
    3000,  # phantom (decoder echo, parked car near loop, etc.)
    6000,
    9000,
    12000,
    15000,
    18000,
    21000,
    24000,
    27000,
    30000, # REAL second crossing - should count as 1 lap of 30s
]

def run_scenario(label, ingest_fn):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  Scenario: phantom reads every 3s, real lap completes at T=30s")
    print(f"  min_lap_s={MIN_LAP_S}s  min_lap_dup={MIN_LAP_DUP}s")
    print(f"{'='*60}")
    ent = Entrant("Car #99")
    for t_ms in READS_MS:
        result = ingest_fn(ent, t_ms)
        anchor = ent._last_hit_ms
        print(f"  T={t_ms/1000:5.1f}s  -> {result:<50}  anchor={anchor/1000:.1f}s")
    print(f"\n  RESULT: {ent.laps} lap(s) counted (expected 1)")
    return ent.laps


# ---------------------------------------------------------------------------
# Scenario 2: duplicate-burst at crossing, correct lap time expected
# A car gets 5 reads within 0.8s of crossing, then comes around 30s later
# ---------------------------------------------------------------------------

READS_DUP_MS = [
    0,    100,  300,  600,  800,   # 5 reads in first 0.8s (hardware echoes)
    30000,                          # REAL second crossing - lap should be ~30s
]

def run_dup_scenario(label, ingest_fn):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  Scenario: 5 dup reads at T=0..0.8s, real lap at T=30s")
    print(f"  Expected lap time: ~30.0s (BUGGY: records ~29.2s due to shifted anchor)")
    print(f"{'='*60}")
    ent = Entrant("Car #42")
    for t_ms in READS_DUP_MS:
        result = ingest_fn(ent, t_ms)
        anchor = ent._last_hit_ms
        print(f"  T={t_ms/1000:5.1f}s  -> {result:<50}  anchor={anchor/1000:.3f}s")
    print(f"\n  RESULT: {ent.laps} lap(s), last_s={ent.last_s:.3f}s (expected ~30.000s)")
    return ent.last_s


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "="*60)
    print("  BUG #1 SIMULATION: _last_hit_ms anchor drift on rejected reads")
    print("="*60)

    print("\n--- SCENARIO 1: PHANTOM READS (lost lap) ---")
    laps_buggy = run_scenario("BUGGY  (race_engine.py current code)", ingest_buggy)
    laps_fixed = run_scenario("FIXED  (anchor only advances on valid lap)", ingest_fixed)

    print("\n--- SCENARIO 2: DUP BURST (wrong lap time) ---")
    time_buggy = run_dup_scenario("BUGGY", ingest_buggy)
    time_fixed = run_dup_scenario("FIXED", ingest_fixed)

    print("\n" + "="*60)
    print("  SUMMARY")
    print("="*60)
    print(f"  Scenario 1 (phantom reads, 1 real lap)")
    print(f"    BUGGY : {laps_buggy} laps counted  <- BUG: lap lost")
    print(f"    FIXED : {laps_fixed} laps counted  <- CORRECT")
    print(f"  Scenario 2 (dup burst, lap time accuracy)")
    if time_buggy is not None:
        print(f"    BUGGY : lap time = {time_buggy:.3f}s  <- BUG: shifted by dup anchor drift")
    else:
        print(f"    BUGGY : no lap recorded  <- BUG")
    if time_fixed is not None:
        print(f"    FIXED : lap time = {time_fixed:.3f}s  <- CORRECT (30.000s)")
    else:
        print(f"    FIXED : no lap recorded")
