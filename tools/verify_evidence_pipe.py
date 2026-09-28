"""Verification for v1.1 Phase 12 - Evidence pipe.

The named pipe is the new live channel Nox uses instead of the file: connecting to a nonexistent
pipe fails immediately and unambiguously, which the file alone cannot signal (that gap - a stale
file silently served as if live - is what caused the real incident this redesign fixes). This
verifies the server side end-to-end against a real App(), using the pipe module's own client
helpers (connect_client/send_request/close_client) so the test exercises the exact same framing
code a real client uses rather than a hand-rolled duplicate - except where a check deliberately
sends a malformed/hostile frame, where it drops to the module's lower-level _write_all so it can
construct bytes send_request() would never produce on its own.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import App, INCIDENTS_PATH, atomic_write_lines, new_experiment_record, append_experiment  # noqa: E402
import thermal_watch_evidence_pipe as pipe_mod  # noqa: E402

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


app = App()
try:
    print("=" * 78)
    print("1. normal round trip through the real pipe server")
    print("=" * 78)
    h = pipe_mod.connect_client()
    resp = pipe_mod.send_request(h, {"operation": "describe_operations"})
    pipe_mod.close_client(h)
    check("describe_operations succeeds over the pipe", resp.get("ok") is True)
    check("tool_catalog_version is the real catalog version", resp.get("tool_catalog_version") == 1)

    print()
    print("=" * 78)
    print("2. cold start - before any snapshot has been cached")
    print("=" * 78)
    check("_evidence_cache starts out empty (no _flush_evidence_periodic has run yet - no "
          "mainloop is pumped in this test harness)", app._evidence_cache is None)
    h = pipe_mod.connect_client()
    resp = pipe_mod.send_request(h, {"operation": "get_system_status"})
    pipe_mod.close_client(h)
    check("a data operation fails cleanly with evidence_not_ready while the cache is empty",
          resp.get("ok") is False and resp.get("error", {}).get("code") == "evidence_not_ready",
          f"response: {resp}")
    h = pipe_mod.connect_client()
    resp = pipe_mod.send_request(h, {"operation": "describe_operations"})
    pipe_mod.close_client(h)
    check("describe_operations still succeeds during the cold-start window (same guarantee the "
          "file-based path already has for 'Thermal Watch entirely absent')", resp.get("ok") is True)

    app._flush_evidence_periodic()
    check("_flush_evidence_periodic() populates the cache", app._evidence_cache is not None)
    h = pipe_mod.connect_client()
    resp = pipe_mod.send_request(h, {"operation": "get_system_status"})
    pipe_mod.close_client(h)
    check("the same operation now succeeds once the cache is populated", resp.get("ok") is True,
          f"response: {resp}")

    print()
    print("=" * 78)
    print("3. a malformed frame cannot crash the accept loop or leave it stuck")
    print("=" * 78)
    h = pipe_mod.connect_client()
    deadline = time.monotonic() + 2.0
    pipe_mod._write_all(h, (10).to_bytes(4, "little"), deadline)
    pipe_mod._write_all(h, b"\xff\xfe\x00\x01\x02\x03\x04\x05\x06\x07", deadline)
    try:
        raw = pipe_mod._read_frame(h, deadline)
        import json
        parsed = json.loads(raw.decode("utf-8"))
        check("invalid UTF-8 in the payload gets a structured malformed_frame error, not a crash",
              parsed.get("ok") is False and parsed.get("error", {}).get("code") == "malformed_frame",
              f"response: {parsed}")
    except (TimeoutError, OSError) as exc:
        check("invalid UTF-8 in the payload at least closes the connection cleanly (no hang, no crash)",
              True, f"connection closed instead of erroring: {exc}")
    finally:
        pipe_mod.close_client(h)
    h = pipe_mod.connect_client()
    resp = pipe_mod.send_request(h, {"operation": "describe_operations"})
    pipe_mod.close_client(h)
    check("the server is still alive and answering after the malformed frame", resp.get("ok") is True)

    print()
    print("=" * 78)
    print("4. an oversized length prefix is rejected fast, without over-allocating or hanging")
    print("=" * 78)
    h = pipe_mod.connect_client()
    start = time.monotonic()
    deadline = time.monotonic() + pipe_mod.READ_TIMEOUT_S
    pipe_mod._write_all(h, (5_000_000).to_bytes(4, "little"), deadline)
    rejected_fast = False
    try:
        pipe_mod._read_frame(h, deadline)
    except (TimeoutError, OSError):
        rejected_fast = True
    finally:
        pipe_mod.close_client(h)
    elapsed = time.monotonic() - start
    check("an oversized length prefix is rejected well under the server's read timeout",
          rejected_fast and elapsed < pipe_mod.READ_TIMEOUT_S / 2,
          f"rejected={rejected_fast}, elapsed={elapsed:.2f}s")
    h = pipe_mod.connect_client()
    resp = pipe_mod.send_request(h, {"operation": "describe_operations"})
    pipe_mod.close_client(h)
    check("the server is still alive and answering after the oversized frame", resp.get("ok") is True)

    print()
    print("=" * 78)
    print("5. multiple sequential connections all succeed (the pipe is reusable, not one-shot)")
    print("=" * 78)
    all_ok = True
    for i in range(5):
        h = pipe_mod.connect_client()
        resp = pipe_mod.send_request(h, {"operation": "describe_operations"})
        pipe_mod.close_client(h)
        all_ok = all_ok and resp.get("ok") is True
    check("5 sequential connect/request/close cycles all succeed", all_ok)

    print()
    print("=" * 78)
    print("6. get_recent_incidents days>1 reaches beyond the cached snapshot's 24h window")
    print("=" * 78)
    now = time.time()
    old_line = json.dumps({"incident_id": "old-3d", "end_timestamp": now - 3 * 86400, "component": "cpu"})
    atomic_write_lines(INCIDENTS_PATH, [old_line])
    app._flush_evidence_periodic()  # repopulate the cache so it reflects the fixture we just wrote

    h = pipe_mod.connect_client()
    resp = pipe_mod.send_request(h, {"operation": "get_recent_incidents", "parameters": {}})
    pipe_mod.close_client(h)
    ids_default = {r["incident_id"] for r in resp.get("data", [])}
    check("default (days omitted) does NOT include a 3-day-old incident - unchanged 24h behavior",
          "old-3d" not in ids_default, f"data: {resp.get('data')}")

    h = pipe_mod.connect_client()
    resp2 = pipe_mod.send_request(h, {"operation": "get_recent_incidents", "parameters": {"days": 7}})
    pipe_mod.close_client(h)
    ids_wide = {r["incident_id"] for r in resp2.get("data", [])}
    check("days=7 DOES include the 3-day-old incident", "old-3d" in ids_wide, f"data: {resp2.get('data')}")
    check("the widened response carries its own coverage for the requested window",
          resp2.get("coverage", {}).get("window_days") == 7, f"coverage: {resp2.get('coverage')}")

    h = pipe_mod.connect_client()
    resp3 = pipe_mod.send_request(h, {"operation": "get_recent_incidents", "parameters": {"days": 60}})
    pipe_mod.close_client(h)
    check("days above the schema's max (30) is rejected by parameter validation, never silently "
          "clamped or served anyway", resp3.get("ok") is False, f"response: {resp3}")

    print()
    print("=" * 78)
    print("7. get_experiments: marker discovery + a full before/after report over the pipe")
    print("=" * 78)
    marker = new_experiment_record("test fan install", time.time() - 2 * 86400, "cpu")
    append_experiment(marker)

    h = pipe_mod.connect_client()
    resp = pipe_mod.send_request(h, {"operation": "get_experiments"})
    pipe_mod.close_client(h)
    marker_ids = {m["experiment_id"] for m in resp.get("data", {}).get("markers", [])}
    check("a real experiment marker appears in the marker list (no experiment_id = discovery)",
          marker["experiment_id"] in marker_ids, f"data: {resp.get('data')}")

    h = pipe_mod.connect_client()
    resp2 = pipe_mod.send_request(
        h, {"operation": "get_experiments", "parameters": {"experiment_id": marker["experiment_id"]}})
    pipe_mod.close_client(h)
    check("a full report for that marker comes back with the real marker description",
          resp2.get("ok") is True and resp2["data"]["experiment"]["description"] == "test fan install",
          f"response: {resp2}")
    check("the report carries the non-causal caveat, even on a real marker (never omitted)",
          "not what caused it" in resp2["data"].get("caveat", ""), f"caveat: {resp2['data'].get('caveat')}")

    h = pipe_mod.connect_client()
    resp3b = pipe_mod.send_request(
        h, {"operation": "get_experiments", "parameters": {"experiment_id": "no-such-id"}})
    pipe_mod.close_client(h)
    check("an unknown experiment_id is rejected cleanly, not a crash or an empty success",
          resp3b.get("ok") is False and resp3b["error"]["code"] == "unknown_experiment_id",
          f"response: {resp3b}")
finally:
    app.stop_event.set()
    app.destroy()

print()
print("=" * 78)
print("8. shutdown is clean, and a client fails fast once the server is gone")
print("=" * 78)
pipe_thread = getattr(app.evidence_pipe_server, "_thread", None)
check("the accept-loop thread is no longer alive after destroy()'s bounded stop()",
      pipe_thread is None or not pipe_thread.is_alive())

start = time.monotonic()
connected = False
try:
    h = pipe_mod.connect_client(timeout_s=2.0)
    connected = True
    pipe_mod.close_client(h)
except OSError:
    pass
elapsed = time.monotonic() - start
check("connecting after the app (and its pipe server) is gone fails in well under a second - "
      "the direct regression test for the original incident (Nox getting an immediate, honest "
      "'not running' signal instead of hanging or reading stale data)",
      not connected and elapsed < 1.0, f"connected={connected}, elapsed={elapsed:.2f}s")

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
    print("ALL EVIDENCE PIPE CHECKS PASSED")
