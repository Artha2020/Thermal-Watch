"""v1.0 release-gate: step-8 controlled A/B localization. One condition per invocation (selected by
argv[1]), each a fresh sandboxed App() instance so results are never contaminated by a prior
condition's state. Sensor polling and all BACKEND data processing (incident detection, session
tracking, telemetry writes) stay fully active in every condition - only a single RENDERING/UI-UPDATE
path is disabled per run, via monkeypatching applied in this script before/after App() construction.
No production code is modified.

Usage: python _leak_ab_driver.py <condition>
  baseline           - everything normal (comparison reference)
  no_event_log       - render_log() becomes a no-op (log_event's own backend list-append stays live)
  no_panel_updates   - _sync_rows skips calling update_fn() on rows that already existed
  no_chart_redraw    - HistoryChart._redraw becomes a no-op (set_points() still runs/accumulates data)
  no_metric_cards    - MetricCard.update_value becomes a no-op (all 3 cards: CPU/GPU/Memory)
  no_workload_display - the 4 alert-strip/live-badge Label widgets' .config() calls become no-ops
"""
import gc
import json
import os
import subprocess
import sys
import tempfile
import time
import tkinter
from pathlib import Path

CONDITION = sys.argv[1] if len(sys.argv) > 1 else "baseline"
VALID = {"baseline", "no_event_log", "no_panel_updates", "no_chart_redraw", "no_metric_cards",
        "no_workload_display"}
assert CONDITION in VALID, f"unknown condition {CONDITION!r}, expected one of {VALID}"

RUN_MINUTES = 12
SAMPLE_EVERY_S = 30

SANDBOX_DIR = Path(tempfile.mkdtemp(prefix=f"thermal_watch_ab_{CONDITION}_"))
os.environ["THERMAL_WATCH_DATA_DIR"] = str(SANDBOX_DIR)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402

disable_panel_updates = [CONDITION == "no_panel_updates"]  # mutable cell, read inside the wrapper
_orig_sync_rows = appmod.App._sync_rows


def _wrapped_sync_rows(self, cache, body, specs):
    """Same shape as the localization driver's wrapper, minus the logging - here only to
    optionally skip update_fn() on rows that already existed, when this condition is selected."""
    if not disable_panel_updates[0]:
        return _orig_sync_rows(self, cache, body, specs)
    seen = set()
    for spec in specs:
        key, build_fn, update_fn = spec[0], spec[1], spec[2]
        legacy_keys = spec[3] if len(spec) > 3 else None
        seen.add(key)
        refs = cache.get(key)
        if refs is None and legacy_keys:
            for legacy_key in legacy_keys:
                if legacy_key != key and legacy_key in cache:
                    refs = cache.pop(legacy_key)
                    cache[key] = refs
                    break
        if refs is None:
            refs = build_fn(body)
            cache[key] = refs
            self.widget_stats["rows_created"] += 1
            update_fn(refs)  # populate initial content for a brand-new row only
        # existing rows: update_fn() intentionally SKIPPED for this condition
    for stale_key in [k for k in cache if k not in seen]:
        cache[stale_key]["frame"].destroy()
        del cache[stale_key]
        self.widget_stats["rows_destroyed"] += 1


appmod.App._sync_rows = _wrapped_sync_rows

if CONDITION == "no_event_log":
    appmod.App.render_log = lambda self: None

if CONDITION == "no_chart_redraw":
    appmod.HistoryChart._redraw = lambda self: None

if CONDITION == "no_metric_cards":
    appmod.MetricCard.update_value = lambda self, *a, **kw: None

from app import App, DATA_DIR, TELEMETRY_DB_PATH  # noqa: E402

assert str(DATA_DIR) == str(SANDBOX_DIR), f"FAIL: DATA_DIR did not redirect: {DATA_DIR}"
print(f"=== CONDITION={CONDITION}  sandboxed to {DATA_DIR}", flush=True)

