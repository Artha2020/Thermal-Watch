"""Verification for Hardware-Change Experiments: user-marked change points compared before/after
using the SAME compare_period_values machinery every other layer uses, the marker store's
read/append/delete round-trip and its deliberate never-pruned lifetime, the two window-fairness
rules (equal duration on both sides, retention clamping) and the minimum-elapsed anti-hype gate,
the ASYMMETRIC corroboration rule (power moving with temperature downgrades; a second independent
temperature moving with it supports), the confound cap when a second change lands inside a window,
the non-causal caveat on every report, ExperimentsWindow's full add/select/delete UI stack, and
that the whole layer stays read-only/on-demand - never the 2s poll."""
import inspect
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
sys.stdout.reconfigure(encoding="utf-8")

import app as appmod  # noqa: E402
from app import (  # noqa: E402
    App, HistoryWindow, ExperimentsWindow, EXPERIMENTS_PATH, SESSIONS_PATH,
    EXPERIMENT_WINDOW_DAYS, EXPERIMENT_MIN_ELAPSED_DAYS, EXPERIMENT_MAX_HISTORY_DAYS,
    EXPERIMENT_COMPONENT_LABELS, EXPERIMENT_CAVEAT, TREND_MIN_SAMPLES, BASELINE_MIN_IDLE_BUCKETS,
    parse_experiment_timestamp, new_experiment_record, read_experiments_file, append_experiment,
    delete_experiment, experiment_window_bounds, overlapping_experiments,
    compute_experiment_period_trend, compute_experiment_report, format_experiment_report,
    format_experiment_timestamp,
)

NOW = time.time()
DAY = 86400.0


def fresh_files():
    for p in (EXPERIMENTS_PATH, SESSIONS_PATH):
        if p.exists():
            p.unlink()


def session_fixture(sid, workload, start, cpu_temp=60.0, cpu_power=80.0, gpu_core=65.0,
                    gpu_hotspot=77.0, gpu_power=250.0, dur=1800):
    return {
        "session_id": sid, "workload_key": workload.casefold(), "workload": workload,
        "start_timestamp": start, "end_timestamp": start + dur, "duration_seconds": dur,
        "duration_exact": True,
        "cpu": {"avg_temp": cpu_temp, "peak_temp": cpu_temp + 5, "avg_power": cpu_power},
        "gpu": {"avg_core_temp": gpu_core, "peak_core_temp": gpu_core + 8,
               "avg_hotspot_temp": gpu_hotspot, "peak_hotspot_temp": gpu_hotspot + 4,
               "avg_vram_temp": 78.0, "peak_vram_temp": 82.0,
               "avg_power": gpu_power, "peak_power": gpu_power + 40},
        "zone_time": {}, "incident_count": 0, "max_incident_severity": None,
        "incident_ids": [], "monitoring_gaps": [],
    }


def straddle_fixture(workload, change_ts, hotspot_shift=0.0, power_shift=0.0, n=6, gpu_hotspot=91.0,
                     gpu_power=318.0):
    """n sessions in the 8-day window BEFORE the change and n in the 8 days after it. Per-session
    jitter is deliberate: perfectly identical values give a period stddev of exactly 0, which
    routes through evaluate_anomaly's absolute-delta fallback instead of the real z-score path
    (the gotcha documented while building Trend Intelligence)."""
    before = [session_fixture(f"{workload}-b{i}", workload, change_ts - (7 - i) * DAY,
                              gpu_hotspot=gpu_hotspot + i * 0.1, gpu_power=gpu_power + i * 0.4,
                              cpu_temp=70.0 + i * 0.1, cpu_power=120.0 + i * 0.4) for i in range(n)]
    after = [session_fixture(f"{workload}-a{i}", workload, change_ts + (i + 1) * DAY * 0.9,
                             gpu_hotspot=gpu_hotspot + hotspot_shift + i * 0.1,
                             gpu_power=gpu_power + power_shift + i * 0.4,
                             cpu_temp=70.0 + i * 0.1, cpu_power=120.0 + i * 0.4) for i in range(n)]
    return before + after


