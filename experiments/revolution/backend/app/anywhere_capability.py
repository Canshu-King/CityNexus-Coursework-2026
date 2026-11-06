"""ORD-0095-1 (CITYNEXUS-ANYWHERE §2/§5/§10): what the backend actually knows about a destination.

Three rules shape this file, and each of them exists because the project has been charged with
breaking it before:

1. **A capability verdict is a server-side policy result, never a count.**
   `READY` requires `readiness_policy_version` to be the thing that decided it, so a destination
   with two museums and no food cannot read "ready for everything" (arbiter v0.2 forbids
   `places.length > 0` explicitly).  The policy is versioned, so "why did this city change state"
   has an answer.

2. **Structure and runtime are two different truths.**  `capability` says what this destination
   *is* (READY / HYDRATABLE / UNAVAILABLE - three values, and deliberately no fourth "unknown":
   "we did not look" is expressed by an *absent* observation, never by a fabricated `OK`).  A
   provider being momentarily unreachable is the other axis and does not make a destination
   structurally unusable, so a resolver failure keeps the destination retryable (R1).

3. **Provenance is never upgraded on the way in.**  Each fact carries the class of source it came
   from (`GOLDEN_FIXTURE` / `REMOTE_PROVIDER` / `SIMULATED` / `REPLAY` / `OFFLINE_SNAPSHOT`) and
   normalisation may only report it, never strengthen it: a simulated row does not become a
   fetched one by passing through a converter.

Everything here reads the database the product already has.  By default nothing leaves the
process: this module answers "what can we plan with *now*", and a provider cannot make that answer
truer.  The one exception is the ORD-0191 ⑤ leg below, which is off unless a deployment names a
resolver (`CITYNEXUS_ANYWHERE_RESOLVER=photon`), because a place this catalog has never heard of is
only *unusable* relative to what we are allowed to consult - and saying otherwise about 云和县,
which exists, is its own kind of false answer.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
import os
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import CityService, Place

# The three versions that make up a durable fingerprint.  They are constants of *this* file
# because they describe this code's behaviour: changing a bucket mapping or a minimum here is a
# new policy, and the new policy must not silently inherit the old policy's hydrated catalogs.
CATALOG_SCHEMA_VERSION = "catalog-v1"
READINESS_POLICY_VERSION = "readiness-v1"
PROVIDER_POLICY_VERSION = "provider-v1"

# The arbiter's structural axis, plus the cell added on 2026-09-22 (answer "one" of `5765903862`,
# relayed by ORD-0197).  **The name on this cell is the arbiter's, not this file's.** Ruling
# `5771378382` (choose B) froze `UNKNOWN_RETRYABLE` in
# `P7_UNKNOWN_RETRYABLE_CAPABILITY_ACCEPTANCE.md`; this module had shipped it as `NOT_ANSWERED`
# while a comment here claimed the name was authorised - that conflated "adding a cell is approved"
# with "you may name it", and three more lanes had already started keying off the spelling by then.
# No alias and no dual-write: one token, and callers that should not care are kept decoupled by
# reading `retryable` + `recommended_action` instead of the cell name.
# This is not the UNKNOWN this file has always refused: "nobody looked" stays an absent
# `provider_status` and a structural answer.  `UNKNOWN_RETRYABLE` means
# the opposite - we looked, and nothing that could answer did - which is a measured runtime fact and
# therefore has to be sayable without choosing between the two lies a three-cell vocabulary forced:
# `UNAVAILABLE` ("this place cannot be planned") and `HYDRATABLE + START_HYDRATION` ("you may
# prepare it now").  The measured consequence of that forced choice is the panel inviting a
# traveller to prepare a destination nobody has named, with answered=false.
# The invariant that keeps the new cell from becoming a fifth "looks like a state" string is
# mechanical, not conventional: UNKNOWN_RETRYABLE => retryable is True and a provider_status exists.
# A caller that reads the state without the pair has not read this answer, it has read a word.
CAPABILITY_STATES = ("READY", "HYDRATABLE", "UNKNOWN_RETRYABLE", "UNAVAILABLE")
COVERAGE_STATES = ("READY", "PARTIAL", "MISSING")
COVERAGE_KEYS = ("attractions", "food", "nightlife", "lodging", "mobility", "booking")
# Runtime axis (R2).  Emitted only when an observation actually exists.
PROVIDER_STATUSES = ("OK", "RATE_LIMITED", "UNREACHABLE", "PARTIAL")
SOURCE_KINDS = ("GOLDEN_FIXTURE", "REMOTE_PROVIDER", "SIMULATED", "REPLAY", "OFFLINE_SNAPSHOT")

READY = "READY"
PARTIAL = "PARTIAL"
MISSING = "MISSING"
UNKNOWN_RETRYABLE = "UNKNOWN_RETRYABLE"

# Which intent each stored category serves.  This mapping *is* the policy: a category that is not
# listed here serves no intent, which is a decision with a version number, not an oversight.
INTENT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "attractions": (
        "museum", "heritage", "landmark", "park", "gallery", "attraction", "attractions",
        "viewpoint", "tourist_attraction", "sights", "monument", "temple", "garden", "景点", "景区",
    ),
    "food": ("food", "foods", "restaurant", "restaurants", "cafe", "street_food", "fast_food",
             "bakery", "美食", "餐饮"),
    "nightlife": ("nightlife", "night_life", "bar", "pub", "club", "live_music", "酒馆", "夜生活"),
    "lodging": ("lodging", "hotel", "hostel", "apartment", "guest_house", "motel", "resort",
                "accommodation", "住宿", "酒店"),
    # mobility and booking are not place categories; they are measured below.
    "mobility": (),
    "booking": (),
}
BOOKABLE_SERVICE_TYPES = ("bookable_experience", "tradable_food")
MOBILITY_SERVICE_TYPES = ("mobility_hub", "bike_share", "transit_station", "parking")

# Minimum *usable* material per intent.  Deliberately not one: "at least one" is exactly the
# `places.length > 0` rule the arbitration named as forbidden, and a city with one bar is not a
# city you can plan evenings in.
MIN_READY_CANDIDATES = {
    "attractions": 3,
    "food": 3,
    "nightlife": 2,
    "lodging": 2,
    "mobility": 3,
    "booking": 2,
}
# Share of a destination's place pairs for which we hold an *explicit* travel fact (not the
# fixture's blanket default) before mobility counts as ready.
MOBILITY_EXPLICIT_RATIO_READY = 0.8
STALE_AFTER_DAYS = 90

REASON_CATALOG_CURRENT = "CATALOG_CURRENT"
REASON_COVERAGE_INCOMPLETE = "COVERAGE_INCOMPLETE"
REASON_CATALOG_STALE = "CATALOG_STALE"
REASON_NO_CANONICAL_IDENTITY = "NO_CANONICAL_IDENTITY"
# Two codes name this query and no winner may be guessed (arbiter `e4b7faa3` state AMBIGUOUS).
# It is a fact about the *request*, which is why it is a reason code and not a fourth `capability`.
REASON_DESTINATION_AMBIGUOUS = "DESTINATION_AMBIGUOUS"
# R1: these are runtime facts and must never be dressed up as the structural verdict.
REASON_RESOLVER_UNAVAILABLE = "DESTINATION_RESOLVER_UNAVAILABLE"
TRANSIENT_REASONS = (
    REASON_RESOLVER_UNAVAILABLE,
    "PROVIDER_RATE_LIMITED",
    "PROVIDER_TIMEOUT",
    "PROVIDER_NETWORK_FAILURE",
    "PROVIDER_SERVER_ERROR",
    "CATALOG_PREPARING",
)

ACTION_START_HYDRATION = "START_HYDRATION"
ACTION_CHOOSE_ANOTHER_DESTINATION = "CHOOSE_ANOTHER_DESTINATION"
ACTION_ACCEPT_PARTIAL_PLAN = "ACCEPT_PARTIAL_PLAN"
#: Added with the capability cell below by the arbiter's option-one ruling (`5765903862`, relayed
#: in ORD-0197's acceptance note): the ruling is not only "give the state a name" but "make the UI
#: action point at trying again later instead of at preparing".  With three actions and none of them
#: meaning "later", that half was unimplementable without a fourth word - so it is added here, named
#: once, and raised as its own R1 question rather than smuggled in behind a state rename.
ACTION_RETRY_LATER = "RETRY_LATER"

#: The named gap the arbitration's acceptance wording asks for (ruling A: `HYDRATABLE` plus
#: `MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE`).  Until this constant gained an emitter it existed only
#: as a literal inside one door, which is how a ruling can read as satisfied while no product answer
#: ever says it - a caller asking "why is this destination not plannable-as-hard" got no name from
#: the service, only from a test.  Emitted by the product, read by the gate.
GAP_MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE = "MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE"

# ---------------------------------------------------------------------------------------------
# ORD-0167 (arbiter `e4b7faa3`): what a destination *request* is allowed to become.
#
# Freeze sentence: *Unknown destination text may identify a request to resolve a destination; it
# does not identify a destination.*  Until this commit the build broke that sentence twice over:
# it wrote `pending:<the traveller's own words>` into `canonical_destination_id`, and it wrote the
# words *after* `_normalise` had eaten their punctuation.  So an identity column held a text
# derivative, and a fingerprint, an idempotency rule and a catalog join all quietly believed it.
#
# Nothing below is written into an identity column any more.  The prefix survives only so the
# migration can recognise rows the old build created.
LEGACY_PENDING_PREFIX = "pending:"

# What the resolver concluded about a request.  This is a third axis, deliberately separate from
# `capability` (what the destination *is*) and `provider_status` (what a provider said just now).
DESTINATION_STATES = ("RESOLVED", "AMBIGUOUS", "NO_MATCH", "RUNTIME_FAILURE")

# Fingerprint namespace for a request whose destination is still unnamed.  Naming the material in
# the key is the point: it is a fingerprint *of a request to resolve*, never a place identifier.
PENDING_REQUEST_NAMESPACE = "pending-request-v1"
# The request surface a pending fingerprint is scoped to.  Hydration jobs are destination-scoped
# by design (a journey-less tap and a journey-bearing tap must converge on one provider chain), so
# the scope is the surface, not the caller - the caller's id must not split one job in two.
PENDING_REQUEST_SCOPE = "catalog-hydration-v1"

# How much trust the stored label earns.  A legacy row whose only surviving text had already been
# normalised is marked as such rather than being presented as the traveller's exact words.
PENDING_LABEL_EXACT_RAW = "EXACT_RAW"
PENDING_LABEL_LEGACY_NORMALIZED = "LEGACY_NORMALIZED"
PENDING_LABEL_FIDELITIES = (PENDING_LABEL_EXACT_RAW, PENDING_LABEL_LEGACY_NORMALIZED)

# Suffixes a traveller types that do not change which place is meant.  They are tolerated when
# *matching* a query against names the catalog already holds, and are never applied to a stored
# label: matching may normalise, testimony may not.  Longest first, so 自治县 is not cut to 自治.
_ADMINISTRATIVE_SUFFIXES = (
    "特别行政区", "自治区", "自治州", "自治县", "地区", "林区", "省", "市", "区", "县",
)

# ---------------------------------------------------------------------------------------------
# ORD-0191 ⑤: a destination may also be named by a provider this process does not hold.
#
# The switch is **off by default** and read **per call**.  Both halves matter:
#   * off by default (R6) because a capability read is on the hot path of ~1100 doors and of every
#     screen paint; silently calling a public geocoder from it would be §17C's exact forbidden move,
#     and would turn "we could not reach the world" into a product answer;
#   * read per call because every import-time setting in this project has already been measured
#     changing a whole suite from the shell it happened to be started from - a developer's exported
#     key must not decide what the product believes about 云和县.
# A deployment that wants the leg says so explicitly: `CITYNEXUS_ANYWHERE_RESOLVER=photon`.
RESOLVER_ENV = "CITYNEXUS_ANYWHERE_RESOLVER"
RESOLVER_SEAMS = ("off", "photon")
# An identity that came from the world, not from a text derivative: the OpenStreetMap entity the
# provider pointed at.  Prefixed so nobody reads it as one of our internal `CN-*` vocabulary codes.
PROVIDER_IDENTITY_PREFIX = "OSM:"
# Measured 2026-09-22 against the live resolver: asking for 云和县 returns the county boundary as
# `R:3289208` (with a four-number extent) *and* a bare node `N:244082768` carrying the same name.
# Those are not two places the traveller must choose between - they are one place expressed twice,
# and answering AMBIGUOUS at that would be a machine asking a human to arbitrate its own index.
# The rule is versioned and named because a caller must never assume it: an administrative **area**
# is the destination; a point that merely marks it is not.  Two bounded areas with the same name
# (e.g. the same county name in two countries) remain genuinely ambiguous and stay AMBIGUOUS.
DESTINATION_DISCRIMINATION_POLICY_VERSION = "destination-discrimination-v1"


def anywhere_resolver_name() -> str:
    """Which seam may name a destination today (folded, defaulted, never guessed)."""

    return (os.getenv(RESOLVER_ENV) or "off").strip().lower()


def anywhere_resolver_attempted(name: str | None = None) -> bool:
    """Is a provider attempt authorised?  A misspelled seam name is not an implicit yes."""

    seam = (name if name is not None else anywhere_resolver_name())
    return seam == "photon"

_TRAVELER_FACING_LABELS = {
    "CNX-DEMO": ("demo", "演示", "模拟", "cnx-demo"),
}


@dataclass
class DestinationResolution:
    """What a query became, how sure we are about it, and what may be stored where.

    `canonical_destination_id` is the only identity field, and it is **null whenever this build
    cannot name a real place**.  The traveller's words belong in `pending_destination_label`,
    verbatim; the folded form used for matching belongs in `resolver_query_normalized`; and the
    opaque `destination_pending_key` is correlation material for one operation.  None of the last
    three is an identifier, and none may be joined on.
    """

    query: str
    canonical_destination_id: str | None
    city_code: str | None
    method: str
    state: str = "RESOLVED"
    matched_label: str | None = None
    reason_code: str = ""
    resolver_attempted: bool = False
    resolver_status: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # Evidence the provider offered and this layer refused: an entity reference it cannot be held
    # to, or a position that is not a legal WGS-84 point.  Deliberately a separate field, because
    # `candidates` means "contenders a traveller may choose between" - broken evidence is not a
    # choice to offer, and folding it in would let a refused record read as an option.
    malformed_candidates: list[dict[str, Any]] = field(default_factory=list)
    retryable: bool = False
    pending_destination_label: str | None = None
    resolver_query_normalized: str | None = None
    destination_pending_key: str | None = None
    pending_label_fidelity: str | None = None
    # Only filled by a provider answer: the facts that make the identity somebody else's record
    # rather than ours (entity id, WGS-84 centroid, extent) and the runtime observation behind it.
    provenance: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None

    @property
    def resolved(self) -> bool:
        return self.canonical_destination_id is not None and self.city_code is not None


def _normalise(text: str) -> str:
    """Fold a spelling for *comparison*.  Never store this as what the user wrote."""

    return re.sub(r"[\s_\-·、,，.。]+", "", str(text or "")).strip().lower()


def _match_forms(text: str) -> set[str]:
    """The spellings a query may be matched under - and never the spellings it is stored under.

    `北京市` and `北京` are one place, and until now the resolver answered as if they were two:
    the suffixed form fell through to the unresolved branch, where its text became an identity.
    Stripping a closed list of administrative suffixes fixes the match without inventing
    anything - the winner still has to be a code the catalog or the vocabulary actually holds.
    """

    base = _normalise(text)
    forms = {base} if base else set()
    for suffix in _ADMINISTRATIVE_SUFFIXES:  # declared longest-first
        if base.endswith(suffix) and len(base) > len(suffix):
            forms.add(base[: -len(suffix)])
    return {form for form in forms if form}


def pending_correlation_key(operation_id: str) -> str:
    """An opaque token for one unresolved operation - correlation material, never identity.

    Derived from the *operation's* id, and deliberately not from the query text: a text-derived
    token would be a fingerprint wearing an identity's clothes, and it would collide two
    travellers' requests into each other's row.  Stability comes from the row, so a re-read of
    the same job names the same token, and a capability answer - which has no job yet - has none.
    The deterministic convergence a repeated request needs is the fingerprint
    (`capability_fingerprint`), a separate concept on purpose.
    """

    return f"pend-{operation_id}"


def _unresolved_identity(query: str) -> dict[str, Any]:
    """The non-identity columns an unresolved request is recorded with.

    No `destination_pending_key` here: that token names an operation, and an answer on a read has
    not opened one.  Inventing one per read would mint a per-response identifier for a request that
    has no work behind it - the same mistake, one column further along.
    """

    raw = str(query if query is not None else "")
    return {
        "pending_destination_label": raw,
        "resolver_query_normalized": _normalise(raw) or None,
        "destination_pending_key": None,
        "pending_label_fidelity": PENDING_LABEL_EXACT_RAW,
    }


def _resolved_identity(query: str) -> dict[str, Any]:
    """A resolved request keeps the raw words for audit and mints no pending token."""

    raw = str(query if query is not None else "")
    return {
        "pending_destination_label": raw,
        "resolver_query_normalized": _normalise(raw) or None,
        "destination_pending_key": None,
        "pending_label_fidelity": PENDING_LABEL_EXACT_RAW,
    }


def identity_fields(resolution: "DestinationResolution") -> dict[str, Any]:
    """The one place the five identity columns are assembled.

    The store and the wire must not drift into telling two stories about one request, so both
    read this - and `canonical_destination_id` is the only key in here that is ever an identity.
    """

    return {
        "canonical_destination_id": resolution.canonical_destination_id,
        "pending_destination_label": resolution.pending_destination_label,
        "resolver_query_normalized": resolution.resolver_query_normalized,
        "destination_pending_key": resolution.destination_pending_key,
        "pending_label_fidelity": resolution.pending_label_fidelity,
    }


def audit_evidence(resolution: "DestinationResolution") -> dict[str, Any]:
    """What was asked and what came back - the part of the answer that has to *survive* it.

    Kept apart from `identity_fields` on purpose: those five columns decide what the product may
    treat as a place, while this is history.  `final outcome != audit history` is the rule - a
    conclusion of NO_MATCH is produced by whichever mirror answered, and the timeout that came
    before it is a fact about our infrastructure that has to be readable afterwards.  Choosing to
    keep only the last attempt would make an outage look like an empty region exactly where nobody
    is looking at a response, because a job outlives the request that started it.

    A named identity is only chaseable the same way: the entity reference, extent, WGS84 point and
    the timestamps that came with them belong with the job that used them, not only with the read
    that happened to render them.
    """

    evidence: dict[str, Any] = {}
    observation = resolution.observation
    if isinstance(observation, dict) and observation.get("attempts"):
        evidence["provider_attempts"] = [dict(item) for item in observation["attempts"]]
    if resolution.provenance:
        evidence["destination_provenance"] = dict(resolution.provenance)
    if resolution.malformed_candidates:
        evidence["malformed_candidates"] = [dict(item) for item in resolution.malformed_candidates]
    return evidence


def backfill_pending_identity_row(
    canonical_destination_id: str | None,
    request_json: str | None,
    fingerprint: str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """What one pre-ORD-0167 row becomes - decided where the vocabulary lives, not in the DDL.

    The rule the arbiter wrote (`e4b7faa3`, migration 1-5): keep the operation and its history,
    null the invented identity, and backfill the label from the **best available raw source**.
    For this product that source is the job's own `request_json.query`, which was always written
    verbatim; only when nothing but the already-folded `pending:` payload survives does the label
    come from there - and then it is marked `LEGACY_NORMALIZED`, because calling a folded string
    "what the user typed" would be the database forging a witness statement.

    The fingerprint is recomputed under `pending-request-v1` so a repeat of the same request still
    converges on *this* row instead of opening a second job beside it.  The old key is kept in
    `legacy_fingerprint` rather than dropped: an operation's history is not scratch space.
    """

    identity = str(canonical_destination_id or "")
    carried = request_json
    raw_from_request: str | None = None
    try:
        payload = json.loads(carried or "{}")
        if isinstance(payload, dict) and isinstance(payload.get("query"), str):
            raw_from_request = payload["query"]
    except (TypeError, ValueError):
        raw_from_request = None

    if not identity.startswith(LEGACY_PENDING_PREFIX):
        # A real identity stays exactly where it is; the raw words are attached for audit only.
        label = raw_from_request
        return {
            "canonical_destination_id": identity or None,
            "pending_destination_label": label,
            "resolver_query_normalized": _normalise(label or "") or None,
            "destination_pending_key": None,
            "pending_label_fidelity": PENDING_LABEL_EXACT_RAW if label is not None else None,
            "legacy_fingerprint": None,
            "fingerprint": fingerprint,
        }

    survivor = identity[len(LEGACY_PENDING_PREFIX) :]
    if survivor == "(blank)":
        survivor = ""
    if raw_from_request:
        label, fidelity = raw_from_request, PENDING_LABEL_EXACT_RAW
    else:
        label, fidelity = survivor, PENDING_LABEL_LEGACY_NORMALIZED
    normalised = _normalise(label) or (survivor or None)
    return {
        "canonical_destination_id": None,
        "pending_destination_label": label,
        "resolver_query_normalized": normalised,
        "destination_pending_key": pending_correlation_key(
            operation_id or uuid.uuid4().hex
        ),
        "pending_label_fidelity": fidelity,
        "legacy_fingerprint": fingerprint,
        "fingerprint": capability_fingerprint(None, resolver_query_normalized=normalised),
    }


def _alias_forms(aliases: Iterable[object]) -> set[str]:
    return {form for item in aliases if (form := _normalise(str(item or "")))}


def _catalog_holdings(session: Session) -> dict[str, int]:
    """Codes the catalog really holds rows for - the strongest statement this build can make."""

    return {
        str(code): int(count or 0)
        for code, count in session.execute(
            select(Place.city_code, func.count(Place.id)).group_by(Place.city_code)
        ).all()
    }


def _display_labels() -> dict[str, str]:
    """The traveller-facing names of the city codes, taken from the product's own vocabulary.

    Imported lazily because `main.py` owns that table today (single source, no copy).  It is *not*
    swallowed any more: until ORD-0167 a failure here returned `{}`, which quietly turned "one of
    my sources broke" into "this destination does not exist" - R1's forbidden laundering, in the
    one place no provider is involved and so nothing else would ever notice.
    """

    from app.main import _CITY_DISPLAY_NAMES  # single source, no copy

    return {f"CN-{code}": label for code, label in (_CITY_DISPLAY_NAMES or {}).items()}


def _provider_transport() -> Any:
    """The one seam a door injects an httpx transport through; production asks for the real stack.

    `None` means "whatever httpx would do", which keeps proxy policy where it already lives
    (`build_httpx_client`, `docs/NETWORK_POLICY.md`).  A door hands this to `httpx.MockTransport`,
    so the outcome it exercises is produced by the provider layer's own classification, not by a
    copy of it made for the test.
    """

    return None


@contextmanager
def _provider_identity_resolver() -> Any:
    """A callable that asks the registered geocoders one question and returns B's typed answer.

    Per ORD-0197 (arbiter 甲) this module no longer speaks to `PhotonGeocoder`: "which of these
    answers is a destination" can only be answered in one place, and two entrances writing that
    rule each is exactly how the classification collapsed into `unresolved:<hash>` once already.
    Whether an answer becomes CityNexus canonical identity is decided below, here.

    It goes through `resolve_from_endpoints` rather than driving `PhotonIdentityProvider`
    directly, on measured grounds: that runner is what wraps the transport with `_TimingTransport`,
    and asking the provider myself recorded every attempt as `elapsed_ms=0.0` on a call that
    actually took 1.8 s - evidence that looks recorded and is not.  The runner also gives endpoint
    failover for free when a second geocoder is ever registered.

    A context manager because the httpx client holds a connection pool: opening one per question
    and never closing it is a leak only visible after a thousand reads, which is precisely what a
    green suite hides.
    """

    from app.core.config import settings
    from app.integrations.discovery.provider_identity import (
        HttpxTransport,
        PhotonIdentityProvider,
        build_httpx_client,
        resolve_from_endpoints,
    )
    from app.integrations.provider_config import ExternalProviderSettings

    provider_settings = ExternalProviderSettings()
    client = build_httpx_client(transport=_provider_transport())
    try:
        transport = HttpxTransport(
            client=client, timeout_s=float(settings.real_city_timeout_seconds)
        )

        def _ask(query: str) -> Any:
            return resolve_from_endpoints(
                query,
                providers=[(PhotonIdentityProvider.provider_key, transport)],
                base_url=provider_settings.photon_base_url,
            )

        yield _ask
    finally:
        client.close()


def _provider_status_of(resolution: Any) -> str:
    """Map the typed answer onto the frozen `provider_status` vocabulary.

    `answered` outranks `reason`: a provider that replied and knew nothing was reachable, and
    calling that `UNREACHABLE` would launder "the answer is no" into "we could not ask" - the same
    pair of confusions, pointed the other way.
    """

    import app.integrations.discovery.provider_identity as provider_layer

    if getattr(resolution, "answered", False):
        return "OK"
    if getattr(resolution, "reason", None) == provider_layer.REASON_RATE_LIMITED:
        return "RATE_LIMITED"
    return "UNREACHABLE"


def _attempt_evidence(resolution: Any) -> list[dict[str, Any]]:
    """What was asked, who answered, with what status - carried into the product's own record.

    The provider layer keeps attempts even on the failure path; R14's "name the cause" needs them
    to survive into the job, not to stop one layer below the wire.
    """

    evidence: list[dict[str, Any]] = []
    for attempt in getattr(resolution, "attempts", ()) or ():
        evidence.append(
            {
                "endpoint": getattr(attempt, "endpoint", None),
                "answered": getattr(attempt, "answered", None),
                "http_status": getattr(attempt, "http_status", None),
                "elapsed_ms": getattr(attempt, "elapsed_ms", None),
                "reason": getattr(attempt, "reason", None),
                "retry_after_s": getattr(attempt, "retry_after_s", None),
                # Reply-level counts from the provider layer (ORD-0201 ②): a reply that carried
                # features but no destination-grade candidates is "malformed/off-grade evidence",
                # not "the place does not exist".  None on all three means no parsed reply.
                "features_viewed": getattr(attempt, "features_viewed", None),
                "candidates_before_filter": getattr(attempt, "candidates_before_filter", None),
                "drop_summary": getattr(attempt, "drop_summary", None),
            }
        )
    return evidence


def _world_answer_reason_names() -> frozenset[str]:
    """The provider layer's reasons that describe the world, not this exchange.

    Spelled by borrowing the provider layer's constant rather than repeating its text: this file has
    no vocabulary of its own to add, and a copy is how two lists drift apart.
    """

    import app.integrations.discovery.provider_identity as provider_layer

    return frozenset({provider_layer.REASON_NO_RESULTS})


def _attribution_fields(resolution: "DestinationResolution") -> dict[str, Any]:
    """The four fields R14 §8 names, derived once from what was actually measured.

    The names follow the arbitration's wording (`attribution_reason`, `evidence_kind`,
    `http_status`, `source_layer`) so the screen, the announcement, the telemetry and the recovery
    action can all read one classification instead of writing their own - which is the whole point
    of §8, and the reason one of the four on its own is not a delivery.

    The tier that spoke is decided in the frozen priority order - a code from the service, then an
    HTTP status, then a transport exception.  A transport answer reports `http_status: null` and
    not `0`: a status that was never received is absent, and zero is a number somebody could
    mistake for a reply.
    """

    observation = resolution.observation
    if not isinstance(observation, dict):
        return {}
    reason = observation.get("reason")
    if not reason:
        return {}
    attempts = [item for item in (observation.get("attempts") or []) if isinstance(item, dict)]
    answered = [item for item in attempts if item.get("answered")]
    # 修法选 ①（F 给了两条、故意没替我选，这里写清为什么选这条）：
    # 只有当"答了"的那次尝试真的带着一枚状态码时，`evidence_kind=HTTP_RESPONSE` 才是可说出口的话；
    # 否则它就是自相矛盾（"我收到了 HTTP 回答"＋"我没有状态码"），落回 transport 形状。
    # 不选 ②（拒绝产出这个组合）的理由：②要么在 attribution_applicable=true 时交回空组——
    # 那正是仲裁 15:58 判红的那格；要么新造一格"我拒绝归因"，而词表不是我一个人能扩的。
    # 顺带把红队 15:41 那条"随便编一个 200 就完全合规"的支路关掉了：`http_status` 只从
    # "answered 且自带状态码"的 attempt 里读，编造者得先造出一次真答复的形状。
    measured = [
        item.get("http_status")
        for item in answered
        if item.get("http_status") is not None
    ]
    if answered and measured:
        evidence_kind = "HTTP_RESPONSE"
        http_status = measured[0]
        source_layer = (
            "RESOURCE_STATE" if str(reason) in _world_answer_reason_names() else "RUNTIME"
        )
    else:
        evidence_kind = "TRANSPORT_EXCEPTION"
        http_status = None
        source_layer = "TRANSPORT"
    return {
        "attribution_reason": str(reason),
        "evidence_kind": evidence_kind,
        "http_status": http_status,
        "source_layer": source_layer,
    }


def _candidate_fields(candidate: Any) -> dict[str, Any]:
    """One provider candidate, described without deciding anything about it."""

    centroid = getattr(candidate, "centroid_wgs84", None) or (None, None)
    return {
        "source_provider": getattr(candidate, "source_provider", None),
        "source_entity_id": getattr(candidate, "source_entity_id", None),
        "label": getattr(candidate, "display_name", None),
        "country": getattr(candidate, "country_code", None),
        "admin_area": getattr(candidate, "admin_area", None),
        "admin_level": getattr(candidate, "admin_level", None),
        "coordinate_system": getattr(candidate, "crs", None),
        "centroid": {"latitude": centroid[0], "longitude": centroid[1]},
        "extent": list(getattr(candidate, "extent_wgs84", None) or ()),
        "observed_at": getattr(candidate, "observed_at", None),
        "fetched_at": getattr(candidate, "fetched_at", None),
    }


def _is_bounded_area(candidate: Any) -> bool:
    """An administrative *area*: a relation carrying a normalised four-number extent."""

    return str(getattr(candidate, "source_entity_id", "")).startswith("R:") and (
        len(getattr(candidate, "extent_wgs84", None) or ()) == 4
    )


#: The element classes OpenStreetMap actually names things by.  Anything else is not a reference
#: this product may carry into an identity column.
OSM_ELEMENT_CLASSES = ("N", "W", "R")


def _names_an_entity(candidate: Any) -> bool:
    """Whether this candidate is something the provider can be quoted as having named.

    The two conditions are the ones the acceptance wording names, and no others: a usable source id
    and a legal WGS-84 position.  A provider reference has the grammar `<N|W|R>:<digits>` - an
    element class and a positive integer - and a coordinate outside `[-90, 90] x [-180, 180]` is
    refused rather than rounded, because a rounded number is a new fact.

    Deliberately *not* required here: the provider layer's richer `provenance_complete` (which also
    wants both timestamps).  It is reported downstream as its own field so a reader can see it;
    refusing an identity for a missing `Date` header would be a third condition nobody ruled on, and
    the mocked answers in this repository's doors carry no header.

    Measured before this existed, with `osm_type` removed from a real answer: the candidate came
    back `MATCH` and was adopted as `OSM:unknown:3289208`; with `osm_type=Z` it became
    `OSM:Z:3289208`.  Both are identities the provider never claimed, and both were joinable,
    cacheable and quotable from then on.
    """

    reference = str(getattr(candidate, "source_entity_id", "") or "")
    element_class, _, identifier = reference.partition(":")
    if element_class not in OSM_ELEMENT_CLASSES:
        return False
    if not identifier.isdigit() or int(identifier) <= 0:
        return False
    if str(getattr(candidate, "crs", "") or "") != "WGS84":
        return False
    centroid = getattr(candidate, "centroid_wgs84", None) or ()
    if len(centroid) != 2:
        return False
    try:
        latitude, longitude = float(centroid[0]), float(centroid[1])
    except (TypeError, ValueError):
        return False
    return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0


def _collapse_point_markers(candidates: list[Any]) -> tuple[list[Any], list[Any]]:
    """`destination-discrimination-v1`, applied to evidence the provider layer refused to rank.

    B's own reading of this data is `AMBIGUOUS` - two entities claim the name, and their layer
    will not choose.  It is right that they refuse.  It is also not the end of the question: when
    one claim is an area with a boundary and the other is a point that marks that same area, a
    traveller is not being asked to choose between two places, and answering AMBIGUOUS would push
    our own index shape onto them.  So the choice is made *here*, narrowly: exactly one bounded
    area among the claims, and every entity set aside is named in the record.  Two bounded areas of
    the same name stay ambiguous, because that is a real tie.
    """

    if len(candidates) < 2:
        return list(candidates), []
    areas = [candidate for candidate in candidates if _is_bounded_area(candidate)]
    if len(areas) == 1:
        return areas, [c for c in candidates if c is not areas[0]]
    return list(candidates), []


def _exact_name_matches(candidates: list[Any], raw: str) -> list[Any]:
    """Only a candidate whose *name* is the question may become a destination.

    The geocoder is a search engine and returns the nearest things it knows.  Adopting one of those
    would be the hash mistake with better credentials, so the caller's words are compared against
    the label the provider reported before anything downstream is allowed to believe it.
    """

    wanted = _normalise(raw)
    return [
        candidate
        for candidate in candidates
        if _normalise(getattr(candidate, "display_name", "") or "") == wanted
    ]


def _adopted(
    raw: str,
    candidate: Any,
    *,
    set_aside: list[Any],
    evidence: list[dict[str, Any]],
    status: str,
    observation: dict[str, Any] | None = None,
) -> DestinationResolution:
    """Turn one provider candidate into server-owned canonical identity, with its audit trail."""

    entity_id = str(candidate.source_entity_id)
    identity = f"{PROVIDER_IDENTITY_PREFIX}{entity_id}"
    fields = _candidate_fields(candidate)
    fields["provider_attempts"] = evidence
    fields["discriminated_by"] = (
        DESTINATION_DISCRIMINATION_POLICY_VERSION if set_aside else None
    )
    fields["collapsed_candidates"] = [_candidate_fields(other) for other in set_aside]
    return DestinationResolution(
        query=raw,
        canonical_destination_id=identity,
        city_code=identity,
        method="provider",
        state="RESOLVED",
        matched_label=fields["label"] or identity,
        resolver_attempted=True,
        resolver_status=status,
        # The whole observation, not a two-key summary: which mirror answered, with what status and
        # how long it took is exactly what a reader looks for when the answer is a good one, and the
        # unresolved paths already carry it.  A success that reports less than a failure does is
        # evidence that only exists in the direction that flatters it.
        observation=dict(
            observation or {"status": status, "stage": "resolving_destination", "attempts": evidence}
        ),
        provenance=fields,
        **_resolved_identity(raw),
    )


def _unreachable_reason_name() -> str:
    """The provider layer's own word for "we could not ask", reused rather than re-invented."""

    import app.integrations.discovery.provider_identity as provider_layer

    return provider_layer.REASON_UNREACHABLE


