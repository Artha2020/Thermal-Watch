"""Optional, provider-neutral AI integration for Thermal Watch evidence."""

from .provider_contract import (  # noqa: F401
    AIProvider,
    EvidenceBroker,
    ProviderCapabilities,
    ProviderConfig,
    ProviderResponse,
)
from .provider_registry import ProviderRegistry, UniversalAIAdapter  # noqa: F401
from .grounding_guard import ClaimResult, GroundingGuard, GroundingReport  # noqa: F401
