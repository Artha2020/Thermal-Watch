"""Verification for the Predictive Maintenance Outlook: the layer refuses far more often than it
projects, never emits a precise countdown, aims only at EXISTING zone thresholds, inherits Trend
Intelligence's confidence rubric rather than recomputing one, caps its horizon to what the
observation window can support, and phrases everything as a conditional about an observed trend."""
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
sys.stdout.reconfigure(encoding="utf-8")

import app as appmod  # noqa: E402
from app import (  # noqa: E402
    App, HistoryWindow, MaintenanceWindow, CPU_ZONES, GPU_HOTSPOT_ZONES, CPU_ORANGE, CPU_YELLOW,
    PREDICTIVE_MIN_CONFIDENCE, PREDICTIVE_MAX_HORIZON_MULTIPLE, PREDICTIVE_MIN_RATE_PER_DAY,
    MAINTENANCE_CAVEAT, next_zone_threshold, project_threshold_horizon, compute_maintenance_outlook,
    format_maintenance_outlook, TREND_MONTH_LOOKBACK_DAYS,
)

NOW = time.time()


def trend(delta, confidence="HIGH", direction="WORSENING", stddev=1.0, recent_mean=70.0):
    """A compare_period_values()-shaped result, built directly so each rule can be probed in
    isolation without having to manufacture telemetry that happens to produce it."""
    return {"older_mean": recent_mean - delta, "recent_mean": recent_mean, "delta": delta,
           "z_score": 4.0, "direction": direction, "confidence": confidence,
           "n_older": 40, "n_recent": 40,
           "older_stats": {"count": 40, "mean": recent_mean - delta, "min": 0.0, "max": 0.0,
                          "stddev": stddev, "established": True}}


