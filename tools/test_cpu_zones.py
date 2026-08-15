"""Standalone check of the 4-zone CPU alert engine's debounce behavior.

Drives App._update_cpu_zone directly with synthetic temperatures and real
sleeps, and prints the resulting confirmed zone / active_alerts / event log
after each step so the debounce timing can be eyeballed.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import App  # noqa: E402


def show(app, label):
    print(f"[{label:>6}] confirmed={app.cpu_zone_confirmed:7} "
          f"active_alerts={list(app.active_alerts.keys())}  "
          f"last_event={app.events[0]['kind'] + ': ' + app.events[0]['text'] if app.events else '-'}")


def main():
    app = App()

    print("--- 1. idle GREEN (75C), no alert expected ---")
    app._update_cpu_zone(75.0)
    show(app, "t+0.0")

    print("\n--- 2. jump to YELLOW (82C): must NOT alert before 3s sustained ---")
    app._update_cpu_zone(82.0)
    show(app, "t+0.0")
    time.sleep(1.5)
    app._update_cpu_zone(82.3)
    show(app, "t+1.5")
    assert app.cpu_zone_confirmed == "GREEN", "FAIL: alerted before debounce window elapsed"
    time.sleep(1.8)
    app._update_cpu_zone(82.1)
    show(app, "t+3.3")
    assert app.cpu_zone_confirmed == "YELLOW", "FAIL: did not confirm YELLOW after sustained 3s"
    assert "cpu" in app.active_alerts

    print("\n--- 3. jump to ORANGE (95C): must NOT alert before another 3s sustained ---")
    app._update_cpu_zone(95.0)
    show(app, "t+0.0")
    assert app.cpu_zone_confirmed == "YELLOW", "FAIL: escalated to ORANGE without debounce"
    time.sleep(3.2)
    app._update_cpu_zone(95.4)
    show(app, "t+3.2")
    assert app.cpu_zone_confirmed == "ORANGE"

    print("\n--- 4. jump to RED (101C): must alert IMMEDIATELY, no debounce ---")
    app._update_cpu_zone(101.0)
    show(app, "t+0.0")
    assert app.cpu_zone_confirmed == "RED", "FAIL: RED did not fire immediately"
    assert app.events[0]["kind"] == "CRIT"

    print("\n--- 5. drop straight to GREEN (70C): must clear IMMEDIATELY, no debounce ---")
    app._update_cpu_zone(70.0)
    show(app, "t+0.0")
    assert app.cpu_zone_confirmed == "GREEN"
    assert "cpu" not in app.active_alerts

    print("\nALL CHECKS PASSED")
    app.stop_event.set()
    app.destroy()


if __name__ == "__main__":
    main()
