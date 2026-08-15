"""Regression test for the v1.0 sleep/resume honesty bug.

THE BUG (found by the release gate, 2026-08-14, real 10.5-minute S3 suspend)
Every monitoring-gap path in the app was reachable only through RESTART reconciliation
(_reconcile_restored_incidents / _reconcile_restored_sessions). A system suspend does not restart
the process - it freezes it - so nothing ever noticed that time.time() had jumped 630 seconds.
The observed damage, from the real run:

  * an ORANGE incident open across the sleep was persisted as
        duration=951.9s  duration_exact=True  monitoring_gap=None
    claiming a precisely measured 15.9-minute event of which 630s (66%) was never observed
  * the open 60s telemetry bucket stretched to 701 seconds (22 samples) instead of closing
  * because that bucket reported continuous coverage, timeline_gap_events() emitted ZERO gaps,
    while compute_coverage() over the same buckets reported 38.1% - the two layers contradicted
    each other, breaking the invariant summarize_timeline() documents as impossible

This test reproduces that shape deterministically: no real sleep, no real hardware, no waiting.
Time is controlled by monkeypatching time.time, so a ~630s wall-clock jump happens between two
consecutive live ticks WITHOUT the process restarting - exactly the condition that was missed.

It runs headless against the real engines (no Tk mainloop, no widgets) by driving the same
methods update_data() drives, in the same order.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _verify_sandbox  # noqa: F401  (redirects every store into a temp dir - must precede `app`)

import time as _time  # noqa: E402
import app as appmod  # noqa: E402
from app import (  # noqa: E402
    MONITORING_DISCONTINUITY_S, TELEMETRY_BUCKET_SECONDS, TELEMETRY_GAP_BUCKETS,
    TIMELINE_MIN_GAP_SECONDS, POLL_SECONDS, SESSION_GAP_THRESHOLD_S,
    read_incidents_file, read_sessions_file, read_telemetry_file,
    timeline_gap_events, compute_coverage, build_timeline,
)

sys.stdout.reconfigure(encoding="utf-8")

FAILURES = []
CHECKS = [0]


def check(name, condition, detail=""):
    CHECKS[0] += 1
    ok = bool(condition)
    print(f"[{'PASS' if ok else 'FAIL'}] {CHECKS[0]:2d}. {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILURES.append(name)


# ---------------------------------------------------------------------------
# Controlled clock: the whole point is a wall-clock jump with no restart.
# ---------------------------------------------------------------------------
class Clock:
    def __init__(self, start=1_800_000_000.0):
        self.t = start

    def advance(self, seconds):
        self.t += seconds
        return self.t

    def __call__(self):
        return self.t


CLOCK = Clock()
_REAL_TIME = _time.time
appmod.time.time = CLOCK


# ---------------------------------------------------------------------------
# A headless App: real engine methods, no Tk. Only the widget/scheduling surface the engines
# touch is stubbed - every method under test is the real one, called unbound on this object.
# ---------------------------------------------------------------------------
class HeadlessApp:
    # the real implementations under test
    _detect_monitoring_discontinuity = appmod.App._detect_monitoring_discontinuity
    _apply_monitoring_discontinuity = appmod.App._apply_monitoring_discontinuity
    _telemetry_split_across_gap = appmod.App._telemetry_split_across_gap
    # staticmethods must be re-wrapped: copied as plain attributes they would rebind as
    # instance methods and receive `self` as their first argument.
    _append_gap_once = staticmethod(appmod.App._append_gap_once)
    _incidents_record_live_gap = appmod.App._incidents_record_live_gap
    _sessions_record_live_gap = appmod.App._sessions_record_live_gap
    _telemetry_observe_tick = appmod.App._telemetry_observe_tick
    _telemetry_finalize_bucket = appmod.App._telemetry_finalize_bucket
    _persist_telemetry_bucket = appmod.App._persist_telemetry_bucket
    _incident_open = appmod.App._incident_open
    _incident_touch = appmod.App._incident_touch
    _incident_close = appmod.App._incident_close
    _incident_to_persistable = staticmethod(appmod.App._incident_to_persistable)
    _finalize_session_record = appmod.App._finalize_session_record
    _dominant_workload = staticmethod(appmod.App._dominant_workload)
    _persist_incident = appmod.App._persist_incident

    def __init__(self):
        self.incidents_active = {}
        self.workload_sessions = {}
        self.incidents_recent = appmod.deque(maxlen=500)
        self.sessions_recent = appmod.deque(maxlen=500)
        self.incident_restore_pending = {}
        self.last_context = {}
        self.last_cpu_top = []
        self.last_gpu_top = []
        self.last_component_values = {}
        self._active_incidents_dirty = False
        self._active_sessions_dirty = False
        self._last_tick_wall_time = CLOCK()
        self.telemetry_bucket = appmod._new_telemetry_bucket(CLOCK())
        self.logged = []

    # -- stubbed surface (nothing under test) --
    def log_event(self, kind, text, meta=None):
        self.logged.append({"kind": kind, "text": text, "meta": meta, "ts": CLOCK()})

    def _save_active_incidents(self):
        # exercise the real persistable projection so a leaked internal key would surface here
        self.persisted_active = {k: self._incident_to_persistable(k, v)
                                 for k, v in self.incidents_active.items()}

    def _current_attribution(self, bias):
        return {"foreground_process": "Game.exe", "foreground_title": "Game",
                "top_cpu_processes": [], "top_gpu_processes": []}


def tick(app, cpu_temp, alerting=True, zone="ORANGE"):
    """One live poll, in update_data()'s real order: discontinuity check first, then observe."""
    gap = app._detect_monitoring_discontinuity()
    if gap is not None:
        app._apply_monitoring_discontinuity(gap)
    app.last_context = {"cpu_temp": cpu_temp}
    app.last_component_values["cpu"] = cpu_temp
    if alerting:
        if "cpu" not in app.incidents_active:
            app._incident_open("cpu", "cpu", "CPU Package", "/amdcpu/0/temperature/2", zone, cpu_temp)
        app._incident_touch("cpu", zone, cpu_temp)
    elif "cpu" in app.incidents_active:
        app._incident_close("cpu", cpu_temp)
    app._telemetry_observe_tick([])
    return gap


