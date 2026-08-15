"""v1.0 release-gate: EXTENDED 2-hour steady-state soak to determine whether the residual native/
private-memory growth found in the 30-minute soak (confirmed real via tracemalloc, canvas-item
tracking, and private-bytes-vs-working-set divergence checks - all ruled OUT as the cause, or
confirmed the growth is genuine committed memory, not OS working-set noise) continues linearly,
decelerates, plateaus, or correlates with a specific periodic operation.

Deliberately NOT doing UI window cycling this run - the main dashboard is left in a stable,
steady state so this measures ONLY normal live monitoring (2s poll, 60s telemetry finalization,
5s bridge-health checks, 10s persistence flushes, 15-min report due-checks), never UI lifecycle
churn (already separately confirmed clean in the 40-cycle lifecycle stress test).

Sandboxed via THERMAL_WATCH_DATA_DIR exactly like every other diagnostic in this pass - production
data is untouched by construction, not by discipline alone.
"""
import ctypes
import gc
import json
import os
import subprocess
import sys
import tempfile
import time
import tkinter
import tracemalloc
from pathlib import Path

RUN_MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 120
SAMPLE_EVERY_S = 60
SEGMENT_MINUTES = float(sys.argv[2]) if len(sys.argv) > 2 else 15

tracemalloc.start(10)

SANDBOX_DIR = Path(tempfile.mkdtemp(prefix="thermal_watch_soak2h_"))
os.environ["THERMAL_WATCH_DATA_DIR"] = str(SANDBOX_DIR)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as appmod  # noqa: E402

# Patch BEFORE instantiating App() - __init__ captures a bound-method reference via
# self.after(..., self._check_due_reports), so the class attribute must be wrapped first for the
# recorded timestamps to actually correspond to real firings, not a copy that never runs.
report_check_firings = []
_orig_check_due_reports = appmod.App._check_due_reports


def _wrapped_check_due_reports(self):
    report_check_firings.append(time.time())
    return _orig_check_due_reports(self)


appmod.App._check_due_reports = _wrapped_check_due_reports

from app import App, TELEMETRY_DB_PATH, DATA_DIR, BRIDGE_STATUS_PATH  # noqa: E402

assert str(DATA_DIR) == str(SANDBOX_DIR), f"FAIL: DATA_DIR did not redirect: {DATA_DIR}"
print(f"=== sandboxed to {DATA_DIR} - production stores cannot be touched by this run", flush=True)

SELF_PID = os.getpid()


def bridge_pid():
    try:
        return json.loads(BRIDGE_STATUS_PATH.read_text(encoding="utf-8-sig")).get("pid")
    except (OSError, ValueError, KeyError):
        return None


def query_processes(pids):
    ids = ",".join(str(p) for p in pids if p)
    if not ids:
        return {}
    cmd = ("Get-Process -Id " + ids + " -ErrorAction SilentlyContinue | Select-Object Id,"
          "WorkingSet64,PrivateMemorySize64,VirtualMemorySize64,Threads,HandleCount | ConvertTo-Json")
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
        result[row["Id"]] = {
            "ws_mb": (row.get("WorkingSet64") or 0) / 1048576.0,
            "priv_mb": (row.get("PrivateMemorySize64") or 0) / 1048576.0,
            "virt_mb": (row.get("VirtualMemorySize64") or 0) / 1048576.0,
            "threads": thread_count, "handles": row.get("HandleCount") or 0,
        }
    return result


def all_widgets(widget):
    """Full recursive widget count - not just the root's direct children (which stays at 1
    regardless, since row-cache widgets live several levels deep, not as root children)."""
    count = 1
    for child in widget.winfo_children():
        count += all_widgets(child)
    return count


def all_canvas_items(widget):
    total, n_canvases = 0, 0
    if isinstance(widget, tkinter.Canvas):
        try:
            total += len(widget.find_all())
            n_canvases += 1
        except tkinter.TclError:
            pass
    for child in widget.winfo_children():
        t, n = all_canvas_items(child)
        total += t
        n_canvases += n
    return total, n_canvases


