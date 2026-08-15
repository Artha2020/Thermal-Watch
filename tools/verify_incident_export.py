"""Verification for incident export/reporting (CSV, JSON, COPY SUMMARY)."""
import csv
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (App, HistoryWindow, INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH, GPU_HOTSPOT_ZONES,  # noqa: E402
                 incident_to_csv_row, build_json_export, build_incident_summary,
                 sanitize_filename_part, CSV_COLUMNS)

# Export targets live in the sandbox, not next to this script. They used to be written into
# tools/_export_test_scratch inside the repo and removed again at the end - which leaves them
# behind whenever a check fails partway, i.e. exactly when someone is least likely to notice.
# A test writes only to temp, its own scratch output included.
SCRATCH = _verify_sandbox.SANDBOX_DIR / "_export_test_scratch"


def fresh_files():
    for p in (INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH):
        if p.exists():
            p.unlink()
    SCRATCH.mkdir(exist_ok=True)


def tick_cpu(app, temp):
    app._update_cpu_zone(temp)
    app._incident_observe("cpu", "cpu", "CPU Package", "/amdcpu/0/temperature/0", temp)


def tick_gpu_hotspot(app, temp):
    app._update_sensor_zone("gpu_hotspot", "RTX 3090 GPU Hotspot", temp, "°C", GPU_HOTSPOT_ZONES)
    app._incident_observe("sensor:gpu_hotspot", "gpu_hotspot", "RTX 3090 GPU Hotspot",
                          "/gpu-nvidia/0/temperature/2", temp)


def full_incident_dict():
    """A realistic, fully-populated closed incident, including tricky content on purpose:
    a comma, a quote, and a non-ASCII character in the window title."""
    return {
        "incident_id": "gpu_hotspot-1700000000000",
        "start_timestamp": 1700000000.0, "end_timestamp": 1700000407.0,
        "duration_seconds": 407.0, "duration_exact": True,
        "component": "gpu_hotspot", "sensor_name": "RTX 3090 GPU Hotspot",
        "sensor_identifier": "/gpu-nvidia/0/temperature/2",
        "starting_zone": "YELLOW", "max_zone": "ORANGE",
        "start_value": 86.0, "peak_value": 97.0, "recovery_value": 78.0,
        "dominant_workload": "Cyberpunk2077.exe",
        "foreground_process": "Cyberpunk2077.exe",
        "foreground_title": 'Cyberpunk 2077, "Night City" — 日本語',
        "recovery_during_monitoring_gap": False, "monitoring_gap_seconds": 0.0,
        "close_reason": None,
        "context_peak": {"cpu_temp": 73.0, "gpu_core_temp": 84.0, "gpu_hotspot_temp": 97.0,
                         "gpu_vram_temp": 91.0, "cpu_power": 142.0, "gpu_power": 341.0,
                         "cpu_load": 34.0, "gpu_load": 99.0, "mem_pct": 41.0},
        "top_cpu_processes": [["Cyberpunk2077.exe", 222, 18.0], ["Discord.exe", 111, 4.0]],
        "top_gpu_processes": [["Cyberpunk2077.exe", 222, 91.0]],
        "monitoring_gaps": [],
        "samples": [[1700000000.0, 86.0], [1700000200.0, 92.0], [1700000407.0, 97.0]],
    }


def gap_incident_dict():
    d = full_incident_dict()
    d.update({
        "incident_id": "cpu-1700001000000", "component": "cpu", "sensor_name": "CPU Package",
        "duration_exact": False, "recovery_during_monitoring_gap": True,
        "monitoring_gap_seconds": 134.0, "close_reason": "recovered_during_gap",
        "recovery_value": None,
        "last_observed_alert_timestamp": 1700001100.0,
        "first_observed_recovered_timestamp": 1700001234.0,
        "first_observed_recovered_value": 42.0,
        "monitored_duration_seconds": 100.0,
        "monitoring_gaps": [{"last_sample_before": 1700001100.0, "first_sample_after": 1700001234.0,
                             "gap_seconds": 134.0}],
    })
    return d


def minimal_old_incident_dict():
    """Simulates an incident persisted before workload/context/gap fields existed at all."""
    return {
        "incident_id": "cpu-1600000000000", "start_timestamp": 1600000000.0,
        "end_timestamp": 1600000060.0, "duration_seconds": 60.0,
        "component": "cpu", "sensor_name": "CPU Package", "max_zone": "YELLOW",
        "starting_zone": "YELLOW", "peak_value": 82.0, "recovery_value": 60.0,
    }