print("=" * 78)
print("SLEEP/RESUME MONITORING-GAP REGRESSION TEST")
print(f"MONITORING_DISCONTINUITY_S={MONITORING_DISCONTINUITY_S} "
      f"(= TIMELINE_MIN_GAP_SECONDS={TIMELINE_MIN_GAP_SECONDS} "
      f"= {TELEMETRY_GAP_BUCKETS} x {TELEMETRY_BUCKET_SECONDS}s)")
print("=" * 78)

# ---------------------------------------------------------------------------
# 1-3. Normal cadence and ordinary jitter must NOT produce a gap.
# ---------------------------------------------------------------------------
print("\n--- normal cadence and jitter (must never produce a gap) ---")
jitter_app = HeadlessApp()
no_gap = True
for _ in range(20):
    CLOCK.advance(POLL_SECONDS)
    if tick(jitter_app, 70.0, alerting=False) is not None:
        no_gap = False
check("2s normal cadence produces no monitoring gap", no_gap,
      f"20 ticks at {POLL_SECONDS}s")

no_gap = True
for delay in (3, 4, 5, 5, 4, 3):
    CLOCK.advance(delay)
    if tick(jitter_app, 70.0, alerting=False) is not None:
        no_gap = False
check("3-5s scheduling delays produce no monitoring gap", no_gap,
      "delays 3,4,5,5,4,3 - all well under the threshold")

no_gap = True
for delay in (30, 60, 120, MONITORING_DISCONTINUITY_S - 1):
    CLOCK.advance(delay)
    if tick(jitter_app, 70.0, alerting=False) is not None:
        no_gap = False
check("stalls below the threshold produce no monitoring gap", no_gap,
      f"30s, 60s, 120s and {MONITORING_DISCONTINUITY_S - 1}s (one second under)")

CLOCK.advance(MONITORING_DISCONTINUITY_S)
check("a stall exactly at the threshold DOES produce a gap",
      tick(jitter_app, 70.0, alerting=False) is not None,
      f"{MONITORING_DISCONTINUITY_S}s - the boundary is inclusive")

# ---------------------------------------------------------------------------
# The main reproduction: ORANGE incident + open bucket + ~630s jump, no restart.
# ---------------------------------------------------------------------------
print("\n--- the real failure: ORANGE incident open across a 630s suspend ---")
app = HeadlessApp()
SLEEP_SECONDS = 630.0

pre_start = CLOCK()
# 125 ticks = 250s: deliberately NOT a whole multiple of the 60s bucket width, so a bucket is
# genuinely still in progress when the suspend hits. That is the condition that produced the
# 701-second bucket; landing exactly on a boundary would quietly test nothing.
for _ in range(125):
    CLOCK.advance(POLL_SECONDS)
    tick(app, 92.0, alerting=True, zone="ORANGE")

