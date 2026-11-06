"""Provider-backed accommodation discovery for any city.

The captured fixture only knows Tianjin and ``LiveAccommodationProvider``
returns nothing, so a real Xiamen trip could never compare stays.

Strategy (all real providers, no invention):

1. Resolve the city anchor with Photon.
2. Ask Overpass (with mirror rotation) for ``tourism=hotel|guest_house|hostel``.
3. If Overpass is unavailable/empty, fall back to Nominatim free-text search,
   which returns real OSM hotel entries with coordinates.

Only provider-backed facts are surfaced: name, coordinates, OSM identity.
**Price is not knowable from OSM** and is therefore reported as unknown rather
than invented.

Every attempt is also *recorded* (`attempts`), because this provider's failure mode is
silent: mirrors that time out are skipped by design, so `discover()` can return an empty
tuple after two minutes and the caller cannot tell "Xiamen has no hotels in OSM" from
"three mirrors were unreachable". The timings (`app.core.phase_timing`) and the attempt
list are how that distinction survives to the response instead of dying in a bare
`except Exception: continue`. Neither changes what a call does - no timeout here is
lengthened or shortened by this instrumentation.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.core.phase_timing import phase
from app.integrations.provider_config import (
    ExternalProviderSettings,
    trust_environment_for_public_providers,
)

_provider_settings = ExternalProviderSettings()

_UA = {"User-Agent": "CityNexusEAC/1.0 (trial)"}

# Public Overpass mirrors, tried in order.  They are frequently unavailable or
# slow, which is exactly why a fallback exists.
_OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)


class OsmAccommodationProvider:
    provider_key = "osm-overpass"
    provenance = "LIVE_PROVIDER"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        # ORD-0057 item 5: this read `max(30.0, settings.real_city_timeout_seconds)`, which
        # silently discarded the configured value for every setting at or below 30s - the
        # default is 12, so "make the provider timeout shorter" did nothing at all and the
        # only way to change it was to know about the hidden floor.  The setting is now the
        # timeout; a slower mirror is reported as a timeout, which is a true answer, rather
        # than being hidden behind a constant nobody configured.
        self._client = client or httpx.Client(
            timeout=settings.real_city_timeout_seconds,
            trust_env=trust_environment_for_public_providers(),
        )
        self._owned = client is None
        # One entry per network attempt, in the order they were made. Public so the stay
        # path can carry it into the response when it has nothing else to say.
        self.attempts: list[dict[str, Any]] = []

    def _note(self, step: str, target: str, outcome: str, detail: str = "") -> None:
        self.attempts.append({"step": step, "target": target, "outcome": outcome, "detail": detail})

    def close(self) -> None:
        if self._owned:
            self._client.close()

    # -- anchor ------------------------------------------------------------
    def _city_label(self, city_code: str) -> str:
        return str(city_code).split("-")[-1].replace("_", " ").strip()

    def _geocode(self, city_code: str) -> tuple[float, float] | None:
        label = self._city_label(city_code)
        if not label or label.upper() in {"CNX", "DEMO"}:
            self._note("geocode", "skipped", "no_city_label", f"label={label!r}")
            return None
        with phase("provider.photon", detail=f"label={label}") as span:
            try:
                r = self._client.get(
                    f"{_provider_settings.photon_base_url.rstrip('/')}/api/",
                    params={"q": label, "limit": 1},
                    headers=_UA,
                )
                r.raise_for_status()
                feats = (r.json() or {}).get("features") or []
                if feats:
                    lon, lat = feats[0]["geometry"]["coordinates"][:2]
                    self._note("geocode", "photon", "ok", f"label={label}")
                    return float(lat), float(lon)
                if span is not None:
                    span.outcome = "empty"
                self._note("geocode", "photon", "empty", f"label={label}")
            except Exception as exc:  # noqa: BLE001 - the anchor is optional; recorded now
                if span is not None:
                    span.outcome = type(exc).__name__
                self._note("geocode", "photon", type(exc).__name__, f"label={label}")
        return None

    # -- providers ---------------------------------------------------------
    def _overpass(self, lat: float, lon: float) -> list[dict[str, Any]]:
        query = (
            f"[out:json][timeout:45];("
            f'node(around:6000,{lat},{lon})["tourism"~"hotel|guest_house|hostel"];'
            f'way(around:6000,{lat},{lon})["tourism"~"hotel|guest_house|hostel"];'
            f");out center 15;"
        )
        with phase("provider.overpass", detail=f"around=6000m mirrors={len(_OVERPASS_MIRRORS)}") as outer:
            for index, url in enumerate(_OVERPASS_MIRRORS, start=1):
                host = url.split("//", 1)[-1].split("/", 1)[0]
                with phase(f"provider.overpass.mirror[{index}]", detail=host) as span:
                    try:
                        r = self._client.post(url, data={"data": query}, headers=_UA)
                        r.raise_for_status()
                        els = (r.json() or {}).get("elements") or []
                    except Exception as exc:  # noqa: BLE001 - rotation to the next mirror
                        if span is not None:
                            span.outcome = type(exc).__name__
                        self._note("overpass", host, type(exc).__name__)
                        continue
                    if not els:
                        if span is not None:
                            span.outcome = "empty"
                        self._note("overpass", host, "empty")
                        continue
                    if outer is not None:
                        outer.outcome = f"ok@{host}"
                    self._note("overpass", host, "ok", f"elements={len(els)}")
                    return els
            if outer is not None and outer.outcome == "ok":
                outer.outcome = "empty"
        return []

    def _nominatim(self, city_code: str) -> list[dict[str, Any]]:
        """Real OSM hotel entries with coordinates, via free-text search."""

        label = self._city_label(city_code)
        if not label:
            return []
        out: list[dict[str, Any]] = []
        with phase("provider.nominatim", detail=f"label={label}") as outer:
            for index, q in enumerate((f"酒店 {label}", f"hotel {label}"), start=1):
                with phase(f"provider.nominatim.query[{index}]", detail=q) as span:
                    try:
                        r = self._client.get(
                            "https://nominatim.openstreetmap.org/search",
                            params={"q": q, "format": "json", "limit": 15, "extratags": 1},
                            headers=_UA,
                        )
                        r.raise_for_status()
                        rows = r.json() or []
                    except Exception as exc:  # noqa: BLE001 - the second query is the fallback
                        if span is not None:
                            span.outcome = type(exc).__name__
                        self._note("nominatim", q, type(exc).__name__)
                        continue
                    if not rows:
                        if span is not None:
                            span.outcome = "empty"
                        self._note("nominatim", q, "empty")
                for row in rows:
                    kind = str(row.get("type") or "")
                    klass = str(row.get("class") or "")
                    if not (
                        kind in {"hotel", "guest_house", "hostel", "motel"}
                        or klass == "tourism"
                        or (row.get("extratags") or {}).get("tourism")
                    ):
                        continue
                    display = str(row.get("display_name") or "").split(",")
                    # Many OSM hotels in China have no ``name`` tag; taking the
                    # street-level part of the address is honest and keeps the
                    # entries distinguishable instead of collapsing them all to
                    # a single "酒店".
                    # e.g. "酒店, 岐山北二路, 禾山街道" -> "酒店 · 岐山北二路"
                    fallback_name = " · ".join(part.strip() for part in display[:2] if part.strip())
                    if fallback_name and display and display[0].strip() == fallback_name:
                        fallback_name = ""
                    try:
                        out.append(
                            {
                                "name": str(row.get("name") or "").strip()
                                or fallback_name
                                or f"OSM hotel {row.get('osm_id')}",
                                "lat": float(row["lat"]),
                                "lon": float(row["lon"]),
                                "osm_id": f"nominatim/{row.get('osm_id')}",
                                "tourism": (row.get("extratags") or {}).get("tourism") or kind,
                            }
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                if out:
                    break
        return out

    # -- public ------------------------------------------------------------
    def discover(self, *, city_code: str, party_size: int, nights: int) -> tuple[Any, ...]:
        from app.stay_mobility import AccommodationOfferDraft

        anchor = self._geocode(city_code)
        drafts: list[AccommodationOfferDraft] = []
        seen: set[str] = set()

        def add(name: str, lat: float, lon: float, ref: str, tourism: str) -> None:
            # Deduplicate on OSM identity, not on name: OSM often names several
            # distinct properties "酒店", and merging them would hide real
            # options from the traveller.
            key = str(ref).casefold()
            if not ref or key in seen:
                return
            seen.add(key)
            drafts.append(
                AccommodationOfferDraft(
                    external_offer_id=f"osm:{ref}",
                    property_name=name,
                    latitude=lat,
                    longitude=lon,
                    # OSM carries no price. Zero + explicit unknown beats a
                    # fabricated nightly rate.
                    nightly_amount_minor=0,
                    room_type=tourism or "hotel",
                    refundable=False,
                    evidence_confidence=0.6,
                    coordinate_crs="WGS84",
                    luggage_drop={},
                )
            )

        if anchor is not None:
            for el in self._overpass(*anchor):
                tags = el.get("tags") or {}
                name = str(tags.get("name:zh") or tags.get("name") or "").strip()
                if el.get("type") == "node":
                    elat, elon = el.get("lat"), el.get("lon")
                else:
                    c = el.get("center") or {}
                    elat, elon = c.get("lat"), c.get("lon")
                if name and elat is not None and elon is not None:
                    add(name, float(elat), float(elon),
                        f"{el.get('type')}/{el.get('id')}", str(tags.get("tourism") or "hotel"))

        # Overpass mirrors carry incomplete regional data, so top up from
        # Nominatim whenever the Overpass result is thin.  Both are real OSM
        # data; neither invents a property.
        if len(drafts) < 4:
            for row in self._nominatim(city_code):
                add(row["name"], row["lat"], row["lon"], row["osm_id"], row["tourism"])

        return tuple(drafts)
