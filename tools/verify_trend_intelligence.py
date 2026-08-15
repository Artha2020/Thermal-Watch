"""Verification for Trend Intelligence: calendar-based (not session-count-based) period-over-
period comparison, the two-factor HIGH/MEDIUM/LOW confidence rubric that caps confidence on thin
data (the user's own "three random samples must never produce YOUR GPU IS DETERIORATING!!!"
guard), workload-matched trends never cross-contaminating between workloads, idle-baseline and
incident-frequency trends, thermal efficiency, hotspot/core delta trends, the WEEK OVER WEEK and
GPU COOLING — N DAY TREND report builders/formatters against the user's own worked examples,
TrendsWindow wiring, and that the whole layer stays read-only/on-demand - never the 2s poll."""
import inspect
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path

# This script's own console output includes "→" (report formatting), which isn't representable
# in the default Windows cp1252 console codepage - a pure test-narration concern, unrelated to
# app.py (Tk Label widgets render Unicode natively regardless of console codepage).
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
import app as appmod  # noqa: E402
from app import (  # noqa: E402
    App, HistoryWindow, TrendsWindow, SESSIONS_PATH, INCIDENTS_PATH,
    calendar_window_halves, compare_period_values, _trend_confidence,
    compute_workload_period_trend, compute_hotspot_core_delta_period_trend,
    session_thermal_efficiency, compute_thermal_efficiency_period_trend,
    compute_idle_metric_period_trend, compute_incident_frequency_trend,
    compute_health_score_period_trend, compute_week_over_week_report,
    compute_workload_cooling_trend_report, format_week_over_week_report,
    format_workload_cooling_trend_report, scalar_sensor_ref, group_sessions_by_workload,
    TREND_MIN_SAMPLES, TREND_GENEROUS_SAMPLES, TREND_WOW_LOOKBACK_DAYS, TREND_MONTH_LOOKBACK_DAYS,
    BASELINE_MIN_IDLE_BUCKETS, ANOMALY_Z_THRESHOLD,
)

NOW = time.time()


def fresh_files():
    for p in (SESSIONS_PATH, INCIDENTS_PATH):
        if p.exists():
            p.unlink()


def session_fixture(sid, workload, start, cpu_temp=60.0, cpu_power=80.0, gpu_core=65.0,
                    gpu_hotspot=77.0, gpu_power=250.0, dur=1800):
    return {
        "session_id": sid, "workload_key": workload.casefold(), "workload": workload,
        "start_timestamp": start, "end_timestamp": start + dur, "duration_seconds": dur,
        "duration_exact": True,
        "cpu": {"avg_temp": cpu_temp, "peak_temp": cpu_temp + 10, "avg_power": cpu_power},
        "gpu": {"avg_core_temp": gpu_core, "peak_core_temp": gpu_core + 10,
               "avg_hotspot_temp": gpu_hotspot, "peak_hotspot_temp": gpu_hotspot + 4,
               "avg_vram_temp": 78.0, "peak_vram_temp": 82.0,
               "avg_power": gpu_power, "peak_power": gpu_power + 50},
        "zone_time": {}, "incident_count": 0, "max_incident_severity": None,
        "incident_ids": [], "monitoring_gaps": [],
    }


@contextmanager
def mock_reads(sessions=None, incidents=None, buckets=None):
    orig_s, orig_i, orig_t = appmod.read_sessions_file, appmod.read_incidents_file, appmod.read_telemetry_file
    if sessions is not None:
        appmod.read_sessions_file = lambda: sessions
    if incidents is not None:
        appmod.read_incidents_file = lambda: incidents
    if buckets is not None:
        appmod.read_telemetry_file = lambda since_ts=None, sensor_key=None: buckets
    try:
        yield
    finally:
        appmod.read_sessions_file, appmod.read_incidents_file, appmod.read_telemetry_file = orig_s, orig_i, orig_t