incident_id_before = app.incidents_active["cpu"]["incident_id"]
bucket_before = dict(app.telemetry_bucket)
last_tick_before_sleep = CLOCK()
check("an ORANGE incident is open before the suspend", "cpu" in app.incidents_active,
      f"incident_id={incident_id_before}")
check("a telemetry bucket is in progress before the suspend",
      app.telemetry_bucket["sample_count"] > 0,
      f"sample_count={bucket_before['sample_count']}")

# ---- the suspend: wall clock jumps, process does NOT restart ----
CLOCK.advance(SLEEP_SECONDS)
resume_ts = CLOCK()
gap = tick(app, 95.0, alerting=True, zone="ORANGE")   # first post-resume tick, still hot

check("the wall-clock jump is detected as a monitoring gap", gap is not None,
      f"gap_seconds={gap['gap_seconds'] if gap else None}")
check("no restart was involved", not app.incident_restore_pending,
      "incident_restore_pending is empty - the restart path never ran")

inc = app.incidents_active.get("cpu")
check("the incident keeps the SAME incident_id across the gap",
      inc is not None and inc["incident_id"] == incident_id_before,
      f"{incident_id_before} -> {inc['incident_id'] if inc else None}")
check("the still-open incident is marked duration_exact=False",
      inc is not None and inc.get("duration_exact") is False,
      f"duration_exact={inc.get('duration_exact') if inc else None}")
check("monitoring_gaps[] contains the outage",
      inc is not None and len(inc.get("monitoring_gaps", [])) == 1,
      f"gaps={inc.get('monitoring_gaps') if inc else None}")
g0 = (inc.get("monitoring_gaps") or [{}])[0] if inc else {}
check("the gap records the true observed bounds",
      abs(g0.get("last_sample_before", 0) - last_tick_before_sleep) < 0.001
      and abs(g0.get("first_sample_after", 0) - resume_ts) < 0.001
      and abs(g0.get("gap_seconds", 0) - SLEEP_SECONDS) < 0.001,
      f"last_sample_before={g0.get('last_sample_before')} "
      f"first_sample_after={g0.get('first_sample_after')} "
      f"gap_seconds={g0.get('gap_seconds')}")
check("still-alerting after resume clears the pending-recovery flag",
      "_live_gap_pending" not in inc,
      "it never recovered during the gap, so uncertain-recovery must NOT apply")
check("the internal flag is never persisted",
      "_live_gap_pending" not in app.persisted_active.get("cpu", {}),
      "a stale flag surviving a restart would mis-mark a later ordinary recovery")

# ---------------------------------------------------------------------------
# Telemetry bucket integrity across the gap.
# ---------------------------------------------------------------------------
print("\n--- telemetry buckets ---")
for _ in range(60):  # 2 more minutes after resume
    CLOCK.advance(POLL_SECONDS)
    tick(app, 95.0, alerting=True, zone="ORANGE")
app._telemetry_finalize_bucket(CLOCK())

# Every phase of this script shares one sandbox store, so scope the bucket assertions to the
# reproduction's own window - otherwise the jitter phase's deliberate sub-threshold stalls (which
# legitimately widen a bucket without declaring a gap) would be counted as this phase's buckets.
buckets = sorted((b for b in read_telemetry_file() if b["start_timestamp"] >= pre_start),
                 key=lambda b: b["start_timestamp"])
widths = [(b["end_timestamp"] - b["start_timestamp"]) for b in buckets if b.get("end_timestamp")]
widest = max(widths) if widths else 0
check("no bucket stretches across the gap", widest < SLEEP_SECONDS,
      f"widest bucket = {widest:.1f}s (the bug produced a 701s bucket)")
check("every bucket is at most one bucket-width plus a poll",
      widest <= TELEMETRY_BUCKET_SECONDS + POLL_SECONDS + 0.001,
      f"widest={widest:.1f}s, limit={TELEMETRY_BUCKET_SECONDS + POLL_SECONDS}s")

pre_gap = [b for b in buckets if b["end_timestamp"] <= last_tick_before_sleep + 0.001]
post_gap = [b for b in buckets if b["start_timestamp"] >= resume_ts - 0.001]
check("a pre-gap bucket ends at or before the last observed sample", pre_gap,
      f"{len(pre_gap)} bucket(s) end by {last_tick_before_sleep:.1f}")
check("a post-gap bucket starts at or after the first post-resume sample", post_gap,
      f"{len(post_gap)} bucket(s) start at/after {resume_ts:.1f}")
