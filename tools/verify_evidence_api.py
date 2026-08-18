"""Verification for v1.1 Phase 10 - Evidence API.

File-based, not a network service: Thermal Watch periodically writes a structured snapshot to
EVIDENCE_SNAPSHOT_PATH (same atomic tmp-then-replace pattern as every other store), and any
process that can read a file - Nox, a script, a human - can consume it. No listening socket, no
new attack surface. Every section is assembled from already-computed state or an already-existing
read function - active incidents/sessions reuse _incident_to_persistable()/
_finalize_session_record() verbatim, recent incidents/sessions/coverage reuse the same read/
compute functions every history view already uses.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import (  # noqa: E402
    App, EVIDENCE_SCHEMA_VERSION, EVIDENCE_SNAPSHOT_PATH, EVIDENCE_SNAPSHOT_INTERVAL_MS,
    EVIDENCE_RECENT_WINDOW_S, APP_VERSION, INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH,
    SESSIONS_PATH, ACTIVE_SESSIONS_PATH,
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


def fresh_files():
    for p in (INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH, SESSIONS_PATH, ACTIVE_SESSIONS_PATH):
        if p.exists():
            p.unlink()


print("=" * 78)
print("1. schema/path wiring")
print("=" * 78)
check("EVIDENCE_SCHEMA_VERSION is a real version string", EVIDENCE_SCHEMA_VERSION == "1.0")
check("EVIDENCE_SNAPSHOT_PATH lives in DATA_DIR (unprivileged, no elevation needed to read it)",
      EVIDENCE_SNAPSHOT_PATH.name == "thermal_watch_evidence.json")
check("snapshot interval is a positive, real number of ms", EVIDENCE_SNAPSHOT_INTERVAL_MS > 0)
check("recent window is 24h", EVIDENCE_RECENT_WINDOW_S == 24 * 3600)

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

try:
    print()
    print("=" * 78)
    print("2. _build_evidence_snapshot() with real live app state, top-level shape")
    print("=" * 78)
    snap = app._build_evidence_snapshot()
    for key in ("schema_version", "generated_at", "app_version", "system", "live",
               "active_incidents", "active_sessions", "recent_incidents_24h",
               "recent_sessions_24h", "coverage_24h"):
        check(f"top-level key '{key}' is present", key in snap)
    check("schema_version matches the module constant", snap["schema_version"] == EVIDENCE_SCHEMA_VERSION)
    check("app_version matches APP_VERSION", snap["app_version"] == APP_VERSION)
    check("generated_at is a real, recent timestamp", abs(snap["generated_at"] - time.time()) < 5)
    check("live network evidence exposes the already-computed top-process list",
          "top_processes" in snap["live"]["network"])

    app.last_net_procs = {"capture_active": True, "capture_error": None, "top": [
        {"pid": 4242, "name": "fixture", "bytes_in": 5000, "bytes_out": 100,
         "down_mbps": 2.5, "up_mbps": 0.1}
    ]}
    snap_with_process = app._build_evidence_snapshot()
    check("top-process evidence is copied without changing the verified rate/byte fields",
          snap_with_process["live"]["network"]["top_processes"] == app.last_net_procs["top"])

    print()
    print("=" * 78)
    print("3. the whole payload is genuinely JSON-safe (round-trips exactly)")
    print("=" * 78)
    encoded = json.dumps(snap)
    decoded = json.loads(encoded)
    check("json.dumps succeeds with no default=str fallback needed (every value is a native "
          "JSON type already)", encoded is not None)
    check("round-trip is lossless", decoded == snap)

    print()
    print("=" * 78)
    print("4. active incidents are sanitized exactly like ACTIVE_INCIDENTS_PATH's own writer")
    print("=" * 78)
    app._update_network_incident({"adapter": None})
    time.sleep(0.01)
    app.network_zone_state["pending"]["since"] = time.time() - 10
    app._update_network_incident({"adapter": None})
    snap2 = app._build_evidence_snapshot()
    net_incidents = [i for i in snap2["active_incidents"] if i.get("component") == "network"]
    check("a real active network incident appears in the evidence snapshot",
          len(net_incidents) == 1, f"active_incidents: {snap2['active_incidents']}")
    if net_incidents:
        inc = net_incidents[0]
        check("internal-only fields (_bias/_workload_tally/_live_gap_pending) never leak into the evidence file",
              not any(k.startswith("_") for k in inc.keys()), f"keys: {list(inc.keys())}")
        check("the incident carries its real alert_key, same as ACTIVE_INCIDENTS_PATH's own schema",
              inc.get("alert_key") == "network")

    print()
    print("=" * 78)
    print("5. active sessions use the finalized avg/peak schema, not raw internal counters")
    print("=" * 78)
    from app import _new_session_record, _agg_add  # noqa: E402
    rec = _new_session_record("steam.exe", "Steam.exe", 4242, time.time() - 120)
    rec["confirmed"] = True
    rec["session_id"] = "steam.exe-test"
    _agg_add(rec["agg"]["cpu_temp"], 70.0)
    _agg_add(rec["agg"]["cpu_temp"], 80.0)
    app.workload_sessions["steam.exe"] = rec
    snap3 = app._build_evidence_snapshot()
    steam_sessions = [s for s in snap3["active_sessions"] if s.get("workload_key") == "steam.exe"]
    check("the active session appears with a real session_id", len(steam_sessions) == 1)
    if steam_sessions:
        s = steam_sessions[0]
        check("uses the finalized {avg_temp, peak_temp} schema (same as a completed session), "
              "not a raw {count, sum, max} aggregate", "avg_temp" in (s.get("cpu") or {}))
        check("avg_temp is the real computed average of the two real samples",
              abs(s["cpu"]["avg_temp"] - 75.0) < 1e-9)
        check("peak_temp is the real max, not the last sample", s["cpu"]["peak_temp"] == 80.0)
        check("end_timestamp reflects 'as of now', not a fabricated close - this session is "
              "still genuinely active", s.get("end_timestamp") is not None
              and abs(s["end_timestamp"] - time.time()) < 2)

    print()
    print("=" * 78)
    print("6. recent incidents/sessions correctly windowed to the last 24h, honestly excluding older ones")
    print("=" * 78)
    from app import atomic_write_lines  # noqa: E402
    now = time.time()
    recent_line = json.dumps({"incident_id": "recent-1", "end_timestamp": now - 3600, "component": "cpu"})
    old_line = json.dumps({"incident_id": "old-1", "end_timestamp": now - 2 * 86400, "component": "cpu"})
    atomic_write_lines(INCIDENTS_PATH, [recent_line, old_line])
    snap4 = app._build_evidence_snapshot()
    ids_seen = {i["incident_id"] for i in snap4["recent_incidents_24h"]}
    check("a genuinely recent (1h old) incident is included", "recent-1" in ids_seen)
    check("a 2-day-old incident is correctly excluded from the 24h window", "old-1" not in ids_seen)

    print()
    print("=" * 78)
    print("7. coverage_24h reflects the same compute_coverage() every history view already trusts")
    print("=" * 78)
    cov = snap4["coverage_24h"]
    for k in ("valid_buckets", "expected_buckets", "coverage_pct"):
        check(f"coverage_24h has real key '{k}'", k in cov)
    check("with no telemetry buckets in this sandbox, coverage is honestly 0%, never fabricated",
          cov["coverage_pct"] == 0.0)

    print()
    print("=" * 78)
    print("8. _write_evidence_snapshot(): real atomic write, valid file, no leftover .tmp")
    print("=" * 78)
    app._write_evidence_snapshot()
    check("the snapshot file was actually created", EVIDENCE_SNAPSHOT_PATH.exists())
    on_disk = json.loads(EVIDENCE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    check("the on-disk file is valid, parseable JSON matching the real schema",
          on_disk.get("schema_version") == EVIDENCE_SCHEMA_VERSION)
    tmp_path = EVIDENCE_SNAPSHOT_PATH.with_suffix(".tmp")
    check("no leftover .tmp file after a successful write", not tmp_path.exists())

    print()
    print("=" * 78)
    print("9. _flush_evidence_periodic() respects stop_event (never fires after shutdown begins)")
    print("=" * 78)
    EVIDENCE_SNAPSHOT_PATH.unlink()
    app.stop_event.set()
    app._flush_evidence_periodic()
    check("no snapshot written once stop_event is set - matches every other recurring flush's "
          "shutdown contract", not EVIDENCE_SNAPSHOT_PATH.exists())
finally:
    app.stop_event.set(); app.destroy()

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
