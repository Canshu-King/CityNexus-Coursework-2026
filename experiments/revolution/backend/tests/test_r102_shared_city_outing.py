from __future__ import annotations

import json
import math

import httpx
import pytest
from urllib.parse import parse_qs

from app.db.models import RealPlaceCandidate
from app.execution.bootstrap_discovery import (
    _candidate_payload,
    _public_access_state,
    _public_candidate,
    _row_provider_provenance,
)
from app.execution.shared_walk import SharedWalkParseError, parse_shared_walk_text
from app.integrations.discovery.real_city import DiscoveryCandidate, OverpassPOIProvider


def _candidate(*, latitude: object, longitude: object, tags: dict[str, object] | None = None) -> DiscoveryCandidate:
    place_tags = {"name": "R102 public place", "tourism": "museum", **(tags or {})}
    return DiscoveryCandidate(
        provider_key="overpass",
        external_id="overpass:node/102001",
        name="R102 public place",
        category="museum",
        latitude=latitude,  # type: ignore[arg-type]
        longitude=longitude,  # type: ignore[arg-type]
        tags=("museum", "real_city"),
        opening_hours={"status": "UNKNOWN"},
        metadata={"tags": place_tags},
    )


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (math.nan, 121.47),
        (math.inf, 121.47),
        (31.23, math.nan),
        (-91.0, 121.47),
        (31.23, 181.0),
        ("not-a-number", 121.47),
    ],
)
def test_r102_public_candidate_rejects_invalid_coordinates_before_persistence(latitude: object, longitude: object) -> None:
    """NaN, infinity, malformed and out-of-range provider centers are never actionable."""

    assert _public_candidate(_candidate(latitude=latitude, longitude=longitude), radius_m=5000) is False


@pytest.mark.parametrize(
    "tags",
    [
        {"access": "private"},
        {"access:conditional": "private @ (Mo-Fr)"},
        {"visibility": "restricted"},
        {"public": "no"},
    ],
)
def test_r102_public_candidate_does_not_promote_restricted_access(tags: dict[str, object]) -> None:
    assert _public_candidate(_candidate(latitude=31.23, longitude=121.47, tags=tags), radius_m=5000) is False


def test_r102_indoor_only_access_is_accepted_only_as_unverified() -> None:
    candidate = _candidate(latitude=31.23, longitude=121.47, tags={"access": "indoor-only"})
    assert _public_candidate(candidate, radius_m=5000) is True
    assert _public_access_state({"access": "indoor-only"}) == "INDOOR_ACCESS_UNVERIFIED"


def test_r102_exact_lookup_skips_malformed_centers_without_repairing_them() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(parse_qs(request.content.decode("utf-8"))["data"][0])
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"type": "node", "id": 102001, "lat": "nan", "lon": 121.47, "tags": {"name": "Bad"}},
                    {"type": "way", "id": 102002, "center": {"lat": 91, "lon": 121.48}, "tags": {"name": "Bad center"}},
                ]
            },
        )

    provider = OverpassPOIProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://controlled.example/api",
    )
    try:
        candidates, attempts = provider.lookup_exact_with_evidence(
            [("node", "102001"), ("way", "102002")]
        )
    finally:
        provider.close()
    assert attempts[0]["answered"] is True
    assert candidates == []
    assert len(seen) == 1


def test_r102_reused_identity_projects_fresh_member_observation_and_grade() -> None:
    row = RealPlaceCandidate(
        id="candidate-102",
        journey_id="journey-102",
        provider_key="overpass",
        external_id="overpass:node/102001",
        city_code="CN-SHANGHAI",
        name="Old shared name",
        category="museum",
        latitude=31.20,
        longitude=121.40,
        tags_json='["museum"]',
        opening_hours_json=json.dumps({"status": "UNKNOWN"}),
        metadata_json=json.dumps(
            {
                "provider_provenance": "LIVE_PROVIDER",
                "osm_tags": {"name": "Old shared name", "tourism": "museum"},
            }
        ),
        captured_at="2026-10-11T00:00:00+00:00",
        expires_at="2026-10-12T00:00:00+00:00",
        distance_m=None,
        evidence_json=json.dumps(
            {
                "bootstrap": True,
                "osm_type": "node",
                "osm_id": "102001",
                "bootstrap_scopes": [
                    {
                        "member_id": "viewer-member",
                        "traveler_id": "viewer-traveler",
                        "receipt_id": "receipt-102",
                        "source_kind": "SHARED_PUBLIC_REFERENCE",
                        "provider_provenance": "TEST_PROVIDER",
                        "observed_name": "Fresh viewer name",
                        "observed_category": "gallery",
                        "latitude": 31.30,
                        "longitude": 121.50,
                        "captured_at": "2026-10-11T01:00:00+00:00",
                        "opening_hours": {"status": "UNKNOWN"},
                        "osm_tags": {"name": "Fresh viewer name", "tourism": "gallery"},
                        "public_access_state": "PUBLIC_ACCESS_UNVERIFIED",
                    }
                ],
            }
        ),
        materialized_place_id=None,
        status="BOOTSTRAP_PRIVATE",
    )

    payload = _candidate_payload(
        row,
        member_id="viewer-member",
        traveler_id="viewer-traveler",
    )
    assert payload["name"] == "Fresh viewer name"
    assert payload["category"] == "gallery"
    assert payload["latitude"] == 31.30
    assert payload["longitude"] == 121.50
    assert payload["opening_observed_at"] == "2026-10-11T01:00:00+00:00"
    assert payload["provider_provenance"] == "TEST_PROVIDER"
    assert _row_provider_provenance(row, scope={"provider_provenance": "TEST_PROVIDER"}) == "TEST_PROVIDER"
    assert _row_provider_provenance(row, scope={}) == "UNVERIFIED"