check("no bucket exists inside the unmonitored interval",
      not [b for b in buckets
           if b["start_timestamp"] > last_tick_before_sleep + 0.001
           and b["start_timestamp"] < resume_ts - 0.001],
      "the missing interval is genuinely absent - nothing synthesized, nothing interpolated")

# ---------------------------------------------------------------------------
# Timeline and coverage must agree again.
# ---------------------------------------------------------------------------
print("\n--- timeline / coverage consistency (the broken invariant) ---")
window_start, window_end = pre_start - 1, CLOCK() + 1
gap_events = timeline_gap_events(buckets, window_start, window_end)
check("timeline_gap_events emits exactly one gap", len(gap_events) == 1,
      f"{len(gap_events)} gap event(s) - the bug emitted 0")
if gap_events:
    ev = gap_events[0]
    ev_seconds = ev["end_timestamp"] - ev["timestamp"]
    check("the timeline gap duration matches the real outage",
          abs(ev_seconds - SLEEP_SECONDS) < POLL_SECONDS + 0.001,
          f"timeline says {ev_seconds:.1f}s, the outage was {SLEEP_SECONDS:.1f}s")

valid, expected, coverage_pct = compute_coverage(
    [b for b in buckets if window_start <= b["start_timestamp"] <= window_end],
    window_end - window_start)
missing_pct = 100.0 * SLEEP_SECONDS / (window_end - window_start)
check("coverage and the gap tell the same story",
      abs((100.0 - coverage_pct) - missing_pct) < 12.0,
      f"coverage={coverage_pct:.1f}% (missing {100 - coverage_pct:.1f}%), "
      f"outage is {missing_pct:.1f}% of the window")

full_tl = build_timeline(window_start, window_end)
check("exactly one gap entry appears on the merged timeline",
      sum(1 for e in full_tl if e["kind"] == "gap") == 1,
      "no duplicate gap rows")

# ---------------------------------------------------------------------------
# Idempotency: one suspend, one gap - even if applied twice.
# ---------------------------------------------------------------------------
print("\n--- idempotency ---")
before = len(app.incidents_active["cpu"]["monitoring_gaps"])
seconds_before = app.incidents_active["cpu"]["monitoring_gap_seconds"]
app._apply_monitoring_discontinuity(gap)   # replay the SAME gap
after = len(app.incidents_active["cpu"]["monitoring_gaps"])
check("re-applying the same discontinuity adds no duplicate gap", before == after,
      f"{before} -> {after}")
check("re-applying the same discontinuity does not double-count gap seconds",
      app.incidents_active["cpu"]["monitoring_gap_seconds"] == seconds_before,
      f"{seconds_before} -> {app.incidents_active['cpu']['monitoring_gap_seconds']}")

# ---------------------------------------------------------------------------
# Closing normally later must keep the record honest.
# ---------------------------------------------------------------------------
print("\n--- the incident closes normally, long after the gap ---")
CLOCK.advance(POLL_SECONDS)
tick(app, 70.0, alerting=False)
completed = read_incidents_file()
closed = next((i for i in completed if i["incident_id"] == incident_id_before), None)
check("the incident is persisted", closed is not None)
check("the persisted incident is duration_exact=False",
      closed is not None and closed.get("duration_exact") is False,
      f"duration_exact={closed.get('duration_exact') if closed else None} "
      f"(the bug persisted True)")
check("the persisted incident carries its monitoring gap",
      closed is not None and len(closed.get("monitoring_gaps", [])) == 1
      and abs(closed.get("monitoring_gap_seconds", 0) - SLEEP_SECONDS) < 0.001,
      f"monitoring_gap_seconds={closed.get('monitoring_gap_seconds') if closed else None}")
check("duration_seconds is honestly larger than the monitored time",
      closed is not None
      and closed["duration_seconds"] > closed.get("monitoring_gap_seconds", 0),
      f"duration={closed['duration_seconds']:.1f}s of which "
      f"{closed.get('monitoring_gap_seconds', 0):.1f}s was never observed")
check("a normal close after the flag was cleared invents no gap recovery",
      closed is not None and not closed.get("recovery_during_monitoring_gap")
      and closed.get("recovery_value") is not None,
      "it was observed alerting after the gap, then observed recovering - both real")

# ---------------------------------------------------------------------------
# Recovery DURING the gap: never fabricate the moment or the value.
# ---------------------------------------------------------------------------
print("\n--- recovery that happened while suspended ---")
app2 = HeadlessApp()
for _ in range(30):
    CLOCK.advance(POLL_SECONDS)
    tick(app2, 92.0, alerting=True, zone="ORANGE")
