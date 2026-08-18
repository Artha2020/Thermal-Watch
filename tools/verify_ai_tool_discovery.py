"""Deterministic Phase 13 verification for canonical AI tool discovery."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: F401,E402
import thermal_watch_evidence_cli as evidence_api  # noqa: E402
from ai.provider_contract import EvidenceBroker, ProviderConfig  # noqa: E402
from ai.providers.custom import CustomProvider  # noqa: E402
from ai.providers.nox import NoxProvider  # noqa: E402
from ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402


FAILURES = []
CHECKS = 0


def check(name, condition):
    global CHECKS
    CHECKS += 1
    print(f"[{'PASS' if condition else 'FAIL'}] {CHECKS:2d}. {name}")
    if not condition:
        FAILURES.append(name)


def schema_is_valid(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        return False
    if not set(required).issubset(properties):
        return False
    if schema.get("additionalProperties") is not False:
        return False
    for value in properties.values():
        if not isinstance(value, dict) or "type" not in value:
            return False
        if value.get("type") == "integer":
            if type(value.get("minimum")) is not int or type(value.get("maximum")) is not int:
                return False
            if value["minimum"] > value["maximum"]:
                return False
    return True


snapshot = {
    "schema_version": "1.0", "generated_at": 1234.5,
    "system": {"cpu_model": "fixture CPU", "cpu_cores": 4, "cpu_threads": 8},
    "live": {
        "bridge_health": "HEALTHY",
        "cpu": {"temp_c": 52.0, "load_pct": 12.0},
        "gpu": {"core_temp_c": None, "load_pct": None},
        "memory": {"used_pct": 25.0},
        "network": {"connected": True, "down_mbps": 10.0, "up_mbps": 1.0,
                    "per_process_capture_active": False, "top_processes": []},
    },
    "recent_incidents_24h": [], "recent_sessions_24h": [],
    "coverage_24h": {"valid_buckets": 10, "expected_buckets": 20, "coverage_pct": 50.0},
}


with tempfile.TemporaryDirectory(prefix="tw_tool_discovery_") as td:
    evidence_path = Path(td) / "thermal_watch_evidence.json"
    evidence_path.write_text(json.dumps(snapshot), encoding="utf-8")
    before = evidence_path.read_bytes()
    with mock.patch.object(evidence_api, "EVIDENCE_SNAPSHOT_PATH", evidence_path):
        broker = EvidenceBroker()
        catalog = broker.catalog()
        operations = catalog["operations"]
        expected = set(evidence_api.OPERATIONS)

        check("catalog carries the independent tool-contract version",
              catalog["tool_catalog_version"] == 1 and catalog["tool_catalog_schema"] == "thermal-watch-tool-catalog")
        check("every registered operation appears exactly once", set(operations) == expected and len(operations) == len(expected))
        check("operation names are unique", len(operations) == len(set(operations)))
        check("every operation embeds its canonical name", all(name == value["name"] for name, value in operations.items()))
        check("every operation has a non-empty public description",
              all(isinstance(value["description"], str) and value["description"].strip() for value in operations.values()))
        check("every parameter schema is structurally valid and strict",
              all(schema_is_valid(value["parameters"]) for value in operations.values()))
        check("every operation publishes a response object schema",
              all(value["response"].get("type") == "object" for value in operations.values()))
        check("every operation is explicitly read-only", all(value["read_only"] is True for value in operations.values()))

        process_capability = operations["get_top_network_processes"]["availability"]
        check("currently unavailable conditional capability remains visible",
              process_capability["state"] == "conditional" and process_capability["available"] is False
              and "unavailable" in process_capability["reason"])
        sensor_capability = operations["get_current_sensors"]["availability"]
        check("component-level hardware availability is represented",
              sensor_capability["state"] == "conditional"
              and sensor_capability["details"]["components"]["gpu"]["available"] is False
              and sensor_capability["details"]["components"]["cpu"]["available"] is True)
        check("unavailable operations are never hidden", "get_top_network_processes" in operations)

        semantics = catalog["semantics"]
        check("all evidence states are documented",
              set(semantics["statuses"]) == {"observed", "derived", "unavailable", "monitoring_gap"})
        check("missing-is-not-zero rule is explicit", "never means zero" in semantics["rules"]["missing_values"])
        check("unmonitored time is explicitly unknowable", "cannot determine" in semantics["rules"]["unmonitored_time"])
        check("correlation is not exposed as causation", "must not" in semantics["rules"]["causation"])
        check("units, timestamps, provenance, and coverage are preserved",
              set(semantics["preserved_metadata"]) == {"units", "timestamps", "provenance", "coverage"})

        import getpass
        import socket
        serialized = json.dumps(catalog, sort_keys=True)
        private_tokens = ("C:\\", "D:\\", "B:\\", getpass.getuser(), socket.gethostname(),
                          "password", "api_key", "SELECT ", "powershell")
        check("catalog exposes no paths, identities, credentials, SQL, or command templates",
              not any(token and token.lower() in serialized.lower() for token in private_tokens))
        mutation_words = ("delete", "write", "set_threshold", "execute", "mutate")
        check("catalog exposes no mutation operation", not any(any(word in name for word in mutation_words) for name in operations))

        unknown = broker.dispatch("thermal_watch_evidence", {
            "operation": "get_system_status", "parameters": {"path": "private"}})
        check("unknown parameters remain rejected by the canonical schema",
              unknown["error"]["code"] == "unknown_parameter")
        legacy = evidence_api.handle_request({"operation": "get_recent_incidents", "parameters": {"limit": 1}})
        check("existing Phase 11 request shape remains unchanged", legacy["ok"] and legacy["operation"] == "get_recent_incidents")

        nox_seen = {}
        def nox_transport(question, tool, dispatch):
            nox_seen["catalog"] = tool["x-thermal-watch-catalog"]
            result = dispatch("thermal_watch_evidence", {"operation": "describe_operations"})
            return {"answer": "catalog consumed", "evidence": [result]}
        nox_result = NoxProvider(ProviderConfig(provider="nox"), transport=nox_transport).ask("what tools exist?", broker)
        check("Nox provider consumes the canonical catalog",
              nox_result.ok and nox_seen["catalog"]["tool_catalog_version"] == 1
              and nox_result.evidence[0]["operations"] == operations)

        custom_seen = {}
        def custom_handler(question, tool, dispatch):
            custom_seen["catalog"] = tool["x-thermal-watch-catalog"]
            return {"answer": "custom catalog", "evidence": []}
        custom = CustomProvider(ProviderConfig(provider="custom"), handler=custom_handler).ask("inspect", broker)
        check("custom provider can inspect the canonical catalog",
              custom.ok and custom_seen["catalog"]["operations"] == operations)

        openai = OpenAICompatibleProvider(
            ProviderConfig(provider="openai_compatible", endpoint="http://127.0.0.1:1234/v1", model="fixture"),
            transport=object())
        openai_tool = broker.tool_definition()
        check("OpenAI-compatible tool operation enum is derived from the catalog",
              openai_tool["function"]["parameters"]["properties"]["operation"]["enum"] == list(operations))
        check("OpenAI-compatible standard tool definition omits local vendor metadata",
              "x-thermal-watch-catalog" not in openai_tool
              and openai.capabilities(broker).as_dict()["tool_catalog_version"] == 1)

        check("discovery and provider inspection leave evidence byte-identical", evidence_path.read_bytes() == before)

    missing_path = Path(td) / "missing.json"
    with mock.patch.object(evidence_api, "EVIDENCE_SNAPSHOT_PATH", missing_path):
        missing_catalog = evidence_api.describe_operation_catalog()
        check("catalog remains discoverable when evidence is unavailable",
              missing_catalog["operations"]["describe_operations"]["availability"]["available"] is True)
        check("missing snapshot marks evidence operations unavailable without hiding them",
              all(not value["availability"]["available"] for name, value in missing_catalog["operations"].items()
                  if name != "describe_operations"))


print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    raise SystemExit(1)
print("ALL AI TOOL DISCOVERY CHECKS PASSED")
