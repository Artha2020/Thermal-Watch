"""Deterministic verification for Phase 12's provider-neutral AI adapter."""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: F401,E402
import thermal_watch_evidence_cli as evidence_api  # noqa: E402
from ai.provider_contract import EvidenceBroker, ProviderConfig, ProviderContractError  # noqa: E402
from ai.provider_registry import ProviderRegistry, UniversalAIAdapter  # noqa: E402


FAILURES = []
CHECKS = 0


def check(name, condition):
    global CHECKS
    CHECKS += 1
    print(f"[{'PASS' if condition else 'FAIL'}] {CHECKS:2d}. {name}")
    if not condition:
        FAILURES.append(name)


def expect_error(name, code, fn):
    try:
        fn()
    except ProviderContractError as exc:
        check(name, exc.code == code)
    else:
        check(name, False)


class FakeTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def post_json(self, url, payload, headers, timeout):
        self.calls.append((url, copy.deepcopy(payload), dict(headers), timeout))
        if self.error:
            raise self.error
        return copy.deepcopy(self.responses.pop(0))


def tool_call(arguments, name="thermal_watch_evidence"):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}}]}


def answer(text="grounded answer"):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


snapshot = {
    "schema_version": "1.0", "generated_at": 1234.5,
    "system": {"cpu_model": "fixture CPU", "cpu_cores": 4, "cpu_threads": 8},
    "live": {
        "bridge_health": "HEALTHY",
        "cpu": {"temp_c": None, "load_pct": 12.0},
        "gpu": {"core_temp_c": 55.0, "load_pct": 20.0},
        "memory": {"used_pct": 25.0},
        "network": {"connected": True, "down_mbps": 10.0, "up_mbps": 1.0,
                    "per_process_capture_active": True, "top_processes": []},
    },
    "recent_incidents_24h": [], "recent_sessions_24h": [],
    "coverage_24h": {"valid_buckets": 10, "expected_buckets": 20, "coverage_pct": 50.0},
}


