from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ExternalProviderSettings:
    real_mobility_enabled: bool = _flag("CITYNEXUS_REAL_MOBILITY_ENABLED")
    router_name: str = os.getenv("CITYNEXUS_ROUTER", "valhalla")
    router_base_url: str | None = os.getenv("CITYNEXUS_ROUTER_BASE_URL")
    router_profile: str = os.getenv("CITYNEXUS_ROUTER_PROFILE", "pedestrian")

    open_meteo_enabled: bool = _flag("CITYNEXUS_OPEN_METEO_ENABLED")
    photon_enabled: bool = _flag("CITYNEXUS_PHOTON_ENABLED")
    photon_base_url: str = os.getenv("CITYNEXUS_PHOTON_BASE_URL", "https://photon.komoot.io")

    # Public-provider network policy (docs/NETWORK_POLICY.md).
    #   AUTO      - honour the environment/proxy configuration first, then a
    #               bounded direct retry when a public provider fails
    #   ENV_PROXY - always honour the environment proxy configuration
    #   DIRECT    - never use the environment proxy
    # Hardcoding either behaviour was wrong: one execution environment had a
    # broken proxy, and the user's machine runs Mihomo and may need one.
    network_mode: str = os.getenv("CITYNEXUS_PROVIDER_NETWORK_MODE", "auto").strip().lower()

    ai_enabled: bool = _flag("CITYNEXUS_AI_ENABLED")
    ai_provider: str = os.getenv("CITYNEXUS_AI_PROVIDER", "deterministic-fallback")
    ai_model: str | None = os.getenv("CITYNEXUS_AI_MODEL")
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY")

    webauthn_enabled: bool = _flag("CITYNEXUS_WEBAUTHN_ENABLED")


def provider_network_mode() -> str:
    """Normalised public-provider network mode."""

    mode = str(ExternalProviderSettings().network_mode or "auto").strip().lower()
    return mode if mode in {"auto", "env_proxy", "direct"} else "auto"


def trust_environment_for_public_providers() -> bool:
    """Whether httpx should honour the environment proxy for public providers.

    ``AUTO`` and ``ENV_PROXY`` honour it; ``DIRECT`` does not.
    """

    return provider_network_mode() in {"auto", "env_proxy"}
