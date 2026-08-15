"""Verification for Per-App Thermal Analytics: pure aggregation-helper correctness
(canonical workload identity, no-fuzzy-merge, missing-value semantics, gap-aware duration
aggregation, all 8 ranking modes, date filtering), AnalyticsWindow GUI wiring, the VIEW
INCIDENTS drill-down into the existing History viewer, old/minimal-schema crash-safety, and
that analytics never runs on the live telemetry poll."""
import inspect
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (App, AnalyticsWindow, HistoryWindow,  # noqa: E402
                 canonical_workload_name, group_incidents_by_workload, filter_incidents_by_range,
                 compute_workload_stats, rank_workloads, RANK_MODES, NOT_IDENTIFIED_KEY, NOT_IDENTIFIED_DISPLAY)

def inc(now, incident_id, component, peak, max_zone, workload, end_offset, duration, duration_exact=True,
       context_peak=None, **extra):
    d = {
        "incident_id": incident_id, "start_timestamp": now - end_offset - duration,
        "end_timestamp": now - end_offset, "duration_seconds": duration, "duration_exact": duration_exact,
        "component": component, "sensor_name": component, "sensor_identifier": f"/synthetic/{component}",
        "starting_zone": "YELLOW", "max_zone": max_zone, "start_value": peak - 5, "peak_value": peak,
        "recovery_value": peak - 20, "dominant_workload": workload, "context_peak": context_peak or {},
    }
    d.update(extra)
    return d


def minimal_old_incident(now):
    """Pre-workload-attribution incident: no dominant_workload/context_peak keys at all."""
    return {"incident_id": "cpu-old-1", "start_timestamp": now - 560, "end_timestamp": now - 500,
           "duration_seconds": 60.0, "component": "cpu", "sensor_name": "CPU Package",
           "max_zone": "YELLOW", "starting_zone": "YELLOW", "peak_value": 82.0, "recovery_value": 60.0}


def build_fixtures(now):
    """All timestamps are anchored to `now` (real wall-clock time at test start) rather than a
    fixed epoch, so both the pure-function assertions (which pass now= explicitly) and the live
    AnalyticsWindow GUI path (which reads real time.time() internally) agree on what's in-range."""
    cy1 = inc(now, "cy1", "cpu", 92, "ORANGE", "Cyberpunk2077.exe", 3600, 300,
             context_peak={"cpu_power": 150.0, "cpu_load": 80.0})
    cy2 = inc(now, "cy2", "gpu_hotspot", 98, "RED", "cyberpunk2077.exe", 90000, 600,
             context_peak={"gpu_power": 350.0, "gpu_load": 99.0})
    cy3 = inc(now, "cy3", "gpu_core", 85, "YELLOW", "Cyberpunk2077.exe", 40 * 86400, 120)
    cy_gap = inc(now, "cy_gap", "cpu", 90, "ORANGE", "Cyberpunk2077.exe", 1000, 400, duration_exact=False)

    bl1 = inc(now, "bl1", "gpu_core", 78, "YELLOW", "blender.exe", 5000, 200)
    bl2 = inc(now, "bl2", "ram", 82, "YELLOW", "blender.exe", 6000, 150)
    bl3 = inc(now, "bl3", "gpu_vram", 88, "YELLOW", "blender.exe", 7000, 50)

    py1 = inc(now, "py1", "cpu", 84, "YELLOW", "python.exe", 2000, 100)

    old_min = minimal_old_incident(now)
    not_id = inc(now, "ni1", "drive", 65, "YELLOW", "Not identified", 800, 90)

    return [cy1, cy2, cy3, cy_gap, bl1, bl2, bl3, py1, old_min, not_id]


