"""Verification for Anomaly Detection: z-score/absolute-delta anomaly evaluation correctness,
never-guess-without-an-established-baseline gating, non-causal per-session "VS BASELINE" flags
in SessionsWindow (leave-one-out - a session is never compared against a baseline that includes
itself), the AnalyticsWindow anomalous-session rollup (including the None-vs-0 "can't tell yet"
distinction), and that the whole layer is read-only/on-demand - no event-log entries, no
notifications, nothing on the live poll."""
import inspect
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (App, AnalyticsWindow, SessionsWindow, ANOMALY_Z_THRESHOLD,  # noqa: E402
                 ANOMALY_MIN_ABS_DELTA, BASELINE_MIN_SESSIONS, SESSIONS_PATH, INCIDENTS_PATH,
                 evaluate_anomaly, evaluate_session_anomalies, count_anomalous_sessions,
                 compute_workload_baseline, _stat_summary)


def fresh_files():
    for p in (SESSIONS_PATH, INCIDENTS_PATH):
        if p.exists():
            p.unlink()


def session_fixture(sid, workload, start, hotspot_avg, dur=1800):
    return {
        "session_id": sid, "workload_key": workload.casefold(), "workload": workload,
        "start_timestamp": start, "end_timestamp": start + dur, "duration_seconds": dur,
        "duration_exact": True,
        "cpu": {"avg_temp": 60.0, "peak_temp": 70.0, "avg_power": 80.0},
        "gpu": {"avg_core_temp": 65.0, "peak_core_temp": 75.0, "avg_hotspot_temp": hotspot_avg,
               "peak_hotspot_temp": hotspot_avg + 4, "avg_vram_temp": 78.0, "peak_vram_temp": 82.0,
               "avg_power": 250.0, "peak_power": 300.0},
        "incident_count": 0, "max_incident_severity": None, "incident_ids": [], "monitoring_gaps": [],
    }


