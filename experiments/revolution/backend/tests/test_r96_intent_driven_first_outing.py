from urllib.parse import parse_qs, unquote_plus

import httpx

from app.integrations.discovery.real_city import OverpassPOIProvider


def test_r96_explicit_outing_intents_emit_bounded_overpass_categories():
    """The four user choices must remain deterministic provider questions."""

    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = parse_qs(request.content.decode("utf-8"))
        queries.append(unquote_plus(payload["data"][0]))
        return httpx.Response(200, json={"elements": []})

    provider = OverpassPOIProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://controlled.example/api",
    )
    try:
        for query in ("museum/gallery", "park/garden", "cafe", "library", "gallery", "mobility"):
            candidates, attempts = provider.search_with_evidence(
                query,
                latitude=31.2304,
                longitude=121.4737,
                radius_m=2500,
                limit=3,
                max_attempts=1,
            )
            assert candidates == []
            assert attempts[0]["answered"] is True
    finally:
        provider.close()

    assert len(queries) == 6
    museum_query = queries[0]
    assert museum_query.startswith('[out:json][timeout:10];(')
    assert museum_query.count('nwr[') == 2
    assert 'nwr["tourism"~"museum|gallery"]' in museum_query
    assert 'nwr["amenity"="arts_centre"]' in museum_query
    assert museum_query.index('nwr["tourism"~"museum|gallery"]') < museum_query.index(
        'nwr["amenity"="arts_centre"]'
    )
    assert museum_query.endswith(');out center tags;')
    assert 'nwr["leisure"~"park|garden"]' in queries[1]
    assert 'nwr["amenity"~"cafe|coffee"]' in queries[2]
    assert 'nwr["amenity"="library"]' in queries[3]
    assert '"amenity"~"cafe|coffee"' not in queries[0]
    assert '"amenity"~"cafe|coffee"' not in queries[1]
    # The Overpass QL default result set is overwritten by each standalone
    # selector. Merely containing two nwr filters is NOT a union: require one
    # enclosing union block before the single output statement.
    for index in (0, 4, 5):
        assert queries[index].startswith("[out:json][timeout:10];(")
        assert queries[index].endswith(");out center tags;")
    assert queries[0].count("nwr[") == 2
    assert queries[4].count("nwr[") == 2
    assert queries[5].count("nwr[") == 3
    assert 'nwr["tourism"="gallery"]' in queries[4]
    assert 'nwr["amenity"="arts_centre"]' in queries[4]
    assert 'nwr["railway"~"station|subway_entrance"]' in queries[5]
    assert 'nwr["amenity"="bus_station"]' in queries[5]
    assert 'nwr["highway"="bus_stop"]' in queries[5]
    for index in (1, 2, 3):
        assert queries[index].count("nwr[") == 1
        assert queries[index].startswith("[out:json][timeout:10];nwr[")


