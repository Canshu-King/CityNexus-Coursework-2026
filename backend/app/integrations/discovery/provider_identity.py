"""Provider-layer destination resolution: a normalised query in, a *typed* provider outcome out.

This module answers one question only - "what did the geocoder say?" - and stops there.  It never
decides whether that answer has become CityNexus canonical identity, never touches a database, and
never writes a `canonical_destination_id`; the hand-over rule is that provider evidence and product
truth are decided by different parties (ORD-0188).

The four outcomes are deliberately not interchangeable, because the failure mode this exists to
prevent is a runtime failure being reported as "this place does not exist":

* ``MATCH``            - the provider answered and exactly one entity is destination-grade.
* ``AMBIGUOUS``        - the provider answered and ≥2 entities are destination-grade.  Nothing here
                         picks one; a caller that wants a single entity must ask a person.
* ``NO_MATCH``         - the provider answered *successfully* and returned nothing destination-grade.
                         Only reachable when at least one attempt actually answered, which is asserted.
* ``RUNTIME_FAILURE`  - no attempt answered: timeout, refused, rate-limited, unreachable, malformed.
                         Carries retryable evidence, and its candidate list means "unknown", not "empty".

Fixtures used by the tests are verbatim captures (see ``tests/fixtures/provider_identity/CAPTURES.md``),
including the real response shapes: Photon answers a county as a *relation* with ``extent`` and the
same county a second time as a bare *node* with no extent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol, Sequence

import httpx

#: photon.komoot.io answers `python-httpx/x.y` with HTTP 403 (measured 2026-09-21, one minute apart
#: from the same request carrying this UA, which answered 200).  A refusal that depends on the UA is
#: not a provider outage, so the UA belongs to the adapter, not to whoever builds the client.
PROVIDER_USER_AGENT = "CityNexus/7.0 (+https://citynexus.local/discovery)"

CRS_WGS84 = "WGS84"

#: `properties.type` values that denote an administrative entity rather than a point of interest.
DESTINATION_GRADE_TYPES = frozenset(
    {"country", "state", "region", "county", "municipality", "city", "district", "town"}
)

#: Reason vocabulary for RUNTIME_FAILURE.  Named separately on purpose: "we could not ask" and
#: "the answer is no" must never share a code.
REASON_TIMEOUT = "PROVIDER_TIMEOUT"
REASON_UNREACHABLE = "PROVIDER_UNREACHABLE"
REASON_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
REASON_REFUSED = "PROVIDER_REFUSED"
REASON_SERVER_ERROR = "PROVIDER_SERVER_ERROR"
REASON_BAD_RESPONSE = "PROVIDER_BAD_RESPONSE"
#: A successful answer that found nothing.  Named separately from every failure above, because
#: the product already carries this code (real_city.py PROVIDER_NO_RESULTS) and the two must not
#: converge into one bucket (lane F g11).
REASON_NO_RESULTS = "PROVIDER_NO_RESULTS"
#: We asked nothing.  Listed with the runtime reasons because it is reported as one.
REASON_EMPTY_QUERY = "EMPTY_QUERY"

_TRAILING_PUNCT = "。，、·．.！!？?：:；;（）()「」\"'"


class ProviderOutcome(str, Enum):
    MATCH = "MATCH"
    AMBIGUOUS = "AMBIGUOUS"
    NO_MATCH = "NO_MATCH"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"


def normalize_query(text: str) -> str:
    """Fold the ways a traveller types one place name into one provider query.

    Deliberately narrow: whitespace collapse and outer punctuation only.  It is *not* the product's
    identity normalisation and must not grow into it - the hash that the identity layer derives from
    a raw string is decided elsewhere (R10).
    """

    folded = re.sub(r"\s+", " ", str(text or "")).strip()
    return folded.strip(_TRAILING_PUNCT).strip()


@dataclass(frozen=True)
class ProviderAttempt:
    """One outbound call, reported as it happened."""

    endpoint: str
    answered: bool
    http_status: int | None
    elapsed_ms: float
    reason: str | None = None
    retry_after_s: float | None = None
    error: str | None = None
    #: Reply-level counts, filled only when the call produced a parseable reply.  `features_viewed`
    #: is what the provider actually sent; `candidates_before_filter` counts destination-grade
    #: entities before id/name/type drops; `drop_summary` names why each grade was refused.  All
    #: three being None means "no parsed reply to count", never "counted zero".
    features_viewed: int | None = None
    candidates_before_filter: int | None = None
    drop_summary: str | None = None


@dataclass(frozen=True)
class ProviderCanonicalCandidate:
    """A destination-grade entity, with the provenance a caller needs to audit it."""

    source_provider: str
    source_entity_id: str
    display_name: str
    centroid_wgs84: tuple[float, float]
    crs: str = CRS_WGS84
    extent_wgs84: tuple[float, float, float, float] | None = None
    admin_level: str | None = None
    country_code: str | None = None
    admin_area: str | None = None
    observed_at: str | None = None
    fetched_at: str | None = None
    raw_feature: dict[str, Any] = field(default_factory=dict)

    @property
    def provenance_complete(self) -> bool:
        """Provider id + a WGS-84 point + both timestamps is the minimum an auditor can chase."""

        return bool(
            self.source_provider
            and self.source_entity_id
            and self.centroid_wgs84
            and self.observed_at
            and self.fetched_at
            and self.crs == CRS_WGS84
        )


@dataclass(frozen=True)
class ProviderResolution:
    outcome: ProviderOutcome
    query: str
    normalized_query: str
    candidates: tuple[ProviderCanonicalCandidate, ...]
    attempts: tuple[ProviderAttempt, ...]
    retryable: bool = False
    reason: str | None = None
    answered: bool = False
    #: A healthy "nothing found" must never be readable as "we did not look".
    provider_replied: bool = False

    @property
    def single_candidate(self) -> ProviderCanonicalCandidate | None:
        return self.candidates[0] if self.outcome is ProviderOutcome.MATCH else None

    def to_evidence(self) -> dict[str, Any]:
        """Plain dict for a caller to store; carries no product-identity verdict."""

        return {
            "outcome": self.outcome.value,
            "query": self.query,
            "normalized_query": self.normalized_query,
            "answered": self.answered,
            "provider_replied": self.provider_replied,
            "retryable": self.retryable,
            "reason": self.reason,
            "attempts": [a.__dict__ for a in self.attempts],
            "candidates": [
                {
                    "source_provider": c.source_provider,
                    "source_entity_id": c.source_entity_id,
                    "display_name": c.display_name,
                    "centroid_wgs84": list(c.centroid_wgs84),
                    "extent_wgs84": list(c.extent_wgs84) if c.extent_wgs84 else None,
                    "crs": c.crs,
                    "admin_level": c.admin_level,
                    "country_code": c.country_code,
                    "admin_area": c.admin_area,
                    "observed_at": c.observed_at,
                    "fetched_at": c.fetched_at,
                    "provenance_complete": c.provenance_complete,
                }
                for c in self.candidates
            ],
        }


class ProviderTransport(Protocol):
    """Anything that can be asked for a URL and reports back.  Tests inject this; nothing else."""

    def get(self, url: str, params: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        ...


@dataclass
class HttpxTransport:
    """The real transport.  Kept this thin so a test can stand in with the same signature."""

    client: httpx.Client
    timeout_s: float = 20.0

    def get(self, url: str, params: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        response = self.client.get(url, params=params, timeout=self.timeout_s)
        headers = {k.lower(): v for k, v in response.headers.items()}
        return response.status_code, headers, response.content


def build_httpx_client(
    *, headers: dict[str, str] | None = None, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """A client that always identifies itself; `transport` is what tests hand to httpx directly."""

    merged = {"User-Agent": PROVIDER_USER_AGENT}
    merged.update(headers or {})
    return httpx.Client(headers=merged, transport=transport)


class PhotonIdentityProvider:
    """Asks one geocoder who a place is, and reports what came back.  No identity decisions."""

    provider_key = "photon"

    def __init__(
        self,
        *,
        transport: ProviderTransport,
        base_url: str = "https://photon.komoot.io",
        now: Callable[[], datetime] | None = None,
        limit: int = 10,
    ) -> None:
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._limit = limit
        self._now = now or (lambda: datetime.now(timezone.utc))

    def resolve(self, query: str) -> ProviderResolution:
        normalized = normalize_query(query)
        if not normalized:
            # Nothing was asked, so nothing was answered.  Reporting this as NO_MATCH would let a
            # caller conclude "that destination does not exist" from our own empty input.
            return ProviderResolution(
                outcome=ProviderOutcome.RUNTIME_FAILURE,
                query=str(query),
                normalized_query="",
                candidates=(),
                attempts=(),
                retryable=False,
                reason=REASON_EMPTY_QUERY,
                answered=False,
                provider_replied=False,
            )
        params = {"q": normalized, "limit": self._limit}
        try:
            status, headers, body = self._transport.get(f"{self._base_url}/api/", params)
        except Exception as exc:  # noqa: BLE001 - the transport's failure is a reading, not a raise
            return self._runtime_failure(query, normalized, _classify_exception(exc), exc)

        reason = _classify_status(status)
        if reason is not None:
            return self._runtime_failure(
                query,
                normalized,
                reason,
                RuntimeError(f"HTTP {status}"),
                retry_after_s=_retry_after(headers),
                http_status=status,
            )
        try:
            payload = _load_json(body)
        except Exception as exc:  # noqa: BLE001
            return self._runtime_failure(
                query, normalized, REASON_BAD_RESPONSE, exc, http_status=status
            )

        fetched_at = _iso(self._now())
        observed_at = headers.get("last-modified") or headers.get("date") or fetched_at
        features = payload.get("features") or []
        drops = {"no_geometry": 0, "no_name_or_id": 0, "not_destination_grade": 0}
        for feature in features:
            props = feature.get("properties") or {}
            geometry = feature.get("geometry") or {}
            if not (geometry.get("coordinates") or []):
                drops["no_geometry"] += 1
            elif props.get("name") is None or props.get("osm_id") is None:
                drops["no_name_or_id"] += 1
            elif props.get("type") not in DESTINATION_GRADE_TYPES:
                drops["not_destination_grade"] += 1
        graded = [
            _candidate(f, self.provider_key, observed_at, fetched_at)
            for f in features
            if _is_destination_grade(f)
        ]
        graded = [c for c in graded if c is not None]
        drop_summary = "; ".join(
            "%s=%d" % (k, v) for k, v in drops.items() if v
        ) or "none"
        return ProviderResolution(
            outcome=_grade_outcome(graded),
            query=str(query),
            normalized_query=normalized,
            candidates=tuple(graded),
            attempts=(
                ProviderAttempt(
                    endpoint=f"{self._base_url}/api/",
                    answered=True,
                    http_status=status,
                    elapsed_ms=0.0,
                    features_viewed=len(features),
                    candidates_before_filter=sum(1 for f in features if _is_destination_grade(f)),
                    drop_summary="drops: " + drop_summary,
                ),
            ),
            answered=True,
            provider_replied=True,
            reason=None if graded else REASON_NO_RESULTS,
        )

    def _runtime_failure(self, query, normalized, reason, exc, *, retry_after_s=None, http_status=None):
        return ProviderResolution(
            outcome=ProviderOutcome.RUNTIME_FAILURE,
            query=str(query),
            normalized_query=normalized,
            candidates=(),
            attempts=(
                ProviderAttempt(
                    endpoint=f"{self._base_url}/api/",
                    answered=False,
                    http_status=http_status,
                    elapsed_ms=0.0,
                    reason=reason,
                    retry_after_s=retry_after_s,
                    error=f"{type(exc).__name__}: {exc}"[:200],
                ),
            ),
            retryable=reason in {REASON_TIMEOUT, REASON_UNREACHABLE, REASON_RATE_LIMITED},
            reason=reason,
            answered=False,
            provider_replied=False,
        )


def _is_destination_grade(feature: dict[str, Any]) -> bool:
    props = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    if not (geometry.get("coordinates") or []):
        return False
    if props.get("name") is None or props.get("osm_id") is None:
        return False
    if props.get("type") not in DESTINATION_GRADE_TYPES:
        return False
    return True


def _candidate(
    feature: dict[str, Any], provider_key: str, observed_at: str, fetched_at: str
) -> ProviderCanonicalCandidate | None:
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2:
        return None
    osm_type = str(props.get("osm_type") or "unknown")
    return ProviderCanonicalCandidate(
        source_provider=provider_key,
        source_entity_id=f"{osm_type}:{props.get('osm_id')}",
        display_name=str(props.get("name")),
        centroid_wgs84=(float(coords[1]), float(coords[0])),
        # Photon's `extent` is not documented as (w,s,e,n) and tonight's captures show the second
        # value larger than the fourth, so it is normalised by min/max and kept verbatim in
        # `raw_feature` rather than trusted positionally.
        extent_wgs84=_extent(props.get("extent")),
        country_code=props.get("countrycode"),
        admin_area=props.get("state") or props.get("county"),
        observed_at=observed_at,
        fetched_at=fetched_at,
        raw_feature=feature,
    )


def _extent(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        lons = sorted((float(raw[0]), float(raw[2])))
        lats = sorted((float(raw[1]), float(raw[3])))
    except (TypeError, ValueError):
        return None
    return (lons[0], lats[0], lons[1], lats[1])


def _grade_outcome(graded: list[ProviderCanonicalCandidate]) -> ProviderOutcome:
    if not graded:
        return ProviderOutcome.NO_MATCH
    if len({c.source_entity_id for c in graded}) > 1:
        # Two entities that both claim to be this place name: choosing between them is a human
        # decision (R10 ⑦), so this layer refuses to rank them.
        return ProviderOutcome.AMBIGUOUS
    return ProviderOutcome.MATCH


def _classify_status(status: int) -> str | None:
    if 200 <= status < 300:
        return None
    if status in (401, 403):
        return REASON_REFUSED
    if status == 429:
        return REASON_RATE_LIMITED
    if 500 <= status < 600:
        # A gateway timeout says "ask again"; a server error says "that endpoint is broken".  Both
        # are runtime facts, but collapsing them loses the distinction between retry and reroute.
        return REASON_TIMEOUT if status == 504 else REASON_SERVER_ERROR
    return REASON_BAD_RESPONSE


def _classify_exception(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if isinstance(exc, httpx.TimeoutException) or "timeout" in name:
        return REASON_TIMEOUT
    if isinstance(exc, (httpx.ConnectError, OSError)) or "unreachable" in name or "connect" in name:
        return REASON_UNREACHABLE
    return REASON_BAD_RESPONSE


def _retry_after(headers: dict[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _load_json(body: bytes) -> dict[str, Any]:
    import json

    parsed = json.loads(body.decode("utf-8", "replace"))
    if not isinstance(parsed, dict):
        raise ValueError(f"expected an object, got {type(parsed).__name__}")
    return parsed


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def resolve_from_endpoints(
    query: str,
    *,
    providers: Sequence[tuple[str, ProviderTransport]],
    base_url: str = "https://photon.komoot.io",
    now: Callable[[], datetime] | None = None,
) -> ProviderResolution:
    """Try each configured geocoder until one *answers*, and keep every attempt as evidence.

    "Answered" is the pivot: a 200 with zero features ends the loop (that is the provider's answer),
    while timeouts/refusals fall through to the next endpoint and, if none answers, become a single
    RUNTIME_FAILURE carrying all attempts.  An empty candidate list after this function therefore
    means "someone replied and there is nothing", never "nobody was reachable".
    """

    attempts: list[ProviderAttempt] = []
    last: ProviderResolution | None = None
    for provider_key, transport in providers:
        provider = PhotonIdentityProvider(
            transport=_TimingTransport(provider_key, transport, attempts), base_url=base_url, now=now
        )
        resolution = provider.resolve(query)
        resolution = ProviderResolution(
            outcome=resolution.outcome,
            query=resolution.query,
            normalized_query=resolution.normalized_query,
            candidates=resolution.candidates,
            attempts=tuple(attempts),
            retryable=resolution.retryable,
            reason=resolution.reason,
            answered=resolution.answered,
            provider_replied=resolution.provider_replied,
        )
        if resolution.answered:
            return resolution
        last = resolution
    if last is None:
        return ProviderResolution(
            outcome=ProviderOutcome.RUNTIME_FAILURE,
            query=str(query),
            normalized_query=normalize_query(query),
            candidates=(),
            attempts=(),
            retryable=True,
            reason=REASON_UNREACHABLE,
        )
    return ProviderResolution(
        outcome=ProviderOutcome.RUNTIME_FAILURE,
        query=last.query,
        normalized_query=last.normalized_query,
        candidates=(),
        attempts=tuple(attempts),
        retryable=any(a.reason in {REASON_TIMEOUT, REASON_UNREACHABLE, REASON_RATE_LIMITED} for a in attempts),
        reason=_merge_reasons(attempts),
        answered=False,
        provider_replied=False,
    )


def _merge_reasons(attempts: Sequence[ProviderAttempt]) -> str:
    reasons = [a.reason for a in attempts if a.reason]
    for preferred in (REASON_UNREACHABLE, REASON_TIMEOUT, REASON_RATE_LIMITED, REASON_REFUSED):
        if preferred in reasons:
            return preferred
    return reasons[0] if reasons else REASON_BAD_RESPONSE


@dataclass
class _TimingTransport:
    """Wraps a transport so each attempt is recorded, without the adapter owning the clock."""

    provider_key: str
    inner: ProviderTransport
    sink: list[ProviderAttempt]

    def get(self, url: str, params: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        started = datetime.now(timezone.utc)
        try:
            status, headers, body = self.inner.get(url, params)
        except Exception as exc:  # noqa: BLE001
            elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
            self.sink.append(
                ProviderAttempt(
                    endpoint=url,
                    answered=False,
                    http_status=None,
                    elapsed_ms=round(elapsed, 1),
                    reason=_classify_exception(exc),
                    error=f"{type(exc).__name__}: {exc}"[:200],
                )
            )
            raise
        elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
        self.sink.append(
            ProviderAttempt(
                endpoint=url,
                answered=(200 <= status < 300),
                http_status=status,
                elapsed_ms=round(elapsed, 1),
                reason=_classify_status(status),
                retry_after_s=_retry_after(headers),
            )
        )
        return status, headers, body
