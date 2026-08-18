"""Deterministic security and semantics verification for the Phase 11 Nox adapter."""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: F401,E402
import thermal_watch_evidence_cli as adapter  # noqa: E402

FAILURES = []
CHECKS = 0


def check(name, condition):
    global CHECKS
    CHECKS += 1
    print(f"[{'PASS' if condition else 'FAIL'}] {CHECKS:2d}. {name}")
    if not condition:
        FAILURES.append(name)


snapshot = {
    "schema_version": "1.0", "generated_at": 1234.5,
    "system": {"cpu_model": "fixture CPU", "cpu_cores": 4, "cpu_threads": 8, "uptime_seconds": 99},
    "live": {
        "cpu": {"temp_c": None, "load_pct": 12.0, "power_w": None, "fan_rpm": None},
        "gpu": {"core_temp_c": 55.0, "hotspot_temp_c": 65.0, "vram_temp_c": None,
                "load_pct": 20.0, "power_w": 80.0, "vram_used_mb": 1000.0, "fan_pct": 30.0},
        "memory": {"used_pct": 25.0}, "bridge_health": "HEALTHY",
        "network": {"adapter_name": "Wi-Fi", "connected": True, "down_mbps": 10.0,
                    "up_mbps": 1.0, "per_process_capture_active": True,
                    "top_processes": [{"pid": 42, "name": "fixture", "bytes_in": 100,
                                       "bytes_out": 20, "down_mbps": 9.5, "up_mbps": 0.5}]},
    },
    "recent_incidents_24h": [{"incident_id": "i-1", "component": "gpu", "peak_value": 91.0}],
    "recent_sessions_24h": [{"session_id": "s-1", "workload_key": "game.exe"}],
    "coverage_24h": {"valid_buckets": 10, "expected_buckets": 20, "coverage_pct": 50.0},
}

with tempfile.TemporaryDirectory(prefix="tw_nox_adapter_") as td:
    evidence_path = Path(td) / "thermal_watch_evidence.json"
    evidence_path.write_text(json.dumps(snapshot), encoding="utf-8")
    before = evidence_path.read_bytes()
    with mock.patch.object(adapter, "EVIDENCE_SNAPSHOT_PATH", evidence_path):
        desc = adapter.handle_request({"operation": "describe_operations"})
        check("operation discovery succeeds and advertises exactly the allowlisted operations",
              desc["ok"] and set(desc["operations"]) == set(adapter.OPERATIONS))
        sensors = adapter.handle_request({"operation": "get_current_sensors"})
        check("valid operation succeeds with Thermal Watch provenance",
              sensors["ok"] and sensors["provenance"]["authority"] == "Thermal Watch")
        check("unavailable sensor values remain null, never zero", sensors["data"]["cpu"]["temp_c"] is None)
        coverage = adapter.handle_request({"operation": "get_coverage"})
        check("incomplete monitoring coverage is preserved as monitoring_gap",
              coverage["evidence_status"] == "monitoring_gap" and coverage["data"]["coverage_pct"] == 50.0)
        check("coverage gap explicitly makes unmonitored periods unanswerable",
              coverage["monitoring_limit"]["can_establish_events_during_unmonitored_time"] is False)
        top = adapter.handle_request({"operation": "get_top_network_processes", "parameters": {"limit": 1}})
        check("verified per-process current-rate evidence is returned read-only with explicit units",
              top["data"][0]["pid"] == 42 and top["data"][0]["current_download_mbps"] == 9.5
              and top["data"][0]["current_combined_mbps"] == 10.0)
        check("right-now operation omits cumulative byte counters that could be mistaken for current usage",
              "bytes_in" not in top["data"][0] and "bytes_out" not in top["data"][0])
        incidents = adapter.handle_request({"operation": "get_recent_incidents"})
        sessions = adapter.handle_request({"operation": "get_recent_sessions"})
        check("incident and session evidence is returned with incomplete-coverage status",
              incidents["data"][0]["incident_id"] == "i-1" and sessions["data"][0]["session_id"] == "s-1"
              and incidents["evidence_status"] == sessions["evidence_status"] == "monitoring_gap")
        check("incident-local gap fields cannot be mistaken for evidence about unmonitored periods",
              "outside recorded incidents" in incidents["monitoring_limit"]["incident_monitoring_gap_seconds_scope"])
        check("unknown operation is rejected", not adapter.handle_request({"operation": "delete_evidence"})["ok"])
        hostile = ("path", "../secret", "C:\\secret", "command", "script", "sql", "mutation")
        for name in hostile:
            value = adapter.handle_request({"operation": "get_system_status", "parameters": {name: "x"}})
            check(f"hostile/unknown parameter '{name}' is rejected", not value["ok"])
        check("unknown top-level request field is rejected",
              not adapter.handle_request({"operation": "get_system_status", "command": "whoami"})["ok"])
        check("invalid bounded limit is rejected",
              not adapter.handle_request({"operation": "get_recent_sessions", "parameters": {"limit": 1000}})["ok"])
        check("adapter cannot mutate its only evidence source", evidence_path.read_bytes() == before)

    cli = Path(__file__).resolve().parent.parent / "thermal_watch_evidence_cli.py"
    proc = subprocess.run([sys.executable, str(cli)], input="{not json", text=True,
                          capture_output=True, timeout=10)
    malformed = json.loads(proc.stdout)
    check("malformed JSON is rejected with one structured JSON response",
          proc.returncode != 0 and malformed["error"]["code"] == "malformed_json")
    encoded = __import__("base64").b64encode(
        json.dumps({"operation": "describe_operations"}).encode("utf-8")
    ).decode("ascii")
    proc_b64 = subprocess.run([sys.executable, str(cli), "--request-base64", encoded], text=True,
                              capture_output=True, timeout=10)
    check("fixed base64 request transport returns the same allowlisted CLI contract",
          proc_b64.returncode == 0 and json.loads(proc_b64.stdout)["operation"] == "describe_operations")
    bad_b64 = subprocess.run([sys.executable, str(cli), "--request-base64", "../not-base64"], text=True,
                             capture_output=True, timeout=10)
    check("malformed base64 transport input is rejected as malformed JSON, never treated as a path",
          bad_b64.returncode != 0 and json.loads(bad_b64.stdout)["error"]["code"] == "malformed_json")

check("Thermal Watch imports and works with Nox entirely absent", adapter.handle_request({"operation": "describe_operations"})["ok"])
check("no command, SQL, eval, exec, subprocess, or caller path primitive exists in adapter request handling",
      all(token not in Path(adapter.__file__).read_text(encoding="utf-8")
          for token in ("os.system", "shell=True", "eval(", "exec(", "sqlite3", "request.get(\"path\")")))

print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    raise SystemExit(1)
print("ALL NOX EVIDENCE ADAPTER CHECKS PASSED")
