"""Deterministic verification for Phase 16 - AI Integration Settings: config schema/validation
and persistence (ai/ai_settings.py), the DPAPI-backed secret wrapper (ai/secret_store.py), the
tiered connection test, App's one-time best-effort config load/reload, and the AISettingsWindow
UI (opened from HistoryWindow, following the same singleton-Toplevel pattern as AskWindow).

House style matches tools/verify_universal_ai_adapter.py and tools/verify_grounding_guard.py:
hand-rolled check() accumulator, numbered PASS/FAIL, FAILURES list, sys.exit(1) on any failure.
The Tk-window checks follow tools/verify_ask.py's pattern: a real App() (real tk.Tk(), no
mainloop(), driven forward with app.update()), real widget state asserted directly.
"""
from __future__ import annotations

import ast
import base64
import gc
import inspect
import json
import urllib.error
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: F401,E402  - MUST precede `import app`
sys.stdout.reconfigure(encoding="utf-8")

import app as appmod  # noqa: E402
from app import App, HistoryWindow, AISettingsWindow, AI_PROVIDER_LABELS  # noqa: E402
from ai import ai_settings, secret_store  # noqa: E402
from ai.provider_contract import ProviderConfig, ProviderContractError  # noqa: E402


def destroy_test_app(app):
    """Destroy one test-only Tk root and finalize its widget cycles on Tk's owner thread. This
    verifier creates several App/Tk roots in one process (unlike production) - same helper and
    same rationale as tools/verify_incident_durability.py's destroy_test_app(): merely destroying
    a root leaves cyclic widget graphs eligible for later collection, and if the NEXT App's worker
    happens to trigger cyclic GC, Tcl finalizers then run on that worker thread and abort with
    Tcl_AsyncDelete. Returning None lets each caller release its last root reference before
    collecting deterministically on the main/Tk thread. No event-loop pumping right at the
    destroy boundary - update() immediately before destroy() is exactly the pattern documented (v1.1
    Phase 4 fix note) to widen this race."""
    app.stop_event.set()
    app.destroy()
    return None


def collect_destroyed_root():
    gc.collect()


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
    """Same shape as tools/verify_universal_ai_adapter.py's FakeTransport - queues responses or
    raises `error` directly (unwrapped), exactly mirroring how a test double differs from the
    real _UrllibTransport (which wraps OSError/URLError into ProviderContractError)."""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def post_json(self, url, payload, headers, timeout):
        self.calls.append((url, payload, dict(headers), timeout))
        if self.error:
            raise self.error
        return self.responses.pop(0)


def answer(text="ready"):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def reset_config_file():
    path = ai_settings.config_path()
    if path.exists():
        path.unlink()
    tmp = path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()


LOOPBACK_CONFIG = dict(provider="openai_compatible", endpoint="http://127.0.0.1:11434/v1",
                        model="fixture-model", allow_remote=False, api_key=None)


