"""Stable provider contract for read-only Thermal Watch evidence.

Thermal Watch owns facts. Providers may ask for allowlisted evidence and explain it,
but they never receive a filesystem, SQL, process, command, or mutation primitive.
"""
from __future__ import annotations

import copy
import dataclasses
import ipaddress
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

import thermal_watch_evidence_cli as evidence_api


CONTRACT_VERSION = "1.0"
EVIDENCE_TOOL_NAME = "thermal_watch_evidence"
MAX_QUESTION_CHARS = 16_000

# Phase 17 - bounded, provider-neutral system instruction. ONE fixed, short constant (not
# per-provider, not model-specific, never carrying any telemetry data) that every provider MAY
# use to steer a model call toward Thermal Watch's own evidence discipline. Threaded through
# AIProvider.ask()/UniversalAIAdapter.ask() as an additive, optional `system_prompt` keyword
# (default None - falls back to this constant only inside providers that actually construct a
# model message, i.e. OpenAICompatibleProvider; Nox owns its own persona and Custom is an
# injected callable, so both accept the keyword for interface consistency but do not use it).
THERMAL_WATCH_SYSTEM_PROMPT = (
    "You are answering questions about this machine using Thermal Watch's own recorded evidence "
    "as the sole source of truth. Always call the thermal_watch_evidence tool rather than "
    "guessing or relying on general knowledge. If evidence is missing or insufficient, say so "
    "plainly instead of filling the gap. Periods Thermal Watch did not monitor are genuinely "
    "unknowable - never infer they were safe. Correlation is not causation. When citing a "
    "record, use the evidence_id exactly as given by the tool result; never invent one."
)


