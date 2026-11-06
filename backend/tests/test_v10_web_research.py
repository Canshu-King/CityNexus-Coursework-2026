from types import SimpleNamespace

from app.integrations.ai.web_research import WebResearchProvider


class _Client:
    model = "deepseek-flash"

    def __init__(self, observations):
        self.observations = observations

    def complete(self, *_args, **_kwargs):
        return SimpleNamespace(
            text="A model summary without a source",
            web_observations=self.observations,
        )


def test_v10_web_summary_without_source_is_not_persisted_as_evidence():
    assert WebResearchProvider(_Client([])).search("official venue notice") == ()


def test_v10_web_observation_keeps_source_and_fingerprint():
    observations = [{"url": "https://official.example/notice", "title": "Closure", "domain": "official.example", "content": "Closed"}]
    result = WebResearchProvider(_Client(observations)).search("official venue notice", source_class="OFFICIAL_NOTICE")
    assert len(result) == 1
    assert result[0].url == "https://official.example/notice"
    assert result[0].evidence_fingerprint
