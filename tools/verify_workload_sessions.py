"""Verification for Workload Session Tracking: start debounce, idle grace, multi-workload
independence, canonical identity, PID-restart handling, streaming aggregate correctness,
missing-value semantics, thermal-zone time accounting, monitoring-gap-aware restart
durability, conservative incident linking, and Application Analytics session/incident
count separation. Drives the session engine directly (App._session_observe_tick() and
friends) with synthetic workload/telemetry snapshots - never touches real hardware/process
sampling, and real time.sleep() is avoided in favor of directly controlling the engine's
internal clock reference and stored timestamps, since SESSION_IDLE_GRACE_S=90s would make a
real-time test suite far too slow."""
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (App, AnalyticsWindow, SESSIONS_PATH, ACTIVE_SESSIONS_PATH,  # noqa: E402
                 SESSION_CPU_ACTIVE_PCT, SESSION_GPU_ACTIVE_PCT, SESSION_START_DEBOUNCE_SAMPLES,
                 SESSION_IDLE_GRACE_S, SESSION_GAP_THRESHOLD_S,
                 _agg_new, _agg_add, _agg_result, _normalize_workload_name,
                 read_sessions_file, group_sessions_by_workload)


def fresh_files():
    for p in (SESSIONS_PATH, ACTIVE_SESSIONS_PATH):
        if p.exists():
            p.unlink()


def tick(app, cpu_top=(), gpu_top=(), foreground=None, context=None, dt=None):
    """Feeds one synthetic snapshot to the session engine, exactly like update_data() would
    after a real 2s poll - only the fields _session_observe_tick() actually reads."""
    app.last_cpu_top = list(cpu_top)
    app.last_gpu_top = list(gpu_top)
    app.last_foreground = foreground
    app.last_context = context or {}
    if dt is not None:
        app._session_last_tick_time = time.time() - dt
    app._session_observe_tick()


