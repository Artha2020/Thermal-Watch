"""Verification for Baseline Learning: streaming statistics correctness (mean/min/max/sample
stddev, missing-value handling, established-threshold gating), per-workload baseline built from
completed session records, idle-baseline built from telemetry buckets NOT covered by any
session, AnalyticsWindow showing workloads that have sessions but zero incidents (a real
integration bug caught and fixed while building this), SensorHistoryWindow's idle baseline
reusing already-fetched data (no extra history query), and that the whole layer is read-only
over already-persisted incidents/sessions/telemetry - never touching the live thermal/session
engines."""
import inspect
import json
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (App, AnalyticsWindow, SensorHistoryWindow, BASELINE_MIN_SESSIONS,  # noqa: E402
                 BASELINE_MIN_IDLE_BUCKETS, SESSIONS_PATH, open_telemetry_db, TELEMETRY_SCALAR_KEYS,
                 _stat_summary, compute_workload_baseline, filter_idle_buckets, compute_idle_baseline,
                 scalar_sensor_ref, read_telemetry_file, read_sessions_file)


def fresh_files():
    from app import TELEMETRY_DB_PATH, TELEMETRY_JSONL_PATH, INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH, \
        ACTIVE_SESSIONS_PATH
    for p in (TELEMETRY_DB_PATH, TELEMETRY_DB_PATH.with_suffix(".db-wal"), TELEMETRY_DB_PATH.with_suffix(".db-shm"),
             TELEMETRY_JSONL_PATH, SESSIONS_PATH, ACTIVE_SESSIONS_PATH, INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH):
        if p.exists():
            p.unlink()


def session_fixture(sid, workload, start, dur, cpu_avg_temp=60.0, gpu_hotspot_avg=82.0, incident_count=0):
    return {
        "session_id": sid, "workload_key": workload.casefold(), "workload": workload,
        "start_timestamp": start, "end_timestamp": start + dur, "duration_seconds": dur,
        "duration_exact": True,
        "cpu": {"avg_temp": cpu_avg_temp, "peak_temp": cpu_avg_temp + 10, "avg_power": 80.0},
        "gpu": {"avg_core_temp": 65.0, "peak_core_temp": 75.0, "avg_hotspot_temp": gpu_hotspot_avg,
               "peak_hotspot_temp": gpu_hotspot_avg + 4, "avg_vram_temp": 78.0, "peak_vram_temp": 82.0,
               "avg_power": 250.0, "peak_power": 300.0},
        "incident_count": incident_count, "max_incident_severity": None, "incident_ids": [],
        "monitoring_gaps": [],
    }


def bucket_fixture(start, cpu_temp=None):
    scalars = {k: None for k in TELEMETRY_SCALAR_KEYS}
    if cpu_temp is not None:
        scalars["cpu_temp"] = {"avg": cpu_temp, "min": cpu_temp - 2, "max": cpu_temp + 2, "count": 30}
    return {"start_timestamp": start, "end_timestamp": start + 60, "sample_count": 30, "scalars": scalars,
           "sensors": {}}


