"""Verification for the Unified Flight Recorder Timeline: every store projected faithfully onto one
ordered axis (never re-deriving a number the source record already holds), UNMONITORED TIME emitted
as a first-class entry using the EXISTING TELEMETRY_GAP_BUCKETS threshold, deterministic ordering
for entries sharing a timestamp, the read-only event-log reader (the timeline must never prune the
log the way App.load_events does), the summary line staying honest when display filters hide rows,
TimelineWindow's full range/filter/select UI stack, and that the whole layer stays read-only/
on-demand - never the 2s poll."""
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
    App, HistoryWindow, TimelineWindow, EVENT_LOG_PATH, INCIDENTS_PATH, SESSIONS_PATH,
    EXPERIMENTS_PATH, TIMELINE_MIN_GAP_SECONDS, TIMELINE_LOG_KINDS, TIMELINE_KIND_ORDER,
    TIMELINE_KIND_LABELS, TIMELINE_RANGE_SECONDS, TELEMETRY_BUCKET_SECONDS, TELEMETRY_GAP_BUCKETS,
    read_event_log_file, build_timeline, summarize_timeline, format_timeline_summary,
    format_timeline_event, timeline_gap_events, timeline_incident_events, timeline_session_events,
    timeline_experiment_events, timeline_log_events,
)

NOW = time.time()
HOUR = 3600.0


STORES = (EVENT_LOG_PATH, INCIDENTS_PATH, SESSIONS_PATH, EXPERIMENTS_PATH)


def fresh_files():
    """Clear this script's fixture stores. Safe by construction: _verify_sandbox (imported above,
    before app) has already pointed every store constant at a temp directory, so these paths cannot
    resolve to the machine's real history. tools/verify_isolation.py independently proves that the
    guarantee holds across the whole suite."""
    for p in STORES:
        if p.exists():
            p.unlink()


def bucket(ts):
    return {"start_timestamp": ts, "end_timestamp": ts + TELEMETRY_BUCKET_SECONDS, "sample_count": 30,
           "scalars": {"cpu_temp": {"avg": 45.0, "min": 44.0, "max": 46.0, "count": 30}}, "sensors": {}}


def contiguous_buckets(from_ts, to_ts):
    ts, out = from_ts, []
    while ts < to_ts:
        out.append(bucket(ts))
        ts += TELEMETRY_BUCKET_SECONDS
    return out


def incident_fixture(iid, component, start, dur=600, zone="ORANGE", peak=92.0, workload="game.exe",
                     duration_exact=True):
    return {"incident_id": iid, "component": component, "start_timestamp": start,
           "end_timestamp": start + dur, "duration_seconds": dur, "duration_exact": duration_exact,
           "max_zone": zone, "peak_value": peak, "dominant_workload": workload, "monitoring_gaps": []}


def session_fixture(sid, workload, start, dur=1800, cpu_peak=78.0, gpu_peak=88.0, incidents=0):
    return {"session_id": sid, "workload_key": workload.casefold(), "workload": workload,
           "start_timestamp": start, "end_timestamp": start + dur, "duration_seconds": dur,
           "cpu": {"avg_temp": 65.0, "peak_temp": cpu_peak, "avg_power": 90.0},
           "gpu": {"avg_hotspot_temp": 80.0, "peak_hotspot_temp": gpu_peak, "avg_power": 250.0},
           "incident_count": incidents, "zone_time": {}, "monitoring_gaps": []}


