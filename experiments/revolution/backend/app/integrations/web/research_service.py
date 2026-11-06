from __future__ import annotations

import re
from typing import Any

from app.db.models import Journey, Place


def build_source_query(place: Place, journey: Journey) -> str:
    """Build a server-owned query; the Agent supplies neither name nor URL."""
    city = str(journey.city_code or "").replace("_", " ")
    identity = str(place.external_id or "").replace(":", " ")
    return f'"{place.name}" {city} official opening hours admission fee {identity}'.strip()


def normalize_identity_text(value: Any) -> str:
    return re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", "", str(value or "").casefold())


def corroborate_observation(*, place: Place, observation: Any, page_text: str, provider_tags: dict[str, Any], nominatim_row: dict[str, Any]) -> dict[str, Any]:
    """Require exact-name evidence plus an independent server-known anchor."""
    page = normalize_identity_text(page_text)
    names = [place.name, provider_tags.get("name"), provider_tags.get("official_name")]
    name = next((str(value) for value in names if value and normalize_identity_text(value) in page), None)
    anchors: list[str] = []
    for key in ("phone", "contact:phone", "addr:postcode", "postcode", "addr:city", "addr:street"):
        value = str(provider_tags.get(key) or nominatim_row.get("address", {}).get(key) or "").strip()
        if value and normalize_identity_text(value) in page:
            anchors.append(key)
    exact_url = str(provider_tags.get("website") or provider_tags.get("contact:website") or provider_tags.get("url") or "").strip()
    if exact_url and exact_url.rstrip("/") == str(observation.url).rstrip("/"):
        anchors.append("exact_provider_website_tag")
    if name and anchors:
        return {"status": "CORROBORATED_OFFICIAL_SOURCE", "matched_provider_entity": True, "provider_entity": place.external_id, "matched_name": name, "matched_anchors": anchors, "web_observation_id": observation.id}
    return {"status": "INSUFFICIENT_IDENTITY_EVIDENCE", "matched_provider_entity": False, "provider_entity": place.external_id, "matched_name": name, "matched_anchors": anchors, "web_observation_id": observation.id}