SELF_PID = os.getpid()


def query_self():
    cmd = (f"Get-Process -Id {SELF_PID} | Select-Object WorkingSet64,PrivateMemorySize64,"
          "HandleCount | ConvertTo-Json")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                        capture_output=True, text=True, timeout=15).stdout
    d = json.loads(out)
    return d["WorkingSet64"] / 1048576.0, d["PrivateMemorySize64"] / 1048576.0, d["HandleCount"]


def widget_total(widget):
    return 1 + sum(widget_total(c) for c in widget.winfo_children())


errors = []
application = App()
application.report_callback_exception = lambda *a: errors.append(a)

if CONDITION == "no_workload_display":
    for w in (application.alert_badge, application.alert_tag, application.alert_text,
             application.live_badge):
        w.config = lambda **kw: None

state = {"start": time.time(), "samples": []}


def tk_commands():
    return application.tk.eval("info commands").split()


def tick():
    now = time.time()
    t_min = (now - state["start"]) / 60.0
    ws_mb, priv_mb, handles = query_self()
    total_widgets = widget_total(application)
    gc.collect()
    commands = tk_commands()
    widget_cmds = sum(1 for c in commands if c.startswith("."))
    loose_cmds = len(commands) - widget_cmds
    after_pending = len(application.tk.call("after", "info"))

    row = {"t_min": t_min, "ws_mb": ws_mb, "priv_mb": priv_mb, "handles": handles,
          "widgets": total_widgets, "widget_cmds": widget_cmds, "loose_cmds": loose_cmds,
          "after_pending": after_pending}
    state["samples"].append(row)
    print(f"SAMPLE t={t_min:5.1f}min ws={ws_mb:7.1f}MB priv={priv_mb:7.1f}MB handles={handles:4d} "
          f"widgets={total_widgets:4d} widget_cmds={widget_cmds:4d} loose_cmds={loose_cmds:4d} "
          f"after={after_pending:2d}", flush=True)

    if t_min >= RUN_MINUTES:
        finish()
    else:
        application.after(SAMPLE_EVERY_S * 1000, tick)


def finish():
    samples = state["samples"]
    first, last = samples[0], samples[-1]
    elapsed = last["t_min"] - first["t_min"]
    priv_slope = (last["priv_mb"] - first["priv_mb"]) / elapsed if elapsed > 0 else 0.0
    ws_slope = (last["ws_mb"] - first["ws_mb"]) / elapsed if elapsed > 0 else 0.0

    print(f"\n---- A/B RESULT: {CONDITION} ----", flush=True)
    print(f"DURATION_MIN={elapsed:.1f}  SAMPLES={len(samples)}  TK_ERRORS={len(errors)}", flush=True)
    for e in errors[:5]:
        print(e, flush=True)
    print(f"PRIV_START_MB={first['priv_mb']:.1f}", flush=True)
    print(f"PRIV_END_MB={last['priv_mb']:.1f}", flush=True)
    print(f"PRIV_SLOPE_MB_PER_MIN={priv_slope:.3f}", flush=True)
    print(f"WS_SLOPE_MB_PER_MIN={ws_slope:.3f}", flush=True)
    print(f"WIDGET_DELTA={last['widgets'] - first['widgets']:+d}", flush=True)
    print(f"WIDGET_CMD_DELTA={last['widget_cmds'] - first['widget_cmds']:+d}", flush=True)
    print(f"LOOSE_CMD_DELTA={last['loose_cmds'] - first['loose_cmds']:+d}", flush=True)
    print(f"AFTER_PENDING_DELTA={last['after_pending'] - first['after_pending']:+d}", flush=True)
    print(f"HANDLES_DELTA={last['handles'] - first['handles']:+d}", flush=True)
    print(f"SANDBOX_DIR={SANDBOX_DIR}", flush=True)
    application.stop_event.set()
    application.destroy()


application.after(15000, tick)
application.mainloop()
