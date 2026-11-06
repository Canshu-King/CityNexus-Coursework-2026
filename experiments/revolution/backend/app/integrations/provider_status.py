from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProviderState(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ProviderStatus:
    provider_key: str
    capability: str
    state: ProviderState
    detail: str | None = None
    latency_ms: int | None = None

    @property
    def usable(self) -> bool:
        return self.state in {ProviderState.READY, ProviderState.DEGRADED}
