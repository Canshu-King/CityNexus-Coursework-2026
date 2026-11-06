from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class PlannerVariant:
    key: str
    utility_milli: int
    disruption_count: int
    feasible: bool = True


class PlannerAdapter(Protocol):
    planner_key: str

    def choose(self, variants: Sequence[PlannerVariant]) -> PlannerVariant | None: ...


class DeterministicVariantSelector:
    planner_key = "deterministic-variant-selector"

    def choose(self, variants: Sequence[PlannerVariant]) -> PlannerVariant | None:
        feasible = [item for item in variants if item.feasible]
        if not feasible:
            return None
        return min(
            feasible, key=lambda item: (-item.utility_milli, item.disruption_count, item.key)
        )
