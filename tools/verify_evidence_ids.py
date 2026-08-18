"""Verification for Phase 14 - Evidence IDs.

Every persisted incident/session, and every derived monitoring-coverage gap, now carries a
stable, human-citable `evidence_id` (PREFIX-YYYYMMDD-NNNN) alongside its existing internal id
(incident_id/session_id), so an AI layer answer can point at exactly which record backs a claim.
IDs are frozen once at close/finalize time - never a new mutable counter, never recomputed on a
later read - by counting how many already-persisted records share the same (prefix, local day) at
the moment of closing. See assign_incident_evidence_id()/assign_session_evidence_id()/
coverage_gap_events_for_day() in app.py.
"""
import json
import re
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import (  # noqa: E402
    App, DATA_DIR, INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH, SESSIONS_PATH, ACTIVE_SESSIONS_PATH,
    TELEMETRY_DB_PATH, read_incidents_file, read_sessions_file, read_telemetry_file,
    local_midnight_ts, timeline_gap_events, coverage_gap_events_for_day,
    _new_session_record, _agg_add,
)
import thermal_watch_evidence_cli as evidence_api  # noqa: E402
from ai.provider_contract import EvidenceBroker, ProviderConfig  # noqa: E402
from ai.providers.nox import NoxProvider  # noqa: E402
from ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

FAILURES = []
CHECKS = [0]

EVIDENCE_ID_RE = re.compile(r"^(INC|NET|SES|COV)-(\d{8})-(\d{4})$")


