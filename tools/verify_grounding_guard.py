"""Deterministic verification for Phase 15 - Grounding Guard.

Checks that GroundingGuard.review() correctly classifies AI-answer claims against the
evidence already carried on the same ProviderResponse (supported / contradicted / unsupported
/ unverifiable), that a contradicted claim is ALWAYS corrected or redacted before it can reach
a caller (fail-closed), that citation validity is enforced, that malformed input degrades safely
without raising, and that UniversalAIAdapter.ask() wires the Guard in as documented (including
its own exception boundary).

House style matches tools/verify_universal_ai_adapter.py: hand-rolled check() accumulator,
numbered PASS/FAIL, FAILURES list, sys.exit(1) on any failure, evidence-file byte-identity
assertions wherever a real snapshot file is involved.
"""
from __future__ import annotations

import ast
import copy
import json
import tempfile
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: F401,E402
import thermal_watch_evidence_cli as evidence_api  # noqa: E402
from ai.grounding_guard import (  # noqa: E402
    BLOCKED_ANSWER_TEXT, FIXED_LITERAL_CITATION, FIXED_LITERAL_CLAIM, GroundingGuard, GroundingReport,
)
from ai.provider_contract import EvidenceBroker, ProviderConfig, ProviderResponse  # noqa: E402
from ai.provider_registry import UniversalAIAdapter  # noqa: E402
from ai.providers.custom import CustomProvider  # noqa: E402
from ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402


FAILURES = []
CHECKS = 0


def check(name, condition):
    global CHECKS
    CHECKS += 1
    print(f"[{'PASS' if condition else 'FAIL'}] {CHECKS:2d}. {name}")
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------------------------------
# Fixture helpers - plain dicts shaped exactly like real thermal_watch_evidence_cli.py responses
# (read in full before writing this file; see the operation handlers in handle_request()).
# ---------------------------------------------------------------------------------------------

def resp(answer, evidence=(), provider="fixture", model="fixture-model"):
    return ProviderResponse(ok=True, provider=provider, answer=answer, model=model, evidence=list(evidence))


def sensors_item(cpu=None, gpu=None, memory=None, evidence_status="observed"):
    return {"operation": "get_current_sensors", "evidence_status": evidence_status,
            "data": {"cpu": cpu or {}, "gpu": gpu or {}, "memory": memory or {}}}


def network_item(**data):
    return {"operation": "get_network_status", "data": dict(data)}


def top_processes_item(rows):
    return {"operation": "get_top_network_processes", "data": list(rows)}


def coverage_item(pct, gaps=None):
    return {"operation": "get_coverage", "data": {"coverage_pct": pct, "gaps": gaps or []}}


def incidents_item(rows):
    return {"operation": "get_recent_incidents", "data": list(rows)}


