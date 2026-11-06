from types import SimpleNamespace

from app.fact_acquisition import _requested_fact_source_binding
from app.integrations.web.research_service import corroborate_observation


def _place():
    return SimpleNamespace(id="p1", external_id="overpass:node/246", name="Craft Museum")


def _observation(url="https://city.example/craft"):
    return SimpleNamespace(id="obs-1", url=url)


def test_agent_raw_url_never_gets_operator_authority():
    binding = _requested_fact_source_binding(
        _place(), {"source_url": "https://city.example/craft", "source_class": "MUNICIPAL_AUTHORITY"}, origin="agent"
    )
    assert binding["approval"] == "AGENT_SOURCE_CANDIDATE"
    assert binding["identity_basis"]["matched_provider_entity"] is False


def test_exact_provider_website_tag_can_corroborate_without_name_guessing():
    result = corroborate_observation(
        place=_place(), observation=_observation(),
        page_text="Craft Museum opening hours 10:00-18:00",
        provider_tags={"name": "Craft Museum", "website": "https://city.example/craft"},
        nominatim_row={},
    )
    assert result["status"] == "CORROBORATED_OFFICIAL_SOURCE"
    assert result["matched_provider_entity"] is True
    assert "exact_provider_website_tag" in result["matched_anchors"]


def test_same_name_without_independent_anchor_is_rejected():
    result = corroborate_observation(
        place=_place(), observation=_observation("https://wrong.example/craft"),
        page_text="Craft Museum opening hours 10:00-18:00",
        provider_tags={"name": "Craft Museum"}, nominatim_row={},
    )
    assert result["status"] == "INSUFFICIENT_IDENTITY_EVIDENCE"
    assert result["matched_provider_entity"] is False


def test_wrong_city_same_name_does_not_match_address_anchor():
    result = corroborate_observation(
        place=_place(), observation=_observation("https://wrong.example/craft"),
        page_text="Craft Museum, Wrong City, 999-0000",
        provider_tags={"name": "Craft Museum", "addr:city": "Right City", "addr:postcode": "100-0000"},
        nominatim_row={},
    )
    assert result["status"] == "INSUFFICIENT_IDENTITY_EVIDENCE"
