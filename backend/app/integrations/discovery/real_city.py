from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.provider_config import (
    ExternalProviderSettings,
    trust_environment_for_public_providers,
)
from app.db.models import (
    AccommodationOfferSnapshot,
    Activity,
    CityService,
    Commitment,
    Journey,
    JourneyTripContext,
    Place,
    ProviderCall,
    RealPlaceCandidate,
    StayJourneyOption,
    WebObservation,
)


LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()



def _place_metadata(
    candidate: Any, mode: str, cost_evidence: dict[str, Any], opening_hours: dict[str, Any]
) -> dict[str, Any]:
    """Metadata for a materialised discovery Place.

    ``cost_minor`` is required by the compose path.  When the provider gives no
    price we record an explicit **zero-cost unknown** and keep the honest
    ``cost_evidence`` alongside it, rather than inventing a price or refusing to
    plan at all: an OSM attraction genuinely has no known ticket price.
    """

    metadata: dict[str, Any] = {
        **candidate.metadata,
        "discovery_mode": mode,
        "cost_evidence": cost_evidence,
    }
    if mode == "offline_snapshot":
        # The row-level source marker the catalogue door promises.  `anywhere_capability
        # ._source_kind` reads `metadata_json.provenance`, and without this key an offline row only
        # classifies as `OFFLINE_SNAPSHOT` through that function's catch-all - true by accident, and
        # indistinguishable from an unaccounted-for provider.  Written for this mode only: giving the
        # replay and live legs a mark here would *strengthen* their class, which the classifier
        # exists to refuse.
        metadata["provenance"] = "OFFLINE_SNAPSHOT"
    status = str(cost_evidence.get("status") or "UNKNOWN").upper()
    if status == "KNOWN" and cost_evidence.get("amount_minor") is not None:
        metadata["cost_minor"] = int(cost_evidence["amount_minor"])
    else:
        metadata.setdefault("cost_minor", 0)
    metadata["price_known"] = status == "KNOWN"
    if not opening_hours:
        metadata.setdefault("opening_hours", {"status": "UNKNOWN"})
    return metadata

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _external_id(provider: str, value: str) -> str:
    return f"{provider}:{value}"[:180]


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


_TIME_PATTERN = re.compile(r"(?P<open>\d{1,2}(?::\d{2})?)\s*[-–]\s*(?P<close>\d{1,2}(?::\d{2})?)")
_DAY_PATTERN = re.compile(
    r"^(?P<days>(?:Mo|Mon|Tu|Tue|We|Wed|Th|Thu|Fr|Fri|Sa|Sat|Su|Sun)"
    r"(?:\s*[-–]\s*(?:Mo|Mon|Tu|Tue|We|Wed|Th|Thu|Fr|Fri|Sa|Sat|Su|Sun))?)\s+(?P<times>.+)$",
    re.IGNORECASE,
)
_DAY_NAMES = {
    "mo": "Mo",
    "mon": "Mo",
    "tu": "Tu",
    "tue": "Tu",
    "we": "We",
    "wed": "We",
    "th": "Th",
    "thu": "Th",
    "fr": "Fr",
    "fri": "Fr",
    "sa": "Sa",
    "sat": "Sa",
    "su": "Su",
    "sun": "Su",
}


def _expand_days(raw: str) -> list[str]:
    parts = [item.strip().lower() for item in re.split(r"\s*[-–]\s*", raw)]
    normalized = [_DAY_NAMES[item] for item in parts]
    if len(normalized) == 1:
        return normalized
    order = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    start, end = order.index(normalized[0]), order.index(normalized[1])
    if start <= end:
        return order[start : end + 1]
    return order[start:] + order[: end + 1]


def normalize_opening_hours(raw: Any) -> dict[str, Any]:
    """Normalize common OSM hours without treating unknown text as all-day."""

    if isinstance(raw, dict):
        if raw.get("status") in {
            "NORMALIZED_VALID",
            "CLOSED",
            "UNKNOWN",
            "RAW_UNPARSED",
            "OFFICIAL_WEB_VERIFIED",
            "VERIFIED_PROVIDER",
            "CONFLICTING",
        }:
            return dict(raw)
        raw_text = raw.get("raw") or (
            f"{raw.get('open')}-{raw.get('close')}" if raw.get("open") and raw.get("close") else ""
        )
    else:
        raw_text = str(raw or "").strip()
    raw_text = str(raw_text).strip()
    if not raw_text:
        return {"status": "UNKNOWN", "raw": None, "intervals": []}
    if raw_text.casefold() in {"off", "closed", "休息", "关闭"}:
        return {"status": "CLOSED", "raw": raw_text, "intervals": []}
    if raw_text.casefold() in {"24/7", "always", "全天"}:
        return {
            "status": "NORMALIZED_VALID",
            "raw": raw_text,
            "open": "00:00",
            "close": "24:00",
            "intervals": [{"open": "00:00", "close": "24:00"}],
        }
    intervals: list[dict[str, Any]] = []
    weekday_intervals: list[dict[str, Any]] = []
    segments = [segment.strip() for segment in re.split(r"\s*;\s*", raw_text) if segment.strip()]
    for segment in segments:
        day_match = _DAY_PATTERN.match(segment)
        days = _expand_days(day_match.group("days")) if day_match else ["*"]
        times_text = day_match.group("times") if day_match else segment
        segment_intervals: list[dict[str, str]] = []
        for match in _TIME_PATTERN.finditer(times_text):
            open_value = match.group("open")
            close_value = match.group("close")
            if ":" not in open_value:
                open_value = f"{open_value}:00"
            if ":" not in close_value:
                close_value = f"{close_value}:00"
            try:
                if not (0 <= int(open_value[:2]) <= 23 and 0 <= int(close_value[:2]) <= 24):
                    continue
            except ValueError:
                continue
            interval = {"open": open_value, "close": close_value}
            segment_intervals.append(interval)
            intervals.append({**interval, "days": days})
        if segment_intervals:
            weekday_intervals.append({"days": days, "intervals": segment_intervals})
    if not intervals:
        return {"status": "RAW_UNPARSED", "raw": raw_text, "intervals": []}
    first = intervals[0]
    return {
        "status": "NORMALIZED_VALID",
        "raw": raw_text,
        "open": first["open"],
        "close": first["close"],
        "intervals": intervals,
        "weekday_intervals": weekday_intervals,
    }


def cost_evidence_from_tags(tags: dict[str, Any] | None, *, source: str) -> dict[str, Any]:
    values = tags or {}
    currency = str(values.get("currency") or values.get("currency:code") or "CNY").upper()
    fee = values.get("fee")
    charge = values.get("charge") or values.get("price")
    if isinstance(fee, str) and fee.casefold() in {"no", "false", "free", "免费"}:
        return {"status": "KNOWN", "amount_minor": 0, "currency": currency, "source": source}
    candidate = charge if charge is not None else fee
    if candidate is not None:
        candidate_text = str(candidate)
        suffix = re.search(r"\b([A-Za-z]{3})\b", candidate_text)
        symbol_currency = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "CNY"}
        for symbol, code in symbol_currency.items():
            if symbol in candidate_text:
                currency = code
                break
        if suffix:
            currency = suffix.group(1).upper()
        match = re.search(r"(\d+(?:\.\d+)?)", candidate_text)
        if match:
            return {
                "status": "KNOWN",
                "amount_minor": round(float(match.group(1)) * 100),
                "currency": currency,
                "source": source,
            }
    return {"status": "UNKNOWN", "amount_minor": None, "currency": currency, "source": source}


def _load_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _replay_snapshot_path() -> Path:
    root = Path(__file__).resolve().parents[4]
    configured = Path(settings.real_city_replay_snapshot_path)
    return configured if configured.is_absolute() else root / configured