def _bad_response_reason_name() -> str:
    """The provider layer's own word for "an answer arrived and does not decode", not a new one.

    Naming a broken answer is the half of item C that this layer can decide on its own: `answered`
    is true, so it is not an outage, and a candidate was offered, so it is not "there is no such
    place".  Which word to use is the provider layer's to have chosen first, and it already chose
    `REASON_BAD_RESPONSE` for a reply it cannot parse into what it claims.
    """

    import app.integrations.discovery.provider_identity as provider_layer

    return provider_layer.REASON_BAD_RESPONSE


def _provider_resolution(raw: str) -> DestinationResolution | None:
    """Ask the authorised provider, and translate its four answers into destination truth.

    Returns `None` when no attempt is authorised: the caller then gives the same structural answer
    it always gave, and claims no observation.  The mapping keeps the classes apart, which is the
    whole reason this layer is typed:

    * MATCH (or an AMBIGUOUS that collapses to one area under v1) -> canonical identity, still
      HYDRATABLE until this catalog holds something for it;
    * AMBIGUOUS -> null identity, candidates in their own field, nobody silently installed;
    * NO_MATCH -> null identity and a structural verdict, with `provider_status=OK` because looking
      and being told "no" is a successful observation, not a failure;
    * RUNTIME_FAILURE -> null identity, retryable, named, and never written as NO_MATCH.
    """

    if not anywhere_resolver_attempted():
        return None
    unresolved = _unresolved_identity(raw)
    try:
        with _provider_identity_resolver() as provider:
            resolution = provider(raw)
    except Exception as exc:  # noqa: BLE001 - the seam itself broke; named below, never laundered
        return DestinationResolution(
            query=raw,
            canonical_destination_id=None,
            city_code=None,
            method="provider_error",
            state="RUNTIME_FAILURE",
            reason_code=REASON_RESOLVER_UNAVAILABLE,
            retryable=True,
            resolver_attempted=True,
            resolver_status="UNREACHABLE",
            observation={
                "status": "UNREACHABLE",
                "stage": "resolving_destination",
                # The cause is this process' own, so it gets the provider layer's own named code
                # rather than a class name: `type(exc)` in a reason field is what §14 forbids.
                "reason": _unreachable_reason_name(),
                "detail": f"{type(exc).__name__}: {exc}"[:200],
            },
            **unresolved,
        )

    status = _provider_status_of(resolution)
    evidence = _attempt_evidence(resolution)
    observation = {
        "status": status,
        "stage": "resolving_destination",
        "reason": getattr(resolution, "reason", None),
        "answered": bool(getattr(resolution, "answered", False)),
        "provider_replied": bool(getattr(resolution, "provider_replied", False)),
        "attempts": evidence,
    }
    outcome_name = getattr(getattr(resolution, "outcome", None), "value", None) or str(
        getattr(resolution, "outcome", "")
    )
    candidates = _exact_name_matches(list(getattr(resolution, "candidates", ()) or []), raw)
    usable = [candidate for candidate in candidates if _names_an_entity(candidate)]
    refused = [candidate for candidate in candidates if not _names_an_entity(candidate)]
    adopted, set_aside = _collapse_point_markers(usable)

    if outcome_name == "RUNTIME_FAILURE":
        # The hard negative the contract names: an outage is never "there is no such place".
        return DestinationResolution(
            query=raw,
            canonical_destination_id=None,
            city_code=None,
            method="provider_error",
            state="RUNTIME_FAILURE",
            reason_code=REASON_RESOLVER_UNAVAILABLE,
            retryable=True,
            resolver_attempted=True,
            resolver_status=status,
            observation=observation,
            **unresolved,
        )
    if refused and outcome_name in {"MATCH", "AMBIGUOUS"}:
        # The provider says it found the place and then hands over a reference it cannot be held
        # to.  That is a broken answer, not an answer about the world: NO_MATCH would tell the
        # traveller "no such place", and inventing a digest would manufacture the place.  So the
        # answer is the provider layer's own word for an undecodable reply, retryable, with the
        # refused evidence kept where an auditor can read it.
        return DestinationResolution(
            query=raw,
            canonical_destination_id=None,
            city_code=None,
            method="provider_evidence_malformed",
            state="RUNTIME_FAILURE",
            reason_code=REASON_RESOLVER_UNAVAILABLE,
            retryable=True,
            resolver_attempted=True,
            resolver_status=status,
            observation={**observation, "reason": _bad_response_reason_name()},
            malformed_candidates=[_candidate_fields(candidate) for candidate in refused],
            **unresolved,
        )
    if len(adopted) == 1 and outcome_name in {"MATCH", "AMBIGUOUS"}:
        return _adopted(
            raw, adopted[0], set_aside=set_aside, evidence=evidence, status=status,
            observation=observation,
        )
    if len(adopted) > 1:
        return DestinationResolution(
            query=raw,
            canonical_destination_id=None,
            city_code=None,
            method="provider_ambiguous",
            state="AMBIGUOUS",
            reason_code=REASON_DESTINATION_AMBIGUOUS,
            resolver_attempted=True,
            resolver_status=status,
            observation=observation,
            candidates=[_candidate_fields(candidate) for candidate in adopted],
            **unresolved,
        )
    return DestinationResolution(
        query=raw,
        canonical_destination_id=None,
        city_code=None,
        method="provider_no_match",
        state="NO_MATCH",
        reason_code=REASON_NO_CANONICAL_IDENTITY,
        resolver_attempted=True,
        resolver_status=status,
        observation=observation,
        **unresolved,
    )


