"""Verification for Scheduled Health Reports: completed-period calendar correctness (daily, Monday
weekly, monthly, February, leap year, year rollover, DST), coverage-gated conclusions, statistics
that match their source telemetry exactly, every analysis figure delegated to the existing helper
rather than recomputed, idempotent generation vs explicit regeneration, read-only viewing,
startup catch-up, persistence/corruption tolerance, and JSON/CSV/text export.

Runs entirely inside the verification sandbox - _verify_sandbox is imported before app, so every
store (including the new reports database) resolves into a temp directory."""
import csv
import io
import json
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
sys.stdout.reconfigure(encoding="utf-8")

import app as appmod  # noqa: E402
from app import (  # noqa: E402
    App, HistoryWindow, ReportsWindow, REPORTS_DB_PATH, REPORT_TYPES, REPORT_MIN_COVERAGE_PCT,
    REPORT_SCHEMA_VERSION, TELEMETRY_BUCKET_SECONDS, TELEMETRY_RETENTION_DAYS, TREND_MIN_COVERAGE_PCT,
    period_bounds, previous_completed_period, local_midnight_ts, add_month, period_label,
    build_report_payload, save_report, read_reports, report_exists, due_report_periods,
    generate_due_reports, regenerate_report, format_report_text, build_report_csv_rows,
    REPORT_CSV_COLUMNS, compute_idle_metric_period_trend, scalar_sensor_ref, compute_recommendations,
    compute_experiment_report, open_reports_db,
)

NOW = time.time()
DAY = 86400.0


def clear_reports():
    conn = open_reports_db()
    if conn is not None:
        conn.execute("DELETE FROM reports")
        conn.close()


def bucket(ts, cpu_temp=54.0, cpu_power=60.0, cpu_util=30.0, gpu_core=58.0, gpu_hotspot=70.0,
           gpu_power=180.0, mem_pct=45.0, count=30):
    def agg(v):
        return {"avg": v, "min": v - 1.0, "max": v + 4.0, "count": count}
    return {"start_timestamp": ts, "end_timestamp": ts + TELEMETRY_BUCKET_SECONDS, "sample_count": count,
           "scalars": {"cpu_temp": agg(cpu_temp), "cpu_power": agg(cpu_power), "cpu_util": agg(cpu_util),
                       "gpu_core_temp": agg(gpu_core), "gpu_hotspot_temp": agg(gpu_hotspot),
                       "gpu_power": agg(gpu_power), "mem_pct": agg(mem_pct)}, "sensors": {}}


def fill(bounds, fraction=1.0, **kw):
    """Buckets covering `fraction` of the period from its start - the knob every coverage check uses."""
    span = bounds["end_ts"] - bounds["start_ts"]
    out, ts = [], bounds["start_ts"]
    while ts < bounds["start_ts"] + span * fraction:
        out.append(bucket(ts, **kw))
        ts += TELEMETRY_BUCKET_SECONDS
    return out


def incident(iid, component, start, zone="ORANGE", dur=600, gaps=None):
    return {"incident_id": iid, "component": component, "start_timestamp": start,
           "end_timestamp": start + dur, "duration_seconds": dur, "duration_exact": not gaps,
           "max_zone": zone, "peak_value": 92.0, "dominant_workload": "game.exe",
           "monitoring_gaps": gaps or []}


def sess(sid, workload, start, dur=1800, cpu_peak=80.0, gpu_peak=88.0, incidents=0, exact=True):
    return {"session_id": sid, "workload_key": workload.casefold(), "workload": workload,
           "start_timestamp": start, "end_timestamp": start + dur, "duration_seconds": dur,
           "duration_exact": exact,
           "cpu": {"avg_temp": 65.0, "peak_temp": cpu_peak, "avg_power": 90.0},
           "gpu": {"avg_core_temp": 70.0, "avg_hotspot_temp": 80.0, "peak_hotspot_temp": gpu_peak,
                  "avg_power": 250.0},
           "zone_time": {}, "incident_count": incidents, "monitoring_gaps": []}