def main():
    fresh_files()

    print("=== 1. pure aggregate helpers: streaming count/sum/max, missing values stay missing ===")
    agg = _agg_new()
    for v in (10.0, None, 20.0, 30.0):
        _agg_add(agg, v)
    r = _agg_result(agg)
    assert r == {"avg": 20.0, "peak": 30.0, "count": 3}, f"FAIL: {r}"
    assert _agg_result(_agg_new()) is None, "FAIL: an untouched aggregate must report None, not 0"
    print("  PASS: avg/peak/count correct, a None sample contributes nothing, empty aggregate is None")

    print("\n=== 2. _normalize_workload_name: case merge, no fuzzy merge, missing -> Not identified ===")
    assert _normalize_workload_name("Cyberpunk2077.exe")[0] == _normalize_workload_name("cyberpunk2077.exe")[0]
    assert _normalize_workload_name("python.exe")[0] == "python.exe"
    assert _normalize_workload_name(None)[0] == "not identified"
    print("  PASS")

    print("\n=== 3. a single 2-second spike creates no session ===")
    app = App()
    tick(app, cpu_top=[("Cyberpunk2077.exe", 111, 90.0)])
    tick(app, cpu_top=[])  # drops immediately - only one qualifying sample ever
    assert "cyberpunk2077.exe" not in app.workload_sessions, "FAIL: a single spike must not create a session"
    print("  PASS: candidate created and dropped on the very next non-qualifying tick, no session opened")

    print(f"\n=== 4. sustained CPU activity opens exactly one session after {SESSION_START_DEBOUNCE_SAMPLES} samples ===")
    for i in range(SESSION_START_DEBOUNCE_SAMPLES - 1):
        tick(app, cpu_top=[("Cyberpunk2077.exe", 111, SESSION_CPU_ACTIVE_PCT + 5)])
        assert "cyberpunk2077.exe" not in app.workload_sessions or not app.workload_sessions["cyberpunk2077.exe"]["confirmed"], \
            f"FAIL: session confirmed too early, at sample {i + 1}"
    tick(app, cpu_top=[("Cyberpunk2077.exe", 111, SESSION_CPU_ACTIVE_PCT + 5)])
    assert "cyberpunk2077.exe" in app.workload_sessions
    sess = app.workload_sessions["cyberpunk2077.exe"]
    assert sess["confirmed"] and sess["session_id"], "FAIL: session should be confirmed after debounce"
    first_session_id = sess["session_id"]
    print(f"  PASS: confirmed after exactly {SESSION_START_DEBOUNCE_SAMPLES} consecutive samples, session_id={first_session_id}")

    print("\n=== 5. sustained GPU-only activity (no CPU) also opens a session ===")
    for _ in range(SESSION_START_DEBOUNCE_SAMPLES):
        tick(app, gpu_top=[("blender.exe", 222, SESSION_GPU_ACTIVE_PCT + 10)])
    assert "blender.exe" in app.workload_sessions and app.workload_sessions["blender.exe"]["confirmed"]
    print("  PASS: GPU-only sustained activity confirms a session independent of CPU%")

    print("\n=== 6. brief utilization drop does not close an already-confirmed session ===")
    tick(app, cpu_top=[])  # Cyberpunk drops out for one tick
    assert "cyberpunk2077.exe" in app.workload_sessions, "FAIL: a single missed tick must not close a confirmed session"
    assert app.workload_sessions["cyberpunk2077.exe"]["session_id"] == first_session_id
    print("  PASS: confirmed session survives a brief drop (idle grace, not immediate close)")

    print("\n=== 7. activity returning within the grace period continues the SAME session_id ===")
    app.workload_sessions["cyberpunk2077.exe"]["last_active_timestamp"] -= (SESSION_IDLE_GRACE_S - 20)
    tick(app, cpu_top=[("Cyberpunk2077.exe", 111, 95.0)])
    assert app.workload_sessions["cyberpunk2077.exe"]["session_id"] == first_session_id, \
        "FAIL: resuming within grace must not start a new session_id"
    print("  PASS: same session_id continues after resuming activity inside the grace window")

    print("\n=== 8. inactivity beyond the grace period closes the session exactly once ===")
    app.workload_sessions["cyberpunk2077.exe"]["last_active_timestamp"] -= (SESSION_IDLE_GRACE_S + 5)
    before_recent = len(app.sessions_recent)
    tick(app, cpu_top=[])
    assert "cyberpunk2077.exe" not in app.workload_sessions, "FAIL: session should be closed and removed"
    assert len(app.sessions_recent) == before_recent + 1
    closed = app.sessions_recent[0]
    assert closed["session_id"] == first_session_id
    assert closed["close_reason"] == "idle_grace_expired"
    assert closed["duration_exact"] is True
    tick(app, cpu_top=[])  # one more idle tick must not close it again / duplicate it
    assert len(app.sessions_recent) == before_recent + 1, "FAIL: closed exactly once, not repeatedly"
    print(f"  PASS: closed exactly once after grace expiry, close_reason={closed['close_reason']!r}, "
          f"duration={closed['duration_seconds']:.1f}s")

    print("\n=== 9. two simultaneous workloads maintain fully independent sessions ===")
    fresh_files()  # isolate from test 5/8's still-confirmed "blender.exe" leftover on disk
    app2 = App()
    for _ in range(SESSION_START_DEBOUNCE_SAMPLES):
        tick(app2, cpu_top=[("Cyberpunk2077.exe", 111, 90.0)], gpu_top=[("obs64.exe", 333, 40.0)])
    assert "cyberpunk2077.exe" in app2.workload_sessions and app2.workload_sessions["cyberpunk2077.exe"]["confirmed"]
    assert "obs64.exe" in app2.workload_sessions and app2.workload_sessions["obs64.exe"]["confirmed"]
    assert app2.workload_sessions["cyberpunk2077.exe"]["session_id"] != app2.workload_sessions["obs64.exe"]["session_id"]
    print("  PASS: Cyberpunk2077.exe and obs64.exe get independent, simultaneously-active sessions")

    print("\n=== 10. case differences across ticks stay ONE session, not duplicated ===")
    app3 = App()
    names = ["Cyberpunk2077.exe", "cyberpunk2077.exe", "CYBERPUNK2077.EXE"]
    for i in range(SESSION_START_DEBOUNCE_SAMPLES):
        tick(app3, cpu_top=[(names[i % len(names)], 111, 90.0)])
    assert len(app3.workload_sessions) == 1, f"FAIL: case variants created {len(app3.workload_sessions)} sessions"
    assert "cyberpunk2077.exe" in app3.workload_sessions
    print("  PASS: alternating casing across ticks still resolves to exactly one session")

    print("\n=== 11. PID restart within grace records BOTH pids, no duplicate session ===")
    for _ in range(SESSION_START_DEBOUNCE_SAMPLES):
        tick(app3, cpu_top=[("blender.exe", 4001, 80.0)])
    sid = app3.workload_sessions["blender.exe"]["session_id"]
    assert app3.workload_sessions["blender.exe"]["observed_pids"] == [4001]
    tick(app3, cpu_top=[])  # old PID exits
    app3.workload_sessions["blender.exe"]["last_active_timestamp"] -= 30  # still within grace
    tick(app3, cpu_top=[("blender.exe", 4002, 80.0)])  # restarted with a new PID
    sess3 = app3.workload_sessions["blender.exe"]
    assert sess3["session_id"] == sid, "FAIL: PID restart within grace must continue the same session"
    assert set(sess3["observed_pids"]) == {4001, 4002}, f"FAIL: expected both PIDs, got {sess3['observed_pids']}"
    assert sess3["starting_pid"] == 4001, "FAIL: starting_pid must stay the ORIGINAL pid"
    print(f"  PASS: session_id unchanged, observed_pids={sess3['observed_pids']}, starting_pid={sess3['starting_pid']}")

    print("\n=== 12. streaming averages/peaks accumulate correctly over real ticks ===")
    fresh_files()
    app4 = App()
    temps = [70.0, 80.0, 60.0]
    for t in temps:
        tick(app4, cpu_top=[("python.exe", 55, 50.0)], context={"cpu_temp": t}, dt=2.0)
    rec = app4.workload_sessions["python.exe"]
    result = _agg_result(rec["agg"]["cpu_temp"])
    assert result["count"] == 3 and result["peak"] == 80.0 and abs(result["avg"] - 70.0) < 1e-9, f"FAIL: {result}"
    print(f"  PASS: cpu_temp aggregate over 3 ticks = {result}")

    print("\n=== 13. missing sensor values remain missing after close (never fabricated as 0) ===")
    app5 = App()
    for _ in range(SESSION_START_DEBOUNCE_SAMPLES):
        tick(app5, cpu_top=[("python.exe", 55, 50.0)],
            context={"cpu_temp": 70.0, "gpu_power": None, "mem_pct": 40.0}, dt=2.0)
    app5.workload_sessions["python.exe"]["last_active_timestamp"] -= (SESSION_IDLE_GRACE_S + 5)
    tick(app5, cpu_top=[])
    closed5 = app5.sessions_recent[0]
    assert closed5["cpu"]["avg_temp"] is not None
    assert closed5["gpu"]["avg_power"] is None, "FAIL: gpu_power was never observed - must stay None, not 0"
    assert closed5["memory"]["avg_pct"] == 40.0
    print("  PASS: never-observed gpu_power stays None; observed metrics compute correctly")

    print("\n=== 14. thermal-zone observed-time accounting is correct ===")
    app6 = App()
    # 80C = CPU YELLOW zone (CPU_YELLOW=80, CPU_ORANGE=90). 3 ticks at exactly dt=2.0s each.
    for _ in range(SESSION_START_DEBOUNCE_SAMPLES):
        tick(app6, cpu_top=[("blender.exe", 77, 80.0)], context={"cpu_temp": 85.0}, dt=2.0)
    zt = app6.workload_sessions["blender.exe"]["zone_time"]["cpu"]
    # Tolerance is generous (50ms) since dt is derived from two real time.time() calls a few
    # bytecode instructions apart - not exact wall-clock control, just close enough to verify
    # the RIGHT zone got the RIGHT elapsed time, not an off-by-orders-of-magnitude bug.
    assert abs(zt["YELLOW"] - 6.0) < 0.05, f"FAIL: expected ~6.0s in YELLOW (3 ticks x 2.0s), got {zt}"
    assert zt["ORANGE"] == 0.0 and zt["RED"] == 0.0
    print(f"  PASS: {SESSION_START_DEBOUNCE_SAMPLES} ticks x 2.0s all landed in CPU YELLOW: zone_time={zt}")

    print("\n=== 15. an unmonitored gap (dt > SESSION_GAP_THRESHOLD_S) adds NO fake zone time ===")
    zt_before = dict(app6.workload_sessions["blender.exe"]["zone_time"]["cpu"])
    tick(app6, cpu_top=[("blender.exe", 77, 80.0)], context={"cpu_temp": 85.0}, dt=SESSION_GAP_THRESHOLD_S + 5)
    zt_after = app6.workload_sessions["blender.exe"]["zone_time"]["cpu"]
    assert zt_after == zt_before, f"FAIL: a >10s gap must contribute zero zone time, before={zt_before} after={zt_after}"
    print(f"  PASS: a {SESSION_GAP_THRESHOLD_S + 5:.0f}s tick gap added no zone time (still {zt_after})")

    print("\n=== 16. conservative incident linking: matches an active session, ignores the rest ===")
    app7 = App()
    for _ in range(SESSION_START_DEBOUNCE_SAMPLES):
        tick(app7, cpu_top=[("Cyberpunk2077.exe", 111, 90.0)])
    matching_inc = {"incident_id": "cpu-1", "dominant_workload": "Cyberpunk2077.exe", "max_zone": "ORANGE"}
    unrelated_inc = {"incident_id": "cpu-2", "dominant_workload": "notepad.exe", "max_zone": "RED"}
    no_session_inc = {"incident_id": "cpu-3", "dominant_workload": "steam.exe", "max_zone": "YELLOW"}
    app7.incidents_recent.appendleft(no_session_inc)
    app7.incidents_recent.appendleft(unrelated_inc)
    app7.incidents_recent.appendleft(matching_inc)
    app7._session_link_incidents()
    linked = app7.workload_sessions["cyberpunk2077.exe"]["incident_ids"]
    assert linked == ["cpu-1"], f"FAIL: expected only cpu-1 linked, got {linked}"
    assert app7.workload_sessions["cyberpunk2077.exe"]["max_incident_severity"] == "ORANGE"
    app7._session_link_incidents()  # idempotent - must not double-link on a second pass
    assert app7.workload_sessions["cyberpunk2077.exe"]["incident_ids"] == ["cpu-1"]
    print("  PASS: only the incident matching an active session's workload was linked, exactly once")

    print("\n=== 17. restart durability: workload still active resumes the SAME session_id ===")
    fresh_files()
    app8 = App()
    for _ in range(SESSION_START_DEBOUNCE_SAMPLES):
        tick(app8, cpu_top=[("Cyberpunk2077.exe", 111, 90.0)], context={"cpu_temp": 75.0}, dt=2.0)
    original_id = app8.workload_sessions["cyberpunk2077.exe"]["session_id"]
    app8._save_active_sessions()
    app8.stop_event.set(); app8.destroy()

    app9 = App()
    app9.load_active_sessions()
    assert "cyberpunk2077.exe" in app9.session_restore_pending, "FAIL: active session was not restored as pending"
    tick(app9, cpu_top=[("Cyberpunk2077.exe", 111, 90.0)])  # a live tick observes it's still active
    app9._reconcile_restored_sessions()
    assert "cyberpunk2077.exe" in app9.workload_sessions, "FAIL: still-active session should resume"
    resumed = app9.workload_sessions["cyberpunk2077.exe"]
    assert resumed["session_id"] == original_id, "FAIL: resumed session must keep its original session_id"
    assert resumed["confirmed"]
    print(f"  PASS: session {original_id} resumed after restart, still confirmed, same id")
    # Let it end normally (idle-grace) so test 19 below has a genuinely COMPLETED, persisted
    # record to look for - a "resumed and still active" session isn't in SESSIONS_PATH yet.
    app9.workload_sessions["cyberpunk2077.exe"]["last_active_timestamp"] -= (SESSION_IDLE_GRACE_S + 5)
    tick(app9, cpu_top=[])
    assert "cyberpunk2077.exe" not in app9.workload_sessions
    app9.stop_event.set(); app9.destroy()

    print("\n=== 18. restart durability: workload ended offline closes with EXPLICIT uncertainty ===")
    # Deliberately no fresh_files() here - SESSIONS_PATH must keep accumulating completed
    # sessions across "restarts" exactly like the real app would; only ACTIVE_SESSIONS_PATH
    # reflects transient in-flight state, and test 17 already left it clean (nothing left
    # confirmed-and-open after closing cyberpunk above).
    app10 = App()
    for _ in range(SESSION_START_DEBOUNCE_SAMPLES):
        tick(app10, cpu_top=[("blender.exe", 222, 80.0)], context={"cpu_temp": 75.0}, dt=2.0)
    offline_id = app10.workload_sessions["blender.exe"]["session_id"]
    app10._save_active_sessions()
    app10.stop_event.set(); app10.destroy()

    app11 = App()
    app11.load_active_sessions()
    assert "blender.exe" in app11.session_restore_pending
    # No live activity at all this time - blender.exe never appears in any tick.
    app11._reconcile_restored_sessions()
    assert "blender.exe" not in app11.workload_sessions, "FAIL: must not silently resume with no evidence"
    on_disk = read_sessions_file()
    closed_rec = next((s for s in on_disk if s["session_id"] == offline_id), None)
    assert closed_rec is not None, "FAIL: the offline-ended session must still be persisted as completed"
    assert closed_rec["duration_exact"] is False, "FAIL: an offline-ended session must be marked uncertain"
    assert closed_rec["close_reason"] == "ended_during_gap"
    assert closed_rec["monitoring_gaps"], "FAIL: monitoring-gap metadata must be preserved"
    print(f"  PASS: session {offline_id} closed with duration_exact=False, close_reason={closed_rec['close_reason']!r}, "
          f"{len(closed_rec['monitoring_gaps'])} monitoring gap(s) recorded - no fabricated end time")

    print("\n=== 19. completed sessions persist across restart ===")
    app12 = App()
    reloaded = [s["session_id"] for s in app12.sessions_recent]
    assert original_id in reloaded and offline_id in reloaded, \
        f"FAIL: expected both {original_id} and {offline_id} reloaded from disk, got {reloaded}"
    print(f"  PASS: {len(reloaded)} session(s) reloaded from SESSIONS_PATH on a fresh App(), including both above")

    print("\n=== 20. background-app noise (low, sub-threshold utilization) creates no session spam ===")
    app13 = App()
    for _ in range(50):
        tick(app13, cpu_top=[("Discord.exe", 999, 3.0)], gpu_top=[("Discord.exe", 999, 1.5)])
    assert "discord.exe" not in app13.workload_sessions, \
        "FAIL: 50 ticks of sub-threshold Discord.exe activity must never create a session"
    assert len(app13.workload_sessions) == 0
    print("  PASS: 50 ticks of realistic background-app noise (3% CPU / 1.5% GPU) created zero sessions")

    print("\n=== 21. Application Analytics distinguishes recorded sessions from associated incidents ===")
    incidents = [{"incident_id": "i1", "dominant_workload": "Cyberpunk2077.exe", "max_zone": "ORANGE",
                 "end_timestamp": time.time(), "start_timestamp": time.time() - 60,
                 "duration_seconds": 60, "duration_exact": True, "component": "cpu", "peak_value": 90.0}
                for _ in range(12)]
    sessions = [{"session_id": f"s{i}", "workload": "Cyberpunk2077.exe", "start_timestamp": time.time() - 100,
                "end_timestamp": time.time(), "duration_seconds": 100, "duration_exact": True,
                "incident_count": 0} for i in range(5)]
    with mock.patch("app.read_incidents_file", return_value=incidents), \
        mock.patch("app.read_sessions_file", return_value=sessions):
        app14 = App()
        win = AnalyticsWindow(app14)
        win.range_var.set("All")
        win._recompute()
        row = next(r for r in (win.tree.item(i)["values"] for i in win.tree.get_children())
                  if r[0] == "Cyberpunk2077.exe")
        # columns: workload, sessions, incidents, critical, worst
        assert row[1] == 5, f"FAIL: expected SESSIONS column = 5, got {row}"
        assert row[2] == 12, f"FAIL: expected INCIDENTS column = 12, got {row}"
        first_iid = win.tree.get_children()[0]
        win.tree.selection_set(first_iid)
        win._on_select(None)
        detail = win.detail_text.cget("text")
        assert "Recorded sessions          5" in detail
        assert "Associated incidents      12" in detail
        win.destroy()
        app14.stop_event.set(); app14.destroy()
    print("  PASS: ranking table and detail panel both show sessions=5 distinct from incidents=12")

    # app8, app9, app10, and app14 were already destroyed inline above, each right after its own test.
    for a in (app, app2, app3, app4, app5, app6, app7, app11, app12, app13):
        a.stop_event.set()
        a.destroy()

    fresh_files()
    print("\nALL WORKLOAD SESSION CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
