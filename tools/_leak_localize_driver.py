"""v1.0 release-gate: localization instrumentation for the steady-state native memory leak found by
the 2-hour soak (Private Bytes 32.0MB -> 607.3MB, ~4.3-5.3MB/min, not explained by Python objects,
canvas items, or bridge memory - and NOT the already-fixed _bind_click issue, which is verified
separately and not revisited here without new evidence).

Adds NO production code changes - every hook is a monkeypatch applied in THIS script, before
App() is constructed, so app.py itself is untouched during diagnosis (steps 1-11 of the
localization plan explicitly forbid modifying production code before a fix is proven).

Covers:
  1. Widget-class inventory, grouped by class AND by dashboard container (which panel)
  2. Widget identity diffing between intervals (new/vanished paths, by container+class)
  3. _sync_rows instrumentation: creates/destroys/rekeys per panel, with sensor identity logged
     for every genuine create (rekeys are distinguished from creates by OBJECT IDENTITY, not
     inferred - if cache[new_key] is the same object that existed under an old key, it's a rekey)
  4. Python widget-object count (gc-tracked) vs Tcl's own `info commands` count - if Tcl-side
     commands grow faster than Python-tracked widgets, that is direct, unambiguous evidence of a
     native-level registration leak independent of anything Python's own accounting can see
  5/6. after() callback count AND the full `info commands` name list, diffed between intervals to
     classify growth as widget commands (start with ".") vs "loose" registered commands (bind/
     after/trace targets - exactly the category the _bind_click bug fell into, checked again here
     in case something SIMILAR exists elsewhere)

Sandboxed via THERMAL_WATCH_DATA_DIR - production stores untouched by construction. Main dashboard
only, no child windows opened, no CPU/GPU stress - steady-state monitoring only, matching what the
2-hour soak actually measured.
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

RUN_MINUTES = 25
SAMPLE_EVERY_S = 45

SANDBOX_DIR = Path(tempfile.mkdtemp(prefix="thermal_watch_localize_"))
os.environ["THERMAL_WATCH_DATA_DIR"] = str(SANDBOX_DIR)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402

# ---------------------------------------------------------------------------
# Instrumentation: patch BEFORE App() exists, exactly like the 2-hour soak's report-check hook -
# __init__-time self.after(...) captures a bound method, and _sync_rows is called by name (self.
# _sync_rows(...)) every poll, so patching the CLASS attribute before instantiation is what makes
# every call site (fan/volt/disk/gpu_thermal/mobo/ram) go through the wrapper.
# ---------------------------------------------------------------------------
sync_stats = {}  # panel name -> {"creates", "destroys", "rekeys", "cache_size", "created_log"}
panel_name_by_cache_id = {}  # filled once App() exists and its cache dicts are known
_orig_sync_rows = appmod.App._sync_rows


def _wrapped_sync_rows(self, cache, body, specs):
    panel = panel_name_by_cache_id.get(id(cache), f"unknown@{id(cache)}")
    before_ids = {k: id(v) for k, v in cache.items()}
    before_created = self.widget_stats.get("rows_created", 0)
    before_destroyed = self.widget_stats.get("rows_destroyed", 0)

    result = _orig_sync_rows(self, cache, body, specs)

    stat = sync_stats.setdefault(panel, {"creates": 0, "destroys": 0, "rekeys": 0,
                                        "cache_size": 0, "created_log": []})
    stat["creates"] += self.widget_stats.get("rows_created", 0) - before_created
    stat["destroys"] += self.widget_stats.get("rows_destroyed", 0) - before_destroyed
    stat["cache_size"] = len(cache)

    truly_new_keys = set(cache.keys()) - set(before_ids.keys())
    for k in truly_new_keys:
        ref_id = id(cache[k])
        if ref_id in before_ids.values():
            stat["rekeys"] += 1  # same widgets, relabeled - the CORRECT path for an upgraded identity
        else:
            spec = next((s for s in specs if s[0] == k), None)
            legacy = spec[3] if spec and len(spec) > 3 else None
            stat["created_log"].append({"t_min": (time.time() - START_WALL) / 60.0 if START_WALL else 0.0,
                                        "key": str(k)[:90], "legacy": str(legacy)[:90] if legacy else None})
    return result


appmod.App._sync_rows = _wrapped_sync_rows

report_check_firings = []
_orig_check_due_reports = appmod.App._check_due_reports


def _wrapped_check_due_reports(self):
    report_check_firings.append(time.time())
    return _orig_check_due_reports(self)


appmod.App._check_due_reports = _wrapped_check_due_reports

from app import App, DATA_DIR, TELEMETRY_DB_PATH  # noqa: E402

assert str(DATA_DIR) == str(SANDBOX_DIR), f"FAIL: DATA_DIR did not redirect: {DATA_DIR}"
print(f"=== sandboxed to {DATA_DIR} - production stores cannot be touched by this run", flush=True)

SELF_PID = os.getpid()
START_WALL = None


def query_self():
    cmd = (f"Get-Process -Id {SELF_PID} | Select-Object WorkingSet64,PrivateMemorySize64,"
          "HandleCount | ConvertTo-Json")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                        capture_output=True, text=True, timeout=15).stdout
    d = json.loads(out)
    return d["WorkingSet64"] / 1048576.0, d["PrivateMemorySize64"] / 1048576.0, d["HandleCount"]


# ---------------------------------------------------------------------------
# Item 1/2: recursive widget inventory, by class and by dashboard container, plus per-widget path
# identity for diffing between intervals.
# ---------------------------------------------------------------------------
def widget_inventory(root_widget):
    """[(path_str, class_name, widget_obj), ...] for every widget in the tree, root included."""
    out = [(str(root_widget), type(root_widget).__name__, root_widget)]
    for child in root_widget.winfo_children():
        out.extend(widget_inventory(child))
    return out


def build_container_map(app):
    """{container_name: widget} for every named region the report groups by. Membership is
    decided by path PREFIX match against each container's own Tk path - cheap and exact, since Tk
    paths are hierarchical strings."""
    containers = {
        "main_cards": app.cards, "chart_area": app.chart, "event_log": app.log_body,
        "fans_panel": app.fan_panel.body, "voltages_panel": app.volt_panel.body,
        "drive_temps_panel": app.disk_panel.body, "gpu_thermal_panel": app.gpu_thermal_panel.body,
        "motherboard_panel": app.mobo_panel.body, "ram_panel": app.ram_panel.body,
    }
    return {name: str(widget) for name, widget in containers.items()}


def classify_container(path, container_paths):
    for name, prefix in container_paths.items():
        if path == prefix or path.startswith(prefix + "."):
            return name
    return "other/unclassified"


errors = []
application = App()
application.report_callback_exception = lambda *a: errors.append(a)
START_WALL = time.time()

panel_name_by_cache_id.update({
    id(application.fan_rows): "fans_panel", id(application.volt_rows): "voltages_panel",
    id(application.disk_rows): "drive_temps_panel", id(application.gpu_thermal_rows): "gpu_thermal_panel",
    id(application.mobo_rows): "motherboard_panel", id(application.ram_rows): "ram_panel",
})

state = {"ticks": 0, "prev_paths": None, "samples": []}


def tk_command_names():
    """Every currently-registered Tcl command - includes every live widget's own command name AND
    every CallWrapper-registered callback (bind/after/trace targets) AND Tcl/Tk's fixed built-ins.
    Widget commands are Tk paths (start with '.'); everything else is a 'loose' registration -
    exactly the category the _bind_click bug fell into. If loose commands grow while widget count
    stays flat, that's direct evidence of a leak below anything Python's gc can see."""
    raw = application.tk.eval("info commands")
    return raw.split()


def tick():
    state["ticks"] += 1
    now = time.time()
    t_min = (now - START_WALL) / 60.0

    ws_mb, priv_mb, handles = query_self()

    inventory = widget_inventory(application)
    container_paths = build_container_map(application)
    class_counts = {}
    container_counts = {}
    current_paths = set()
    for path, cls, _w in inventory:
        current_paths.add(path)
        class_counts[cls] = class_counts.get(cls, 0) + 1
        container = classify_container(path, container_paths)
        container_counts[container] = container_counts.get(container, 0) + 1

    gc.collect()
    py_widget_count = sum(1 for o in gc.get_objects() if isinstance(o, tkinter.BaseWidget))
    tcl_commands = tk_command_names()
    widget_commands = [c for c in tcl_commands if c.startswith(".")]
    loose_commands = [c for c in tcl_commands if not c.startswith(".")]
    after_pending = len(application.tk.call("after", "info"))

    new_paths = current_paths - state["prev_paths"] if state["prev_paths"] is not None else set()
    gone_paths = state["prev_paths"] - current_paths if state["prev_paths"] is not None else set()
    state["prev_paths"] = current_paths

    row = {"t_min": t_min, "ws_mb": ws_mb, "priv_mb": priv_mb, "handles": handles,
          "widget_total": len(inventory), "py_widget_count": py_widget_count,
          "tcl_command_total": len(tcl_commands), "widget_commands": len(widget_commands),
          "loose_commands": len(loose_commands), "after_pending": after_pending,
          "class_counts": class_counts, "container_counts": container_counts,
          "new_path_count": len(new_paths), "gone_path_count": len(gone_paths)}
    state["samples"].append(row)

    print(f"\nSAMPLE t={t_min:6.1f}min  ws={ws_mb:7.1f}MB priv={priv_mb:7.1f}MB handles={handles:4d}", flush=True)
    print(f"  widgets: total={row['widget_total']} py_tracked={py_widget_count} "
          f"tcl_commands={row['tcl_command_total']} (widget_cmds={len(widget_commands)} "
          f"loose_cmds={len(loose_commands)}) after_pending={after_pending}", flush=True)
    print(f"  by_class: " + ", ".join(f"{k}={v}" for k, v in sorted(class_counts.items(), key=lambda kv: -kv[1])),
          flush=True)
    print(f"  by_container: " + ", ".join(f"{k}={v}" for k, v in sorted(container_counts.items(), key=lambda kv: -kv[1])),
          flush=True)
    if new_paths:
        print(f"  +{len(new_paths)} NEW widget path(s):", flush=True)
        for p in sorted(new_paths)[:15]:
            print(f"      + {p}", flush=True)
    if gone_paths:
        print(f"  -{len(gone_paths)} widget path(s) VANISHED:", flush=True)
        for p in sorted(gone_paths)[:15]:
            print(f"      - {p}", flush=True)

    for panel, stat in sync_stats.items():
        if stat["created_log"]:
            print(f"  _sync_rows[{panel}]: creates={stat['creates']} destroys={stat['destroys']} "
                  f"rekeys={stat['rekeys']} cache_size={stat['cache_size']}", flush=True)
            for entry in stat["created_log"][-5:]:
                print(f"      CREATE t={entry['t_min']:.1f}min key={entry['key']} legacy={entry['legacy']}",
                      flush=True)

    if t_min >= RUN_MINUTES:
        finish()
    else:
        application.after(SAMPLE_EVERY_S * 1000, tick)


def finish():
    samples = state["samples"]
    first, last = samples[0], samples[-1]
    elapsed = last["t_min"] - first["t_min"]

    print("\n\n---- LOCALIZATION RUN SUMMARY ----", flush=True)
    print(f"DURATION_MIN={elapsed:.1f}  SAMPLES={len(samples)}  TK_ERRORS={len(errors)}", flush=True)
    for e in errors[:5]:
        print(e, flush=True)

    print(f"\nDELTA priv_mb: {first['priv_mb']:.1f} -> {last['priv_mb']:.1f}  "
          f"({last['priv_mb'] - first['priv_mb']:+.1f}, {(last['priv_mb'] - first['priv_mb']) / elapsed:.3f} MB/min)",
          flush=True)
    print(f"DELTA widget_total: {first['widget_total']} -> {last['widget_total']} "
          f"({last['widget_total'] - first['widget_total']:+d})", flush=True)
    print(f"DELTA py_widget_count: {first['py_widget_count']} -> {last['py_widget_count']} "
          f"({last['py_widget_count'] - first['py_widget_count']:+d})", flush=True)
    print(f"DELTA tcl_command_total: {first['tcl_command_total']} -> {last['tcl_command_total']} "
          f"({last['tcl_command_total'] - first['tcl_command_total']:+d})", flush=True)
    print(f"DELTA widget_commands: {first['widget_commands']} -> {last['widget_commands']} "
          f"({last['widget_commands'] - first['widget_commands']:+d})", flush=True)
    print(f"DELTA loose_commands: {first['loose_commands']} -> {last['loose_commands']} "
          f"({last['loose_commands'] - first['loose_commands']:+d})", flush=True)
    print(f"DELTA after_pending: {first['after_pending']} -> {last['after_pending']} "
          f"({last['after_pending'] - first['after_pending']:+d})", flush=True)

    print("\n---- BY CLASS: first vs last ----", flush=True)
    all_classes = set(first["class_counts"]) | set(last["class_counts"])
    for cls in sorted(all_classes, key=lambda c: -(last["class_counts"].get(c, 0) - first["class_counts"].get(c, 0))):
        f0, l0 = first["class_counts"].get(cls, 0), last["class_counts"].get(cls, 0)
        if f0 != l0:
            print(f"  {cls:20s} {f0:5d} -> {l0:5d}  ({l0 - f0:+d})", flush=True)

    print("\n---- BY CONTAINER: first vs last ----", flush=True)
    all_containers = set(first["container_counts"]) | set(last["container_counts"])
    for name in sorted(all_containers, key=lambda c: -(last["container_counts"].get(c, 0) - first["container_counts"].get(c, 0))):
        f0, l0 = first["container_counts"].get(name, 0), last["container_counts"].get(name, 0)
        print(f"  {name:22s} {f0:5d} -> {l0:5d}  ({l0 - f0:+d})", flush=True)

    print("\n---- _sync_rows TOTALS ----", flush=True)
    for panel, stat in sync_stats.items():
        print(f"  {panel:22s} creates={stat['creates']:4d} destroys={stat['destroys']:4d} "
              f"rekeys={stat['rekeys']:4d} final_cache_size={stat['cache_size']:4d}", flush=True)
        for entry in stat["created_log"]:
            print(f"      CREATE t={entry['t_min']:.1f}min key={entry['key']} legacy={entry['legacy']}", flush=True)

    print(f"\nREPORT_DUE_CHECK_FIRINGS={[f'{(t - START_WALL) / 60.0:.1f}' for t in report_check_firings]}",
          flush=True)
    print(f"\nSANDBOX_DIR={SANDBOX_DIR}", flush=True)
    application.stop_event.set()
    application.destroy()


application.after(15000, tick)
application.mainloop()