def _load_replay_snapshot() -> dict[str, Any]:
    try:
        value = json.loads(_replay_snapshot_path().read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


_OFFICIAL_EVIDENCE_CLASSES = {
    "OFFICIAL_WEB",
    "OFFICIAL_OPERATOR",
    "OFFICIAL_TICKETING",
    "MUNICIPAL_AUTHORITY",
}


def _promote_captured_evidence(
    candidate_external_id: str,
    completion: Any,
    *,
    opening_hours: dict[str, Any],
    cost_evidence: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Promote only facts tied to captured, official source observations."""

    if not isinstance(completion, dict):
        return opening_hours, cost_evidence, None
    if str(completion.get("candidate_external_id") or "") != candidate_external_id:
        return opening_hours, cost_evidence, None
    observations = completion.get("observations")
    if not isinstance(observations, list):
        return opening_hours, cost_evidence, None
    captured = {
        str(item.get("id")): item
        for item in observations
        if isinstance(item, dict)
        and item.get("id")
        and item.get("url")
        and str(item.get("source_class") or "") in _OFFICIAL_EVIDENCE_CLASSES
    }
    if not captured:
        return opening_hours, cost_evidence, None

    promoted: dict[str, Any] = {
        "candidate_external_id": candidate_external_id,
        "observations": [captured[key] for key in sorted(captured)],
        "promoted_facts": {},
        "authority": "CAPTURED_OFFICIAL_SOURCE_REQUIRED",
    }
    facts = completion.get("facts") if isinstance(completion.get("facts"), dict) else {}
    for fact_type, current in (
        ("opening_hours", opening_hours),
        ("cost_evidence", cost_evidence),
    ):
        proposed = facts.get(fact_type)
        if not isinstance(proposed, dict):
            continue
        source_ids = proposed.get("source_observation_ids")
        if not isinstance(source_ids, list) or not source_ids:
            continue
        if any(str(item) not in captured for item in source_ids):
            continue
        if proposed.get("status") == "CONFLICTING":
            promoted["promoted_facts"][fact_type] = {
                **proposed,
                "status": "CONFLICTING",
                "source_observation_ids": [str(item) for item in source_ids],
            }
            continue
        if fact_type == "opening_hours":
            normalized = normalize_opening_hours(proposed)
            if normalized.get("status") in {"UNKNOWN", "RAW_UNPARSED", "CLOSED"}:
                continue
            value = {
                **normalized,
                "status": "OFFICIAL_WEB_VERIFIED",
                "source_observation_ids": [str(item) for item in source_ids],
                "source_urls": [captured[str(item)]["url"] for item in source_ids],
            }
        else:
            if proposed.get("amount_minor") is None and proposed.get("status") != "CONFLICTING":
                continue
            value = {
                **proposed,
                "status": "VERIFIED_OFFICIAL_WEB",
                "source_observation_ids": [str(item) for item in source_ids],
                "source_urls": [captured[str(item)]["url"] for item in source_ids],
            }
        promoted["promoted_facts"][fact_type] = value
        if fact_type == "opening_hours":
            opening_hours = value
        else:
            cost_evidence = value
    return opening_hours, cost_evidence, promoted


def discovery_anchor_options(session: Session, journey: Journey) -> list[dict[str, Any]]:
    """Return server-known discovery centers in product precedence order."""

    activities: list[Activity] = []
    if journey.active_plan_version_id:
        activities = list(
            session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == journey.active_plan_version_id)
                .order_by(Activity.sequence_no)
            ).all()
        )
    options: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(
        key: str,
        *,
        source: str,
        latitude: Any,
        longitude: Any,
        location: str | None = None,
        activity_id: str | None = None,
        place_id: str | None = None,
        at: str | None = None,
    ) -> None:
        if latitude is None or longitude is None:
            return
        try:
            point = {"latitude": float(latitude), "longitude": float(longitude)}
        except (TypeError, ValueError):
            return
        if key in seen:
            return
        seen.add(key)
        options.append(
            {
                "anchor_key": key,
                "source": source,
                **point,
                "location": location,
                "activity_id": activity_id,
                "place_id": place_id,
                "at": at,
            }
        )

    ordered = sorted(
        activities,
        key=lambda item: (0 if item.id == journey.current_activity_id else 1, item.sequence_no),
    )
    for activity in ordered:
        if not activity.place_id:
            continue
        place = session.get(Place, activity.place_id)
        if place is not None:
            add(
                "CURRENT_ACTIVITY" if activity.id == journey.current_activity_id else "ACTIVITY",
                source=(
                    "journey_current_activity"
                    if activity.id == journey.current_activity_id
                    else "journey_route_activity"
                ),
                latitude=place.latitude,
                longitude=place.longitude,
                location=place.name,
                    activity_id=activity.id if activity else None,
                place_id=place.id,
                at=activity.start_at,
            )
            if activity.id == journey.current_activity_id:
                break

    selected_option = session.scalar(
        select(StayJourneyOption).where(
            StayJourneyOption.journey_id == journey.id,
            StayJourneyOption.status == "SELECTED",
        ).order_by(StayJourneyOption.rank)
    )
    if selected_option is not None:
        hotel = session.get(AccommodationOfferSnapshot, selected_option.accommodation_offer_id)
        if hotel is not None:
            add(
                "HOTEL",
                source="selected_accommodation",
                latitude=hotel.latitude,
                longitude=hotel.longitude,
                location=hotel.property_name,
                at=hotel.check_in_at,
            )

    context = session.get(JourneyTripContext, journey.id)
    arrival = _load_json(context.arrival_anchor_json if context else None, {})
    departure = _load_json(context.departure_anchor_json if context else None, {})
    add(
        "ARRIVAL",
        source="journey_memory_arrival",
        latitude=arrival.get("latitude"),
        longitude=arrival.get("longitude"),
        location=arrival.get("location"),
        at=arrival.get("at") or journey.start_at,
    )

    commitment_rows = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
            Commitment.level == "C4_Anchored",
        )
    ).all()
    commitment_rows.sort(
        key=lambda item: str(_load_json(item.policy_json, {}).get("scheduled_start_at") or "")
    )
    for commitment in commitment_rows:
        activity = session.get(Activity, commitment.activity_id) if commitment.activity_id else None
        policy = _load_json(commitment.policy_json, {})
        semantic_place_id = str(policy.get("semantic_key") or "")
        place = (
            session.get(Place, activity.place_id)
            if activity and activity.place_id
            else session.get(Place, semantic_place_id)
        )
        if place is not None:
            add(
                "UPCOMING_COMMITMENT",
                source="protected_commitment",
                latitude=place.latitude,
                longitude=place.longitude,
                location=place.name,
                activity_id=activity.id if activity else None,
                place_id=place.id,
                at=policy.get("scheduled_start_at") or (activity.start_at if activity else None),
            )
            break

    add(
        "DEPARTURE",
        source="journey_memory_departure",
        latitude=departure.get("latitude"),
        longitude=departure.get("longitude"),
        location=departure.get("location"),
        at=departure.get("at") or journey.end_at,
    )
    return options


def derive_search_anchor(session: Session, journey: Journey) -> dict[str, Any] | None:
    """Resolve the first usable server-known Journey discovery anchor."""

    options = discovery_anchor_options(session, journey)
    return options[0] if options else None


def _city_label(city_code: str) -> str:
    raw = str(city_code or "").strip()
    if "-" in raw:
        raw = raw.split("-", 1)[1]
    return raw.replace("_", " ").replace("-", " ").strip() or str(city_code)


#: Stable names for "why the live path did not answer" (ORD-0099). A Python exception class
#: name is not a product reason: it changes with the HTTP client, means nothing to a
#: traveller, and puts an implementation detail on a screen. Five outcomes are
#: distinguishable, and each maps from a different failure shape.
RESOLVER_UNREACHABLE = "RESOLVER_UNREACHABLE"
PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
PROVIDER_BAD_RESPONSE = "PROVIDER_BAD_RESPONSE"
#: 5xx is operationally different from a malformed 2xx: one says "ask again soon",
#: the other says "the answer came back in a shape we cannot read".  Collapsing them
#: was lane F's g11 finding (six shapes, five codes).
PROVIDER_SERVER_ERROR = "PROVIDER_SERVER_ERROR"
PROVIDER_NO_RESULTS = "PROVIDER_NO_RESULTS"
#: Our own request was malformed (an internal city code reached a public geocoder).
#: Reported separately on purpose: blaming a provider for a bug we sent is how a lane
#: spends an evening chasing someone else's outage.
RESOLVER_LABEL_CARRIES_INTERNAL_CODE = "RESOLVER_LABEL_CARRIES_INTERNAL_CODE"

LIVE_FAILURE_REASONS = frozenset(
    {
        RESOLVER_UNREACHABLE,
        PROVIDER_TIMEOUT,
        PROVIDER_RATE_LIMITED,
        PROVIDER_SERVER_ERROR,
        PROVIDER_BAD_RESPONSE,
        PROVIDER_NO_RESULTS,
        RESOLVER_LABEL_CARRIES_INTERNAL_CODE,
    }
)


def live_failure_reason(exc: BaseException) -> str:
    """Name a live-discovery failure the way the product means it, not by exception class.

    Order matters: `httpx.TimeoutException` and `httpx.ConnectError` are both
    `httpx.HTTPError` subclasses, and an `HTTPStatusError` carries the response, so a 429
    has to be read before the generic branch - otherwise rate limiting disappears into
    "bad response" and the one failure an operator should retry differently is masked.
    """

    if isinstance(exc, ValueError) and "RESOLVER_LABEL_CARRIES_INTERNAL_CODE" in str(exc):
        return RESOLVER_LABEL_CARRIES_INTERNAL_CODE
    if isinstance(exc, httpx.TimeoutException):
        return PROVIDER_TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        return RESOLVER_UNREACHABLE
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return PROVIDER_RATE_LIMITED
    if isinstance(status, int):
        if 500 <= status < 600:
            return PROVIDER_SERVER_ERROR
        return PROVIDER_BAD_RESPONSE
    if isinstance(exc, httpx.HTTPError):
        return RESOLVER_UNREACHABLE
    if isinstance(exc, RuntimeError) and "NO_JOURNEY_DERIVED_CITY_ANCHOR" in str(exc):
        return PROVIDER_NO_RESULTS
    return PROVIDER_BAD_RESPONSE


def _resolver_city_label(city_code: str) -> str:
    """The one label both geocode calls send, asserted free of our own internal codes.

    `city_code` is a CityNexus identifier (`CN-TIANJIN`, `CNX-DEMO`). Forwarding it as the
    place name asks Nominatim to look for a string no traveller ever typed, which is how the
    bounded fallback kept answering "nothing found" (ORD-0099 ①). `_city_label` strips the
    prefix for every code the product mints; this guard is the assertion that the strip
    really happened, so a new code format cannot quietly reintroduce the bug.
    """

    label = str(_city_label(city_code)).strip()
    # `_city_label` turns hyphens into spaces, so a still-prefixed label can read
    # "CN TIANJIN" as easily as "CN-TIANJIN"; both are the same leak.
    normalized = label.upper().replace("_", " ").replace("-", " ")
    if re.match(r"^(CN|CNX|JP|SG|TH)\s", normalized):
        raise ValueError("RESOLVER_LABEL_CARRIES_INTERNAL_CODE")
    return label


def _is_tianjin_replay_city(city_code: str) -> bool:
    normalized = str(city_code or "").strip().upper().replace("_", "-")
    return normalized in {"CN-TIANJIN", "TIANJIN", "TSN", "CNX-DEMO"}


@dataclass(frozen=True)
class DiscoveryCandidate:
    provider_key: str
    external_id: str
    name: str
    category: str
    latitude: float
    longitude: float
    tags: tuple[str, ...]
    opening_hours: dict[str, Any]
    metadata: dict[str, Any]
    distance_m: float | None = None
    cost_evidence: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "external_id": self.external_id,
            "name": self.name,
            "category": self.category,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "tags": list(self.tags),
            "opening_hours": self.opening_hours,
            "metadata": self.metadata,
            "distance_m": round(self.distance_m, 1) if self.distance_m is not None else None,
            "cost_evidence": self.cost_evidence,
        }


class NominatimGeocoder:
    """Small, identified Nominatim client for bounded discovery calls."""

    provider_key = "nominatim"

    def __init__(self, *, client: httpx.Client | None = None, base_url: str | None = None):
        # Public-provider network policy (docs/NETWORK_POLICY.md): honour the
        # environment/proxy configuration by default so a Mihomo setup works,
        # and let the operator pin DIRECT or ENV_PROXY explicitly.
        self.client = client or httpx.Client(
            timeout=settings.real_city_timeout_seconds,
            trust_env=trust_environment_for_public_providers(),
        )
        self.base_url = (base_url or settings.nominatim_url).rstrip("/")
        self._owned_client = client is None

    def search(self, query: str, *, city: str, limit: int) -> list[DiscoveryCandidate]:
        response = self.client.get(
            f"{self.base_url}/search",
            params={
                "q": f"{query}, {city}",
                "format": "jsonv2",
                "limit": min(limit, 10),
                "addressdetails": 1,
                "extratags": 1,
            },
            headers={"User-Agent": "CityNexus/7.0 (+https://citynexus.local/real-city)"},
        )
        response.raise_for_status()
        rows = response.json()
        candidates: list[DiscoveryCandidate] = []
        for row in rows:
            try:
                lat = float(row["lat"])
                lon = float(row["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            osm_type = str(row.get("osm_type") or "place")
            osm_id = str(
                row.get("osm_id")
                or row.get("place_id")
                or hashlib.sha1(str(row).encode()).hexdigest()
            )
            category = str(row.get("type") or row.get("class") or "service")
            candidates.append(
                DiscoveryCandidate(
                    provider_key=self.provider_key,
                    external_id=_external_id(self.provider_key, f"{osm_type}/{osm_id}"),
                    name=str(row.get("display_name") or query).split(",")[0],
                    category=category,
                    latitude=lat,
                    longitude=lon,
                    tags=(category, "real_city"),
                    opening_hours={},
                    metadata={"display_name": row.get("display_name"), "osm_type": osm_type},
                    cost_evidence=cost_evidence_from_tags(row.get("extratags"), source="nominatim"),
                )
            )
        return candidates

    def close(self) -> None:
        if self._owned_client:
            self.client.close()


# Public Overpass mirrors, tried in order; they are frequently unavailable or
# slow, which is exactly why a fallback exists.
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)


#: ORD-0519: which geocoder grounds the city anchor, in preference order, and it is decided here
#: rather than hardcoded into one call.  This machine answers Photon and refuses Nominatim at the
#: TCP layer (measured 2026-09-30: `anchor_geocode` → `PROVIDER_TIMEOUT` with `network_mode=direct`,
#: while photon answers 200 in ~1 s), so a single nominatim-only leg made the whole demo board
#: unbuildable on a host that *does* have a working channel.  The rule that did not move: an
#: unanswered leg is never credited, and a REPLAY fallback is never supply.
ANCHOR_LEG_ENV = "CITYNEXUS_ANCHOR_LEG_ORDER"
DEFAULT_ANCHOR_LEGS = ("photon", "nominatim")


def anchor_leg_order() -> tuple[str, ...]:
    """The legs to try, named by configuration, validated against what this file can actually do."""

    raw = (os.getenv(ANCHOR_LEG_ENV) or "").strip().lower()
    if not raw:
        return DEFAULT_ANCHOR_LEGS
    wanted = tuple(part.strip() for part in raw.replace(",", " ").split() if part.strip())
    return tuple(leg for leg in wanted if leg in DEFAULT_ANCHOR_LEGS) or DEFAULT_ANCHOR_LEGS


class PhotonCityAnchor:
    """The second city-anchor leg: Photon, asked with the product's own identifying User-Agent."""

    provider_key = "photon"

    def __init__(self, *, client: httpx.Client | None = None, base_url: str | None = None):
        self.client = client or httpx.Client(
            timeout=settings.real_city_timeout_seconds,
            trust_env=trust_environment_for_public_providers(),
            headers={"User-Agent": "CityNexus/7.0 (+https://citynexus.local/real-city)"},
        )
        configured = base_url or ExternalProviderSettings().photon_base_url
        self.base_url = str(configured).rstrip("/")
        self._owned_client = client is None

    def search(self, query: str, *, city: str, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return anchor points and one honest reading of the call that produced them.

        The reading carries the status, the byte count and the elapsed time even when the answer is
        empty: "answered, found nothing" and "never replied" have to stay tellable apart afterwards.
        """

        started = time.monotonic()
        # Measured on photon.komoot.io today: `geometry=1` is refused with HTTP 400
        # ("Geometry output requested but not available in database"), while the default answer
        # already carries GeoJSON coordinates.  Asking for geometry therefore lost the leg that
        # was the only one able to answer on a host where Nominatim times out.
        params = {"q": f"{query}, {city}", "limit": min(max(limit, 1), 10)}
        try:
            response = self.client.get(f"{self.base_url}/api/", params=params)
        except Exception as exc:
            return [], {
                "leg": self.provider_key,
                "answered": False,
                "http_status": None,
                "bytes": 0,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                "outcome": f"NOT_ANSWERED_{type(exc).__name__}",
            }
        elapsed = round((time.monotonic() - started) * 1000.0, 1)
        points: list[dict[str, Any]] = []
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                # A 200 whose body is not a GeoJSON object is one leg that replied with something
                # this build cannot read - it is *not* a reason to abandon the capture.  This shape
                # used to escape as an `AttributeError`, which the orchestration filed as
                # `PROVIDER_BAD_RESPONSE at anchor_geocode`; that demoted the whole live pass to the
                # replay fallback, and because the TTL gate credits only `provider_key == "overpass"`
                # rows, every later warm ask then paid for the network again (ORD-0544: the g16b
                # 0-call promise broken by an unreadable body, three generations measured in
                # `review/ord0544/`).  Reported per leg, with bytes, so the miss stays tellable.
                return [], {
                    "leg": self.provider_key,
                    "answered": True,
                    "http_status": response.status_code,
                    "bytes": len(response.content),
                    "elapsed_ms": elapsed,
                    "outcome": "ANSWERED_NOT_GEOJSON",
                }
            try:
                for feature in (payload.get("features") or []):
                    coords = ((feature.get("geometry") or {}).get("coordinates")) or []
                    props = feature.get("properties") or {}
                    if len(coords) >= 2:
                        points.append(
                            {
                                "latitude": float(coords[1]),
                                "longitude": float(coords[0]),
                                "name": props.get("name"),
                                "osm_id": props.get("osm_id"),
                            }
                        )
            except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                return [], {
                    "leg": self.provider_key,
                    "answered": True,
                    "http_status": response.status_code,
                    "bytes": len(response.content),
                    "elapsed_ms": elapsed,
                    "outcome": "BAD_RESPONSE",
                }
        return points, {
            "leg": self.provider_key,
            "answered": response.status_code == 200,
            "http_status": response.status_code,
            "bytes": len(response.content),
            "elapsed_ms": elapsed,
            "outcome": "ANSWERED" if points else ("ANSWERED_EMPTY" if response.status_code == 200 else "REFUSED"),
        }

    def close(self) -> None:
        if self._owned_client:
            self.client.close()


class OverpassPOIProvider:
    """Bounded OSM POI query with a provider-owned, auditable request."""

    provider_key = "overpass"

    def __init__(self, *, client: httpx.Client | None = None, base_url: str | None = None):
        # Public-provider network policy (docs/NETWORK_POLICY.md): honour the
        # environment/proxy configuration by default so a Mihomo setup works,
        # and let the operator pin DIRECT or ENV_PROXY explicitly.
        self.client = client or httpx.Client(
            timeout=settings.real_city_timeout_seconds,
            trust_env=trust_environment_for_public_providers(),
        )
        self.base_url = (base_url or settings.overpass_url).rstrip("/")
        self._owned_client = client is None

    def search(
        self,
        query: str,
        *,
        latitude: float,
        longitude: float,
        radius_m: int,
        limit: int,
    ) -> list[DiscoveryCandidate]:
        candidates, _attempts = self.search_with_evidence(
            query, latitude=latitude, longitude=longitude, radius_m=radius_m, limit=limit
        )
        return candidates

    def search_with_evidence(
        self,
        query: str,
        *,
        latitude: float,
        longitude: float,
        radius_m: int,
        limit: int,
    ) -> tuple[list[DiscoveryCandidate], list[dict[str, Any]]]:
        """Ask one bounded category question, and report which endpoints actually answered.

        The attempts list exists because "nobody replied" and "somebody replied: none" are different
        statements; a caller must never have to guess which one it got.
        """

        requested = query.casefold()
        # The provider must answer the question that was actually asked.  A
        # single fixed cafe/restaurant tag set meant "any museums nearby" came
        # back with cafes, which then looked like a relevant answer.
        tags = '"amenity"~"cafe|restaurant|library|arts_centre|theatre"'
        for keywords, candidate_tags in (
            (("museum", "博物馆"), '"tourism"="museum"'),
            (("gallery", "美术馆", "画廊"), '"tourism"="gallery"|"amenity"="arts_centre"'),
            (("theatre", "theater", "剧院", "剧场"), '"amenity"="theatre"'),
            (("library", "图书馆"), '"amenity"="library"'),
            (("cafe", "coffee", "咖啡馆", "咖啡"), '"amenity"~"cafe|coffee"'),
            (("restaurant", "food", "餐厅", "吃饭", "美食"), '"amenity"~"restaurant|food_court"'),
            (("park", "garden", "公园", "户外", "outdoor"), '"leisure"~"park|garden"'),
            (("hotel", "住宿", "酒店"), '"tourism"~"hotel|hostel|guest_house"'),
            (("attraction", "sight", "景点", "tourist"), '"tourism"~"attraction|viewpoint"'),
            # The three coverage categories that used to borrow the default set (g18a/g18b):
            # each asks its own question, so "0 answers" reads as a real zero per category
            # instead of "the default cafe query found nothing".
            (("nightlife", "夜生活", "酒吧", "夜店"), '"amenity"~"bar|pub|nightclub"'),
            (
                ("mobility", "地铁站", "公交", "交通站点"),
                '"railway"~"station|subway_entrance"|"amenity"="bus_station"|"highway"="bus_stop"',
            ),
            (
                ("booking", "门票预约", "票务", "售票"),
                '"tourism"="ticket_converter"|"amenity"="ticket_office"',
            ),
        ):
            if any(token in requested for token in keywords):
                tags = candidate_tags
                break
        query_text = (
            f"[out:json][timeout:10];nwr[{tags}](around:{radius_m},{latitude},{longitude});"
            "out center tags;"
        )
        # Public Overpass mirrors are individually flaky.  Rotating through
        # them is what keeps a real category question answerable instead of
        # collapsing into a permanent "providers unreachable" state.
        elements: list[dict[str, Any]] = []
        last_error: Exception | None = None
        attempts: list[dict[str, Any]] = []
        urls = [f"{self.base_url}/interpreter"] + [
            url for url in OVERPASS_MIRRORS if url != f"{self.base_url}/interpreter"
        ]
        for url in urls:
            started = time.monotonic()
            try:
                response = self.client.post(
                    url,
                    data={"data": query_text},
                    headers={
                        "User-Agent": "CityNexus/7.0 (+https://citynexus.local/real-city)"
                    },
                )
                status = response.status_code
                retry_after = response.headers.get("Retry-After")
                response.raise_for_status()
                elements = response.json().get("elements", [])
                attempts.append(
                    {
                        "endpoint": url,
                        "answered": True,
                        "http_status": status,
                        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                        "elements": len(elements),
                        "retry_after": retry_after,
                    }
                )
                # A mirror that answered has answered the question, including with nothing.
                # Falling through here is what made a valid zero look like an outage.
                break
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                attempts.append(
                    {
                        "endpoint": url,
                        "answered": False,
                        "http_status": getattr(getattr(exc, "response", None), "status_code", None),
                        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                        "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    }
                )
                last_error = exc
                continue
        if not attempts or all(not a["answered"] for a in attempts):
            # Nothing replied: this is a runtime failure and must stay one.  The attempts we
            # recorded are evidence, so they travel with the exception instead of being lost.
            error = last_error if last_error is not None else RuntimeError("NO_PROVIDER_ATTEMPTED")
            try:
                error.provider_attempts = attempts  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - frozen exception types refuse attributes
                pass
            raise error
        candidates: list[DiscoveryCandidate] = []
        for row in elements[:limit]:
            tags_data = row.get("tags") or {}
            center = row.get("center") or row
            try:
                lat = float(center["lat"])
                lon = float(center["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            category = str(
                tags_data.get("tourism")
                or tags_data.get("amenity")
                or tags_data.get("leisure")
                or "service"
            )
            name = str(tags_data.get("name") or f"OSM {category}")
            osm_id = f"{row.get('type', 'n')}/{row.get('id')}"
            candidates.append(
                DiscoveryCandidate(
                    provider_key=self.provider_key,
                    external_id=_external_id(self.provider_key, osm_id),
                    name=name,
                    category=category,
                    latitude=lat,
                    longitude=lon,
                    tags=tuple(
                        sorted(
                            {
                                category,
                                "real_city",
                                "indoor" if category not in {"park", "garden"} else "outdoor",
                            }
                        )
                    ),
                    opening_hours=normalize_opening_hours(tags_data.get("opening_hours")),
                    metadata={"osm_id": osm_id, "tags": tags_data},
                    distance_m=_distance_m(latitude, longitude, lat, lon),
                    cost_evidence=cost_evidence_from_tags(tags_data, source="overpass"),
                )
            )
        return candidates, attempts

    def close(self) -> None:
        if self._owned_client:
            self.client.close()


_TIANJIN_REPLAY = (
    DiscoveryCandidate(
        provider_key="openstreetmap-replay",
        external_id="node/281001",
        name="Tianjin Italian Style Town Indoor Gallery",
        category="arts_centre",
        latitude=39.1315,
        longitude=117.2050,
        tags=("culture", "indoor", "real_city", "tianjin"),
        opening_hours={"open": "10:00", "close": "21:00"},
        metadata={"source": "captured_replay", "city": "Tianjin"},
        cost_evidence={
            "status": "ESTIMATED",
            "amount_minor": 800,
            "currency": "CNY",
            "source": "captured_replay",
        },
    ),
    DiscoveryCandidate(
        provider_key="openstreetmap-replay",
        external_id="node/281002",
        name="Haihe Riverside Design Library",
        category="library",
        latitude=39.1226,
        longitude=117.1964,
        tags=("culture", "quiet", "indoor", "real_city", "tianjin"),
        opening_hours={"open": "09:30", "close": "20:00"},
        metadata={"source": "captured_replay", "city": "Tianjin"},
        cost_evidence={
            "status": "ESTIMATED",
            "amount_minor": 0,
            "currency": "CNY",
            "source": "captured_replay",
        },
    ),
    DiscoveryCandidate(
        provider_key="openstreetmap-replay",
        external_id="node/281003",
        name="Tianjin Ancient Culture Street Tea House",
        category="cafe",
        latitude=39.1530,
        longitude=117.1908,
        tags=("local_food", "indoor", "real_city", "tianjin"),
        opening_hours={"open": "09:00", "close": "22:00"},
        metadata={"source": "captured_replay", "city": "Tianjin"},
        cost_evidence={
            "status": "ESTIMATED",
            "amount_minor": 300,
            "currency": "CNY",
            "source": "captured_replay",
        },
    ),
)


def _replay_candidates(query: str, *, limit: int) -> list[DiscoveryCandidate]:
    needle = query.casefold()
    ranked = sorted(
        _TIANJIN_REPLAY,
        key=lambda item: (
            0
            if any(
                token in f"{item.name} {item.category} {' '.join(item.tags)}".casefold()
                for token in needle.split()
            )
            else 1,
            item.name,
        ),
    )
    return list(ranked[:limit])


def _captured_replay_candidates(
    query: str,
    *,
    limit: int,
    anchor: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> list[DiscoveryCandidate]:
    path = _replay_snapshot_path()
    payload = snapshot if snapshot is not None else _load_replay_snapshot()
    if not payload:
        return []
    completions = {
        str(item.get("candidate_external_id")): item
        for item in payload.get("evidence_completion", [])
        if isinstance(item, dict) and item.get("candidate_external_id")
    }
    ranked: list[tuple[int, DiscoveryCandidate]] = []
    needle = query.casefold()
    for row in payload.get("candidates", []):
        try:
            tags_data = row.get("tags") or {}
            if isinstance(tags_data, dict):
                tags = tuple(
                    sorted({str(value).casefold() for value in tags_data.values() if value})
                )
            else:
                tags = tuple(str(value) for value in tags_data)
            semantic_tags = set(tags)
            category = str(row.get("category") or "service").casefold()
            if category in {"museum", "library", "arts_centre", "theatre", "gallery"}:
                semantic_tags.update({"indoor", "culture"})
            elif category in {"cafe", "restaurant", "food"}:
                semantic_tags.update({"indoor", "local_food"})
            elif category in {"park", "garden", "viewpoint"}:
                semantic_tags.update({"outdoor", "scenic"})
            opening_hours = normalize_opening_hours(row.get("opening_hours"))
            cost_evidence = row.get("cost_evidence") or {
                "status": "UNKNOWN",
                "currency": "CNY",
            }
            opening_hours, cost_evidence, promoted = _promote_captured_evidence(
                str(row["external_id"]),
                completions.get(str(row["external_id"])),
                opening_hours=opening_hours,
                cost_evidence=cost_evidence,
            )
            metadata = {
                "source": "sealed_live_capture",
                "capture_path": str(path),
                "provider": row.get("provider"),
                "city": payload.get("city_name"),
                "snapshot_captured_at": payload.get("captured_at"),
            }
            if promoted is not None:
                metadata["evidence_completion"] = promoted
            distance = None
            if anchor and anchor.get("latitude") is not None and anchor.get("longitude") is not None:
                distance = _distance_m(
                    float(anchor["latitude"]),
                    float(anchor["longitude"]),
                    float(row["latitude"]),
                    float(row["longitude"]),
                )
            candidate = DiscoveryCandidate(
                provider_key="openstreetmap-replay",
                external_id=str(row["external_id"]),
                name=str(row["name"]),
                category=category,
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                tags=(*sorted(semantic_tags), "real_city", "captured_live_replay"),
                opening_hours=opening_hours,
                metadata=metadata,
                distance_m=distance,
                cost_evidence=cost_evidence,
            )
        except (KeyError, TypeError, ValueError):
            continue
        score = (
            0
            if any(
                token
                in f"{candidate.name} {candidate.category} {' '.join(candidate.tags)}".casefold()
                for token in needle.split()
            )
            else 1
        )
        ranked.append((score, candidate))
    ranked.sort(key=lambda item: (item[0], item[1].name))
    return [item for _, item in ranked[:limit]]


def materialize_candidates(
    session: Session,
    journey: Journey | None,
    candidates: list[DiscoveryCandidate],
    *,
    captured_at: str,
    mode: str,
    city_code: str | None = None,
) -> list[dict[str, Any]]:
    """Apply captured candidates to the store.

    ORD-0526 widened the owner from "a journey" to "a journey or a city": the catalogue-hydration
    door prepares a *destination*, and a traveller who never opened a trip should still get a
    registered offline source applied. `city_code` is honoured only when no journey is passed, and
    a city-scoped row carries `journey_id = NULL` rather than an invented one - the foreign key is
    on, so a made-up owner id would fail at the store instead of lying quietly.
    """

    owner_id = journey.id if journey is not None else None
    owner_city = journey.city_code if journey is not None else (city_code or "")
    if not owner_city:
        raise ValueError("MATERIALISE_NEEDS_A_CITY")
    expires_at = (
        datetime.fromisoformat(captured_at)
        + timedelta(seconds=settings.real_city_cache_ttl_seconds)
    ).isoformat()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        distance = candidate.distance_m
        external_id = candidate.external_id
        opening_hours = normalize_opening_hours(candidate.opening_hours)
        cost_evidence = candidate.cost_evidence or cost_evidence_from_tags(
            candidate.metadata.get("tags") if isinstance(candidate.metadata, dict) else None,
            source=candidate.provider_key,
        )
        row = session.scalar(
            select(RealPlaceCandidate).where(
                (
                    RealPlaceCandidate.journey_id == owner_id
                    if owner_id
                    else RealPlaceCandidate.journey_id.is_(None)
                ),
                RealPlaceCandidate.provider_key == candidate.provider_key,
                RealPlaceCandidate.external_id == external_id,
            )
        )
        if row is None:
            row = RealPlaceCandidate(
                id=f"rpc-{hashlib.sha1(f'{owner_id or owner_city}|{candidate.provider_key}|{external_id}'.encode()).hexdigest()[:24]}",
                journey_id=owner_id,
                provider_key=candidate.provider_key,
                external_id=external_id,
            )
            session.add(row)
        row.journey_id = owner_id
        row.city_code = owner_city
        row.name = candidate.name
        row.category = candidate.category
        row.latitude = candidate.latitude
        row.longitude = candidate.longitude
        row.tags_json = _json(list(candidate.tags))
        row.opening_hours_json = _json(opening_hours)
        row.metadata_json = _json(
            {**candidate.metadata, "discovery_mode": mode, "cost_evidence": cost_evidence}
        )
        row.captured_at = captured_at
        row.expires_at = expires_at
        row.distance_m = distance
        row.evidence_json = _json(
            {
                "provider": candidate.provider_key,
                "captured_at": captured_at,
                "ttl_seconds": settings.real_city_cache_ttl_seconds,
                "provenance": (
                    "OFFLINE_SNAPSHOT"
                    if mode == "offline_snapshot"
                    else (
                        "SEALED_REAL_REPLAY"
                        if mode == "captured_replay"
                        else (
                            "REPLAY"
                            if mode in {"replay", "replay-fallback"}
                            else "LIVE_PROVIDER"
                        )
                    )
                ),
                "opening_hours": opening_hours,
                "cost_evidence": cost_evidence,
                "evidence_completion": candidate.metadata.get("evidence_completion")
                if isinstance(candidate.metadata, dict)
                else None,
            }
        )
        row.status = "DISCOVERED"

        completion = candidate.metadata.get("evidence_completion") if isinstance(candidate.metadata, dict) else None
        # A city-scoped row has no journey to hang a web observation on, and inventing an owner id
        # would be caught by the foreign key - so that leg is skipped, not faked.
        if journey is not None and isinstance(completion, dict):
            observation_ids: list[str] = []
            for observation in completion.get("observations", []):
                if not isinstance(observation, dict) or not observation.get("url"):
                    continue
                fingerprint = hashlib.sha256(
                    _json(
                        {
                            "candidate": external_id,
                            "url": observation.get("url"),
                            "title": observation.get("title"),
                            "excerpt": observation.get("excerpt"),
                        }
                    ).encode("utf-8")
                ).hexdigest()
                existing_observation = session.scalar(
                    select(WebObservation).where(
                        WebObservation.journey_id == journey.id,
                        WebObservation.evidence_fingerprint == fingerprint,
                    )
                )
                if existing_observation is None:
                    existing_observation = WebObservation(
                        id=str(uuid4()),
                        journey_id=journey.id,
                        query=candidate.name,
                        url=str(observation["url"]),
                        domain=str(observation.get("domain") or ""),
                        title=str(observation.get("title") or candidate.name),
                        source_class=str(observation.get("source_class") or "OPEN_WEB"),
                        provider_key="sealed-provider-evidence",
                        model_key="provider-capture",
                        captured_at=captured_at,
                        evidence_fingerprint=fingerprint,
                        content_excerpt=str(observation.get("excerpt") or "")[:2000],
                        authority_status="CAPTURED_SOURCE",
                        created_at=captured_at,
                    )
                    session.add(existing_observation)
                observation_ids.append(existing_observation.id)
            if observation_ids:
                completion["web_observation_ids"] = observation_ids
                evidence_payload = _load_json(row.evidence_json, {})
                evidence_payload["web_observation_ids"] = observation_ids
                row.evidence_json = _json(evidence_payload)

        place = session.scalar(
            select(Place).where(
                Place.provider_key == candidate.provider_key,
                Place.external_id == external_id,
            )
        )
        if place is None:
            place = Place(
                id=f"real-place-{hashlib.sha1(f'{candidate.provider_key}|{external_id}'.encode()).hexdigest()[:24]}",
                provider_key=candidate.provider_key,
                external_id=external_id,
                city_code=owner_city,
                name=candidate.name,
                category=candidate.category,
                latitude=candidate.latitude,
                longitude=candidate.longitude,
                opening_hours_json=_json(opening_hours),
                tags_json=_json(list(candidate.tags)),
                metadata_json=_json(
                    _place_metadata(candidate, mode, cost_evidence, opening_hours)
                ),
                updated_at=captured_at,
            )
            session.add(place)
        else:
            place.city_code = owner_city
            place.name = candidate.name
            place.category = candidate.category
            place.latitude = candidate.latitude
            place.longitude = candidate.longitude
            place.opening_hours_json = _json(opening_hours)
            place.tags_json = _json(list(candidate.tags))
            place.metadata_json = _json(
                _place_metadata(candidate, mode, cost_evidence, opening_hours)
            )
            place.updated_at = captured_at
        session.flush()
        row.materialized_place_id = place.id
        service = session.scalar(
            select(CityService).where(
                CityService.provider_key == candidate.provider_key,
                CityService.external_id == f"service:{external_id}",
            )
        )
        if service is None:
            session.add(
                CityService(
                    id=f"service-{hashlib.sha1(f'{candidate.provider_key}|{external_id}'.encode()).hexdigest()[:24]}",
                    provider_key=candidate.provider_key,
                    external_id=f"service:{external_id}",
                    place_id=place.id,
                    city_code=owner_city,
                    service_type=candidate.category,
                    name=candidate.name,
                    availability_json=_json(opening_hours),
                    metadata_json=_json(candidate.metadata),
                    updated_at=captured_at,
                )
            )
        result.append(
            {
                **candidate.payload(),
                "opening_hours": opening_hours,
                "cost_evidence": cost_evidence,
                "id": place.id,
                "materialized_place_id": place.id,
                # Read off the row that now holds these coordinates, not off the
                # candidate: the stored system and the system the provider handed
                # us are different claims and only the row knows both.
                "coordinate_system": place.coordinate_system,
                "source_coordinate_system": place.source_coordinate_system,
            }
        )
    session.flush()
    return result


def _unexpired_cached_candidates(
    session: Session, journey: Journey, query: str, anchor: dict[str, Any] | None
) -> list[DiscoveryCandidate] | None:
    """Unexpired provider candidates for this journey+question, straight from the store.

    The TTL gate (lane F's g16b): ``expires_at`` has been written since the first capture but
    never read, so a warm ask paid the full cold cost again.  Returns None when nothing usable
    is cached — the caller then goes to the providers as before.
    """

    now = datetime.fromisoformat(_now())
    rows = session.scalars(
        select(RealPlaceCandidate).where(RealPlaceCandidate.journey_id == journey.id)
    ).all()
    usable: list[DiscoveryCandidate] = []
    for row in rows:
        raw_expires = str(row.expires_at or "")
        if not raw_expires:
            continue
        try:
            expires = datetime.fromisoformat(raw_expires)
        except ValueError:
            continue
        if expires <= now:
            continue
        if str(row.provider_key or "") != "overpass":
            continue
        evidence = _load_json(row.evidence_json, {})
        recorded_query = str((evidence.get("request") or {}).get("query") or "")
        if recorded_query and recorded_query != query:
            continue
        place = session.get(Place, row.materialized_place_id) if row.materialized_place_id else None
        if place is None:
            continue
        distance = None
        if anchor and anchor.get("latitude") is not None and anchor.get("longitude") is not None:
            distance = _distance_m(
                float(anchor["latitude"]),
                float(anchor["longitude"]),
                float(place.latitude),
                float(place.longitude),
            )
        metadata = _load_json(place.metadata_json, {})
        stored_cost = evidence.get("cost_evidence") or {
            "status": "UNKNOWN",
            "currency": "CNY",
        }
        usable.append(
            DiscoveryCandidate(
                provider_key=str(place.provider_key),
                external_id=str(place.external_id),
                name=str(place.name),
                category=str(place.category),
                latitude=float(place.latitude),
                longitude=float(place.longitude),
                tags=tuple(sorted(_load_json(place.tags_json, []) or [])),
                opening_hours=normalize_opening_hours(_load_json(place.opening_hours_json, {})),
                metadata={**metadata, "cache": "HIT"},
                distance_m=distance,
                cost_evidence=stored_cost,
            )
        )
    return usable or None


def capture_real_city_candidates(
    session: Session,
    journey: Journey,
    *,
    query: str,
    radius_m: int = 2500,
    limit: int = 8,
    mode: str = "replay",
) -> dict[str, Any]:
    captured_at = _now()
    mode = mode.lower()
    requested_mode = mode
    provider_status: dict[str, Any]
    candidates: list[DiscoveryCandidate]
    if mode == "captured_replay" and not _is_tianjin_replay_city(journey.city_code):
        raise ValueError(
            f"CAPTURED_REPLAY_CITY_MISMATCH: Tianjin replay cannot be used for {journey.city_code}"
        )
    snapshot = _load_replay_snapshot() if mode == "captured_replay" else {}
    journey_anchor = derive_search_anchor(session, journey)
    if mode == "live":
        # g16b: an unexpired candidate set for this same journey+question answers without any
        # outbound call.  The TTL has been written since the first capture; reading it is the half
        # that was missing (242.9 s / 238.3 s cold/warm pair came exactly from this).
        cached = _unexpired_cached_candidates(session, journey, query, journey_anchor)
        if cached:
            captured_at_hit = _now()
            session.add(
                ProviderCall(
                    id=str(uuid4()),
                    provider_key="real-city-provider-mesh",
                    operation="discover_city_services",
                    request_json=_json(
                        {
                            "city_code": journey.city_code,
                            "limit": limit,
                            "mode": "live-cache-hit",
                            "query": query,
                            "radius_m": radius_m,
                            "requested_mode": requested_mode,
                        }
                    ),
                    response_json=_json(
                        {
                            "count": len(cached),
                            "providers": {
                                "anchor": journey_anchor,
                                "cache": "HIT",
                                "overpass": "CACHED",
                                "attempts": [],
                            },
                        }
                    ),
                    status="SUCCESS",
                    latency_ms=0,
                    created_at=captured_at_hit,
                )
            )
            materialized_hit = materialize_candidates(
                session, journey, cached, captured_at=captured_at_hit, mode="live"
            )
            return {
                "journey_id": journey.id,
                "city_code": journey.city_code,
                "city_context": journey.city_code,
                "mode": "live-cache-hit",
                "captured_at": captured_at_hit,
                "expires_at": (
                    datetime.fromisoformat(captured_at_hit)
                    + timedelta(seconds=settings.real_city_cache_ttl_seconds)
                ).isoformat(),
                "provider_status": {
                    "anchor": journey_anchor,
                    "cache": "HIT",
                    # ORD-0519: a cache hit asked nobody. Crediting both geocoders with "CACHED"
                    # made a photon-grounded city read back as nominatim on every repeat call,
                    # which is exactly the mis-credit this order forbids; the honest statement is
                    # that this call did not speak to either leg.
                    "overpass": "NOT_ASKED_THIS_CALL",
                    "nominatim": "NOT_ASKED_THIS_CALL",
                    "photon": "NOT_ASKED_THIS_CALL",
                    "anchor_legs": [],
                    "anchor_leg_of_original_capture": None,
                    "attempts": [],
                    "failure_stage": None,
                    "fallback_reason": None,
                },
                "provider_evidence": {"snapshot_schema": None, "capture_anchor": None,
                                      "mobility": None, "weather": None},
                "provenance": "LIVE_PROVIDER",
                "runtime_provenance": "LIVE_CACHE_HIT",
                "candidates": materialized_hit,
            }
    if mode == "live":
        live_error: str | None = None
        # initialised here because the failure shapes are reachable from paths that never
        # reach the gate below (a malformed label raises before any leg is asked), and an
        # unbound name there is an UnboundLocalError replacing the honest reason code.
        fallback_geocode = "NOT_REACHED"
        failure_stage: str | None = None
        candidates = []
        overpass_attempts: list[dict[str, Any]] = []
        # Declared before the try because the failure shapes below must still be able to say which
        # anchor legs were asked and what each one did.
        anchor_leg_readings: list[dict[str, Any]] = []
        anchor = journey_anchor
        if not settings.real_city_enabled:
            live_error = "REAL_CITY_DISABLED"
        else:
            geocoder = NominatimGeocoder()
            poi = OverpassPOIProvider()
            stage = "anchor_geocode"
            try:
                # Nominatim grounds the Journey/city anchor and names; Overpass
                # owns bounded category discovery around that server-derived anchor.
                city_label = _resolver_city_label(journey.city_code)
                context = session.get(JourneyTripContext, journey.id)
                arrival = _load_json(context.arrival_anchor_json if context else None, {})
                anchor_query = str(arrival.get("location") or city_label)
                # ORD-0519: the anchor is taken from the first leg that actually answers, in the
                # configured order, and the leg that answered is the one named in `anchor.source`.
                # A leg that was never asked because an earlier one answered is recorded as SKIPPED,
                # so no reading can be read as "nominatim was fine" when nobody spoke to it.
                if anchor is None:
                    legs = anchor_leg_order()
                    for position, leg in enumerate(legs):
                        if leg == "photon":
                            anchors, reading = PhotonCityAnchor().search(
                                anchor_query, city=city_label, limit=1
                            )
                            if anchors:
                                anchor = {
                                    "latitude": anchors[0]["latitude"],
                                    "longitude": anchors[0]["longitude"],
                                    "source": "photon_city_anchor",
                                }
                        else:
                            started = time.monotonic()
                            try:
                                hits = geocoder.search(anchor_query, city=city_label, limit=1)
                                reading = {
                                    "leg": "nominatim",
                                    "answered": True,
                                    "http_status": 200,
                                    "bytes": None,
                                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                                    "outcome": "ANSWERED" if hits else "ANSWERED_EMPTY",
                                }
                                if hits:
                                    anchor = {
                                        "latitude": hits[0].latitude,
                                        "longitude": hits[0].longitude,
                                        "source": "nominatim_city_anchor",
                                    }
                            except httpx.HTTPError as exc:
                                reading = {
                                    "leg": "nominatim",
                                    "answered": False,
                                    "http_status": getattr(
                                        getattr(exc, "response", None), "status_code", None
                                    ),
                                    "bytes": None,
                                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                                    "outcome": f"NOT_ANSWERED_{type(exc).__name__}",
                                }
                                hits = []
                        reading["attempted"] = True
                        anchor_leg_readings.append(reading)
                        if anchor is not None:
                            for skipped in legs[position + 1 :]:
                                anchor_leg_readings.append(
                                    {"leg": skipped, "attempted": False, "outcome": "SKIPPED"}
                                )
                            break
                    if anchor is None:
                        # Both legs were asked and neither grounded the city: say which ones, per
                        # leg, instead of the old single-leg RuntimeError.
                        raise RuntimeError("NO_ANCHOR_LEG_ANSWERED")
                elif anchor is not None:
                    anchor_leg_readings = [
                        {"leg": "journey_state", "attempted": True, "outcome": "ANSWERED"}
                    ]
                if anchor is None:
                    raise RuntimeError("NO_JOURNEY_DERIVED_CITY_ANCHOR")
                stage = "category_search"
                if hasattr(poi, "search_with_evidence"):
                    candidates, overpass_attempts = poi.search_with_evidence(
                        query,
                        latitude=float(anchor["latitude"]),
                        longitude=float(anchor["longitude"]),
                        radius_m=radius_m,
                        limit=limit,
                    )
                else:  # test doubles implement only the historical shape
                    candidates = poi.search(
                        query,
                        latitude=float(anchor["latitude"]),
                        longitude=float(anchor["longitude"]),
                        radius_m=radius_m,
                        limit=limit,
                    )
                    overpass_attempts = []
                # CMDR 02:35 ⑤: a leg that grounded the city already answered the geography
                # question in this call, so asking Nominatim the same bounded words again is a
                # second geocode that cannot change the answer - and, like the answered-zero rule
                # it shares a root with, it turns "the mirror said nothing here" into fresh outbound
                # work. The fallback door therefore serves exactly one case: this call asked no leg
                # at all (the centre came from the journey's own state).
                anchor_grounded_by_leg = any(
                    reading.get("attempted")
                    and reading.get("answered")
                    and str((anchor or {}).get("source") or "").startswith(f"{reading.get('leg')}_")
                    for reading in anchor_leg_readings
                )
                fallback_geocode: str
                if candidates:
                    fallback_geocode = "NOT_NEEDED"
                elif anchor_grounded_by_leg:
                    fallback_geocode = (
                        f"SKIPPED_ANCHOR_GROUNDED_BY_{(anchor or {}).get('source')}"
                    )
                else:
                    # The bounded fallback is a *second* attempt at the same bounded
                    # question, so it must be asked with the same words: `city_code` is
                    # our identifier, not a place name, and sending it asked Nominatim
                    # for "tea shops, CN-TIANJIN" (ORD-0099 ①, measured).
                    stage = "fallback_geocode"
                    fallback_geocode = "ASKED"
                    candidates = geocoder.search(query, city=city_label, limit=limit)
                leg_states: dict[str, str] = {}
                for reading in anchor_leg_readings:
                    name = reading.get("leg")
                    if not reading.get("attempted"):
                        leg_states[name] = "SKIPPED"
                    elif (anchor or {}).get("source", "").startswith(str(name)):
                        leg_states[name] = "CAPTURED"
                    elif reading.get("answered"):
                        leg_states[name] = "ANSWERED_EMPTY"
                    else:
                        leg_states[name] = "NOT_ANSWERED"
                if stage == "fallback_geocode" and candidates:
                    # The bounded second attempt is Nominatim's own answer about places, which is a
                    # different fact from who grounded the city.  Naming it separately is what keeps
                    # "photon grounded this" from being overwritten by "nominatim said something".
                    leg_states["nominatim"] = "CAPTURED_FOR_POI"
                provider_status = {
                    "nominatim": leg_states.get("nominatim", "NOT_ASKED"),
                    "photon": leg_states.get("photon", "NOT_ASKED"),
                    # An answered mirror owns the answer even when it found nothing; only
                    # "no endpoint replied" is a failure-shaped status.
                    "overpass": "CAPTURED"
                    if any(a.get("answered") for a in overpass_attempts)
                    else "NOT_ANSWERED",
                    "anchor": anchor,
                    # Named, not implied: whether the second bounded ask happened, and if not, why -
                    # a reader must be able to tell "skipped because a leg already grounded this
                    # city" from "never got as far as asking".
                    "fallback_geocode": fallback_geocode,
                    # ORD-0519: who was asked, who replied, with what status and how many bytes.
                    "anchor_legs": anchor_leg_readings,
                    "failure_stage": stage,
                    # ORD-0192 ⑥: which endpoints were asked, what they said, how long they took.
                    "attempts": overpass_attempts,
                }
            except (
                httpx.HTTPError,
                KeyError,
                TypeError,
                ValueError,
                AttributeError,
                RuntimeError,
            ) as exc:
                # Named, stable, and screen-safe. The exception class and its message go
                # to the log, where an operator reads them; a caller gets one of five
                # reason codes (ORD-0099 ②).
                live_error = live_failure_reason(exc)
                failure_stage = stage
                recovered = getattr(exc, "provider_attempts", None)
                if isinstance(recovered, list) and recovered and not overpass_attempts:
                    overpass_attempts = recovered
                LOGGER.warning(
                    "real-city live discovery failed (%s) at %s: %s: %s",
                    live_error,
                    stage,
                    type(exc).__name__,
                    exc,
                )
            finally:
                geocoder.close()
                poi.close()
        if live_error or not candidates:
            if _is_tianjin_replay_city(journey.city_code):
                candidates = _replay_candidates(query, limit=limit)
                mode = "replay-fallback"
                provider_status = {
                    "nominatim": "REPLAY_FALLBACK",
                    "overpass": "REPLAY_FALLBACK",
                    # ORD-0519: this shape replaces the whole status, so it has to carry the credit
                    # over itself.  A city that photon grounded and Overpass then refused used to
                    # read back as `nominatim: REPLAY_FALLBACK` with no anchor at all - the
                    # mis-credit this order exists to stop.  `counts_as_supply: false` keeps the
                    # ban: naming the leg is not the same as accepting its places.
                    "anchor": anchor or journey_anchor,
                    "anchor_credit_kept": anchor is not None,
                    # Carried into this shape too: the caller's response is the replay-fallback
                    # one, so a fact that only lived in the success dict would never be readable.
                    "fallback_geocode": fallback_geocode,
                    # One vocabulary for one question: a named reason, never an exception
                    # class, and `PROVIDER_NO_RESULTS` when the providers answered but found
                    # nothing (which is a different thing from answering at all).
                    "fallback_reason": live_error or PROVIDER_NO_RESULTS,
                    "failure_stage": failure_stage,
                    # The board cell has to be nameable even in this shape: which leg was asked,
                    # what it returned, and that none of it was supply.
                    "anchor_legs": anchor_leg_readings,
                    "counts_as_supply": False,
                }
            else:
                # A generic-city trial must fail visibly rather than importing
                # Tianjin demo facts into another city's Journey.
                candidates = []
                mode = "live-unavailable"
                provider_status = {
                    "nominatim": "UNAVAILABLE",
                    "overpass": "UNAVAILABLE",
                    "anchor": anchor or journey_anchor,
                    "anchor_credit_kept": anchor is not None,
                    "fallback_geocode": fallback_geocode,
                    "fallback_reason": live_error or PROVIDER_NO_RESULTS,
                    "failure_stage": failure_stage,
                    "wrong_city_replay_blocked": True,
                    "anchor_legs": anchor_leg_readings,
                    "counts_as_supply": False,
                    # Observations from before the failure: which endpoints were asked and
                    # what each said.  Empty only when nothing was even attempted.
                    "attempts": overpass_attempts,
                }
    else:
        candidates = (
            _captured_replay_candidates(
                query,
                limit=limit,
                anchor=journey_anchor,
                snapshot=snapshot,
            )
            if mode == "captured_replay"
            else _replay_candidates(query, limit=limit)
        )
        provider_status = {
            "nominatim": "REPLAY_NOT_CALLED",
            "overpass": "REPLAY_NOT_CALLED",
            "openstreetmap_replay": "SEALED_REAL_REPLAY"
            if mode == "captured_replay"
            else "CAPTURED",
            "anchor": journey_anchor or snapshot.get("anchor"),
            "snapshot_anchor": snapshot.get("anchor"),
            "evidence_completion": sum(
                1
                for item in candidates
                if isinstance(item.metadata, dict) and item.metadata.get("evidence_completion")
            ),
        }
    request_payload = {
        "query": query,
        "city_code": journey.city_code,
        "radius_m": radius_m,
        "limit": limit,
        "mode": mode,
        "requested_mode": requested_mode,
    }
    session.add(
        ProviderCall(
            # Provider calls are audit events, not idempotency keys. Re-running
            # the same request must append a new event instead of colliding.
            id=str(uuid4()),
            provider_key=(
                "openstreetmap-replay"
                if mode in {"replay", "captured_replay", "replay-fallback"}
                else "real-city-provider-mesh"
            ),
            operation="discover_city_services",
            request_json=_json(request_payload),
            response_json=_json({"providers": provider_status, "count": len(candidates)}),
            status="SUCCESS",
            latency_ms=0,
            created_at=captured_at,
        )
    )
    materialized = materialize_candidates(
        session, journey, candidates, captured_at=captured_at, mode=mode
    )
    return {
        "journey_id": journey.id,
        "city_code": journey.city_code,
        "city_context": journey.city_code,
        "mode": mode,
        "captured_at": captured_at,
        "expires_at": (
            datetime.fromisoformat(captured_at)
            + timedelta(seconds=settings.real_city_cache_ttl_seconds)
        ).isoformat(),
        "provider_status": provider_status,
        "provider_evidence": {
            "snapshot_schema": snapshot.get("schema_version") if snapshot else None,
            "capture_anchor": snapshot.get("anchor") if snapshot else None,
            "mobility": snapshot.get("mobility") if snapshot else None,
            "weather": snapshot.get("weather") if snapshot else None,
        },
        # Keep the historical direct-discovery field for V7/V8 clients while
        # exposing the authoritative V13.4 runtime label separately.  The
        # orchestration contract consumes runtime_provenance, so new plans do
        # not inherit the legacy alias.
        "provenance": "SEALED_LIVE_CAPTURE"
        if mode == "captured_replay"
        else ("REPLAY" if mode in {"replay", "replay-fallback"} else "LIVE_PROVIDER"),
        "runtime_provenance": "SEALED_REAL_REPLAY"
        if mode == "captured_replay"
        else ("REPLAY" if mode in {"replay", "replay-fallback"} else "LIVE_PROVIDER"),
        "candidates": materialized,
    }
