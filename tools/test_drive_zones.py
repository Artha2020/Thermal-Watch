"""Real-timing check of the per-drive zone debounce engine (mirrors test_cpu_zones.py)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import App  # noqa: E402

DRIVE = "Storage TEST DRIVE"
NAME = "TEST DRIVE"


def state(app):
    return app.drive_zone_state.get(DRIVE, {}).get("confirmed", "GREEN")


def show(app, label):
    key = f"disk:{DRIVE}"
    print(f"[{label:>6}] confirmed={state(app):7} active={key in app.active_alerts}  "
          f"last_event={app.events[0]['kind'] + ': ' + app.events[0]['text'] if app.events else '-'}")


def main():
    app = App()

    print("--- 1. idle GREEN (45C) ---")
    app._update_drive_zone(DRIVE, NAME, 45.0)
    show(app, "t+0.0")
    assert state(app) == "GREEN"

    print("\n--- 2. jump to YELLOW/WARM (62C): must NOT alert before 3s sustained ---")
    app._update_drive_zone(DRIVE, NAME, 62.0)
    show(app, "t+0.0")
    assert state(app) == "GREEN", "FAIL: alerted instantly"
    time.sleep(3.2)
    app._update_drive_zone(DRIVE, NAME, 62.5)
    show(app, "t+3.2")
    assert state(app) == "YELLOW"
    assert f"disk:{DRIVE}" in app.active_alerts

    print("\n--- 3. jump to ORANGE/HOT (74C): must NOT alert before another 3s ---")
    app._update_drive_zone(DRIVE, NAME, 74.0)
    show(app, "t+0.0")
    assert state(app) == "YELLOW"
    time.sleep(3.2)
    app._update_drive_zone(DRIVE, NAME, 74.3)
    show(app, "t+3.2")
    assert state(app) == "ORANGE"

    print("\n--- 4. jump to RED/CRITICAL (85C): must alert IMMEDIATELY ---")
    app._update_drive_zone(DRIVE, NAME, 85.0)
    show(app, "t+0.0")
    assert state(app) == "RED"
    assert app.events[0]["kind"] == "CRIT"
    assert NAME in app.events[0]["text"] and "CRITICAL" in app.events[0]["text"]

    print("\n--- 5. drop straight to GREEN (50C): must clear IMMEDIATELY ---")
    app._update_drive_zone(DRIVE, NAME, 50.0)
    show(app, "t+0.0")
    assert state(app) == "GREEN"
    assert f"disk:{DRIVE}" not in app.active_alerts

    print("\nALL DRIVE-ZONE CHECKS PASSED")
    app.stop_event.set()
    app.destroy()


if __name__ == "__main__":
    main()
