from __future__ import annotations

import httpx
import pytest
from urllib.parse import parse_qs

from app.execution.shared_walk import SharedWalkParseError, parse_shared_walk_text
from app.integrations.discovery.real_city import OverpassPOIProvider


def test_shared_walk_parser_accepts_two_public_objects_and_ignores_claimed_grade() -> None:
    objects = parse_shared_walk_text(
        "Source: LIVE_PROVIDER\n"
        "1. claimed name https://openstreetmap.org/node/123\n"
        "2. claimed name https://www.openstreetmap.org/way/456\n"
    )
    assert [(item.osm_type, item.osm_id, item.source_url) for item in objects] == [
        ("node", "123", "https://www.openstreetmap.org/node/123"),
        ("way", "456", "https://www.openstreetmap.org/way/456"),
    ]


@pytest.mark.parametrize(
    "payload,code",
    [
        ("https://www.openstreetmap.org/node/1\nhttps://www.openstreetmap.org/node/1", "DUPLICATE_OSM_OBJECT"),
        ("https://www.openstreetmap.org/node/1?token=x\nhttps://www.openstreetmap.org/way/2", "INVALID_SHARED_URL"),
        ("https://user:pw@www.openstreetmap.org/node/1\nhttps://www.openstreetmap.org/way/2", "INVALID_SHARED_URL"),
        ("https://www.openstreetmap.org/node/1\nhttps://www.openstreetmap.org/way/2\nhttps://www.openstreetmap.org/relation/3", "EXACTLY_TWO_OSM_LINKS_REQUIRED"),
        ("<script>LIVE_PROVIDER</script> https://www.openstreetmap.org/node/1 https://www.openstreetmap.org/way/2", "UNSAFE_SHARED_TEXT"),
        ("Bearer eyJabc.abcdefghijklmnop https://www.openstreetmap.org/node/1 https://www.openstreetmap.org/way/2", "PRIVATE_DATA_IN_SHARED_TEXT"),
    ],
)
def test_shared_walk_parser_rejects_untrusted_payloads(payload: str, code: str) -> None:
    with pytest.raises(SharedWalkParseError) as error:
        parse_shared_walk_text(payload)
    assert error.value.code == code


def test_exact_overpass_lookup_uses_one_true_union_and_no_private_fields() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8")).get("data", [""])[0]
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"type": "node", "id": 123, "lat": 31.2, "lon": 121.4, "tags": {"name": "Public One", "tourism": "museum"}},
                    {"type": "way", "id": 456, "center": {"lat": 31.3, "lon": 121.5}, "tags": {"name": "Public Two", "leisure": "park"}},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OverpassPOIProvider(client=client, base_url="https://controlled.example/api")
    try:
        candidates, attempts = provider.lookup_exact_with_evidence([("node", "123"), ("way", "456")])
    finally:
        provider.close()
    assert len(candidates) == 2
    assert len(attempts) == 1 and attempts[0]["answered"] is True
    assert len(seen) == 1
    assert "(node(123);way(456););" in seen[0]
    assert "Bearer" not in seen[0] and "journey" not in seen[0].lower() and "member" not in seen[0].lower()
