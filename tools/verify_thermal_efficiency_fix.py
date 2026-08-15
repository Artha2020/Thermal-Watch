"""Verification for the GPU °C/W compatibility fix: session_thermal_efficiency() resolves each
component block's OWN temperature field instead of assuming the cpu block's `avg_temp`, so the
thermal-efficiency trend actually works for the GPU. Two things must both hold: GPU efficiency now
produces real numbers where it silently produced None, and CPU behaviour is BIT-IDENTICAL to the
pre-fix implementation (reproduced here and compared value-by-value, not merely eyeballed)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
sys.stdout.reconfigure(encoding="utf-8")

from app import (  # noqa: E402
    SESSION_EFFICIENCY_TEMP_FIELD, session_thermal_efficiency,
    compute_thermal_efficiency_period_trend, compute_experiment_report, EXPERIMENT_COMPONENTS,
)

NOW = time.time()
DAY = 86400.0


def old_session_thermal_efficiency(session, component_block):
    """The EXACT pre-fix implementation, kept here as the reference the CPU path is compared
    against. If this and the fixed function ever disagree for a cpu block, the fix changed
    something it had no business changing."""
    blk = session.get(component_block) or {}
    temp, power = blk.get("avg_temp"), blk.get("avg_power")
    if temp is None or not power or power < 1.0:
        return None
    return temp / power


def session(sid, start, cpu_temp=60.0, cpu_power=80.0, gpu_hotspot=77.0, gpu_core=65.0,
            gpu_power=250.0, workload="game.exe", dur=1800):
    return {"session_id": sid, "workload_key": workload.casefold(), "workload": workload,
           "start_timestamp": start, "end_timestamp": start + dur, "duration_seconds": dur,
           "cpu": {"avg_temp": cpu_temp, "peak_temp": None if cpu_temp is None else cpu_temp + 5,
                  "avg_power": cpu_power},
           "gpu": {"avg_core_temp": gpu_core, "avg_hotspot_temp": gpu_hotspot,
                  "peak_hotspot_temp": gpu_hotspot + 4, "avg_power": gpu_power},
           "zone_time": {}, "incident_count": 0, "monitoring_gaps": []}


def main():
    print("=== 1. The bug: the gpu block has no `avg_temp` at all, which is why it silently died ===")
    s = session("s0", NOW)
    assert "avg_temp" not in s["gpu"], "fixture must mirror the real session schema"
    assert "avg_temp" in s["cpu"]
    assert old_session_thermal_efficiency(s, "gpu") is None, \
        "FAIL: the pre-fix function must be shown returning None for a perfectly good GPU session"
    print("  PASS: a real session's gpu block stores avg_core_temp/avg_hotspot_temp and no avg_temp, "
          "so the old avg_temp lookup returned None for every GPU session ever asked about")

    print("\n=== 2. Fixed: GPU efficiency is a real number, computed from HOTSPOT over power ===")
    gpu_eff = session_thermal_efficiency(s, "gpu")
    assert gpu_eff is not None, "FAIL: GPU efficiency must now compute"
    assert abs(gpu_eff - 77.0 / 250.0) < 1e-12, gpu_eff
    assert SESSION_EFFICIENCY_TEMP_FIELD["gpu"] == "avg_hotspot_temp"
    assert abs(session_thermal_efficiency(s, "gpu", temp_field="avg_core_temp") - 65.0 / 250.0) < 1e-12, \
        "FAIL: an explicit temp_field must still override the table"
    print(f"  PASS: GPU -> {gpu_eff:.6f} °C/W (77°C hotspot / 250W), hotspot by default because it is this "
          f"project's primary GPU cooling signal; temp_field still overridable to core")

    print("\n=== 3. CPU behaviour is BIT-IDENTICAL to the pre-fix implementation ===")
    cases = [session("c1", NOW, cpu_temp=60.0, cpu_power=80.0),
            session("c2", NOW, cpu_temp=91.3, cpu_power=187.5),
            session("c3", NOW, cpu_temp=0.0, cpu_power=90.0),          # 0°C is a real value, not "missing"
            session("c4", NOW, cpu_temp=70.0, cpu_power=0.0),          # zero power -> None
            session("c5", NOW, cpu_temp=70.0, cpu_power=0.5),          # sub-1W -> None (unchanged guard)
            session("c6", NOW, cpu_temp=None, cpu_power=90.0),         # missing temp -> None
            {"session_id": "c7"},                                       # no cpu block at all
            {"session_id": "c8", "cpu": None}]                          # explicit null block
    for case in cases:
        old, new = old_session_thermal_efficiency(case, "cpu"), session_thermal_efficiency(case, "cpu")
        assert old == new, f"FAIL: CPU result changed for {case.get('session_id')}: {old} -> {new}"
    print(f"  PASS: all {len(cases)} CPU cases (normal, 0°C, zero power, sub-1W, missing temp, missing block, "
          f"null block) return exactly what the pre-fix code returned")

    print("\n=== 4. compute_thermal_efficiency_period_trend now WORKS for the GPU (it returned None before) ===")
    # Rising hotspot at flat power = worsening °C/W. Jitter avoids the zero-stddev fallback path.
    older = [session(f"o{i}", NOW - (28 - i * 2) * DAY, gpu_hotspot=77.0 + i * 0.1, gpu_power=250.0 + i * 0.3)
             for i in range(6)]
    recent = [session(f"r{i}", NOW - (13 - i * 2) * DAY, gpu_hotspot=90.0 + i * 0.1, gpu_power=250.0 + i * 0.3)
              for i in range(6)]
    gpu_trend = compute_thermal_efficiency_period_trend(older + recent, "gpu", 30, now=NOW)
    assert gpu_trend is not None, "FAIL: the GPU efficiency trend must now produce a result"
    assert gpu_trend["direction"] == "WORSENING", gpu_trend
    assert gpu_trend["n_older"] == 6 and gpu_trend["n_recent"] == 6, gpu_trend
    print(f"  PASS: hotspot 77->90°C at flat power -> {gpu_trend['direction']} °C/W "
          f"({gpu_trend['older_mean']:.4f} -> {gpu_trend['recent_mean']:.4f}), from 6 sessions each side - "
          f"this call returned None for every input before the fix")

    print("\n=== 5. Proportional temp+power rise still reports STABLE efficiency (the metric's whole point) ===")
    prop_older = [session(f"po{i}", NOW - (28 - i * 2) * DAY, gpu_hotspot=70.0 + i * 0.1, gpu_power=200.0 + i * 0.3)
                  for i in range(6)]
    prop_recent = [session(f"pr{i}", NOW - (13 - i * 2) * DAY, gpu_hotspot=105.0 + i * 0.15, gpu_power=300.0 + i * 0.45)
                   for i in range(6)]
    prop = compute_thermal_efficiency_period_trend(prop_older + prop_recent, "gpu", 30, now=NOW)
    assert prop is not None and prop["direction"] == "STABLE", \
        f"FAIL: 0.35 °C/W before and after is the same efficiency - a harder-working GPU is not a degrading one: {prop}"
    print(f"  PASS: hotspot 70->105°C but power 200->300W -> {prop['direction']} (0.350 -> 0.350 °C/W): the "
          f"ratio is unchanged, so the GPU simply worked harder")

    print("\n=== 6. CPU trend results are unchanged end-to-end, not just per-session ===")
    cpu_older = [session(f"co{i}", NOW - (28 - i * 2) * DAY, cpu_temp=60.0 + i * 0.1, cpu_power=80.0 + i * 0.2)
                 for i in range(6)]
    cpu_recent = [session(f"cr{i}", NOW - (13 - i * 2) * DAY, cpu_temp=75.0 + i * 0.1, cpu_power=80.0 + i * 0.2)
                  for i in range(6)]
    cpu_trend = compute_thermal_efficiency_period_trend(cpu_older + cpu_recent, "cpu", 30, now=NOW)
    expected_older = sum(old_session_thermal_efficiency(s, "cpu") for s in cpu_older) / 6
    expected_recent = sum(old_session_thermal_efficiency(s, "cpu") for s in cpu_recent) / 6
    assert abs(cpu_trend["older_mean"] - expected_older) < 1e-12, cpu_trend
    assert abs(cpu_trend["recent_mean"] - expected_recent) < 1e-12, cpu_trend
    assert cpu_trend["direction"] == "WORSENING"
    print(f"  PASS: the CPU trend's means match the pre-fix implementation's own values to 1e-12 "
          f"({cpu_trend['older_mean']:.6f} -> {cpu_trend['recent_mean']:.6f} °C/W)")

    print("\n=== 7. One implementation of °C/W: the experiments layer's duplicate is gone and its "
          "reports are unaffected ===")
    import app as appmod
    assert not hasattr(appmod, "experiment_thermal_efficiency"), \
        "FAIL: the duplicate efficiency function must be gone - two implementations WILL drift"
    assert EXPERIMENT_COMPONENTS["gpu"]["temp_field"] == "avg_hotspot_temp"
    assert EXPERIMENT_COMPONENTS["cpu"]["temp_field"] == "avg_temp"
    change = NOW - 8 * DAY
    before = [session(f"eb{i}", change - (7 - i) * DAY, gpu_hotspot=91.0 + i * 0.1, gpu_power=318.0 + i * 0.4)
              for i in range(6)]
    after = [session(f"ea{i}", change + (i + 1) * DAY * 0.9, gpu_hotspot=84.0 + i * 0.1, gpu_power=318.0 + i * 0.4)
             for i in range(6)]
    orig_sessions, orig_buckets, orig_experiments = (appmod.read_sessions_file, appmod.read_telemetry_file,
                                                     appmod.read_experiments_file)
    appmod.read_sessions_file = lambda: before + after
    appmod.read_telemetry_file = lambda since_ts=None, sensor_key=None: []
    appmod.read_experiments_file = lambda: []
    try:
        report = compute_experiment_report(
            {"experiment_id": "e1", "change_timestamp": change, "description": "Repasted GPU",
             "component": "gpu"}, now=NOW)
    finally:
        (appmod.read_sessions_file, appmod.read_telemetry_file,
         appmod.read_experiments_file) = orig_sessions, orig_buckets, orig_experiments
    eff = report["workload_trends"][0]["efficiency"]
    assert report["direction"] == "IMPROVED", report["direction"]
    assert eff is not None and eff["direction"] == "IMPROVING", eff
    assert abs(eff["older_mean"] - sum(session_thermal_efficiency(s, "gpu") for s in before) / 6) < 1e-12
    print(f"  PASS: experiments still report GPU efficiency ({eff['older_mean']:.4f} -> "
          f"{eff['recent_mean']:.4f} °C/W, {eff['direction']}) through the shared function, with the "
          f"duplicate removed")

    print("\nALL GPU °C/W COMPATIBILITY CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
