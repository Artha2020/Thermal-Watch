"""Verification for Cooling/Fan Intelligence: cpu_fan_rpm/gpu_fan_pct now PERSISTED as telemetry
scalars (not just live-context), comparable-load/fan-speed binning correctness, the anti-hype
guard the user explicitly asked for (never "learn a curve" from one sitting - requires multiple
qualifying fan-speed bins each spanning multiple distinct calendar days), the meaningful-cooling
and diminishing-returns report shapes against the user's own two worked examples,
FanIntelligenceWindow wiring, RECOMMENDATIONS-ONLY (no fan-control code path anywhere), and that
the whole layer stays read-only/on-demand - never the 2s poll."""
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
sys.stdout.reconfigure(encoding="utf-8")

import app as appmod  # noqa: E402
from app import (  # noqa: E402
    App, HistoryWindow, FanIntelligenceWindow, TELEMETRY_SCALAR_KEYS, _new_telemetry_bucket,
    group_buckets_by_comparable_load_and_fan, _distinct_days, compute_fan_cooling_response,
    format_fan_cooling_response, scalar_sensor_ref, FAN_RESPONSE_MIN_BUCKETS_PER_BIN,
    FAN_RESPONSE_MIN_DISTINCT_DAYS,
)

NOW = time.time()


def bucket(start_ts, gpu_util=96.0, gpu_fan_pct=60.0, gpu_hotspot=91.0, gpu_power=318.0,
          cpu_util=90.0, cpu_fan_rpm=1600.0, cpu_temp=80.0, cpu_power=190.0):
    return {"start_timestamp": start_ts, "end_timestamp": start_ts + 60, "sample_count": 30,
           "scalars": {
               "gpu_util": {"avg": gpu_util, "min": gpu_util, "max": gpu_util, "count": 30},
               "gpu_fan_pct": {"avg": gpu_fan_pct, "min": gpu_fan_pct, "max": gpu_fan_pct, "count": 30},
               "gpu_hotspot_temp": {"avg": gpu_hotspot, "min": gpu_hotspot, "max": gpu_hotspot, "count": 30},
               "gpu_power": {"avg": gpu_power, "min": gpu_power, "max": gpu_power, "count": 30},
               "cpu_util": {"avg": cpu_util, "min": cpu_util, "max": cpu_util, "count": 30},
               "cpu_fan_rpm": {"avg": cpu_fan_rpm, "min": cpu_fan_rpm, "max": cpu_fan_rpm, "count": 30},
               "cpu_temp": {"avg": cpu_temp, "min": cpu_temp, "max": cpu_temp, "count": 30},
               "cpu_power": {"avg": cpu_power, "min": cpu_power, "max": cpu_power, "count": 30},
           }, "sensors": {}}


