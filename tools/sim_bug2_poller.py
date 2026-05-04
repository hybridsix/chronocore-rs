"""
sim_bug2_poller.py - Simulation for Bug #2: op_control.js poll loop never starts

Demonstrates that CCRS.makePoller() returns a {start, stop} handle object, but
op_control.js discards it without calling .start(), so pollState() is never
executed and the operator control view's clock/flag/standings never update.

Run this before and after applying the fix to ui/js/op_control.js.
"""

# ---------------------------------------------------------------------------
# Python model of the JavaScript objects involved
# ---------------------------------------------------------------------------

class PollerHandle:
    """Simulates the object returned by CCRS.makePoller() in base.js."""
    def __init__(self, fn, interval_ms):
        self._fn = fn
        self._interval_ms = interval_ms
        self._active = False
        self.start_called = False

    def start(self):
        self._active = True
        self.start_called = True
        # Execute one tick immediately to mirror JS behavior
        self._fn()

    def stop(self):
        self._active = False


# Tracks how many times pollState() would fire
poll_calls = []

def poll_state():
    poll_calls.append("poll")


def CCRS_makePoller(fn, interval_ms):
    """Simulates CCRS.makePoller from base.js - returns a handle, does NOT auto-start."""
    return PollerHandle(fn, interval_ms)


# ---------------------------------------------------------------------------
# BUGGY startPolling() from op_control.js (current code)
#
#   if (typeof CCRS.makePoller === "function") {
#     CCRS.makePoller(pollState, 1000);  <- handle returned but discarded
#     return;                             <- exits, so setInterval never runs
#   }
# ---------------------------------------------------------------------------

def start_polling_buggy():
    global poll_calls
    poll_calls = []

    # makePoller exists (it's always exported by base.js)
    handle = CCRS_makePoller(poll_state, 1000)
    # BUG: handle is created but .start() is never called
    # The function then returns - setInterval fallback never reached
    return handle  # handle.start_called will be False


# ---------------------------------------------------------------------------
# FIXED startPolling() from op_control.js
#
#   if (typeof CCRS.makePoller === "function") {
#     CCRS.makePoller(pollState, 1000).start();  <- .start() called
#     return;
#   }
# ---------------------------------------------------------------------------

def start_polling_fixed():
    global poll_calls
    poll_calls = []

    handle = CCRS_makePoller(poll_state, 1000)
    handle.start()   # FIX: .start() is now called
    return handle


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "="*60)
    print("  BUG #2 SIMULATION: op_control.js poll loop never starts")
    print("="*60)

    print("\n--- BUGGY (current op_control.js startPolling) ---")
    handle_b = start_polling_buggy()
    print(f"  handle.start_called = {handle_b.start_called}")
    print(f"  poll_state() calls  = {len(poll_calls)}")
    print(f"  Result: UI never receives /race/state updates -> clock/standings frozen")

    print("\n--- FIXED (after adding .start() call) ---")
    handle_f = start_polling_fixed()
    print(f"  handle.start_called = {handle_f.start_called}")
    print(f"  poll_state() calls  = {len(poll_calls)}")
    print(f"  Result: UI begins receiving /race/state updates immediately")

    print("\n" + "="*60)
    print("  SUMMARY")
    print("="*60)
    buggy_ok = not handle_b.start_called and len(poll_calls) == 0
    fixed_ok = handle_f.start_called and len(poll_calls) > 0
    print(f"  BUGGY: poll started = {handle_b.start_called}  <- BUG confirmed" if buggy_ok else "  BUGGY: unexpected result")
    print(f"  FIXED: poll started = {handle_f.start_called}  <- CORRECT"        if fixed_ok else "  FIXED: unexpected result")
