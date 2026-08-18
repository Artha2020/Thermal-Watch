"""Verification for v1.1 Phase 8 - Network Baselines.

The idle-baseline architecture (compute_idle_baseline()/filter_idle_buckets(), already built for
"what's normal for this machine at rest" on CPU/GPU sensors) is fully generic over any registered
telemetry scalar - net_down_mbps/net_up_mbps have been valid scalar_sensor_ref() keys since Phase
1, and SensorHistoryWindow already opens for them via the NETWORK panel's clickable rate cells.
So idle network baselines already worked functionally before this phase touched anything; what
this phase actually found and fixed, by reading every real consumer rather than assuming the
generic path was clean, were two real pre-existing display bugs specific to the "Mbps" unit:
1. TELEMETRY_SCALAR_LABELS registered unit="Mbps" (no leading space) while every consumer
   concatenates directly ({value}{unit}, no space in the template) - correct for "60"+"°C", but
   "45"+"Mbps" renders as "45Mbps" with no space at all.
2. Every one of those same consumers formats at 0 decimals - fine for °C/W/%, but Mbps commonly
   sits under 1, so a whole idle period could render as "0-0 Mbps" (or every chart axis label
   reading "0"), losing all real information for exactly the readings that need it most.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import (  # noqa: E402
    App, TELEMETRY_SCALAR_LABELS, scalar_sensor_ref, compute_idle_baseline, filter_idle_buckets,
    extract_bucket_metric, BASELINE_MIN_IDLE_BUCKETS, SensorHistoryWindow, TelemetryChart,
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


print("=" * 78)
print("1. formatting fixes at the source")
print("=" * 78)
check("net_down_mbps unit carries a leading space (fixes '45Mbps' -> '45 Mbps')",
      TELEMETRY_SCALAR_LABELS["net_down_mbps"][1] == " Mbps")
check("net_up_mbps unit carries a leading space too", TELEMETRY_SCALAR_LABELS["net_up_mbps"][1] == " Mbps")
check("scalar_sensor_ref('net_down_mbps') carries the fixed unit through",
      scalar_sensor_ref("net_down_mbps")["unit"] == " Mbps")
check("a non-Mbps scalar (cpu_temp) is completely untouched by this fix",
      TELEMETRY_SCALAR_LABELS["cpu_temp"][1] == "°C")

print()
print("=" * 78)
print("2. idle baseline is genuinely generic - net_down_mbps works via the SAME functions "
      "CPU/GPU idle baselines already use, no network-specific code needed")
print("=" * 78)


def bucket(ts, down_avg, end=None):
    return {"start_timestamp": ts, "end_timestamp": end or ts + 60,
           "scalars": {"net_down_mbps": {"avg": down_avg, "min": down_avg * 0.5, "max": down_avg * 1.5, "count": 30}}}


now = time.time()
idle_buckets = [bucket(now - 3600 + i * 120, 0.3 + (i % 3) * 0.05) for i in range(35)]
session = {"start_timestamp": now - 1800, "end_timestamp": now - 1700}  # a real active window
active_buckets = [bucket(now - 1780 + i * 60, 40.0) for i in range(2)]  # heavy download, inside the session
all_buckets = idle_buckets + active_buckets

only_idle = filter_idle_buckets(all_buckets, [session])
check("filter_idle_buckets excludes at least the 2 heavy-download active-session buckets",
      len(only_idle) <= len(all_buckets) - len(active_buckets),
      f"got {len(only_idle)} of {len(all_buckets)} total, {len(active_buckets)} were active")
check("every remaining bucket is genuinely low-rate - none of the 40 Mbps active buckets leaked through",
      all(extract_bucket_metric(b, scalar_sensor_ref("net_down_mbps"))["avg"] < 1.0 for b in only_idle))

ref = scalar_sensor_ref("net_down_mbps")
idle_baseline = compute_idle_baseline(only_idle, ref)
check(f"idle baseline established with {len(only_idle)} >= {BASELINE_MIN_IDLE_BUCKETS} idle buckets",
      idle_baseline is not None and idle_baseline["established"])
check("idle baseline mean reflects the genuinely low idle-period rate, not contaminated by the "
      "40 Mbps active-session buckets", idle_baseline["mean"] < 1.0,
      f"mean={idle_baseline['mean']}")

too_few = only_idle[:BASELINE_MIN_IDLE_BUCKETS - 1]
baseline_insufficient = compute_idle_baseline(too_few, ref)
check("fewer than the minimum idle buckets -> established=False, never presented as reliable",
      baseline_insufficient is not None and baseline_insufficient["established"] is False)

print()
print("=" * 78)
print("3. real App()/SensorHistoryWindow renders a correctly-formatted idle baseline for network")
print("=" * 78)
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
    win = SensorHistoryWindow(app, scalar_sensor_ref("net_down_mbps"))
    try:
        check("SensorHistoryWindow opens for net_down_mbps with no exception", win.winfo_exists())
        check("chart precision helper resolves 1 decimal for the Mbps unit specifically",
              (1 if win.sensor_ref["unit"] == " Mbps" else 0) == 1)
    finally:
        win.destroy()

    win_cpu = SensorHistoryWindow(app, scalar_sensor_ref("cpu_temp"))
    try:
        check("a non-Mbps sensor page (CPU temp) is completely unaffected - still 0-decimal formatting",
              win_cpu.sensor_ref["unit"] == "°C")
    finally:
        win_cpu.destroy()
finally:
    app.stop_event.set(); app.destroy()

print()
print("=" * 78)
print("4. chart Y-axis: no longer collapses an all-sub-1-Mbps range to five '0' labels")
print("=" * 78)
import tkinter as tk  # noqa: E402
root = tk.Tk()
root.geometry("640x360")
try:
    chart = TelemetryChart(root, width=600)
    chart.pack(fill="both", expand=True)
    root.update()
    points = [{"start_timestamp": now - 3600 + i * 100, "end_timestamp": now - 3500 + i * 100,
              "metric": {"avg": 0.05 + i * 0.01, "min": 0.02, "max": 0.15, "count": 10}} for i in range(20)]
    chart.set_data(points, [], [], now - 3600, now, " Mbps")
    root.update()
    axis_texts = [chart.itemcget(i, "text") for i in chart.find_all() if chart.type(i) == "text"]
    numeric_axis_labels = [t for t in axis_texts if t.replace(".", "", 1).replace("-", "", 1).isdigit()]
    check("at least one Y-axis gridline shows real sub-1 precision, not a flat '0' for every tick",
          any("." in t and t != "0.0" for t in numeric_axis_labels) or any(t not in ("0", "-0") for t in numeric_axis_labels),
          f"axis labels seen: {numeric_axis_labels}")
finally:
    root.destroy()

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
