from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from app.planning.anchors import (
    DEPARTURE_ROLE,
    LOCATION_RESOLVED,
    LOCATION_UNRESOLVED,
    STRUCTURAL_ANCHOR_KEYS,
    anchor_identity_from_policy,
    is_structural_key,
    normalise_role,
    structural_place_id,
)


def active_c4_hard_constraints(commitments: Iterable[Any]) -> list[dict[str, Any]]:
    """Translate durable C4 Commitment facts into feasibility hard constraints.

    ``Commitment.activity_id`` belongs to a particular PlanVersion and therefore
    is not a stable cross-version identity.  The WP04 policy stores the stable
    semantic key and scheduled start; those facts are the authoritative anchor
    used for replanning.

    The emitted ``semantic_ref`` is always a key a generator can actually
    satisfy: the structural arrival/departure node, or the real MID_TRIP
    place/service.  A genuinely unresolved location keeps its temporal
    commitment and is reported explicitly instead of pretending to be met.
    """

    constraints: list[dict[str, Any]] = []
    for item in commitments:
        if str(getattr(item, "status", "")) != "ACTIVE":
            continue
        if str(getattr(item, "level", "")) != "C4_Anchored":
            continue
        raw = getattr(item, "policy_json", "{}") or "{}"
        try:
            policy = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("C4 commitment policy is invalid JSON") from exc
        scheduled_start = policy.get("scheduled_start_at")
        if not scheduled_start:
            if not normalise_role(policy.get("anchor_role")):
                # Booking/order anchors are bound to a concrete Activity, not a
                # relative schedule; they are protected through activity_id.
                continue
            raise ValueError("C4 commitment lacks a stable time anchor")

        role = normalise_role(policy.get("anchor_role"))
        raw_key = str(policy.get("semantic_key") or "").strip()
        legacy_key = str(policy.get("semantic_node_key") or "").strip()
        semantic_key = raw_key or legacy_key

        # ARRIVAL / DEPARTURE *reservation* anchors always resolve to the
        # structural planner Place identity, even when an earlier release
        # persisted an unsatisfiable synthetic UUID or a bare placeholder.
        # A booking/order C4 anchor has no anchor_role and keeps its own
        # activity-anchored semantic_key untouched.
        if role in STRUCTURAL_ANCHOR_KEYS:
            journey_id = str(getattr(item, "journey_id", "") or "")
            semantic_key = (
                structural_place_id(journey_id, role) if journey_id else STRUCTURAL_ANCHOR_KEYS[role]
            )

        if not semantic_key:
            if not role:
                # A booking/order anchor with no readable identity is not a
                # reservation anchor; leave it to the existing activity_id
                # protection path rather than inventing an unresolved anchor.
                continue
            constraints.append(
                {
                    "kind": "HARD",
                    "category": "commitment",
                    "rule_code": "UNRESOLVED_COMMITMENT_ANCHOR",
                    "params": {
                        "anchor_role": role,
                        "scheduled_start_at": str(scheduled_start),
                        "time_boundary": (
                            "END" if role == DEPARTURE_ROLE else "START"
                        ),
                        "location_evidence": LOCATION_UNRESOLVED,
                        "semantic_ref": raw_key or None,
                    },
                    "active": True,
                }
            )
            continue

        constraints.append(
            {
                "kind": "HARD",
                "category": "commitment",
                "rule_code": "LOCKED_COMMITMENT",
                "params": {
                    "semantic_ref": semantic_key,
                    "start_at": str(scheduled_start),
                    "anchor_role": role,
                    # A structural anchor has a defined location node but no
                    # traveller-chosen place; the generator owns producing it.
                    "generator_owns_anchor": role in STRUCTURAL_ANCHOR_KEYS,
                    "location_evidence": (
                        LOCATION_RESOLVED
                        if not is_structural_key(semantic_key)
                        else "STRUCTURAL_ANCHOR"
                    ),
                    "time_boundary": (
                        "END"
                        if role == DEPARTURE_ROLE
                        else "START"
                    ),
                },
                "active": True,
            }
        )
    return constraints


__all__ = [
    "active_c4_hard_constraints",
    "anchor_identity_from_policy",
]