def idle_bucket(ts, cpu_temp, gpu_hotspot):
    def agg(v):
        return {"avg": v, "min": v, "max": v, "count": 30}
    return {"start_timestamp": ts, "end_timestamp": ts + 60, "sample_count": 30,
           "scalars": {"cpu_temp": agg(cpu_temp), "gpu_hotspot_temp": agg(gpu_hotspot)}, "sensors": {}}


def idle_history(change_ts, cpu_before=42.0, cpu_after=42.0, gpu_before=48.0, gpu_after=48.0,
                 n=BASELINE_MIN_IDLE_BUCKETS + 5):
    out = []
    for i in range(n):
        out.append(idle_bucket(change_ts - (i + 1) * 3600, cpu_before + i * 0.05, gpu_before + i * 0.05))
        out.append(idle_bucket(change_ts + (i + 1) * 3600, cpu_after + i * 0.05, gpu_after + i * 0.05))
    return out


class mock_data:
    """Replaces the three module-level readers the experiments layer consults, so every check runs
    against known data through the REAL functions (never a reimplemented copy of them)."""

    def __init__(self, sessions=(), buckets=(), experiments=()):
        self.sessions, self.buckets, self.experiments = list(sessions), list(buckets), list(experiments)

    def __enter__(self):
        self.orig = (appmod.read_sessions_file, appmod.read_telemetry_file, appmod.read_experiments_file)
        appmod.read_sessions_file = lambda: self.sessions
        appmod.read_telemetry_file = lambda since_ts=None, sensor_key=None: self.buckets
        appmod.read_experiments_file = lambda: self.experiments
        return self

    def __exit__(self, *exc):
        (appmod.read_sessions_file, appmod.read_telemetry_file, appmod.read_experiments_file) = self.orig


def experiment(description, change_ts, component="gpu", experiment_id="exp-test"):
    return {"experiment_id": experiment_id, "created_timestamp": NOW, "change_timestamp": change_ts,
           "description": description, "component": component}


