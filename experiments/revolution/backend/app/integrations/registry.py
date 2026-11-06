from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.provider_status import ProviderState, ProviderStatus


@dataclass
class RegisteredProvider:
    capability: str
    provider: Any
    status: ProviderStatus
    priority: int = 100


class ProviderRegistry:
    """Small runtime registry for capability-first provider selection.

    Real providers may fail independently. CityNexus asks for a capability and
    receives the highest-priority usable provider; mocks/fallbacks can stay
    registered at lower priority.
    """

    def __init__(self) -> None:
        self._items: list[RegisteredProvider] = []

    def register(
        self,
        capability: str,
        provider: Any,
        *,
        state: ProviderState = ProviderState.READY,
        priority: int = 100,
        detail: str | None = None,
    ) -> None:
        provider_key = str(getattr(provider, "provider_key", provider.__class__.__name__))
        self._items.append(
            RegisteredProvider(
                capability=capability,
                provider=provider,
                priority=priority,
                status=ProviderStatus(
                    provider_key=provider_key,
                    capability=capability,
                    state=state,
                    detail=detail,
                ),
            )
        )

    def statuses(self, capability: str | None = None) -> tuple[ProviderStatus, ...]:
        items = (
            self._items
            if capability is None
            else [i for i in self._items if i.capability == capability]
        )
        return tuple(
            i.status
            for i in sorted(
                items, key=lambda item: (item.capability, item.priority, item.status.provider_key)
            )
        )

    def resolve(self, capability: str) -> Any:
        candidates = [
            item for item in self._items if item.capability == capability and item.status.usable
        ]
        if not candidates:
            raise LookupError(f"No usable provider for capability {capability!r}")
        selected = min(candidates, key=lambda item: (item.priority, item.status.provider_key))
        return selected.provider
