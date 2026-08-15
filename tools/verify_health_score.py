"""Verification for Transparent Health Scoring: the 0-100 session score is derived ONLY from
already-measured signals (zone time, incidents, anomaly detection, cross-sensor diagnostics),
every point lost has a fixed documented weight (never gamified/random/ML), the score is NEVER
shown without its full breakdown, a quiet session scores 100, session-to-session TREND findings
never count against an individual session's own score, the workload-level average is a plain mean
of already-computed per-session numbers, wiring into SessionsWindow/AnalyticsWindow, and that the
whole layer stays read-only/on-demand - never the 2s poll."""
import inspect
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (  # noqa: E402
    App, SessionsWindow, AnalyticsWindow, SESSIONS_PATH, INCIDENTS_PATH,
    compute_session_health_score, compute_workload_health_average,
    compute_workload_session_health_scores, health_score_label, format_health_score,
    HEALTH_SCORE_MAX, run_session_diagnostics, evaluate_session_anomalies, compute_workload_baseline,
)


def fresh_files():
    for p in (SESSIONS_PATH, INCIDENTS_PATH):
        if p.exists():
            p.unlink()


def session_fixture(sid, workload, start, dur=1800, zone_time=None, incident_count=0,
                    max_incident_severity=None, cpu_temp=60.0, cpu_power=80.0, gpu_core=65.0,
                    gpu_hotspot=77.0, gpu_power=250.0):
    return {
        "session_id": sid, "workload_key": workload.casefold(), "workload": workload,
        "start_timestamp": start, "end_timestamp": start + dur, "duration_seconds": dur,
        "duration_exact": True,
        "cpu": {"avg_temp": cpu_temp, "peak_temp": cpu_temp + 10, "avg_power": cpu_power},
        "gpu": {"avg_core_temp": gpu_core, "peak_core_temp": gpu_core + 10,
               "avg_hotspot_temp": gpu_hotspot, "peak_hotspot_temp": gpu_hotspot + 4,
               "avg_vram_temp": 78.0, "peak_vram_temp": 82.0,
               "avg_power": gpu_power, "peak_power": gpu_power + 50},
        "zone_time": zone_time or {},
        "incident_count": incident_count, "max_incident_severity": max_incident_severity,
        "incident_ids": [], "monitoring_gaps": [],
    }


