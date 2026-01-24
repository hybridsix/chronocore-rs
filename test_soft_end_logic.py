"""
Test soft-end logic for both time-based and lap-based races.

Tests verify:
1. WHITE flag fires at traditional times (T-60s for time races, lap N-1 for lap races)
2. CHECKERED flag fires at limit (T=0 for time, lap N for lap races)
3. With soft_end: race continues counting laps after CHECKERED for timeout period
4. finish_order tracks crossing sequence after CHECKERED
5. Race freezes after soft_end timeout expires
"""

import sys
import time
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from backend.race_engine import RaceEngine

def simulate_time_race_soft_end():
    """Test time-based race with soft_end enabled."""
    print("\n" + "="*80)
    print("TEST 1: Time-based race with soft_end (3 minute race, 30s timeout)")
    print("="*80)
    
    config = {
        "app": {
            "engine": {
                "persistence": {
                    "sqlite_path": ":memory:",
                    "enabled": False
                }
            },
            "features": {
                "pit_timing": False,
                "auto_provisional": False
            }
        },
        "modes": {
            "test_time": {
                "limit": {
                    "type": "time",
                    "value_s": 180,  # 3 minutes
                    "soft_end": True,
                    "soft_end_timeout_s": 30
                },
                "min_lap_s": 5.0
            }
        }
    }
    
    engine = RaceEngine(config)
    
    # Load race with 3 entrants
    entrants = [
        {"entrant_id": 1, "enabled": True, "status": "ACTIVE", "tag": "TAG001", "number": "1", "name": "Driver 1"},
        {"entrant_id": 2, "enabled": True, "status": "ACTIVE", "tag": "TAG002", "number": "2", "name": "Driver 2"},
        {"entrant_id": 3, "enabled": True, "status": "ACTIVE", "tag": "TAG003", "number": "3", "name": "Driver 3"},
    ]
    
    engine.load(1, entrants, "test_time")
    print(f"[OK] Race loaded: flag={engine.flag}")
    
    # Start race
    engine.set_flag("GREEN")
    start_time = time.perf_counter()
    engine.clock_start_monotonic = start_time - 120.0  # Pretend we started 120s ago
    print(f"[OK] Race started: flag={engine.flag}, running={engine.running}")
    
    # Simulate time passing to 120s (T-60s window should trigger WHITE)
    engine._update_clock()  # This will calculate clock_ms from monotonic time
    print(f"  DEBUG: clock_ms={engine.clock_ms}, _time_limit_s={engine._time_limit_s}")
    print(f"  DEBUG: _white_window_begun={engine._white_window_begun}, _white_set={engine._white_set}")
    print(f"  DEBUG: rem={engine._time_limit_s - (engine.clock_ms/1000.0)}")
    snapshot = engine.snapshot()  # snapshot also calls _update_clock and _maybe_auto_white_time
    print(f"[OK] At ~T=120s: flag={engine.flag}, clock_ms={engine.clock_ms} (should be WHITE)")
    print(f"  DEBUG after snapshot: _white_window_begun={engine._white_window_begun}")
    assert engine.flag == "white", f"Expected WHITE at T-60s, got {engine.flag}"
    
    # Simulate time passing to 180s (limit reached, should trigger CHECKERED)
    engine.clock_start_monotonic = start_time - 180.0  # Pretend we started 180s ago
    engine._update_clock()
    print(f"[OK] At ~T=180s (limit): flag={engine.flag}, running={engine.running}")
    assert engine.flag == "checkered", f"Expected CHECKERED at T=0, got {engine.flag}"
    assert engine.running == True, f"Race should still be running with soft_end"
    checkered_start = engine._checkered_flag_start_ms
    assert checkered_start is not None, "CHECKERED start time should be captured"
    print(f"  CHECKERED started at clock_ms={checkered_start}")
    
    # Simulate entrants crossing finish line after CHECKERED (during soft_end window)
    # First crossing by Driver 1
    engine.entrants[1]._last_hit_ms = checkered_start - 10000  # Set previous mark (10s before CHECKERED)
    engine.clock_ms = checkered_start + 2000  # T+2s
    result1 = engine.ingest_pass("TAG001")
    print(f"[OK] Driver 1 crosses at T+2s: lap_added={result1['lap_added']}, finish_order={engine.entrants[1].finish_order}")
    assert result1['lap_added'] == True, "Lap should be counted during soft_end"
    assert engine.entrants[1].finish_order == 1, "First to cross should have finish_order=1"
    assert engine.entrants[1].soft_end_completed == True, "Should mark as completed"
    
    # Second crossing by Driver 2
    engine.entrants[2]._last_hit_ms = checkered_start - 10000
    engine.clock_ms = checkered_start + 5000  # T+5s
    result2 = engine.ingest_pass("TAG002")
    print(f"[OK] Driver 2 crosses at T+5s: lap_added={result2['lap_added']}, finish_order={engine.entrants[2].finish_order}")
    assert result2['lap_added'] == True, "Lap should be counted during soft_end"
    assert engine.entrants[2].finish_order == 2, "Second to cross should have finish_order=2"
    
    # Third crossing by Driver 3
    engine.entrants[3]._last_hit_ms = checkered_start - 10000
    engine.clock_ms = checkered_start + 10000  # T+10s
    result3 = engine.ingest_pass("TAG003")
    print(f"[OK] Driver 3 crosses at T+10s: lap_added={result3['lap_added']}, finish_order={engine.entrants[3].finish_order}")
    assert result3['lap_added'] == True, "Lap should be counted during soft_end"
    assert engine.entrants[3].finish_order == 3, "Third to cross should have finish_order=3"
    
    # Try to make Driver 1 cross again - should be rejected
    engine.clock_ms = checkered_start + 15000  # T+15s
    result1_again = engine.ingest_pass("TAG001")
    print(f"[OK] Driver 1 tries to cross again at T+15s: lap_added={result1_again['lap_added']}, reason={result1_again['reason']}")
    assert result1_again['lap_added'] == False, "Second crossing should be rejected"
    assert result1_again['reason'] == "soft_end_completed", "Should indicate soft_end_completed"
    
    # Simulate timeout expiration (30s after CHECKERED)
    engine.clock_ms = checkered_start + 31000  # T+31s (past 30s timeout)
    engine._update_clock()
    print(f"[OK] At T+31s (past timeout): running={engine.running}, clock_ms_frozen={engine.clock_ms_frozen}")
    assert engine.running == False, "Race should be frozen after timeout"
    assert engine.clock_ms_frozen == checkered_start + 31000, "Final time should be captured"
    
    # Verify sorting uses finish_order
    snapshot = engine.snapshot()
    standings = snapshot['standings']
    print(f"[OK] Final standings order: {[s['number'] for s in standings]}")
    assert standings[0]['number'] == "1", "Driver 1 (finish_order=1) should be first"
    assert standings[1]['number'] == "2", "Driver 2 (finish_order=2) should be second"
    assert standings[2]['number'] == "3", "Driver 3 (finish_order=3) should be third"
    
    print("\n[PASS] Time-based soft_end test PASSED")
    return True


