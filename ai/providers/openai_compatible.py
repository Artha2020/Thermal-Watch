"""Minimal OpenAI-compatible chat-completions provider with bounded tool calls."""
from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request
from typing import Any

from ..provider_contract import (
    AIProvider,
    EVIDENCE_TOOL_NAME,
    EvidenceBroker,
    ProviderCapabilities,
    ProviderContractError,
    ProviderResponse,
    THERMAL_WATCH_SYSTEM_PROMPT,
    parse_tool_arguments,
    validate_question,
)


MAX_TOOL_CALLS = 4
MAX_RESPONSE_BYTES = 1_000_000


def _chat_url(endpoint: str) -> str:
    if endpoint.endswith("/chat/completions"):
        return endpoint
    return endpoint + "/chat/completions"


class _UrllibTransport:
    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            url, data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise ProviderContractError("provider_unavailable", "OpenAI-compatible provider is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderContractError("malformed_provider_response", "provider response exceeds the size limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderContractError("malformed_provider_response", "provider returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise ProviderContractError("malformed_provider_response", "provider response must be a JSON object")
        return value


def _message_from_response(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderContractError("malformed_provider_response", "provider response has no choice")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProviderContractError("malformed_provider_response", "provider response has no assistant message")
    return message


class OpenAICompatibleProvider(AIProvider):
    def __init__(self, config, *, transport=None, timeout=30.0, **_):
        super().__init__(config)
        if not config.endpoint or not config.model:
            raise ProviderContractError("invalid_config", "openai_compatible requires endpoint and model")
        self.transport = transport or _UrllibTransport()
        self.timeout = float(timeout)

    def capabilities(self, broker: EvidenceBroker) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="openai_compatible", available=True, model=self.config.model,
            endpoint_type="openai_chat_completions", evidence_operations=broker.operations(),
            integration_mode="bounded_tool_call_loop",
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        return self.transport.post_json(_chat_url(self.config.endpoint), payload, headers, self.timeout)

    def ask(self, question: str, broker: EvidenceBroker, *, system_prompt: str | None = None) -> ProviderResponse:
        question = validate_question(question)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt or THERMAL_WATCH_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        evidence_results: list[dict[str, Any]] = []
        for _ in range(MAX_TOOL_CALLS + 1):
            response = self._post({
                "model": self.config.model, "messages": messages,
                "tools": [broker.tool_definition()], "tool_choice": "auto", "stream": False,
            })
            message = _message_from_response(response)
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    return ProviderResponse.failure("openai_compatible", "malformed_provider_response",
                                                    "provider returned neither an answer nor a tool call", self.config.model)
                return ProviderResponse(True, "openai_compatible", content.strip(), self.config.model,
                                        copy.deepcopy(evidence_results))
            if not isinstance(tool_calls, list) or not tool_calls:
                return ProviderResponse.failure("openai_compatible", "malformed_provider_response",
                                                "provider tool_calls must be a non-empty array", self.config.model)
            if len(evidence_results) + len(tool_calls) > MAX_TOOL_CALLS:
                return ProviderResponse.failure("openai_compatible", "tool_limit_exceeded",
                                                "provider exceeded the evidence tool-call limit", self.config.model)
            messages.append(copy.deepcopy(message))
            for call in tool_calls:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                    return ProviderResponse.failure("openai_compatible", "malformed_provider_response",
                                                    "provider returned an invalid tool call", self.config.model)
                function = call.get("function")
                if not isinstance(function, dict):
                    return ProviderResponse.failure("openai_compatible", "malformed_provider_response",
                                                    "provider returned an invalid function call", self.config.model)
                name = function.get("name")
                try:
                    arguments = parse_tool_arguments(function.get("arguments"))
                except ProviderContractError as exc:
                    return ProviderResponse.failure("openai_compatible", exc.code, exc.message, self.config.model)
                result = broker.dispatch(name, arguments)
                evidence_results.append(copy.deepcopy(result))
                messages.append({
                    "role": "tool", "tool_call_id": call["id"], "name": EVIDENCE_TOOL_NAME,
                    "content": json.dumps(result, separators=(",", ":"), ensure_ascii=False),
                })
        return ProviderResponse.failure("openai_compatible", "tool_limit_exceeded",
                                        "provider did not complete within the evidence tool-call limit", self.config.model)