sample0 = query_processes([SELF_PID])
assert SELF_PID in sample0 and sample0[SELF_PID]["ws_mb"] > 1.0, \
    f"FAIL: instrumentation sanity check failed - self-process query returned {sample0}"
print(f"=== instrumentation sanity: self ws={sample0[SELF_PID]['ws_mb']:.1f}MB "
      f"priv={sample0[SELF_PID]['priv_mb']:.1f}MB (non-zero, real)", flush=True)

errors = []
application = App()
application.report_callback_exception = lambda *a: errors.append(a)

state = {"ticks": 0, "samples": [], "start_wall": time.time(), "prev_telemetry_kb": None}


def tick():
    state["ticks"] += 1
    now = time.time()
    bpid = bridge_pid()
    try:
        bstate = json.loads(BRIDGE_STATUS_PATH.read_text(encoding="utf-8-sig")).get("state")
    except (OSError, ValueError, KeyError):
        bstate = None
    procs = query_processes([SELF_PID, bpid])
    self_row = procs.get(SELF_PID, {})
    bridge_row = procs.get(bpid, {}) if bpid else {}

    gc.collect()
    gc_count = len(gc.get_objects())
    tm_current, tm_peak = tracemalloc.get_traced_memory()

    widget_count = all_widgets(application)
    canvas_items, canvas_count = all_canvas_items(application)
    after_pending = len(application.tk.call("after", "info"))
    telemetry_kb = TELEMETRY_DB_PATH.stat().st_size / 1024.0 if TELEMETRY_DB_PATH.exists() else 0.0
    telemetry_grew = (state["prev_telemetry_kb"] is not None and telemetry_kb > state["prev_telemetry_kb"])
    state["prev_telemetry_kb"] = telemetry_kb

    row = {
        "t_min": (now - state["start_wall"]) / 60.0, "wall": now,
        "self_ws_mb": self_row.get("ws_mb", 0.0), "self_priv_mb": self_row.get("priv_mb", 0.0),
        "self_virt_mb": self_row.get("virt_mb", 0.0), "self_threads": self_row.get("threads", 0),
        "self_handles": self_row.get("handles", 0),
        "bridge_pid": bpid, "bridge_state": bstate,
        "bridge_ws_mb": bridge_row.get("ws_mb"), "bridge_priv_mb": bridge_row.get("priv_mb"),
        "tm_current_mb": tm_current / 1048576.0, "tm_peak_mb": tm_peak / 1048576.0,
        "gc_count": gc_count, "after_pending": after_pending,
        "widget_count": widget_count, "canvas_items": canvas_items, "canvas_count": canvas_count,
        "telemetry_kb": telemetry_kb, "telemetry_grew": telemetry_grew,
    }
    state["samples"].append(row)
    print(f"SAMPLE {row['t_min']:6.1f}min  ws={row['self_ws_mb']:7.1f}MB priv={row['self_priv_mb']:7.1f}MB "
          f"virt={row['self_virt_mb']:8.1f}MB handles={row['self_handles']:4d} threads={row['self_threads']:2d} "
          f"tm_cur={row['tm_current_mb']:5.2f}MB tm_peak={row['tm_peak_mb']:5.2f}MB gc={row['gc_count']:7d} "
          f"after={row['after_pending']:2d} widgets={row['widget_count']:4d} "
          f"canvas_items={row['canvas_items']:3d}({row['canvas_count']}) "
          f"bridge={row['bridge_state']}/{row['bridge_pid']} "
          f"bridge_ws={row['bridge_ws_mb']}MB bridge_priv={row['bridge_priv_mb']}MB "
          f"telemetry={row['telemetry_kb']:7.1f}KB{'  [BUCKET FINALIZED]' if row['telemetry_grew'] else ''}",
          flush=True)

    if row["t_min"] >= RUN_MINUTES:
        finish()
    else:
        application.after(SAMPLE_EVERY_S * 1000, tick)