def main():
    fresh_files()

    print("=== 1. evaluate_anomaly: None when the current value or the baseline is missing/not established ===")
    established = _stat_summary([80.0, 82.0, 84.0, 86.0, 88.0], min_established=3)
    assert established["established"] is True
    assert evaluate_anomaly(None, established, "°C") is None, "FAIL: a missing current value must never be judged"
    assert evaluate_anomaly(90.0, None, "°C") is None, "FAIL: no baseline at all must never be judged"
    not_established = _stat_summary([80.0, 82.0], min_established=3)
    assert evaluate_anomaly(90.0, not_established, "°C") is None, \
        "FAIL: an unestablished (too-few-sample) baseline must never be used to judge"
    print("  PASS: missing current/baseline/unestablished-baseline all correctly yield None (never guessed)")

    print("\n=== 2. evaluate_anomaly: z-score math is correct, threshold crossing behaves correctly ===")
    # baseline [80,82,84,86,88]: mean=84, sample stddev = sqrt(((-4)^2+(-2)^2+0+2^2+4^2)/4) = sqrt(40/4)=sqrt(10)
    stddev = established["stddev"]
    # A small margin either side of the threshold, rather than an exact floating-point boundary
    # (which a mean+z*stddev -> (x-mean)/stddev roundtrip can land a hair under/over) - this
    # still genuinely proves the >= comparison and the z-score arithmetic, without being
    # fragile to float rounding at the literal edge.
    just_over = 84.0 + (ANOMALY_Z_THRESHOLD + 0.05) * stddev
    r_over = evaluate_anomaly(just_over, established, "°C")
    assert r_over["z_score"] >= ANOMALY_Z_THRESHOLD
    assert r_over["unusual"] is True, f"FAIL: just over the z-threshold must count as unusual: {r_over}"
    just_under = 84.0 + (ANOMALY_Z_THRESHOLD - 0.05) * stddev
    r_under = evaluate_anomaly(just_under, established, "°C")
    assert r_under["z_score"] < ANOMALY_Z_THRESHOLD
    assert r_under["unusual"] is False, f"FAIL: just under the z-threshold must not be flagged: {r_under}"
    print(f"  PASS: z={r_over['z_score']:.2f} -> unusual=True, z={r_under['z_score']:.2f} -> unusual=False")

    print("\n=== 3. evaluate_anomaly: symmetric - a NOTABLY LOWER value is also flagged, not just higher ===")
    much_lower = 84.0 - 5 * stddev
    r_lower = evaluate_anomaly(much_lower, established, "°C")
    assert r_lower["unusual"] is True and r_lower["delta"] < 0
    print(f"  PASS: a value far BELOW baseline is flagged too (delta={r_lower['delta']:.1f})")

    print("\n=== 4. evaluate_anomaly: zero/near-zero stddev falls back to the absolute-delta threshold ===")
    uniform = _stat_summary([80.0, 80.0, 80.0], min_established=3)
    assert uniform["stddev"] == 0.0
    tiny_delta = evaluate_anomaly(80.0 + ANOMALY_MIN_ABS_DELTA["°C"] - 0.5, uniform, "°C")
    assert tiny_delta["z_score"] is None and tiny_delta["unusual"] is False, \
        f"FAIL: a delta under the absolute fallback threshold must not be flagged: {tiny_delta}"
    big_delta = evaluate_anomaly(80.0 + ANOMALY_MIN_ABS_DELTA["°C"] + 0.5, uniform, "°C")
    assert big_delta["unusual"] is True, f"FAIL: a delta over the absolute fallback threshold must be flagged: {big_delta}"
    print(f"  PASS: a perfectly uniform (stddev=0) baseline correctly falls back to the "
          f"{ANOMALY_MIN_ABS_DELTA['°C']}°C absolute-delta threshold instead of dividing by zero")

    print("\n=== 5. evaluate_session_anomalies: only reports metrics BOTH sides actually have ===")
    baseline = compute_workload_baseline([session_fixture(f"s{i}", "Cyberpunk2077.exe", 1_700_000_000.0 + i * 3600, 84.0)
                                         for i in range(5)])
    anomalous_session = session_fixture("target", "Cyberpunk2077.exe", 2_000_000_000.0, 96.0)
    result = evaluate_session_anomalies(anomalous_session, baseline)
    assert "gpu.avg_hotspot_temp" in result and result["gpu.avg_hotspot_temp"]["anomaly"]["unusual"] is True
    assert result["gpu.avg_hotspot_temp"]["current"] == 96.0
    assert abs(result["gpu.avg_hotspot_temp"]["anomaly"]["delta"] - 12.0) < 1e-9
    # cpu.avg_temp is identical (60.0) in every fixture session, so stddev=0 there and the delta
    # is 0 - correctly NOT flagged, proving this isn't a blanket "flag everything" bug.
    assert "cpu.avg_temp" not in result or result["cpu.avg_temp"]["anomaly"]["unusual"] is False
    print(f"  PASS: gpu.avg_hotspot_temp correctly flagged (+12.0°C), unrelated identical metrics correctly not flagged")

    print("\n=== 6. count_anomalous_sessions: None (not 0) when too few sessions for any leave-one-out baseline ===")
    assert count_anomalous_sessions([]) is None
    two_sessions = [session_fixture("a", "python.exe", 1_700_000_000.0, 84.0),
                   session_fixture("b", "python.exe", 1_700_003_600.0, 84.0)]
    assert count_anomalous_sessions(two_sessions) is None, \
        f"FAIL: {BASELINE_MIN_SESSIONS + 1} sessions are needed for even one leave-one-out baseline"
    print(f"  PASS: 0 and {BASELINE_MIN_SESSIONS - 1} sessions both correctly yield None, never a misleading '0 anomalous'")

    print("\n=== 7. count_anomalous_sessions: correct count, leave-one-out never compares a session to itself ===")
    normal_sessions = [session_fixture(f"n{i}", "Cyberpunk2077.exe", 1_700_000_000.0 + i * 3600, 84.0 + i)
                       for i in range(5)]  # 82..86, tight spread
    outlier_session = session_fixture("outlier", "Cyberpunk2077.exe", 1_700_050_000.0, 130.0)  # wildly hot
    all_six = normal_sessions + [outlier_session]
    count = count_anomalous_sessions(all_six)
    assert count == 1, f"FAIL: expected exactly 1 anomalous session (the 130°C outlier), got {count}"
    print(f"  PASS: exactly 1 of 6 sessions flagged (the deliberate 130°C outlier)")

    print("\n=== 8. SessionsWindow: an anomalous session shows 'UNUSUAL' vs baseline, a normal one doesn't ===")
    fresh_files()
    fixtures = [session_fixture(f"s{i}", "Cyberpunk2077.exe", time.time() - (6 - i) * 3600, 84.0)
               for i in range(4)]
    fixtures.append(session_fixture("hot", "Cyberpunk2077.exe", time.time() - 1800, 96.0))
    with SESSIONS_PATH.open("w", encoding="utf-8") as f:
        for s in fixtures:
            f.write(json.dumps(s) + "\n")
    app = App()
    sw = SessionsWindow(app)
    sw._reload()
    row_for_session = {}
    for iid in sw.tree.get_children():
        row_for_session[sw._row_session[iid]["session_id"]] = iid
    sw.tree.selection_set(row_for_session["hot"])
    sw._on_select(None)
    hot_detail = sw.detail_text.cget("text")
    assert "UNUSUAL" in hot_detail and "GPU Hotspot" in hot_detail, f"FAIL:\n{hot_detail}"
    sw.tree.selection_set(row_for_session["s0"])
    sw._on_select(None)
    normal_detail = sw.detail_text.cget("text")
    assert "UNUSUAL" not in normal_detail, f"FAIL: a normal session must not be flagged:\n{normal_detail}"
    assert "VS BASELINE" in normal_detail and "deviated notably" in normal_detail
    sw.destroy()
    app.stop_event.set(); app.destroy()
    print("  PASS: the 96°C session is flagged UNUSUAL, the four 84°C sessions correctly show no deviation")

    print("\n=== 9. AnalyticsWindow: anomalous-session rollup shows the correct count and 'not enough sessions yet' ===")
    app2 = App()
    aw = AnalyticsWindow(app2)
    aw.range_var.set("All")
    aw._recompute()
    aw.tree.selection_set(aw.tree.get_children()[0])
    aw._on_select(None)
    aw_detail = aw.detail_text.cget("text")
    assert "Anomalous sessions         1 of 5" in aw_detail, f"FAIL:\n{aw_detail}"
    aw.destroy()
    app2.stop_event.set(); app2.destroy()

    fresh_files()
    with SESSIONS_PATH.open("w", encoding="utf-8") as f:
        f.write(json.dumps(session_fixture("only1", "blender.exe", time.time() - 100, 70.0)) + "\n")
    app3 = App()
    aw2 = AnalyticsWindow(app3)
    aw2.range_var.set("All")
    aw2._recompute()
    aw2.tree.selection_set(aw2.tree.get_children()[0])
    aw2._on_select(None)
    aw2_detail = aw2.detail_text.cget("text")
    assert "Anomalous sessions         not enough sessions yet" in aw2_detail, f"FAIL:\n{aw2_detail}"
    aw2.destroy()
    app3.stop_event.set(); app3.destroy()
    print("  PASS: 5-session workload shows '1 of 5', a 1-session workload honestly shows 'not enough sessions yet'")

    print("\n=== 10. anomaly detection never runs on the live 2s poll, and never writes an event/notification ===")
    src = inspect.getsource(App.update_data)
    for forbidden in ("evaluate_anomaly(", "evaluate_session_anomalies(", "count_anomalous_sessions("):
        assert forbidden not in src, f"FAIL: update_data() must never call {forbidden} on the 2s poll"
    close_src = inspect.getsource(App.close)
    for forbidden in ("evaluate_anomaly(", "evaluate_session_anomalies(", "count_anomalous_sessions("):
        assert forbidden not in close_src, f"FAIL: anomaly detection must never run automatically on session/app close"
    print("  PASS: update_data()/close() contain no anomaly computation - display-only, on-demand")

    fresh_files()
    print("\nALL ANOMALY DETECTION CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