def test_r96_shared_osm_identity_keeps_member_request_grade(monkeypatch):
    """The shared OSM row must never upgrade B's controlled receipt to A's live grade.

    Provider endpoint strings in this test are MOCKED, not evidence that a
    real public network request occurred.
    """
    from datetime import datetime, timezone
    from uuid import uuid4

    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.core.config import settings
    from app.db.models import JourneyMember
    from app.db.session import SessionLocal
    from app.integrations.discovery.real_city import DiscoveryCandidate

    previous = {
        "traveler_auth_secret": settings.traveler_auth_secret,
        "real_city_enabled": settings.real_city_enabled,
        "real_city_mode": settings.real_city_mode,
    }
    object.__setattr__(settings, "traveler_auth_secret", "REDACTED_FOR_PUBLIC_RELEASE")
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")
    attempts = []

    def fake_search(_self, query, **kwargs):
        attempts.append(query)
        endpoint = (
            "https://overpass-api.de/api/interpreter"
            if len(attempts) == 1
            else "http://127.0.0.1:9626/interpreter"
        )
        candidate = DiscoveryCandidate(
            provider_key="overpass",
            external_id="overpass:node/96121",
            name="Shared R96 Museum",
            category="museum",
            latitude=31.2304,
            longitude=121.4737,
            tags=("tourism",),
            opening_hours={"status": "UNKNOWN"},
            metadata={"tags": {"name": "Shared R96 Museum", "tourism": "museum"}},
            cost_evidence={"status": "UNKNOWN"},
        )
        return [candidate], [
            {"endpoint": endpoint, "answered": True, "http_status": 200, "elements": 1}
        ]

    monkeypatch.setattr(
        "app.execution.bootstrap_discovery.OverpassPOIProvider.search_with_evidence",
        fake_search,
    )
    client = TestClient(main_module.app)
    try:
        def traveler(label):
            response = client.post(
                "/api/v1/travelers",
                json={"display_name": label, "timezone": "Asia/Shanghai", "locale": "en-GB"},
            )
            assert response.status_code == 201, response.text
            return response.json()

        owner = traveler("R96 shared grade owner")
        colleague = traveler("R96 shared grade collaborator")
        journey_resp = client.post("/api/v1/journeys", json={
            "traveler_id": owner["id"],
            "title": "R96 shared grade signed scopes",
            "city_code": f"CN-R96-SCOPE-{uuid4().hex[:8]}",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-15T10:00:00+08:00",
            "end_at": "2026-10-16T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        })
        assert journey_resp.status_code == 201, journey_resp.text
        journey_id = journey_resp.json()["id"]
        stamp = datetime.now(timezone.utc).isoformat()
        with SessionLocal() as session:
            session.add(JourneyMember(
                id=str(uuid4()), journey_id=journey_id,
                traveler_id=colleague["id"], role="COLLABORATOR",
                invited_by=owner["id"], expires_at=None, joined_at=stamp,
                removed_at=None, preferences_json="{}",
                created_at=stamp, updated_at=stamp,
            ))
            session.commit()

        def headers(person):
            return {"Authorization": f"Bearer {person['access_token']}"}

        body = {
            "query": "museum/gallery", "radius_m": 2500, "limit": 1,
            "source_kind": "MANUAL_PUBLIC_CENTER", "provider_consent": True,
            "latitude": 31.2304, "longitude": 121.4737,
            "coordinate_system": "WGS84", "mode": "live",
        }
        first = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=headers(owner), json=body,
        )
        assert first.status_code == 200, first.text
        first_receipt = first.json()
        assert first_receipt["provider_provenance"] == "LIVE_PROVIDER"
        second = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=headers(colleague), json=body,
        )
        assert second.status_code == 200, second.text
        second_receipt = second.json()
        assert second_receipt["provider_provenance"] == "TEST_PROVIDER"
        assert first_receipt["candidates"][0]["candidate_id"] == second_receipt["candidates"][0]["candidate_id"]
        assert second_receipt["candidates"][0]["provider_provenance"] == "TEST_PROVIDER"

        for person, receipt, grade in (
            (owner, first_receipt, "LIVE_PROVIDER"),
            (colleague, second_receipt, "TEST_PROVIDER"),
        ):
            candidate = receipt["candidates"][0]
            preview = client.post(
                f"/api/v1/journeys/{journey_id}/bootstrap-discovery/preview",
                headers=headers(person), json={
                    "receipt_id": receipt["receipt_id"],
                    "candidate_id": candidate["candidate_id"],
                    "public_map_consent": True,
                    "latitude": candidate["latitude"],
                    "longitude": candidate["longitude"],
                    "coordinate_system": "WGS84",
                },
            )
            assert preview.status_code == 200, preview.text
            assert preview.json()["provider_provenance"] == grade
            saved = client.post(
                f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
                headers=headers(person), json={
                    "receipt_id": receipt["receipt_id"],
                    "candidate_id": candidate["candidate_id"],
                },
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["source_provenance"] == grade
        assert len(attempts) == 2
    finally:
        for key, original in previous.items():
            object.__setattr__(settings, key, original)
