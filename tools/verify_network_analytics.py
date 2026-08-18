"""Verification for v1.1 Phase 7 - Network Analytics.

Reuses Application Analytics' existing baseline-learning/anomaly-detection/health-score
machinery entirely: BASELINE_SESSION_METRICS gained four network entries
(avg/peak down/up Mbps), and compute_workload_baseline()/evaluate_session_anomalies()/the
Analytics detail renderer all already iterate that list generically - per-workload network
baselines and session-level network anomaly flagging come from that addition alone. The only
NEW code is: Mbps-aware formatting precision (0 decimals is uninformative for typical sub-1-Mbps
averages) and a deliberate exclusion of network anomalies from Health Score deductions (unusual
bandwidth isn't a thermal/operational health concern the way an unusual temperature is).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import (  # noqa: E402
    App, AnalyticsWindow, BASELINE_SESSION_METRICS, ANOMALY_MIN_ABS_DELTA,
    compute_workload_baseline, evaluate_session_anomalies, compute_session_health_score,
    count_anomalous_sessions, BASELINE_MIN_SESSIONS, compute_workload_stats,
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


def make_session(sid, down, up, cpu_temp=60.0):
    return {"session_id": sid, "workload": "Steam.exe", "workload_key": "steam.exe",
           "start_timestamp": 1000.0, "end_timestamp": 1300.0, "duration_seconds": 300.0,
           "incident_count": 0, "max_incident_severity": None, "zone_time": {},
           "cpu": {"avg_temp": cpu_temp, "peak_temp": cpu_temp + 5},
           "gpu": {}, "memory": {},
           "network": {"avg_down_mbps": down, "peak_down_mbps": down * 1.5,
                      "avg_up_mbps": up, "peak_up_mbps": up * 1.5}}


print("=" * 78)
print("1. schema wiring")
print("=" * 78)
network_metrics = [m for m in BASELINE_SESSION_METRICS if m[0] == "network"]
check("all 4 network metrics are registered", len(network_metrics) == 4,
      f"found: {[m[1] for m in network_metrics]}")
check("network unit is ' Mbps' (leading space, matches the formatter's precision check)",
      all(m[3] == " Mbps" for m in network_metrics))
check("ANOMALY_MIN_ABS_DELTA has a Mbps fallback threshold", ANOMALY_MIN_ABS_DELTA.get(" Mbps") == 5.0)

print()
print("=" * 78)
print("2. compute_workload_baseline() computes real network stats")
print("=" * 78)
sessions = [make_session(f"s{i}", down, up) for i, (down, up) in
           enumerate([(40.0, 2.0), (55.0, 3.0), (30.0, 1.5), (90.0, 8.0), (20.0, 2.5)])]
baseline = compute_workload_baseline(sessions)
check("network.avg_down_mbps baseline is present and established (5 sessions >= min)",
      baseline["network.avg_down_mbps"]["stats"] is not None
      and baseline["network.avg_down_mbps"]["stats"]["established"])
expected_avg = sum(40.0 + 55.0 + 30.0 + 90.0 + 20.0 for _ in [1]) and (40.0 + 55.0 + 30.0 + 90.0 + 20.0) / 5
check("baseline mean matches the exact real average", abs(baseline["network.avg_down_mbps"]["stats"]["mean"] - expected_avg) < 1e-9)
check("baseline peak_down_mbps also computed", baseline["network.peak_down_mbps"]["stats"] is not None)

too_few = sessions[:2]
baseline_few = compute_workload_baseline(too_few)
check(f"fewer than {BASELINE_MIN_SESSIONS} sessions -> established=False, never presented as reliable",
      baseline_few["network.avg_down_mbps"]["stats"]["established"] is False)

print()
print("=" * 78)
print("3. evaluate_session_anomalies() flags real network outliers")
print("=" * 78)
outlier = make_session("outlier", 500.0, 40.0)  # way above the 20-90 Mbps baseline range
anomalies = evaluate_session_anomalies(outlier, baseline)
check("an extreme download spike is flagged unusual vs this workload's baseline",
      anomalies.get("network.avg_down_mbps", {}).get("anomaly", {}).get("unusual") is True)
normal = make_session("normal", 45.0, 2.5)
anomalies_normal = evaluate_session_anomalies(normal, baseline)
check("a download rate within the normal range is NOT flagged",
      not anomalies_normal.get("network.avg_down_mbps", {}).get("anomaly", {}).get("unusual", False))

print()
print("=" * 78)
print("4. Health Score deliberately excludes network anomalies")
print("=" * 78)
health_with_outlier = compute_session_health_score(outlier, anomalies, [])
network_deductions = [d for d in health_with_outlier["deductions"] if "network" in d["reason"].lower()]
check("a session with ONLY a network anomaly (no thermal issues) loses zero points for it",
      health_with_outlier["score"] == 100.0,
      f"score={health_with_outlier['score']}, deductions={health_with_outlier['deductions']}")

thermal_anomalies = {"cpu.avg_temp": {"label": "CPU", "unit": "°C", "current": 95.0,
                                      "anomaly": {"unusual": True, "delta": 20, "z_score": 4.0, "baseline_mean": 60.0}}}
health_with_thermal = compute_session_health_score(outlier, thermal_anomalies, [])
check("a REAL thermal anomaly still docks points normally (the exclusion is network-specific, not blanket)",
      health_with_thermal["score"] < 100.0)

mixed_anomalies = dict(anomalies)
mixed_anomalies.update(thermal_anomalies)
health_mixed = compute_session_health_score(outlier, mixed_anomalies, [])
check("mixed thermal+network anomalies: score matches thermal-only (network contributes nothing)",
      health_mixed["score"] == health_with_thermal["score"])

print()
print("=" * 78)
print("5. count_anomalous_sessions() DOES count a network-only anomaly (informational, not scoring)")
print("=" * 78)
mixed_group = sessions + [outlier]
count = count_anomalous_sessions(mixed_group)
check("the network-outlier session is counted as anomalous in the informational tally",
      count is not None and count >= 1)

print()
print("=" * 78)
print("6. real App()/AnalyticsWindow: renders the network baseline and VS BASELINE sections")
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
    win = AnalyticsWindow(app)
    try:
        # Real stat shape via the same function AnalyticsWindow._recompute() itself calls, then
        # layer on the network baseline exactly like _recompute() layers on "baseline" - rather
        # than guessing every field _show_detail() happens to read.
        stat = compute_workload_stats("steam.exe", "Steam.exe", [], now=2000.0)
        stat["recorded_sessions"] = len(sessions)
        stat["baseline"] = baseline
        win._show_detail(stat)
        analytics_text = win.detail_text.cget("text")
        check("AnalyticsWindow's BASELINE section shows a real, decimal-precision Mbps figure "
              "for this workload (not a rounded-to-0 '0 Mbps')",
              "Mbps" in analytics_text and any("." in line.split("Mbps")[0][-6:]
                                               for line in analytics_text.splitlines() if "Mbps" in line),
              f"lines with Mbps: {[l for l in analytics_text.splitlines() if 'Mbps' in l]}")
    finally:
        win.destroy()

    from app import SessionsWindow  # noqa: E402
    swin = SessionsWindow(app)
    try:
        all_for_detail = sessions + [outlier]
        swin.all_sessions = all_for_detail
        swin._show_detail(outlier)
        text = swin.detail_text.cget("text")
        check("SessionsWindow's VS BASELINE section renders a Mbps figure with real decimal precision, "
              "not a rounded-to-0 '0 Mbps'", "Mbps" in text and ("." in text.split("Mbps")[0][-6:]),
              f"rendered excerpt around Mbps: {[l for l in text.splitlines() if 'Mbps' in l]}")
    finally:
        swin.destroy()
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