def main():
    print("=== 1. Default state: no config file present is valid - 'no AI configured', no crash ===")
    reset_config_file()
    check("no file on disk", not ai_settings.config_path().exists())
    check("load_provider_config() returns None with no file", ai_settings.load_provider_config() is None)
    app = App()
    check("App() with no AI config file starts with ai_config=None", app.ai_config is None)
    check("App() with no AI config file starts with ai_adapter=None", app.ai_adapter is None)
    app = destroy_test_app(app)
    collect_destroyed_root()

    print("\n=== 2. 'none'/disabled provider: adapter is never constructed, no error ===")
    reset_config_file()
    ai_settings.disable_provider()
    check("disabled sentinel written to disk", ai_settings.config_path().exists())
    check("disabled config loads as None (no adapter)", ai_settings.load_provider_config() is None)
    on_disk = json.loads(ai_settings.config_path().read_text(encoding="utf-8"))
    check("disabled payload uses the 'none' sentinel, not a 4th registered provider",
          on_disk["provider"] == "none" and on_disk["provider"] not in ("nox", "openai_compatible", "custom"))

    print("\n=== 3. Nox config validates via ProviderConfig.from_mapping - no endpoint/model required ===")
    nox_src = inspect.getsource(sys.modules["ai.providers.nox"])
    check("confirmed from the real Nox provider source: it never reads config.endpoint",
          "config.endpoint" not in nox_src)
    nox_config = ProviderConfig.from_mapping({"provider": "nox"})
    check("bare {'provider': 'nox'} validates with no endpoint/model", nox_config.provider == "nox")

    print("\n=== 4. OpenAI-compatible config with a loopback endpoint validates ===")
    loopback = ProviderConfig.from_mapping({"provider": "openai_compatible",
                                            "endpoint": "http://127.0.0.1:11434/v1", "model": "m"})
    check("loopback endpoint validates", loopback.endpoint == "http://127.0.0.1:11434/v1")

    print("\n=== 5. Malformed endpoints are rejected with invalid_endpoint ===")
    for bad in ("file:///tmp/model", "not a url", "ftp://127.0.0.1/x", ""):
        expect_error(f"malformed endpoint rejected: {bad!r}", "invalid_endpoint",
                    lambda bad=bad: ProviderConfig.from_mapping({"provider": "openai_compatible",
                                                                 "endpoint": bad, "model": "m"}))

    print("\n=== 6. Endpoint with embedded credentials is rejected ===")
    expect_error("credentials-in-URL rejected", "invalid_endpoint", lambda: ProviderConfig.from_mapping({
        "provider": "openai_compatible", "endpoint": "http://user:pass@127.0.0.1:1234/v1", "model": "m"}))

    print("\n=== 7. Remote endpoint without allow_remote is blocked ===")
    expect_error("remote endpoint blocked by default", "remote_endpoint_disabled", lambda: ProviderConfig.from_mapping({
        "provider": "openai_compatible", "endpoint": "https://example.com/v1", "model": "m"}))

    print("\n=== 8. Same remote endpoint IS allowed once allow_remote=True ===")
    remote_allowed = ProviderConfig.from_mapping({"provider": "openai_compatible",
                                                  "endpoint": "https://example.com/v1", "model": "m",
                                                  "allow_remote": True})
    check("remote endpoint allowed with allow_remote=True", remote_allowed.allow_remote is True)

    print("\n=== 9. API key never appears anywhere in the serialized config JSON ===")
    reset_config_file()
    secret = "sk-real-plaintext-secret-value-777"
    saved = ai_settings.save_provider_config(provider="openai_compatible",
                                             endpoint="http://127.0.0.1:11434/v1", model="m", allow_remote=False,
                                             api_key=secret)
    raw_bytes = ai_settings.config_path().read_bytes()
    check("plaintext API key is ABSENT from the written config file's bytes", secret.encode() not in raw_bytes)
    on_disk = json.loads(raw_bytes.decode("utf-8"))
    check("credential_ref is present", on_disk.get("credential_ref"))
    check("credential_ref ciphertext differs from the plaintext key", on_disk["credential_ref"] != secret)
    check("save_provider_config() returns a live ProviderConfig with the real key in memory only",
          saved.api_key == secret)

    print("\n=== 10. API key never appears in logs/status/export (public_dict + static ban-list) ===")
    check("public_dict() never includes the raw key",
          secret not in json.dumps(saved.public_dict()) and "api_key_configured" in saved.public_dict())
    status = ai_settings.status_from_config(saved)
    check("AISettingsStatus (the settings-window projection) never carries the raw key",
          secret not in json.dumps(vars(status)))
    # Static-analysis ban-list, same spirit as tools/verify_ask.py's ast.walk pass: walk every
    # print()/logging.*() call in the Phase 16 modules and assert none of them references any
    # api-key/credential-shaped identifier as an argument.
    banned_name_fragments = ("api_key", "apikey", "credential", "secret", "plaintext")
    scanned_sources = {
        "ai/ai_settings.py": Path(__file__).resolve().parent.parent / "ai" / "ai_settings.py",
        "ai/secret_store.py": Path(__file__).resolve().parent.parent / "ai" / "secret_store.py",
    }
    offending = []
    for label, path in scanned_sources.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_log_call = (isinstance(func, ast.Name) and func.id == "print") or (
                isinstance(func, ast.Attribute) and func.attr in ("print", "debug", "info", "warning", "error", "log"))
            if not is_log_call:
                continue
            for arg in ast.walk(node):
                if isinstance(arg, ast.Name) and any(frag in arg.id.lower() for frag in banned_name_fragments):
                    offending.append(f"{label}: {ast.dump(node)}")
    check("no print()/log() call anywhere in ai_settings.py/secret_store.py references a secret-shaped variable",
          not offending)
    # AISettingsWindow's own source, extracted from app.py the same way verify_ask.py extracts
    # the Ask layer's source, gets the same ban-list pass.
    win_src = inspect.getsource(AISettingsWindow)
    win_tree = ast.parse(win_src)
    win_offending = []
    for node in ast.walk(win_tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_log_call = (isinstance(func, ast.Name) and func.id == "print") or (
            isinstance(func, ast.Attribute) and func.attr in ("print", "debug", "info", "warning", "error", "log"))
        if not is_log_call:
            continue
        for arg in ast.walk(node):
            if isinstance(arg, ast.Name) and any(frag in arg.id.lower() for frag in banned_name_fragments):
                win_offending.append(ast.dump(node))
    check("AISettingsWindow itself has no print()/log() call referencing a secret-shaped variable",
          not win_offending)

    print("\n=== 11. Config persists across restart, secret round-trips via DPAPI ===")
    reloaded = ai_settings.load_provider_config()
    check("provider persists", reloaded.provider == "openai_compatible")
    check("endpoint persists", reloaded.endpoint == "http://127.0.0.1:11434/v1")
    check("model persists", reloaded.model == "m")
    check("allow_remote persists", reloaded.allow_remote is False)
    check("the API key round-trips byte-for-byte through DPAPI across a simulated restart",
          reloaded.api_key == secret)
    # Simulate a real restart: construct a fresh App() pointed at the same sandboxed DATA_DIR.
    app2 = App()
    check("a fresh App() picks up the persisted (non-secret) provider/endpoint/model",
          app2.ai_config is not None and app2.ai_config.provider == "openai_compatible"
          and app2.ai_config.endpoint == "http://127.0.0.1:11434/v1" and app2.ai_config.model == "m")
    check("a fresh App() also recovers the DPAPI-protected secret", app2.ai_config.api_key == secret)
    app2 = destroy_test_app(app2)
    collect_destroyed_root()

    print("\n=== 12. Invalid persisted config falls back safely to disabled/None - never crashes App ===")
    # Pure-logic pass first (no Tk root involved) over every malformed-config shape - this
    # verifier creates several real App()/Tk roots elsewhere in the same process (see
    # destroy_test_app()'s docstring on the pre-existing Tcl_AsyncDelete race that rapid App()
    # churn can trigger), so App() itself is only constructed for the two variants the task
    # explicitly calls out (hand-written garbage JSON, and a credential_ref that fails
    # unprotect()) rather than once per malformed shape.
    malformed_variants = {
        "garbage (non-JSON) bytes": lambda p: p.write_text("{not json at all", encoding="utf-8"),
        "valid JSON, wrong schema (missing keys)": lambda p: p.write_text(json.dumps({"provider": "openai_compatible"}), encoding="utf-8"),
        "valid JSON, unknown extra field": lambda p: p.write_text(json.dumps({
            **ai_settings.disabled_payload(), "provider": "openai_compatible", "endpoint": "http://127.0.0.1:1/v1",
            "model": "m", "extra_unexpected_field": True}), encoding="utf-8"),
        "wrong schema_version": lambda p: p.write_text(json.dumps({
            **ai_settings.disabled_payload(), "provider": "openai_compatible", "endpoint": "http://127.0.0.1:1/v1",
            "model": "m", "schema_version": 999}), encoding="utf-8"),
        "corrupted credential_ref (fails DPAPI unprotect)": lambda p: p.write_text(json.dumps({
            "schema_version": 1, "provider": "openai_compatible", "endpoint": "http://127.0.0.1:1/v1",
            "model": "m", "allow_remote": False,
            "credential_ref": base64.b64encode(b"not a real DPAPI blob").decode("ascii")}), encoding="utf-8"),
    }
    for label, write in malformed_variants.items():
        reset_config_file()
        write(ai_settings.config_path())
        check(f"malformed config ({label}) loads as None, not a crash", ai_settings.load_provider_config() is None)

    for label in ("garbage (non-JSON) bytes", "corrupted credential_ref (fails DPAPI unprotect)"):
        reset_config_file()
        malformed_variants[label](ai_settings.config_path())
        app3 = App()
        check(f"App() still constructs cleanly with malformed config on disk ({label})",
              app3.ai_config is None and app3.ai_adapter is None)
        app3 = destroy_test_app(app3)
        collect_destroyed_root()

    print("\n=== 13. Reset/disable works: old secret is gone, reload reflects the disabled state ===")
    reset_config_file()
    ai_settings.save_provider_config(provider="openai_compatible", endpoint="http://127.0.0.1:11434/v1",
                                     model="m", allow_remote=False, api_key="another-real-secret-999")
    check("a real config with a secret was actually written first",
          ai_settings.load_provider_config() is not None
          and ai_settings.load_provider_config().api_key == "another-real-secret-999")
    ai_settings.disable_provider()
    check("after reset, load returns None (disabled)", ai_settings.load_provider_config() is None)
    on_disk = json.loads(ai_settings.config_path().read_text(encoding="utf-8"))
    check("after reset, credential_ref is null on disk - the old secret is gone", on_disk["credential_ref"] is None)
    check("after reset, provider is the disabled sentinel", on_disk["provider"] == "none")

    print("\n=== 14. Connection test: success path (openai_compatible, FakeTransport) -> Connected ===")
    transport = FakeTransport(responses=[answer("ready")])
    result = ai_settings.test_connection(**LOOPBACK_CONFIG, provider_dependencies={"transport": transport})
    check("successful round trip classifies as Connected", result["status"] == "Connected")
    check("result is one of the 6 bounded statuses", result["status"] in ai_settings.CONNECTION_STATUSES)

    print("\n=== 15. Connection test: endpoint-unavailable path (connection-level error) ===")
    transport = FakeTransport(error=OSError("connection refused"))
    result = ai_settings.test_connection(**LOOPBACK_CONFIG, provider_dependencies={"transport": transport})
    check("a raw connection-level error classifies as Endpoint unavailable", result["status"] == "Endpoint unavailable")

    print("\n=== 16. Connection test: authentication-failed path (HTTP 401/403-shaped failure) ===")
    transport = FakeTransport(error=urllib.error.HTTPError("http://x/v1/chat/completions", 401, "Unauthorized", None, None))
    result = ai_settings.test_connection(**LOOPBACK_CONFIG, provider_dependencies={"transport": transport})
    check("an HTTP 401 classifies as Authentication failed", result["status"] == "Authentication failed")
    transport403 = FakeTransport(error=urllib.error.HTTPError("http://x/v1/chat/completions", 403, "Forbidden", None, None))
    result403 = ai_settings.test_connection(**LOOPBACK_CONFIG, provider_dependencies={"transport": transport403})
    check("an HTTP 403 also classifies as Authentication failed", result403["status"] == "Authentication failed")

    print("\n=== 17. Connection test: provider crash is contained (never a raw traceback) ===")
    transport = FakeTransport(error=RuntimeError("boom - unexpected internal failure"))
    result = ai_settings.test_connection(**LOOPBACK_CONFIG, provider_dependencies={"transport": transport})
    check("an unexpected exception still yields one of the 6 bounded statuses",
          result["status"] in ai_settings.CONNECTION_STATUSES)
    check("the crash is NOT re-raised out of test_connection()", result["status"] == "Provider unavailable")
    check("no exception object or traceback text leaks into the detail string",
          "RuntimeError" not in result["detail"] and "Traceback" not in result["detail"])
    invalid_result = ai_settings.test_connection(provider="openai_compatible", endpoint="not a url",
                                                 model="m", allow_remote=False, api_key=None)
    check("invalid configuration is caught before any network attempt", invalid_result["status"] == "Invalid configuration")
    nox_result = ai_settings.test_connection(provider="nox", endpoint=None, model=None, allow_remote=False, api_key=None)
    check("nox with no injected transport reports itself unavailable honestly (no live call attempted)",
          nox_result["status"] == "Provider unavailable")

    print("\n=== 18. Monitoring is unaffected by invalid AI configuration ===")
    reset_config_file()
    ai_settings.config_path().write_text("{totally not json", encoding="utf-8")
    app4 = App()
    for _ in range(5):
        app4.update()
    check("App() with garbage AI config still has its normal recurring subsystems wired",
          hasattr(app4, "poll") and hasattr(app4, "tick_uptime") and hasattr(app4, "check_bridge_health")
          and hasattr(app4, "worker_thread") and app4.worker_thread.is_alive())
    check("garbage AI config left ai_config/ai_adapter safely disabled", app4.ai_config is None and app4.ai_adapter is None)
    hw4 = HistoryWindow(app4)
    app4.update()
    check("HistoryWindow still opens normally alongside a garbage AI config", hw4.winfo_exists())
    hw4.destroy()
    app4 = destroy_test_app(app4)
    collect_destroyed_root()

    print("\n=== 19. Test isolation redirects AI config storage through THERMAL_WATCH_DATA_DIR ===")
    check("ai_settings.config_path() resolves inside the sandbox dir, exactly like every other store",
          ai_settings.config_path().parent == _verify_sandbox.SANDBOX_DIR.resolve())
    check("app.DATA_DIR (every other store) matches the very same sandbox dir",
          Path(appmod.DATA_DIR).resolve() == _verify_sandbox.SANDBOX_DIR.resolve())

    print("\n=== 20. Existing deterministic Ask behavior is unchanged by Phase 16 ===")
    with_mock_lines = appmod.answer_question("what is the capital of France")
    check("Ask still gives its honest 'I don't know how to answer that' refusal, untouched by Phase 16",
          "I don't know how to answer that" in "\n".join(with_mock_lines["lines"]))
    check("classify_question() is untouched", appmod.classify_question("how is my gpu doing") == "status")

    print("\n=== 21. DPAPI round-trip proof: protect()/unprotect() on real bytes, on this machine ===")
    original = b"a genuinely random secret payload \x00\x01\xfe\xff for round-trip proof"
    ciphertext = secret_store.protect(original)
    check("DPAPI ciphertext differs from the plaintext", ciphertext != original)
    recovered = secret_store.unprotect(ciphertext)
    check("DPAPI unprotect() recovers the exact original bytes", recovered == original)
    try:
        secret_store.unprotect(b"not a real DPAPI blob")
        check("a corrupted blob raises DPAPIError rather than returning garbage", False)
    except secret_store.DPAPIError:
        check("a corrupted blob raises DPAPIError rather than returning garbage", True)

    print("\n=== 22. Blank/empty API key input is stored as no-credential, never empty-string ciphertext ===")
    reset_config_file()
    blank_saved = ai_settings.save_provider_config(provider="openai_compatible",
                                                    endpoint="http://127.0.0.1:11434/v1", model="m",
                                                    allow_remote=False, api_key="")
    check("blank api_key input normalizes to None on the returned ProviderConfig", blank_saved.api_key is None)
    on_disk = json.loads(ai_settings.config_path().read_text(encoding="utf-8"))
    check("blank api_key input persists credential_ref as null, never an empty-string ciphertext",
          on_disk["credential_ref"] is None)
    check("(sanity) loading it back also reports no credential",
          ai_settings.status_from_config(ai_settings.load_provider_config()).credential_configured is False)

    print("\n=== 23. AISettingsWindow: singleton from History, provider-specific fields, full save round trip ===")
    reset_config_file()
    app5 = App()
    hw5 = HistoryWindow(app5)
    hw5.open_ai_settings()
    app5.update()
    win = hw5.ai_settings_window
    hw5.open_ai_settings()
    check("open_ai_settings() reuses the existing window (singleton)", hw5.ai_settings_window is win)
    check("default provider shown is Disabled (No AI)", win.provider_var.get() == AI_PROVIDER_LABELS["none"])
    check("no network fields are built for the disabled provider", len(win.fields_frame.winfo_children()) == 0)

    win.provider_var.set(AI_PROVIDER_LABELS["nox"])
    win._rebuild_fields()
    check("Nox shows no endpoint/API key/allow-remote fields (irrelevant fields hidden, not just disabled)",
          len(win.fields_frame.winfo_children()) == 0)

    win.provider_var.set(AI_PROVIDER_LABELS["openai_compatible"])
    win._rebuild_fields()
    check("openai_compatible shows its network fields", len(win.fields_frame.winfo_children()) > 0)
    win.endpoint_var.set("http://127.0.0.1:11434/v1")
    win.model_var.set("ui-fixture-model")
    win.api_key_var.set("ui-entered-secret-321")
    win._save()
    app5.update()
    check("save through the real UI updates the live App.ai_config",
          app5.ai_config is not None and app5.ai_config.endpoint == "http://127.0.0.1:11434/v1"
          and app5.ai_config.model == "ui-fixture-model")
    check("save through the real UI status label reports success", win.status_label.cget("text") == "Saved.")
    check("the API-key Entry is cleared after a successful save (never left showing/re-showing the secret)",
          win.api_key_var.get() == "")
    raw_after_ui_save = ai_settings.config_path().read_bytes()
    check("the UI-entered secret never lands in the config file's bytes",
          b"ui-entered-secret-321" not in raw_after_ui_save)

    win._reset()
    app5.update()
    check("RESET through the real UI disables AI in the live App", app5.ai_config is None and app5.ai_adapter is None)
    check("RESET through the real UI resets the provider dropdown", win.provider_var.get() == AI_PROVIDER_LABELS["none"])

    update_src = inspect.getsource(App.update_data)
    check("update_data() (the live 2s poll) never calls into AI settings/adapter machinery",
          "reload_ai_config(" not in update_src and "ai_settings." not in update_src)

    win.destroy(); hw5.destroy()
    app5 = destroy_test_app(app5)
    collect_destroyed_root()

    reset_config_file()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nFAILURES:")
        for name in FAILURES:
            print(f"  - {name}")
        raise SystemExit(1)
    print("ALL AI INTEGRATION SETTINGS CHECKS PASSED")


if __name__ == "__main__":
    main()
