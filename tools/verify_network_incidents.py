"""Verification for v1.1 Phase 6 - Network Incidents.

Applies the existing incident engine (open/touch/close, active_alerts, restart durability) to
sustained connectivity loss, by calling _incident_open()/_incident_touch()/_incident_close()
directly rather than through _update_sensor_zone()/_incident_observe() - both of those require a
real numeric value, and there is no honest one for connectivity (a fabricated 1.0/0.0 proxy would
show up as a nonsensical "peak 1°C" in History/Timeline). Debounce mirrors every other component:
losing connectivity needs ALERT_DEBOUNCE_S sustained before an incident opens; recovery is
immediate, same as every other component's "de-escalation never waits" rule.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import (  # noqa: E402
    App, ACTIVE_INCIDENTS_PATH, INCIDENTS_PATH, ALERT_DEBOUNCE_S,
    COMPONENT_LABELS, INCIDENT_BIAS, HistoryWindow, read_incidents_file,
)

sys.stdout.reconfigure(encoding="utf-8")

FAILURES = []
CHECKS = [0]


def check(name, condition, detail=""):
    CHECKS[0] += 1
    ok = bool(condition)
    print(f"[{'PASS' if ok else 'FAIL'}] {CHECKS[0]:2d}. {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILURES.append(name)


def fresh_files():
    for p in (INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH):
        if p.exists():
            p.unlink()


def net(connected):
    return {"adapter": ({"index": 1, "name": "Ethernet", "type": "Ethernet"} if connected else None)}


print("=" * 78)
print("1. schema wiring")
print("=" * 78)
check("network has a component label", COMPONENT_LABELS.get("network") == "Network Connectivity")
check("network has no workload-attribution bias (like drive)", INCIDENT_BIAS.get("network") is None)
check("network is a selectable component filter in History", "network" in HistoryWindow.COMPONENTS)

fresh_files()
app = App()
app.stop_event.set()
for after_id in app.tk.eval("after info").split():
    try:
        command = app.tk.call("after", "info", after_id)[0]
    except Exception:
        continue
    if any(str(command).endswith(name) for name in app._RECURRING_AFTER_METHODS):
        app.after_cancel(after_id)

try:
    print()
    print("=" * 78)
    print("2. debounce: a brief connectivity blip does NOT open an incident")
    print("=" * 78)
    app._update_network_incident(net(True))
    app._update_network_incident(net(False))
    check("one tick of disconnection alone does not open an incident (needs sustained debounce)",
          "network" not in app.incidents_active)
    app._update_network_incident(net(True))
    check("reconnecting before the debounce elapses cleanly cancels the pending alert",
          "network" not in app.active_alerts)

    print()
    print("=" * 78)
    print("3. sustained loss opens a real incident, value always honestly None")
    print("=" * 78)
    app.network_zone_state["pending"] = {"zone": "RED", "since": time.time() - ALERT_DEBOUNCE_S - 0.5}
    app._update_network_incident(net(False))
    check("sustained disconnection opens a network incident", "network" in app.incidents_active)
    inc = app.incidents_active.get("network", {})
    check("component is 'network'", inc.get("component") == "network")
    check("starting_zone is RED", inc.get("starting_zone") == "RED")
    check("start_value is honestly None - never a fabricated numeric proxy", inc.get("start_value") is None)
    check("peak_value is honestly None", inc.get("peak_value") is None)
    check("active_alerts carries the RED entry", app.active_alerts.get("network", {}).get("zone") == "RED")

    for _ in range(3):
        app._update_network_incident(net(False))
    check("peak_value stays None across repeated touches (never drifts to a fabricated value)",
          app.incidents_active["network"]["peak_value"] is None)

    print()
    print("=" * 78)
    print("4. recovery is immediate - no debounce needed to declare 'connectivity is back'")
    print("=" * 78)
    before_recent = len(app.incidents_recent)
    app._update_network_incident(net(True))
    check("reconnecting closes the incident on the very next tick, no debounce wait",
          "network" not in app.incidents_active and len(app.incidents_recent) == before_recent + 1)
    closed = app.incidents_recent[0]
    check("closed incident's component is network", closed.get("component") == "network")
    check("recovery_value is honestly None", closed.get("recovery_value") is None)
    check("dominant_workload falls back to 'Not identified' (bias=None, no tally ever collected)",
          closed.get("dominant_workload") == "Not identified")
    check("active_alerts no longer carries the network entry", "network" not in app.active_alerts)

    print()
    print("=" * 78)
    print("5. persists to disk and round-trips cleanly")
    print("=" * 78)
    from_disk = read_incidents_file()
    check("the closed network incident is on disk", any(i.get("component") == "network" for i in from_disk))

    print()
    print("=" * 78)
    print("6. History window renders a None-valued network incident without crashing")
    print("=" * 78)
    hist = HistoryWindow(app)
    try:
        hist.component_var.set("network")
        hist._apply_filters()
        check("filtering History to the network component finds the real incident, no exception",
              any(i.get("component") == "network" for i in hist.filtered))
    finally:
        hist.destroy()

    print()
    print("=" * 78)
    print("7. restart durability: an open network incident survives and resumes correctly")
    print("=" * 78)
    fresh_files()
    app.network_zone_state = {"confirmed": "GREEN", "pending": {"zone": "GREEN", "since": time.time()}}
    app.active_alerts.pop("network", None)
    app.incidents_active.pop("network", None)
    app.network_zone_state["pending"] = {"zone": "RED", "since": time.time() - ALERT_DEBOUNCE_S - 0.5}
    app._update_network_incident(net(False))
    check("re-armed and opened a fresh incident for the restart test", "network" in app.incidents_active)
    app._save_active_incidents()

    app2 = App()
    # Deliberately NOT stop_event.set() here (unlike the other tests' setup): this test calls
    # _reconcile_restored_incidents() directly below, and that method's own guard
    # (`if self.stop_event.is_set(): return`) treats a set stop_event as "already shutting
    # down, skip reconciliation" - setting it early would silently no-op the very call this
    # section exists to test. Cancelling the recurring after() jobs alone is enough safety here,
    # since nothing in this test ever pumps the Tk event loop (no update()/poll() call) to drain
    # the real worker thread's queue anyway.
    for after_id in app2.tk.eval("after info").split():
        try:
            command = app2.tk.call("after", "info", after_id)[0]
        except Exception:
            continue
        if any(str(command).endswith(name) for name in app2._RECURRING_AFTER_METHODS):
            app2.after_cancel(after_id)
    try:
        check("the restarted app loads the network incident as restore-pending",
              "network" in app2.incident_restore_pending)
        # Still disconnected after "restart" - the debounce engine gets a fair chance to
        # reconfirm from scratch, exactly like every other component's restart contract.
        app2.network_zone_state["pending"] = {"zone": "RED", "since": time.time() - ALERT_DEBOUNCE_S - 0.5}
        app2._update_network_incident(net(False))
        check("still-disconnected network state is NOT touched while restore is pending "
              "(no duplicate incident opened)", "network" not in app2.incidents_active)
        app2._reconcile_restored_incidents()
        check("reconciliation resumes the SAME network incident after restart",
              "network" in app2.incidents_active)
        check("resumed incident recorded a real monitoring gap",
              len(app2.incidents_active["network"].get("monitoring_gaps", [])) >= 1)
    finally:
        app2.stop_event.set(); app2.destroy()
finally:
    app.stop_event.set(); app.destroy()

print()
print("=" * 78)
summary = f"{CHECKS[0] - len(FAILURES)}/{CHECKS[0]} checks passed"
print(summary)
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