id2 = app2.incidents_active["cpu"]["incident_id"]
last_hot = CLOCK()
CLOCK.advance(SLEEP_SECONDS)
tick(app2, 65.0, alerting=False)          # first post-resume sample: already cool

rec2 = next((i for i in read_incidents_file() if i["incident_id"] == id2), None)
check("an incident that recovered during the gap is closed", rec2 is not None)
check("it is flagged recovery_during_monitoring_gap",
      rec2 is not None and rec2.get("recovery_during_monitoring_gap") is True)
check("no recovery value is fabricated for the unobserved moment",
      rec2 is not None and rec2.get("recovery_value") is None,
      f"recovery_value={rec2.get('recovery_value') if rec2 else None}")
check("the real first-post-gap reading is kept separately",
      rec2 is not None and rec2.get("first_observed_recovered_value") == 65.0,
      "a real reading at a real time, distinct from a claimed recovery point")
check("monitored_duration_seconds records only what was actually watched",
      rec2 is not None
      and abs(rec2["monitored_duration_seconds"] - (last_hot - rec2["start_timestamp"])) < 0.001,
      f"monitored={rec2.get('monitored_duration_seconds'):.1f}s vs "
      f"duration={rec2.get('duration_seconds'):.1f}s")
check("it is duration_exact=False", rec2 is not None and rec2.get("duration_exact") is False)

# ---------------------------------------------------------------------------
# Sessions: same honesty, and no zone/foreground time across the gap.
# ---------------------------------------------------------------------------
print("\n--- workload sessions ---")
app3 = HeadlessApp()
sess = appmod._new_session_record("game.exe", "Game.exe", 1234, CLOCK())
sess["confirmed"] = True
sess["session_id"] = "sess-1"
sess["zone_time"]["cpu"]["ORANGE"] = 120.0
sess["foreground_seconds"] = 120.0
app3.workload_sessions["game.exe"] = sess

zone_before = sess["zone_time"]["cpu"]["ORANGE"]
fg_before = sess["foreground_seconds"]
CLOCK.advance(SLEEP_SECONDS)
g3 = app3._detect_monitoring_discontinuity()
app3._apply_monitoring_discontinuity(g3)

check("an active session records the gap", len(sess["monitoring_gaps"]) == 1,
      f"gaps={len(sess['monitoring_gaps'])}")
check("no zone time is added across the gap",
      sess["zone_time"]["cpu"]["ORANGE"] == zone_before,
      f"{zone_before} -> {sess['zone_time']['cpu']['ORANGE']}")
check("no foreground time is added across the gap",
      sess["foreground_seconds"] == fg_before,
      f"{fg_before} -> {sess['foreground_seconds']}")
check("session gap accounting is guarded independently by SESSION_GAP_THRESHOLD_S",
      SESSION_GAP_THRESHOLD_S < MONITORING_DISCONTINUITY_S,
      f"per-tick dt is clamped at {SESSION_GAP_THRESHOLD_S}s, so a suspend contributes 0.0")

app3._sessions_record_live_gap(g3)  # replay
check("replaying the gap adds no duplicate session gap", len(sess["monitoring_gaps"]) == 1,
      f"gaps={len(sess['monitoring_gaps'])}")

CLOCK.advance(POLL_SECONDS)
finalized = app3._finalize_session_record(sess, CLOCK(), uncertain=False)
check("a session that lived through a gap is NOT duration_exact, even ending normally",
      finalized["duration_exact"] is False,
      "start and end are both real, but the span contains unobserved time")
check("the finalized session carries the gap",
      len(finalized["monitoring_gaps"]) == 1)

clean_sess = appmod._new_session_record("idle.exe", "Idle.exe", 99, CLOCK())
clean_sess["confirmed"] = True
CLOCK.advance(60)
clean = app3._finalize_session_record(clean_sess, CLOCK(), uncertain=False)
check("a session with no gap is still duration_exact=True", clean["duration_exact"] is True,
      "the fix must not make every session uncertain")

# ---------------------------------------------------------------------------
print("\n" + "=" * 78)
appmod.time.time = _REAL_TIME
if FAILURES:
    print(f"{len(FAILURES)} OF {CHECKS[0]} CHECKS FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print(f"ALL {CHECKS[0]} SLEEP/RESUME GAP CHECKS PASSED, NO TRACEBACK")