def test_r102_python_parser_matches_browser_canonical_url_boundary() -> None:
    objects = parse_shared_walk_text(
        "HTTPS://OPENSTREETMAP.ORG/node/102001.\n"
        "https://www.openstreetmap.org/way/102002"
    )
    assert [item.source_url for item in objects] == [
        "https://www.openstreetmap.org/node/102001",
        "https://www.openstreetmap.org/way/102002",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "https://www.openstreetmap.org/node/102001%2Fextra\nhttps://www.openstreetmap.org/way/102002",
        "https://www.openstreetmap.org/node%2F102001\nhttps://www.openstreetmap.org/way/102002",
        "https://www.openstreetmap.org/node/102001?next=%2Fway%2F2\nhttps://www.openstreetmap.org/way/102002",
        "https://www.openstreetmap.org/node/102001#%2Fway%2F2\nhttps://www.openstreetmap.org/way/102002",
    ],
)
def test_r102_parser_rejects_encoded_separators_and_query_fragment(payload: str) -> None:
    with pytest.raises(SharedWalkParseError):
        parse_shared_walk_text(payload)


def test_r102_native_selection_reads_member_scope_not_first_shared_row(monkeypatch):
    """A later member must not materialize another member's old coordinates."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app.execution.bootstrap_discovery import BootstrapDiscoveryError, select_bootstrap_candidate
    from uuid import UUID

    # A row with A's normalized opening looks eligible, but B's independent
    # observation is UNKNOWN; B's selection must stop before any Place write.
    row = RealPlaceCandidate(
        id="00000000-0000-4000-8000-000000000102",
        journey_id="journey-102",
        provider_key="overpass",
        external_id="overpass:node/102001",
        city_code="CN-SHANGHAI",
        name="A stale public name",
        category="museum",
        latitude=31.20,
        longitude=121.40,
        tags_json='["museum"]',
        opening_hours_json='{"status":"NORMALIZED_VALID"}',
        metadata_json='{"cost_evidence":{"status":"UNKNOWN"}}',
        captured_at="2026-10-11T00:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
        evidence_json=json.dumps({
            "bootstrap": True, "osm_type": "node", "osm_id": "102001",
            "bootstrap_scopes": [{
                "member_id": "B", "traveler_id": "traveler-B",
                "receipt_id": "00000000-0000-4000-8000-000000000202",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "provider_provenance": "TEST_PROVIDER",
                "observed_name": "B current public name",
                "observed_category": "gallery",
                "latitude": 31.30, "longitude": 121.50,
                "opening_hours": {"status":"UNKNOWN"},
                "captured_at": "2026-10-11T01:00:00+00:00",
                "public_access_state":"PUBLIC_ACCESS_UNVERIFIED",
            }]
        }),
        materialized_place_id=None, status="BOOTSTRAP_PRIVATE",
    )
    session = MagicMock()
    session.scalar.side_effect = [None, row]
    journey = SimpleNamespace(id=row.journey_id, status="DRAFT", active_plan_version_id=None)
    with pytest.raises(BootstrapDiscoveryError) as error:
        select_bootstrap_candidate(
            session, journey,
            member=SimpleNamespace(id="B"), traveler_id="traveler-B",
            receipt_id=UUID("00000000-0000-4000-8000-000000000202"),
            candidate_id=UUID(row.id),
        )
    assert error.value.code == "BOOTSTRAP_OPENING_UNVERIFIED"
    assert not session.add.called
    assert not session.flush.called


def test_r103_atomic_friend_import_requires_exact_member_receipt_before_writes() -> None:
    """Ordinary discovery candidates cannot be laundered into friend-import authority."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from uuid import UUID

    from app.execution.bootstrap_discovery import (
        BootstrapDiscoveryError,
        save_bootstrap_shared_walk_pocket_walk,
    )

    candidate_id = "00000000-0000-4000-8000-000000001031"
    receipt_id = "00000000-0000-4000-8000-000000001032"
    first = SimpleNamespace(
        id=candidate_id, journey_id="r103-journey", provider_key="overpass",
        status="BOOTSTRAP_PRIVATE",
        evidence_json=json.dumps({
            "bootstrap": True,
            "bootstrap_scopes": [{
                "member_id": "owner", "traveler_id": "owner-traveler",
                "receipt_id": receipt_id,
                "source_kind": "MANUAL_PUBLIC_CENTER",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }],
        }),
    )
    session = MagicMock()
    session.scalar.return_value = first
    with pytest.raises(BootstrapDiscoveryError) as error:
        save_bootstrap_shared_walk_pocket_walk(
            session,
            SimpleNamespace(id="r103-journey", status="DRAFT", active_plan_version_id=None),
            member=SimpleNamespace(id="owner"),
            traveler_id="owner-traveler",
            receipt_id=UUID(receipt_id),
            first_candidate_id=UUID(candidate_id),
            second_candidate_id=UUID("00000000-0000-4000-8000-000000001033"),
            max_walk_minutes=None,
        )
    assert error.value.code == "SHARED_WALK_EXACT_RECEIPT_REQUIRED"
    session.execute.assert_not_called()
    session.flush.assert_not_called()
    session.add.assert_not_called()