class FakeTransport:
    """Same shape as tools/verify_universal_ai_adapter.py's FakeTransport - never touches a
    real network socket."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_json(self, url, payload, headers, timeout):
        self.calls.append((url, copy.deepcopy(payload), dict(headers), timeout))
        return copy.deepcopy(self.responses.pop(0))


def tool_call(arguments, name="thermal_watch_evidence"):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}}]}


def answer_msg(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


print("=== 1. Correct numeric claim -> supported, answer unchanged ===")
r1 = GroundingGuard().review(resp(
    "The CPU temperature is 45.0 C right now.",
    [sensors_item(cpu={"temp_c": 45.0})]))
check("correct CPU temperature claim is supported", r1.grounding.claims[0].verdict == "supported")
check("a supported claim leaves the answer text untouched", r1.answer == "The CPU temperature is 45.0 C right now.")
check("no correction was applied", r1.grounding.corrected is False and r1.grounding.verdict == "clean")

print("\n=== 2. Wrong numeric value -> contradicted, corrected/redacted in the final answer ===")
r2 = GroundingGuard().review(resp(
    "The CPU temperature is 80.0 C right now.",
    [sensors_item(cpu={"temp_c": 45.0})]))
check("wrong CPU temperature value is contradicted", r2.grounding.claims[0].verdict == "contradicted")
check("the wrong value never survives into the final answer", "80.0" not in r2.answer)
check("a fixed literal redaction replaced the wrong value", FIXED_LITERAL_CLAIM in r2.answer)
check("grounding.corrected is True", r2.grounding.corrected is True and r2.grounding.verdict == "corrected")

print("\n=== 3. Correct value, wrong semantic field (real number, wrong sensor label) -> contradicted ===")
r3 = GroundingGuard().review(resp(
    "The GPU hotspot temperature is 45.0 C right now.",
    [sensors_item(cpu={"temp_c": 45.0}, gpu={"hotspot_temp_c": 60.0})]))
check("a real value attached to the wrong sensor is contradicted", r3.grounding.claims[0].verdict == "contradicted")
check("the unambiguous sibling match relabels the field rather than nuking the number",
      "45.0" in r3.answer and "CPU Package Temperature" in r3.answer)
check("the wrong label ('GPU hotspot temperature') no longer appears", "GPU hotspot temperature" not in r3.answer)

print("\n=== 4. SYNTHETIC swapped upload/download regression fixture (Phase 15 canonical case) ===")
# NOTE: I searched this repo (ROADMAP.md, commit history, full-text grep) for the "canonical
# Phase 13" swapped upload/download incident this task description references and found no
# recorded transcript, numbers, or process name anywhere. This fixture is entirely SYNTHETIC,
# modeling the described transposition failure mode - it is not a recovered real incident.
# Exercised end-to-end through UniversalAIAdapter + OpenAICompatibleProvider's real bounded
# tool-call loop with a FakeTransport, per the house style in verify_universal_ai_adapter.py.
snapshot4 = {
    "schema_version": "1.0", "generated_at": 1234.5,
    "system": {"cpu_model": "fixture CPU", "cpu_cores": 4, "cpu_threads": 8},
    "live": {
        "bridge_health": "HEALTHY",
        "cpu": {"temp_c": 50.0, "load_pct": 10.0},
        "gpu": {"core_temp_c": None, "load_pct": None},
        "memory": {"used_pct": 30.0},
        "network": {
            "connected": True, "down_mbps": 100.0, "up_mbps": 4.0,
            "per_process_capture_active": True,
            # Real confirmed field names from thermal_watch_evidence_cli.get_top_network_processes:
            # "down_mbps"/"up_mbps" on the raw snapshot row, exposed to providers as
            # "current_download_mbps"/"current_upload_mbps".
            "top_processes": [
                {"pid": 111, "name": "steam.exe", "down_mbps": 99.64, "up_mbps": 3.20},
                {"pid": 222, "name": "chrome.exe", "down_mbps": 5.0, "up_mbps": 1.0},
            ],
        },
    },
    "recent_incidents_24h": [], "recent_sessions_24h": [],
    "coverage_24h": {"valid_buckets": 10, "expected_buckets": 20, "coverage_pct": 50.0, "gaps": []},
}
with tempfile.TemporaryDirectory(prefix="tw_grounding_guard_") as td4:
    evidence_path4 = Path(td4) / "thermal_watch_evidence.json"
    evidence_path4.write_text(json.dumps(snapshot4), encoding="utf-8")
    before4 = evidence_path4.read_bytes()
    with mock.patch.object(evidence_api, "EVIDENCE_SNAPSHOT_PATH", evidence_path4):
        transport4 = FakeTransport([
            tool_call({"operation": "get_top_network_processes"}),
            # The model's canned answer TRANSPOSES the direction: steam's real download rate
            # (99.64 Mbps) is misreported as its upload rate.
            answer_msg("steam is uploading at 99.64 Mbps right now."),
        ])
        config4 = {"provider": "openai_compatible", "endpoint": "http://127.0.0.1:1234/v1", "model": "fixture"}
        adapter4 = UniversalAIAdapter(config4, provider_dependencies={"transport": transport4})
        result4 = adapter4.ask("How much bandwidth is steam using?")
        check("adapter call succeeds through the real bounded tool-call loop", result4.ok)
        proc_claims4 = [c for c in result4.grounding.claims if c.field == "net_process_rate"]
        check("the swapped upload/download claim is caught as contradicted",
              len(proc_claims4) == 1 and proc_claims4[0].verdict == "contradicted")
        check("the final answer's verb is corrected from uploading to downloading",
              "steam is downloading at 99.64 Mbps" in result4.answer)
        check("grounding.verdict is 'corrected' for the swap fixture", result4.grounding.verdict == "corrected")
    check("evidence file is byte-identical after the swap regression check", evidence_path4.read_bytes() == before4)

print("\n=== 5. Wrong process attribution (real value, wrong process name) -> contradicted ===")
process_rows5 = [
    {"pid": 111, "process_name": "steam.exe", "current_download_mbps": 99.64, "current_upload_mbps": 3.20},
    {"pid": 222, "process_name": "chrome.exe", "current_download_mbps": 5.0, "current_upload_mbps": 1.0},
]
r5 = GroundingGuard().review(resp(
    "chrome is downloading at 99.64 Mbps right now.",
    [top_processes_item(process_rows5)]))
check("misattributed process rate is contradicted", r5.grounding.claims[0].verdict == "contradicted")
check("the process name is corrected to the one evidence actually reports it for",
      "steam.exe is downloading at 99.64 Mbps" in r5.answer)

print("\n=== 6. Correct incident fact + valid evidence ID -> supported, citation preserved ===")
r6 = GroundingGuard().review(resp(
    "A CPU incident occurred, see INC-20260810-0001 for details.",
    [incidents_item([{"incident_id": "i1", "evidence_id": "INC-20260810-0001",
                       "component": "cpu", "max_zone": "ORANGE", "peak_value": 88.0}])]))
check("valid citation resolves to a supported claim",
      any(c.verdict == "supported" and c.claimed_value == "INC-20260810-0001" for c in r6.grounding.claims))
check("valid citation is preserved verbatim in the final answer", "INC-20260810-0001" in r6.answer)
check("citations_valid records the id", r6.grounding.citations_valid == ["INC-20260810-0001"])
check("nothing was redacted", r6.grounding.corrected is False)

print("\n=== 7. Fabricated evidence ID (well-formed, not present anywhere) -> rejected, redacted ===")
r7 = GroundingGuard().review(resp(
    "See INC-20260810-9999 for details.",
    [incidents_item([{"incident_id": "i1", "evidence_id": "INC-20260810-0001", "component": "cpu"}])]))
check("fabricated citation is rejected", r7.grounding.citations_rejected == ["INC-20260810-9999"])
check("fabricated citation is redacted from the final answer", "INC-20260810-9999" not in r7.answer
      and FIXED_LITERAL_CITATION in r7.answer)
check("the fabricated citation's claim is contradicted",
      any(c.claimed_value == "INC-20260810-9999" and c.verdict == "contradicted" for c in r7.grounding.claims))

print("\n=== 8. Well-formed evidence ID belonging to a DIFFERENT, absent record -> rejected the same way ===")
r8 = GroundingGuard().review(resp(
    "See INC-20260812-0007 for details.",
    [incidents_item([{"incident_id": "i1", "evidence_id": "INC-20260810-0001", "component": "cpu"}])]))
check("a real-shaped id from a different, absent record is rejected identically to a fabricated one",
      r8.grounding.citations_rejected == ["INC-20260812-0007"] and FIXED_LITERAL_CITATION in r8.answer)

print("\n=== 9. Null represented as zero -> contradicted ===")
r9 = GroundingGuard().review(resp(
    "CPU temperature is 0 C.",
    [sensors_item(cpu={"temp_c": None})]))
check("a null field claimed as literal zero is contradicted", r9.grounding.claims[0].verdict == "contradicted")
check("the zero-as-null claim is redacted, not left standing", "0 C" not in r9.answer and FIXED_LITERAL_CLAIM in r9.answer)

print("\n=== 10. Unavailable value claimed as a known concrete number -> contradicted ===")
r10 = GroundingGuard().review(resp(
    "GPU hotspot temperature is 72.0 C.",
    [sensors_item(gpu={"hotspot_temp_c": None}, evidence_status="unavailable")]))
check("an unavailable sensor claimed as a known value is contradicted", r10.grounding.claims[0].verdict == "contradicted")
check("the unavailable-claimed-as-known text is redacted", "72.0" not in r10.answer)

print("\n=== 11. Monitoring-gap claims: definite -> contradicted, hedged/no-data-point -> unsupported ===")
gapped_coverage = [coverage_item(40.0)]
r11a = GroundingGuard().review(resp("Everything was fine all night despite the gap.", gapped_coverage))
check("a DEFINITE claim over a real monitoring gap is contradicted",
      any(c.field == "monitoring_gap" and c.verdict == "contradicted" for c in r11a.grounding.claims))
check("the definite over-a-gap claim is redacted", FIXED_LITERAL_CLAIM in r11a.answer)
r11b = GroundingGuard().review(resp("It might have stayed safe overnight, hard to say for sure.", gapped_coverage))
check("a HEDGED claim over a possibly-unmonitored period is unsupported, not contradicted",
      any(c.field == "monitoring_gap" and c.verdict == "unsupported" for c in r11b.grounding.claims))
check("a hedged claim is never redacted", r11b.answer == "It might have stayed safe overnight, hard to say for sure.")

print("\n=== 12. Correlation stated as causation -> contradicted (the PHRASING triggers it, not the facts) ===")
true_incident = [incidents_item([{"incident_id": "i1", "evidence_id": "INC-20260810-0001",
                                   "component": "gpu_hotspot", "max_zone": "RED", "peak_value": 101.0,
                                   "dominant_workload": "Cyberpunk2077.exe"}])]
r12 = GroundingGuard().review(resp(
    "The overheating was caused by Cyberpunk2077.exe running (see INC-20260810-0001).",
    true_incident))
check("causal phrasing is contradicted even though the underlying incident fact is true",
      any(c.verdict == "contradicted" and c.reason.startswith("causal language") for c in r12.grounding.claims))
check("the TRUE citation in the same sentence still resolves supported (facts vs. phrasing are separate)",
      "INC-20260810-0001" in r12.grounding.citations_valid)
check("the causal connective is redacted while the real citation survives",
      "caused by" not in r12.answer and "INC-20260810-0001" in r12.answer)

print("\n=== 13. Ordinary conversational prose -> zero ClaimResults, never flagged ===")
r13 = GroundingGuard().review(resp(
    "Let me know if you'd like more detail! Overall things look pretty normal today.", []))
check("conversational prose produces no claims at all", r13.grounding.claims == [])
check("conversational prose is never falsely marked contradicted/unsupported/unverifiable",
      r13.grounding.verdict == "clean" and r13.answer == r13.answer)

print("\n=== 14. Qualitative summary genuinely supported by evidence -> supported, unchanged ===")
r14 = GroundingGuard().review(resp(
    "CPU temperatures have stayed in a safe range today.",
    [sensors_item(cpu={"temp_c": 55.0}), incidents_item([])]))
check("a true qualitative safe-range claim is supported", r14.grounding.claims[0].verdict == "supported")
check("a supported qualitative claim leaves the answer untouched",
      r14.answer == "CPU temperatures have stayed in a safe range today." and r14.grounding.corrected is False)

print("\n=== 15. Malformed provider output never raises; always returns a valid ProviderResponse ===")
malformed_cases = [
    resp(None, []),
    ProviderResponse(ok=True, provider="x", answer="45", evidence="not a list"),
    resp("CPU temperature is 45", ["not a dict", 123, None,
         {"operation": "get_current_sensors", "data": {"cpu": {"temp_c": {"deeply": ["nested", "garbage"]}}}}]),
    ProviderResponse(ok=True, provider="x", answer=12345, evidence=[]),
    ProviderResponse(ok=True, provider="x", answer="", evidence=[{"operation": None, "data": None}]),
    resp("steam is uploading at abc Mbps", [top_processes_item([{"process_name": "steam.exe"}])]),
]
all_ok = True
for case in malformed_cases:
    try:
        out = GroundingGuard().review(case)
        all_ok = all_ok and isinstance(out, ProviderResponse) and isinstance(out.grounding, GroundingReport)
    except Exception as exc:  # pragma: no cover - the check itself is that this never happens
        print(f"        unexpected exception: {exc!r}")
        all_ok = False
check("review() never raises across a battery of malformed inputs and always returns a valid response", all_ok)

print("\n=== 16. Provider crash (before the Guard) is unaffected by Phase 15's changes ===")
print("    --- static analysis: grounding_guard.py never touches app/telemetry/network/subprocess ---")
guard_path = Path(__file__).resolve().parent.parent / "ai" / "grounding_guard.py"
guard_src = guard_path.read_text(encoding="utf-8")
guard_tree = ast.parse(guard_src)
imported_modules = set()
for node in ast.walk(guard_tree):
    if isinstance(node, ast.Import):
        imported_modules.update(alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        imported_modules.add(node.module.split(".")[0])
opens_files = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"
                   for n in ast.walk(guard_tree))
check("grounding_guard.py never imports app or thermal_watch_evidence_cli",
      not (imported_modules & {"app", "thermal_watch_evidence_cli"}))
check("grounding_guard.py never imports sqlite3, subprocess, socket, or an HTTP client",
      not (imported_modules & {"sqlite3", "subprocess", "socket", "requests", "urllib", "http"}))
check("grounding_guard.py never calls open() directly", not opens_files)
check("grounding_guard.py source contains no SQL/subprocess/network primitives",
      not any(token in guard_src for token in (".execute(", "Popen(", "socket.", "requests.")))

# Tests 16-18 route through UniversalAIAdapter/CustomProvider, whose EvidenceBroker.tool_definition()
# reads thermal_watch_evidence_cli.EVIDENCE_SNAPSHOT_PATH even for a failing/crashing provider -
# so, per house style, these run inside their own patched temp snapshot rather than touching the
# real production evidence file.
snapshot_other = {
    "schema_version": "1.0", "generated_at": 1234.5,
    "system": {"cpu_model": "fixture CPU", "cpu_cores": 4, "cpu_threads": 8},
    "live": {
        "bridge_health": "HEALTHY",
        "cpu": {"temp_c": 40.0, "load_pct": 5.0},
        "gpu": {"core_temp_c": None, "load_pct": None},
        "memory": {"used_pct": 20.0},
        "network": {"connected": True, "down_mbps": 1.0, "up_mbps": 0.1,
                    "per_process_capture_active": False, "top_processes": []},
    },
    "recent_incidents_24h": [], "recent_sessions_24h": [],
    "coverage_24h": {"valid_buckets": 10, "expected_buckets": 20, "coverage_pct": 50.0, "gaps": []},
}
with tempfile.TemporaryDirectory(prefix="tw_grounding_guard_other_") as td_other:
    evidence_path_other = Path(td_other) / "thermal_watch_evidence.json"
    evidence_path_other.write_text(json.dumps(snapshot_other), encoding="utf-8")
    before_other = evidence_path_other.read_bytes()
    with mock.patch.object(evidence_api, "EVIDENCE_SNAPSHOT_PATH", evidence_path_other):
        crashing_custom = UniversalAIAdapter({"provider": "custom"}, provider_dependencies={
            "handler": lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))}).ask("status")
        check("a provider crash still produces a clean ProviderResponse.failure(...)",
              not crashing_custom.ok and crashing_custom.error["code"] == "provider_unavailable")
        unknown_provider = UniversalAIAdapter({"provider": "unknown"}).ask("question")
        check("an unrecognized provider is still rejected before the Guard ever runs",
              unknown_provider.error["code"] == "unknown_provider")

        print("\n=== 17. Guard failure is contained by UniversalAIAdapter.ask(); never propagates ===")
        with mock.patch("ai.grounding_guard._build_evidence_index",
                         side_effect=RuntimeError("forced guard failure")):
            blocked = UniversalAIAdapter({"provider": "custom"}, provider_dependencies={
                "handler": lambda *a: {"answer": "grounded answer", "evidence": []}}).ask("status")
        check("a forced internal Guard exception never propagates out of ask()", blocked.ok is True)
        check("the adapter falls back to the bounded blocked answer", blocked.answer == BLOCKED_ANSWER_TEXT)
        check("grounding.verdict is 'blocked' on the forced-failure fallback",
              blocked.grounding is not None and blocked.grounding.verdict == "blocked")
        check("evidence is emptied on the blocked fallback", blocked.evidence == [])

        print("\n=== 18. Legacy compatibility: pre-Phase-15 construction and the ungrounded direct-provider path ===")
        legacy = ProviderResponse(ok=True, provider="legacy", answer="hi", model="m", evidence=[{"a": 1}])
        check("grounding defaults to None for code that never mentions it", legacy.grounding is None)
        check("every pre-existing field still behaves identically",
              legacy.ok is True and legacy.provider == "legacy" and legacy.answer == "hi"
              and legacy.model == "m" and legacy.evidence == [{"a": 1}] and legacy.error is None)
        legacy_dict = legacy.as_dict()
        check("as_dict() still returns every original key, additively including grounding",
              set(legacy_dict) == {"ok", "provider", "model", "answer", "evidence", "error", "grounding"}
              and legacy_dict["grounding"] is None)

        direct_custom = CustomProvider(ProviderConfig(provider="custom"),
                                        handler=lambda *a: {"answer": "ungrounded direct answer", "evidence": []})
        direct_result = direct_custom.ask("status", EvidenceBroker())
        check("calling a provider directly (bypassing the adapter) is intentionally left ungrounded",
              direct_result.ok and direct_result.answer == "ungrounded direct answer"
              and direct_result.grounding is None)
    check("evidence file is byte-identical after tests 16-18", evidence_path_other.read_bytes() == before_other)


print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    raise SystemExit(1)
print("ALL GROUNDING GUARD CHECKS PASSED")