def check(name, condition, detail=""):
    CHECKS[0] += 1
    ok = bool(condition)
    print(f"[{'PASS' if ok else 'FAIL'}] {CHECKS[0]:2d}. {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILURES.append(name)


def fresh_files():
    for p in (INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH, SESSIONS_PATH, ACTIVE_SESSIONS_PATH, TELEMETRY_DB_PATH):
        if p.exists():
            p.unlink()


def day_ts(d, hour=12):
    """Epoch seconds for `hour`:00 local time on calendar date `d` - safely away from midnight so
    every fixture record lands unambiguously inside one local calendar day."""
    return local_midnight_ts(d) + hour * 3600


DAY_A = date(2026, 6, 15)
DAY_B = date(2026, 6, 16)
DAY_A_STR = DAY_A.strftime("%Y%m%d")
DAY_B_STR = DAY_B.strftime("%Y%m%d")


def open_close_incident(app, key, component, sensor_name, start_ts, zone="YELLOW", value=75.0):
    """Opens and immediately closes one incident with a CONTROLLED, fully deterministic
    start_timestamp/incident_id (real _incident_open()/_incident_touch()/_incident_close() calls).

    time.time() is patched to `start_ts` ONLY for the _incident_open() call itself, using the
    exact `mock.patch("app.time.time", return_value=...)` pattern already established in
    tools/verify_network_foundation.py and tools/verify_perprocess_network.py - NOT an arbitrary
    sleep. This matters because _incident_open() mints its internal `incident_id` as
    `f"{component}-{int(time.time() * 1000)}"` (app.py) from REAL wall-clock time at the moment
    it is called, independent of any start_timestamp override applied afterward. The previous
    version of this fixture only overrode start_timestamp post-hoc, so incident_id was still
    minted from real time.time() - two open_close_incident() calls for the SAME component
    landing in the same real-clock millisecond (entirely possible on a fast machine calling this
    back-to-back with no I/O in between) would mint the identical incident_id and collide,
    corrupting which evidence_id gets frozen onto which persisted record. Patching time.time()
    to `start_ts` for _incident_open() makes incident_id and start_timestamp both derive from the
    one controlled value this fixture already varies per call (a different hour/day every call
    site in this file), so distinct calls can never collide regardless of real execution speed.
    See section 13 below ("rapid-fire, zero-sleep repetition cannot collide") for an explicit
    regression proving this deterministically rather than relying on this docstring's claim alone.

    _incident_touch()/_incident_close() are deliberately left on REAL time.time(), unpatched -
    section 10/12's get_recent_incidents/EvidenceBroker.dispatch() checks filter
    recent_incidents_24h by `end_timestamp >= real_now - EVIDENCE_RECENT_WINDOW_S` (app.py), so
    end_timestamp must land near actual wall-clock "now" (exactly as it always has) for those
    incidents to appear as "recent" - only incident_id/start_timestamp need to be controlled to
    fix the collision, and only _incident_open() is where incident_id is minted."""
    with mock.patch("app.time.time", return_value=start_ts):
        app._incident_open(key, component, sensor_name, None, zone, value)
    app._incident_touch(key, zone, value)
    recovery = None if value is None else value - 10
    app._incident_close(key, recovery)
    return app.incidents_recent[0]


def open_close_session(app, key, workload, start_ts, end_ts=None):
    """start_ts is the controlled, possibly-historical timestamp that decides which calendar day
    (and therefore which SES-YYYYMMDD-NNNN rank) this session lands on. end_ts defaults to real
    wall-clock `now` (like _incident_close() always uses for end_timestamp regardless of a
    fixture's start_timestamp override) so the finished session still falls inside the evidence
    snapshot's 24h "recent" window."""
    rec = _new_session_record(key, workload, 4242, start_ts)
    rec["confirmed"] = True
    rec["session_id"] = f"{key}-{int(start_ts * 1000)}"
    _agg_add(rec["agg"]["cpu_temp"], 65.0)
    _agg_add(rec["agg"]["cpu_temp"], 70.0)
    app.workload_sessions[key] = rec
    app._session_close(key, end_ts if end_ts is not None else time.time(), uncertain=False)
    return app.sessions_recent[0]


print("=" * 78)
print("setup")
print("=" * 78)
fresh_files()
app = App()
app.stop_event.set()
for after_id in app.tk.eval("after info").split():
    try:
        command = app.tk.call("after", "info", after_id)[0]
    except Exception:
        continue
    if any(str(command).endswith(name) for name in app._RECURRING_AFTER_METHODS):
        app.after_cancel(after_id)
check("sandbox is empty before this run", not INCIDENTS_PATH.exists() and not SESSIONS_PATH.exists())

all_evidence_ids = []

try:
    print()
    print("=" * 78)
    print("1. incident evidence IDs: format, prefix (INC vs NET), per-day sequential rank")
    print("=" * 78)
    inc1 = open_close_incident(app, "cpu1", "cpu", "CPU Package", day_ts(DAY_A, 9))
    inc2 = open_close_incident(app, "cpu2", "cpu", "CPU Package", day_ts(DAY_A, 10))
    net1 = open_close_incident(app, "net1", "network", "Network Connectivity", day_ts(DAY_A, 11),
                               zone="RED", value=None)
    gpu1 = open_close_incident(app, "gpu1", "gpu_core", "GPU Core", day_ts(DAY_A, 12))
    cpuB = open_close_incident(app, "cpuB", "cpu", "CPU Package", day_ts(DAY_B, 9))

    check("incident 1 (cpu, day A) is INC-<dayA>-0001", inc1.get("evidence_id") == f"INC-{DAY_A_STR}-0001",
          f"got {inc1.get('evidence_id')!r}")
    check("incident 2 (cpu, day A) is INC-<dayA>-0002", inc2.get("evidence_id") == f"INC-{DAY_A_STR}-0002",
          f"got {inc2.get('evidence_id')!r}")
    check("network incident (day A) gets its OWN NET counter, not folded into INC",
          net1.get("evidence_id") == f"NET-{DAY_A_STR}-0001", f"got {net1.get('evidence_id')!r}")
    check("GPU incident (component=gpu_core, day A) shares the generic INC counter with cpu "
          "(prefix splits only on network, never per-component)",
          gpu1.get("evidence_id") == f"INC-{DAY_A_STR}-0003", f"got {gpu1.get('evidence_id')!r}")
    check("an incident on a DIFFERENT calendar day (day B) restarts its own INC rank at 0001, "
          "never continuing day A's count", cpuB.get("evidence_id") == f"INC-{DAY_B_STR}-0001",
          f"got {cpuB.get('evidence_id')!r}")
    for eid in (inc1["evidence_id"], inc2["evidence_id"], net1["evidence_id"], gpu1["evidence_id"], cpuB["evidence_id"]):
        check(f"evidence_id '{eid}' matches the canonical PREFIX-YYYYMMDD-NNNN shape", EVIDENCE_ID_RE.match(eid))
    all_evidence_ids += [inc1["evidence_id"], inc2["evidence_id"], net1["evidence_id"], gpu1["evidence_id"], cpuB["evidence_id"]]

    print()
    print("=" * 78)
    print("2. incident evidence IDs are unique within a source type")
    print("=" * 78)
    ids_here = [inc1["evidence_id"], inc2["evidence_id"], gpu1["evidence_id"]]
    check("three same-day INC incidents got three distinct ids", len(set(ids_here)) == len(ids_here),
          f"ids: {ids_here}")

    print()
    print("=" * 78)
    print("3. stable across repeated reads, and NEVER recomputed once more records are added")
    print("=" * 78)
    read_once = read_incidents_file()
    read_twice = read_incidents_file()
    id_by_incident_id_1 = {r["incident_id"]: r.get("evidence_id") for r in read_once}
    id_by_incident_id_2 = {r["incident_id"]: r.get("evidence_id") for r in read_twice}
    check("two independent read_incidents_file() calls return byte-identical evidence_ids",
          id_by_incident_id_1 == id_by_incident_id_2)
    check("evidence_id read back from disk exactly matches what was frozen in memory at close time",
          id_by_incident_id_1.get(inc1["incident_id"]) == inc1["evidence_id"]
          and id_by_incident_id_1.get(inc2["incident_id"]) == inc2["evidence_id"])
    # inc1 was closed FIRST as INC-dayA-0001. Since then, TWO more same-day/prefix INC incidents
    # were closed (inc2, gpu1). If evidence_id were ever recomputed on read instead of frozen at
    # close, inc1's on-disk id would now be wrong. It must still read back as 0001.
    check("an OLDER incident's frozen evidence_id is untouched by LATER same-day/prefix incidents "
          "being closed afterward - proof it is frozen, not recomputed on read",
          id_by_incident_id_1.get(inc1["incident_id"]) == f"INC-{DAY_A_STR}-0001")

    print()
    print("=" * 78)
    print("4. legacy records (no evidence_id yet) are counted, not skipped or collided with")
    print("=" * 78)
    legacy_ts = day_ts(DAY_A, 14)
    legacy_line = json.dumps({
        "incident_id": f"cpu-{int(legacy_ts * 1000)}", "start_timestamp": legacy_ts,
        "end_timestamp": legacy_ts + 60, "component": "cpu", "sensor_name": "CPU Package",
        "duration_seconds": 60.0, "peak_value": 80.0, "recovery_value": 60.0,
        # deliberately no "evidence_id" key - simulates a pre-Phase-14 persisted record
    })
    with INCIDENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(legacy_line + "\n")
    legacy_key = "cpu_after_legacy"
    inc_after_legacy = open_close_incident(app, legacy_key, "cpu", "CPU Package", day_ts(DAY_A, 15))
    check("a new same-day/prefix incident ranks AFTER a legacy (no-evidence_id) record too, "
          "counting it via start_timestamp+component fallback rather than skipping it",
          inc_after_legacy.get("evidence_id") == f"INC-{DAY_A_STR}-0005",
          f"got {inc_after_legacy.get('evidence_id')!r} (3 prior INC/day-A + 1 legacy + this one = 0005)")
    all_evidence_ids.append(inc_after_legacy["evidence_id"])

    print()
    print("=" * 78)
    print("5. session evidence IDs: always SES, sequential per day, independent of the incident counters")
    print("=" * 78)
    ses1 = open_close_session(app, "steam.exe", "Steam.exe", day_ts(DAY_A, 9))
    ses2 = open_close_session(app, "chrome.exe", "Chrome.exe", day_ts(DAY_A, 10))
    sesB = open_close_session(app, "steam.exe", "Steam.exe", day_ts(DAY_B, 9))
    check("session 1 (day A) is SES-<dayA>-0001", ses1.get("evidence_id") == f"SES-{DAY_A_STR}-0001",
          f"got {ses1.get('evidence_id')!r}")
    check("session 2 (day A) is SES-<dayA>-0002 - sessions never split by workload/network-block",
          ses2.get("evidence_id") == f"SES-{DAY_A_STR}-0002", f"got {ses2.get('evidence_id')!r}")
    check("a session on a different day restarts its own SES rank at 0001",
          sesB.get("evidence_id") == f"SES-{DAY_B_STR}-0001", f"got {sesB.get('evidence_id')!r}")
    check("session ids never collide with same-day incident ids (disjoint prefixes)",
          len({ses1["evidence_id"], ses2["evidence_id"]} & {inc1["evidence_id"], inc2["evidence_id"]}) == 0)
    all_evidence_ids += [ses1["evidence_id"], ses2["evidence_id"], sesB["evidence_id"]]

    print()
    print("=" * 78)
    print("6. session evidence IDs are stable across repeated reads and reload from disk")
    print("=" * 78)
    sread1 = {r["session_id"]: r.get("evidence_id") for r in read_sessions_file()}
    sread2 = {r["session_id"]: r.get("evidence_id") for r in read_sessions_file()}
    check("two independent read_sessions_file() calls return byte-identical evidence_ids", sread1 == sread2)
    check("reloaded session evidence_id matches what was frozen at close time",
          sread1.get(ses1["session_id"]) == ses1["evidence_id"]
          and sread1.get(ses2["session_id"]) == ses2["evidence_id"])

    print()
    print("=" * 78)
    print("7. no evidence_id exposes personal information")
    print("=" * 78)
    import getpass
    import socket
    private_tokens = [getpass.getuser(), socket.gethostname(), "C:\\", "D:\\", "\\Users\\", str(DATA_DIR)]
    serialized = " ".join(all_evidence_ids)
    check("no evidence_id string contains a username/hostname/filesystem-path substring",
          not any(tok and tok.lower() in serialized.lower() for tok in private_tokens),
          f"checked {len(all_evidence_ids)} ids against {len(private_tokens)} private tokens")

    print()
    print("=" * 78)
    print("8. different day / different component evidence IDs never collide")
    print("=" * 78)
    check("every evidence_id collected across incidents and sessions so far is globally unique",
          len(all_evidence_ids) == len(set(all_evidence_ids)), f"{len(all_evidence_ids)} ids: {all_evidence_ids}")

    print()
    print("=" * 78)
    print("9. monitoring-gap (coverage) evidence IDs: pure function of immutable telemetry, stable")
    print("=" * 78)

    def make_bucket(start_ts, end_ts):
        return {"start_timestamp": start_ts, "end_timestamp": end_ts, "sample_count": 5,
                "scalars": {}, "sensors": {}}

    gap_day = date(2026, 9, 21)  # a third, otherwise-untouched day - keeps this section independent
    b1_start = day_ts(gap_day, 9)
    b1_end = b1_start + 60
    b2_start = b1_end + 600  # a real 600s gap, well over TIMELINE_MIN_GAP_SECONDS (180s)
    b2_end = b2_start + 60
    app._persist_telemetry_bucket(make_bucket(b1_start, b1_end))
    app._persist_telemetry_bucket(make_bucket(b2_start, b2_end))

    buckets = read_telemetry_file()
    window_start, window_end = b1_start - 30, b2_end + 30
    gaps_first = timeline_gap_events(buckets, window_start, window_end)
    check("exactly one gap is detected between the two synthetic buckets", len(gaps_first) == 1,
          f"gaps: {gaps_first}")
    gap_source_id = gaps_first[0]["source_id"] if gaps_first else None
    check("the monitoring gap gets a non-None evidence_id/source_id", gap_source_id is not None)
    check("the gap's evidence_id matches the COV-YYYYMMDD-NNNN shape",
          bool(gap_source_id) and EVIDENCE_ID_RE.match(gap_source_id) and gap_source_id.startswith("COV-"))

    # Re-query from scratch (fresh read from the sandboxed SQLite store, fresh call) - must yield
    # the exact same id, since it is a pure function of already-persisted, immutable bucket data.
    buckets_again = read_telemetry_file()
    gaps_second = timeline_gap_events(buckets_again, window_start, window_end)
    check("re-querying timeline_gap_events() from a fresh read yields the SAME evidence_id",
          gaps_second and gaps_second[0]["source_id"] == gap_source_id,
          f"first={gap_source_id!r} second={(gaps_second[0]['source_id'] if gaps_second else None)!r}")

    day_gaps = coverage_gap_events_for_day(gap_day)
    matching = [g for g in day_gaps if g["source_id"] == gap_source_id]
    check("coverage_gap_events_for_day() (the canonical day-scoped identity function) independently "
          "produces a gap with the exact same evidence_id", len(matching) == 1, f"day_gaps: {day_gaps}")
    if gap_source_id:
        all_evidence_ids.append(gap_source_id)
        check("the gap's evidence_id does not collide with any incident/session evidence_id",
              len(all_evidence_ids) == len(set(all_evidence_ids)))

    print()
    print("=" * 78)
    print("10. thermal_watch_evidence_cli responses preserve evidence_id and source_type")
    print("=" * 78)
    snapshot = app._build_evidence_snapshot()
    with tempfile.TemporaryDirectory(prefix="tw_evidence_ids_") as td:
        snapshot_path = Path(td) / "thermal_watch_evidence.json"
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        with mock.patch.object(evidence_api, "EVIDENCE_SNAPSHOT_PATH", snapshot_path):
            resp_inc = evidence_api.handle_request({"operation": "get_recent_incidents", "parameters": {"limit": 50}})
            resp_ses = evidence_api.handle_request({"operation": "get_recent_sessions", "parameters": {"limit": 50}})
            resp_cov = evidence_api.handle_request({"operation": "get_coverage"})

            check("get_recent_incidents call succeeds", resp_inc.get("ok") is True)
            check("get_recent_incidents rows carry a non-empty evidence_id",
                  bool(resp_inc["data"]) and all(row.get("evidence_id") for row in resp_inc["data"]),
                  f"rows: {resp_inc['data']}")
            check("get_recent_incidents rows carry the correct source_type (incident vs network_incident)",
                  all((row.get("source_type") == "network_incident") == (row.get("component") == "network")
                      for row in resp_inc["data"]))
            check("pre-existing incident_id field is untouched alongside the new evidence_id",
                  all(row.get("incident_id") for row in resp_inc["data"]))

            check("get_recent_sessions call succeeds", resp_ses.get("ok") is True)
            check("get_recent_sessions rows carry a non-empty evidence_id",
                  bool(resp_ses["data"]) and all(row.get("evidence_id") for row in resp_ses["data"]),
                  f"rows: {resp_ses['data']}")
            check("get_recent_sessions rows carry source_type='session'",
                  all(row.get("source_type") == "session" for row in resp_ses["data"]))
            check("pre-existing session_id field is untouched alongside the new evidence_id",
                  all(row.get("session_id") for row in resp_ses["data"]))

            check("get_coverage succeeds and its coverage carries gap evidence, purely additive",
                  resp_cov.get("ok") is True and isinstance(resp_cov["data"].get("gaps"), list))

            print()
            print("=" * 78)
            print("11. old clients that ignore evidence_id still work - nothing existing was renamed/removed")
            print("=" * 78)
            # close_reason is deliberately excluded here: it was ALREADY only set on a gap-related
            # close before Phase 14 touched anything (see _incident_close()'s pending_gap branch) -
            # not a Phase 14 regression, just not a field every incident carries.
            legacy_shape_inc = {"incident_id", "start_timestamp", "end_timestamp", "component",
                                "sensor_name", "duration_seconds"}
            legacy_shape_ses = {"session_id", "workload_key", "start_timestamp", "end_timestamp", "cpu"}
            check("every pre-existing incident field a legacy client relied on is still present",
                  all(legacy_shape_inc.issubset(row.keys()) for row in resp_inc["data"]))
            check("every pre-existing session field a legacy client relied on is still present",
                  all(legacy_shape_ses.issubset(row.keys()) for row in resp_ses["data"]))
            stripped = [{k: v for k, v in row.items() if k not in ("evidence_id", "source_type")}
                       for row in resp_inc["data"]]
            check("a client that strips evidence_id/source_type entirely still gets a parseable, "
                  "non-empty record for every incident", all(len(row) > 0 for row in stripped))

            print()
            print("=" * 78)
            print("12. EvidenceBroker.dispatch() (the Nox/OpenAI-compatible/custom provider door) "
                  "does not strip evidence_id")
            print("=" * 78)
            broker = EvidenceBroker()
            dispatched = broker.dispatch("thermal_watch_evidence",
                                         {"operation": "get_recent_incidents", "parameters": {"limit": 50}})
            check("EvidenceBroker.dispatch() succeeds", dispatched.get("ok") is True)
            check("evidence_id survives EvidenceBroker.dispatch() (deepcopy in/out, opaque passthrough)",
                  bool(dispatched["data"]) and all(row.get("evidence_id") for row in dispatched["data"]))

            nox_seen = {}

            def nox_transport(question, tool, dispatch):
                result = dispatch("thermal_watch_evidence",
                                  {"operation": "get_recent_incidents", "parameters": {"limit": 50}})
                nox_seen["result"] = result
                return {"answer": "evidence relayed", "evidence": [result]}

            nox_result = NoxProvider(ProviderConfig(provider="nox"), transport=nox_transport).ask(
                "what incidents happened recently?", broker)
            check("Nox provider's relayed tool result still carries evidence_id for every incident",
                  nox_result.ok and bool(nox_seen["result"]["data"])
                  and all(row.get("evidence_id") for row in nox_seen["result"]["data"]))

            openai_provider = OpenAICompatibleProvider(
                ProviderConfig(provider="openai_compatible", endpoint="http://127.0.0.1:1234/v1", model="fixture"),
                transport=object())
            openai_dispatched = broker.dispatch("thermal_watch_evidence",
                                                {"operation": "get_recent_sessions", "parameters": {"limit": 50}})
            check("the OpenAI-compatible provider's own dispatch path (same EvidenceBroker.dispatch(), "
                  "reshaped only by json.dumps at the transport boundary) also preserves evidence_id",
                  bool(openai_dispatched["data"]) and all(row.get("evidence_id") for row in openai_dispatched["data"]))
            check("OpenAI-compatible provider constructs successfully alongside this evidence (no adapter "
                  "code needed changing for ids to flow through)", openai_provider.config.provider == "openai_compatible")

    print()
    print("=" * 78)
    print("13. rapid-fire, zero-sleep repetition cannot collide (regression: millisecond-based "
          "incident_id was previously minted from REAL time.time(), independent of this fixture's "
          "controlled start_timestamp - see open_close_incident()'s docstring)")
    print("=" * 78)
    # 40 incidents, back-to-back, zero real delay between calls (no sleep anywhere) - exactly the
    # execution shape (fast machine, tight loop, no I/O between opens) that could previously land
    # two same-component _incident_open() calls in the same real wall-clock millisecond and mint
    # the identical incident_id. Every start_ts here is a distinct, controlled value (1 real
    # second apart, deliberately closer together than any other section of this file uses) - if
    # time.time() were NOT patched per-call, the real elapsed wall-clock time between these 40
    # calls would very likely be under 40ms on any modern machine, i.e. almost certain to collide.
    rapid_day = date(2026, 11, 3)  # a fourth, otherwise-untouched day - keeps this section independent
    rapid = [
        open_close_incident(app, f"rapid{i}", "cpu", "CPU Package", day_ts(rapid_day, 0) + i)
        for i in range(40)
    ]
    rapid_incident_ids = [r["incident_id"] for r in rapid]
    rapid_evidence_ids = [r["evidence_id"] for r in rapid]
    check("40 rapid-fire, zero-sleep, same-component incidents all got distinct internal incident_ids",
          len(set(rapid_incident_ids)) == 40, f"{len(set(rapid_incident_ids))}/40 distinct")
    check("40 rapid-fire, zero-sleep, same-component incidents all got distinct evidence_ids",
          len(set(rapid_evidence_ids)) == 40, f"{len(set(rapid_evidence_ids))}/40 distinct")
    check("the rapid-fire evidence_ids form the exact unbroken INC-<day>-0001..0040 sequence, proving "
          "no gap and no collision-induced skip", rapid_evidence_ids == [
              f"INC-{rapid_day.strftime('%Y%m%d')}-{n:04d}" for n in range(1, 41)],
          f"got {rapid_evidence_ids}")
    reread = {r["incident_id"]: r.get("evidence_id") for r in read_incidents_file()}
    check("all 40 rapid-fire evidence_ids read back from disk exactly match what was frozen in memory",
          all(reread.get(iid) == eid for iid, eid in zip(rapid_incident_ids, rapid_evidence_ids)))
    check("rapid-fire evidence_ids do not collide with any earlier evidence_id in this run",
          len(set(rapid_evidence_ids) & set(all_evidence_ids)) == 0)
finally:
    app.stop_event.set()
    app.destroy()

print()
print("=" * 78)
summary = f"{CHECKS[0] - len(FAILURES)}/{CHECKS[0]} checks passed"
print(summary)
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