def fan_level_buckets(day_offsets, fan_value, load=96.0, temp=91.0, power=318.0, n_per_day=None,
                      fan_key="gpu_fan_pct", temp_key="gpu_hotspot", load_key="gpu_util", power_key="gpu_power"):
    n_per_day = n_per_day or (FAN_RESPONSE_MIN_BUCKETS_PER_BIN // len(day_offsets) + 2)
    out = []
    kwargs_base = {load_key: load, fan_key: fan_value, temp_key: temp, power_key: power}
    for day in day_offsets:
        for i in range(n_per_day):
            t = NOW - day * 86400 - i * 60
            out.append(bucket(t, **kwargs_base))
    return out


def main():
    print("=== 1. cpu_fan_rpm/gpu_fan_pct are now REAL persisted telemetry scalars, not just live-context ===")
    assert "cpu_fan_rpm" in TELEMETRY_SCALAR_KEYS and "gpu_fan_pct" in TELEMETRY_SCALAR_KEYS
    app = App()
    app.telemetry_bucket = _new_telemetry_bucket(NOW)
    app.last_context = {"cpu_fan_rpm": 1400.0, "gpu_fan_pct": 55.0}
    app._telemetry_observe_tick([])
    b = app.telemetry_bucket
    assert b["scalars"]["cpu_fan_rpm"]["sum"] == 1400.0 and b["scalars"]["gpu_fan_pct"]["sum"] == 55.0
    app.stop_event.set(); app.destroy()
    print("  PASS: a live tick's cpu_fan_rpm/gpu_fan_pct correctly land in the telemetry bucket's scalars")

    print("\n=== 2. group_buckets_by_comparable_load_and_fan: excludes buckets missing load/fan/temp, carries power along ===")
    complete = bucket(NOW)
    missing_fan = dict(complete); missing_fan["scalars"] = {k: v for k, v in complete["scalars"].items() if k != "gpu_fan_pct"}
    cells = group_buckets_by_comparable_load_and_fan([complete, missing_fan], scalar_sensor_ref("gpu_util"),
                                                     scalar_sensor_ref("gpu_fan_pct"), scalar_sensor_ref("gpu_hotspot_temp"),
                                                     10.0, 10.0, power_ref=scalar_sensor_ref("gpu_power"))
    total_entries = sum(len(v) for v in cells.values())
    assert total_entries == 1, f"FAIL: the bucket missing gpu_fan_pct must be excluded entirely: {cells}"
    (load_band, fan_bin), entries = next(iter(cells.items()))
    assert entries[0]["power"] == 318.0
    print(f"  PASS: 1 of 2 buckets used (the one missing fan data excluded outright), power carried as context ({entries[0]['power']}W)")

    print("\n=== 3. _distinct_days: counts real calendar-day diversity, not sample count ===")
    same_day = [{"start_timestamp": NOW - i * 60} for i in range(50)]
    three_days = [{"start_timestamp": NOW - d * 86400} for d in (0, 1, 2)]
    assert _distinct_days(same_day) == 1
    assert _distinct_days(three_days) == 3
    print("  PASS: 50 samples in one sitting -> 1 distinct day; 3 samples on 3 different days -> 3 distinct days")

    print("\n=== 4. compute_fan_cooling_response: no telemetry at all -> None ===")
    with monkeypatch_telemetry([]):
        assert compute_fan_cooling_response("gpu") is None
    print("  PASS: an empty telemetry history returns None (not a fabricated report)")

    print("\n=== 5. compute_fan_cooling_response: only ONE fan-speed level ever observed -> None (nothing to compare) ===")
    one_level = fan_level_buckets([0, 3, 6], fan_value=60.0)
    with monkeypatch_telemetry(one_level):
        assert compute_fan_cooling_response("gpu") is None
    print("  PASS: a single fan-speed level, however much history, has nothing to compare it against - None")

    print("\n=== 6. THE anti-hype guard: two fan levels with enough SAMPLES but crammed into ONE calendar day -> None ===")
    one_day_two_levels = (fan_level_buckets([5], fan_value=60.0, temp=91.0, n_per_day=FAN_RESPONSE_MIN_BUCKETS_PER_BIN + 5)
                          + fan_level_buckets([5], fan_value=80.0, temp=85.0, n_per_day=FAN_RESPONSE_MIN_BUCKETS_PER_BIN + 5))
    with monkeypatch_telemetry(one_day_two_levels):
        assert compute_fan_cooling_response("gpu") is None, \
            "FAIL: two fan levels observed only within a single calendar day must NEVER be treated as a learned response"
    print(f"  PASS: {FAN_RESPONSE_MIN_BUCKETS_PER_BIN + 5}+ samples at each of 2 fan levels, but all on ONE day -> "
          f"None (this is the direct fix for 'don't learn a curve from one sitting')")

    print("\n=== 7. Enough samples AND enough distinct days -> a real report, matching the meaningful-cooling worked example ===")
    meaningful = (fan_level_buckets([18, 15, 12], fan_value=60.0, temp=91.0)
                 + fan_level_buckets([9, 6, 3], fan_value=75.0, temp=85.0))
    with monkeypatch_telemetry(meaningful):
        report = compute_fan_cooling_response("gpu")
    assert report is not None
    assert abs(report["lowest"]["fan"] - 60.0) < 1e-9 and abs(report["highest"]["fan"] - 80.0) < 1e-9  # 75 rounds to 80
    assert abs(report["response_delta"] - (-6.0)) < 1e-9, report
    lines = format_fan_cooling_response(report, "GPU")
    assert lines[0] == "GPU COOLING RESPONSE"
    assert any(l.startswith("GPU fans:") for l in lines)
    assert any(l.startswith("Hotspot:") for l in lines)
    assert "Cooling response: -6" in "\n".join(lines)
    assert "Higher fan speed produced a meaningful temperature reduction" in "\n".join(lines)
    print("  PASS: matches the worked example's shape:")
    for line in lines:
        print(f"    {line}")

    print("\n=== 8. Diminishing returns: a genuine 3rd fan level with a much smaller marginal benefit is flagged and formatted ===")
    diminishing = (fan_level_buckets([25, 20, 15], fan_value=55.0, temp=91.0)
                  + fan_level_buckets([10, 8, 6], fan_value=75.0, temp=85.0)
                  + fan_level_buckets([4, 2, 0], fan_value=95.0, temp=84.0))
    with monkeypatch_telemetry(diminishing):
        dim_report = compute_fan_cooling_response("gpu")
    assert dim_report is not None and dim_report["diminishing_returns"] is True, dim_report
    dim_lines = format_fan_cooling_response(dim_report, "GPU")
    assert "DIMINISHING RETURNS" in dim_lines
    joined = "\n".join(dim_lines)
    assert "80 → 100 %" in joined or "80 \u2192 100 %" in joined, joined
    assert "Additional fan speed produced little additional cooling." in joined
    print("  PASS: a 3rd fan level with a small marginal benefit correctly triggers the DIMINISHING RETURNS section:")
    for line in dim_lines:
        print(f"    {line}")

    print("\n=== 9. Different load bands are correctly isolated - a relationship at one load never bleeds into another's comparison ===")
    mixed_load = (fan_level_buckets([18, 15, 12], fan_value=60.0, temp=91.0, load=96.0)  # high load, 2 fan levels
                 + fan_level_buckets([9, 6, 3], fan_value=75.0, temp=85.0, load=96.0)
                 + fan_level_buckets([5, 5, 5], fan_value=90.0, temp=50.0, load=20.0))    # low load, single fan level, would corrupt if merged
    with monkeypatch_telemetry(mixed_load):
        isolated_report = compute_fan_cooling_response("gpu")
    assert isolated_report is not None and abs(isolated_report["load_band"] - 100.0) < 1e-9, isolated_report
    assert abs(isolated_report["response_delta"] - (-6.0)) < 1e-9, \
        "FAIL: the unrelated low-load/single-fan-level data corrupted the high-load comparison"
    print(f"  PASS: the low-load band (single fan level, would be unusable alone) never contaminates the "
          f"high-load band's own clean 2-level comparison (response still exactly -6°C)")

    print("\n=== 10. CPU component: same machinery, CPU-specific labels (RPM, 'CPU:' not 'Hotspot:', singular 'fan') ===")
    cpu_meaningful = (fan_level_buckets([18, 15, 12], fan_value=1400.0, temp=92.0, fan_key="cpu_fan_rpm",
                                        temp_key="cpu_temp", load_key="cpu_util", power_key="cpu_power")
                      + fan_level_buckets([9, 6, 3], fan_value=1800.0, temp=86.0, fan_key="cpu_fan_rpm",
                                          temp_key="cpu_temp", load_key="cpu_util", power_key="cpu_power"))
    with monkeypatch_telemetry(cpu_meaningful):
        cpu_report = compute_fan_cooling_response("cpu")
    assert cpu_report is not None and cpu_report["fan_unit"] == "RPM"
    cpu_lines = format_fan_cooling_response(cpu_report, "CPU")
    joined = "\n".join(cpu_lines)
    assert "CPU fan:" in joined and "Hotspot:" not in joined and "CPU:" in joined
    assert "1,400" in joined or "1400" in joined
    print("  PASS: CPU report uses RPM formatting, singular 'CPU fan:', and 'CPU:' (not 'Hotspot:') for temperature:")
    for line in cpu_lines:
        print(f"    {line}")

    print("\n=== 11. format_fan_cooling_response: None -> an honest 'not enough data yet' message, never a fabricated report ===")
    none_lines = format_fan_cooling_response(None, "GPU")
    assert none_lines[0] == "GPU COOLING RESPONSE"
    assert "Not enough data yet" in "\n".join(none_lines)
    assert str(FAN_RESPONSE_MIN_DISTINCT_DAYS) in "\n".join(none_lines)
    print(f"  PASS: None -> explicit 'Not enough data yet' message naming the {FAN_RESPONSE_MIN_DISTINCT_DAYS}-day minimum")

    print("\n=== 12. FanIntelligenceWindow: opens via HistoryWindow (singleton), renders both GPU and CPU panels ===")
    with monkeypatch_telemetry(meaningful):
        app2 = App()
        hw = HistoryWindow(app2)
        hw.open_fan_intelligence()
        app2.update()
        win1 = hw.fan_window
        hw.open_fan_intelligence()  # must reuse the same window, not open a second one
        assert hw.fan_window is win1, "FAIL: open_fan_intelligence() must reuse the existing window (singleton pattern)"
        gpu_text = win1.panels["gpu"].cget("text")
        cpu_text = win1.panels["cpu"].cget("text")
        assert "GPU COOLING RESPONSE" in gpu_text and "Cooling response: -6" in gpu_text, gpu_text
        assert "CPU COOLING RESPONSE" in cpu_text and "Not enough data yet" in cpu_text, cpu_text
        win1.destroy()
        hw.destroy()
        app2.stop_event.set(); app2.destroy()
    print("  PASS: GPU panel shows the real computed response, CPU panel (no CPU fan history in this fixture) "
          "honestly shows 'not enough data yet' - both through the full UI stack")

    print("\n=== 13. Advisory-only: no fan-control code path exists anywhere in this file ===")
    src = inspect.getsource(appmod)
    forbidden = ("set_fan_curve", "SetFanSpeed", "set_fan_speed", "write_fan", "fan_curve_write",
                "SetFanControl", "os.system", "shutdown /")
    for needle in forbidden:
        assert needle not in src, f"FAIL: found a potential fan-control code path: {needle}"
    print("  PASS: no fan-speed-write/fan-control code path exists anywhere in app.py - recommendations only")

    print("\n=== 14. Cooling/Fan Intelligence never runs on the live 2s poll or on session/app close ===")
    forbidden_calls = ("compute_fan_cooling_response(", "group_buckets_by_comparable_load_and_fan(")
    update_src = inspect.getsource(App.update_data)
    close_src = inspect.getsource(App.close)
    for name in forbidden_calls:
        assert name not in update_src, f"FAIL: update_data() must never call {name} on the 2s poll"
        assert name not in close_src, f"FAIL: fan-response computation must never run automatically on session/app close"
    print("  PASS: update_data()/close() contain no fan-response computation - display-only, on-demand")

    print("\nALL COOLING/FAN INTELLIGENCE CHECKS PASSED, NO TRACEBACK")


class monkeypatch_telemetry:
    def __init__(self, buckets):
        self.buckets = buckets

    def __enter__(self):
        self.orig = appmod.read_telemetry_file
        appmod.read_telemetry_file = lambda since_ts=None, sensor_key=None: self.buckets
        return self

    def __exit__(self, *exc):
        appmod.read_telemetry_file = self.orig


if __name__ == "__main__":
    main()
