"""Verification for v1.1 Phase 9 - Cross-System Intelligence.

"What else was happening system-wide while this workload's thermal incidents occurred" already
existed for CPU/GPU power/load/memory (ANALYTICS_CONTEXT_KEYS/context_peak, captured generically
by _incident_touch() from self.last_context every tick since v1.0). Network context_peak has
been captured there automatically since v1.0 Phase 1 (net_down_mbps/net_up_mbps were added to
last_context then) - it was simply never surfaced. This phase adds network to three existing,
already-generic display surfaces (AnalyticsWindow's per-workload context aggregate, the incident
CSV export, and HistoryWindow's per-incident detail line), and nowhere else - no new capture
code, no new analysis engine. Purely observational throughout: "network activity peaked at X
Mbps during this workload's thermal incidents" is a fact about what else was happening, never a
causal claim.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import (  # noqa: E402
    App, ANALYTICS_CONTEXT_KEYS, CONTEXT_PEAK_TO_CSV, incident_to_csv_row,
    compute_workload_stats, AnalyticsWindow, HistoryWindow, INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH,
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


def make_incident(iid, dominant_workload, net_down=None, net_up=None, cpu_power=280.0):
    now = time.time()
    return {
        "incident_id": iid, "start_timestamp": now - 600, "end_timestamp": now - 300,
        "duration_seconds": 300, "component": "cpu", "sensor_name": "CPU Package",
        "sensor_identifier": None, "starting_zone": "YELLOW", "max_zone": "YELLOW",
        "start_value": 82.0, "peak_value": 86.0, "recovery_value": 60.0,
        "dominant_workload": dominant_workload, "foreground_process": None, "foreground_title": None,
        "top_cpu_processes": [], "top_gpu_processes": [],
        "context_peak": {"cpu_power": cpu_power, "net_down_mbps": net_down, "net_up_mbps": net_up},
        "samples": [], "monitoring_gaps": [], "monitoring_gap_seconds": 0.0,
        "recovery_during_monitoring_gap": False, "close_reason": "idle_grace_expired",
        "duration_exact": True,
    }


print("=" * 78)
print("1. schema wiring")
print("=" * 78)
check("net_down_mbps is a registered Analytics context key", "net_down_mbps" in ANALYTICS_CONTEXT_KEYS)
check("net_up_mbps is a registered Analytics context key", "net_up_mbps" in ANALYTICS_CONTEXT_KEYS)
check("existing context keys (cpu_power etc.) are all still present - purely additive",
      all(k in ANALYTICS_CONTEXT_KEYS for k in ("cpu_power", "gpu_power", "cpu_load", "gpu_load", "mem_pct")))
check("net_down_mbps/net_up_mbps map to real CSV column names",
      CONTEXT_PEAK_TO_CSV.get("net_down_mbps") and CONTEXT_PEAK_TO_CSV.get("net_up_mbps"))

print()
print("=" * 78)
print("2. incident CSV export carries real network context")
print("=" * 78)
inc_with_net = make_incident("cpu-1", "Steam.exe", net_down=123.45, net_up=6.78)
row = incident_to_csv_row(inc_with_net)
check("CSV row has a real, precise (2-decimal) peak network download figure",
      row.get("peak_network_download_mbps") == "123.45")
check("CSV row has a real peak network upload figure", row.get("peak_network_upload_mbps") == "6.78")

inc_no_net = make_incident("cpu-2", "OldApp.exe", net_down=None, net_up=None)
row_no_net = incident_to_csv_row(inc_no_net)
check("an incident that predates network context capture exports blank cells, never a fabricated 0",
      row_no_net.get("peak_network_download_mbps") == "" and row_no_net.get("peak_network_upload_mbps") == "")

print()
print("=" * 78)
print("3. real Application Analytics aggregates network context per workload")
print("=" * 78)
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
    incidents = [
        make_incident("cpu-a", "Steam.exe", net_down=100.0, net_up=5.0),
        make_incident("cpu-b", "Steam.exe", net_down=200.0, net_up=10.0),
        make_incident("cpu-c", "Steam.exe", net_down=None, net_up=None),  # honest gap in one incident
    ]
    stat = compute_workload_stats("steam.exe", "Steam.exe", incidents, now=time.time())
    net_ctx = stat["context"].get("net_down_mbps")
    check("per-workload network context aggregate computed from real incident data",
          net_ctx is not None and net_ctx["count"] == 2,
          f"got {net_ctx}")
    check("avg peak download matches the exact real average of the 2 incidents that observed it",
          abs(net_ctx["avg_peak"] - 150.0) < 1e-9)
    check("max peak download is the true maximum, not the last value", net_ctx["max_peak"] == 200.0)

    win = AnalyticsWindow(app)
    try:
        win._show_detail(stat)
        text = win.detail_text.cget("text")
        check("AnalyticsWindow's detail panel shows a real NETWORK DOWNLOAD context section "
              "with decimal-precision Mbps, not '0-0 Mbps'",
              "NETWORK DOWNLOAD" in text and "150.0 Mbps" in text,
              f"lines: {[l for l in text.splitlines() if 'NETWORK' in l or 'Mbps' in l]}")
    finally:
        win.destroy()

    no_net_stat = compute_workload_stats("oldapp.exe", "OldApp.exe", [inc_no_net], now=time.time())
    check("a workload with zero observed network context shows no NETWORK section at all "
          "(never a misleading placeholder)",
          no_net_stat["context"].get("net_down_mbps") is None)

    print()
    print("=" * 78)
    print("4. real HistoryWindow per-incident detail shows network context, honestly omitted when absent")
    print("=" * 78)
    hist = HistoryWindow(app)
    try:
        hist._show_detail(inc_with_net)
        detail_text = hist.detail_text.cget("text")
        check("per-incident detail line includes real network peak figures",
              "Network down peak: 123.5 Mbps" in detail_text and "Network up peak: 6.8 Mbps" in detail_text,
              f"detail text tail: {detail_text.splitlines()[-1] if detail_text else None}")

        hist._show_detail(inc_no_net)
        detail_text_no_net = hist.detail_text.cget("text")
        check("an incident with no network context omits the network segment entirely - "
              "existing fields (e.g. CPU power peak, which this fixture DOES set) still render "
              "exactly as before, untouched by the network addition",
              "Network down peak" not in detail_text_no_net and "Network up peak" not in detail_text_no_net
              and "CPU power peak: 280W" in detail_text_no_net,
              f"detail text tail: {detail_text_no_net.splitlines()[-1] if detail_text_no_net else None}")
    finally:
        hist.destroy()
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
