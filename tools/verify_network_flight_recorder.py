"""Verification for v1.1 Phase 4 - Network Flight Recorder.

App._detect_network_flight_events() logs a NETWORK-kind event log entry whenever the active
adapter's identity or connection state genuinely changes, reusing the existing event log/
Flight Recorder Timeline architecture entirely (TIMELINE_LOG_KINDS, build_timeline's "log"
builder) rather than adding a new store or a new timeline kind. This suite covers: never
logging on the first observation, correct detection of connect/disconnect/switch/link-change,
no log when nothing changed, and that a logged NETWORK event actually surfaces on the Timeline.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import (  # noqa: E402
    App, build_timeline, TIMELINE_LOG_KINDS, _NET_STATE_UNSET,
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


def net(adapter):
    return {"adapter": adapter}


def eth(index=1, name="Ethernet", connected=True):
    return {"index": index, "name": name, "type": "Ethernet", "media_connect_state": connected,
           "in_octets": 0, "out_octets": 0, "receive_link_speed_bps": None, "transmit_link_speed_bps": None}


def wifi(index=2, name="Wi-Fi", connected=True):
    return {"index": index, "name": name, "type": "Wi-Fi", "media_connect_state": connected,
           "in_octets": 0, "out_octets": 0, "receive_link_speed_bps": None, "transmit_link_speed_bps": None}


print("=" * 78)
print("1. TIMELINE_LOG_KINDS / _colors_for wiring")
print("=" * 78)
check("NETWORK is included in TIMELINE_LOG_KINDS (so it reaches the Flight Recorder Timeline)",
      "NETWORK" in TIMELINE_LOG_KINDS)
check("WARN/CRIT are still present - Phase 4 is additive, not a replacement", "WARN" in TIMELINE_LOG_KINDS and "CRIT" in TIMELINE_LOG_KINDS)

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
    fg, border = App._colors_for("NETWORK")
    check("NETWORK has its own distinct dashboard-feed color, not the generic fallback",
          (fg, border) != App._colors_for("UNKNOWN_KIND"))

    print()
    print("=" * 78)
    print("2. _detect_network_flight_events() - state-machine correctness")
    print("=" * 78)
    check("starts in the UNSET sentinel state", app._prev_net_adapter is _NET_STATE_UNSET)

    events_before = len(app.events)
    app._detect_network_flight_events(net(eth()))
    check("first-ever observation logs NOTHING (no prior state to have changed from)",
          len(app.events) == events_before)
    check("prev state is now seeded from the first observation", app._prev_net_adapter is not None
          and app._prev_net_adapter["name"] == "Ethernet")

    app._detect_network_flight_events(net(eth()))
    check("identical adapter on the next tick logs nothing (nothing changed)",
          len(app.events) == events_before)

    app._detect_network_flight_events(net(None))
    check("adapter disappearing logs exactly one NETWORK event", len(app.events) == events_before + 1)
    check("event kind is NETWORK", app.events[0]["kind"] == "NETWORK")
    check("event text honestly names what was lost", "Ethernet" in app.events[0]["text"] and "lost" in app.events[0]["text"].lower())

    app._detect_network_flight_events(net(None))
    check("staying offline on the next tick logs nothing more", len(app.events) == events_before + 1)

    app._detect_network_flight_events(net(wifi()))
    check("adapter reappearing after an absence logs a 'connected via' event",
          len(app.events) == events_before + 2 and "connected via Wi-Fi" in app.events[0]["text"])

    app._detect_network_flight_events(net(eth()))
    check("switching identity (Wi-Fi -> Ethernet) logs a 'switched' event, not a connect/disconnect pair",
          len(app.events) == events_before + 3 and "switched" in app.events[0]["text"].lower()
          and "Wi-Fi" in app.events[0]["text"] and "Ethernet" in app.events[0]["text"])

    app._detect_network_flight_events(net(eth(connected=False)))
    check("same adapter losing its link (index unchanged, connected flips) logs a link-down event, "
          "not a full disconnect/switch message",
          len(app.events) == events_before + 4 and "link down" in app.events[0]["text"].lower())

    app._detect_network_flight_events(net(eth(connected=True)))
    check("the same adapter's link coming back logs a link-restored event",
          len(app.events) == events_before + 5 and "link restored" in app.events[0]["text"].lower())

    print()
    print("=" * 78)
    print("3. NETWORK events reach the Flight Recorder Timeline")
    print("=" * 78)
    now = time.time()
    events = build_timeline(now - 3600, now + 1, incidents=[], sessions=[], experiments=[], buckets=[],
                            log_records=[{"ts": now, "kind": "NETWORK", "text": "Network — connected via Ethernet"}])
    network_rows = [e for e in events if e["kind"] == "log" and e["severity"] == "NETWORK"]
    check("a NETWORK-kind log record surfaces as a 'log' timeline entry with severity=NETWORK",
          len(network_rows) == 1, f"got {len(network_rows)} matching rows")
    check("its title is the real logged text, not a placeholder",
          network_rows and network_rows[0]["title"] == "Network — connected via Ethernet")
finally:
    app.destroy()

print()
print("=" * 78)
print("4. real App(): a genuine adapter transition through update_data() lands in the event feed")
print("=" * 78)
app2 = App()
app2.stop_event.set()
for after_id in app2.tk.eval("after info").split():
    try:
        command = app2.tk.call("after", "info", after_id)[0]
    except Exception:
        continue
    if any(str(command).endswith(name) for name in app2._RECURRING_AFTER_METHODS):
        app2.after_cancel(after_id)
try:
    base_payload = {"time": None, "cpu_load": 0, "mem_pct": 0, "mem_used": 0, "mem_total": 0,
                     "gpus": [], "lhm": None, "workload": None,
                     "net_procs": {"capture_active": False, "capture_error": None, "top": []},
                     "connections": []}
    from datetime import datetime as _dt
    app2.update_data({**base_payload, "time": _dt.now(), "net": net(eth())})
    events_after_first = len(app2.events)
    app2.update_data({**base_payload, "time": _dt.now(), "net": net(None)})
    check("a real update_data() cycle observing a lost adapter logs a real NETWORK event",
          len(app2.events) == events_after_first + 1 and app2.events[0]["kind"] == "NETWORK")
    check("the event was also persisted to the event log file, same as any WARN/CRIT event",
          any('"kind": "NETWORK"' in line for line in
              __import__("app").EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines()))
finally:
    app2.destroy()

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
