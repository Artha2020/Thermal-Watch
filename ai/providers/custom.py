"""Injected custom/local provider adapter with the same bounded evidence door."""
from __future__ import annotations

from ..provider_contract import AIProvider, EvidenceBroker, ProviderCapabilities, ProviderResponse, validate_question


class CustomProvider(AIProvider):
    def __init__(self, config, *, handler=None, **_):
        super().__init__(config)
        self.handler = handler

    def capabilities(self, broker: EvidenceBroker) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="custom", available=callable(self.handler), model=self.config.model,
            endpoint_type="injected_local_callable", evidence_operations=broker.operations(),
            integration_mode="bounded_callback",
        )

    def ask(self, question: str, broker: EvidenceBroker, *, system_prompt: str | None = None) -> ProviderResponse:
        # system_prompt is accepted for interface consistency with AIProvider.ask() only - the
        # injected handler owns its own prompting entirely; no Thermal Watch instruction is
        # forced into an arbitrary callable's contract.
        question = validate_question(question)
        if not callable(self.handler):
            return ProviderResponse.failure("custom", "provider_unavailable", "custom provider is unavailable", self.config.model)
        try:
            result = self.handler(question, broker.tool_definition(include_catalog=True), broker.dispatch)
        except Exception:
            return ProviderResponse.failure("custom", "provider_unavailable", "custom provider failed safely", self.config.model)
        if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
            return ProviderResponse.failure("custom", "malformed_provider_response", "custom provider returned an invalid response", self.config.model)
        evidence = result.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(row, dict) for row in evidence):
            return ProviderResponse.failure("custom", "malformed_provider_response", "custom provider returned invalid evidence metadata", self.config.model)
        return ProviderResponse(True, "custom", result["answer"], self.config.model, evidence)