def main():
    fresh_files()

    print("=== 1. calendar_window_halves: two equal, adjacent, non-overlapping calendar halves ===")
    o_start, o_end, r_start, r_end = calendar_window_halves(14, now=NOW)
    assert o_end == r_start, "FAIL: the two halves must be exactly adjacent"
    assert r_end == NOW
    assert abs((o_end - o_start) - 7 * 86400) < 1e-6 and abs((r_end - r_start) - 7 * 86400) < 1e-6
    o2_start, o2_end, r2_start, r2_end = calendar_window_halves(30, now=NOW)
    assert abs((o2_end - o2_start) - 15 * 86400) < 1e-6 and abs((r2_end - r2_start) - 15 * 86400) < 1e-6
    print(f"  PASS: 14 days -> two 7-day adjacent halves, 30 days -> two 15-day adjacent halves")

    print("\n=== 2. _trend_confidence: thin data caps out below what generous data would reach ===")
    assert _trend_confidence(3.5, 6, 6) == "HIGH"
    assert _trend_confidence(3.5, 3, 3) == "MEDIUM", "FAIL: a huge shift on thin (3-sample) data must never be HIGH"
    assert _trend_confidence(3.5, 3, 8) == "MEDIUM", "FAIL: EITHER period being thin caps confidence"
    assert _trend_confidence(2.5, 6, 6) == "MEDIUM"
    assert _trend_confidence(2.5, 3, 3) == "LOW", "FAIL: a modest shift on thin data must be LOW"
    assert _trend_confidence(None, 3, 3) == "LOW"
    assert _trend_confidence(None, 8, 8) == "MEDIUM"
    print("  PASS: thin data (either period < 6 samples) never reaches HIGH regardless of shift size")

    print("\n=== 3. compare_period_values: below TREND_MIN_SAMPLES -> None (the '3 random samples' guard) ===")
    assert compare_period_values([80.0, 82.0], [95.0, 96.0], "°C") is None, \
        "FAIL: 2 samples/period is below TREND_MIN_SAMPLES - must never produce a trend verdict"
    exactly_min = compare_period_values([80.0, 82.0, 81.0], [95.0, 96.0, 97.0], "°C")
    assert exactly_min is not None, "FAIL: exactly TREND_MIN_SAMPLES should be enough to compute SOMETHING"
    assert exactly_min["confidence"] != "HIGH", \
        f"FAIL: exactly-at-minimum samples (the literal '3 random samples' case) must never reach HIGH: {exactly_min}"
    print(f"  PASS: 2/period -> None (not enough data yet); exactly 3/period with a real shift -> "
          f"confidence={exactly_min['confidence']} (never HIGH) - directly answers the '3 random samples' complaint")

    print("\n=== 4. compare_period_values: STABLE when no real difference, WORSENING/IMPROVING otherwise ===")
    stable = compare_period_values([80.0, 81.0, 82.0, 80.5, 81.5, 81.0], [80.2, 81.2, 81.8, 80.6, 81.4, 81.1], "°C")
    assert stable is not None and stable["direction"] == "STABLE", f"FAIL: near-identical periods must be STABLE: {stable}"
    worse = compare_period_values([70.0] * 6, [90.0] * 6, "°C", higher_is_worse=True)
    assert worse["direction"] == "WORSENING"
    better_low_worse = compare_period_values([70.0] * 6, [90.0] * 6, "°C", higher_is_worse=False)
    assert better_low_worse["direction"] == "IMPROVING", "FAIL: higher_is_worse=False must flip the wording, not the math"
    assert better_low_worse["delta"] == worse["delta"], "FAIL: higher_is_worse must never change the underlying numbers"
    print("  PASS: no real difference -> STABLE; higher_is_worse flips WORSENING<->IMPROVING wording only, never the math")

    print("\n=== 5. compute_workload_period_trend: takes an already-filtered session list on trust (like the rest ===")
    print("===    of this file - e.g. diagnose_gpu_cooling_pattern) - isolation is the CALLER's job, verified ===")
    print("===    end-to-end via the report builders below (test 12/13), which group_sessions_by_workload first ===")
    py_sessions = ([session_fixture(f"py_old{i}", "python.exe", NOW - (25 - i) * 86400, cpu_temp=70.0) for i in range(4)]
                   + [session_fixture(f"py_new{i}", "python.exe", NOW - (10 - i) * 86400, cpu_temp=76.0) for i in range(4)])
    trend_isolated = compute_workload_period_trend(py_sessions, "cpu", "avg_temp", "°C", 30, now=NOW)
    assert trend_isolated["direction"] == "WORSENING" and abs(trend_isolated["delta"] - 6.0) < 1e-6
    print(f"  PASS: python.exe's own CPU trend correctly computed ({trend_isolated['direction']}, "
          f"+{trend_isolated['delta']:.1f}°C) from its pre-filtered session list")

    print("\n=== 6. compute_hotspot_core_delta_period_trend: mean-of-differences, period-over-period ===")
    delta_sessions = ([session_fixture(f"d_old{i}", "game.exe", NOW - (25 - i) * 86400, gpu_core=65.0, gpu_hotspot=75.0)
                       for i in range(4)]
                      + [session_fixture(f"d_new{i}", "game.exe", NOW - (10 - i) * 86400, gpu_core=65.0, gpu_hotspot=80.0)
                        for i in range(4)])
    delta_trend = compute_hotspot_core_delta_period_trend(delta_sessions, 30, now=NOW)
    assert delta_trend is not None and abs(delta_trend["older_mean"] - 10.0) < 1e-6 and abs(delta_trend["recent_mean"] - 15.0) < 1e-6
    print(f"  PASS: older delta {delta_trend['older_mean']:.0f}°C -> recent delta {delta_trend['recent_mean']:.0f}°C "
          f"({delta_trend['direction']})")

    print("\n=== 7. session_thermal_efficiency: °C/W, never divides by zero/missing power ===")
    eff_session = session_fixture("e1", "x.exe", NOW, cpu_temp=60.0, cpu_power=80.0)
    assert abs(session_thermal_efficiency(eff_session, "cpu") - 0.75) < 1e-9
    zero_power = session_fixture("e2", "x.exe", NOW, cpu_temp=60.0, cpu_power=0.0)
    assert session_thermal_efficiency(zero_power, "cpu") is None, "FAIL: near-zero power must never divide-by-zero/fabricate a ratio"
    missing_block = {"cpu": {}}
    assert session_thermal_efficiency(missing_block, "cpu") is None
    print("  PASS: 60°C/80W -> 0.75°C/W; zero power and a missing block both correctly yield None")

    print("\n=== 8. compute_thermal_efficiency_period_trend: rising ratio flags even if temp alone looks 'proportional' ===")
    # Small per-session jitter (real sessions are never bit-for-bit identical) so each period has
    # a real (non-zero) stddev and the z-score path actually runs, rather than accidentally
    # exercising the documented zero-stddev/no-established-threshold edge case from test 7's
    # docstring. Same temp rise as power rise (70->84 = +20%, 100->120W = +20%) - proportional,
    # ratio unchanged.
    eff_sessions_flat = ([session_fixture(f"f_old{i}", "x.exe", NOW - (25 - i) * 86400,
                                          cpu_temp=70.0 + i * 0.1, cpu_power=100.0 + i * 0.1) for i in range(4)]
                         + [session_fixture(f"f_new{i}", "x.exe", NOW - (10 - i) * 86400,
                                           cpu_temp=84.0 + i * 0.12, cpu_power=120.0 + i * 0.12) for i in range(4)])
    flat_eff = compute_thermal_efficiency_period_trend(eff_sessions_flat, "cpu", 30, now=NOW)
    assert flat_eff is not None and flat_eff["direction"] == "STABLE", \
        f"FAIL: a proportional temp/power rise means UNCHANGED efficiency - must be STABLE: {flat_eff}"
    # Temp rises but power does NOT - genuine efficiency degradation (more heat per watt).
    eff_sessions_worse = ([session_fixture(f"w_old{i}", "x.exe", NOW - (25 - i) * 86400,
                                           cpu_temp=70.0 + i * 0.1, cpu_power=100.0 + i * 0.1) for i in range(4)]
                          + [session_fixture(f"w_new{i}", "x.exe", NOW - (10 - i) * 86400,
                                            cpu_temp=85.0 + i * 0.1, cpu_power=100.0 + i * 0.1) for i in range(4)])
    worse_eff = compute_thermal_efficiency_period_trend(eff_sessions_worse, "cpu", 30, now=NOW)
    assert worse_eff is not None and worse_eff["direction"] == "WORSENING", worse_eff
    print(f"  PASS: proportional temp+power rise -> STABLE efficiency; temp rise with FLAT power -> "
          f"WORSENING efficiency ({worse_eff['older_mean']:.2f} -> {worse_eff['recent_mean']:.2f} °C/W)")

    print("\n=== 9. compute_idle_metric_period_trend: idle-time only, gated on BASELINE_MIN_IDLE_BUCKETS ===")
    def bucket(start_ts, cpu_temp):
        return {"start_timestamp": start_ts, "end_timestamp": start_ts + 60, "sample_count": 30,
               "scalars": {"cpu_temp": {"avg": cpu_temp, "min": cpu_temp - 1, "max": cpu_temp + 1, "count": 30}},
               "sensors": {}}
    idle_buckets = ([bucket(NOW - (25 * 86400) + i * 90, 40.0) for i in range(BASELINE_MIN_IDLE_BUCKETS)]
                    + [bucket(NOW - (10 * 86400) + i * 90, 44.0) for i in range(BASELINE_MIN_IDLE_BUCKETS)])
    with mock_reads(sessions=[], buckets=idle_buckets):
        idle_trend = compute_idle_metric_period_trend(scalar_sensor_ref("cpu_temp"), 30, now=NOW)
    assert idle_trend is not None and idle_trend["direction"] == "WORSENING"
    assert abs(idle_trend["older_mean"] - 40.0) < 1e-6 and abs(idle_trend["recent_mean"] - 44.0) < 1e-6
    too_few_buckets = idle_buckets[:BASELINE_MIN_IDLE_BUCKETS // 2] + idle_buckets[-5:]
    with mock_reads(sessions=[], buckets=too_few_buckets):
        thin_idle = compute_idle_metric_period_trend(scalar_sensor_ref("cpu_temp"), 30, now=NOW)
    assert thin_idle is None, "FAIL: too few idle buckets in the recent half must yield None, never a guessed trend"
    print(f"  PASS: {BASELINE_MIN_IDLE_BUCKETS} buckets/period -> real trend (idle CPU {idle_trend['older_mean']:.0f}°C "
          f"-> {idle_trend['recent_mean']:.0f}°C); too few buckets -> None")

    print("\n=== 10. compute_incident_frequency_trend: coverage-gated, never 'fewer incidents' from an unwatched period ===")
    def incident(start_ts, max_zone):
        return {"start_timestamp": start_ts, "end_timestamp": start_ts + 60, "max_zone": max_zone}
    incidents = [incident(NOW - 20 * 86400, "RED"), incident(NOW - 5 * 86400, "RED"), incident(NOW - 3 * 86400, "RED")]
    good_coverage_buckets = [bucket(NOW - 30 * 86400 + i * 60, 50.0) for i in range(int(30 * 86400 / 60))]
    with mock_reads(incidents=incidents, buckets=good_coverage_buckets):
        freq = compute_incident_frequency_trend(30, max_zone="RED", now=NOW)
    assert freq is not None and freq["older_count"] == 1 and freq["recent_count"] == 2 and freq["direction"] == "WORSENING"
    with mock_reads(incidents=incidents, buckets=[]):
        no_coverage_freq = compute_incident_frequency_trend(30, max_zone="RED", now=NOW)
    assert no_coverage_freq is None, "FAIL: near-zero telemetry coverage must never be treated as evidence of a real count"
    print(f"  PASS: well-covered period -> {freq['older_count']} -> {freq['recent_count']} RED incidents "
          f"({freq['direction']}); near-zero coverage -> None, not a misleading '0 -> 0'")

    print("\n=== 11. compute_health_score_period_trend: each session scored against ITS OWN workload's peers ===")
    # workload A always runs hot (its own 'normal'); workload B always runs cool (its own
    # 'normal') - if scoring leaked across workloads, A's sessions would ALL look anomalous
    # against B's baseline and vice versa, corrupting both workloads' trend.
    hot_workload = ([session_fixture(f"hotA{i}", "hot.exe", NOW - (25 - i) * 86400, cpu_temp=85.0) for i in range(4)]
                    + [session_fixture(f"hotB{i}", "hot.exe", NOW - (10 - i) * 86400, cpu_temp=85.0) for i in range(4)])
    cool_workload = ([session_fixture(f"coolA{i}", "cool.exe", NOW - (25 - i) * 86400, cpu_temp=45.0) for i in range(4)]
                     + [session_fixture(f"coolB{i}", "cool.exe", NOW - (10 - i) * 86400, cpu_temp=45.0) for i in range(4)])
    mixed_trend = compute_health_score_period_trend(hot_workload + cool_workload, 30, now=NOW)
    assert mixed_trend is not None and mixed_trend["direction"] == "STABLE", \
        f"FAIL: both workloads are internally consistent (each matches its OWN normal) - mixed average must be STABLE, not skewed: {mixed_trend}"
    print(f"  PASS: two workloads with very different 'normal' temperatures, each internally stable, "
          f"average health-score trend correctly reports STABLE (no cross-workload contamination)")

    print("\n=== 12. compute_week_over_week_report / format: matches the WEEK OVER WEEK worked example's shape ===")
    py_wow = ([session_fixture(f"pyw_old{i}", "python.exe", NOW - (13 - i) * 86400, cpu_temp=72.0) for i in range(4)]
             + [session_fixture(f"pyw_new{i}", "python.exe", NOW - (6 - i) * 86400, cpu_temp=73.0) for i in range(4)])
    cp_wow = ([session_fixture(f"cpw_old{i}", "Cyberpunk2077.exe", NOW - (13 - i) * 86400, gpu_hotspot=85.0) for i in range(4)]
             + [session_fixture(f"cpw_new{i}", "Cyberpunk2077.exe", NOW - (6 - i) * 86400, gpu_hotspot=89.0) for i in range(4)])
    wow_incidents = [incident(NOW - 12 * 86400, "RED"), incident(NOW - 4 * 86400, "RED"), incident(NOW - 2 * 86400, "RED")]
    # Full 60s-spaced density (matches TELEMETRY_BUCKET_SECONDS) across the whole 14-day window,
    # so compute_coverage() sees genuine ~100% coverage in both halves, not an artificially sparse
    # mock that would trip the coverage gate for reasons unrelated to what this test checks.
    wow_buckets = [bucket(NOW - 14 * 86400 + i * 60, 43.0 if i < 10080 else 44.0) for i in range(20160)]
    # A third, absurd-valued workload with only 2 recent sessions (fewer than python.exe/
    # Cyberpunk's 4 each) - present in the pool to prove report-level workload isolation (the
    # report builder groups by workload BEFORE comparing) without displacing the real top-N picks.
    noise_workload = [session_fixture(f"noise{i}", "noise.exe", NOW - (3 - i) * 86400, cpu_temp=999.0) for i in range(2)]
    with mock_reads(sessions=py_wow + cp_wow + noise_workload, incidents=wow_incidents, buckets=wow_buckets):
        wow_report = compute_week_over_week_report(now=NOW)
    wow_lines = format_week_over_week_report(wow_report)
    assert wow_lines[0] == "WEEK OVER WEEK"
    joined = "\n".join(wow_lines)
    assert "CPU average under python.exe: 72" in joined, joined
    assert "GPU Hotspot under Cyberpunk2077.exe: 85" in joined, joined
    assert "999" not in joined, f"FAIL: an absurd-valued third workload's data leaked into the report:\n{joined}"
    assert "Critical incidents: 1 → 2" in joined, joined
    print(f"  PASS: all {len(wow_lines) - 1} WEEK OVER WEEK lines present and correctly matched-per-workload:")
    for line in wow_lines:
        print(f"    {line}")

    print("\n=== 13. compute_workload_cooling_trend_report / format: matches the GPU COOLING worked example's shape ===")
    # Small per-session jitter (real sessions are never bit-for-bit identical) so a genuine
    # z-score computes instead of hitting the zero-stddev/absolute-fallback edge case, which caps
    # confidence at MEDIUM by design (see test 8) regardless of how large the delta is.
    # Power uses the SAME jitter pattern in both halves (no additive shift) so its mean is
    # genuinely, exactly unchanged - "flat power" here means truly flat, not just a small
    # absolute change that a tiny jitter's stddev would still flag as statistically "significant".
    cooling_sessions = ([session_fixture(f"c_old{i}", "Cyberpunk2077.exe", NOW - (25 - i) * 86400,
                                        gpu_core=65.0 + i * 0.05, gpu_hotspot=74.0 + i * 0.05, gpu_power=250.0 + i * 0.3)
                        for i in range(6)]
                        + [session_fixture(f"c_new{i}", "Cyberpunk2077.exe", NOW - (10 - i) * 86400,
                                          gpu_core=66.0 + i * 0.05, gpu_hotspot=78.3 + i * 0.05, gpu_power=250.0 + i * 0.3)
                          for i in range(6)])
    with mock_reads(sessions=cooling_sessions):
        cooling_report = compute_workload_cooling_trend_report("cyberpunk2077.exe", now=NOW)
    assert cooling_report["direction"] == "WORSENING" and cooling_report["confidence"] == "HIGH", cooling_report
    cooling_lines = format_workload_cooling_trend_report(cooling_report)
    assert cooling_lines[0] == "GPU COOLING — 30 DAY TREND"
    assert any(l.startswith("Hotspot under comparable") for l in cooling_lines)
    assert cooling_lines[-2] == "Trend: WORSENING" and cooling_lines[-1] == "Confidence: HIGH"
    print("  PASS: all GPU COOLING — 30 DAY TREND lines present, matches the worked example's shape:")
    for line in cooling_lines:
        print(f"    {line}")

    print("\n=== 14. compute_workload_cooling_trend_report: power ALSO rising downgrades confidence one tier ===")
    corroborated_sessions = ([session_fixture(f"cor_old{i}", "game2.exe", NOW - (25 - i) * 86400,
                                              gpu_core=65.0 + i * 0.05, gpu_hotspot=74.0 + i * 0.05, gpu_power=250.0 + i * 0.3)
                             for i in range(6)]
                             + [session_fixture(f"cor_new{i}", "game2.exe", NOW - (10 - i) * 86400,
                                               gpu_core=66.0 + i * 0.05, gpu_hotspot=78.3 + i * 0.05, gpu_power=250.0 + i * 0.3)
                               for i in range(6)])
    uncorroborated_sessions = ([session_fixture(f"unc_old{i}", "game3.exe", NOW - (25 - i) * 86400,
                                                gpu_core=65.0 + i * 0.05, gpu_hotspot=74.0 + i * 0.05, gpu_power=250.0 + i * 0.1)
                               for i in range(6)]
                               + [session_fixture(f"unc_new{i}", "game3.exe", NOW - (10 - i) * 86400,
                                                 gpu_core=70.0 + i * 0.05, gpu_hotspot=78.3 + i * 0.05, gpu_power=400.0 + i * 0.1)
                                 for i in range(6)])
    with mock_reads(sessions=corroborated_sessions):
        cor_report = compute_workload_cooling_trend_report("game2.exe", now=NOW)
    with mock_reads(sessions=uncorroborated_sessions):
        unc_report = compute_workload_cooling_trend_report("game3.exe", now=NOW)
    assert cor_report["confidence"] == "HIGH"
    assert unc_report["confidence"] in ("MEDIUM", "LOW"), \
        f"FAIL: power ALSO rising must downgrade confidence below the power-flat case: {unc_report}"
    print(f"  PASS: power flat + hotspot rise -> {cor_report['confidence']} confidence; "
          f"power ALSO rose alongside hotspot -> downgraded to {unc_report['confidence']}")

    print("\n=== 15. compute_workload_cooling_trend_report: unknown workload -> None, formats as an honest message ===")
    with mock_reads(sessions=[]):
        none_report = compute_workload_cooling_trend_report("nonexistent.exe", now=NOW)
    assert none_report is None
    assert format_workload_cooling_trend_report(none_report) == ["No recorded sessions for this workload yet"]
    print("  PASS: an unknown/never-seen workload key returns None, formats as an explicit message")

    print("\n=== 16. TrendsWindow: opens via HistoryWindow (singleton pattern), renders both reports ===")
    fresh_files()
    all_fixture_sessions = py_wow + cp_wow + cooling_sessions
    # Retention-safe: App() prunes sessions older than SESSION_RETENTION_DAYS (30) at startup -
    # every fixture above is <=25 days old, comfortably inside that window.
    with SESSIONS_PATH.open("w", encoding="utf-8") as fh:
        for s in all_fixture_sessions:
            fh.write(json.dumps(s) + "\n")
    app = App()
    hw = HistoryWindow(app)
    hw.open_trends()
    app.update()
    win1 = hw.trends_window
    hw.open_trends()  # must reuse the same window, not open a second one
    assert hw.trends_window is win1, "FAIL: open_trends() must reuse the existing window (singleton pattern)"
    wow_text = win1.wow_text.cget("text")
    assert "WEEK OVER WEEK" in wow_text and "python.exe" in wow_text, f"FAIL:\n{wow_text}"
    display_names = list(win1._workload_keys.keys())
    assert any(n.casefold() == "cyberpunk2077.exe" for n in display_names), display_names
    cp_name = next(n for n in display_names if n.casefold() == "cyberpunk2077.exe")
    win1._on_workload_change(cp_name)
    app.update()
    trend_text = win1.trend_text.cget("text")
    assert "GPU COOLING" in trend_text and "WORSENING" in trend_text, f"FAIL:\n{trend_text}"
    win1.destroy()
    hw.destroy()
    app.stop_event.set(); app.destroy()
    print("  PASS: TrendsWindow opens as a singleton from History, WEEK OVER WEEK and per-workload "
          "GPU COOLING trend both render real computed data through the full UI stack")

    print("\n=== 17. Trend Intelligence never runs on the live 2s poll or on session/app close ===")
    forbidden = ("compute_week_over_week_report(", "compute_workload_cooling_trend_report(",
                "compute_workload_period_trend(", "compute_idle_metric_period_trend(",
                "compute_incident_frequency_trend(", "compute_health_score_period_trend(")
    update_src = inspect.getsource(App.update_data)
    close_src = inspect.getsource(App.close)
    for name in forbidden:
        assert name not in update_src, f"FAIL: update_data() must never call {name} on the 2s poll"
        assert name not in close_src, f"FAIL: trend computation must never run automatically on session/app close"
    print("  PASS: update_data()/close() contain no trend computation - display-only, on-demand")

    fresh_files()
    print("\nALL TREND INTELLIGENCE CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
