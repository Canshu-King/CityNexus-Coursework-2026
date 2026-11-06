import pytest

from app.integrations.discovery.pipeline import DiscoveryCandidate, DiscoveryPipeline
from app.intelligence.reason_codes import ReasonCode


class Source:
    def __init__(self, items):
        self.items = items

    async def candidates(self, query):
        return list(self.items)


class Enricher:
    async def enrich(self, candidate):
        return DiscoveryCandidate(
            **{
                **candidate.__dict__,
                "tags": tuple(sorted(set(candidate.tags) | {"verified"})),
                "evidence": {**candidate.evidence, "open_verified": True},
            }
        )


class Scorer:
    def score(self, candidate):
        score = 1000 + 100 * len(candidate.tags)
        return DiscoveryCandidate(
            **{
                **candidate.__dict__,
                "utility_milli": score,
                "reason_codes": (ReasonCode.OPEN_AT_VISIT_TIME,),
            }
        )


@pytest.mark.asyncio
async def test_places_recommendation_style_pipeline_is_deterministic_and_dedupes():
    poor = DiscoveryCandidate("osm:N1", "Museum", 39.1, 117.2)
    rich = DiscoveryCandidate("osm:N1", "Museum", 39.1, 117.2, tags=("history",))
    other = DiscoveryCandidate("osm:N2", "Cafe", 39.2, 117.3, tags=("coffee",))
    pipeline = DiscoveryPipeline(
        sources=[Source([poor]), Source([rich, other])],
        enrichers=[Enricher()],
        scorer=Scorer(),
    )
    result = await pipeline.run("culture")
    assert [item.semantic_key for item in result] == ["osm:N1", "osm:N2"]
    assert result[0].evidence["open_verified"] is True
