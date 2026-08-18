"""Phase 16 - AI Integration Settings: config schema, DPAPI-backed secret storage, and load/
save/reset persistence for the user's chosen AI provider, plus the settings-screen connection
test. Deliberately independent of app.py: ai/ has never imported app.py and this must not change
that (app.py imports FROM ai/, not the reverse - see app.py's own AISettingsWindow for the other
side of this), so DATA_DIR is resolved here the same way app.py resolves it, rather than by
importing data_path() from app.py, which would create an ai/ -> app.py -> ai/ cycle.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import secret_store
from .provider_contract import EvidenceBroker, ProviderConfig, ProviderContractError
from .provider_registry import ProviderRegistry

CONFIG_FILENAME = "thermal_watch_ai_config.json"
SCHEMA_VERSION = 1

# Sentinel meaning "no AI provider configured". Deliberately a string, not JSON null, so the
# persisted file always has the same five-key shape whether AI is on or off. There is no "none"
# entry in ProviderRegistry - this is a settings-layer-only concept meaning "do not construct any
# adapter at all", never a 4th registered provider.
DISABLED_PROVIDER = "none"

_SCHEMA_KEYS = {"schema_version", "provider", "endpoint", "model", "allow_remote", "credential_ref"}


def _data_dir() -> Path:
    """Mirrors app.py's own _APP_DIR/DATA_DIR resolution (app.py:229-236) without importing
    app.py. Path(__file__) here is ai/ai_settings.py, so .parent.parent is the repo root - the
    same directory Path(__file__).parent resolves to from app.py itself (which lives at the repo
    root). Reads THERMAL_WATCH_DATA_DIR dynamically on every call (not cached at import time)
    so it follows the exact same env-var redirection tools/_verify_sandbox.py uses for every
    other store, regardless of import order relative to app.py."""
    app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    return Path(os.environ.get("THERMAL_WATCH_DATA_DIR") or app_dir).resolve()


def config_path() -> Path:
    return _data_dir() / CONFIG_FILENAME


def disabled_payload() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "provider": DISABLED_PROVIDER, "endpoint": None,
            "model": None, "allow_remote": False, "credential_ref": None}


def _atomic_write(payload: dict[str, Any]) -> bool:
    """Same tmp-write-then-replace pattern app.py uses for every other store (e.g.
    App._save_active_incidents, app.py:9471-9485) - a save failure must never crash anything,
    and an interrupted write can never corrupt a previously valid config."""
    try:
        path = config_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


@dataclass
class AISettingsStatus:
    """UI-facing projection of the persisted config. Guaranteed to never carry the raw API key -
    only whether one is configured, exactly like ProviderConfig.public_dict()."""
    provider: str | None  # None means disabled/not configured
    endpoint: str | None
    model: str | None
    allow_remote: bool
    credential_configured: bool


def load_provider_config() -> ProviderConfig | None:
    """Loads and validates the persisted AI config. Returns None for "no AI configured" - an
    absent file, the disabled sentinel, or ANY validation/decrypt failure (malformed JSON, wrong
    schema_version, unknown/missing field, wrong type, a credential_ref that fails to decrypt on
    this machine/user). This function must never raise: App.reload_ai_config() and every verify
    check rely on that to keep a broken AI config file from ever affecting app startup or
    monitoring."""
    try:
        path = config_path()
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != _SCHEMA_KEYS:
            return None
        if payload.get("schema_version") != SCHEMA_VERSION:
            return None
        provider = payload.get("provider")
        if not isinstance(provider, str) or not provider:
            return None
        if provider == DISABLED_PROVIDER:
            return None
        allow_remote = payload.get("allow_remote")
        if not isinstance(allow_remote, bool):
            return None
        endpoint = payload.get("endpoint")
        if endpoint is not None and not isinstance(endpoint, str):
            return None
        model = payload.get("model")
        if model is not None and not isinstance(model, str):
            return None
        credential_ref = payload.get("credential_ref")
        if credential_ref is not None and not isinstance(credential_ref, str):
            return None
        api_key = None
        if credential_ref:
            api_key = secret_store.unprotect(base64.b64decode(credential_ref)).decode("utf-8")
        mapping = {"provider": provider, "endpoint": endpoint, "model": model,
                   "allow_remote": allow_remote, "api_key": api_key}
        return ProviderConfig.from_mapping(mapping)
    except Exception:
        # Malformed JSON, wrong schema, a credential_ref that can't decrypt on this machine/user
        # (base64 error, DPAPIError, wrong-user blob), or a field ProviderConfig.from_mapping
        # itself rejects - all fall back to "no AI configured" rather than raising into the
        # caller (in particular, App.__init__ must never fail to start over this).
        return None


def status_from_config(config: ProviderConfig | None) -> AISettingsStatus:
    if config is None:
        return AISettingsStatus(provider=None, endpoint=None, model=None, allow_remote=False,
                                 credential_configured=False)
    return AISettingsStatus(provider=config.provider, endpoint=config.endpoint, model=config.model,
                             allow_remote=config.allow_remote, credential_configured=config.api_key is not None)


def save_provider_config(*, provider: str, endpoint: str | None, model: str | None,
                          allow_remote: bool, api_key: str | None) -> ProviderConfig:
    """Validates via ProviderConfig.from_mapping() - the single source of truth for provider
    validation (ai/provider_contract.py) - and, only once validation passes, persists an
    encrypted config. Raises ProviderContractError on invalid input with the exact codes/messages
    the contract already defines; the caller (AISettingsWindow._save) surfaces that directly
    rather than this module reimplementing validation.

    A blank/empty api_key is treated as "no credential" (ProviderConfig.from_mapping already
    normalizes falsy values via `api_key or None` below) - stored as credential_ref: null, never
    an empty-string ciphertext."""
    if provider == DISABLED_PROVIDER:
        disable_provider()
        return ProviderConfig(provider=DISABLED_PROVIDER)
    mapping = {"provider": provider, "endpoint": endpoint, "model": model,
               "allow_remote": bool(allow_remote), "api_key": api_key or None}
    config = ProviderConfig.from_mapping(mapping)  # raises ProviderContractError on bad input
    credential_ref = None
    if config.api_key:
        credential_ref = base64.b64encode(secret_store.protect(config.api_key.encode("utf-8"))).decode("ascii")
    payload = {"schema_version": SCHEMA_VERSION, "provider": config.provider, "endpoint": config.endpoint,
               "model": config.model, "allow_remote": config.allow_remote, "credential_ref": credential_ref}
    _atomic_write(payload)
    return config


def disable_provider() -> bool:
    """Reset/disable: atomically overwrites the file with the disabled sentinel. This also
    invalidates any previously stored credential_ref - it is simply overwritten with null, never
    read again."""
    return _atomic_write(disabled_payload())


# ---------------------------------------------------------------------------
# Connection test (settings-screen "Test Connection" button)
# ---------------------------------------------------------------------------
# Exactly 6 bounded, UI-safe outcomes. No caller of test_connection() ever sees anything else -
# in particular, never a raw traceback or exception message (see the blanket `except Exception`
# at the bottom of test_connection()).
CONNECTION_STATUSES = ("Invalid configuration", "Provider unavailable", "Endpoint unavailable",
                        "Authentication failed", "Model unavailable", "Connected")

# Fixed, clearly-synthetic, non-thermal prompt whose only purpose is to confirm the endpoint/
# model respond - deliberately asks the model NOT to use a tool, though the bounded tool-call
# loop in OpenAICompatibleProvider.ask() would handle it correctly either way. No mutation: this
# is a read-only ask() call, same guarantee every other ask() call already has.
PROBE_QUESTION = "Reply with exactly the single word: ready. Do not call any tool."
_AUTH_HTTP_CODES = (401, 403)


def _looks_like_missing_model(text: str) -> bool:
    """Heuristic, NOT guaranteed: OpenAI-compatible backends vary in exactly how they report an
    unknown model (message wording, HTTP 404 vs 400 vs 200-with-error-body, ...). This only
    recognizes the common shape (the word "model" plus a not-found-ish phrase) and is documented
    here as best-effort, not a contract every possible backend is guaranteed to satisfy."""
    if not text:
        return False
    low = text.lower()
    return "model" in low and any(
        kw in low for kw in ("not found", "does not exist", "unknown model", "no such model", "not available")
    )


def _classify_http_error(exc: urllib.error.HTTPError) -> dict[str, str]:
    if exc.code in _AUTH_HTTP_CODES:
        return {"status": "Authentication failed", "detail": f"endpoint returned HTTP {exc.code}"}
    body = ""
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    if _looks_like_missing_model(body):
        return {"status": "Model unavailable",
                "detail": f"endpoint returned HTTP {exc.code} (heuristic model-not-found match)"}
    return {"status": "Endpoint unavailable", "detail": f"endpoint returned HTTP {exc.code}"}


def test_connection(*, provider: str, endpoint: str | None, model: str | None,
                     allow_remote: bool, api_key: str | None,
                     provider_dependencies: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Tiered connection test:
      1. Local validation (ProviderConfig.from_mapping) - any ProviderContractError here, whether
         raised by the contract itself or by provider construction (e.g. openai_compatible
         requires endpoint+model), means "Invalid configuration"; no network attempt is made.
      2. Local capability check (provider.capabilities(broker)) - available=False means
         "Provider unavailable". This also exercises Thermal Watch's own tool discovery
         (broker.operations()/catalog()) as a side effect, with no network call.
      3. openai_compatible ONLY: one bounded, minimal, read-only live round trip through the
         provider's own already-tested ask() (no new HTTP primitive) with PROBE_QUESTION.
      4. nox/custom: no live call is possible from a bare settings screen - there is no
         dependency-injected callable a JSON settings file can itself supply - so this reports
         the local `available` flag honestly rather than faking a network check.
    Never raises. Always returns {"status": one of CONNECTION_STATUSES, "detail": short safe
    string} - never a raw traceback or exception message."""
    try:
        mapping = {"provider": provider, "endpoint": endpoint, "model": model,
                   "allow_remote": bool(allow_remote), "api_key": api_key or None}
        config = ProviderConfig.from_mapping(mapping)
    except ProviderContractError as exc:
        return {"status": "Invalid configuration", "detail": exc.message}

    broker = EvidenceBroker()
    registry = ProviderRegistry()
    deps = dict(provider_dependencies or {})
    try:
        provider_obj = registry.create(config, **deps)
        capabilities = provider_obj.capabilities(broker)
    except ProviderContractError as exc:
        # e.g. openai_compatible constructed without endpoint/model - a configuration problem,
        # not a runtime/network one.
        return {"status": "Invalid configuration", "detail": exc.message}
    except Exception:
        return {"status": "Provider unavailable", "detail": "provider capability check failed"}

    if not capabilities.available:
        if config.provider in ("nox", "custom"):
            return {"status": "Provider unavailable",
                    "detail": f"{config.provider} has no transport/handler injected from a settings screen"}
        return {"status": "Provider unavailable", "detail": "provider reports itself unavailable"}

    if config.provider != "openai_compatible":
        # nox/custom: local availability is the only thing a settings screen can check - there is
        # no callable a JSON settings file can supply, so no live call is attempted here.
        return {"status": "Connected", "detail": f"{config.provider} provider reports available"}

    try:
        response = provider_obj.ask(PROBE_QUESTION, broker)
    except urllib.error.HTTPError as exc:
        # Raised directly by an injected test transport that doesn't wrap it (see
        # tools/verify_ai_settings.py's FakeTransport) - the real _UrllibTransport wraps this
        # into ProviderContractError instead, handled in the branch below via __cause__.
        return _classify_http_error(exc)
    except ProviderContractError as exc:
        cause = exc.__cause__
        if isinstance(cause, urllib.error.HTTPError):
            return _classify_http_error(cause)
        return {"status": "Endpoint unavailable", "detail": "endpoint did not respond"}
    except (OSError, urllib.error.URLError):
        return {"status": "Endpoint unavailable", "detail": "endpoint did not respond"}
    except Exception:
        # Never let a raw traceback/exception message reach the UI - see the module docstring.
        return {"status": "Provider unavailable", "detail": "connection test failed unexpectedly"}

    if response.ok:
        return {"status": "Connected", "detail": f"endpoint responded using model {config.model}"}
    error = response.error or {}
    message = error.get("message") or ""
    if _looks_like_missing_model(message):
        return {"status": "Model unavailable", "detail": f"model {config.model!r} was not recognized (heuristic)"}
    return {"status": "Provider unavailable", "detail": message or error.get("code") or "provider returned an error"}
