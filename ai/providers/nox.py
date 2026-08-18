"""Nox reference-provider metadata.

Nox owns its persona/tool loop and invokes the universal evidence CLI itself. Thermal
Watch neither imports Nox nor reaches into its internals.
"""
from __future__ import annotations

from ..provider_contract import AIProvider, EvidenceBroker, ProviderCapabilities, ProviderResponse, validate_question


class NoxProvider(AIProvider):
    def __init__(self, config, *, transport=None, **_):
        super().__init__(config)
        self.transport = transport

    def capabilities(self, broker: EvidenceBroker) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="nox", available=self.transport is not None, model=self.config.model,
            endpoint_type="external_local_tool_client", evidence_operations=broker.operations(),
            integration_mode="provider_invokes_thermal_watch_cli",
        )

    def ask(self, question: str, broker: EvidenceBroker, *, system_prompt: str | None = None) -> ProviderResponse:
        # system_prompt is accepted for interface consistency with AIProvider.ask() only - Nox
        # owns its own persona/tool loop entirely (see the module docstring) and is never handed
        # a Thermal Watch system instruction to inject.
        question = validate_question(question)
        if self.transport is None:
            return ProviderResponse.failure(
                "nox", "external_provider", "Nox queries Thermal Watch through its own local persona tool loop",
                self.config.model,
            )
        try:
            result = self.transport(question, broker.tool_definition(include_catalog=True), broker.dispatch)
        except Exception:
            return ProviderResponse.failure("nox", "provider_unavailable", "Nox provider is unavailable", self.config.model)
        if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
            return ProviderResponse.failure("nox", "malformed_provider_response", "Nox returned an invalid response", self.config.model)
        evidence = result.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(row, dict) for row in evidence):
            return ProviderResponse.failure("nox", "malformed_provider_response", "Nox returned invalid evidence metadata", self.config.model)
        return ProviderResponse(True, "nox", result["answer"], self.config.model, evidence)
