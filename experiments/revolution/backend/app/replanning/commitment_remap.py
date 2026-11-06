"""Bind a protected commitment to exactly one activity on the active plan.

A ``Commitment.activity_id`` is PlanVersion-local: the moment a replan becomes
authoritative, the activity it named may belong to a superseded plan.  Both
writers that can leave a commitment pointing at the old plan — accepting a
replan, and capturing the payment behind a booking — therefore have to re-point
it, and the re-pointing is where identity truth is decided.

The invariant, and the only one this module enforces:

    zero matches    -> fail closed.  Silence here is what strands a live C4
                       commitment on an obsolete plan.
    exactly one     -> rebind to it.
    more than one   -> AMBIGUOUS, fail closed.  Picking "the first one in
                       sequence order" invents an identity the record does not
                       support.

Identity is structural and compared for exact equality only: semantic keys from
the commitment policy and the activity metadata, then the activity's own
``place_id`` / ``city_service_id``, plus the scheduled instant the commitment was
anchored to.  Substring and suffix matching are deliberately absent.  A
commitment's canonical semantic key has historically been spelled several ways
(``v12:<journey>:departure-anchor`` versus a bare ``departure-anchor`` versus a
place id), and the tempting fix — ``candidate.endswith(term)`` — quietly turns
identity into string similarity, which is how a dinner anchor can match a
departure anchor that happens to share a suffix.  Titles are never consulted.

A generic (place/service) match is accepted only when neither side carries a
stronger semantic identity, so a place overlap can never outvote a conflicting
semantic key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from app.db.models import Activity


class RemapConflict(RuntimeError):
    """Raised when a commitment cannot be bound to exactly one active activity."""

    def __init__(self, message: str, *, matched: int) -> None:
        super().__init__(message)
        self.matched = matched


@dataclass
class CommitmentIdentity:
    """The identity a commitment asserts, read from its policy and its activity."""

    strong: set[str] = field(default_factory=set)
    generic: set[str] = field(default_factory=set)
    scheduled_at: str | None = None
    boundary: str = "START"

    @property
    def has_strong(self) -> bool:
        return bool(self.strong)


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _clean(values: Iterable[Any]) -> set[str]:
    return {str(value) for value in values if value}


def commitment_identity(
    *, policy: dict[str, Any], activity: Activity | None
) -> CommitmentIdentity:
    """Read what the commitment is anchored to.

    The activity the commitment currently names is a first-class identity source:
    a booking made against a plan carries the place and semantic keys that activity
    had, even when the policy was written by an older anchor format that only knew
    a canonical key string.
    """

    metadata = _loads(getattr(activity, "metadata_json", None))
    strong = _clean(
        [
            policy.get("semantic_key"),
            policy.get("semantic_node_key"),
            metadata.get("semantic_key"),
            metadata.get("semantic_node_key"),
        ]
    )
    generic = _clean(
        [
            policy.get("semantic_key"),
            getattr(activity, "place_id", None),
            getattr(activity, "city_service_id", None),
        ]
    )
    scheduled = str(policy.get("scheduled_start_at") or "") or None
    boundary = (
        "END" if str(policy.get("anchor_role") or "").upper() == "DEPARTURE" else "START"
    )
    return CommitmentIdentity(
        strong=strong, generic=generic, scheduled_at=scheduled, boundary=boundary
    )


def _same_instant(left: str | None, right: str | None) -> bool:
    """Compare the instant, not the string.

    A proposed plan is normalised to the journey's UTC offset while a commitment
    may hold the same instant in UTC, so textual equality would reject a real
    match and a substring test would accept a false one.
    """

    if not left or not right:
        return False
    try:
        return datetime.fromisoformat(left) == datetime.fromisoformat(right)
    except ValueError:
        return False


def matching_activities(
    identity: CommitmentIdentity, candidates: Sequence[Activity]
) -> list[Activity]:
    """Return every candidate that is provably the same commitment target."""

    strong_matches: list[Activity] = []
    generic_matches: list[Activity] = []
    for candidate in candidates:
        if candidate.status in {"CANCELLED"}:
            continue
        metadata = _loads(candidate.metadata_json)
        candidate_strong = _clean(
            [metadata.get("semantic_key"), metadata.get("semantic_node_key")]
        )
        candidate_generic = _clean([candidate.place_id, candidate.city_service_id])
        moment = candidate.end_at if identity.boundary == "END" else candidate.start_at
        if not _same_instant(identity.scheduled_at, moment):
            continue
        if identity.strong & candidate_strong:
            strong_matches.append(candidate)
            continue
        # A place/service overlap is a compatibility path for commitments whose
        # semantic key is the place id itself (which is what a booking stores).  It
        # is refused only when the candidate carries its own stronger semantic
        # identity, so a place overlap can never outvote a real semantic conflict.
        if identity.generic & candidate_generic and not (identity.strong and candidate_strong):
            generic_matches.append(candidate)
    return strong_matches if strong_matches else generic_matches


def resolve_commitment_target(
    *,
    policy: dict[str, Any],
    anchored_activity: Activity | None,
    candidates: Sequence[Activity],
    subject: str = "commitment",
) -> Activity:
    """Fail closed unless exactly one candidate carries the commitment's identity."""

    identity = commitment_identity(policy=policy, activity=anchored_activity)
    if not identity.scheduled_at:
        raise RemapConflict(
            f"{subject} carries no scheduled anchor, so it cannot be bound to an activity",
            matched=0,
        )
    matches = matching_activities(identity, candidates)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RemapConflict(
            f"{subject} remap is AMBIGUOUS on the active Journey plan "
            f"({len(matches)} candidates carry the same identity)",
            matched=len(matches),
        )
    raise RemapConflict(
        f"{subject} cannot be attached to the active Journey plan",
        matched=0,
    )