class ProviderContractError(ValueError):
    """A contained configuration, provider, or protocol failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_endpoint(endpoint: str, *, allow_remote: bool = False) -> str:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ProviderContractError("invalid_endpoint", "endpoint must be a non-empty HTTP(S) URL")
    value = endpoint.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderContractError("invalid_endpoint", "endpoint must be a valid HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderContractError("invalid_endpoint", "endpoint credentials, query strings, and fragments are not allowed")
    if not allow_remote and not _is_loopback(parsed.hostname):
        raise ProviderContractError("remote_endpoint_disabled", "remote AI endpoints require explicit allow_remote approval")
    return value


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    endpoint: str | None = None
    model: str | None = None
    api_key: str | None = field(default=None, repr=False)
    allow_remote: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProviderConfig":
        if not isinstance(value, Mapping):
            raise ProviderContractError("invalid_config", "provider configuration must be an object")
        allowed = {"provider", "endpoint", "model", "api_key", "allow_remote"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ProviderContractError("invalid_config", f"unknown provider configuration field: {unknown[0]}")
        provider = value.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise ProviderContractError("invalid_provider", "provider must be a non-empty string")
        endpoint = value.get("endpoint")
        allow_remote = value.get("allow_remote", False)
        if type(allow_remote) is not bool:
            raise ProviderContractError("invalid_config", "allow_remote must be boolean")
        if endpoint is not None:
            endpoint = validate_endpoint(endpoint, allow_remote=allow_remote)
        model = value.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ProviderContractError("invalid_model", "model must be a non-empty string when supplied")
        api_key = value.get("api_key")
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            raise ProviderContractError("invalid_api_key", "api_key must be null or a non-empty runtime string")
        return cls(provider=provider.strip(), endpoint=endpoint,
                   model=model.strip() if isinstance(model, str) else None,
                   api_key=api_key, allow_remote=allow_remote)

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model": self.model,
            "api_key_configured": self.api_key is not None,
            "allow_remote": self.allow_remote,
        }


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    available: bool
    model: str | None
    endpoint_type: str
    evidence_operations: tuple[str, ...]
    integration_mode: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "model": self.model,
            "endpoint_type": self.endpoint_type,
            "evidence_operations": list(self.evidence_operations),
            "integration_mode": self.integration_mode,
            "contract_version": CONTRACT_VERSION,
            "tool_catalog_version": evidence_api.TOOL_CATALOG_VERSION,
        }


@dataclass
class ProviderResponse:
    ok: bool
    provider: str
    answer: str | None = None
    model: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, str] | None = None
    # Phase 15 - Grounding Guard. Additive and optional, same discipline Phase 14 used for
    # evidence_id: default None means "not reviewed" so any pre-Phase-15 code constructing or
    # reading a ProviderResponse without knowing this field keeps working unchanged. Populated
    # only by GroundingGuard.review() (see ai/grounding_guard.py), wired in through
    # UniversalAIAdapter.ask() - never by a provider itself.
    grounding: Any | None = None

    @classmethod
    def failure(cls, provider: str, code: str, message: str, model: str | None = None) -> "ProviderResponse":
        return cls(ok=False, provider=provider, model=model, error={"code": code, "message": message})

    def as_dict(self) -> dict[str, Any]:
        grounding = self.grounding
        if dataclasses.is_dataclass(grounding) and not isinstance(grounding, type):
            grounding = dataclasses.asdict(grounding)
        return {
            "ok": self.ok,
            "provider": self.provider,
            "model": self.model,
            "answer": self.answer,
            "evidence": copy.deepcopy(self.evidence),
            "error": copy.deepcopy(self.error),
            "grounding": copy.deepcopy(grounding),
        }


class EvidenceBroker:
    """The only provider-facing door into Thermal Watch facts."""

    def catalog(self) -> dict[str, Any]:
        result = evidence_api.describe_operation_catalog()
        if not result.get("ok"):
            raise ProviderContractError("catalog_unavailable", "Thermal Watch tool catalog is unavailable")
        return copy.deepcopy(result)

    def operations(self) -> tuple[str, ...]:
        return tuple(self.catalog()["operations"])

    def tool_definition(self, *, include_catalog: bool = False) -> dict[str, Any]:
        catalog = self.catalog()
        operations = catalog["operations"]
        parameter_properties = {}
        for definition in operations.values():
            for name, schema in definition["parameters"].get("properties", {}).items():
                parameter_properties.setdefault(name, copy.deepcopy(schema))
        operation_summary = "; ".join(
            f"{name}: {definition['description']}" for name, definition in operations.items()
        )
        tool = {
            "type": "function",
            "function": {
                "name": EVIDENCE_TOOL_NAME,
                "description": (
                    "Query read-only facts recorded by Thermal Watch. Call describe_operations for the "
                    "complete versioned catalog and current availability. Null means unavailable; "
                    "monitoring_gap means coverage is incomplete; correlation is not causation. "
                    "Operations: " + operation_summary
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": list(operations)},
                        "parameters": {
                            "type": "object",
                            "properties": parameter_properties,
                            "additionalProperties": False,
                        },
                    },
                    "required": ["operation"],
                    "additionalProperties": False,
                },
            },
        }
        if include_catalog:
            # Kept outside the standard function object so strict OpenAI-compatible
            # endpoints never receive a vendor extension. Local/custom clients can
            # inspect the same canonical metadata without another request.
            tool["x-thermal-watch-catalog"] = catalog
        return tool

    def dispatch(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if tool_name != EVIDENCE_TOOL_NAME:
            return {"ok": False, "error": {"code": "unknown_tool", "message": "tool is not allowlisted"}}
        if not isinstance(arguments, Mapping):
            return {"ok": False, "error": {"code": "invalid_request", "message": "tool arguments must be an object"}}
        # Copy before dispatch so a provider cannot retain and mutate the request object.
        return copy.deepcopy(evidence_api.handle_request(copy.deepcopy(dict(arguments))))


def validate_question(question: str) -> str:
    if not isinstance(question, str) or not question.strip():
        raise ProviderContractError("invalid_question", "question must be a non-empty string")
    if len(question) > MAX_QUESTION_CHARS:
        raise ProviderContractError("invalid_question", "question exceeds the provider contract limit")
    return question.strip()


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        raise ProviderContractError("malformed_provider_response", "tool arguments must be JSON object text")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderContractError("malformed_provider_response", "provider returned malformed tool arguments") from exc
    if not isinstance(value, dict):
        raise ProviderContractError("malformed_provider_response", "provider tool arguments must decode to an object")
    return value


class AIProvider(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    def capabilities(self, broker: EvidenceBroker) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    def ask(self, question: str, broker: EvidenceBroker, *, system_prompt: str | None = None) -> ProviderResponse:
        raise NotImplementedError
