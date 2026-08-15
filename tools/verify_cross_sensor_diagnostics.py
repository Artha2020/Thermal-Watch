"""Verification for Cross-Sensor Diagnostics: PATTERN-level findings across two-or-more sensors
together (GPU Core<->Hotspot delta, CPU/GPU temp<->power, CPU/GPU thermal-ceiling<->fan/System-
sensor, session-to-session trend), the strictly evidence-based/non-causal HIGH-vs-MEDIUM
confidence split, correct "no finding at all" behavior when nothing is actually unusual (never a
padded entry), the live-only fan-RPM scope decision, wiring into SessionsWindow and
SensorHistoryWindow, and that the whole layer stays read-only/on-demand - never the 2s poll."""
import inspect
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (  # noqa: E402
    App, SessionsWindow, SensorHistoryWindow, SESSIONS_PATH, INCIDENTS_PATH,
    _diagnostic_confidence, compute_session_delta_baseline, diagnose_gpu_cooling_pattern,
    diagnose_temp_vs_power_pattern, diagnose_cpu_cooling_ceiling, diagnose_gpu_cooling_ceiling,
    diagnose_session_trend, run_session_diagnostics, run_session_trend_diagnostics,
    run_live_cooling_ceiling_diagnostics, format_diagnostic_finding, scalar_sensor_ref,
    _stat_summary, evaluate_anomaly, BASELINE_MIN_SESSIONS,
)


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
        "incident_count": 0, "max_incident_severity": None, "incident_ids": [], "monitoring_gaps": [],
    }


