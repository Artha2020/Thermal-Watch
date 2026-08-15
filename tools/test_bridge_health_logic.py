"""Unit-level checks of the Python-side bridge health/recovery logic - no elevation needed."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import (App, compute_bridge_health, BRIDGE_FRESH_SECONDS,  # noqa: E402
                 BRIDGE_RECOVERY_MIN_INTERVAL_S, _process_exists, os)


def main():
    print("=== compute_bridge_health() pure-function checks ===")
    assert compute_bridge_health(None, None) == "MISSING"
    assert compute_bridge_health(2.0, None) == "HEALTHY"
    assert compute_bridge_health(BRIDGE_FRESH_SECONDS + 5, None) == "STALE"
    assert compute_bridge_health(BRIDGE_FRESH_SECONDS + 5, {"state": "ERROR"}) == "ERROR"
    assert compute_bridge_health(2.0, {"state": "ERROR"}) == "HEALTHY"  # fresh file wins even if last poll errored
    print("  PASS: MISSING/HEALTHY/STALE/ERROR all classify correctly")

    print("\n=== _process_exists() ===")
    assert _process_exists(os.getpid()) is True
    assert _process_exists(999999) is False  # implausible PID
    print("  PASS: correctly identifies our own live PID and a nonexistent one")

    print("\n=== rate limiting in check_bridge_health() (monkeypatched, no real elevation) ===")
    app = App()
    calls = []
    import app as app_module
    real_spawn = app_module.spawn_bridge_recovery
    app_module.spawn_bridge_recovery = lambda: (calls.append(time.time()) or True)
    real_age = app_module.bridge_tier1_age
    app_module.bridge_tier1_age = lambda: 999  # pretend always stale
    real_status = app_module.bridge_status
    app_module.bridge_status = lambda: None

    try:
        app.last_bridge_recovery_attempt = 0.0
        app.check_bridge_health()
        assert len(calls) == 1, "first stale check should trigger one recovery attempt"
        assert app.bridge_health == "RESTARTING"
        app.check_bridge_health()
        app.check_bridge_health()
        assert len(calls) == 1, f"FAIL: rate limit did not hold, got {len(calls)} calls in quick succession"
        print(f"  PASS: 1 recovery attempt triggered, then correctly rate-limited "
              f"(next allowed only after {BRIDGE_RECOVERY_MIN_INTERVAL_S}s)")
    finally:
        app_module.spawn_bridge_recovery = real_spawn
        app_module.bridge_tier1_age = real_age
        app_module.bridge_status = real_status
        app.stop_event.set()
        app.destroy()

    print("\nALL BRIDGE HEALTH LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