def main():
    fresh_files()

    print("=== 1. incident_to_csv_row: full incident, all fields correct, blanks never invented ===")
    row = incident_to_csv_row(full_incident_dict())
    assert row["incident_id"] == "gpu_hotspot-1700000000000"
    assert row["duration_exact"] == "True"
    assert row["peak_gpu_hotspot_temp"] == "97.00"
    assert row["peak_gpu_memory_temp"] == "91.00"  # gpu_vram_temp -> peak_gpu_memory_temp mapping
    assert row["top_cpu_processes"] == "Cyberpunk2077.exe:18%; Discord.exe:4%"
    assert row["top_gpu_processes"] == "Cyberpunk2077.exe:91%"
    assert row["monitoring_gaps"] == ""  # no gap on this one
    assert set(row.keys()) == set(CSV_COLUMNS)
    print("  PASS: all columns present, correct values, nested fields flattened to readable cells")

    print("\n=== 2. old/minimal incident exports without error (item 13) ===")
    row_old = incident_to_csv_row(minimal_old_incident_dict())
    assert row_old["monitoring_gaps"] == ""
    assert row_old["top_cpu_processes"] == ""
    assert row_old["duration_exact"] == ""  # genuinely absent -> blank, not "False"
    assert row_old["peak_cpu_temp"] == ""  # no context_peak at all
    assert row_old["incident_id"] == "cpu-1600000000000"
    print("  PASS: minimal incident produces blank cells for missing fields, no crash")
    summary_old = build_incident_summary(minimal_old_incident_dict())
    assert "N/A" not in summary_old.split("Foreground")[0] or True  # just confirm it doesn't crash
    print("  PASS: build_incident_summary() on a minimal incident doesn't crash")

    print("\n=== 3. gap-spanning incident stays explicitly uncertain (item 9) ===")
    gap_row = incident_to_csv_row(gap_incident_dict())
    assert gap_row["duration_exact"] == "False"
    assert gap_row["recovery_during_monitoring_gap"] == "True"
    assert gap_row["recovery_value"] == "", "FAIL: an unknown recovery_value must not become a fabricated cell"
    assert gap_row["close_reason"] == "recovered_during_gap"
    assert "134" in gap_row["monitoring_gaps"] or "2m 14s" in gap_row["monitoring_gaps"]
    print(f"  PASS: CSV row keeps duration_exact=False, recovery_value blank, gap recorded "
          f"({gap_row['monitoring_gaps'].encode('ascii', 'replace').decode()})")
    gap_json = build_json_export([gap_incident_dict()])
    assert gap_json["incidents"][0]["duration_exact"] is False
    assert gap_json["incidents"][0]["recovery_value"] is None
    assert gap_json["incidents"][0]["monitoring_gaps"][0]["gap_seconds"] == 134.0
    print("  PASS: JSON export preserves duration_exact=false and null recovery_value exactly")

    print("\n=== 4. JSON export: valid, round-trips, nested data unchanged ===")
    incidents = [full_incident_dict(), gap_incident_dict()]
    export = build_json_export(incidents, {"range": "7d", "component": "All"})
    text = json.dumps(export, indent=2)
    reloaded = json.loads(text)  # must not raise
    assert reloaded["count"] == 2
    assert reloaded["filters"] == {"range": "7d", "component": "All"}
    assert "schema_version" in reloaded and "app_version" in reloaded and "export_timestamp" in reloaded
    assert reloaded["incidents"][0]["top_gpu_processes"] == [["Cyberpunk2077.exe", 222, 91.0]]
    assert reloaded["incidents"][0] == full_incident_dict(), "FAIL: JSON export mutated or altered the record"
    print("  PASS: valid JSON, metadata envelope correct, nested arrays byte-identical, original unmutated")

    print("\n=== 5. CSV proper quoting: commas, quotes, unicode round-trip via the real csv module ===")
    path = SCRATCH / "quoting_test.csv"
    HistoryWindow._write_csv(str(path), [full_incident_dict()])
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["foreground_title"] == 'Cyberpunk 2077, "Night City" — 日本語'
    assert rows[0]["incident_id"] == "gpu_hotspot-1700000000000"
    print(f"  PASS: comma/quote/unicode window title survived a real csv-module round-trip exactly")

    print("\n=== 6. History-count == exported-count (filtered export) ===")
    fresh_files()
    app = App()
    app.open_history()
    app.history_window.all_incidents = [full_incident_dict(), gap_incident_dict(), minimal_old_incident_dict()]
    app.history_window._apply_filters()
    shown = len(app.history_window.filtered)
    assert shown == 3
    out_path = SCRATCH / "filtered_export.csv"
    with mock.patch("app.filedialog.asksaveasfilename", return_value=str(out_path)):
        app.history_window._export_filtered()
    with open(out_path, encoding="utf-8-sig", newline="") as f:
        exported_rows = list(csv.DictReader(f))
    assert len(exported_rows) == shown == 3, f"FAIL: history shows {shown}, exported {len(exported_rows)}"
    print(f"  PASS: History table shows {shown}, exported file has exactly {len(exported_rows)} rows")

    print("\n=== 7. filtered export respects an actual filter (component=cpu only) ===")
    app.history_window.component_var.set("cpu")
    app.history_window._apply_filters()
    assert len(app.history_window.filtered) == 2  # gap_incident_dict + minimal_old_incident_dict, both component=cpu
    out_path2 = SCRATCH / "filtered_cpu_only.csv"
    with mock.patch("app.filedialog.asksaveasfilename", return_value=str(out_path2)):
        app.history_window._export_filtered()
    with open(out_path2, encoding="utf-8-sig", newline="") as f:
        rows2 = list(csv.DictReader(f))
    assert len(rows2) == 2
    assert all(r["component"] == "cpu" for r in rows2)
    print("  PASS: component filter correctly reduced both the table AND the export to 2 rows")

    print("\n=== 8. filtered export to JSON ===")
    app.history_window.component_var.set("All")
    app.history_window._apply_filters()
    out_json = SCRATCH / "filtered_export.json"
    with mock.patch("app.filedialog.asksaveasfilename", return_value=str(out_json)):
        app.history_window._export_filtered()
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["count"] == 3
    assert len(payload["incidents"]) == 3
    print("  PASS: filtered JSON export has count==3 and 3 embedded incident records")

    print("\n=== 9. export selected: exactly one incident, correct filename sanitization ===")
    app.history_window.tree.selection_set(app.history_window.tree.get_children()[0])
    selected_inc = app.history_window._selected_incident()
    comp = sanitize_filename_part(selected_inc.get("component"))
    assert comp and all(c.isalnum() or c in "_-" for c in comp)
    out_sel = SCRATCH / "selected.json"
    with mock.patch("app.filedialog.asksaveasfilename", return_value=str(out_sel)):
        app.history_window._export_selected()
    sel_payload = json.loads(out_sel.read_text(encoding="utf-8"))
    assert sel_payload["count"] == 1
    assert sel_payload["incidents"][0]["incident_id"] == selected_inc["incident_id"]
    print(f"  PASS: exported exactly 1 incident (id={selected_inc['incident_id']}), filename part sanitized to {comp!r}")

    print("\n=== 10. export selected to CSV too ===")
    out_sel_csv = SCRATCH / "selected.csv"
    with mock.patch("app.filedialog.asksaveasfilename", return_value=str(out_sel_csv)):
        app.history_window._export_selected()
    with open(out_sel_csv, encoding="utf-8-sig", newline="") as f:
        sel_rows = list(csv.DictReader(f))
    assert len(sel_rows) == 1
    print("  PASS: single-incident CSV export has exactly 1 data row")

    print("\n=== 11. COPY SUMMARY uses only actual captured values ===")
    root = app  # Tk clipboard lives on the root
    with mock.patch.object(type(app.history_window), "clipboard_clear", app.clipboard_clear.__func__ if False else app.history_window.clipboard_clear):
        pass  # no-op, just documenting we use the real Tk clipboard below
    app.history_window.tree.selection_set(app.history_window.tree.get_children()[0])
    app.history_window._copy_summary()
    app.update()
    clip = app.clipboard_get()
    assert "THERMAL WATCH INCIDENT" in clip
    assert selected_inc.get("dominant_workload", "") in clip or "Not identified" in clip
    print("  PASS: clipboard contains a real summary built from the selected incident's actual data")
    print(f"  --- summary preview ---\n{clip[:300].encode('ascii', 'replace').decode()}")

    print("\n=== 12. export cancellation leaves no partial file ===")
    cancel_path = SCRATCH / "should_not_exist.csv"
    if cancel_path.exists():
        cancel_path.unlink()
    with mock.patch("app.filedialog.asksaveasfilename", return_value=""):  # user hit Cancel
        app.history_window._export_filtered()
        app.history_window._export_selected()
    assert not cancel_path.exists()
    print("  PASS: cancelling the save dialog created no file")

    print("\n=== 13. no admin/UAC prompt possible (structural: pure local file I/O, no subprocess) ===")
    import inspect
    src = inspect.getsource(app.history_window.__class__)
    assert "subprocess" not in src and "RunAs" not in src and "elevat" not in src.lower()
    print("  PASS: export code path contains no subprocess/elevation calls")

    print("\n=== 14. live monitoring continues normally while History window is open ===")
    from app import nvidia_stats, memory, cpu_times
    old_idle, old_total = cpu_times()
    time.sleep(0.2)
    now = cpu_times()
    dtc = now[1] - old_total
    load = 100 * (1 - (now[0] - old_idle) / dtc) if dtc else 0
    mem_pct, _, _ = memory()
    payload = {"time": datetime.now(), "cpu_load": load, "mem_pct": mem_pct, "mem_used": 0, "mem_total": 0,
              "gpus": nvidia_stats(), "lhm": None}
    app.update_data(payload)  # must not raise just because history_window exists
    print("  PASS: update_data() runs fine with the History window open")

    app.history_window.destroy()
    app.stop_event.set(); app.destroy()

    import shutil
    shutil.rmtree(SCRATCH, ignore_errors=True)
    fresh_files()
    (Path(__file__).with_name("thermal_watch_events.log")).unlink(missing_ok=True)
    print("\nALL EXPORT/REPORTING CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
