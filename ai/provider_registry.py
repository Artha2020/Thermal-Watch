"""Provider registration and optional universal adapter facade."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .grounding_guard import BLOCKED_ANSWER_TEXT, GroundingGuard, GroundingReport
from .provider_contract import EvidenceBroker, ProviderConfig, ProviderContractError, ProviderResponse
from .providers.custom import CustomProvider
from .providers.nox import NoxProvider
from .providers.openai_compatible import OpenAICompatibleProvider


class ProviderRegistry:
    def __init__(self):
        self._factories: dict[str, Callable[..., Any]] = {
            "nox": NoxProvider,
            "openai_compatible": OpenAICompatibleProvider,
            "custom": CustomProvider,
        }

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def create(self, config: ProviderConfig, **dependencies):
        factory = self._factories.get(config.provider)
        if factory is None:
            raise ProviderContractError("unknown_provider", f"unknown AI provider: {config.provider}")
        return factory(config, **dependencies)


class UniversalAIAdapter:
    """Optional facade. Monitoring and evidence generation do not depend on it."""

    def __init__(self, config: Mapping[str, Any] | ProviderConfig | None = None,
                 *, registry: ProviderRegistry | None = None, broker: EvidenceBroker | None = None,
                 provider_dependencies: Mapping[str, Any] | None = None):
        self.registry = registry or ProviderRegistry()
        self.broker = broker or EvidenceBroker()
        self.config = None if config is None else (
            config if isinstance(config, ProviderConfig) else ProviderConfig.from_mapping(config)
        )
        self.dependencies = dict(provider_dependencies or {})

    def capabilities(self) -> dict[str, Any]:
        if self.config is None:
            return {
                "configured": False,
                "available": False,
                "providers": list(self.registry.names()),
                "evidence_operations": list(self.broker.operations()),
            }
        provider = self.registry.create(self.config, **self.dependencies)
        result = provider.capabilities(self.broker).as_dict()
        result["configured"] = True
        return result

    def ask(self, question: str, *, system_prompt: str | None = None) -> ProviderResponse:
        if self.config is None:
            return ProviderResponse.failure("none", "provider_not_configured", "no AI provider is configured")
        try:
            provider = self.registry.create(self.config, **self.dependencies)
            response = provider.ask(question, self.broker, system_prompt=system_prompt)
        except ProviderContractError as exc:
            return ProviderResponse.failure(self.config.provider, exc.code, exc.message, self.config.model)
        except Exception:
            # Provider failures are contained. Monitoring and evidence generation remain independent.
            return ProviderResponse.failure(self.config.provider, "provider_failure", "AI provider failed safely", self.config.model)
        # Phase 15 - Grounding Guard. Reviewed only through this facade (providers/broker are
        # untouched) - a response returned by a provider called directly, bypassing the adapter,
        # is deliberately left ungrounded; that is expected, not a gap (see ai/grounding_guard.py
        # and tools/verify_grounding_guard.py for the documented boundary). The Guard is pure and
        # defensively coded, but this try/except is the contractual backstop: it must never be
        # possible for a Guard failure to raise out of ask() or affect anything else in the app.
        try:
            response = GroundingGuard().review(response)
        except Exception:
            response = ProviderResponse(
                ok=True, provider=response.provider, model=response.model,
                answer=BLOCKED_ANSWER_TEXT, evidence=[],
                grounding=GroundingReport(claims=[], citations_valid=[], citations_rejected=[],
                                           corrected=True, verdict="blocked"),
            )
        return response
