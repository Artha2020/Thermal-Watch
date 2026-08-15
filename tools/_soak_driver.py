"""v1.0 release-gate: 30-minute soak plus UI endurance, run in isolation.

Uses Get-Process for validated resource measurements of both Thermal Watch and its external bridge,
and redirects all persistent stores to a throwaway directory through THERMAL_WATCH_DATA_DIR. The
driver asserts that redirection before launching the application, so it cannot touch real history.

No CPU/GPU stress is run here - this measures the app's own steady-state resource behavior under
normal polling plus UI open/close/reopen cycling only.
"""
import ctypes
import gc
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

MINUTES = 30
SAMPLE_EVERY_S = 30

SANDBOX_DIR = Path(tempfile.mkdtemp(prefix="thermal_watch_soak_"))
os.environ["THERMAL_WATCH_DATA_DIR"] = str(SANDBOX_DIR)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import (  # noqa: E402
    App, HistoryWindow, TELEMETRY_DB_PATH, EVENT_LOG_PATH, DATA_DIR, BRIDGE_STATUS_PATH,
)

assert str(DATA_DIR) == str(SANDBOX_DIR), f"FAIL: DATA_DIR did not redirect: {DATA_DIR}"
print(f"=== sandboxed to {DATA_DIR} - production stores cannot be touched by this run", flush=True)

SELF_PID = os.getpid()


def bridge_pid():
    try:
        return json.loads(BRIDGE_STATUS_PATH.read_text(encoding="utf-8-sig")).get("pid")
    except (OSError, ValueError, KeyError):
        return None


def query_processes(pids):
    """One PowerShell round-trip for CPU-seconds/working-set/threads/handles of the given pids.
    Get-Process is used rather than hand-rolled ctypes - it is well-tested, and it transparently
    handles reading basic stats from a DIFFERENT process (the elevated bridge) which does not need
    the same privilege as terminating one. Missing pids are simply absent from the result."""
    ids = ",".join(str(p) for p in pids if p)
    if not ids:
        return {}
    cmd = ("Get-Process -Id " + ids + " -ErrorAction SilentlyContinue | "
          "Select-Object Id,CPU,WorkingSet64,Threads,HandleCount | ConvertTo-Json")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                             capture_output=True, text=True, timeout=15).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if not out:
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    if isinstance(data, dict):
        data = [data]
    result = {}
    for row in data:
        threads = row.get("Threads")
        thread_count = len(threads) if isinstance(threads, list) else (1 if threads else 0)
        result[row["Id"]] = {"cpu_s": row.get("CPU") or 0.0,
                             "ws_mb": (row.get("WorkingSet64") or 0) / 1048576.0,
                             "threads": thread_count, "handles": row.get("HandleCount") or 0}
    return result


sample0 = query_processes([SELF_PID])
assert SELF_PID in sample0 and sample0[SELF_PID]["ws_mb"] > 1.0, \
    f"FAIL: instrumentation sanity check failed - self-process query returned {sample0}"
print(f"=== instrumentation sanity: self ws={sample0[SELF_PID]['ws_mb']:.1f}MB "
      f"handles={sample0[SELF_PID]['handles']} threads={sample0[SELF_PID]['threads']} (non-zero, real)",
      flush=True)

OPENERS = ["open_analytics", "open_trends", "open_recommendations", "open_fan_intelligence",
           "open_experiments", "open_timeline", "open_reports", "open_maintenance", "open_ask"]
ATTRS = {"open_analytics": "analytics_window", "open_trends": "trends_window",
         "open_recommendations": "recommendations_window", "open_fan_intelligence": "fan_window",
         "open_experiments": "experiments_window", "open_timeline": "timeline_window",
         "open_reports": "reports_window", "open_maintenance": "maintenance_window",
         "open_ask": "ask_window"}

errors = []
application = App()
application.report_callback_exception = lambda *a: errors.append(a)

state = {"ticks": 0, "cycles": 0, "widgets": set(), "singleton_ok": 0, "singleton_bad": 0,
         "samples": [], "prev_cpu": {}, "prev_wall": time.time(), "start_wall": time.time()}


def ui_cycle():
    hw = HistoryWindow(application)
    for opener in OPENERS:
        getattr(hw, opener)()
        application.update()
        first = getattr(hw, ATTRS[opener])
        getattr(hw, opener)()
        if getattr(hw, ATTRS[opener]) is first:
            state["singleton_ok"] += 1
        else:
            state["singleton_bad"] += 1
    application.update()
    for opener in OPENERS:
        win = getattr(hw, ATTRS[opener])
        if win is not None and win.winfo_exists():
            win.destroy()
    hw.destroy()
    application.update()
    state["cycles"] += 1