def finish():
    samples = state["samples"]
    print("\n---- 2-HOUR STEADY-STATE SOAK SUMMARY ----", flush=True)
    print(f"DURATION_MIN={samples[-1]['t_min']:.1f}", flush=True)
    print(f"TOTAL_SAMPLES={len(samples)}", flush=True)
    print(f"TK_ERRORS={len(errors)}", flush=True)
    for e in errors[:5]:
        print(e, flush=True)

    bridge_pids_seen = sorted({s["bridge_pid"] for s in samples if s["bridge_pid"]})
    print(f"BRIDGE_PIDS_SEEN={bridge_pids_seen}", flush=True)

    print(f"\nREPORT_DUE_CHECK_FIRINGS ({len(report_check_firings)} total):", flush=True)
    for ts in report_check_firings:
        print(f"    t={(ts - state['start_wall']) / 60.0:.1f}min", flush=True)

    bucket_finalizations = [s["t_min"] for s in samples if s["telemetry_grew"]]
    print(f"\nTELEMETRY BUCKET FINALIZATIONS DETECTED ({len(bucket_finalizations)} total, "
          f"expected ~1 per minute):", flush=True)
    print(f"    first 5: {[f'{t:.1f}' for t in bucket_finalizations[:5]]}", flush=True)
    print(f"    last 5:  {[f'{t:.1f}' for t in bucket_finalizations[-5:]]}", flush=True)

    print("\n---- 15-MINUTE SEGMENT ANALYSIS (Private Bytes) ----", flush=True)
    print(f"{'segment':<12}{'priv_start':>12}{'priv_end':>12}{'delta_mb':>12}{'slope_mb_min':>14}", flush=True)
    segments = []
    n_segments = int(RUN_MINUTES // SEGMENT_MINUTES)
    for i in range(n_segments):
        seg_start_min, seg_end_min = i * SEGMENT_MINUTES, (i + 1) * SEGMENT_MINUTES
        in_seg = [s for s in samples if seg_start_min <= s["t_min"] <= seg_end_min]
        if len(in_seg) < 2:
            continue
        start_priv, end_priv = in_seg[0]["self_priv_mb"], in_seg[-1]["self_priv_mb"]
        elapsed_min = in_seg[-1]["t_min"] - in_seg[0]["t_min"]
        delta = end_priv - start_priv
        slope = delta / elapsed_min if elapsed_min > 0 else 0.0
        segments.append({"label": f"{seg_start_min}-{seg_end_min}m", "delta": delta, "slope": slope})
        print(f"{seg_start_min:>3}-{seg_end_min:<8}{start_priv:>12.1f}{end_priv:>12.1f}{delta:>+12.1f}{slope:>+14.3f}",
              flush=True)

    print("\n---- WORKING SET (for comparison, same segments) ----", flush=True)
    for i in range(n_segments):
        seg_start_min, seg_end_min = i * SEGMENT_MINUTES, (i + 1) * SEGMENT_MINUTES
        in_seg = [s for s in samples if seg_start_min <= s["t_min"] <= seg_end_min]
        if len(in_seg) < 2:
            continue
        delta = in_seg[-1]["self_ws_mb"] - in_seg[0]["self_ws_mb"]
        print(f"  {seg_start_min:>3}-{seg_end_min}m: {delta:+.1f}MB", flush=True)

    # Verdict delegated to tools/soak_verdict.py so it can be tested deterministically against
    # synthetic series (tools/verify_soak_verdict.py). The previous inline ratio-only rule was
    # calibrated for the ~5 MB/min PDH leak and became meaningless once every slope was near zero:
    # it compared noise to noise and printed BLOCKED for a bounded 0.17 MB/min tail.
    from soak_verdict import classify, render
    slopes = [s["slope"] for s in segments]
    total_growth = samples[-1]["self_priv_mb"] - samples[0]["self_priv_mb"]
    duration = samples[-1]["t_min"] - samples[0]["t_min"]
    print("", flush=True)
    verdict_state, verdict_reason = classify(slopes, total_growth, duration)
    verdict_lines, _exit_code = render(verdict_state, verdict_reason, slopes, total_growth, duration)
    for line in verdict_lines:
        print(line, flush=True)

    print(f"\nSANDBOX_DIR={SANDBOX_DIR}", flush=True)
    application.stop_event.set()
    application.destroy()


application.after(15000, tick)
application.mainloop()