def simulate_lap_race_soft_end():
    """Test lap-based race with soft_end enabled."""
    print("\n" + "="*80)
    print("TEST 2: Lap-based race with soft_end (10 laps, 30s timeout)")
    print("="*80)
    
    config = {
        "app": {
            "engine": {
                "persistence": {
                    "sqlite_path": ":memory:",
                    "enabled": False
                }
            },
            "features": {
                "pit_timing": False,
                "auto_provisional": False
            }
        },
        "modes": {
            "test_laps": {
                "limit": {
                    "type": "laps",
                    "value_laps": 10,
                    "soft_end": True,
                    "soft_end_timeout_s": 30
                },
                "min_lap_s": 5.0
            }
        }
    }
    
    engine = RaceEngine(config)
    
    # Load race with 3 entrants
    entrants = [
        {"entrant_id": 1, "enabled": True, "status": "ACTIVE", "tag": "TAG001", "number": "1", "name": "Driver 1"},
        {"entrant_id": 2, "enabled": True, "status": "ACTIVE", "tag": "TAG002", "number": "2", "name": "Driver 2"},
        {"entrant_id": 3, "enabled": True, "status": "ACTIVE", "tag": "TAG003", "number": "3", "name": "Driver 3"},
    ]
    
    engine.load(1, entrants, "test_laps")
    print(f"[OK] Race loaded: flag={engine.flag}")
    
    # Start race
    engine.set_flag("GREEN")
    print(f"[OK] Race started: flag={engine.flag}, running={engine.running}")
    
    # Simulate laps for all drivers up to lap 8
    base_time = 10000
    lap_time = 12000  # 12 seconds per lap
    
    for lap in range(1, 9):  # Laps 1-8
        for driver_id in [1, 2, 3]:
            tag = f"TAG00{driver_id}"
            engine.clock_ms = base_time + (lap * lap_time) + (driver_id * 100)  # Stagger drivers
            if lap == 1:
                engine.entrants[driver_id]._last_hit_ms = base_time  # Set initial mark
            else:
                engine.entrants[driver_id]._last_hit_ms = base_time + ((lap - 1) * lap_time) + (driver_id * 100)
            
            result = engine.ingest_pass(tag)
            if lap == 1:
                print(f"  Driver {driver_id} completes lap {result.get('entrant_id') and engine.entrants[driver_id].laps}")
    
    print(f"[OK] All drivers at lap 8")
    
    # Leader (Driver 1) completes lap 9 - should trigger WHITE
    engine.clock_ms = base_time + (9 * lap_time) + 100
    engine.entrants[1]._last_hit_ms = base_time + (8 * lap_time) + 100
    result = engine.ingest_pass("TAG001")
    print(f"[OK] Driver 1 completes lap 9: flag={engine.flag}")
    assert engine.flag == "white", f"Expected WHITE at lap N-1, got {engine.flag}"
    assert engine.entrants[1].laps == 9, "Driver 1 should be on lap 9"
    
    # Leader (Driver 1) completes lap 10 - should trigger CHECKERED
    checkered_time = base_time + (10 * lap_time) + 100
    engine.clock_ms = checkered_time
    engine.entrants[1]._last_hit_ms = base_time + (9 * lap_time) + 100
    result = engine.ingest_pass("TAG001")
    print(f"[OK] Driver 1 completes lap 10 (limit): flag={engine.flag}, running={engine.running}, laps={engine.entrants[1].laps}")
    assert engine.flag == "checkered", f"Expected CHECKERED at lap N, got {engine.flag}"
    assert engine.running == True, f"Race should still be running with soft_end"
    assert engine.entrants[1].laps == 10, "Driver 1 should have 10 laps"
    assert engine.entrants[1].finish_order == 1, "Driver 1 should have finish_order=1"
    assert engine._checkered_flag_start_ms == checkered_time, "CHECKERED start time should be captured"
    
    # Drivers 2 and 3 still on lap 8, now complete lap 9 (during soft_end)
    engine.clock_ms = checkered_time + 3000  # 3s after CHECKERED
    engine.entrants[2]._last_hit_ms = base_time + (8 * lap_time) + 200
    result2 = engine.ingest_pass("TAG002")
    print(f"[OK] Driver 2 completes lap 9 (soft_end): lap_added={result2['lap_added']}, finish_order={engine.entrants[2].finish_order}")
    assert result2['lap_added'] == True, "Lap should be counted during soft_end"
    assert engine.entrants[2].laps == 9, "Driver 2 should have 9 laps"
    assert engine.entrants[2].finish_order == 2, "Driver 2 should have finish_order=2"
    
    engine.clock_ms = checkered_time + 5000  # 5s after CHECKERED
    engine.entrants[3]._last_hit_ms = base_time + (8 * lap_time) + 300
    result3 = engine.ingest_pass("TAG003")
    print(f"[OK] Driver 3 completes lap 9 (soft_end): lap_added={result3['lap_added']}, finish_order={engine.entrants[3].finish_order}")
    assert result3['lap_added'] == True, "Lap should be counted during soft_end"
    assert engine.entrants[3].laps == 9, "Driver 3 should have 9 laps"
    assert engine.entrants[3].finish_order == 3, "Driver 3 should have finish_order=3"
    
    # Try Driver 2 to complete another lap - should be rejected (soft_end_completed)
    engine.clock_ms = checkered_time + 15000  # 15s after CHECKERED
    result2_again = engine.ingest_pass("TAG002")
    print(f"[OK] Driver 2 tries lap 10: lap_added={result2_again['lap_added']}, reason={result2_again['reason']}")
    assert result2_again['lap_added'] == False, "Second crossing should be rejected"
    assert result2_again['reason'] == "soft_end_completed", "Should indicate soft_end_completed"
    
    # Simulate timeout expiration (30s after CHECKERED)
    engine.clock_ms = checkered_time + 31000  # 31s after CHECKERED
    engine._update_clock()
    print(f"[OK] At +31s (past timeout): running={engine.running}, clock_ms_frozen={engine.clock_ms_frozen}")
    assert engine.running == False, "Race should be frozen after timeout"
    assert engine.clock_ms_frozen == checkered_time + 31000, "Final time should be captured"
    
    # Verify sorting: Driver 1 (10 laps) wins, then by finish_order for same lap count
    snapshot = engine.snapshot()
    standings = snapshot['standings']
    print(f"[OK] Final standings: {[(s['number'], s['laps']) for s in standings]}")
    assert standings[0]['number'] == "1", "Driver 1 (10 laps, finish_order=1) should be first"
    assert standings[0]['laps'] == 10, "Winner should have 10 laps"
    assert standings[1]['number'] == "2", "Driver 2 (9 laps, finish_order=2) should be second"
    assert standings[1]['laps'] == 9, "Second place should have 9 laps"
    assert standings[2]['number'] == "3", "Driver 3 (9 laps, finish_order=3) should be third"
    assert standings[2]['laps'] == 9, "Third place should have 9 laps"
    
    print("\n[PASS] Lap-based soft_end test PASSED")
    return True