def main():
    fresh_files()

    print("=== 1. _stat_summary: mean/min/max/sample-stddev correctness ===")
    r = _stat_summary([10.0, 20.0, 30.0])
    assert r["count"] == 3 and r["mean"] == 20.0 and r["min"] == 10.0 and r["max"] == 30.0
    # sample stddev of [10,20,30]: variance = ((10-20)^2+(0)+(10)^2)/(3-1) = 200/2 = 100 -> stddev=10
    assert abs(r["stddev"] - 10.0) < 1e-9, f"FAIL: {r}"
    print(f"  PASS: {r}")

    print("\n=== 2. _stat_summary: missing values excluded, never treated as 0 ===")
    r2 = _stat_summary([10.0, None, None, 30.0])
    assert r2["count"] == 2 and r2["mean"] == 20.0, f"FAIL: None samples affected the result: {r2}"
    print(f"  PASS: {r2} (2 real samples only)")

    print("\n=== 3. _stat_summary: n=1 -> stddev is None (undefined, not 0); n=0 -> None entirely ===")
    r3 = _stat_summary([50.0])
    assert r3["count"] == 1 and r3["stddev"] is None, f"FAIL: {r3}"
    assert _stat_summary([]) is None, "FAIL: zero samples must yield None, never a fabricated baseline"
    assert _stat_summary([None, None]) is None, "FAIL: all-missing must yield None"
    print(f"  PASS: single-sample stddev=None ({r3}), zero/all-missing -> None")

    print("\n=== 4. _stat_summary: established-threshold gating is exact at the boundary ===")
    assert _stat_summary([1.0, 2.0], min_established=3)["established"] is False
    assert _stat_summary([1.0, 2.0, 3.0], min_established=3)["established"] is True
    print("  PASS: 2/3 -> not established, 3/3 -> established (exact boundary)")

    print("\n=== 5. compute_workload_baseline: correct per-metric stats across sessions, missing metric -> None ===")
    sessions = [session_fixture(f"s{i}", "Cyberpunk2077.exe", 1_700_000_000.0 + i * 3600, 1800,
                                cpu_avg_temp=60.0 + i, gpu_hotspot_avg=82.0 + i) for i in range(5)]
    baseline = compute_workload_baseline(sessions)
    cpu_avg = baseline["cpu.avg_temp"]["stats"]
    assert cpu_avg["count"] == 5 and cpu_avg["mean"] == 62.0, f"FAIL: {cpu_avg}"  # 60,61,62,63,64 -> mean 62
    assert cpu_avg["established"] is True
    hotspot_avg = baseline["gpu.avg_hotspot_temp"]["stats"]
    assert hotspot_avg["mean"] == 84.0  # 82..86 -> mean 84
    assert baseline["cpu.avg_temp"]["label"] == "CPU Package (session avg)"
    assert baseline["cpu.avg_temp"]["unit"] == "°C"
    print(f"  PASS: cpu.avg_temp mean={cpu_avg['mean']}, gpu.avg_hotspot_temp mean={hotspot_avg['mean']}")

    print("\n=== 6. compute_workload_baseline: below-threshold sessions still compute REAL numbers, flagged not established ===")
    few_sessions = sessions[:2]  # only 2, BASELINE_MIN_SESSIONS=3
    baseline_few = compute_workload_baseline(few_sessions)
    stats_few = baseline_few["cpu.avg_temp"]["stats"]
    assert stats_few["count"] == 2 and stats_few["established"] is False
    assert stats_few["mean"] == 60.5, f"FAIL: real mean should still be computed: {stats_few}"
    print(f"  PASS: 2 sessions (< {BASELINE_MIN_SESSIONS}) -> real mean={stats_few['mean']}, established=False")

    print("\n=== 7. filter_idle_buckets: buckets overlapping a session excluded, others kept ===")
    now = time.time()
    session_span = [{"start_timestamp": now - 3600, "end_timestamp": now - 1800}]
    buckets = [bucket_fixture(now - 3600 + i * 60) for i in range(60)]  # spans the whole last hour
    idle = filter_idle_buckets(buckets, session_span)
    idle_starts = {b["start_timestamp"] for b in idle}
    active_starts = {b["start_timestamp"] for b in buckets if now - 3600 <= b["start_timestamp"] < now - 1800}
    assert idle_starts.isdisjoint(active_starts), "FAIL: an active-session bucket leaked into 'idle'"
    assert len(idle) == len(buckets) - len(active_starts), \
        f"FAIL: expected {len(buckets) - len(active_starts)} idle buckets, got {len(idle)}"
    print(f"  PASS: {len(active_starts)} active buckets excluded, {len(idle)} idle buckets kept")

    print("\n=== 8. filter_idle_buckets: a session missing end_timestamp is treated as ongoing-to-now (conservative) ===")
    open_session = [{"start_timestamp": now - 600, "end_timestamp": None}]
    recent_buckets = [bucket_fixture(now - 300)]  # falls inside [now-600, now]
    idle2 = filter_idle_buckets(recent_buckets, open_session)
    assert idle2 == [], "FAIL: a bucket inside a still-open session's span must not count as idle"
    print("  PASS: bucket inside an open-ended session's [start, now] span correctly excluded from idle")

    print("\n=== 9. compute_idle_baseline: correct aggregation for a scalar sensor_ref, established gating ===")
    idle_buckets_cpu = [bucket_fixture(now - i * 60, cpu_temp=40.0) for i in range(BASELINE_MIN_IDLE_BUCKETS)]
    result = compute_idle_baseline(idle_buckets_cpu, scalar_sensor_ref("cpu_temp"))
    assert result["count"] == BASELINE_MIN_IDLE_BUCKETS and result["mean"] == 40.0 and result["established"] is True
    fewer = compute_idle_baseline(idle_buckets_cpu[:-1], scalar_sensor_ref("cpu_temp"))
    assert fewer["established"] is False, f"FAIL: one bucket under the threshold must not be established: {fewer}"
    empty = compute_idle_baseline([], scalar_sensor_ref("cpu_temp"))
    assert empty is None, "FAIL: no idle buckets at all must yield None, never a fabricated baseline"
    print(f"  PASS: exactly {BASELINE_MIN_IDLE_BUCKETS} buckets -> established, "
          f"{BASELINE_MIN_IDLE_BUCKETS - 1} -> not established, 0 -> None")

    print("\n=== 10. compute_idle_baseline: works identically for a 'sensor' (drive/DIMM) kind via extract_bucket_metric ===")
    from app import _sensor_bucket_key
    drive_key = _sensor_bucket_key("/nvme/0/temperature/0")
    drive_buckets = []
    for i in range(BASELINE_MIN_IDLE_BUCKETS):
        b = bucket_fixture(now - i * 60)
        b["sensors"] = {drive_key: {"identifier": "/nvme/0/temperature/0", "parent": "Storage", "name": "NVMe",
                                    "sensor_type": "Temperature", "component": "drive", "unverified": False,
                                    "avg": 35.0, "min": 33.0, "max": 37.0, "count": 30}}
        drive_buckets.append(b)
    drive_ref = {"kind": "sensor", "key": drive_key, "label": "NVMe", "unit": "°C", "is_temp": True, "component": "drive"}
    drive_result = compute_idle_baseline(drive_buckets, drive_ref)
    assert drive_result["mean"] == 35.0 and drive_result["established"] is True
    print(f"  PASS: per-sensor idle baseline mean={drive_result['mean']}")

    print("\n=== 11. AnalyticsWindow: a workload with sessions but ZERO incidents still appears with its baseline ===")
    fresh_files()
    sessions_only = [session_fixture(f"s{i}", "Cyberpunk2077.exe", now - (i + 1) * 3600, 1800,
                                     cpu_avg_temp=60.0 + i) for i in range(5)]
    with SESSIONS_PATH.open("w", encoding="utf-8") as f:
        for s in sessions_only:
            f.write(json.dumps(s) + "\n")
    app = App()
    win = AnalyticsWindow(app)
    win.range_var.set("All")
    win._recompute()
    rows = [win.tree.item(i)["values"] for i in win.tree.get_children()]
    assert len(rows) == 1 and rows[0][0] == "Cyberpunk2077.exe" and rows[0][1] == 5 and rows[0][2] == 0, \
        f"FAIL: expected exactly 1 row (5 sessions, 0 incidents), got {rows}"
    win.tree.selection_set(win.tree.get_children()[0])
    win._on_select(None)
    detail = win.detail_text.cget("text")
    assert "BASELINE" in detail, f"FAIL: baseline section missing from detail text:\n{detail}"
    assert "CPU Package (session avg)" in detail
    assert "(n=5)" in detail
    win.destroy()
    app.stop_event.set(); app.destroy()
    print("  PASS: workload with 0 incidents / 5 sessions appears in the table with a full baseline block")

    print("\n=== 12. AnalyticsWindow: below-threshold session count shows 'not enough data yet', not a fabricated range ===")
    fresh_files()
    with SESSIONS_PATH.open("w", encoding="utf-8") as f:
        for s in sessions_only[:2]:  # only 2, below BASELINE_MIN_SESSIONS=3
            f.write(json.dumps(s) + "\n")
    app2 = App()
    win2 = AnalyticsWindow(app2)
    win2.range_var.set("All")
    win2._recompute()
    win2.tree.selection_set(win2.tree.get_children()[0])
    win2._on_select(None)
    detail2 = win2.detail_text.cget("text")
    assert "not enough data yet" in detail2, f"FAIL: expected a 'not enough data yet' line:\n{detail2}"
    assert "2/3" in detail2 or f"2/{BASELINE_MIN_SESSIONS}" in detail2
    win2.destroy()
    app2.stop_event.set(); app2.destroy()
    print("  PASS: 2 sessions (below threshold) reported honestly as insufficient, not shown as an established range")

    print("\n=== 13. SensorHistoryWindow: idle baseline correctly excludes a concurrent session's active time ===")
    fresh_files()
    session_recent = [session_fixture("s1", "Cyberpunk2077.exe", now - 3600, 1800)]
    with SESSIONS_PATH.open("w", encoding="utf-8") as f:
        for s in session_recent:
            f.write(json.dumps(s) + "\n")
    conn = open_telemetry_db()
    conn.execute("BEGIN")
    for i in range(60):
        ts = now - 3600 + i * 60
        is_active = (now - 3600) <= ts < (now - 1800)
        scalars = json.dumps({k: ({"avg": 80.0, "min": 78.0, "max": 82.0, "count": 30} if k == "cpu_temp" else None)
                              for k in TELEMETRY_SCALAR_KEYS}) if is_active else \
                  json.dumps({k: ({"avg": 40.0, "min": 38.0, "max": 42.0, "count": 30} if k == "cpu_temp" else None)
                              for k in TELEMETRY_SCALAR_KEYS})
        conn.execute("INSERT INTO buckets (start_timestamp, end_timestamp, sample_count, scalars_json) "
                     "VALUES (?,?,?,?)", (ts, ts + 60, 30, scalars))
    conn.commit()
    conn.close()
    app3 = App()
    win3 = SensorHistoryWindow(app3, scalar_sensor_ref("cpu_temp"))
    win3.range_var.set("1h")
    win3._recompute()
    summary = win3.summary_label.cget("text")
    assert "Idle baseline: 40" in summary, f"FAIL: expected idle baseline ~40°C, got:\n{summary}"
    assert "Average: 60" in summary, f"FAIL: expected overall average ~60°C (mixing active+idle): {summary}"
    win3.destroy()
    app3.stop_event.set(); app3.destroy()
    print("  PASS: idle baseline (40°C) correctly distinct from the overall range average (60°C, includes active time)")

    print("\n=== 14. SensorHistoryWindow: idle baseline reuses already-fetched buckets/sessions - no extra query ===")
    app4 = App()
    win4 = SensorHistoryWindow(app4, scalar_sensor_ref("cpu_temp"))
    win4.range_var.set("1h")
    call_counts = {"telemetry": 0, "sessions": 0}
    real_read_telemetry = read_telemetry_file
    real_read_sessions = read_sessions_file

    def counted_telemetry(*a, **kw):
        call_counts["telemetry"] += 1
        return real_read_telemetry(*a, **kw)

    def counted_sessions(*a, **kw):
        call_counts["sessions"] += 1
        return real_read_sessions(*a, **kw)

    with mock.patch("app.read_telemetry_file", side_effect=counted_telemetry), \
        mock.patch("app.read_sessions_file", side_effect=counted_sessions):
        win4._recompute()
    assert call_counts["telemetry"] == 1, f"FAIL: expected exactly 1 telemetry query per _recompute(), got {call_counts['telemetry']}"
    assert call_counts["sessions"] == 1, f"FAIL: expected exactly 1 sessions query per _recompute(), got {call_counts['sessions']}"
    win4.destroy()
    app4.stop_event.set(); app4.destroy()
    print("  PASS: exactly 1 telemetry + 1 sessions query per refresh - idle baseline added no new I/O")

    print("\n=== 15. baseline learning never runs on the live 2s poll ===")
    src = inspect.getsource(App.update_data)
    for forbidden in ("compute_workload_baseline(", "compute_idle_baseline(", "filter_idle_buckets(", "_stat_summary("):
        assert forbidden not in src, f"FAIL: update_data() must never call {forbidden} on the 2s poll"
    print("  PASS: update_data() contains no baseline computation")

    fresh_files()
    print("\nALL BASELINE LEARNING CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