def resolve_destination(session: Session, query: str) -> DestinationResolution:
    """Turn a query into a canonical destination, from the sources this build may consult.

    Four answers, and they are not interchangeable:

    * **RESOLVED** - exactly one code names this query, and that code is the identity.  A
      destination the *catalog* holds rows for wins over one the vocabulary merely names, because
      the catalog is what the plan will be built from.
    * **AMBIGUOUS** - two codes name it.  No winner is guessed; candidates stay in their own
      field and the identity is null.
    * **NO_MATCH** - nothing here can name it.  Identity null, the words kept verbatim, and a
      request fingerprint so a repeat of the same question finds the same job.
    * **RUNTIME_FAILURE** - reading a source itself failed.  Retryable, named, and *not* a
      structural verdict.

    What is absent from all four: text-derived identity.  No digest, no raw text, no prefix.
    """

    raw = str(query if query is not None else "")
    unresolved = _unresolved_identity(raw)
    forms = _match_forms(raw)
    if not forms:
        return DestinationResolution(
            query=raw,
            canonical_destination_id=None,
            city_code=None,
            method="none",
            state="NO_MATCH",
            reason_code=REASON_NO_CANONICAL_IDENTITY,
            **unresolved,
        )

    try:
        held = _catalog_holdings(session)
        labels = _display_labels()
    except Exception as exc:  # noqa: BLE001 - named below, never laundered into NO_MATCH
        return DestinationResolution(
            query=raw,
            canonical_destination_id=None,
            city_code=None,
            method="resolver_error",
            state="RUNTIME_FAILURE",
            reason_code=REASON_RESOLVER_UNAVAILABLE,
            retryable=True,
            candidates=[],
            **unresolved,
        )

    def matches(source: dict[str, str]) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for city_code, label in sorted(source.items()):
            aliases = {city_code, city_code.replace("CN-", ""), label}
            if city_code == "CNX-DEMO":
                for extra, extra_aliases in _TRAVELER_FACING_LABELS.items():
                    if extra == city_code:
                        aliases.update(extra_aliases)
            if forms & _alias_forms(aliases):
                found.append((city_code, label))
        return found

    # 1. codes the catalog holds rows for.
    hits = matches({code: labels.get(code, "") for code in held})
    source = "catalog"
    if not hits:
        # 2. a code the product can *name* but has no rows for: the identity is real, the catalog
        #    is empty.  This is the case a traveller can act on, so it is not "unavailable".
        hits = matches(labels)
        source = "vocabulary"

    distinct = sorted({code for code, _label in hits})
    if len(distinct) > 1:
        return DestinationResolution(
            query=raw,
            canonical_destination_id=None,
            city_code=None,
            method="ambiguous",
            state="AMBIGUOUS",
            reason_code=REASON_DESTINATION_AMBIGUOUS,
            candidates=[
                {
                    "city_code": code,
                    "label": labels.get(code) or code,
                    "source": source,
                    "places_held": int(held.get(code) or 0),
                }
                for code, _label in hits
            ],
            **unresolved,
        )
    if distinct:
        city_code = distinct[0]
        return DestinationResolution(
            query=raw,
            canonical_destination_id=city_code,
            city_code=city_code,
            method=source,
            state="RESOLVED",
            matched_label=labels.get(city_code) or city_code,
            **_resolved_identity(raw),
        )

    # 3. nothing in this process can name it.  If this deployment authorised a provider, ask it -
    #    and keep its four answers separate from ours (a near match, a tie and a timeout are three
    #    different truths, and none of them is allowed to become an identity by default).
    provider = _provider_resolution(raw)
    if provider is not None:
        return provider

    # 4. nothing we are authorised to consult can name it - and the answer is a null identity plus
    #    the words, not a placeholder that a fingerprint or a join would go on believing.
    return DestinationResolution(
        query=raw,
        canonical_destination_id=None,
        city_code=None,
        method="unresolved",
        state="NO_MATCH",
        reason_code=REASON_NO_CANONICAL_IDENTITY,
        **unresolved,
    )