def main():
    fresh_files()
    NOW = time.time()
    BASE = NOW - 200 * 3600

    print("=== 1. A quiet session (no zone time, no incidents, no anomalies, no findings) scores 100 ===")
    quiet = session_fixture("q1", "idle.exe", BASE)
    result = compute_session_health_score(quiet)
    assert result["score"] == HEALTH_SCORE_MAX, f"FAIL: expected 100, got {result['score']}"
    assert result["label"] == "EXCELLENT"
    assert result["deductions"] == []
    print(f"  PASS: score={result['score']:.0f}, label={result['label']}, deductions={result['deductions']}")

    print("\n=== 2. Zone time deducts proportionally to the FRACTION of the session spent there ===")
    half_red = session_fixture("r1", "game.exe", BASE, dur=1000,
                               zone_time={"gpu_hotspot": {"YELLOW": 0.0, "ORANGE": 0.0, "RED": 500.0}})
    r = compute_session_health_score(half_red)
    assert len(r["deductions"]) == 1
    assert abs(r["deductions"][0]["points"] - 17.5) < 1e-6, r["deductions"]  # 50% of session * 35.0 weight
    assert abs(r["score"] - 82.5) < 1e-6, r["score"]
    full_red = session_fixture("r2", "game.exe", BASE, dur=1000,
                               zone_time={"gpu_hotspot": {"YELLOW": 0.0, "ORANGE": 0.0, "RED": 1000.0}})
    r_full = compute_session_health_score(full_red)
    assert abs(r_full["deductions"][0]["points"] - 35.0) < 1e-6
    print(f"  PASS: 50% of session in RED -> -{r['deductions'][0]['points']:.1f} (score {r['score']:.0f}), "
          f"100% in RED -> -{r_full['deductions'][0]['points']:.1f} (score {r_full['score']:.0f})")

    print("\n=== 3. Multiple simultaneously-affected components/zones ALL show up - never hidden/merged ===")
    multi = session_fixture("m1", "game.exe", BASE, dur=1000,
                            zone_time={"cpu": {"YELLOW": 0.0, "ORANGE": 200.0, "RED": 0.0},
                                      "gpu_hotspot": {"YELLOW": 0.0, "ORANGE": 0.0, "RED": 300.0}})
    r_multi = compute_session_health_score(multi)
    assert len(r_multi["deductions"]) == 2, f"FAIL: expected 2 separate deduction lines, got {r_multi['deductions']}"
    reasons = {d["reason"] for d in r_multi["deductions"]}
    assert any("CPU" in x and "Orange" in x for x in reasons)
    assert any("GPU Hotspot" in x and "Red" in x for x in reasons)
    print(f"  PASS: two independently-affected components -> two separate, labeled deduction lines")

    print("\n=== 4. Incidents deduct by count x highest-severity weight; zero incidents deducts nothing ===")
    incidented = session_fixture("i1", "game.exe", BASE, incident_count=3, max_incident_severity="ORANGE")
    r_inc = compute_session_health_score(incidented)
    assert abs(r_inc["deductions"][0]["points"] - 15.0) < 1e-6, r_inc["deductions"]  # 3 * 5.0
    assert "3 associated incident" in r_inc["deductions"][0]["reason"] and "Orange" in r_inc["deductions"][0]["reason"]
    no_incidents = session_fixture("i2", "game.exe", BASE, incident_count=0, max_incident_severity=None)
    assert compute_session_health_score(no_incidents)["deductions"] == []
    print(f"  PASS: 3 ORANGE incidents -> -{r_inc['deductions'][0]['points']:.0f}, 0 incidents -> no deduction")

    print("\n=== 5. Anomalies: a missing baseline (anomalies=None) contributes 0, never a penalty for missing data ===")
    plain = session_fixture("a1", "game.exe", BASE)
    assert compute_session_health_score(plain, anomalies=None)["score"] == HEALTH_SCORE_MAX
    baseline_sessions = [session_fixture(f"b{i}", "game.exe", BASE + i * 3600, cpu_temp=60.0) for i in range(5)]
    hot = session_fixture("hot", "game.exe", BASE + 100_000, cpu_temp=80.0)
    baseline = compute_workload_baseline(baseline_sessions)
    anomalies = evaluate_session_anomalies(hot, baseline)
    r_anom = compute_session_health_score(hot, anomalies=anomalies)
    assert any("deviated notably" in d["reason"] for d in r_anom["deductions"]), r_anom["deductions"]
    print(f"  PASS: anomalies=None -> 0 deduction (never penalized for missing baseline); "
          f"a real 20°C outlier -> {[d for d in r_anom['deductions'] if 'deviated' in d['reason']]}")

    print("\n=== 6. Cross-sensor diagnostic findings deduct by their own Confidence tier (HIGH > MEDIUM) ===")
    high_finding = {"title": "GPU COOLING PATTERN — UNUSUAL", "confidence": "HIGH",
                    "evidence": [], "interpretation": "x"}
    medium_finding = {"title": "CPU THERMAL PATTERN — UNUSUAL", "confidence": "MEDIUM",
                      "evidence": [], "interpretation": "x"}
    r_diag = compute_session_health_score(plain, diagnostic_findings=[high_finding, medium_finding])
    points = {d["reason"]: d["points"] for d in r_diag["deductions"]}
    assert any("HIGH" in k for k in points) and any("MEDIUM" in k for k in points)
    high_pts = next(v for k, v in points.items() if "HIGH" in k)
    med_pts = next(v for k, v in points.items() if "MEDIUM" in k)
    assert high_pts > med_pts, f"FAIL: HIGH confidence must cost more than MEDIUM: {points}"
    print(f"  PASS: HIGH finding -{high_pts:.0f}, MEDIUM finding -{med_pts:.0f}")

    print("\n=== 7. Score never goes below 0 or above 100, regardless of deduction total ===")
    catastrophic = session_fixture("cat", "game.exe", BASE, dur=1000, incident_count=50,
                                   max_incident_severity="RED",
                                   zone_time={"cpu": {"YELLOW": 0, "ORANGE": 0, "RED": 1000.0},
                                             "gpu_hotspot": {"YELLOW": 0, "ORANGE": 0, "RED": 1000.0}})
    r_cat = compute_session_health_score(catastrophic)
    assert r_cat["score"] == 0.0 and r_cat["label"] == "CRITICAL"
    assert sum(d["points"] for d in r_cat["deductions"]) > 100
    print(f"  PASS: a deliberately catastrophic session clamps to 0/CRITICAL, "
          f"even though raw deductions total {sum(d['points'] for d in r_cat['deductions']):.0f}")

    print("\n=== 8. health_score_label bands match the documented thresholds ===")
    for score, expected in ((100, "EXCELLENT"), (90, "EXCELLENT"), (89.9, "GOOD"), (75, "GOOD"),
                            (74.9, "FAIR"), (55, "FAIR"), (54.9, "POOR"), (30, "POOR"), (29.9, "CRITICAL"), (0, "CRITICAL")):
        assert health_score_label(score) == expected, f"FAIL: {score} -> {health_score_label(score)}, expected {expected}"
    print("  PASS: all band boundaries (90/75/55/30) map to the documented label on both sides")

    print("\n=== 9. compute_workload_health_average: plain mean of already-computed scores, None when empty ===")
    assert compute_workload_health_average([]) is None
    avg = compute_workload_health_average([100.0, 80.0, 60.0])
    assert abs(avg["score"] - 80.0) < 1e-9 and avg["label"] == "GOOD"
    print(f"  PASS: [] -> None, [100,80,60] -> {avg['score']:.0f} ({avg['label']})")

    print("\n=== 10. compute_workload_session_health_scores: leave-one-out, one score per session ===")
    scores = compute_workload_session_health_scores(baseline_sessions + [hot])
    assert len(scores) == 6
    # the 80°C outlier's own score must be lower than the five uniform 60°C baseline sessions
    assert scores[-1] < min(scores[:-1]), f"FAIL: the outlier session should score lowest: {scores}"
    print(f"  PASS: 6 scores returned (one per session), outlier scores lowest: {[round(s) for s in scores]}")

    print("\n=== 11. format_health_score: always shows score+label; deductions sorted worst-first; 'No deductions' when quiet ===")
    lines_quiet = format_health_score(compute_session_health_score(quiet))
    assert lines_quiet[0].startswith("HEALTH SCORE: 100/100")
    assert "No deductions" in lines_quiet[1]
    lines_multi = format_health_score(r_multi)
    assert lines_multi[0].startswith(f"HEALTH SCORE: {r_multi['score']:.0f}/100")
    pts = [float(l.split()[0].lstrip("-")) for l in lines_multi[1:]]
    assert pts == sorted(pts, reverse=True), f"FAIL: deductions must be worst-first: {lines_multi}"
    print("  PASS: score+label always first, deductions worst-first, quiet session says 'No deductions'")

    print("\n=== 12. run_session_diagnostics feeds the score, but run_session_trend_diagnostics never does ===")
    src = inspect.getsource(compute_session_health_score)
    assert "run_session_trend_diagnostics(" not in src, \
        "FAIL: compute_session_health_score must never call the trend-finding function itself"
    trend_finding = {"title": "SESSION TREND — GPU HOTSPOT", "confidence": "HIGH", "evidence": [], "interpretation": "x"}
    with_trend = compute_session_health_score(plain, diagnostic_findings=[trend_finding])
    without_trend = compute_session_health_score(plain, diagnostic_findings=[])
    # a caller that accidentally passed a trend finding in would still see it deducted (the
    # function itself doesn't discriminate by title) - the actual exclusion happens at the
    # SessionsWindow call site, which is what the wiring check below verifies.
    assert with_trend["score"] < without_trend["score"]
    print("  PASS: the function deducts whatever findings it's given; SessionsWindow itself is what excludes trend findings (checked next)")

    print("\n=== 13. SessionsWindow: HEALTH SCORE section present, trend findings excluded from ITS deduction ===")
    fresh_files()
    trending = ([session_fixture(f"old{i}", "Game.exe", BASE + i * 3600, gpu_hotspot=70.0 + i * 0.1) for i in range(3)]
               + [session_fixture(f"new{i}", "Game.exe", BASE + 50_000 + i * 3600, gpu_hotspot=88.0 + i * 0.1) for i in range(3)])
    with SESSIONS_PATH.open("w", encoding="utf-8") as fh:
        for s in trending:
            fh.write(json.dumps(s) + "\n")
    app = App()
    sw = SessionsWindow(app)
    sw._reload()
    row_for_session = {sw._row_session[iid]["session_id"]: iid for iid in sw.tree.get_children()}
    sw.tree.selection_set(row_for_session["old0"])
    sw._on_select(None)
    detail = sw.detail_text.cget("text")
    assert "HEALTH SCORE:" in detail, f"FAIL:\n{detail}"
    assert "SESSION TREND" in detail, "FAIL: this session's own workload trend finding should still be VISIBLE in CROSS-SENSOR DIAGNOSTICS"
    # Recompute what the score SHOULD be if trend findings were (wrongly) included, to prove the
    # displayed score matches the trend-EXCLUDED calculation, not the trend-included one.
    old0 = row_for_session and sw._row_session[row_for_session["old0"]]
    others = [o for o in sw.all_sessions if o.get("workload_key") == "game.exe" and o.get("session_id") != "old0"]
    baseline_g = compute_workload_baseline(others)
    anomalies_g = evaluate_session_anomalies(old0, baseline_g)
    session_only_findings = run_session_diagnostics(old0, others, "Game.exe")
    expected = compute_session_health_score(old0, anomalies_g, session_only_findings)
    displayed_score_line = next(l for l in detail.split("\n") if l.startswith("HEALTH SCORE:"))
    assert f"{expected['score']:.0f}/100" in displayed_score_line, \
        f"FAIL: displayed score doesn't match the trend-excluded calculation:\n{displayed_score_line}\nexpected {expected}"
    sw.destroy()
    app.stop_event.set(); app.destroy()
    print(f"  PASS: HEALTH SCORE shown ({displayed_score_line.strip()}), matches the trend-EXCLUDED calculation, "
          f"while the trend finding itself still appears under CROSS-SENSOR DIAGNOSTICS")

    print("\n=== 14. AnalyticsWindow: workload detail shows the averaged health score, or 'no completed sessions yet' ===")
    fresh_files()
    with SESSIONS_PATH.open("w", encoding="utf-8") as fh:
        for s in baseline_sessions + [hot]:
            fh.write(json.dumps(s) + "\n")
    app2 = App()
    aw = AnalyticsWindow(app2)
    aw.range_var.set("All")
    aw._recompute()
    row = next(iid for iid in aw._row_stats if aw._row_stats[iid]["display_name"].casefold() == "game.exe")
    aw.tree.selection_set(row)
    aw._on_select(None)
    aw_detail = aw.detail_text.cget("text")
    assert "Health score" in aw_detail and "avg of this workload's own sessions" in aw_detail, f"FAIL:\n{aw_detail}"
    aw.destroy()
    app2.stop_event.set(); app2.destroy()
    print("  PASS: AnalyticsWindow shows the workload's averaged health score")

    print("\n=== 15. Health scoring never runs on the live 2s poll or on session/app close ===")
    update_src = inspect.getsource(App.update_data)
    close_src = inspect.getsource(App.close)
    forbidden = ("compute_session_health_score(", "compute_workload_health_average(",
                "compute_workload_session_health_scores(")
    for name in forbidden:
        assert name not in update_src, f"FAIL: update_data() must never call {name} on the 2s poll"
        assert name not in close_src, f"FAIL: health scoring must never run automatically on session/app close"
    print("  PASS: update_data()/close() contain no health-score computation - display-only, on-demand")

    fresh_files()
    print("\nALL HEALTH SCORE CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