def main():
    fresh_files()

    print("=== 1. parse_experiment_timestamp: both accepted shapes, and a FUTURE change is rejected ===")
    assert parse_experiment_timestamp("2026-08-04", now=NOW) == time.mktime(time.strptime("2026-08-04", "%Y-%m-%d"))
    assert parse_experiment_timestamp("2026-08-04 19:40", now=NOW) is not None
    assert parse_experiment_timestamp("  2026-08-04  ", now=NOW) is not None
    assert parse_experiment_timestamp("last tuesday", now=NOW) is None
    assert parse_experiment_timestamp("2026-13-45", now=NOW) is None
    assert parse_experiment_timestamp("", now=NOW) is None
    assert parse_experiment_timestamp(time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW + 5 * DAY)), now=NOW) is None, \
        "FAIL: a change dated in the future can only ever produce an empty comparison - it must be rejected"
    print("  PASS: 'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM' parse; garbage, impossible dates and FUTURE dates -> None")

    print("\n=== 2. new_experiment_record: refuses an unusable marker, and never reuses an id ===")
    for bad_args in (("", NOW - DAY, "gpu"), ("   ", NOW - DAY, "gpu"),
                     ("repaste", NOW - DAY, "toaster"), ("repaste", None, "gpu")):
        try:
            new_experiment_record(*bad_args)
        except ValueError:
            continue
        raise AssertionError(f"FAIL: new_experiment_record accepted an unusable marker: {bad_args}")
    change = NOW - 8 * DAY
    first = new_experiment_record("Replaced GPU thermal paste", change, "gpu", now=NOW)
    second = new_experiment_record("Second change same second", change, "cpu", now=NOW,
                                   existing_ids=[first["experiment_id"]])
    assert first["experiment_id"] != second["experiment_id"], "FAIL: two markers must never share an id"
    assert first["description"] == "Replaced GPU thermal paste" and first["component"] == "gpu"
    print(f"  PASS: empty/whitespace description, unknown component and a missing date all rejected; "
          f"colliding ids disambiguated ({first['experiment_id']} vs {second['experiment_id']})")

    print("\n=== 3. Marker store: append/read/delete round-trip, sorted by CHANGE date not append order ===")
    fresh_files()
    older = new_experiment_record("Cleaned dust filters", NOW - 20 * DAY, "system", now=NOW)
    newer = new_experiment_record("New CPU cooler", NOW - 2 * DAY, "cpu", now=NOW)
    assert append_experiment(older) and append_experiment(newer)  # appended OLDEST-change last-but-one
    stored = read_experiments_file()
    assert [e["description"] for e in stored] == ["New CPU cooler", "Cleaned dust filters"], stored
    assert delete_experiment(newer["experiment_id"]) is True
    assert [e["description"] for e in read_experiments_file()] == ["Cleaned dust filters"]
    assert delete_experiment("exp-does-not-exist") is False, "FAIL: deleting an unknown id must report False"
    print("  PASS: markers round-trip through the real file, are ordered by when the CHANGE happened "
          "(not when it was typed), and delete is exact")

    print("\n=== 4. Markers are NEVER auto-pruned, unlike incidents/sessions/telemetry ===")
    fresh_files()
    ancient = new_experiment_record("Built the machine", NOW - 400 * DAY, "system", now=NOW)
    append_experiment(ancient)
    assert [e["description"] for e in read_experiments_file()] == ["Built the machine"], \
        "FAIL: a user's own annotation must never be silently discarded on age"
    print(f"  PASS: a marker {400} days old (far past the {EXPERIMENT_MAX_HISTORY_DAYS}-day data retention) "
          f"is still returned in full - only the measured data around it expires")
    fresh_files()

    print("\n=== 5. experiment_window_bounds: the two sides are always EQUAL length and adjacent ===")
    bounds, reason = experiment_window_bounds(NOW - 3 * DAY, now=NOW)
    assert reason is None and bounds is not None
    before_len = bounds["before_end"] - bounds["before_start"]
    after_len = bounds["after_end"] - bounds["after_start"]
    assert abs(before_len - after_len) < 1e-6, f"FAIL: unequal windows {before_len} vs {after_len}"
    assert bounds["before_end"] == bounds["after_start"] == NOW - 3 * DAY
    assert bounds["after_end"] <= NOW + 1e-6, "FAIL: the after window must never reach into the future"
    assert abs(bounds["duration_days"] - 3.0) < 1e-6, bounds
    print(f"  PASS: a change 3 days ago -> two adjacent {bounds['duration_days']:.1f}-day windows meeting "
          f"exactly at the change, neither reaching past now")

    print("\n=== 6. Anti-hype gate: no verdict at all until EXPERIMENT_MIN_ELAPSED_DAYS has passed ===")
    bounds, reason = experiment_window_bounds(NOW - 3600, now=NOW)
    assert bounds is None and "days since the change" in reason, reason
    report = compute_experiment_report(experiment("Repasted an hour ago", NOW - 3600), now=NOW)
    assert report["direction"] is None and report["bounds"] is None
    lines = "\n".join(format_experiment_report(report))
    assert "Not enough data yet" in lines, lines
    print(f"  PASS: a change made 1 hour ago yields NO verdict however much data exists - "
          f"'{reason}'")

    print("\n=== 7. Retention clamping: the before window never reaches past data that no longer exists ===")
    bounds, _ = experiment_window_bounds(NOW - 20 * DAY, now=NOW)
    assert abs(bounds["duration_days"] - (EXPERIMENT_MAX_HISTORY_DAYS - 20)) < 1e-6, bounds
    bounds_capped, _ = experiment_window_bounds(NOW - 15 * DAY, now=NOW)
    assert abs(bounds_capped["duration_days"] - EXPERIMENT_WINDOW_DAYS) < 1e-6, bounds_capped
    none_bounds, retention_reason = experiment_window_bounds(NOW - (EXPERIMENT_MAX_HISTORY_DAYS - 0.5) * DAY, now=NOW)
    assert none_bounds is None and "stored history exists before this change" in retention_reason, retention_reason
    print(f"  PASS: 20 days ago -> {EXPERIMENT_MAX_HISTORY_DAYS - 20:.0f}-day windows (limited by retention); "
          f"15 days ago -> {EXPERIMENT_WINDOW_DAYS}-day windows (limited by EXPERIMENT_WINDOW_DAYS); "
          f"a change right at the retention edge -> no comparison, with the reason named")

    print("\n=== 8. compute_experiment_period_trend: splits at the MARKER, and a straddling session is "
          "handled by the same overlapping_sessions convention as every other window in this file ===")
    change = NOW - 8 * DAY
    bounds, _ = experiment_window_bounds(change, now=NOW)
    sessions = straddle_fixture("Cyberpunk2077.exe", change, hotspot_shift=-7.0)
    trend = compute_experiment_period_trend(sessions, "gpu", "avg_hotspot_temp", "°C", bounds)
    assert trend is not None and trend["n_older"] == 6 and trend["n_recent"] == 6, trend
    straddler = session_fixture("straddle", "Cyberpunk2077.exe", change - 1800, dur=3600)
    both = compute_experiment_period_trend(sessions + [straddler], "gpu", "avg_hotspot_temp", "°C", bounds)
    assert both["n_older"] == 7 and both["n_recent"] == 7, \
        f"FAIL: a session spanning the marker must appear on BOTH sides (documented convention): {both}"
    print("  PASS: 6 sessions each side split cleanly at the marker; one session spanning the change counts "
          "on both sides, exactly as the calendar-half trends already do")

    print("\n=== 9. Too little on either side -> an honest 'not enough data yet', never a verdict ===")
    thin = straddle_fixture("thin.exe", change, hotspot_shift=-7.0, n=TREND_MIN_SAMPLES - 1)
    with mock_data(sessions=thin):
        thin_report = compute_experiment_report(experiment("Repasted GPU", change), now=NOW)
    assert thin_report["bounds"] is not None, "FAIL: the windows themselves are fine here - only the samples are thin"
    assert thin_report["direction"] is None and thin_report["confidence"] is None
    assert f"{TREND_MIN_SAMPLES} comparable samples" in thin_report["insufficient_reason"], thin_report
    print(f"  PASS: {TREND_MIN_SAMPLES - 1} sessions per side -> no result, reason names the "
          f"{TREND_MIN_SAMPLES}-sample bar")

    print("\n=== 10. A real improvement with FLAT power -> IMPROVED, confidence NOT downgraded ===")
    improved_sessions = straddle_fixture("Cyberpunk2077.exe", change, hotspot_shift=-7.0, power_shift=0.0)
    with mock_data(sessions=improved_sessions):
        improved = compute_experiment_report(experiment("Replaced GPU thermal paste", change), now=NOW)
    assert improved["direction"] == "IMPROVED", improved
    assert improved["confidence"] in ("MEDIUM", "HIGH"), improved
    assert "Cyberpunk2077.exe" in improved["primary_source"], improved
    improved_lines = format_experiment_report(improved)
    joined = "\n".join(improved_lines)
    assert "EXPERIMENT — Replaced GPU thermal paste" in joined
    assert "Result: IMPROVED" in joined and "Thermal efficiency:" in joined
    print(f"  PASS: hotspot -7°C at flat power -> IMPROVED / {improved['confidence']}:")
    for line in improved_lines:
        print(f"    {line}")

    print("\n=== 11. Corroboration is ASYMMETRIC: power falling ALONGSIDE temperature costs a tier ===")
    also_less_power = straddle_fixture("Cyberpunk2077.exe", change, hotspot_shift=-7.0, power_shift=-60.0)
    with mock_data(sessions=also_less_power):
        confounded_by_power = compute_experiment_report(experiment("Replaced GPU thermal paste", change), now=NOW)
    assert confounded_by_power["direction"] == "IMPROVED"
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    assert order[confounded_by_power["confidence"]] < order[improved["confidence"]], \
        (f"FAIL: a GPU that also drew 60W less may simply have been working less hard - that alternative "
         f"explanation must cost a tier: {confounded_by_power['confidence']} vs {improved['confidence']}")
    print(f"  PASS: same -7°C improvement, but power fell 60W too -> {confounded_by_power['confidence']} "
          f"instead of {improved['confidence']} (an alternative explanation, never extra support)")

    print("\n=== 12. A change that made things worse is reported as WORSE, not softened ===")
    worse_sessions = straddle_fixture("Cyberpunk2077.exe", change, hotspot_shift=+8.0)
    with mock_data(sessions=worse_sessions):
        worse = compute_experiment_report(experiment("Swapped case fans", change), now=NOW)
    assert worse["direction"] == "WORSE", worse
    print(f"  PASS: hotspot +8°C after the change -> WORSE / {worse['confidence']}")

    print("\n=== 13. No real shift is REPORTED as 'NO MEASURED CHANGE' (never dropped), and never HIGH ===")
    flat_sessions = straddle_fixture("Cyberpunk2077.exe", change, hotspot_shift=0.0)
    with mock_data(sessions=flat_sessions):
        flat = compute_experiment_report(experiment("Added a second case fan", change), now=NOW)
    assert flat["direction"] == "NO MEASURED CHANGE", flat
    assert flat["confidence"] != "HIGH", \
        f"FAIL: 'we could not measure a change' can never be a HIGH-confidence claim: {flat}"
    assert "Result: NO MEASURED CHANGE" in "\n".join(format_experiment_report(flat))
    print(f"  PASS: 'did it help?' gets an answer even when the answer is no -> NO MEASURED CHANGE / "
          f"{flat['confidence']} (capped below HIGH by construction)")

    print("\n=== 14. Confound: a SECOND marked change inside a window caps confidence at LOW and names it ===")
    other = experiment("Reapplied CPU paste", change + 2 * DAY, component="cpu", experiment_id="exp-other")
    assert len(overlapping_experiments([other], bounds["before_start"], bounds["after_end"])) == 1
    assert overlapping_experiments([other], bounds["before_start"], bounds["after_end"],
                                   exclude_id="exp-other") == [], "FAIL: an experiment must never confound itself"
    with mock_data(sessions=improved_sessions, experiments=[other]):
        confounded = compute_experiment_report(experiment("Replaced GPU thermal paste", change), now=NOW)
    assert confounded["confidence"] == "LOW", confounded
    conf_lines = "\n".join(format_experiment_report(confounded))
    assert "Confounded by another marked change" in conf_lines and "Reapplied CPU paste" in conf_lines, conf_lines
    assert "cannot be told apart" in conf_lines
    print(f"  PASS: same evidence that scored {improved['confidence']} alone is capped at LOW when a second "
          f"change lands inside the window, with the other change named in the report")

    print("\n=== 15. Workload isolation: the verdict comes from the best-sampled workload, never a blend ===")
    quiet_other = straddle_fixture("notepad.exe", change, hotspot_shift=+15.0, n=TREND_MIN_SAMPLES,
                                   gpu_hotspot=55.0, gpu_power=40.0)
    with mock_data(sessions=improved_sessions + quiet_other):
        mixed = compute_experiment_report(experiment("Replaced GPU thermal paste", change), now=NOW)
    assert mixed["direction"] == "IMPROVED", \
        f"FAIL: a lightly-sampled unrelated workload must not overturn the best-sampled one: {mixed}"
    assert "Cyberpunk2077.exe" in mixed["primary_source"], mixed
    workloads = {wt["workload"] for wt in mixed["workload_trends"]}
    assert workloads == {"Cyberpunk2077.exe", "notepad.exe"}, workloads
    print(f"  PASS: both workloads are reported separately, and the verdict follows the best-sampled one "
          f"({mixed['primary_source']}) rather than averaging across them")

    print("\n=== 16. A chassis/airflow change has NO workload block - idle temperature is the primary signal, "
          "and a second independent idle temperature is genuine SUPPORT ===")
    both_fell = idle_history(change, cpu_before=45.0, cpu_after=41.0, gpu_before=52.0, gpu_after=48.0)
    with mock_data(sessions=[], buckets=both_fell):
        airflow = compute_experiment_report(experiment("Added 2 intake fans", change, component="system"), now=NOW)
    assert airflow["workload_trends"] == [], "FAIL: a system-class experiment has no per-workload block by design"
    assert airflow["direction"] == "IMPROVED", airflow
    assert airflow["primary_source"] == "Idle CPU Package", airflow
    only_cpu_fell = idle_history(change, cpu_before=45.0, cpu_after=41.0, gpu_before=52.0, gpu_after=52.0)
    with mock_data(sessions=[], buckets=only_cpu_fell):
        uncorroborated = compute_experiment_report(experiment("Added 2 intake fans", change, component="system"), now=NOW)
    assert uncorroborated["direction"] == "IMPROVED"
    assert order[uncorroborated["confidence"]] < order[airflow["confidence"]], \
        (f"FAIL: one resting temperature falling alone is weaker evidence of a machine-wide airflow change than "
         f"two falling together: {uncorroborated['confidence']} vs {airflow['confidence']}")
    print(f"  PASS: both resting temperatures falling -> {airflow['confidence']}; only the CPU's falling -> "
          f"{uncorroborated['confidence']} (a second temperature SUPPORTS, unlike power which explains away)")

    print("\n=== 17. Idle comparisons obey the EXISTING BASELINE_MIN_IDLE_BUCKETS bar, not a looser one ===")
    too_few_idle = idle_history(change, cpu_before=45.0, cpu_after=41.0, gpu_before=52.0, gpu_after=48.0,
                                n=BASELINE_MIN_IDLE_BUCKETS - 5)
    with mock_data(sessions=[], buckets=too_few_idle):
        thin_idle = compute_experiment_report(experiment("Added 2 intake fans", change, component="system"), now=NOW)
    assert thin_idle["idle"]["cpu_temp"] is None and thin_idle["direction"] is None, thin_idle
    print(f"  PASS: {BASELINE_MIN_IDLE_BUCKETS - 5} idle buckets per side (below BASELINE_MIN_IDLE_BUCKETS="
          f"{BASELINE_MIN_IDLE_BUCKETS}) -> no idle trend and no verdict")

    print("\n=== 18. EVIDENCE, NEVER ATTRIBUTION: the caveat is on every measured report, including a good one ===")
    for name, rep in (("IMPROVED", improved), ("WORSE", worse), ("NO MEASURED CHANGE", flat),
                      ("system/idle", airflow)):
        text = "\n".join(format_experiment_report(rep))
        assert EXPERIMENT_CAVEAT in text, f"FAIL: the non-causal caveat is missing from a {name} report"
        for causal in ("caused by", "because you", "proves", "fixed the", "thanks to"):
            assert causal not in text.lower(), f"FAIL: causal language '{causal}' in a {name} report:\n{text}"
    assert "reports what changed, not what caused it" in EXPERIMENT_CAVEAT
    print("  PASS: every report that shows a measurement carries the caveat, and none of them claims the "
          "marked change produced the difference")

    print("\n=== 19. ExperimentsWindow: full add -> report -> delete stack through the real UI ===")
    fresh_files()
    with SESSIONS_PATH.open("w", encoding="utf-8") as fh:
        for s in improved_sessions:
            fh.write(json.dumps(s) + "\n")
    app = App()
    hw = HistoryWindow(app)
    hw.open_experiments()
    app.update()
    win = hw.experiments_window
    hw.open_experiments()
    assert hw.experiments_window is win, "FAIL: open_experiments() must reuse the existing window (singleton)"
    assert win.tree.get_children() == () and "No hardware changes marked yet" in win.detail_text.cget("text")

    win.description_var.set("Replaced GPU thermal paste")
    win.component_var.set(EXPERIMENT_COMPONENT_LABELS["gpu"])
    win.when_var.set(format_experiment_timestamp(change))
    win._mark_change()
    app.update()
    rows = win.tree.get_children()
    assert len(rows) == 1, f"FAIL: the marked change should appear as exactly one row: {rows}"
    values = win.tree.item(rows[0], "values")
    assert values[2] == "Replaced GPU thermal paste" and "IMPROVED" in values[3], values
    assert "Result: IMPROVED" in win.detail_text.cget("text"), win.detail_text.cget("text")
    assert EXPERIMENT_CAVEAT in win.detail_text.cget("text")

    win.description_var.set("")
    win._mark_change()
    assert "Describe what was changed" in win.status_label.cget("text")
    win.description_var.set("Future change")
    win.when_var.set(time.strftime("%Y-%m-%d", time.localtime(NOW + 10 * DAY)))
    win._mark_change()
    assert "past date" in win.status_label.cget("text"), win.status_label.cget("text")
    assert len(win.tree.get_children()) == 1, "FAIL: rejected input must never add a row"

    win.tree.selection_set(rows[0])
    win._delete_selected()
    app.update()
    assert win.tree.get_children() == () and read_experiments_file() == []
    win.destroy(); hw.destroy()
    app.stop_event.set(); app.destroy()
    print("  PASS: marker added through the UI is persisted, scored (IMPROVED) in both the table and the detail "
          "panel, empty/future input is rejected without adding a row, and delete removes it from disk")

    print("\n=== 20. Experiments never run on the live 2s poll, on session close, or on app close ===")
    update_src = inspect.getsource(App.update_data)
    close_src = inspect.getsource(App.close)
    for name in ("compute_experiment_report(", "read_experiments_file(", "append_experiment(",
                 "compute_experiment_idle_trend("):
        assert name not in update_src, f"FAIL: update_data() must never call {name} on the 2s poll"
        assert name not in close_src, f"FAIL: experiment computation must never run automatically on close"
    src = inspect.getsource(appmod)
    assert src.count("EXPERIMENTS_PATH.open") == 1, \
        "FAIL: the marker store should be written from exactly one place (append_experiment)"
    print("  PASS: update_data()/close() contain no experiment computation - display-only, on-demand, and the "
          "marker store has a single append path")

    print("\n=== 21. Listing N markers costs ONE read of each store, not N - and pre-fetched data gives "
          "byte-identical results to letting the report fetch for itself ===")
    fresh_files()
    with SESSIONS_PATH.open("w", encoding="utf-8") as fh:
        for s in improved_sessions:
            fh.write(json.dumps(s) + "\n")
    for i, offset in enumerate((8, 10, 12)):
        append_experiment(new_experiment_record(f"Change {i}", NOW - offset * DAY, "gpu", now=NOW,
                                                existing_ids=[e["experiment_id"] for e in read_experiments_file()]))
    app = App()
    counts = {"sessions": 0, "telemetry": 0, "experiments": 0}
    originals = (appmod.read_sessions_file, appmod.read_telemetry_file, appmod.read_experiments_file)

    def counted(name, fn):
        def wrapper(*a, **kw):
            counts[name] += 1
            return fn(*a, **kw)
        return wrapper

    appmod.read_sessions_file = counted("sessions", originals[0])
    appmod.read_telemetry_file = counted("telemetry", originals[1])
    appmod.read_experiments_file = counted("experiments", originals[2])
    try:
        win = ExperimentsWindow(app)
        app.update()
        assert len(win.tree.get_children()) == 3, win.tree.get_children()
        assert counts == {"sessions": 1, "telemetry": 1, "experiments": 1}, \
            f"FAIL: 3 markers must still cost exactly one read of each store: {counts}"
        render_counts = dict(counts)  # snapshot: the identical-results check below reads again itself
        marker = read_experiments_file()[0]
        prefetched = win._reports[marker["experiment_id"]]
        self_fetched = compute_experiment_report(marker)
        assert format_experiment_report(prefetched) == format_experiment_report(self_fetched), \
            "FAIL: pre-fetching must be a pure optimization - the report must be identical either way"
        win.destroy()
    finally:
        (appmod.read_sessions_file, appmod.read_telemetry_file, appmod.read_experiments_file) = originals
    app.stop_event.set(); app.destroy()
    print(f"  PASS: 3 markers rendered from exactly {render_counts} reads, and a pre-fetched report is "
          f"identical to a self-fetching one")

    fresh_files()
    print("\nALL HARDWARE-CHANGE EXPERIMENT CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