def main():
    fresh_files()
    # A real App() instance prunes any completed session past SESSION_RETENTION_DAYS (30) at
    # startup (load_sessions(), synchronous, mirrors load_incidents()) - fixture timestamps must
    # stay recent-relative-to-now, not an arbitrary fixed epoch, or App() would silently prune
    # them out from under tests 14/15 before SessionsWindow ever gets to read them back.
    NOW = time.time()
    BASE = NOW - 200 * 3600  # ~8.3 days ago - comfortably inside the 30-day retention window

    print("=== 1. _diagnostic_confidence: HIGH only for a large (>=3 sigma) AND corroborated signal ===")
    assert _diagnostic_confidence(3.5, True) == "HIGH"
    assert _diagnostic_confidence(3.5, False) == "MEDIUM", "FAIL: a large signal with no corroboration must not be HIGH"
    assert _diagnostic_confidence(2.5, True) == "MEDIUM", "FAIL: corroboration alone can't promote a modest signal to HIGH"
    assert _diagnostic_confidence(None, True) == "MEDIUM"
    print("  PASS: HIGH requires both a >=3 sigma signal AND corroboration; anything less is MEDIUM")

    print("\n=== 2. compute_session_delta_baseline: mean-of-differences, missing fields never fabricated ===")
    sessions = [session_fixture(f"s{i}", "Game.exe", BASE + i * 3600, gpu_core=65.0, gpu_hotspot=65.0 + d)
               for i, d in enumerate([10.0, 12.0, 14.0])]
    delta_baseline = compute_session_delta_baseline(sessions, "gpu", "avg_hotspot_temp", "avg_core_temp")
    assert abs(delta_baseline["mean"] - 12.0) < 1e-9, f"FAIL: expected mean delta 12.0, got {delta_baseline['mean']}"
    incomplete = [session_fixture("a", "x.exe", BASE)]
    incomplete[0]["gpu"]["avg_core_temp"] = None
    assert compute_session_delta_baseline(incomplete, "gpu", "avg_hotspot_temp", "avg_core_temp") is None
    print(f"  PASS: mean-of-per-session-deltas = {delta_baseline['mean']:.1f}°C; a session missing either side drops out, never treated as 0")

    print("\n=== 3. diagnose_gpu_cooling_pattern: wide delta + normal power -> corroborated finding ===")
    baseline_sessions = [session_fixture(f"b{i}", "Cyberpunk2077.exe", BASE + i * 3600,
                                         gpu_core=65.0, gpu_hotspot=65.0 + 13.0, gpu_power=250.0)
                        for i in range(5)]
    hot_session = session_fixture("hot", "Cyberpunk2077.exe", BASE + 100_000,
                                  gpu_core=74.0, gpu_hotspot=97.0, gpu_power=252.0)
    finding = diagnose_gpu_cooling_pattern(hot_session, baseline_sessions, "Cyberpunk2077.exe")
    assert finding is not None, "FAIL: a 23°C delta against a ~13°C baseline must be flagged"
    assert finding["title"] == "GPU COOLING PATTERN — UNUSUAL"
    assert "normal for this workload" in finding["interpretation"]
    assert finding["confidence"] in ("HIGH", "MEDIUM")
    assert any("Delta: 23" in e for e in finding["evidence"]), finding["evidence"]
    print(f"  PASS: GPU COOLING PATTERN flagged, confidence={finding['confidence']}, "
          f"evidence includes '{finding['evidence'][2]}'")

    print("\n=== 4. diagnose_gpu_cooling_pattern: wide delta + ALSO-elevated power -> weaker, still-flagged finding ===")
    hot_session_hot_power = session_fixture("hot2", "Cyberpunk2077.exe", BASE + 100_000,
                                            gpu_core=74.0, gpu_hotspot=97.0, gpu_power=400.0)
    finding2 = diagnose_gpu_cooling_pattern(hot_session_hot_power, baseline_sessions, "Cyberpunk2077.exe")
    assert finding2 is not None
    assert "cannot be ruled out" in finding2["interpretation"], finding2["interpretation"]
    assert finding2["confidence"] == "MEDIUM", "FAIL: an uncorroborated finding must never be HIGH"
    print("  PASS: power also elevated -> workload-intensity explicitly not ruled out, confidence capped at MEDIUM")

    print("\n=== 5. diagnose_gpu_cooling_pattern: a NARROWER-than-usual delta is not a cooling finding ===")
    narrow_session = session_fixture("narrow", "Cyberpunk2077.exe", BASE + 100_000,
                                     gpu_core=74.0, gpu_hotspot=79.0, gpu_power=250.0)  # delta=5, baseline~13
    assert diagnose_gpu_cooling_pattern(narrow_session, baseline_sessions, "Cyberpunk2077.exe") is None, \
        "FAIL: a narrower delta must never be reported as a cooling concern"
    normal_session = session_fixture("normal", "Cyberpunk2077.exe", BASE + 100_000,
                                     gpu_core=65.0, gpu_hotspot=78.0, gpu_power=250.0)  # delta=13, matches baseline
    assert diagnose_gpu_cooling_pattern(normal_session, baseline_sessions, "Cyberpunk2077.exe") is None
    print("  PASS: a narrower-than-usual or in-line delta produces no finding at all (never padded)")

    print("\n=== 6. diagnose_temp_vs_power_pattern: temp up + power normal -> finding; temp AND power up -> no finding ===")
    cpu_baseline_sessions = [session_fixture(f"c{i}", "blender.exe", BASE + i * 3600, cpu_temp=60.0, cpu_power=80.0)
                             for i in range(5)]
    elevated_temp_only = session_fixture("hot_cpu", "blender.exe", BASE + 100_000, cpu_temp=76.0, cpu_power=81.0)
    f_temp_only = diagnose_temp_vs_power_pattern("CPU THERMAL PATTERN", "cpu", "avg_temp", "CPU Package",
                                                 "avg_power", "CPU Power", elevated_temp_only,
                                                 cpu_baseline_sessions, "blender.exe")
    assert f_temp_only is not None and f_temp_only["title"] == "CPU THERMAL PATTERN — UNUSUAL"
    assert "normal for this workload" in f_temp_only["evidence"][-1]
    elevated_both = session_fixture("hot_both", "blender.exe", BASE + 100_000, cpu_temp=76.0, cpu_power=140.0)
    assert diagnose_temp_vs_power_pattern("CPU THERMAL PATTERN", "cpu", "avg_temp", "CPU Package", "avg_power",
                                          "CPU Power", elevated_both, cpu_baseline_sessions, "blender.exe") is None, \
        "FAIL: temperature AND power both elevated together is fully explained by workload intensity - must not flag"
    print("  PASS: temp-up/power-normal flagged; temp-up/power-ALSO-up correctly produces no finding")

    print("\n=== 7. diagnose_cpu_cooling_ceiling: reuses the EXISTING CPU zone thresholds, never a new one ===")
    calm_baseline = _stat_summary([39.0, 40.0, 41.0, 40.5, 39.5], min_established=1)
    assert diagnose_cpu_cooling_ceiling(70.0, 90.0, 1500.0, 41.0, calm_baseline) is None, \
        "FAIL: 70°C is GREEN/YELLOW zone - must not be treated as a ceiling finding"
    orange_finding = diagnose_cpu_cooling_ceiling(95.0, 190.0, 1520.0, 41.0, calm_baseline)
    assert orange_finding is not None and orange_finding["title"] == "CPU COOLING PATTERN"
    assert orange_finding["confidence"] == "MEDIUM", "FAIL: ORANGE (not RED) must never reach HIGH"
    assert any("1,520 RPM" in e for e in orange_finding["evidence"]), orange_finding["evidence"]
    red_finding = diagnose_cpu_cooling_ceiling(102.0, 195.0, 1500.0, 41.0, calm_baseline)
    assert red_finding["confidence"] == "HIGH", "FAIL: RED zone + corroborated System temp should reach HIGH"
    print(f"  PASS: 70°C -> no finding, 95°C(ORANGE) -> MEDIUM, 102°C(RED)+corroborated -> HIGH, "
          f"fan RPM formatted as evidence ('{orange_finding['evidence'][2]}')")

    print("\n=== 8. diagnose_cpu_cooling_ceiling: System temp ALSO elevated -> can't isolate, no finding ===")
    assert diagnose_cpu_cooling_ceiling(102.0, 195.0, 1500.0, 60.0, calm_baseline) is None, \
        "FAIL: a System sensor that's ALSO well above its own idle baseline must suppress this finding"
    print("  PASS: an elevated System/case temperature correctly suppresses the CPU-specific ceiling finding")

    print("\n=== 9. diagnose_cpu_cooling_ceiling: no System idle baseline yet -> finding still produced, uncorroborated ===")
    no_baseline_finding = diagnose_cpu_cooling_ceiling(102.0, 195.0, 1500.0, 41.0, None)
    assert no_baseline_finding is not None and no_baseline_finding["confidence"] == "MEDIUM"
    print("  PASS: missing System baseline never blocks the finding, but caps confidence at MEDIUM")

    print("\n=== 10. diagnose_gpu_cooling_ceiling: mirrors the CPU pattern using GPU_HOTSPOT_ZONES ===")
    assert diagnose_gpu_cooling_ceiling(80.0, 250.0, 60.0, 41.0, calm_baseline) is None
    gpu_orange = diagnose_gpu_cooling_ceiling(97.0, 260.0, 65.0, 41.0, calm_baseline)
    assert gpu_orange is not None and gpu_orange["title"] == "GPU THERMAL CEILING"
    assert any("65%" in e for e in gpu_orange["evidence"]), gpu_orange["evidence"]
    print(f"  PASS: 80°C -> no finding, 97°C(ORANGE) -> flagged as '{gpu_orange['title']}', "
          f"fan shown as a percentage not RPM")

    print("\n=== 11. diagnose_session_trend: needs real sessions on BOTH sides, flags a genuine directional shift ===")
    too_few = [session_fixture(f"t{i}", "Game.exe", BASE + i * 3600, gpu_hotspot=70.0 + i) for i in range(4)]
    assert diagnose_session_trend(too_few, "gpu", "avg_hotspot_temp", "GPU Hotspot", "°C", "Game.exe") is None, \
        "FAIL: 4 sessions can't fill both a 3-session older half AND a 3-session recent half"
    stable = [session_fixture(f"st{i}", "Game.exe", BASE + i * 3600, gpu_hotspot=70.0 + (i % 2))
             for i in range(8)]
    assert diagnose_session_trend(stable, "gpu", "avg_hotspot_temp", "GPU Hotspot", "°C", "Game.exe") is None, \
        "FAIL: sessions oscillating around the same value must not be reported as a trend"
    trending = ([session_fixture(f"old{i}", "Game.exe", BASE + i * 3600, gpu_hotspot=70.0 + i * 0.1) for i in range(3)]
               + [session_fixture(f"new{i}", "Game.exe", BASE + 50_000 + i * 3600, gpu_hotspot=88.0 + i * 0.1) for i in range(3)])
    trend_finding = diagnose_session_trend(trending, "gpu", "avg_hotspot_temp", "GPU Hotspot", "°C", "Game.exe")
    assert trend_finding is not None and "higher" in trend_finding["interpretation"]
    assert trend_finding["title"] == "SESSION TREND — GPU HOTSPOT"
    print(f"  PASS: 4 sessions -> None, stable 8 sessions -> None, "
          f"a genuine ~18°C older-vs-recent shift -> '{trend_finding['title']}' ({trend_finding['confidence']})")

    print("\n=== 12. run_session_diagnostics / run_session_trend_diagnostics: only real findings, never padded ===")
    quiet_session = session_fixture("quiet", "Cyberpunk2077.exe", BASE + 100_000, cpu_temp=60.0, cpu_power=80.0,
                                    gpu_core=65.0, gpu_hotspot=78.0, gpu_power=250.0)
    all_quiet_baseline = [session_fixture(f"q{i}", "Cyberpunk2077.exe", BASE + i * 3600)
                          for i in range(5)]
    assert run_session_diagnostics(quiet_session, all_quiet_baseline, "Cyberpunk2077.exe") == [], \
        "FAIL: a session matching its own workload's baseline in every dimension must yield zero findings"
    findings = run_session_diagnostics(hot_session, baseline_sessions, "Cyberpunk2077.exe")
    assert len(findings) >= 1 and any(f["title"].startswith("GPU COOLING") for f in findings)
    trend_findings = run_session_trend_diagnostics(trending, "Game.exe")
    assert len(trend_findings) == 1
    print(f"  PASS: an unremarkable session yields [], the deliberately hot session yields "
          f"{[f['title'] for f in findings]}")

    print("\n=== 13. format_diagnostic_finding: title / evidence / Interpretation: / Confidence: lines ===")
    lines = format_diagnostic_finding(finding)
    assert lines[0] == "GPU COOLING PATTERN — UNUSUAL"
    assert lines[-2].startswith("Interpretation: ")
    assert lines[-1] == f"Confidence: {finding['confidence']}"
    print("  PASS: line structure matches the worked-example format (title, evidence, Interpretation, Confidence)")

    print("\n=== 14. SessionsWindow: a session with a genuine cooling pattern shows it; a quiet one shows 'inconclusive' ===")
    fresh_files()
    # Two entirely separate, non-overlapping workloads: Cyberpunk2077.exe carries the deliberate
    # 97°C outlier (hot_session) so its OWN workload-level session-trend also legitimately shows a
    # finding for every session in that group (not a bug - the trend pattern is workload-scoped,
    # not leave-one-out like the anomaly/cooling-pattern checks). blender.exe's 5 sessions are all
    # perfectly uniform with no outlier anywhere, so it's the one that should show "inconclusive".
    fixtures = list(baseline_sessions) + [hot_session] + list(cpu_baseline_sessions)
    with SESSIONS_PATH.open("w", encoding="utf-8") as fh:
        for s in fixtures:
            fh.write(json.dumps(s) + "\n")
    app = App()
    sw = SessionsWindow(app)
    sw._reload()
    row_for_session = {}
    for iid in sw.tree.get_children():
        row_for_session[sw._row_session[iid]["session_id"]] = iid
    sw.tree.selection_set(row_for_session["hot"])
    sw._on_select(None)
    hot_detail = sw.detail_text.cget("text")
    assert "CROSS-SENSOR DIAGNOSTICS" in hot_detail and "GPU COOLING PATTERN" in hot_detail, f"FAIL:\n{hot_detail}"
    assert "Confidence:" in hot_detail
    sw.tree.selection_set(row_for_session["c0"])
    sw._on_select(None)
    quiet_detail = sw.detail_text.cget("text")
    assert "CROSS-SENSOR DIAGNOSTICS" in quiet_detail, f"FAIL:\n{quiet_detail}"
    assert "inconclusive" in quiet_detail, f"FAIL: an unremarkable session must say so explicitly:\n{quiet_detail}"
    sw.destroy()
    app.stop_event.set(); app.destroy()
    print("  PASS: the 97°C-hotspot session shows a GPU COOLING PATTERN block, a uniform blender.exe session says 'inconclusive'")

    print("\n=== 15. SensorHistoryWindow: live cooling-ceiling diagnostics appear ONLY on cpu_temp/gpu_hotspot_temp pages ===")
    fresh_files()
    app2 = App()
    app2.last_context = {"cpu_temp": 95.0, "cpu_power": 190.0, "cpu_fan_rpm": 1520.0,
                         "gpu_hotspot_temp": 97.0, "gpu_power": 260.0, "gpu_fan_pct": 65.0, "system_temp": 41.0}
    app2._lhm = []  # no System sensor found live -> uncorroborated finding, not a crash
    win_cpu = SensorHistoryWindow(app2, scalar_sensor_ref("cpu_temp"))
    cpu_summary = win_cpu.summary_label.cget("text")
    assert "DIAGNOSTICS (live)" in cpu_summary and "CPU COOLING PATTERN" in cpu_summary, f"FAIL:\n{cpu_summary}"
    win_cpu.destroy()

    win_gpu = SensorHistoryWindow(app2, scalar_sensor_ref("gpu_hotspot_temp"))
    gpu_summary = win_gpu.summary_label.cget("text")
    assert "GPU THERMAL CEILING" in gpu_summary, f"FAIL:\n{gpu_summary}"
    win_gpu.destroy()

    win_unrelated = SensorHistoryWindow(app2, scalar_sensor_ref("gpu_core_temp"))
    unrelated_summary = win_unrelated.summary_label.cget("text")
    assert "DIAGNOSTICS" not in unrelated_summary, \
        f"FAIL: a page this pattern doesn't apply to must never show a diagnostics block:\n{unrelated_summary}"
    win_unrelated.destroy()
    app2.stop_event.set(); app2.destroy()
    print("  PASS: CPU/GPU-hotspot pages show live diagnostics, an unrelated sensor page (GPU Core) shows none")

    print("\n=== 16. Cross-sensor diagnostics never runs on the live 2s poll or on session/app close ===")
    forbidden = ("diagnose_gpu_cooling_pattern(", "diagnose_temp_vs_power_pattern(", "diagnose_cpu_cooling_ceiling(",
                "diagnose_gpu_cooling_ceiling(", "diagnose_session_trend(", "run_session_diagnostics(",
                "run_session_trend_diagnostics(", "run_live_cooling_ceiling_diagnostics(")
    update_src = inspect.getsource(App.update_data)
    close_src = inspect.getsource(App.close)
    for name in forbidden:
        assert name not in update_src, f"FAIL: update_data() must never call {name} on the 2s poll"
        assert name not in close_src, f"FAIL: diagnostics must never run automatically on session/app close"
    print("  PASS: update_data()/close() contain no cross-sensor diagnostic calls - display-only, on-demand")

    fresh_files()
    print("\nALL CROSS-SENSOR DIAGNOSTICS CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