def main():
    fresh_files()
    start_ts, end_ts = NOW - 24 * HOUR, NOW

    print("=== 1. read_event_log_file: reads the log WITHOUT pruning or rewriting it ===")
    fresh_files()
    old = {"ts": NOW - 400 * 86400, "kind": "WARN", "text": "Ancient warning"}
    recent = {"ts": NOW - HOUR, "kind": "CRIT", "text": "CPU Package entered RED"}
    with EVENT_LOG_PATH.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(old) + "\n" + json.dumps(recent) + "\n")
    before_bytes = EVENT_LOG_PATH.read_bytes()
    records = read_event_log_file()
    assert len(records) == 2, records
    assert EVENT_LOG_PATH.read_bytes() == before_bytes, \
        "FAIL: reading the log for a view must never rewrite it (App.load_events prunes; this must not)"
    print("  PASS: both entries returned (including one 400 days old) and the file is byte-identical afterwards")

    print("\n=== 2. Malformed/incomplete log lines are skipped, never crash the timeline ===")
    with EVENT_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n\n" + json.dumps({"kind": "WARN", "text": "no timestamp"}) + "\n")
    assert len(read_event_log_file()) == 2, "FAIL: garbage and ts-less lines must be skipped silently"
    print("  PASS: unparseable lines, blank lines and an entry with no 'ts' are all skipped")

    print("\n=== 3. THE flight-recorder property: unmonitored time becomes a first-class entry ===")
    covered = contiguous_buckets(start_ts, start_ts + 2 * HOUR) + contiguous_buckets(NOW - 2 * HOUR, NOW)
    gaps = timeline_gap_events(covered, start_ts, end_ts)
    assert len(gaps) == 1, f"FAIL: expected exactly one gap between the two monitored stretches: {gaps}"
    gap = gaps[0]
    assert abs((gap["end_timestamp"] - gap["timestamp"]) - 20 * HOUR) < 120, gap
    assert gap["severity"] == "GAP" and gap["end_timestamp"] is not None
    assert "makes no claim" in "\n".join(gap["detail"]), gap["detail"]
    print(f"  PASS: a 20-hour hole between two monitored stretches becomes one 'not monitored' entry that "
          f"explicitly disclaims any knowledge of that period")

    print("\n=== 4. The gap threshold is the EXISTING TELEMETRY_GAP_BUCKETS, not a new number ===")
    assert TIMELINE_MIN_GAP_SECONDS == TELEMETRY_GAP_BUCKETS * TELEMETRY_BUCKET_SECONDS
    short_hole = (contiguous_buckets(start_ts, start_ts + HOUR)
                  + contiguous_buckets(start_ts + HOUR + TIMELINE_MIN_GAP_SECONDS - 60, end_ts))
    assert timeline_gap_events(short_hole, start_ts, end_ts) == [], \
        "FAIL: a hole shorter than the chart's own gap threshold must not be reported as unmonitored time"
    long_hole = (contiguous_buckets(start_ts, start_ts + HOUR)
                 + contiguous_buckets(start_ts + HOUR + TIMELINE_MIN_GAP_SECONDS + 60, end_ts))
    assert len(timeline_gap_events(long_hole, start_ts, end_ts)) == 1
    print(f"  PASS: a hole just under {TIMELINE_MIN_GAP_SECONDS:.0f}s is not a gap; just over it is - the same "
          f"threshold the historical chart already refuses to draw across")

    print("\n=== 5. A window with NO telemetry at all is one honest full-window gap, not an empty timeline ===")
    empty = timeline_gap_events([], start_ts, end_ts)
    assert len(empty) == 1 and abs((empty[0]["end_timestamp"] - empty[0]["timestamp"]) - 24 * HOUR) < 1e-6
    print("  PASS: a machine that wasn't running Thermal Watch gets a single full-window 'not monitored' entry "
          "- never suppressed for looking dramatic")

    print("\n=== 6. Each store is projected FAITHFULLY - the timeline never recomputes a source's numbers ===")
    inc = incident_fixture("inc-1", "cpu", NOW - 3 * HOUR, dur=600, zone="RED", peak=101.0)
    ev = timeline_incident_events([inc], start_ts, end_ts)[0]
    assert ev["source_id"] == "inc-1" and ev["severity"] == "RED" and ev["kind"] == "incident"
    assert "CPU Package — RED" == ev["title"], ev["title"]
    assert "Peak: 101°C" in ev["detail"] and "Dominant workload: game.exe" in ev["detail"]
    sess = session_fixture("s-1", "Cyberpunk2077.exe", NOW - 5 * HOUR, cpu_peak=78.0, gpu_peak=91.0, incidents=2)
    sev = timeline_session_events([sess], start_ts, end_ts)[0]
    assert sev["source_id"] == "s-1" and "GPU Hotspot peak: 91°C" in sev["detail"]
    assert "Incidents during this session: 2" in sev["detail"]
    exp = {"experiment_id": "exp-1", "change_timestamp": NOW - 6 * HOUR, "description": "Repasted GPU",
          "component": "gpu"}
    xev = timeline_experiment_events([exp], start_ts, end_ts)[0]
    assert xev["source_id"] == "exp-1" and xev["end_timestamp"] is None, \
        "FAIL: a hardware-change marker is a point in time, never a span"
    print("  PASS: incident/session/experiment entries carry their source id and their source record's own "
          "numbers verbatim; a marker stays a point in time")

    print("\n=== 7. An incident whose duration is not exact says so, rather than presenting it as measured ===")
    inexact = incident_fixture("inc-2", "gpu_hotspot", NOW - 4 * HOUR, duration_exact=False)
    detail = timeline_incident_events([inexact], start_ts, end_ts)[0]["detail"]
    assert any("not exact" in d for d in detail), detail
    print("  PASS: 'Duration contains an unmonitored interval and is not exact.' is carried onto the timeline")

    print("\n=== 8. INFO log chatter is filtered out by default; WARN/CRIT are kept ===")
    records = [{"ts": NOW - HOUR, "kind": "INFO", "text": "Polling interval set to 2000ms"},
              {"ts": NOW - HOUR, "kind": "WARN", "text": "Bridge stale"},
              {"ts": NOW - HOUR, "kind": "CRIT", "text": "CPU Package entered RED"}]
    kept = timeline_log_events(records, start_ts, end_ts)
    assert {e["title"] for e in kept} == {"Bridge stale", "CPU Package entered RED"}, kept
    assert len(timeline_log_events(records, start_ts, end_ts, kinds=("INFO", "WARN", "CRIT"))) == 3, \
        "FAIL: the INFO filter must be a parameter, not hard-coded - nothing is deleted, only not shown"
    assert TIMELINE_LOG_KINDS == ("WARN", "CRIT")
    print("  PASS: lifecycle INFO chatter is excluded by default, and including it is one argument away")

    print("\n=== 9. build_timeline: every store on ONE axis, newest first, with a deterministic tie-break ===")
    same_ts = NOW - 8 * HOUR
    incidents = [incident_fixture("inc-3", "cpu", same_ts)]
    sessions = [session_fixture("s-2", "game.exe", same_ts)]
    experiments = [{"experiment_id": "exp-2", "change_timestamp": same_ts, "description": "New fan",
                    "component": "system"}]
    buckets = contiguous_buckets(start_ts, end_ts)
    events = build_timeline(start_ts, end_ts, incidents=incidents, sessions=sessions,
                            experiments=experiments, buckets=buckets, log_records=[])
    timestamps = [e["timestamp"] for e in events]
    assert timestamps == sorted(timestamps, reverse=True), "FAIL: the timeline must be newest-first"
    tied = [e["kind"] for e in events if e["timestamp"] == same_ts]
    assert tied == sorted(tied, key=lambda k: TIMELINE_KIND_ORDER[k]), tied
    assert tied == ["experiment", "session", "incident"], f"FAIL: unstable tie-break ordering: {tied}"
    again = build_timeline(start_ts, end_ts, incidents=incidents, sessions=sessions,
                           experiments=experiments, buckets=buckets, log_records=[])
    assert [e["title"] for e in again] == [e["title"] for e in events], \
        "FAIL: the same stores must always render in the same order"
    print(f"  PASS: 3 stores merged newest-first; three entries sharing a timestamp always order "
          f"{tied} (context before the specific thing that happened)")

    print("\n=== 10. The `kinds` filter applies BEFORE the work, and an unknown kind yields nothing ===")
    only_incidents = build_timeline(start_ts, end_ts, incidents=incidents, sessions=sessions,
                                    experiments=experiments, buckets=buckets, log_records=[],
                                    kinds=("incident",))
    assert {e["kind"] for e in only_incidents} == {"incident"}, only_incidents
    assert build_timeline(start_ts, end_ts, incidents=incidents, sessions=sessions,
                          experiments=experiments, buckets=buckets, log_records=[], kinds=()) == []
    print("  PASS: kinds=('incident',) builds only incidents; kinds=() builds nothing at all")

    print("\n=== 11. summarize_timeline: coverage comes from the SAME buckets the gaps were derived from ===")
    half = contiguous_buckets(start_ts, start_ts + 12 * HOUR)
    half_events = build_timeline(start_ts, end_ts, incidents=[], sessions=[], experiments=[],
                                 buckets=half, log_records=[])
    summary = summarize_timeline(half_events, half, start_ts, end_ts)
    assert 45.0 <= summary["coverage_pct"] <= 55.0, summary
    assert abs(summary["gap_seconds"] - 12 * HOUR) < 120, summary
    assert summary["counts"]["gap"] == 1
    line = format_timeline_summary(summary)
    assert "Monitoring coverage: 50%" in line and "unmonitored:" in line, line
    print(f"  PASS: 12 of 24 hours monitored -> {line}")

    print("\n=== 11b. fmt_timeline_span: readable at timeline scale, byte-identical to fmt_dur below an hour ===")
    from app import fmt_dur, fmt_timeline_span  # noqa: E402
    for short in (0, 5, 59, 60, 90, 3599):
        assert fmt_timeline_span(short) == fmt_dur(short), \
            f"FAIL: sub-hour durations must render exactly as every other view already renders them: {short}"
    assert fmt_timeline_span(12 * HOUR) == "12h 00m", fmt_timeline_span(12 * HOUR)
    assert fmt_timeline_span(20 * 86400 + 5 * HOUR) == "20d 05h", fmt_timeline_span(20 * 86400 + 5 * HOUR)
    assert fmt_dur(12 * HOUR) == "720m 00s", "fmt_dur itself must stay untouched - other views depend on it"
    print("  PASS: <1h delegates to fmt_dur unchanged (720m 00s stays 720m 00s there); 12h -> '12h 00m', "
          "20d5h -> '20d 05h' on the timeline")

    print("\n=== 12. format_timeline_event: never fabricates a field the source record didn't capture ===")
    bare = incident_fixture("inc-4", "cpu", NOW - HOUR)
    bare["peak_value"] = None
    bare["duration_seconds"] = None
    lines = format_timeline_event(timeline_incident_events([bare], start_ts, end_ts)[0])
    joined = "\n".join(lines)
    assert "Peak: N/A" in joined and "Duration: N/A" in joined, joined
    assert lines[0].startswith("INCIDENT — CPU Package")
    point_lines = format_timeline_event(timeline_experiment_events([exp], start_ts, end_ts)[0])
    assert not any(l.startswith("End:") for l in point_lines), \
        "FAIL: a point-in-time entry must not print an End line"
    print("  PASS: missing values render as N/A rather than an invented number, and a point-in-time entry "
          "prints no End line")

    print("\n=== 13. TimelineWindow: real stores, real ranges, real selection through the full UI stack ===")
    fresh_files()
    with INCIDENTS_PATH.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(incident_fixture("inc-live", "cpu", NOW - 2 * HOUR, zone="RED")) + "\n")
    with SESSIONS_PATH.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(session_fixture("s-live", "Cyberpunk2077.exe", NOW - 3 * HOUR)) + "\n")
    with EVENT_LOG_PATH.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": NOW - HOUR, "kind": "CRIT", "text": "CPU Package entered RED"}) + "\n")
    app = App()
    hw = HistoryWindow(app)
    hw.open_timeline()
    app.update()
    win = hw.timeline_window
    hw.open_timeline()
    assert hw.timeline_window is win, "FAIL: open_timeline() must reuse the existing window (singleton)"
    kinds_shown = {win._row_event[r]["kind"] for r in win.tree.get_children()}
    assert {"incident", "session", "log"} <= kinds_shown, f"FAIL: expected all three stores merged: {kinds_shown}"
    rows = win.tree.get_children()
    win.tree.selection_set(rows[0])
    app.update()
    assert win.detail_text.cget("text") and "Start:" in win.detail_text.cget("text")
    print(f"  PASS: TimelineWindow opens as a singleton from History and merges {sorted(kinds_shown)} "
          f"from three separate real on-disk stores into {len(rows)} ordered rows")

    print("\n=== 14. Hiding a kind must NEVER change the reported coverage/unmonitored summary ===")
    summary_before = win.summary_label.cget("text")
    win.kind_vars["gap"].set(False)
    win._render()
    app.update()
    assert win.summary_label.cget("text") == summary_before, \
        "FAIL: a display filter changed the reported coverage - hiding rows must not restate what was true"
    assert all(win._row_event[r]["kind"] != "gap" for r in win.tree.get_children())
    win.kind_vars["gap"].set(True)
    win._render()
    for kind in win.kind_vars.values():
        kind.set(False)
    win._render()
    app.update()
    assert win.tree.get_children() == () and "Nothing recorded" in win.detail_text.cget("text")
    assert win.summary_label.cget("text") == summary_before, "FAIL: hiding EVERYTHING still must not restate coverage"
    for kind in win.kind_vars.values():
        kind.set(True)
    win._render()
    print("  PASS: hiding the 'not monitored' rows (and then every row) leaves the coverage summary "
          "byte-identical - the filter changes what you read, never what was true")

    print("\n=== 15. Changing range re-reads and rebuilds; every range key is real ===")
    for key in win.RANGE_ORDER:
        assert key in TIMELINE_RANGE_SECONDS, key
        win.range_var.set(key)
        win._reload()
        app.update()
    assert win.range_var.get() == "30d"
    win.destroy(); hw.destroy()
    app.stop_event.set(); app.destroy()
    print(f"  PASS: all {len(win.RANGE_ORDER)} ranges ({', '.join(win.RANGE_ORDER)}) rebuild cleanly against "
          f"the real stores with no exception")

    print("\n=== 16. Read-only: the timeline never writes to any store it reads ===")
    src = inspect.getsource(appmod)
    # Bound the slice at the NEXT module section, not at class MEMORYSTATUSEX. The Scheduled Health
    # Reports layer was later inserted between the timeline block and that class, and it writes to
    # its own store legitimately - an end marker that drifts as code is added would silently start
    # scanning unrelated code (it did, and this check failed for the wrong reason). The check itself
    # is unchanged; only the region it is pointed at is now pinned to the timeline layer.
    timeline_start = src.index("# Unified Flight Recorder Timeline")
    timeline_end = src.index("# Scheduled Health Reports", timeline_start)
    timeline_src = src[timeline_start:timeline_end]
    assert "def build_timeline(" in timeline_src and "def summarize_timeline(" in timeline_src, \
        "FAIL: the timeline source slice no longer covers the timeline layer - fix the markers"
    assert "def build_report_payload(" not in timeline_src, "the slice must stop before the reports layer"
    window_src = inspect.getsource(TimelineWindow)
    for forbidden in (".open(\"w\"", ".open(\"a\"", ".write_text(", ".unlink(", ".replace("):
        assert forbidden not in timeline_src, f"FAIL: the timeline layer writes to disk: {forbidden}"
        assert forbidden not in window_src, f"FAIL: TimelineWindow writes to disk: {forbidden}"
    print("  PASS: neither the timeline layer nor its window contains any write/delete call - it only merges")

    print("\n=== 17. The timeline never runs on the live 2s poll or on session/app close ===")
    update_src = inspect.getsource(App.update_data)
    close_src = inspect.getsource(App.close)
    for name in ("build_timeline(", "summarize_timeline(", "read_event_log_file(", "timeline_gap_events("):
        assert name not in update_src, f"FAIL: update_data() must never call {name} on the 2s poll"
        assert name not in close_src, f"FAIL: timeline computation must never run automatically on close"
    print("  PASS: update_data()/close() contain no timeline computation - display-only, on-demand")

    fresh_files()
    print("\nALL FLIGHT RECORDER TIMELINE CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