with tempfile.TemporaryDirectory(prefix="tw_universal_ai_") as td:
    evidence_path = Path(td) / "thermal_watch_evidence.json"
    evidence_path.write_text(json.dumps(snapshot), encoding="utf-8")
    before = evidence_path.read_bytes()
    with mock.patch.object(evidence_api, "EVIDENCE_SNAPSHOT_PATH", evidence_path):
        broker = EvidenceBroker()
        registry = ProviderRegistry()

        none_adapter = UniversalAIAdapter()
        check("no provider configured leaves the optional adapter unavailable",
              not none_adapter.capabilities()["configured"])
        check("no provider configured fails safely without touching monitoring",
              none_adapter.ask("what is my CPU temperature?").error["code"] == "provider_not_configured")
        check("registry exposes exactly the Phase 12 provider types",
              set(registry.names()) == {"nox", "openai_compatible", "custom"})

        unknown = UniversalAIAdapter({"provider": "unknown"})
        check("unknown provider is rejected and contained",
              unknown.ask("question").error["code"] == "unknown_provider")
        expect_error("non-HTTP endpoint is rejected", "invalid_endpoint", lambda: ProviderConfig.from_mapping({
            "provider": "openai_compatible", "endpoint": "file:///tmp/model", "model": "fixture"}))
        expect_error("endpoint credentials are rejected", "invalid_endpoint", lambda: ProviderConfig.from_mapping({
            "provider": "openai_compatible", "endpoint": "http://user:pass@localhost:1234/v1", "model": "fixture"}))
        expect_error("remote endpoint requires explicit approval", "remote_endpoint_disabled", lambda: ProviderConfig.from_mapping({
            "provider": "openai_compatible", "endpoint": "https://example.com/v1", "model": "fixture"}))
        local_config = ProviderConfig.from_mapping({
            "provider": "openai_compatible", "endpoint": "http://127.0.0.1:1234/v1",
            "model": "fixture", "api_key": "runtime-only-secret",
        })
        check("runtime API keys are redacted from public configuration",
              "runtime-only-secret" not in json.dumps(local_config.public_dict()))

        rejected_tool = broker.dispatch("read_file", {"path": "../private"})
        check("provider cannot request arbitrary files", rejected_tool["error"]["code"] == "unknown_tool")
        rejected_command = broker.dispatch("thermal_watch_evidence", {
            "operation": "get_system_status", "parameters": {"command": "whoami"}})
        check("provider cannot execute commands", rejected_command["error"]["code"] == "unknown_parameter")
        rejected_mutation = broker.dispatch("thermal_watch_evidence", {"operation": "delete_evidence"})
        check("provider cannot request mutations", rejected_mutation["error"]["code"] == "unknown_operation")
        coverage = broker.dispatch("thermal_watch_evidence", {"operation": "get_coverage"})
        check("evidence gaps pass through unchanged",
              coverage["evidence_status"] == "monitoring_gap"
              and coverage["monitoring_limit"]["can_establish_events_during_unmonitored_time"] is False)
        sensors = broker.dispatch("thermal_watch_evidence", {"operation": "get_current_sensors"})
        check("unavailable evidence remains null", sensors["data"]["cpu"]["temp_c"] is None)

        def nox_transport(question, schema, dispatch):
            value = dispatch("thermal_watch_evidence", {"operation": "get_coverage"})
            return {"answer": "Nox used Thermal Watch evidence.", "evidence": [value]}

        nox = UniversalAIAdapter({"provider": "nox"}, provider_dependencies={"transport": nox_transport})
        nox_result = nox.ask("what was monitored?")
        check("Nox conforms through the same universal broker", nox_result.ok and nox_result.evidence[0] == coverage)
        nox_unavailable = UniversalAIAdapter({"provider": "nox"}).ask("question")
        check("external Nox absence is handled safely", nox_unavailable.error["code"] == "external_provider")

        transport = FakeTransport([
            tool_call({"operation": "get_current_sensors"}), answer(),
        ])
        generic = UniversalAIAdapter(local_config, provider_dependencies={"transport": transport})
        generic_result = generic.ask("How hot is the GPU?")
        check("OpenAI-compatible provider completes a bounded evidence tool loop",
              generic_result.ok and generic_result.answer == "grounded answer" and len(generic_result.evidence) == 1)
        check("OpenAI-compatible request advertises only the Thermal Watch evidence tool",
              [row["function"]["name"] for row in transport.calls[0][1]["tools"]] == ["thermal_watch_evidence"])
        check("tool result returned to provider preserves Thermal Watch authority",
              generic_result.evidence[0]["provenance"]["authority"] == "Thermal Watch")
        check("generic endpoint path uses OpenAI chat-completions shape",
              transport.calls[0][0] == "http://127.0.0.1:1234/v1/chat/completions")

        malicious_transport = FakeTransport([
            tool_call({"path": "C:\\private"}, name="read_file"), answer("request rejected"),
        ])
        malicious = UniversalAIAdapter(local_config, provider_dependencies={"transport": malicious_transport}).ask("read it")
        check("malicious provider tool request is contained as rejected evidence",
              malicious.ok and malicious.evidence[0]["error"]["code"] == "unknown_tool")

        malformed = UniversalAIAdapter(local_config, provider_dependencies={
            "transport": FakeTransport([{"choices": []}])}).ask("question")
        check("malformed provider response is rejected",
              not malformed.ok and malformed.error["code"] == "malformed_provider_response")
        malformed_args = UniversalAIAdapter(local_config, provider_dependencies={
            "transport": FakeTransport([{"choices": [{"message": {"tool_calls": [{
                "id": "bad", "function": {"name": "thermal_watch_evidence", "arguments": "{bad"}}]}}]}])
        }).ask("question")
        check("malformed tool arguments are rejected",
              not malformed_args.ok and malformed_args.error["code"] == "malformed_provider_response")

        unavailable = UniversalAIAdapter(local_config, provider_dependencies={
            "transport": FakeTransport(error=ProviderContractError("provider_unavailable", "offline"))
        }).ask("question")
        check("unavailable provider cannot escape the adapter boundary",
              not unavailable.ok and unavailable.error["code"] == "provider_unavailable")

        def custom_handler(question, schema, dispatch):
            return {"answer": "custom", "evidence": [dispatch("thermal_watch_evidence", {
                "operation": "get_system_status"})]}

        custom = UniversalAIAdapter({"provider": "custom"}, provider_dependencies={"handler": custom_handler}).ask("status")
        check("custom/local provider uses the same read-only contract", custom.ok and custom.answer == "custom")
        bad_custom = UniversalAIAdapter({"provider": "custom"}, provider_dependencies={
            "handler": lambda *_: {"unexpected": True}}).ask("status")
        check("malformed custom response is rejected", bad_custom.error["code"] == "malformed_provider_response")
        crashing_custom = UniversalAIAdapter({"provider": "custom"}, provider_dependencies={
            "handler": lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))}).ask("status")
        check("provider crash is contained", crashing_custom.error["code"] == "provider_unavailable")

        check("all provider activity leaves the evidence file byte-identical", evidence_path.read_bytes() == before)


print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    raise SystemExit(1)
print("ALL UNIVERSAL AI ADAPTER CHECKS PASSED")
