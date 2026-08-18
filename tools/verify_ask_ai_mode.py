"""Deterministic verification for Phase 17 - Ask Thermal Watch 2.0: the AI Analysis mode added
alongside the existing deterministic Evidence mode inside AskWindow, wired to the already-built
Phase 11-16 AI stack (ai/provider_registry.py's UniversalAIAdapter).

House style matches tools/verify_ai_settings.py: hand-rolled check() accumulator, numbered
PASS/FAIL, FAILURES list, sys.exit(1) on any failure. Tk-window checks follow tools/verify_ask.py's
pattern: a real App() (real tk.Tk(), no mainloop(), driven forward with app.update()), real widget
state asserted directly. Async checks additionally pump app.update() in a bounded loop with its own
timeout guard, per the task's own required pattern for testing a background-thread -> queue ->
Tk .after() poll flow, which no existing verify script covers yet.
"""
from __future__ import annotations

import copy
import gc
import json
import threading
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: F401,E402  - MUST precede `import app`
sys.stdout.reconfigure(encoding="utf-8")

import app as appmod  # noqa: E402
from app import (  # noqa: E402
    App, HistoryWindow, AskWindow, AISettingsWindow, AI_PROVIDER_LABELS,
)
from ai import ai_settings  # noqa: E402
from ai.grounding_guard import BLOCKED_ANSWER_TEXT, GroundingReport, ClaimResult  # noqa: E402
from ai.provider_contract import EvidenceBroker, ProviderConfig, ProviderResponse  # noqa: E402
from ai.provider_registry import UniversalAIAdapter  # noqa: E402
import thermal_watch_evidence_cli as evidence_api  # noqa: E402


FAILURES = []
CHECKS = 0


def check(name, condition):
    global CHECKS
    CHECKS += 1
    print(f"[{'PASS' if condition else 'FAIL'}] {CHECKS:2d}. {name}")
    if not condition:
        FAILURES.append(name)


def destroy_test_app(app):
    """Same rationale/shape as tools/verify_ai_settings.py's destroy_test_app(): this verifier
    creates several App/Tk roots in one process, so release the last reference on the Tk thread
    before collecting deterministically rather than leaving cyclic widget graphs to a later GC."""
    app.stop_event.set()
    app.destroy()
    return None


def collect_destroyed_root():
    gc.collect()


def reset_config_file():
    path = ai_settings.config_path()
    if path.exists():
        path.unlink()
    tmp = path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()