def _classify_source(provider_key: object, evidence: object) -> str:
    """One rule for one class of source, shared by places and services.

    The list is closed on purpose: an unrecognised provider is reported as the *weakest* thing it
    could honestly be (`OFFLINE_SNAPSHOT`), because inventing `REMOTE_PROVIDER` for a row nobody
    can account for is how a fixture ends up on screen as live data.
    """

    key = str(provider_key or "")
    mark = str(evidence or "")
    if key == "mock-city":
        return "GOLDEN_FIXTURE"
    if key in {"citynexus-demo", "mock-city-demo"}:
        return "SIMULATED"
    if mark in {"SEALED_REAL_REPLAY", "REPLAY"}:
        return "REPLAY"
    if mark == "OFFLINE_SNAPSHOT":
        return "OFFLINE_SNAPSHOT"
    if key in {"overpass", "photon", "amap", "valhalla"} or mark == "LIVE_PROVIDER":
        return "REMOTE_PROVIDER"
    return "OFFLINE_SNAPSHOT"


def _source_kind(place: Place) -> str:
    """Classify a row by where it came from - and never strengthen that class."""

    metadata = _loads(getattr(place, "metadata_json", None))
    evidence = metadata.get("provenance") if isinstance(metadata, dict) else ""
    return _classify_source(getattr(place, "provider_key", ""), evidence)