def main():
    now = time.time()
    incidents = build_fixtures(now)

    print("=== 1. canonical_workload_name: case-insensitive merge, no fuzzy-merge ===")
    assert canonical_workload_name(incidents[0]) == ("cyberpunk2077.exe", "Cyberpunk2077.exe")
    assert canonical_workload_name(incidents[1]) == ("cyberpunk2077.exe", "cyberpunk2077.exe")
    assert canonical_workload_name(incidents[1])[0] == canonical_workload_name(incidents[0])[0], \
        "FAIL: case-variant executable names must aggregate to the same key"
    py_key, py_display = canonical_workload_name(incidents[7])
    assert py_key == "python.exe" and py_display == "python.exe", \
        "FAIL: python.exe must never be fuzzy-merged into another app's identity"
    assert canonical_workload_name(minimal_old_incident(now)) == (NOT_IDENTIFIED_KEY, NOT_IDENTIFIED_DISPLAY)
    assert canonical_workload_name(incidents[9]) == (NOT_IDENTIFIED_KEY, NOT_IDENTIFIED_DISPLAY)
    print("  PASS: case merges, unrelated executables stay separate, missing/explicit 'Not identified' unify")

    print("\n=== 2. group_incidents_by_workload: correct membership counts, insertion order preserved ===")
    groups = group_incidents_by_workload(incidents)
    assert list(groups.keys()) == ["cyberpunk2077.exe", "blender.exe", "python.exe", NOT_IDENTIFIED_KEY]
    assert len(groups["cyberpunk2077.exe"]["incidents"]) == 4
    assert len(groups["blender.exe"]["incidents"]) == 3
    assert len(groups["python.exe"]["incidents"]) == 1
    assert len(groups[NOT_IDENTIFIED_KEY]["incidents"]) == 2
    print("  PASS: 4/3/1/2 incidents grouped correctly, first-seen order preserved")

    print("\n=== 3. compute_workload_stats: missing-value, avg/max, critical count, gap exclusion ===")
    cy_stats = compute_workload_stats("cyberpunk2077.exe", "Cyberpunk2077.exe", groups["cyberpunk2077.exe"]["incidents"], now=now)
    assert cy_stats["total_incidents"] == 4
    assert cy_stats["critical_count"] == 1, "FAIL: exactly one RED (cy2) incident expected"
    assert cy_stats["components"]["ram"] is None, "FAIL: missing measurement must stay None, never 0"
    assert cy_stats["components"]["cpu"]["count"] == 2
    assert cy_stats["components"]["cpu"]["avg_peak"] == 91.0, "FAIL: avg must average only incidents WITH that measurement"
    assert cy_stats["components"]["cpu"]["max_peak"] == 92.0
    assert cy_stats["components"]["gpu_hotspot"]["max_peak"] == 98.0
    assert cy_stats["gap_incident_count"] == 1
    assert cy_stats["exact_duration_incident_count"] == 3, "FAIL: gap incident must be excluded from exact-duration set"
    assert cy_stats["total_duration_seconds"] == 300 + 600 + 120, "FAIL: gap incident's duration must not be summed"
    assert cy_stats["context"]["cpu_power"]["max_peak"] == 150.0
    assert cy_stats["context"]["mem_pct"] is None
    print("  PASS: None-for-missing, avg/max correct, critical count correct, gap incident excluded from exact duration")

    bl_stats = compute_workload_stats("blender.exe", "blender.exe", groups["blender.exe"]["incidents"], now=now)
    assert bl_stats["components"]["cpu"] is None
    assert bl_stats["components"]["gpu_vram"]["max_peak"] == 88.0
    assert bl_stats["total_duration_seconds"] == 400

    old_stats = compute_workload_stats(NOT_IDENTIFIED_KEY, NOT_IDENTIFIED_DISPLAY,
                                       groups[NOT_IDENTIFIED_KEY]["incidents"], now=now)
    assert old_stats["total_incidents"] == 2
    assert old_stats["exact_duration_incident_count"] == 2, \
        "FAIL: an old incident with no duration_exact field must default to exact, not be dropped"
    assert old_stats["components"]["cpu"]["max_peak"] == 82.0
    assert old_stats["components"]["drive"]["max_peak"] == 65.0
    print("  PASS: old/minimal-schema incident (no dominant_workload/context_peak/duration_exact) computes cleanly, no crash")

    print("\n=== 4. filter_incidents_by_range: date filtering removes only what's out of range ===")
    all_stats_full = [compute_workload_stats(k, g["display_name"], g["incidents"], now=now)
                      for k, g in groups.items()]
    filtered_30d = filter_incidents_by_range(incidents, 30 * 86400, now=now)
    assert len(filtered_30d) == 9, "FAIL: only cy3 (40 days old) should be excluded by a 30d window"
    assert "cy3" not in {i["incident_id"] for i in filtered_30d}
    groups_30d = group_incidents_by_workload(filtered_30d)
    cy_stats_30d = compute_workload_stats("cyberpunk2077.exe", "Cyberpunk2077.exe",
                                          groups_30d["cyberpunk2077.exe"]["incidents"], now=now)
    assert cy_stats_30d["total_incidents"] == 3
    assert cy_stats_30d["components"]["gpu_core"] is None, \
        "FAIL: gpu_core stat should disappear once its only contributing incident (cy3) is filtered out"
    assert cy_stats_30d["total_duration_seconds"] == 900
    assert filter_incidents_by_range(incidents, None, now=now) == incidents, "FAIL: 'All' range must not filter"
    print("  PASS: date range filtering changes membership and downstream stats correctly")

    print("\n=== 5. rank_workloads: every one of the 8 modes, missing-values-sort-last ===")
    expected_orders = {
        "most_incidents": ["cyberpunk2077.exe", "blender.exe", NOT_IDENTIFIED_KEY, "python.exe"],
        "most_critical": ["cyberpunk2077.exe", "blender.exe", "python.exe", NOT_IDENTIFIED_KEY],
        "cpu_peak": ["cyberpunk2077.exe", "python.exe", NOT_IDENTIFIED_KEY, "blender.exe"],
        "gpu_core_peak": ["cyberpunk2077.exe", "blender.exe", "python.exe", NOT_IDENTIFIED_KEY],
        "gpu_hotspot_peak": ["cyberpunk2077.exe", "blender.exe", "python.exe", NOT_IDENTIFIED_KEY],
        "gpu_vram_peak": ["blender.exe", "cyberpunk2077.exe", "python.exe", NOT_IDENTIFIED_KEY],
        "longest_thermal_time": ["cyberpunk2077.exe", "blender.exe", NOT_IDENTIFIED_KEY, "python.exe"],
        "most_recent": [NOT_IDENTIFIED_KEY, "cyberpunk2077.exe", "python.exe", "blender.exe"],
    }
    assert set(expected_orders) == {mode for _label, mode in RANK_MODES}, "FAIL: test doesn't cover all rank modes"
    for mode, expected_keys in expected_orders.items():
        ranked = rank_workloads(all_stats_full, mode)
        got_keys = [s["workload_key"] for s in ranked]
        assert got_keys == expected_keys, f"FAIL mode={mode}: expected {expected_keys}, got {got_keys}"
        print(f"  {mode:22s} -> {got_keys}  PASS")
    print("  PASS: all 8 ranking modes deterministic and correct, missing values sort last")

    print("\n=== 6. AnalyticsWindow GUI: table population, detail text, VIEW INCIDENTS drill-down ===")
    # Session tracking (a later task) added a SESSIONS column between WORKLOAD and INCIDENTS -
    # mocked to an empty list here since this file only exercises incident-based analytics.
    with mock.patch("app.read_incidents_file", return_value=incidents), \
        mock.patch("app.read_sessions_file", return_value=[]):
        app = App()
        win = AnalyticsWindow(app)
        win.range_var.set("All")
        win.rank_var.set("Most incidents")
        win._recompute()
        rows = [win.tree.item(iid)["values"] for iid in win.tree.get_children()]
        # columns: workload, sessions, incidents, critical, worst
        assert rows[0][0] == "Cyberpunk2077.exe" and rows[0][2] == 4 and rows[0][3] == 1, \
            f"FAIL: top ranking table row wrong: {rows[0]}"
        print(f"  table (Most incidents): {rows}")

        first_iid = win.tree.get_children()[0]
        win.tree.selection_set(first_iid)
        win._on_select(None)
        assert "CYBERPUNK2077.EXE" in win.detail_text.cget("text")
        assert "Associated incidents" in win.detail_text.cget("text")
        assert "caused" not in win.detail_text.cget("text").lower(), \
            "FAIL: detail text must never use causal language"
        print("  PASS: detail panel populated with non-causal 'Associated incidents' language")

        win._view_incidents()
        hw = app.history_window
        assert hw is not None and hw.winfo_exists(), "FAIL: VIEW INCIDENTS must reuse History, not build a new viewer"
        assert {i["incident_id"] for i in hw.filtered} == {"cy1", "cy2", "cy3", "cy_gap"}, \
            f"FAIL: drill-down should show exactly Cyberpunk's 4 incidents, got {[i['incident_id'] for i in hw.filtered]}"
        print("  PASS: VIEW INCIDENTS drove History's own existing workload filter to exactly the right 4 incidents")

        # range filter narrows the drill-down set consistently with the analytics stat it came from
        win.range_var.set("30d")
        win._recompute()
        cy_row = next(r for r in (win.tree.item(i)["values"] for i in win.tree.get_children()) if r[0] == "Cyberpunk2077.exe")
        assert cy_row[2] == 3, f"FAIL: 30d-ranged table row should show 3 incidents, got {cy_row}"
        print("  PASS: RANGE control re-narrows the ranking table (cy3 dropped at 30d)")

        win.destroy()
        app.stop_event.set()
        app.destroy()

    print("\n=== 7. analytics never runs on the live telemetry poll ===")
    src = inspect.getsource(App.update_data)
    for forbidden in ("compute_workload_stats", "rank_workloads", "group_incidents_by_workload", "AnalyticsWindow"):
        assert forbidden not in src, f"FAIL: update_data() must never call {forbidden} on the 2s poll"
    print("  PASS: update_data() contains no analytics computation - Analytics computes only on open/filter/rank change")

    print("\nALL PER-APP ANALYTICS CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