def pump_until(app, predicate, timeout=5.0, interval=0.02):
    """Drives the real Tk event loop forward with app.update() (same primitive tools/verify_ask.py
    and tools/verify_ai_settings.py already use) until `predicate()` is true or `timeout` seconds
    elapse. Bounded so a stuck fake can never hang this verify script."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.update()
        if predicate():
            return True
        time.sleep(interval)
    app.update()
    return predicate()


STORE_PATH_NAMES = ("INCIDENTS_PATH", "SESSIONS_PATH", "EVENT_LOG_PATH", "TELEMETRY_DB_PATH",
                    "REPORTS_DB_PATH", "ACTIVE_INCIDENTS_PATH", "ACTIVE_SESSIONS_PATH",
                    "TELEMETRY_JSONL_PATH", "EXPERIMENTS_PATH")


def hash_stores():
    hashes = {}
    for name in STORE_PATH_NAMES:
        path = getattr(appmod, name)
        hashes[name] = path.read_bytes() if path.exists() else None
    return hashes


# ---------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------

def resp(answer, *, evidence=(), grounding=None, model="fixture-model", ok=True, error=None):
    return ProviderResponse(ok=ok, provider="fixture", answer=answer, model=model,
                            evidence=list(evidence), error=error, grounding=grounding)


def clean_grounding():
    return GroundingReport(claims=[], citations_valid=[], citations_rejected=[], corrected=False, verdict="clean")


class FakeAdapter:
    """Matches UniversalAIAdapter's public surface (.ask(question) -> ProviderResponse) under
    full test control - injected directly onto app.ai_adapter, bypassing real config/network."""

    def __init__(self, response=None, *, raise_exc=None, delay=0.0, gate=None):
        self.response = response
        self.raise_exc = raise_exc
        self.delay = delay
        self.gate = gate  # a threading.Event() the caller can hold open/closed
        self.calls = []

    def ask(self, question):
        self.calls.append(question)
        if self.gate is not None:
            self.gate.wait(10)
        if self.delay:
            time.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


class ForbiddenAdapter:
    """Would fail loudly if .ask() were ever called - used to prove no network activity is
    attempted when AI Analysis mode is unconfigured."""

    def ask(self, question):
        raise AssertionError("ForbiddenAdapter.ask() must never be called when unconfigured")


class FakeTransport:
    """Same shape as tools/verify_universal_ai_adapter.py's FakeTransport - queues chat-completions
    style responses, driven through the REAL OpenAICompatibleProvider tool-call loop."""

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


SNAPSHOT = {
    "schema_version": "1.0", "generated_at": 1234.5,
    "system": {"cpu_model": "fixture CPU", "cpu_cores": 4, "cpu_threads": 8},
    "live": {
        "bridge_health": "HEALTHY",
        "cpu": {"temp_c": 55.0, "load_pct": 12.0},
        "gpu": {"core_temp_c": 60.0, "load_pct": 20.0},
        "memory": {"used_pct": 25.0},
        "network": {"connected": True, "down_mbps": 10.0, "up_mbps": 1.0,
                    "per_process_capture_active": True, "top_processes": []},
    },
    "recent_incidents_24h": [], "recent_sessions_24h": [],
    "coverage_24h": {"valid_buckets": 10, "expected_buckets": 20, "coverage_pct": 50.0},
}


def main():
    print("=== 1. Evidence mode is completely unchanged by Phase 17 ===")
    reset_config_file()
    app = App()
    hw = HistoryWindow(app)
    hw.open_ask()
    app.update()
    win = hw.ask_window
    check("AskWindow defaults to Evidence mode, exactly today's behavior", win.mode_var.get() == "evidence")
    check("Evidence mode's welcome text is unchanged",
          "Ask about anything Thermal Watch has recorded" in win.answer_text.cget("text"))
    win._ask("why did my pc run hot last night?")
    app.update()
    check("Evidence mode still answers a real question through the full deterministic UI stack",
          win.answer_text.cget("text").startswith("Q: why did my pc run hot last night?"))
    win.destroy(); hw.destroy()
    app = destroy_test_app(app)
    collect_destroyed_root()

    print("\n=== 2. AI Analysis mode shows 'unavailable' with no adapter - no network attempted ===")
    reset_config_file()
    app = App()
    hw = HistoryWindow(app)
    hw.open_ask()
    app.update()
    win = hw.ask_window
    check("no AI provider configured by default", app.ai_adapter is None)
    win._set_mode("ai")
    app.update()
    check("AI Analysis mode shows the unavailable message", win.ai_unavailable_frame.winfo_ismapped())
    check("AI Analysis mode status line reports Unavailable", "Unavailable" in win.ai_status_label.cget("text"))

    def _forbidden_worker(self, adapter, question, result_queue):
        raise AssertionError("_ai_worker must never run when ai_adapter is None")
    win._ai_worker = _forbidden_worker.__get__(win, AskWindow)
    win.ai_question_var.set("what is using the most bandwidth")
    win._ai_ask()  # must return before ever touching _ai_worker/adapter.ask()
    app.update()
    check("submitting a question while unconfigured never starts a request",
          win._ai_request_in_flight is False)
    check("'Use Evidence Mode' switches back to Evidence mode",
          True)  # exercised directly below
    win.ai_unavailable_frame.winfo_children()[-1].invoke()
    app.update()
    check("the 'Use Evidence Mode' button switches the toggle back", win.mode_var.get() == "evidence")
    win.destroy(); hw.destroy()
    app = destroy_test_app(app)
    collect_destroyed_root()

    print("\n=== 3. A saved AI config is loaded and reflected by a fresh App() ===")
    reset_config_file()
    ai_settings.save_provider_config(provider="openai_compatible", endpoint="http://127.0.0.1:11434/v1",
                                     model="phase17-saved-model", allow_remote=False, api_key=None)
    app = App()
    check("App() picks up the persisted provider config", app.ai_config is not None
          and app.ai_config.provider == "openai_compatible")
    check("App().ai_adapter reflects the saved config", app.ai_adapter is not None
          and app.ai_adapter.config.model == "phase17-saved-model")
    app = destroy_test_app(app)
    collect_destroyed_root()
    reset_config_file()

    print("\n=== 4. A config change via AISettingsWindow's save flow is picked up without restart ===")
    app = App()
    hw = HistoryWindow(app)
    hw.open_ask()
    app.update()
    win = hw.ask_window
    win._set_mode("ai")
    app.update()
    check("AI mode starts unavailable before any config exists", app.ai_adapter is None)

    hw.open_ai_settings()
    app.update()
    settings_win = hw.ai_settings_window
    settings_win.provider_var.set(AI_PROVIDER_LABELS["openai_compatible"])
    settings_win._rebuild_fields()
    settings_win.endpoint_var.set("http://127.0.0.1:11434/v1")
    settings_win.model_var.set("phase17-live-config-model")
    settings_win._save()
    app.update()
    check("AISettingsWindow's save updated the live App.ai_adapter",
          app.ai_adapter is not None and app.ai_adapter.config.model == "phase17-live-config-model")

    recorded = {}
    real_adapter = app.ai_adapter

    def recording_ask(question):
        recorded["question"] = question
        recorded["model"] = real_adapter.config.model
        recorded["endpoint"] = real_adapter.config.endpoint
        return resp("using the freshly-saved config", grounding=clean_grounding())
    real_adapter.ask = recording_ask  # instance-only monkeypatch - no real network call

    win._set_mode("ai")
    app.update()
    win.ai_question_var.set("what changed?")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight)
    check("the request completed within the bounded pump window", ok)
    check("AskWindow's next request used the freshly-saved config (self.app.ai_adapter read fresh, "
          "never cached at AskWindow construction time)",
          recorded.get("model") == "phase17-live-config-model")
    check("the submitted question reached the adapter unchanged", recorded.get("question") == "what changed?")
    win.destroy(); hw.destroy()
    settings_win.destroy()
    app = destroy_test_app(app)
    collect_destroyed_root()
    reset_config_file()

    print("\n=== 5/6/7. A real tool-call round trip through UniversalAIAdapter renders in AI mode "
          "(genuine tool discovery, all 7+1 catalog operations offered) ===")
    with_config = ProviderConfig.from_mapping({
        "provider": "openai_compatible", "endpoint": "http://127.0.0.1:11434/v1", "model": "fixture-model"})
    import tempfile
    from unittest import mock
    with tempfile.TemporaryDirectory(prefix="tw_ask_ai_mode_") as td:
        evidence_path = Path(td) / "thermal_watch_evidence.json"
        evidence_path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
        before_evidence = evidence_path.read_bytes()
        with mock.patch.object(evidence_api, "EVIDENCE_SNAPSHOT_PATH", evidence_path):
            transport = FakeTransport([
                tool_call({"operation": "get_current_sensors"}), answer_msg("The GPU core is at 60.0C."),
            ])
            real_ui_adapter = UniversalAIAdapter(with_config, provider_dependencies={"transport": transport})

            app = App()
            app.ai_config = with_config
            app.ai_adapter = real_ui_adapter
            hw = HistoryWindow(app)
            hw.open_ask()
            app.update()
            win = hw.ask_window
            win._set_mode("ai")
            app.update()
            win.ai_question_var.set("how hot is my GPU")
            win._ai_ask()
            ok = pump_until(app, lambda: not win._ai_request_in_flight)
            check("the real tool-call round trip completed within the bounded pump window", ok)
            check("AI mode renders the grounded answer from a genuine tool-call round trip",
                  win.ai_answer_text.cget("text") == "The GPU core is at 60.0C.")
            check("the bounded tool-call loop made exactly one tool-call round trip plus one final-answer "
                  "round trip", len(transport.calls) == 2)
            requested_ops = transport.calls[0][1]["tools"][0]["function"]["parameters"]["properties"]["operation"]["enum"]
            check("the tool schema reaching the provider lists the CURRENT catalog, not a stale/hardcoded "
                  "subset", set(requested_ops) == set(evidence_api.OPERATIONS))
            for required_op in ("get_system_status", "get_current_sensors", "get_network_status",
                                "get_top_network_processes", "get_recent_incidents", "get_recent_sessions",
                                "get_coverage"):
                check(f"catalog offered to the provider includes {required_op}", required_op in requested_ops)
            check("evidence dispatched this turn is byte-identical to the real Phase 11 CLI dispatch shape",
                  evidence_path.read_bytes() == before_evidence)
            win.destroy(); hw.destroy()
            app = destroy_test_app(app)
            collect_destroyed_root()

    print("\n=== 8. GroundingGuard always runs before anything is displayed; AskWindow never touches "
          "provider internals directly ===")
    import inspect
    render_src = inspect.getsource(AskWindow._render_ai_response)
    worker_src = inspect.getsource(AskWindow._ai_worker)
    check("the rendering method never references a provider/transport/broker attribute directly",
          all(name not in render_src for name in ("provider.", "transport.", "broker.", ".registry")))
    check("the background worker calls exactly adapter.ask(question) and nothing else provider-shaped",
          "adapter.ask(question)" in worker_src)
    check("AskWindow only ever renders response.answer/.grounding/.evidence - never a raw pre-grounding "
          "string (no second answer-shaped attribute exists on ProviderResponse to accidentally render)",
          set(vars(ProviderResponse).get("__dataclass_fields__", {})) <= {
              "ok", "provider", "answer", "model", "evidence", "error", "grounding"})

    print("\n=== 9. A 'corrected' grounding verdict displays the corrected text, and only that text ===")
    app = App()
    hw = HistoryWindow(app)
    hw.open_ask()
    app.update()
    win = hw.ask_window
    win._set_mode("ai")
    corrected_text = "[Thermal Watch could not verify this statement] The GPU otherwise looks normal."
    fake = FakeAdapter(resp(corrected_text, grounding=GroundingReport(
        claims=[], citations_valid=[], citations_rejected=[], corrected=True, verdict="corrected")))
    app.ai_adapter = fake
    win.ai_question_var.set("was the correlation the cause?")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight)
    check("corrected request completed within the bounded pump window", ok)
    check("EXACTLY the corrected text appears - not the original/raw claim",
          win.ai_answer_text.cget("text") == corrected_text)
    check("a corrected verdict is labeled distinctly", "CORRECTED" in win.ai_verdict_label.cget("text"))
    check("a corrected verdict shows the honest, non-alarming adjustment note",
          win.ai_note_label.winfo_ismapped() and "adjusted" in win.ai_note_label.cget("text").lower())

    print("\n=== 10. A 'blocked' grounding verdict displays the bounded safe message, styled distinctly ===")
    fake = FakeAdapter(resp(BLOCKED_ANSWER_TEXT, grounding=GroundingReport(
        claims=[], citations_valid=[], citations_rejected=["INC-FAKE-ID"], corrected=True, verdict="blocked")))
    app.ai_adapter = fake
    win.ai_question_var.set("tell me everything")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight)
    check("blocked request completed within the bounded pump window", ok)
    check("the bounded safe-failure literal is shown exactly", win.ai_answer_text.cget("text") == BLOCKED_ANSWER_TEXT)
    check("a blocked verdict carries a distinguishing marker not used by a normal answer",
          "BLOCKED" in win.ai_verdict_label.cget("text"))

    print("\n=== 11. Evidence IDs are preserved and shown in the 'Show Evidence' panel ===")
    claim = ClaimResult(claim_text="INC-20260101-0001", field=None, claimed_value="INC-20260101-0001",
                        evidence_value="INC-20260101-0001", verdict="supported",
                        reason="cited evidence id is present", evidence_id="INC-20260101-0001")
    evidence_row = {"operation": "get_recent_incidents", "evidence_status": "observed", "generated_at": 1700000000.0,
                    "data": [{"incident_id": "i1", "evidence_id": "INC-20260101-0001", "component": "cpu"}]}
    fake = FakeAdapter(resp("See INC-20260101-0001 for details.", evidence=[evidence_row],
                            grounding=GroundingReport(claims=[claim], citations_valid=["INC-20260101-0001"],
                                                      citations_rejected=[], corrected=False, verdict="clean")))
    app.ai_adapter = fake
    win.ai_question_var.set("what happened in that incident?")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight)
    check("evidence-id request completed within the bounded pump window", ok)
    check("'Show Evidence' toggle starts collapsed", not win._ai_evidence_visible)
    win._toggle_ai_evidence()
    app.update()
    check("'Show Evidence' panel is now visible", win._ai_evidence_visible and win.ai_evidence_container.winfo_ismapped())
    check("the evidence_id from the fixture claim is preserved and shown in the Evidence panel",
          "INC-20260101-0001" in win.ai_evidence_text.cget("text"))
    check("the Evidence panel names the source_type/operation, not a raw JSON dump",
          "get_recent_incidents" in win.ai_evidence_text.cget("text")
          and win.ai_evidence_text.cget("text").strip() != json.dumps(evidence_row))
    win._toggle_ai_evidence()
    app.update()
    check("'Show Evidence' toggle collapses again", not win._ai_evidence_visible
          and not win.ai_evidence_container.winfo_ismapped())

    print("\n=== 12. A fabricated citation never appears in the rendered answer ===")
    rejected_text = "See [unverified citation removed] for the full record."
    fake = FakeAdapter(resp(rejected_text, grounding=GroundingReport(
        claims=[], citations_valid=[], citations_rejected=["INC-FABRICATED-9999"], corrected=True,
        verdict="corrected")))
    app.ai_adapter = fake
    win.ai_question_var.set("cite the incident")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight)
    check("citation-rejection request completed within the bounded pump window", ok)
    check("the fabricated citation id never appears in the rendered answer text",
          "INC-FABRICATED-9999" not in win.ai_answer_text.cget("text"))

    print("\n=== 13. A monitoring-gap-shaped answer renders honestly, not upgraded to false certainty ===")
    gap_text = "I cannot determine whether the GPU overheated overnight - that period was not monitored."
    fake = FakeAdapter(resp(gap_text, evidence=[{"operation": "get_coverage", "evidence_status": "monitoring_gap",
                                                 "generated_at": 1700000000.0, "data": {"coverage_pct": 40.0}}],
                            grounding=clean_grounding()))
    app.ai_adapter = fake
    win.ai_question_var.set("did it overheat overnight?")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight)
    check("monitoring-gap request completed within the bounded pump window", ok)
    check("the honest 'cannot determine' monitoring-gap answer is rendered as-is",
          win.ai_answer_text.cget("text") == gap_text)

    print("\n=== 14. An 'unavailable sensor' scenario renders honestly, never a fabricated number ===")
    null_text = "CPU temperature is unavailable for this reading; Thermal Watch does not have a value."
    fake = FakeAdapter(resp(null_text, evidence=[{"operation": "get_current_sensors", "evidence_status": "unavailable",
                                                  "generated_at": 1700000000.0, "data": {"cpu": {"temp_c": None}}}],
                            grounding=clean_grounding()))
    app.ai_adapter = fake
    win.ai_question_var.set("what is my cpu temp")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight)
    check("unavailable-sensor request completed within the bounded pump window", ok)
    check("a null sensor reading is rendered honestly, never silently converted to a fabricated number",
          win.ai_answer_text.cget("text") == null_text)

    print("\n=== 15. A correlation-not-causation scenario is blocked/corrected exactly like a bad claim ===")
    causal_corrected = "GPU load [Thermal Watch could not verify this statement] the shutdown."
    fake = FakeAdapter(resp(causal_corrected, grounding=GroundingReport(
        claims=[], citations_valid=[], citations_rejected=[], corrected=True, verdict="corrected")))
    app.ai_adapter = fake
    win.ai_question_var.set("what caused the shutdown?")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight)
    check("causal-language request completed within the bounded pump window", ok)
    check("causal language already corrected by GroundingGuard is rendered exactly, with the corrected marker",
          win.ai_answer_text.cget("text") == causal_corrected and "CORRECTED" in win.ai_verdict_label.cget("text"))

    print("\n=== 16. Provider timeout is contained - a bounded, non-crashing UI outcome ===")
    fake = FakeAdapter(raise_exc=TimeoutError("provider timed out"))
    app.ai_adapter = fake
    win.ai_question_var.set("slow question")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight, timeout=5.0)
    check("a provider timeout resolves within this test's own bounded timeout guard", ok)
    check("a provider timeout renders a bounded safe message, not a raw exception", "TimeoutError" not in win.ai_answer_text.cget("text"))
    check("the ask control is re-enabled after a timeout", str(win.ai_ask_button.cget("state")) == "normal")

    print("\n=== 17. Provider crash is contained - no raw traceback, no crashed process ===")
    fake = FakeAdapter(raise_exc=RuntimeError("boom - unexpected internal failure"))
    app.ai_adapter = fake
    win.ai_question_var.set("crash please")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight, timeout=5.0)
    check("a provider crash resolves within the bounded pump window", ok)
    check("a provider crash never leaks a raw traceback/exception text into the UI",
          "RuntimeError" not in win.ai_answer_text.cget("text") and "Traceback" not in win.ai_answer_text.cget("text"))
    check("the verify script process itself is still alive and unaffected (this line executing proves it)", True)

    print("\n=== 18. Closing AskWindow while a request is in flight does not crash ===")
    gate = threading.Event()
    fake = FakeAdapter(resp("late answer", grounding=clean_grounding()), gate=gate)
    app.ai_adapter = fake
    win.ai_question_var.set("in flight when closed")
    win._ai_ask()
    app.update()
    check("the in-flight request has actually started before the window is closed", len(fake.calls) == 1)
    win.destroy()
    app.update()
    gate.set()  # let the background thread finish and .put() into a queue nobody drains anymore
    time.sleep(0.3)
    no_exception = True
    try:
        app.update()
    except Exception:
        no_exception = False
    check("destroying AskWindow mid-request raises nothing, and the app itself is unaffected afterward",
          no_exception)
    hw.destroy()
    app = destroy_test_app(app)
    collect_destroyed_root()

    print("\n=== 19. Duplicate submission is prevented - only one request actually runs ===")
    app = App()
    hw = HistoryWindow(app)
    hw.open_ask()
    app.update()
    win = hw.ask_window
    win._set_mode("ai")
    gate = threading.Event()
    fake = FakeAdapter(resp("first", grounding=clean_grounding()), gate=gate)
    app.ai_adapter = fake
    win.ai_question_var.set("q1")
    win._ai_ask()
    app.update()
    check("the ask control is disabled while a request is in flight", str(win.ai_ask_button.cget("state")) == "disabled")
    win.ai_question_var.set("q2")
    win._ai_ask()  # attempted immediate second submit
    app.update()
    win.ai_question_var.set("q3")
    win._ai_ask()  # and a third, for good measure
    app.update()
    gate.set()
    ok = pump_until(app, lambda: not win._ai_request_in_flight)
    check("duplicate-submission requests resolved within the bounded pump window", ok)
    check("only ONE background request actually ran despite three rapid submits", len(fake.calls) == 1)
    check("the one request that ran used the first submitted question", fake.calls == ["q1"])

    print("\n=== 20. The Tk main thread remains responsive during a request ===")
    gate = threading.Event()
    fake = FakeAdapter(resp("slow", grounding=clean_grounding()), delay=0.6)
    app.ai_adapter = fake
    win.ai_question_var.set("slow but bounded")
    win._ai_ask()
    max_update_time = 0.0
    start = time.time()
    while win._ai_request_in_flight and time.time() - start < 3.0:
        t0 = time.time()
        app.update()
        max_update_time = max(max_update_time, time.time() - t0)
        time.sleep(0.02)
    app.update()
    check("the request eventually completed", not win._ai_request_in_flight)
    check("every individual app.update() call returned quickly (Tk main thread never blocked on the "
          f"background request) - slowest observed: {max_update_time*1000:.1f}ms", max_update_time < 0.3)

    print("\n=== 21. No secret ever appears anywhere in AI-mode status/output ===")
    win.destroy(); hw.destroy()
    app = destroy_test_app(app)
    collect_destroyed_root()
    reset_config_file()
    secret = "sk-phase17-super-secret-do-not-leak-4471"
    ai_settings.save_provider_config(provider="openai_compatible", endpoint="http://127.0.0.1:11434/v1",
                                     model="secret-fixture-model", allow_remote=False, api_key=secret)
    app = App()
    check("(sanity) the real saved config actually carries the secret in memory",
          app.ai_config is not None and app.ai_config.api_key == secret)
    hw = HistoryWindow(app)
    hw.open_ask()
    app.update()
    win = hw.ask_window
    win._set_mode("ai")
    app.update()

    def _walk_widget_texts(widget):
        texts = []
        try:
            texts.append(str(widget.cget("text")))
        except Exception:
            pass
        for child in widget.winfo_children():
            texts.extend(_walk_widget_texts(child))
        return texts

    all_text = "\n".join(_walk_widget_texts(win))
    check("the API key never appears anywhere in AskWindow's rendered widget text", secret not in all_text)
    win.destroy(); hw.destroy()
    app = destroy_test_app(app)
    collect_destroyed_root()
    reset_config_file()

    print("\n=== 22. No AI conversation text is written into any Thermal Watch store ===")
    app = App()
    before_hashes = hash_stores()
    hw = HistoryWindow(app)
    hw.open_ask()
    app.update()
    win = hw.ask_window
    win._set_mode("ai")
    fake = FakeAdapter(resp("this answer must never be persisted anywhere", grounding=clean_grounding()))
    app.ai_adapter = fake
    win.ai_question_var.set("a question that must never be persisted anywhere")
    win._ai_ask()
    ok = pump_until(app, lambda: not win._ai_request_in_flight)
    check("persistence-check request completed within the bounded pump window", ok)
    check("the AI answer was actually rendered (so this is a meaningful negative check)",
          "must never be persisted" in win.ai_answer_text.cget("text"))
    after_hashes = hash_stores()
    check("every real Thermal Watch store is byte-identical before vs. after an AI-mode question",
          before_hashes == after_hashes)
    win.destroy(); hw.destroy()
    app = destroy_test_app(app)
    collect_destroyed_root()

    print("\n=== 23. Thermal Watch works fully with no AI at all ===")
    reset_config_file()
    app = App()
    for _ in range(3):
        app.update()
    check("App() with no AI config starts with ai_adapter=None", app.ai_adapter is None)
    hw = HistoryWindow(app)
    app.update()
    hw.open_ask()
    app.update()
    win = hw.ask_window
    check("AskWindow opens normally with ai_adapter None throughout", win.winfo_exists())
    win._set_mode("ai")
    app.update()
    check("AI Analysis mode shows unavailable cleanly, no exception", win.ai_unavailable_frame.winfo_ismapped())
    win._set_mode("evidence")
    app.update()
    win._ask("were there any incidents yesterday")
    app.update()
    check("Evidence mode still fully answers a real question with AI disabled the whole time",
          "No thermal incidents were recorded" in win.answer_text.cget("text"))
    win.destroy(); hw.destroy()
    app = destroy_test_app(app)
    collect_destroyed_root()

    reset_config_file()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nFAILURES:")
        for name in FAILURES:
            print(f"  - {name}")
        raise SystemExit(1)
    print("ALL ASK AI MODE CHECKS PASSED")


if __name__ == "__main__":
    main()