def main():
    print("=== 1. next_zone_threshold reads the EXISTING zone tables, and stops at the top zone ===")
    assert next_zone_threshold(50.0, CPU_ZONES) == {"threshold": CPU_YELLOW, "zone": "YELLOW"}
    assert next_zone_threshold(85.0, CPU_ZONES) == {"threshold": CPU_ORANGE, "zone": "ORANGE"}
    assert next_zone_threshold(200.0, CPU_ZONES) is None, \
        "FAIL: above the top zone there is nothing to aim at - a limit beyond RED must never be invented"
    assert next_zone_threshold(None, CPU_ZONES) is None
    assert next_zone_threshold(90.0, GPU_HOTSPOT_ZONES)["threshold"] == 95.0
    print("  PASS: thresholds come from CPU_ZONES/GPU_HOTSPOT_ZONES; already-RED and missing values yield None")

    print("\n=== 2. No projection without an established WORSENING trend ===")
    for direction in ("STABLE", "IMPROVING"):
        assert project_threshold_horizon(70.0, trend(5.0, direction=direction), 30, CPU_ZONES) is None
    assert project_threshold_horizon(70.0, None, 30, CPU_ZONES) is None
    print("  PASS: STABLE, IMPROVING and a missing trend all produce no projection at all")

    print("\n=== 3. Confidence is INHERITED from Trend Intelligence - LOW never projects ===")
    assert PREDICTIVE_MIN_CONFIDENCE == "MEDIUM"
    assert project_threshold_horizon(70.0, trend(5.0, confidence="LOW"), 30, CPU_ZONES) is None, \
        "FAIL: a LOW-confidence trend must never produce a projection"
    assert project_threshold_horizon(70.0, trend(5.0, confidence="MEDIUM"), 30, CPU_ZONES) is not None
    assert project_threshold_horizon(70.0, trend(5.0, confidence="HIGH"), 30, CPU_ZONES) is not None
    print("  PASS: LOW is refused; MEDIUM and HIGH are eligible - Trend Intelligence's own two-factor "
          "rubric (thin data caps at MEDIUM) therefore gates this layer for free")

    print("\n=== 4. THE anti-'17 days left' rule: the output is a coarse RANGE, never a precise countdown ===")
    proj = project_threshold_horizon(70.0, trend(5.0, stddev=1.0), 30, CPU_ZONES)
    assert proj["projected"] is True
    assert proj["days_soonest"] < proj["days_central"] < proj["days_latest"], proj
    assert isinstance(proj["bucket"], str) and any(w in proj["bucket"] for w in ("weeks", "months", "week"))
    lines = format_maintenance_outlook({"window_days": 30, "generated_timestamp": NOW, "entries": [
        {"key": "cpu_idle", "label": "CPU Package (idle)", "unit": "°C", "trend": trend(5.0), "projection": proj,
         "reason": None}]})
    joined = "\n".join(lines)
    assert "IF this trend continued unchanged" in joined, joined
    import re
    assert not re.search(r"\bin \d+ days\b", joined), f"FAIL: a precise day countdown appeared: {joined}"
    assert not re.search(r"\b\d+ days left\b", joined), joined
    print(f"  PASS: horizon renders as '{proj['bucket']}' with an explicit IF-continued conditional; "
          f"no '<n> days' countdown anywhere in the output")

    print("\n=== 5. The range widens with the trend's own dispersion, not with an invented factor ===")
    tight = project_threshold_horizon(70.0, trend(5.0, stddev=0.1), 30, CPU_ZONES)
    loose = project_threshold_horizon(70.0, trend(5.0, stddev=2.0), 30, CPU_ZONES)
    tight_span = tight["days_latest"] - tight["days_soonest"]
    loose_span = loose["days_latest"] - loose["days_soonest"]
    assert loose_span > tight_span * 3, (tight_span, loose_span)
    assert abs(tight["days_central"] - loose["days_central"]) < 1e-9, "the central estimate must be unchanged"
    print(f"  PASS: stddev 0.1 -> a {tight_span:.1f}-day span; stddev 2.0 -> {loose_span:.1f} days, with the "
          f"same central estimate - noisier evidence produces a wider, more honest range")

    print("\n=== 6. Horizon capping: a distant threshold is an explicit refusal, not a reassuring number ===")
    slow = project_threshold_horizon(50.0, trend(0.2, stddev=0.01), 14, CPU_ZONES)
    assert slow is not None and slow["projected"] is False and slow["reason"] == "beyond_horizon", slow
    assert slow["max_horizon_days"] == 14 * PREDICTIVE_MAX_HORIZON_MULTIPLE
    text = "\n".join(format_maintenance_outlook({"window_days": 14, "generated_timestamp": NOW, "entries": [
        {"key": "cpu_idle", "label": "CPU Package (idle)", "unit": "°C", "trend": trend(0.2),
         "projection": slow, "reason": None}]}))
    assert "No meaningful horizon" in text and "further away than" in text, text
    print(f"  PASS: a threshold {slow['max_horizon_days']:.0f}+ days out over a 14-day window is reported as "
          f"'No meaningful horizon', naming the data limit rather than implying safety")

    print("\n=== 7. A rise too small to distinguish from noise is not a trajectory ===")
    assert project_threshold_horizon(70.0, trend(PREDICTIVE_MIN_RATE_PER_DAY * 30 * 0.4), 30, CPU_ZONES) is None
    print(f"  PASS: a per-day rate below {PREDICTIVE_MIN_RATE_PER_DAY}°C/day yields no projection")

    print("\n=== 8. The rate uses HALF the window - the elapsed time between the two period means ===")
    p30 = project_threshold_horizon(70.0, trend(5.0, stddev=0.0), 30, CPU_ZONES)
    assert abs(p30["rate_per_day"] - (5.0 / 15.0)) < 1e-12, p30["rate_per_day"]
    gap = CPU_YELLOW - 70.0
    assert abs(p30["days_central"] - gap / (5.0 / 15.0)) < 1e-9, p30
    print(f"  PASS: a +5.0°C shift across a 30-day split is {p30['rate_per_day']:.4f}°C/day (5/15, not 5/30), "
          f"and the central horizon is exactly the remaining {gap:.0f}°C at that rate")

    print("\n=== 9. Already past the top zone, or already above the threshold: no projection ===")
    assert project_threshold_horizon(300.0, trend(5.0), 30, CPU_ZONES) is None
    print("  PASS: a value above every zone floor has nothing to project toward")

    print("\n=== 10. compute_maintenance_outlook: no idle telemetry -> honest refusal, never a guess ===")
    orig = appmod.compute_idle_metric_period_trend
    appmod.compute_idle_metric_period_trend = lambda ref, days, now=None: None
    try:
        empty = compute_maintenance_outlook(now=NOW)
    finally:
        appmod.compute_idle_metric_period_trend = orig
    assert len(empty["entries"]) == 2 and all(e["trend"] is None for e in empty["entries"])
    assert all(e["reason"] == "insufficient_data" for e in empty["entries"])
    empty_text = "\n".join(format_maintenance_outlook(empty))
    assert "Not enough idle telemetry yet" in empty_text
    assert "Nothing is currently projected to reach a thermal threshold." in empty_text
    assert MAINTENANCE_CAVEAT in empty_text, "the caveat belongs on every rendering, including an empty one"
    print("  PASS: with no idle history both metrics report insufficient data, the view says nothing is "
          "projected, and the caveat is still present")

    print("\n=== 11. A real worsening idle trend flows end-to-end into a projected horizon ===")
    def fake_trend(ref, days, now=None):
        return trend(6.0, confidence="HIGH", stddev=0.5,
                    recent_mean=76.0 if ref["key"] == "cpu_temp" else 88.0)
    appmod.compute_idle_metric_period_trend = fake_trend
    try:
        outlook = compute_maintenance_outlook(now=NOW)
    finally:
        appmod.compute_idle_metric_period_trend = orig
    cpu_entry = next(e for e in outlook["entries"] if e["key"] == "cpu_idle")
    gpu_entry = next(e for e in outlook["entries"] if e["key"] == "gpu_hotspot_idle")
    # 76°C's NEXT zone floor is YELLOW at 80°C - the projection aims at the boundary actually coming
    # up, not at the scariest one further along.
    assert cpu_entry["projection"]["projected"] is True, cpu_entry
    assert cpu_entry["projection"]["zone"] == "YELLOW" and cpu_entry["projection"]["threshold"] == CPU_YELLOW
    assert gpu_entry["projection"]["zone"] == "ORANGE" and gpu_entry["projection"]["threshold"] == 95.0
    full_text = "\n".join(format_maintenance_outlook(outlook))
    assert "MAINTENANCE OUTLOOK" in full_text and "IF this trend continued unchanged" in full_text
    assert MAINTENANCE_CAVEAT in full_text
    print("  PASS: CPU idle 76°C -> the NEXT boundary (YELLOW at 80°C) and GPU hotspot 88°C -> ORANGE at "
          "95°C, each with a banded horizon:")
    for line in full_text.splitlines()[:12]:
        print(f"    {line}")

    print("\n=== 12. Nothing here forecasts failure or names a component as failing ===")
    for phrase in ("will fail", "failure in", "days left", "expected to fail", "lifespan", "remaining life"):
        assert phrase not in full_text.lower(), f"FAIL: failure-forecast language '{phrase}'"
    assert "not predictions about the hardware" in MAINTENANCE_CAVEAT
    assert "does not forecast failure" in MAINTENANCE_CAVEAT
    print("  PASS: no failure-forecast or lifespan language anywhere; the caveat states plainly that this "
          "projects an observed trend rather than forecasting hardware failure")

    print("\n=== 13. No new statistics: the layer consumes a trend it never recomputes ===")
    import ast
    src = inspect.getsource(appmod)
    start = src.index("# Predictive Maintenance Outlook")
    end = src.index("class MEMORYSTATUSEX", start)
    layer = src[start:end]
    # Parse and look for real CALLS rather than substring-matching the text: the docstrings here
    # legitimately NAME compare_period_values when explaining where the trend comes from, and a
    # textual scan cannot tell a citation from an invocation.
    called = {node.func.id for node in ast.walk(ast.parse(layer))
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    for banned in ("_stat_summary", "evaluate_anomaly", "compare_period_values"):
        assert banned not in called, f"FAIL: the predictive layer recomputes statistics itself: {banned}()"
    assert "compute_idle_metric_period_trend" in called, "it must CONSUME Trend Intelligence's result"
    print(f"  PASS: the layer calls compute_idle_metric_period_trend and invokes none of "
          f"_stat_summary/evaluate_anomaly/compare_period_values (docstrings cite them; the AST shows "
          f"no call)")

    print("\n=== 14. MaintenanceWindow: singleton from History, renders, never runs on the 2s poll ===")
    app = App()
    hw = HistoryWindow(app)
    hw.open_maintenance()
    app.update()
    win = hw.maintenance_window
    hw.open_maintenance()
    assert hw.maintenance_window is win, "FAIL: open_maintenance() must reuse the existing window"
    rendered = win.text.cget("text")
    assert "MAINTENANCE OUTLOOK" in rendered and MAINTENANCE_CAVEAT in rendered
    win._recompute()
    app.update()
    update_src = inspect.getsource(App.update_data)
    for name in ("compute_maintenance_outlook(", "project_threshold_horizon("):
        assert name not in update_src, f"FAIL: update_data() must never call {name}"
    win.destroy(); hw.destroy()
    app.stop_event.set(); app.destroy()
    print("  PASS: opens as a singleton from History against the real (empty) store, renders the caveat, "
          "and no projection work touches the live poll")

    print("\nALL PREDICTIVE MAINTENANCE CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
