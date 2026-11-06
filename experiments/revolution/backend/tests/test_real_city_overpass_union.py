from __future__ import annotations

import httpx
from urllib.parse import unquote_plus

from app.integrations.discovery.real_city import OverpassPOIProvider


def test_gallery_search_emits_valid_overpass_union_for_each_tag_family() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content.decode())
        return httpx.Response(200, json={"elements": []})

    provider = OverpassPOIProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://overpass.example/api",
    )
    try:
        candidates, attempts = provider.search_with_evidence(
            "gallery cultural centre",
            latitude=35.0,
            longitude=135.7,
            radius_m=5000,
            limit=10,
        )
    finally:
        provider.close()

    query = unquote_plus(requests[0])
    assert candidates == []
    assert attempts[0]["answered"] is True
    assert 'nwr["tourism"="gallery"](around:5000,35.0,135.7);' in query
    assert 'nwr["amenity"="arts_centre"](around:5000,35.0,135.7);' in query
    assert '"tourism"="gallery"|"amenity"="arts_centre"' not in query