def _service_source_kind(service: CityService) -> str:
    """The same classification for a bookable service row: `FIXTURE` is not `OSM_FETCHED`."""

    metadata = _loads(getattr(service, "metadata_json", None))
    evidence = metadata.get("provenance") if isinstance(metadata, dict) else ""
    return _classify_source(getattr(service, "provider_key", ""), evidence)


def _loads(blob: Any) -> dict[str, Any]:
    if isinstance(blob, dict):
        return blob
    try:
        value = json.loads(blob or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _age_days(value: Any) -> float | None:
    moment = None
    try:
        text = str(value)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() / 86400.0


def _mobility_evidence(session: Session, city_code: str) -> tuple[int, int, list[str]]:
    """How many of this destination's place pairs have an explicit travel fact, and of what class.

    The fixture's blanket `default_minutes` is a constant, not a measurement, so it does not
    count: a destination where every leg answers "20" because the file says so has no mobility
    evidence, however many pairs there are.

    The classes come back beside the count on purpose.  "Covered" and "usable as a hard constraint"
    are different claims - a sealed fixture minute is real material for showing a traveller what
    exists and not real enough for the planner to promise a budget holds - and until now the answer
    carried the first without saying the second, which is how "mobility READY" could be read as a
    guarantee by one consumer and as a hint by another.
    """

    from app.db.models import MobilityFact
    from app.integrations.mock.providers import MOCK_ROOT
    from app.replanning.provider_snapshots import EXPLICIT_FROZEN_FIXTURE, LIVE_PROVIDER_CAPTURE

    fixture = _loads((MOCK_ROOT / "mobility_matrix.json").read_text(encoding="utf-8"))
    explicit = fixture.get("minutes") if isinstance(fixture.get("minutes"), dict) else {}
    ids = [
        str(item)
        for item in session.scalars(select(Place.id).where(Place.city_code == city_code)).all()
    ]
    pairs = [(origin, destination) for origin in ids for destination in ids if origin != destination]
    covered = sum(1 for origin, destination in pairs if f"{origin}|{destination}" in explicit)
    classes: list[str] = []
    if covered:
        classes.append(EXPLICIT_FROZEN_FIXTURE)
    # A captured live route outranks the fixture only if it actually covers a pair of this
    # destination; `mobility_facts` is journey-scoped, so its keys are matched against these places
    # rather than counted in the abstract.  One column, not two: `scalars()` over a two-entity
    # select yields the first column as a scalar, so indexing the "row" returned a single character
    # and the comparison could never be true - a silent never-eligible reading, which is precisely
    # the kind of bug a gate that only ever expects the gap cannot see.
    captured = [
        str(value)
        for value in session.scalars(
            select(MobilityFact.provenance).where(
                MobilityFact.from_key.in_(ids), MobilityFact.to_key.in_(ids)
            )
        ).all()
    ]
    if LIVE_PROVIDER_CAPTURE in captured:
        classes.append(LIVE_PROVIDER_CAPTURE)
    return covered, len(pairs), sorted(set(classes))


def coverage(session: Session, city_code: str | None) -> dict[str, str]:
    """The flat six-key coverage a screen and a gate both read (`{key: READY|PARTIAL|MISSING}`)."""

    return {key: value["state"] for key, value in coverage_detail(session, city_code).items()}


def coverage_detail(session: Session, city_code: str | None) -> dict[str, dict[str, Any]]:
    """Per-intent counts, quality and provenance, beside the flat states.

    Deliberately a *separate* field: the flat `coverage` map is what the contract pins its enum
    to, and burying the counts inside it would turn every consumer's `coverage["food"]` into a
    dict and break the promise that this surface only ever gains fields.
    """

    detail: dict[str, dict[str, Any]] = {}
    if city_code is None:
        for key in COVERAGE_KEYS:
            detail[key] = {
                "state": MISSING,
                "candidate_count": 0,
                "min_ready": MIN_READY_CANDIDATES[key],
                "source_kinds": [],
                "stale": False,
            }
        return detail

    places = list(session.scalars(select(Place).where(Place.city_code == city_code)).all())
    services = list(
        session.scalars(select(CityService).where(CityService.city_code == city_code)).all()
    )
    by_category: dict[str, list[Place]] = {}
    for place in places:
        by_category.setdefault(str(place.category or ""), []).append(place)
    places_by_id = {str(place.id): place for place in places}
    # A bookable service is about a place, so its source can be named: the service row first,
    # and the linked place if the service itself carries no provider of its own.
    places_by_service = {
        str(service.id): places_by_id.get(str(service.place_id))
        for service in services
        if service.place_id
    }

    ages = [
        age
        for age in (_age_days(place.updated_at) for place in places)
        if age is not None
    ]
    stale = bool(places) and all(age > STALE_AFTER_DAYS for age in ages)

    for key in COVERAGE_KEYS:
        contributors: list[Place] = []
        # Only the mobility axis grows these two fields. The other five are made of catalogue rows
        # whose quality question is "is it stale, is it a fixture", already answered by
        # `stale`/`source_kinds`; a hard-eligibility rule for them would be a second policy nobody
        # wrote, so they keep the same shape they always had.
        extras: dict[str, Any] = {}
        if key == "mobility":
            covered, total, classes = _mobility_evidence(session, city_code)
            ratio = (covered / total) if total else 0.0
            count = covered
            state = (
                READY
                if total and ratio >= MOBILITY_EXPLICIT_RATIO_READY and covered >= MIN_READY_CANDIDATES[key]
                else (PARTIAL if covered else MISSING)
            )
            # The legs are computed between catalogue places, so the places they connect are the
            # source of this axis - and a fixture leg is not an OSM leg.
            contributors = places
            from app.replanning.provider_snapshots import LIVE_PROVIDER_CAPTURE

            extras = {
                "source_classes": classes,
                # "covered" answers whether material exists; this answers whether any of it may be
                # treated as a constraint. A destination can legitimately be PARTIAL here and still
                # have nothing hard-eligible, and the gap name is what lets a caller say so.
                "hard_eligible": LIVE_PROVIDER_CAPTURE in classes,
            }
        elif key == "booking":
            bookable = [
                item for item in services if str(item.service_type) in BOOKABLE_SERVICE_TYPES
            ]
            count = len(bookable)
            state = _state_for(count, MIN_READY_CANDIDATES[key])
            contributors = [
                places_by_service.get(str(item.id))
                for item in bookable
                if places_by_service.get(str(item.id)) is not None
            ]
            if contributors:
                kinds = sorted({_service_source_kind(item) for item in bookable})
            else:
                contributors = []
                kinds = []
        else:
            rows = [row for cat in INTENT_CATEGORIES[key] for row in by_category.get(cat, [])]
            count = len(rows)
            state = _state_for(count, MIN_READY_CANDIDATES[key])
            contributors = rows
        if key != "booking":
            kinds = sorted({_source_kind(place) for place in contributors})
        detail[key] = {
            "state": state,
            "candidate_count": count,
            "min_ready": MIN_READY_CANDIDATES[key],
            "source_kinds": kinds,
            "stale": bool(stale),
            **extras,
        }
    return detail


def _state_for(count: int, minimum: int) -> str:
    if count <= 0:
        return MISSING
    if count >= minimum:
        return READY
    return PARTIAL


def unclassified_material(session: Session, city_code: str | None) -> dict[str, int]:
    """Categories the readiness policy does not name, and how many rows each holds.

    Without this, a destination whose rows carry an unexpected category string reads as "zero
    candidates" in every intent - the same shape as an empty catalog, which is how a capability
    answer can lie while every number in it is technically true.  The map is reported, not merged
    into `coverage`: the six keys are what the contract promises, and a seventh would break that.
    """

    if not city_code:
        return {}
    named = {category for bucket in INTENT_CATEGORIES.values() for category in bucket}
    counts: dict[str, int] = {}
    for category in session.scalars(
        select(Place.category).where(Place.city_code == city_code)
    ).all():
        key = str(category or "")
        if key and key not in named:
            counts[key] = counts.get(key, 0) + 1
    return counts


def fingerprint_material(
    canonical_destination_id: str | None,
    *,
    resolver_query_normalized: str | None = None,
    scope: str = PENDING_REQUEST_SCOPE,
) -> dict[str, Any]:
    """The exact bytes a fingerprint is made of, spelled out for both halves of the contract.

    * Destination named -> the four segments arbitration v0.2 froze: canonical destination plus
      the three policy versions.  Unchanged from before ORD-0167, on purpose: an existing job's
      key must not move under it because the resolver learned a new column.
    * Destination unnamed -> a **request** fingerprint, namespaced `pending-request-v1` and built
      from the *normalised* query plus the request scope and the same three versions.  It contains
      no canonical identity (there is none), no verbatim label (the punctuation is the traveller's,
      not the machine's) and no `destination_pending_key` (correlation, not convergence).
    """

    versions = {
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "readiness_policy_version": READINESS_POLICY_VERSION,
        "provider_policy_version": PROVIDER_POLICY_VERSION,
    }
    if canonical_destination_id:
        return {"canonical_destination_id": canonical_destination_id, **versions}
    return {
        "namespace": PENDING_REQUEST_NAMESPACE,
        "scope": scope,
        "canonical_destination_id": None,
        "resolver_query_normalized": _normalise(resolver_query_normalized or "") or None,
        **versions,
    }


def capability_fingerprint(
    canonical_destination_id: str | None,
    *,
    resolver_query_normalized: str | None = None,
    scope: str = PENDING_REQUEST_SCOPE,
) -> str:
    """The durable idempotency key: which destination, under which three versions.

    Anything that can change the answer is in here; anything that cannot (who asked, when, with
    which idempotency header) is not, so five concurrent requests for one county are one job -
    and five concurrent requests for one *unnamed* county are one job too, which is the whole
    reason the unnamed branch above is deterministic without ever being an identity.
    """

    material = fingerprint_material(
        canonical_destination_id, resolver_query_normalized=resolver_query_normalized, scope=scope
    )
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def pending_request_fingerprint(query: str) -> str:
    """The key an *unnamed* destination is filed under - computable before a provider is asked.

    Deliberately the same expression `identity_fields()` stores (see the
    `capability_fingerprint(None, resolver_query_normalized=…)` call above), so a lookup by this key
    can only ever reach a job that is itself waiting on an unnamed destination: a job that resolved
    is keyed by its canonical id and is unreachable from here.  Normalised and namespaced exactly
    like the stored value, and still **not** an identity - it says "the same open question", never
    "the same place".
    """

    return capability_fingerprint(None, resolver_query_normalized=_normalise(query))


def fingerprint_parts(
    canonical_destination_id: str | None,
    *,
    resolver_query_normalized: str | None = None,
    scope: str = PENDING_REQUEST_SCOPE,
) -> dict[str, Any]:
    """The segments spelled out - a caller must be able to ask *why* keys matched."""

    return fingerprint_material(
        canonical_destination_id, resolver_query_normalized=resolver_query_normalized, scope=scope
    )


def evaluate_capability(
    session: Session,
    query: str,
    *,
    resolution: DestinationResolution | None = None,
    provider_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The answer `GET /destinations/capability` gives, and the only place it is computed."""

    resolution = resolution or resolve_destination(session, query)
    detail = coverage_detail(session, resolution.city_code)
    flat = {key: value["state"] for key, value in detail.items()}
    missing = [key for key, value in flat.items() if value != READY]
    place_count = int(
        session.scalar(
            select(func.count(Place.id)).where(Place.city_code == resolution.city_code)
        )
        or 0
    ) if resolution.city_code else 0

    if resolution.state == "RUNTIME_FAILURE":
        # R1 applied to our own sources: a lookup that broke is a *runtime* fact, so the answer
        # stays retryable.  The bug this replaces swallowed the exception into `{}` and let the
        # branch below report "this destination cannot be planned" about a database hiccup.
        #
        # This cell used to read `HYDRATABLE` + `START_HYDRATION`, which was the least wrong of
        # three words: it told a traveller they may prepare a destination nobody has named, with
        # `answered=false` in the same body (measured at `16cc652`, and the reason the commander
        # took it to the arbiter rather than letting this file add a fourth string on its own).
        # Answer "one" approved *adding the cell*; it did not approve a name, and this file supplied
        # one anyway (`NOT_ANSWERED`). `5771378382` (choose B) froze the arbiter's own spelling, so
        # that invented token is gone from every surface this lane owns. Approving a cell is not
        # approving what to call it - the cost of confusing the two was three lanes waiting on it.
        verdict, reason, action = (
            UNKNOWN_RETRYABLE,
            REASON_RESOLVER_UNAVAILABLE,
            ACTION_RETRY_LATER,
        )
    elif resolution.state == "AMBIGUOUS":
        # Unusable *as typed*, which is not the same claim as "unusable" about either candidate:
        # the reason names ambiguity and `candidates` carries what to choose between.
        verdict, reason, action = (
            "UNAVAILABLE",
            REASON_DESTINATION_AMBIGUOUS,
            ACTION_CHOOSE_ANOTHER_DESTINATION,
        )
    elif not resolution.resolved:
        # Structural: nothing in this build can name the destination at all.  Note that this
        # branch is *not* reached when a resolver merely failed - R1 keeps that retryable.
        verdict, reason, action = "UNAVAILABLE", REASON_NO_CANONICAL_IDENTITY, ACTION_CHOOSE_ANOTHER_DESTINATION
    elif not missing and not any(detail[key]["stale"] for key in COVERAGE_KEYS):
        verdict, reason, action = READY, REASON_CATALOG_CURRENT, ACTION_ACCEPT_PARTIAL_PLAN
    else:
        # `HYDRATABLE` covers both halves of the arbitration's definition - something missing, or
        # something that has gone stale.  A complete-but-ancient catalog is not "ready": the
        # traveller's answer would be today's plan built from last season's facts.
        verdict = "HYDRATABLE"
        # `STALE` is reserved for "nothing is missing, but what we hold has aged": the two
        # conditions ask for different things from a traveller (prepare more vs refresh), and
        # naming a coverage gap "stale" would send them to the wrong action.
        reason = (
            REASON_CATALOG_STALE
            if not missing and any(detail[key]["stale"] for key in COVERAGE_KEYS)
            else REASON_COVERAGE_INCOMPLETE
        )
        action = ACTION_START_HYDRATION

    places = (
        list(session.scalars(select(Place).where(Place.city_code == resolution.city_code)).all())
        if resolution.city_code
        else []
    )
    source_kinds = sorted({_source_kind(place) for place in places})
    # The named gap, said by the service rather than inferred by a caller that noticed the counts.
    # Three conditions, all measured, because the sentence has to mean one thing:
    # the destination is named, this build really holds material for it, and the mobility material
    # it holds is real but not of a class anything may treat as a constraint.  An *empty* mobility
    # axis is not this gap - that is MISSING, and reporting a gap there would claim material exists.
    mobility = detail["mobility"]
    hard_gaps: list[str] = []
    # `is False`, not `not ...`: a mobility report that never answered the eligibility question
    # (a caller-built detail, an older shape) must not be read as "held, but not hard enough".  The
    # gap claims material exists and falls short of a constraint; claiming it from silence would be
    # the same class of overstatement in the other direction.
    hard_eligible = mobility.get("hard_eligible")
    if (
        resolution.resolved
        and place_count > 0
        and int(mobility.get("candidate_count") or 0) > 0
        and hard_eligible is False
    ):
        hard_gaps.append(GAP_MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE)
    body: dict[str, Any] = {
        "query": str(query),
        **identity_fields(resolution),
        "destination_state": resolution.state,
        "destination_resolution": resolution.method,
        # g10 (lane F): the normalized catalog carries coordinates for every geometry kind.
        # `out center` gives nodes a point too, so node rows are not silently coordinate-less.
        **(
            {
                "places": [
                    {
                        "id": str(place.id),
                        "source_provider": place.provider_key,
                        "source_identity": place.external_id,
                        "osm_type": str(place.external_id).split(":", 1)[0].lower()
                        if place.external_id
                        else None,
                        "name": place.name,
                        "category": place.category,
                        "latitude": place.latitude,
                        "longitude": place.longitude,
                    }
                    for place in session.scalars(
                        select(Place).where(Place.city_code == resolution.city_code)
                    ).all()
                ]
            }
            if resolution.city_code
            else {"places": []}
        ),
        # The pair the new cell is obliged to keep: `UNKNOWN_RETRYABLE` without `retryable` would be a
        # fourth "looks like a state" string, which is precisely what the ruling forbade when it
        # authorised adding a cell.  Every other verdict reports exactly what was measured.
        "retryable": bool(resolution.retryable) or verdict == UNKNOWN_RETRYABLE,
        "candidates": resolution.candidates,
        # Always present, empty when nothing was refused: a reader must be able to ask "was there
        # evidence we could not use?" without first guessing which keys this answer chose to send.
        "malformed_candidates": resolution.malformed_candidates,
        "matched_label": resolution.matched_label,
        "capability": verdict,
        "reason_code": reason,
        "recommended_action": action,
        "coverage": flat,
        "coverage_detail": detail,
        "missing_categories": missing,
        # Named gaps, always present (empty when nothing is missing beyond the counts). A caller
        # that wants to say "why this is not plannable as a hard commitment" reads this instead of
        # writing its own rule about source classes and drifting from everyone else's.
        "hard_gaps": hard_gaps,
        # Named rather than swallowed: if a category exists but no intent names it, a reader must
        # be able to see that we have material we failed to classify, not infer an empty catalog.
        "unclassified_categories": unclassified_material(session, resolution.city_code),
        "place_count": place_count,
        "provenance": {
            "source_kinds": source_kinds,
            "by_category": {
                key: value["source_kinds"] for key, value in detail.items()
            },
            # Stated because it is the rule this file obeys: normalisation reports the class a
            # fact arrived with and never raises it.
            "upgraded_by_normalisation": False,
        },
        "fingerprint": capability_fingerprint(
            resolution.canonical_destination_id,
            resolver_query_normalized=resolution.resolver_query_normalized,
        ),
        **fingerprint_parts(
            resolution.canonical_destination_id,
            resolver_query_normalized=resolution.resolver_query_normalized,
        ),
    }
    # R2: an observation exists only if something actually looked.  Absence is the honest way to
    # say "not measured"; a default `OK` would be a fabricated measurement.
    if provider_observations:
        body["provider_status"] = [
            item for item in provider_observations if item.get("status") in PROVIDER_STATUSES
        ]
    # `reason_code` stays the frozen *class* (DESTINATION_RESOLVER_UNAVAILABLE); these four are the
    # distinct cause underneath it.  Collapsing seven provider shapes into the one class name is
    # exactly what F's six-shapes gate charges the layer below with, so the cause travels through
    # here instead of being flattened at the boundary - and it travels as one classification, not as
    # a lone reason the screen, the announcement, the telemetry and the recovery action each have to
    # interpret for themselves (§8).
    body.update(_attribution_fields(resolution))
    if resolution.provenance:
        # Only a provider answer carries this: for a destination the catalog named, the source is
        # the catalog itself and `provenance.source_kinds` already says so.
        body["destination_provenance"] = resolution.provenance
    observation = resolution.observation
    # Appended rather than used only-when-empty.  This leg's own answer used to be the thin
    # `{status, stage}` summary on success while the failure paths carried `answered`, `reason` and
    # the per-attempt list, so the mirror that answered, with what status and how slowly was visible
    # only when something went wrong - and a reader who looked in `provider_status` for it found
    # nothing.  Coverage observations stay where they are; this one is additional evidence, not a
    # fallback.
    if isinstance(observation, dict) and observation.get("status") in PROVIDER_STATUSES:
        existing = body.get("provider_status") or []
        if not any(
            isinstance(item, dict) and item.get("stage") == observation.get("stage")
            for item in existing
        ):
            body["provider_status"] = [*existing, observation]
    if resolution.resolver_status and not body.get("provider_status"):
        body["provider_status"] = [{"status": resolution.resolver_status, "stage": "resolving"}]
    return body
