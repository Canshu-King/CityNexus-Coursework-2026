import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import fact_acquisition as facts
from app.db.models import Place
from app.db.session import SessionLocal, ensure_schema


@pytest.mark.parametrize("external_id", [
    "overpass:node/1515",
    "nominatim:relation/1515",
    "photon:way/1515",
])
def test_osm_provider_identity_can_be_rechecked_by_exact_osm_id(external_id):
    place = Place(external_id=external_id)
    assert facts._element_ref(place) == (external_id.split(":", 1)[1].split("/", 1)[0], "1515")


@pytest.mark.parametrize("provider_website,expected_known", [(None, 0), ("https://official.example/visit", 1)])
def test_agent_source_only_promotes_when_fresh_exact_entity_corroborates(monkeypatch, provider_website, expected_known):
    ensure_schema()
    city = f"R15-TEST-{uuid4().hex}"
    tags = {"website": provider_website} if provider_website else {}
    monkeypatch.setattr(facts, "_acquire_overpass_payload", lambda *_: (
        {"elements": [{"type": "node", "id": 1515, "tags": tags}]}, "https://overpass.example", []))
    monkeypatch.setattr(facts, "_nominatim_lookup", lambda *_: ({}, "https://nominatim.example/lookup"))
    monkeypatch.setattr(facts, "_official_web_facts", lambda *_, **kw: {
        "status": "ACQUIRED",
        "opening_hours": {"status": "OFFICIAL_WEB_VERIFIED", "open": "09:00", "close": "17:00"},
        "cost_evidence": {"status": "VERIFIED_PROVIDER", "amount_minor": 2000, "currency": "CNY"},
    })
    monkeypatch.setattr(facts, "_search_fact_candidates", lambda *_, **kw: {"status": "UNCONFIGURED"})
    with SessionLocal() as session:
        place = Place(id=uuid4().hex, provider_key="overpass", external_id="overpass:node/1515",
            city_code=city, name="Museum", category="museum", latitude=1, longitude=2,
            opening_hours_json='{"status":"UNKNOWN"}', tags_json="[]", metadata_json="{}", updated_at="2026-10-01")
        session.add(place)
        session.flush()
        receipt = facts.acquire_place_facts(session, SimpleNamespace(city_code=city, id=uuid4().hex, start_at="2026-10-13T09:00:00+08:00"),
            source_bindings={place.id: {"source_url": "https://official.example/visit", "approval": "OPERATOR_VERIFIED"}},
            binding_origin="agent")
        binding = receipt["places"][0]["source_binding"]
        assert binding["approval"] != "OPERATOR_VERIFIED"
        assert binding["identity_basis"]["matched_provider_entity"] == bool(expected_known)
        assert receipt["known_opening_count"] == expected_known
        assert receipt["known_cost_count"] == expected_known
        assert (json.loads(place.metadata_json)["cost_evidence"]["status"] == "UNKNOWN") == (not expected_known)
