import json
from types import SimpleNamespace
from uuid import uuid4

import httpx

from app import fact_acquisition as facts
from app.db.models import Journey, Place, Traveler, WebObservation
from app.db.session import SessionLocal, ensure_schema


def _journey(city: str, traveler_id: str, journey_id: str) -> Journey:
    return Journey(
        id=journey_id,
        traveler_id=traveler_id,
        title="R27 truth mesh",
        city_code=city,
        timezone="Asia/Tokyo",
        start_at="2026-10-12T09:00:00+09:00",
        end_at="2026-10-12T20:00:00+09:00",
        budget_currency="JPY",
        budget_amount_minor=100000,
        planning_mode="balanced",
        status="DRAFT",
        active_plan_version_id=None,
        current_activity_id=None,
        created_at="2026-10-01T00:00:00+00:00",
        updated_at="2026-10-01T00:00:00+00:00",
    )


def test_truth_mesh_nominatim_exact_identity_continues_after_overpass_failure(monkeypatch):
    ensure_schema()
    city = f"R27-TRUTH-MESH-{uuid4().hex}"
    traveler_id, journey_id, place_id, observation_id = [uuid4().hex for _ in range(4)]
    url = "https://official.example/craft"
    fetched: list[str] = []
    html = "<html>Craft Museum Opening hours: 10:00-18:00 Admission fee: 0 JPY</html>"

    def fail_overpass(*_args, **_kwargs):
        error = httpx.ReadTimeout("overpass unavailable")
        error.provider_attempts = [{"endpoint": "https://overpass.example", "answered": False}]
        raise error

    def exact_nominatim(*_args, **_kwargs):
        return {
            ("node", "2465643503"): {
                "osm_type": "node",
                "osm_id": "2465643503",
                "display_name": "Craft Museum, Nara",
                "extratags": {"name": "Craft Museum", "website": url},
            }
        }, "https://nominatim.example/lookup"

    def fake_get(_client, requested_url, *, retrieved_at):
        fetched.append(requested_url)
        return httpx.Response(200, request=httpx.Request("GET", requested_url), text=html), requested_url

    monkeypatch.setattr(facts, "_acquire_overpass_payload", fail_overpass)
    monkeypatch.setattr(facts, "_nominatim_lookup", exact_nominatim)
    monkeypatch.setattr(facts, "_safe_get", fake_get)
    monkeypatch.setattr(facts, "_search_fact_candidates", lambda *_, **__: {"status": "UNCONFIGURED", "attempted": False, "observations": []})

    with SessionLocal() as session:
        traveler = Traveler(
            id=traveler_id, display_name="R27 traveler", email=None, locale="en-US",
            timezone="Asia/Tokyo", created_at="2026-10-01", updated_at="2026-10-01",
        )
        journey = _journey(city, traveler_id, journey_id)
        place = Place(
            id=place_id, provider_key="overpass", external_id="overpass:node/2465643503",
            city_code=city, name="Craft Museum", category="museum", latitude=34.68,
            longitude=135.83, opening_hours_json=json.dumps({"status": "UNKNOWN"}),
            tags_json="[]", metadata_json="{}", updated_at="2026-10-01",
        )
        observation = WebObservation(
            id=observation_id, journey_id=journey_id, query="server-owned exact query", url=url,
            domain="official.example", title="Craft Museum", source_class="MUNICIPAL_AUTHORITY",
            provider_key="test-search", model_key="test", captured_at="2026-10-01",
            evidence_fingerprint=uuid4().hex, content_excerpt="Craft Museum", authority_status="NON_AUTHORITATIVE",
            created_at="2026-10-01",
        )
        session.add(traveler)
        session.commit()
        session.add(journey)
        session.commit()
        session.add_all([place, observation])
        session.commit()

        receipt = facts.acquire_place_facts(
            session,
            journey,
            place_ids=[place.id],
            source_bindings={place.id: {"source_url": url, "source_observation_id": observation.id}},
            binding_origin="agent_observation",
        )

        row = receipt["places"][0]
        assert receipt["degraded_providers"] == ["overpass"]
        assert receipt["identity_authority"] == "EXACT_OSM_ID_VIA_NOMINATIM"
        assert row["identity_authority"] == "EXACT_OSM_ID_VIA_NOMINATIM"
        assert row["source_binding"]["approval"] == "CORROBORATED_OFFICIAL_SOURCE"
        assert receipt["official_web"]["acquired"] == 1
        assert row["opening_hours"]["status"] == "OFFICIAL_WEB_VERIFIED"
        assert row["cost_evidence"]["status"] == "VERIFIED_PROVIDER"
        assert fetched == [url, url]


def test_truth_mesh_fails_closed_without_exact_identity(monkeypatch):
    ensure_schema()
    city = f"R27-TRUTH-MESH-NO-ID-{uuid4().hex}"
    traveler_id, journey_id, place_id = [uuid4().hex for _ in range(3)]
    monkeypatch.setattr(facts, "_acquire_overpass_payload", lambda *_: ( {"elements": []}, "https://overpass.example", []))
    monkeypatch.setattr(facts, "_nominatim_lookup", lambda *_: ({}, "https://nominatim.example/lookup"))

    with SessionLocal() as session:
        session.add(Traveler(id=traveler_id, display_name="R27 traveler", email=None, locale="en-US", timezone="Asia/Tokyo", created_at="2026-10-01", updated_at="2026-10-01"))
        session.commit()
        journey = _journey(city, traveler_id, journey_id)
        place = Place(id=place_id, provider_key="overpass", external_id="overpass:node/999", city_code=city, name="Unknown", category="museum", latitude=1, longitude=1, opening_hours_json="{}", tags_json="[]", metadata_json="{}", updated_at="2026-10-01")
        session.add(journey)
        session.commit()
        session.add(place)
        session.commit()
        receipt = facts.acquire_place_facts(session, journey, place_ids=[place.id])
        assert receipt["places"][0]["status"] == "IDENTITY_UNAVAILABLE"
        assert receipt["known_opening_count"] == 0
        assert receipt["known_cost_count"] == 0
