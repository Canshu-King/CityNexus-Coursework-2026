from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol

from app.intelligence.reason_codes import ReasonCode


@dataclass(frozen=True)
class DiscoveryCandidate:
    semantic_key: str
    name: str
    latitude: float
    longitude: float
    category: str | None = None
    open_now: bool | None = None
    cost_minor: int | None = None
    tags: tuple[str, ...] = ()
    source_keys: tuple[str, ...] = ()
    utility_milli: int = 0
    reason_codes: tuple[ReasonCode, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)


class CandidateSource(Protocol):
    async def candidates(self, query: str) -> list[DiscoveryCandidate]: ...


class CandidateEnricher(Protocol):
    async def enrich(self, candidate: DiscoveryCandidate) -> DiscoveryCandidate: ...


class CandidateScorer(Protocol):
    def score(self, candidate: DiscoveryCandidate) -> DiscoveryCandidate: ...


def dedupe_candidates(items: Iterable[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
    """Deterministically de-duplicate by semantic key, retaining richer facts."""
    selected: dict[str, DiscoveryCandidate] = {}
    for item in items:
        current = selected.get(item.semantic_key)
        if current is None:
            selected[item.semantic_key] = item
            continue
        current_fact_count = len(current.tags) + len(current.evidence)
        new_fact_count = len(item.tags) + len(item.evidence)
        if new_fact_count > current_fact_count:
            selected[item.semantic_key] = item
    return [selected[key] for key in sorted(selected)]


class DiscoveryPipeline:
    """PlacesRecommendation-inspired pipeline without importing its .NET app.

    candidate generation -> enrichment -> deterministic validation/scoring ->
    verified explanation happens later through the CityNexus AI boundary.
    """

    def __init__(
        self,
        *,
        sources: list[CandidateSource],
        enrichers: list[CandidateEnricher],
        scorer: CandidateScorer,
    ) -> None:
        self._sources = sources
        self._enrichers = enrichers
        self._scorer = scorer

    async def run(self, query: str) -> list[DiscoveryCandidate]:
        raw: list[DiscoveryCandidate] = []
        for source in self._sources:
            raw.extend(await source.candidates(query))
        candidates = dedupe_candidates(raw)
        enriched: list[DiscoveryCandidate] = []
        for candidate in candidates:
            current = candidate
            for enricher in self._enrichers:
                current = await enricher.enrich(current)
            enriched.append(self._scorer.score(current))
        return sorted(enriched, key=lambda item: (-item.utility_milli, item.semantic_key))
