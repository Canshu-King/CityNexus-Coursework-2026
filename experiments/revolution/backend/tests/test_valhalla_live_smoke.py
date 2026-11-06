from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.integrations.mobility.valhalla_http import ValhallaHTTPMobilityProvider


@pytest.mark.integration
@pytest.mark.skipif(
    not settings.live_valhalla_url,
    reason="CITYNEXUS_LIVE_VALHALLA_URL is not configured",
)
def test_opt_in_live_valhalla_route_smoke():
    points = {
        "origin": {"latitude": 40.7484, "longitude": -73.9857},
        "destination": {"latitude": 40.7527, "longitude": -73.9772},
    }
    with httpx.Client(timeout=10.0) as client:
        provider = ValhallaHTTPMobilityProvider(
            client=client,
            base_url=settings.live_valhalla_url or "",
            place_lookup=points.__getitem__,
        )
        estimate = provider.estimate("origin", "destination")
    assert estimate.duration_min >= 0
