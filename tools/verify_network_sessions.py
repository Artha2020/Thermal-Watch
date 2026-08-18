"""Verification for v1.1 Phase 5 - Network Sessions.

Adds net_down_mbps/net_up_mbps to the existing per-session streaming aggregate machinery
(SESSION_METRIC_KEYS, _session_apply_sample, _finalize_session_record's new "network" block) -
the same avg/peak-over-samples pattern cpu_temp/gpu_temp already use, not a new engine. Network
figures are the ACTIVE ADAPTER's whole-machine rate observed while the workload was active - an
observed correlation, never a causal "this workload used N Mbps" claim (no per-process,
session-scoped network data exists in this app). Drives the session engine directly
(App._session_observe_tick()), exactly like verify_workload_sessions.py, rather than duplicating
that script's own already-passing debounce/idle-grace/restart coverage.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import (  # noqa: E402
    App, SESSIONS_PATH, ACTIVE_SESSIONS_PATH, SESSION_METRIC_KEYS,
    SESSION_START_DEBOUNCE_SAMPLES, SESSION_IDLE_GRACE_S,
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
    for p in (SESSIONS_PATH, ACTIVE_SESSIONS_PATH):
        if p.exists():
            p.unlink()


def tick(app, cpu_top=(), gpu_top=(), foreground=None, context=None, dt=None):
    app.last_cpu_top = list(cpu_top)
    app.last_gpu_top = list(gpu_top)
    app.last_foreground = foreground
    app.last_context = context or {}
    if dt is not None:
        app._session_last_tick_time = time.time() - dt
    app._session_observe_tick()


print("=" * 78)
print("1. schema wiring")
print("=" * 78)
check("net_down_mbps is a tracked session metric", "net_down_mbps" in SESSION_METRIC_KEYS)
check("net_up_mbps is a tracked session metric", "net_up_mbps" in SESSION_METRIC_KEYS)

fresh_files()
app = App()

print()
print("=" * 78)
print("2. avg/peak accumulate correctly across real ticks, exactly like cpu_temp/gpu_temp")
print("=" * 78)
down_samples = [40.0, 55.0, 30.0, 90.0, 20.0]
up_samples = [2.0, 3.0, 1.5, 8.0, 2.5]
observed_down, observed_up = [], []

for i in range(SESSION_START_DEBOUNCE_SAMPLES):
    d, u = down_samples[i % len(down_samples)], up_samples[i % len(up_samples)]
    tick(app, cpu_top=[("Steam.exe", 4242, 90.0)], context={"net_down_mbps": d, "net_up_mbps": u})
    observed_down.append(d); observed_up.append(u)
sess = app.workload_sessions["steam.exe"]
check("session confirmed with real network samples already accumulating", sess["confirmed"])

for d, u in zip(down_samples, up_samples):
    tick(app, cpu_top=[("Steam.exe", 4242, 90.0)], context={"net_down_mbps": d, "net_up_mbps": u})
    observed_down.append(d); observed_up.append(u)

app.workload_sessions["steam.exe"]["last_active_timestamp"] -= (SESSION_IDLE_GRACE_S + 5)
before = len(app.sessions_recent)
tick(app, cpu_top=[])
check("session closed after idle grace", len(app.sessions_recent) == before + 1)
closed = app.sessions_recent[0]
net = closed.get("network") or {}

expected_avg_down = sum(observed_down) / len(observed_down)
expected_peak_down = max(observed_down)
expected_avg_up = sum(observed_up) / len(observed_up)
expected_peak_up = max(observed_up)

check("network block exists on the completed session", "network" in closed)
check("avg_down_mbps matches the exact streaming average over every real sample",
      net.get("avg_down_mbps") is not None and abs(net["avg_down_mbps"] - expected_avg_down) < 1e-9,
      f"got {net.get('avg_down_mbps')}, expected {expected_avg_down}")
check("peak_down_mbps is the true maximum observed, not the last sample",
      net.get("peak_down_mbps") == expected_peak_down)
check("avg_up_mbps matches the exact streaming average", net.get("avg_up_mbps") is not None
      and abs(net["avg_up_mbps"] - expected_avg_up) < 1e-9)
check("peak_up_mbps is the true maximum observed", net.get("peak_up_mbps") == expected_peak_up)

print()
print("=" * 78)
print("3. honesty: a session with zero real network samples reports None, never a fabricated 0")
print("=" * 78)
# Deliberately NOT fresh_files() here - this session ("blender.exe") is a different workload key
# from section 2's ("steam.exe"), and section 4 below needs section 2's persisted session to
# still be on disk to verify its round-trip through SESSIONS_PATH.
app2 = App()
for _ in range(SESSION_START_DEBOUNCE_SAMPLES):
    # context deliberately carries no net_down_mbps/net_up_mbps key at all - the same shape
    # last_context has whenever active_network_snapshot() found no adapter this tick.
    tick(app2, cpu_top=[("Blender.exe", 555, 90.0)], context={"cpu_temp": 60.0})
app2.workload_sessions["blender.exe"]["last_active_timestamp"] -= (SESSION_IDLE_GRACE_S + 5)
tick(app2, cpu_top=[])
closed_no_net = app2.sessions_recent[0]
net_empty = closed_no_net.get("network") or {}
check("avg_down_mbps is None, not 0.0, when no network sample was ever observed",
      net_empty.get("avg_down_mbps") is None)
check("peak_down_mbps is None, not 0.0, when no network sample was ever observed",
      net_empty.get("peak_down_mbps") is None)
check("avg_up_mbps is None when never observed", net_empty.get("avg_up_mbps") is None)
check("peak_up_mbps is None when never observed", net_empty.get("peak_up_mbps") is None)
check("cpu block is unaffected by the missing network data (independent aggregates)",
      closed_no_net["cpu"]["avg_temp"] == 60.0)

print()
print("=" * 78)
print("4. persists across restart, byte-identical on reload")
print("=" * 78)
from app import read_sessions_file  # noqa: E402
reloaded = read_sessions_file()
by_id = {s["session_id"]: s for s in reloaded}
check("the network-carrying session round-trips through SESSIONS_PATH with its network block intact",
      closed["session_id"] in by_id and by_id[closed["session_id"]].get("network") == closed.get("network"))

print()
print("=" * 78)
print("5. SessionsWindow detail rendering")
print("=" * 78)
from app import SessionsWindow  # noqa: E402
win = SessionsWindow(app)
try:
    win._show_detail(closed)
    text = win.detail_text.cget("text")
    check("a session with real network data shows a NETWORK section with real Mbps figures",
          "NETWORK" in text and "Mbps" in text and f"{expected_peak_down:.1f}" in text,
          f"peak_down expected in text: {expected_peak_down:.1f}")

    win._show_detail(closed_no_net)
    text2 = win.detail_text.cget("text")
    check("a session with no real network data shows NO network section at all (not a "
          "misleading 'N/A' block)", "NETWORK" not in text2)
finally:
    win.destroy()
    app.stop_event.set(); app.destroy()
    app2.stop_event.set(); app2.destroy()

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