def simulate_hard_end_time_race():
    """Test time-based race with soft_end disabled (hard end)."""
    print("\n" + "="*80)
    print("TEST 3: Time-based race WITHOUT soft_end (hard end)")
    print("="*80)
    
    config = {
        "app": {
            "engine": {
                "persistence": {
                    "sqlite_path": ":memory:",
                    "enabled": False
                }
            },
            "features": {
                "pit_timing": False,
                "auto_provisional": False
            }
        },
        "modes": {
            "test_time_hard": {
                "limit": {
                    "type": "time",
                    "value_s": 180,  # 3 minutes
                    "soft_end": False  # Hard end
                },
                "min_lap_s": 5.0
            }
        }
    }
    
    engine = RaceEngine(config)
    
    entrants = [
        {"entrant_id": 1, "enabled": True, "status": "ACTIVE", "tag": "TAG001", "number": "1", "name": "Driver 1"},
        {"entrant_id": 2, "enabled": True, "status": "ACTIVE", "tag": "TAG002", "number": "2", "name": "Driver 2"},
    ]
    
    engine.load(1, entrants, "test_time_hard")
    engine.set_flag("GREEN")
    print(f"[OK] Race started with soft_end={engine.soft_end}")
    
    # Reach time limit - should trigger CHECKERED
    engine.clock_ms = 180000
    engine._update_clock()
    print(f"[OK] At T=180s: flag={engine.flag}, running={engine.running}")
    assert engine.flag == "checkered", f"Expected CHECKERED at limit"
    assert engine.running == False, "Race should freeze immediately (hard end)"
    assert engine.clock_ms_frozen == 180000, "Clock should be frozen"
    
    # Try to add lap after CHECKERED - should be rejected
    engine.entrants[1]._last_hit_ms = 170000
    engine.clock_ms = 182000
    result = engine.ingest_pass("TAG001")
    print(f"[OK] Try to cross after CHECKERED: lap_added={result['lap_added']}, reason={result['reason']}")
    assert result['lap_added'] == False, "Laps should not count after hard CHECKERED"
    assert result['reason'] == "checkered_freeze", "Should indicate checkered_freeze"
    
    print("\n[PASS] Hard-end test PASSED")
    return True


if __name__ == "__main__":
    try:
        # Run all tests
        test1_passed = simulate_time_race_soft_end()
        test2_passed = simulate_lap_race_soft_end()
        test3_passed = simulate_hard_end_time_race()
        
        print("\n" + "="*80)
        if test1_passed and test2_passed and test3_passed:
            print("[SUCCESS] ALL TESTS PASSED!")
            print("="*80)
            sys.exit(0)
        else:
            print("[FAIL] SOME TESTS FAILED")
            print("="*80)
            sys.exit(1)
            
    except AssertionError as e:
        print(f"\n[FAIL] TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n[FAIL] ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