def tick():
    state["ticks"] += 1
    state["widgets"].add(len(application.winfo_children()))
    if state["ticks"] % 2 == 1:
        try:
            ui_cycle()
        except Exception:
            import traceback
            errors.append(traceback.format_exc())

    now = time.time()
    bpid = bridge_pid()
    procs = query_processes([SELF_PID, bpid])
    self_row = procs.get(SELF_PID, {})
    bridge_row = procs.get(bpid, {}) if bpid else {}

    elapsed = now - state["prev_wall"]
    cpu_pct = None
    if SELF_PID in state["prev_cpu"] and elapsed > 0:
        cpu_pct = (self_row.get("cpu_s", 0.0) - state["prev_cpu"][SELF_PID]) / elapsed * 100.0
    state["prev_cpu"][SELF_PID] = self_row.get("cpu_s", 0.0)
    state["prev_wall"] = now

    gc_raw = len(gc.get_objects())
    gc.collect()
    gc_settled = len(gc.get_objects())

    row = {
        "t_min": (now - state["start_wall"]) / 60.0,
        "self_cpu_pct": cpu_pct, "self_ws_mb": self_row.get("ws_mb", 0.0),
        "self_threads": self_row.get("threads", 0), "self_handles": self_row.get("handles", 0),
        "bridge_pid": bpid, "bridge_ws_mb": bridge_row.get("ws_mb"),
        "gc_raw": gc_raw, "gc_settled": gc_settled,
        "telemetry_kb": TELEMETRY_DB_PATH.stat().st_size / 1024.0 if TELEMETRY_DB_PATH.exists() else 0.0,
        "eventlog_kb": EVENT_LOG_PATH.stat().st_size / 1024.0 if EVENT_LOG_PATH.exists() else 0.0,
        "after_pending": len(application.tk.call("after", "info")),
    }
    state["samples"].append(row)
    cpu_str = f"{cpu_pct:5.1f}%" if cpu_pct is not None else "  n/a"
    bridge_ws_str = f"{row['bridge_ws_mb']:6.1f}MB" if row["bridge_ws_mb"] is not None else "   n/a"
    print(f"SAMPLE {row['t_min']:5.1f}min  self_cpu={cpu_str} self_ws={row['self_ws_mb']:6.1f}MB "
          f"threads={row['self_threads']:2d} handles={row['self_handles']:4d} "
          f"bridge_pid={row['bridge_pid']} bridge_ws={bridge_ws_str} "
          f"gc_raw={row['gc_raw']:7d} gc_settled={row['gc_settled']:7d} after_pending={row['after_pending']:2d} "
          f"telemetry={row['telemetry_kb']:7.1f}KB log={row['eventlog_kb']:6.1f}KB", flush=True)

    if row["t_min"] >= MINUTES:
        finish()
    else:
        application.after(SAMPLE_EVERY_S * 1000, tick)


def finish():
    samples = state["samples"]
    first, last = samples[0], samples[-1]
    cpu_values = [s["self_cpu_pct"] for s in samples if s["self_cpu_pct"] is not None]
    avg_cpu = sum(cpu_values) / len(cpu_values) if cpu_values else None

    print("\n---- SOAK SUMMARY ----", flush=True)
    print(f"DURATION_MIN={last['t_min']:.1f}", flush=True)
    print(f"UI_CYCLES={state['cycles']}  WINDOWS_PER_CYCLE={len(OPENERS)}  "
          f"TOTAL_WINDOW_OPENS={state['cycles'] * len(OPENERS)}", flush=True)
    print(f"SINGLETON_OK={state['singleton_ok']} SINGLETON_BAD={state['singleton_bad']}", flush=True)
    print(f"DASHBOARD_WIDGET_COUNTS={sorted(state['widgets'])}", flush=True)
    print(f"AVG_SELF_CPU_PCT={avg_cpu:.2f}" if avg_cpu is not None else "AVG_SELF_CPU_PCT=n/a", flush=True)
    for key in ("self_ws_mb", "self_threads", "self_handles", "gc_raw", "gc_settled",
               "telemetry_kb", "eventlog_kb", "after_pending"):
        print(f"DELTA {key:14s} {first[key]:10.1f} -> {last[key]:10.1f}  ({last[key] - first[key]:+.1f})",
              flush=True)
    bridge_first = [s["bridge_ws_mb"] for s in samples if s["bridge_ws_mb"] is not None]
    bridge_last_vals = [s["bridge_ws_mb"] for s in reversed(samples) if s["bridge_ws_mb"] is not None]
    if bridge_first and bridge_last_vals:
        print(f"DELTA bridge_ws_mb   {bridge_first[0]:10.1f} -> {bridge_last_vals[0]:10.1f}  "
              f"({bridge_last_vals[0] - bridge_first[0]:+.1f})", flush=True)
    bridge_pids_seen = sorted({s["bridge_pid"] for s in samples if s["bridge_pid"]})
    print(f"BRIDGE_PIDS_SEEN={bridge_pids_seen}", flush=True)
    print(f"TK_ERRORS={len(errors)}", flush=True)
    for e in errors[:5]:
        print(e, flush=True)
    print(f"SANDBOX_DIR={SANDBOX_DIR}", flush=True)
    application.stop_event.set()
    application.destroy()


application.after(15000, tick)
application.mainloop()