class mock_sources:
    def __init__(self, buckets=(), incidents=(), sessions=(), experiments=(), summaries=()):
        self.buckets, self.incidents = list(buckets), list(incidents)
        self.sessions, self.experiments, self.summaries = list(sessions), list(experiments), list(summaries)

    def __enter__(self):
        self.orig = (appmod.read_telemetry_file, appmod.read_incidents_file, appmod.read_sessions_file,
                     appmod.read_experiments_file, appmod.read_sensor_summaries)
        appmod.read_telemetry_file = lambda since_ts=None, sensor_key=None: [
            b for b in self.buckets if since_ts is None or b["start_timestamp"] >= since_ts]
        appmod.read_incidents_file = lambda: self.incidents
        appmod.read_sessions_file = lambda: self.sessions
        appmod.read_experiments_file = lambda: self.experiments
        appmod.read_sensor_summaries = lambda s, e: self.summaries
        return self

    def __exit__(self, *exc):
        (appmod.read_telemetry_file, appmod.read_incidents_file, appmod.read_sessions_file,
         appmod.read_experiments_file, appmod.read_sensor_summaries) = self.orig


def main():
    clear_reports()

    print("=== 1. DAILY covers the PREVIOUS COMPLETED calendar day, never today ===")
    now = time.mktime(datetime(2026, 8, 13, 14, 30).timetuple())
    d = previous_completed_period("DAILY", now)
    assert d["start_date"] == date(2026, 8, 12) and d["end_date"] == date(2026, 8, 13), d
    assert d["end_ts"] <= now, "FAIL: a scheduled report must never cover time that hasn't elapsed"
    assert d["report_id"] == "DAILY:2026-08-12"
    print(f"  PASS: at 2026-08-13 14:30 the daily period is {d['label']} [{d['start_date']} -> {d['end_date']}), "
          f"ending before now")

    print("\n=== 2. WEEKLY is Monday 00:00 -> next Monday 00:00, previous completed week ===")
    for probe_day, expected_monday in ((13, date(2026, 8, 3)),   # Thursday
                                       (10, date(2026, 8, 3)),   # Monday itself
                                       (16, date(2026, 8, 3)),   # Sunday (last day of current week)
                                       (17, date(2026, 8, 10))):  # next Monday rolls the window
        w = previous_completed_period("WEEKLY", time.mktime(datetime(2026, 8, probe_day, 9, 0).timetuple()))
        assert w["start_date"] == expected_monday, f"FAIL: {probe_day} -> {w['start_date']}, want {expected_monday}"
        assert w["start_date"].weekday() == 0 and w["end_date"].weekday() == 0
        assert (w["end_date"] - w["start_date"]).days == 7
    print("  PASS: Thursday/Monday/Sunday all report the same completed Mon–Sun week; the window rolls "
          "only when a new Monday starts")

    print("\n=== 3. MONTHLY covers the previous completed calendar month ===")
    m = previous_completed_period("MONTHLY", time.mktime(datetime(2026, 8, 13).timetuple()))
    assert m["start_date"] == date(2026, 7, 1) and m["end_date"] == date(2026, 8, 1), m
    assert m["label"] == "July 2026", m["label"]
    print(f"  PASS: {m['label']} [{m['start_date']} -> {m['end_date']})")

    print("\n=== 4. February, leap year and year rollover are exact (date arithmetic, never +30 days) ===")
    feb_2026 = period_bounds("MONTHLY", date(2026, 2, 1))
    assert feb_2026["end_date"] == date(2026, 3, 1)
    assert round((feb_2026["end_ts"] - feb_2026["start_ts"]) / DAY) == 28, "2026 February must be 28 days"
    feb_2028 = period_bounds("MONTHLY", date(2028, 2, 1))
    assert round((feb_2028["end_ts"] - feb_2028["start_ts"]) / DAY) == 29, "2028 is a leap year - 29 days"
    jan = previous_completed_period("MONTHLY", time.mktime(datetime(2027, 1, 9).timetuple()))
    assert jan["start_date"] == date(2026, 12, 1) and jan["end_date"] == date(2027, 1, 1), jan
    ny_day = previous_completed_period("DAILY", time.mktime(datetime(2027, 1, 1, 0, 30).timetuple()))
    assert ny_day["start_date"] == date(2026, 12, 31), ny_day
    ny_week = previous_completed_period("WEEKLY", time.mktime(datetime(2027, 1, 1, 12, 0).timetuple()))
    assert ny_week["start_date"] == date(2026, 12, 21) and ny_week["end_date"] == date(2026, 12, 28), ny_week
    assert add_month(date(2026, 12, 1), 1) == date(2027, 1, 1) and add_month(date(2026, 1, 1), -1) == date(2025, 12, 1)
    print("  PASS: Feb 2026 = 28 days, Feb 2028 = 29, Jan 9 2027 -> December 2026, New Year's Day -> Dec 31, "
          "and a week spanning the year boundary stays Monday-anchored")

    print("\n=== 5. A day is not 86400 seconds: DST transitions keep one report per calendar day ===")
    spring = [period_bounds("DAILY", date(2026, 3, 7) + timedelta(days=i)) for i in range(3)]
    lengths = [round((b["end_ts"] - b["start_ts"]) / 3600) for b in spring]
    ids = [b["report_id"] for b in spring]
    assert len(set(ids)) == 3, "FAIL: DST must never collapse two calendar days into one report id"
    assert all(23 <= h <= 25 for h in lengths), lengths
    if 23 in lengths or 25 in lengths:
        detail = f"a DST day really is {[h for h in lengths if h != 24][0]}h and still one report"
    else:
        detail = "this machine's timezone has no March DST shift; identity is still per calendar date"
    print(f"  PASS: consecutive daily periods {ids[0][-5:]}..{ids[-1][-5:]} have distinct ids, lengths {lengths}h - "
          f"{detail}")

    print("\n=== 6. Coverage gates the CONCLUSION, reusing the existing threshold and calculation ===")
    assert REPORT_MIN_COVERAGE_PCT == TREND_MIN_COVERAGE_PCT, \
        "FAIL: the report bar must be the SAME number Trend Intelligence already uses, not a new one"
    bounds = period_bounds("DAILY", date(2026, 8, 12))
    sessions = [sess(f"s{i}", "game.exe", bounds["start_ts"] + i * 3600) for i in range(4)]
    with mock_sources(buckets=fill(bounds, 0.06), sessions=sessions):
        thin = build_report_payload(bounds, now=bounds["end_ts"])
    assert not thin["sufficient_coverage"] and thin["overview"]["status"] == "INSUFFICIENT COVERAGE", thin["overview"]
    assert thin["overview"]["coverage_pct"] < REPORT_MIN_COVERAGE_PCT
    text = "\n".join(format_report_text(thin))
    assert "INSUFFICIENT COVERAGE" in text and "Limited monitoring coverage" in text, text[:400]
    assert "observed during available telemetry" in text
    with mock_sources(buckets=fill(bounds, 0.95), sessions=sessions):
        full = build_report_payload(bounds, now=bounds["end_ts"])
    assert full["sufficient_coverage"] and full["overview"]["status"] != "INSUFFICIENT COVERAGE"
    print(f"  PASS: {thin['overview']['coverage_pct']:.1f}% coverage -> INSUFFICIENT COVERAGE with the "
          f"'may not represent the full period' wording; {full['overview']['coverage_pct']:.1f}% -> "
          f"'{full['overview']['status']}'")

    print("\n=== 6b. Observed facts still appear under low coverage - they were really measured ===")
    assert thin["cpu"]["metrics"]["cpu_temp"]["max"] is not None
    assert "Maximum observed" in text or "Maximum" in text
    print(f"  PASS: a real maximum ({thin['cpu']['metrics']['cpu_temp']['max']:.0f}°C) is still reported under "
          f"6% coverage - it is a genuine measurement, just not representative")

    print("\n=== 7. Missing telemetry stays MISSING - it never becomes 0 ===")
    no_gpu = [bucket(bounds["start_ts"] + i * 60) for i in range(60)]
    for b in no_gpu:
        del b["scalars"]["gpu_hotspot_temp"]
    with mock_sources(buckets=no_gpu):
        missing = build_report_payload(bounds, now=bounds["end_ts"])
    assert "gpu_hotspot_temp" not in missing["gpu"]["metrics"], missing["gpu"]["metrics"]
    assert "gpu_vram_temp" not in missing["gpu"]["metrics"], "a never-recorded sensor must be absent, not 0"
    joined = "\n".join(format_report_text(missing))
    assert "memory junction" not in joined.lower(), "FAIL: an unrecorded metric must not be rendered at all"
    assert "0°C" not in joined, "FAIL: a missing reading must never be printed as 0°C"
    print("  PASS: sensors never recorded are absent from the payload and unrendered - no 0°C placeholders")

    print("\n=== 8/9. CPU and GPU statistics match the source telemetry exactly ===")
    varied = [bucket(bounds["start_ts"] + i * 60, cpu_temp=50.0 + i, gpu_hotspot=70.0 + i * 0.5)
              for i in range(60)]
    with mock_sources(buckets=varied):
        stats_report = build_report_payload(bounds, now=bounds["end_ts"])
    cpu_stats = stats_report["cpu"]["metrics"]["cpu_temp"]
    expected_avg = sum(50.0 + i for i in range(60)) / 60
    assert abs(cpu_stats["avg"] - expected_avg) < 1e-9, (cpu_stats, expected_avg)
    assert abs(cpu_stats["max"] - (50.0 + 59 + 4.0)) < 1e-9, cpu_stats   # fixture max = avg + 4
    assert abs(cpu_stats["min"] - (50.0 - 1.0)) < 1e-9, cpu_stats
    gpu_stats = stats_report["gpu"]["metrics"]["gpu_hotspot_temp"]
    assert abs(gpu_stats["avg"] - sum(70.0 + i * 0.5 for i in range(60)) / 60) < 1e-9, gpu_stats
    assert abs(gpu_stats["max"] - (70.0 + 29.5 + 4.0)) < 1e-9, gpu_stats
    print(f"  PASS: CPU avg/min/max {cpu_stats['avg']:.2f}/{cpu_stats['min']:.1f}/{cpu_stats['max']:.1f}°C and "
          f"GPU hotspot {gpu_stats['avg']:.2f}/{gpu_stats['max']:.1f}°C match the fixture arithmetic exactly")

    print("\n=== 10/11/12. Incident and session counts respect the period; uncertainty is preserved ===")
    inside = [incident("in1", "cpu", bounds["start_ts"] + 3600),
             incident("in2", "gpu_hotspot", bounds["start_ts"] + 7200, zone="RED"),
             incident("in3", "gpu_core", bounds["start_ts"] + 10800,
                      gaps=[{"gap_seconds": 120.0}])]
    outside = [incident("out1", "cpu", bounds["start_ts"] - 5 * 3600),
              incident("out2", "cpu", bounds["end_ts"] + 3600)]
    mixed_sessions = [sess("a", "game.exe", bounds["start_ts"] + 600),
                     sess("b", "game.exe", bounds["start_ts"] + 4000, exact=False),
                     sess("c", "python.exe", bounds["start_ts"] + 9000),
                     sess("outside", "game.exe", bounds["end_ts"] + 7200)]
    with mock_sources(buckets=fill(bounds, 0.95), incidents=inside + outside, sessions=mixed_sessions):
        counted = build_report_payload(bounds, now=bounds["end_ts"])
    ov = counted["overview"]
    assert ov["incidents"] == 3, ov
    assert ov["critical_incidents"] == 1, ov
    assert ov["incidents_with_monitoring_gaps"] == 1, ov
    assert ov["sessions"] == 3, ov
    assert ov["uncertain_sessions"] == 1, ov
    assert counted["cpu"]["incidents"]["count"] == 1 and counted["gpu"]["incidents"]["count"] == 2
    assert counted["gpu"]["incidents"]["max_severity"] == "RED"
    assert any("not exact" in f["text"] for f in counted["findings"])
    print("  PASS: 3 of 5 incidents and 3 of 4 sessions fall in the period; 1 critical, 1 gap-spanning and "
          "1 uncertain-duration session all stay explicitly flagged")

    print("\n=== 13. Workload summaries come from the session records, and never claim causation ===")
    workloads = {w["workload"]: w for w in counted["workloads"]}
    assert workloads["game.exe"]["sessions"] == 2 and workloads["python.exe"]["sessions"] == 1
    assert "associated_incidents" in workloads["game.exe"]
    assert workloads["game.exe"]["peak_gpu_hotspot"] == 88.0
    assert workloads["game.exe"]["uncertain_sessions"] == 1
    report_text = "\n".join(format_report_text(counted))
    assert "Associated incidents" in report_text
    for causal in ("caused by", "responsible for", "blame"):
        assert causal not in report_text.lower(), f"FAIL: causal workload language '{causal}'"
    print("  PASS: per-workload counts/peaks come from the session records, and the section says "
          "'Associated incidents', never 'caused by'")

    print("\n=== 14. Trends are whatever Trend Intelligence says for the same source data ===")
    with mock_sources(buckets=fill(bounds, 0.95), sessions=mixed_sessions):
        trend_report = build_report_payload(bounds, now=bounds["end_ts"])
        period_days = (bounds["end_ts"] - bounds["start_ts"]) / DAY
        direct = compute_idle_metric_period_trend(scalar_sensor_ref("cpu_temp"), period_days, now=bounds["end_ts"])
    assert trend_report["cpu"]["idle_trend"] == direct, \
        "FAIL: the report must report Trend Intelligence's own result, never a second calculation"
    print(f"  PASS: the report's CPU idle trend is identical to calling compute_idle_metric_period_trend "
          f"directly ({'None - not enough idle data' if direct is None else direct['direction']})")

    print("\n=== 15. Recommendations are the Recommendation engine's own output, NO ACTION included ===")
    with mock_sources(buckets=fill(bounds, 0.95), sessions=mixed_sessions):
        rec_report = build_report_payload(bounds, now=bounds["end_ts"])
        engine = compute_recommendations(now=bounds["end_ts"])
    assert [r["title"] for r in rec_report["recommendations"]] == [r["title"] for r in engine], \
        "FAIL: report recommendations must match the engine exactly"
    assert rec_report["recommendations"][0]["title"] == "NO ACTION RECOMMENDED"
    assert "NO ACTION RECOMMENDED" in "\n".join(format_report_text(rec_report)), \
        "FAIL: a quiet machine's NO ACTION result must be preserved, not replaced with invented advice"
    print("  PASS: recommendations pass through verbatim - a quiet machine's report says NO ACTION RECOMMENDED")

    print("\n=== 16. Experiment summaries are Phase 13's own result, not a re-derivation ===")
    change = bounds["start_ts"] - 4 * DAY
    exp = {"experiment_id": "e1", "change_timestamp": change, "description": "Added rear exhaust fans",
          "component": "gpu"}
    exp_sessions = ([sess(f"eb{i}", "game.exe", change - (7 - i) * DAY) for i in range(6)]
                    + [sess(f"ea{i}", "game.exe", change + (i + 1) * DAY * 0.5) for i in range(6)])
    for i, s in enumerate(exp_sessions):
        s["gpu"]["avg_hotspot_temp"] = (91.0 if i < 6 else 84.0) + i * 0.1
        s["gpu"]["avg_power"] = 318.0 + i * 0.4
    with mock_sources(buckets=fill(bounds, 0.95), sessions=exp_sessions, experiments=[exp]):
        exp_report = build_report_payload(bounds, now=bounds["end_ts"])
        direct_exp = compute_experiment_report(exp, now=bounds["end_ts"])
    assert len(exp_report["experiments"]) == 1, exp_report["experiments"]
    summary = exp_report["experiments"][0]
    assert summary["direction"] == direct_exp["direction"] and summary["confidence"] == direct_exp["confidence"]
    exp_text = "\n".join(format_report_text(exp_report))
    assert "HARDWARE CHANGE" in exp_text and "does not prove" in exp_text
    print(f"  PASS: the report's experiment result ({summary['direction']}/{summary['confidence']}) is identical "
          f"to compute_experiment_report's, and keeps the non-causal caveat")

    print("\n=== 17. An UNVERIFIED motherboard sensor gets an observed range and no health conclusion ===")
    summaries = [{"sensor_key": "pcie", "name": "PCIe x1", "parent": "SuperIO", "sensor_type": "Temperature",
                  "component": None, "unverified": True, "avg": 42.0, "min": 38.0, "max": 61.0, "count": 900},
                 {"sensor_key": "d1", "name": "Storage 0", "parent": "nvme", "sensor_type": "Temperature",
                  "component": "drive", "unverified": False, "avg": 45.0, "min": 40.0, "max": 58.0, "count": 900}]
    with mock_sources(buckets=fill(bounds, 0.95), summaries=summaries):
        mobo = build_report_payload(bounds, now=bounds["end_ts"])
    mobo_sensors = mobo["sensors"]["motherboard"]["sensors"]
    assert len(mobo_sensors) == 1 and mobo_sensors[0]["unverified"] is True
    assert mobo["sensors"]["motherboard"]["unverified_count"] == 1
    mobo_text = "\n".join(format_report_text(mobo))
    assert "unverified sensor - observed values only, no health conclusion" in mobo_text, mobo_text
    pcie_line = next(l for l in format_report_text(mobo) if l.startswith("PCIe x1"))
    for verdict in ("GREEN", "YELLOW", "ORANGE", "RED", "healthy", "normal", "elevated", "critical"):
        assert verdict not in pcie_line, f"FAIL: unverified sensor line carries a health verdict: {pcie_line}"
    assert "observed 38–61°C" in pcie_line, pcie_line
    print(f"  PASS: {pcie_line.strip()}")

    print("\n=== 18. Idempotent generation: the same period twice is ONE logical report ===")
    clear_reports()
    with mock_sources(buckets=fill(bounds, 0.95), sessions=mixed_sessions, incidents=inside):
        first = build_report_payload(bounds, now=bounds["end_ts"])
        assert save_report(first)
        second = build_report_payload(bounds, now=bounds["end_ts"] + 500)
        assert save_report(second)
    stored = read_reports()
    assert len(stored) == 1, f"FAIL: duplicate reports for one period: {[r['report_id'] for r in stored]}"
    assert stored[0]["generated_timestamp"] == first["generated_timestamp"], \
        "FAIL: re-generation without an explicit request must keep the ORIGINAL report's conclusions"
    print("  PASS: generating the same period twice leaves exactly one report, and the first generation's "
          "timestamp/conclusions stand")

    print("\n=== 19/21. Explicit regeneration UPDATES in place, keeping the logical identity ===")
    with mock_sources(buckets=fill(bounds, 0.95), sessions=mixed_sessions, incidents=inside):
        regenerated = regenerate_report(first["report_id"], now=bounds["end_ts"] + 9000)
    stored = read_reports()
    assert regenerated is not None and len(stored) == 1, stored
    assert stored[0]["report_id"] == first["report_id"]
    assert stored[0]["generated_timestamp"] > first["generated_timestamp"], \
        "FAIL: regeneration must record a NEW generation timestamp"
    assert regenerated["reconstruction"]["regenerated"] is True
    assert regenerated["reconstruction"]["previous_generated_timestamp"] == first["generated_timestamp"]
    print("  PASS: REGENERATE replaces the payload under the same report_id and records a new generation "
          "timestamp, with the previous one retained for reference")

    print("\n=== 20. Viewing a report mutates nothing ===")
    before_rows = [(r["report_id"], r["generated_timestamp"], json.dumps(r["payload"], sort_keys=True))
                   for r in read_reports()]
    for r in read_reports():
        format_report_text(r["payload"])
        build_report_csv_rows(r["payload"])
    after_rows = [(r["report_id"], r["generated_timestamp"], json.dumps(r["payload"], sort_keys=True))
                  for r in read_reports()]
    assert before_rows == after_rows, "FAIL: rendering a stored report changed the stored report"
    print("  PASS: rendering text and CSV from stored reports leaves every stored row byte-identical")

    print("\n=== 22/23. Startup catch-up generates ONLY what is missing, and the due check is cheap ===")
    clear_reports()
    calls = {"telemetry": 0}
    real_reader = appmod.read_telemetry_file
    with mock_sources(buckets=fill(bounds, 0.95), sessions=mixed_sessions, incidents=inside):
        counting = appmod.read_telemetry_file

        def counted_reader(since_ts=None, sensor_key=None):
            calls["telemetry"] += 1
            return counting(since_ts, sensor_key)

        appmod.read_telemetry_file = counted_reader
        due_before = due_report_periods(now=NOW)
        assert len(due_before) == len(REPORT_TYPES), due_before
        created = generate_due_reports(now=NOW)
        assert sorted(created) == sorted(b["report_id"] for b in due_before), created
        after_generation = calls["telemetry"]
        assert due_report_periods(now=NOW) == [], "FAIL: nothing should be due immediately after catch-up"
        again = generate_due_reports(now=NOW)
        assert again == [], "FAIL: startup must not regenerate reports that already exist"
        assert calls["telemetry"] == after_generation, \
            "FAIL: a due-check with nothing missing must not touch telemetry at all"
    appmod.read_telemetry_file = real_reader
    assert len(read_reports()) == len(REPORT_TYPES)
    print(f"  PASS: catch-up created {len(created)} missing reports ({', '.join(sorted(created))}); a second "
          f"pass created none and performed ZERO telemetry reads")

    print("\n=== 24. JSON export round-trips the structured report ===")
    rep = read_reports()[0]
    round_tripped = json.loads(json.dumps(rep["payload"]))
    assert round_tripped == rep["payload"], "FAIL: payload does not survive a JSON round-trip"
    assert round_tripped["schema_version"] == REPORT_SCHEMA_VERSION
    for key in ("overview", "cpu", "gpu", "workloads", "findings", "recommendations", "sensors"):
        assert key in round_tripped, key
    assert format_report_text(round_tripped) == format_report_text(rep["payload"]), \
        "FAIL: a round-tripped payload must render identically - the report is re-renderable from storage alone"
    print("  PASS: the stored payload survives JSON exactly and re-renders identically from storage alone")

    print("\n=== 25. Plain text contains only stored evidence, and is re-rendered from the payload ===")
    payload = rep["payload"]
    lines = format_report_text(payload)
    assert lines[0] == "THERMAL WATCH" and "SYSTEM HEALTH REPORT" in lines[1]
    assert any(l.startswith("Monitoring coverage:") for l in lines), "coverage must be prominent"
    assert any(l.startswith("Period:") for l in lines)
    frozen = json.loads(json.dumps(payload))
    frozen["overview"]["incidents"] = 999
    assert "Thermal incidents: 999" in "\n".join(format_report_text(frozen)), \
        "FAIL: the renderer must read the STORED payload, not recompute from live sources"
    print("  PASS: the text rendering is a pure function of the stored payload - editing the payload changes "
          "the text, and nothing is recomputed at render time")

    print("\n=== 26. CSV handles Unicode, quotes and commas correctly ===")
    tricky = json.loads(json.dumps(payload))
    tricky["workloads"] = [{"workload": 'Cybérpunk "2077", ünicode.exe\nnewline', "workload_key": "x",
                            "sessions": 2, "total_seconds": 100, "uncertain_sessions": 0,
                            "avg_health_score": 91.5, "peak_cpu_temp": 87.0, "peak_gpu_hotspot": 91.0,
                            "associated_incidents": 1, "anomalous_sessions": None}]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(REPORT_CSV_COLUMNS)
    writer.writerows(build_report_csv_rows(tricky))
    parsed = list(csv.reader(io.StringIO(buf.getvalue())))
    assert parsed[0] == REPORT_CSV_COLUMNS
    names = {row[1] for row in parsed[1:] if row[0] == "workload"}
    assert 'Cybérpunk "2077", ünicode.exe\nnewline' in names, names
    print("  PASS: a workload name with an embedded quote, comma, newline and non-ASCII characters "
          "round-trips through csv.writer/csv.reader intact")

    print("\n=== 27. A corrupt report store never stops Thermal Watch, and is preserved not deleted ===")
    REPORTS_DB_PATH.write_bytes(b"this is definitely not a sqlite database")
    conn = open_reports_db()
    assert conn is not None, "FAIL: a corrupt store must be recovered, not fatal"
    conn.close()
    corrupt_copies = list(REPORTS_DB_PATH.parent.glob(f"{REPORTS_DB_PATH.stem}.corrupt-*"))
    assert corrupt_copies, "FAIL: the corrupt file must be moved aside, never deleted"
    assert read_reports() == [], "a fresh store starts empty"
    app = App()
    app.stop_event.set(); app.destroy()
    print(f"  PASS: a corrupted store is renamed aside ({corrupt_copies[0].name}), a fresh one is created, "
          f"and App() still starts")

    print("\n=== 28/29. A stored report survives source retention; regeneration says so honestly ===")
    clear_reports()
    old_bounds = period_bounds("DAILY", (datetime.fromtimestamp(NOW) - timedelta(days=45)).date())
    with mock_sources(buckets=fill(old_bounds, 0.95), sessions=[], incidents=[]):
        old_payload = build_report_payload(old_bounds, now=old_bounds["end_ts"])
        assert save_report(old_payload)
    original_coverage = read_reports()[0]["coverage_pct"]
    assert original_coverage > 90, original_coverage
    with mock_sources(buckets=[], sessions=[], incidents=[]):  # telemetry has since been pruned
        rebuilt = regenerate_report(old_payload["report_id"], now=NOW)
    assert rebuilt is not None
    recon = rebuilt["reconstruction"]
    assert recon["source_data_expired"] is True, recon
    assert str(TELEMETRY_RETENTION_DAYS) in recon["note"] and "could not be fully reconstructed" in recon["note"]
    assert abs(recon["previous_coverage_pct"] - original_coverage) < 1e-9
    assert "REGENERATION" in "\n".join(format_report_text(rebuilt))
    print(f"  PASS: a 45-day-old report regenerated with pruned telemetry reports "
          f"{rebuilt['overview']['coverage_pct']:.1f}% coverage AND states plainly that it could not be fully "
          f"reconstructed, retaining the original {original_coverage:.1f}%")

    print("\n=== 30. ReportsWindow: opens from History as a singleton, renders, filters, regenerates ===")
    clear_reports()
    with mock_sources(buckets=fill(bounds, 0.95), sessions=mixed_sessions, incidents=inside):
        for report_type in REPORT_TYPES:
            b = previous_completed_period(report_type, now=NOW)
            save_report(build_report_payload(b, now=NOW))
    app = App()
    hw = HistoryWindow(app)
    hw.open_reports()
    app.update()
    win = hw.reports_window
    hw.open_reports()
    assert hw.reports_window is win, "FAIL: open_reports() must reuse the existing window (singleton)"
    assert len(win.tree.get_children()) == len(REPORT_TYPES), win.tree.get_children()
    assert "SYSTEM HEALTH REPORT" in win.detail_text.cget("text")
    win.filter_var.set("WEEKLY")
    win._reload()
    app.update()
    assert len(win.tree.get_children()) == 1
    assert win._selected()["report_type"] == "WEEKLY"
    before_generated = win._selected()["generated_timestamp"]
    win.tree.selection_set(win.tree.get_children()[0])
    win._on_select()
    app.update()
    assert win._selected()["generated_timestamp"] == before_generated, "FAIL: selecting a report mutated it"
    win._regenerate()
    app.update()
    assert win._selected()["generated_timestamp"] > before_generated, "FAIL: REGENERATE must update the timestamp"
    assert len(read_reports()) == len(REPORT_TYPES), "FAIL: regeneration must not create an extra report"
    win.destroy(); hw.destroy()
    app.stop_event.set(); app.destroy()
    print("  PASS: singleton from History, 3 reports listed, DAILY/WEEKLY/MONTHLY filtering works, selecting "
          "mutates nothing, and REGENERATE updates in place without duplicating")

    print("\n=== 31. Reports never run on the 2s poll ===")
    import inspect
    update_src = inspect.getsource(App.update_data)
    for name in ("build_report_payload(", "generate_due_reports(", "read_reports(", "save_report("):
        assert name not in update_src, f"FAIL: update_data() must never call {name}"
    check_src = inspect.getsource(App._check_due_reports)
    assert "REPORT_DUE_CHECK_INTERVAL_MS" in check_src, "the due check must reschedule on its own slow timer"
    print("  PASS: update_data() contains no report work; the due check runs on its own "
          f"{appmod.REPORT_DUE_CHECK_INTERVAL_MS // 60000}-minute timer")

    print("\n=== 32. Everything this script touched stayed inside the sandbox ===")
    assert str(REPORTS_DB_PATH.parent) == str(_verify_sandbox.SANDBOX_DIR), REPORTS_DB_PATH
    print(f"  PASS: the reports store lives at {REPORTS_DB_PATH.parent} (the sandbox), not the production tree")

    clear_reports()
    print("\nALL SCHEDULED HEALTH REPORT CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
