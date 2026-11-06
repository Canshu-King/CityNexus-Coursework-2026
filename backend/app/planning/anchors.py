"""Canonical C4 anchor identity.

A confirmed reservation becomes a ``C4_Anchored`` Commitment.  That commitment
must carry **one** stable semantic location identity that every stage of the
pipeline agrees on:

    generation -> C4 commitment -> frozen mobility evidence -> PlanVersion
    persistence -> selection -> booking -> start -> replanning

Historically ARRIVAL and DEPARTURE anchors were stored as synthetic UUIDs with
no corresponding routable node, so no generator could ever emit a matching
activity and every plan was rejected with ``LOCKED_COMMITMENT_LOST``.

The model is:

    ``Activity.metadata.semantic_key``  - stable routing/feasibility identity
    ``Activity.place_id``              - concrete database Place identity

For ARRIVAL / DEPARTURE the semantic key is the *structural anchor* key that
the authoritative planner materialises (``arrival-anchor`` /
``departure-anchor``).  For MID_TRIP it is the user-selected place or service
key.  When a location genuinely cannot be resolved the temporal commitment is
preserved and the unresolved location is recorded explicitly - the anchor is
never silently reported as satisfied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

ARRIVAL_ROLE = "ARRIVAL"
DEPARTURE_ROLE = "DEPARTURE"
MID_TRIP_ROLE = "MID_TRIP"
ANCHOR_ROLES = (ARRIVAL_ROLE, DEPARTURE_ROLE, MID_TRIP_ROLE)

# The structural planner keys the authoritative V12/V13 planner materialises.
STRUCTURAL_ANCHOR_KEYS = {
    ARRIVAL_ROLE: "arrival-anchor",
    DEPARTURE_ROLE: "departure-anchor",
}
# Legacy aliases still present in older persisted policy_json rows.
LEGACY_STRUCTURAL_KEYS = frozenset(
    {"arrival-anchor", "departure-anchor", "anchor:arrival", "anchor:departure"}
)
# Location evidence states.
LOCATION_RESOLVED = "RESOLVED"
LOCATION_UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class AnchorIdentity:
    """One stable, resolver-independent view of a C4 anchor."""

    role: str
    semantic_key: str
    place_id: str | None
    scheduled_start_at: str
    time_boundary: str
    location_evidence: str
    routable: bool
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "anchor_role": self.role,
            "semantic_key": self.semantic_key,
            "place_id": self.place_id,
            "scheduled_start_at": self.scheduled_start_at,
            "time_boundary": self.time_boundary,
            "location_evidence": self.location_evidence,
            "routable": self.routable,
            "source": self.source,
        }


def normalise_role(value: Any) -> str:
    """Normalise a reservation anchor role.

    An explicit, recognised role is returned as-is.  Anything else returns
    ``""`` - notably a booking/order commitment, which has no reservation role
    and must never be treated as an ARRIVAL anchor.
    """

    role = str(value or "").upper().strip()
    return role if role in ANCHOR_ROLES else ""


def is_reservation_anchor(policy: Mapping[str, Any]) -> bool:
    """True when this C4 policy is a reservation anchor (not a booking anchor)."""

    return bool(normalise_role(policy.get("anchor_role")))


def time_boundary_for(role: str) -> str:
    """A departure anchor constrains the END of its activity, an arrival the START."""

    return "END" if normalise_role(role) == DEPARTURE_ROLE else "START"


def is_structural_key(value: Any) -> bool:
    """True when a key is a structural anchor identity rather than a chosen POI."""

    key = str(value or "").strip()
    if not key:
        return False
    if key in LEGACY_STRUCTURAL_KEYS or key in set(STRUCTURAL_ANCHOR_KEYS.values()):
        return True
    return any(key.endswith(f":{placeholder}") for placeholder in STRUCTURAL_ANCHOR_KEYS.values())


def structural_place_id(journey_id: str, role: str) -> str:
    """The concrete Place identity the authoritative planner materialises.

    ``authoritative_v12._ensure_place`` builds ``f"v12:{journey_id}:{key}"``, so
    this must agree exactly for generation, C4 commitments, frozen evidence and
    replanning to share one identity.
    """

    role = normalise_role(role)
    return f"v12:{journey_id}:{STRUCTURAL_ANCHOR_KEYS[role]}"


def structural_external_id(journey_id: str, role: str) -> str:
    role = normalise_role(role)
    return f"{journey_id}:{STRUCTURAL_ANCHOR_KEYS[role]}"


def resolve_anchor_key(
    *,
    role: str,
    intent_key: str | None,
    journey_id: str,
) -> str | None:
    """Resolve the canonical semantic key for one C4 anchor role.

    ARRIVAL / DEPARTURE always resolve to the planner's structural Place id so
    the generator has a stable, satisfiable target.  MID_TRIP resolves to the
    caller's real place/service key, or ``None`` when the location is genuinely
    unresolved.
    """

    role = normalise_role(role)
    if role in (ARRIVAL_ROLE, DEPARTURE_ROLE):
        return structural_place_id(journey_id, role)
    candidate = str(intent_key or "").strip()
    if not candidate or is_structural_key(candidate):
        return None
    return candidate


def anchor_claim_identity(
    policy: Mapping[str, Any], *, journey_id: str
) -> str | None:
    """The node identity a persisted C4 policy claims, or ``None`` when it claims none.

    ``None`` is the case that keeps commerce commitments out of the single-ACTIVE rule:
    a booking/order policy carries no ``anchor_role``, and ``resolve_plan_anchors`` skips
    those rows before it ever forms an anchor.  The two branches below are the same split
    that function makes when it builds ``ResolvedAnchor.semantic_key`` - structural key for
    ARRIVAL/DEPARTURE, the stored place/service key for MID_TRIP - so a duplicate is
    recognised by the exact value that collides inside the planner rather than by a second
    vocabulary invented here.
    """

    role = normalise_role(policy.get("anchor_role"))
    if not role:
        return None
    if role in (ARRIVAL_ROLE, DEPARTURE_ROLE):
        return structural_place_id(journey_id, role)
    return (
        str(policy.get("semantic_key") or "").strip()
        or str(policy.get("semantic_node_key") or "").strip()
        or None
    )


def anchor_identity_from_policy(
    policy: Mapping[str, Any],
    *,
    resolved_place_id: str | None = None,
    journey_id: str | None = None,
) -> AnchorIdentity:
    """Build the canonical identity view from a persisted Commitment policy."""

    role = normalise_role(policy.get("anchor_role"))
    semantic_key = str(policy.get("semantic_key") or "").strip()
    if not semantic_key:
        semantic_key = str(policy.get("semantic_node_key") or "").strip()
    if role in (ARRIVAL_ROLE, DEPARTURE_ROLE) and journey_id:
        # Normalise a legacy synthetic UUID / placeholder to the structural
        # Place id so every consumer agrees even on commitments written by
        # earlier V19.2.x releases.
        if not semantic_key or is_structural_key(semantic_key) or _looks_synthetic(semantic_key):
            semantic_key = (
                f"v12:{journey_id}:{STRUCTURAL_ANCHOR_KEYS[role]}"
                if journey_id
                else STRUCTURAL_ANCHOR_KEYS[role]
            )
    place_id = resolved_place_id or (semantic_key or None)
    unresolved = not semantic_key
    return AnchorIdentity(
        role=role,
        semantic_key=semantic_key,
        place_id=place_id,
        scheduled_start_at=str(policy.get("scheduled_start_at") or ""),
        time_boundary=time_boundary_for(role),
        location_evidence=(
            LOCATION_UNRESOLVED
            if unresolved
            else "STRUCTURAL_ANCHOR"
            if is_structural_key(semantic_key)
            else LOCATION_RESOLVED
        ),
        routable=bool(place_id),
        source=str(policy.get("source") or "UNKNOWN"),
    )


def _looks_synthetic(value: str) -> bool:
    """True for the V19.2 synthetic uuid5 anchor keys (not place identities)."""

    parts = value.split("-")
    return len(parts) == 5 and all(len(part) in {4, 8, 12} for part in parts)
