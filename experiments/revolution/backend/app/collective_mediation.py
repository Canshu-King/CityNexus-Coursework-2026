"""Small deterministic projections for shared Journey mediation.

This module deliberately contains arithmetic and classification only.  It does
not select an authority, call a provider, or turn a soft preference into a
hard constraint.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping
import hashlib
import json

from sqlalchemy import select

from app.db.models import JourneyMission, PlanVersion, ReplanProposal
from app.replanning.candidate_evidence import verified_interest_preference_breakdown


def _mission_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def journey_has_unresolved_collective_mediation(session, journey_id: str) -> bool:
    """Return whether explicit member disagreement still locks social authority.

    The lock is Journey-scoped and follows both the ORM parent pointer and the
    server-owned source_mission_id recorded in mediator intent, so callers
    cannot bypass it by starting another Agent mission or using a direct REST
    mutation path.
    """

    missions = list(
        session.scalars(
            select(JourneyMission).where(JourneyMission.journey_id == journey_id)
        ).all()
    )
    by_parent: dict[str, list[JourneyMission]] = {}
    for row in missions:
        if row.parent_mission_id:
            by_parent.setdefault(str(row.parent_mission_id), []).append(row)
        mediation = _mission_object(row.resolved_intent_json).get(
            "collective_mediation"
        )
        source_mission_id = (
            mediation.get("source_mission_id")
            if isinstance(mediation, dict)
            and mediation.get("kind") == "DISAGREEMENT_MEDIATION"
            else None
        )
        if source_mission_id:
            by_parent.setdefault(str(source_mission_id), []).append(row)

    def resolved_through_mediator(
        source: JourneyMission,
        seen: set[str],
    ) -> bool:
        if source.id in seen:
            return False
        seen = {*seen, source.id}
        clarification = _mission_object(source.clarification_json)
        if clarification.get("status") == "RESOLVED":
            return True
        if clarification.get("status") != "MEDIATION_REQUIRED":
            return False
        for child in by_parent.get(source.id, []):
            mediation = _mission_object(child.resolved_intent_json).get(
                "collective_mediation"
            )
            if not (
                isinstance(mediation, dict)
                and mediation.get("kind") == "DISAGREEMENT_MEDIATION"
                and str(mediation.get("source_mission_id") or "") == source.id
            ):
                continue
            if resolved_through_mediator(child, seen):
                return True
        return False

    for mission in missions:
        clarification = _mission_object(mission.clarification_json)
        if clarification.get("status") != "MEDIATION_REQUIRED":
            continue
        if not resolved_through_mediator(mission, set()):
            return True
    return False


def pending_resolved_mediation_selection(
    session,
    journey_id: str,
    *,
    active_plan_version_id: str | None,
) -> dict[str, str] | None:
    """Return exact renewed-consent authority that has not yet been consumed.

    A resolved mediation round authorizes one selected proposal/plan, not every
    algorithmically fair sibling. The authority is pending while the proposal
    remains PRESENTED and its plan remains a CANDIDATE distinct from active
    reality. Once that proposal is accepted or the plan is no longer a live
    candidate, the old decision no longer re-locks future Journey evolution.
    """

    missions = list(
        session.scalars(
            select(JourneyMission).where(JourneyMission.journey_id == journey_id)
        ).all()
    )
    candidates: list[tuple[str, JourneyMission, dict[str, Any]]] = []
    for mission in missions:
        mediation = _mission_object(mission.resolved_intent_json).get(
            "collective_mediation"
        )
        clarification = _mission_object(mission.clarification_json)
        if not (
            isinstance(mediation, dict)
            and mediation.get("kind") == "DISAGREEMENT_MEDIATION"
            and clarification.get("status") == "RESOLVED"
            and clarification.get("selected_proposal_id")
        ):
            continue
        candidates.append(
            (
                str(mission.completed_at or mission.started_at or mission.created_at or ""),
                mission,
                clarification,
            )
        )

    ordered = sorted(
        candidates,
        key=lambda item: (item[0], item[1].id),
        reverse=True,
    )
    if not ordered:
        return None

    # Social authority is monotonic: only the latest resolved mediation may
    # grant a pending capability. If that newest decision is already consumed,
    # stale, or invalidated, never fall back to an older superseded consent.
    _stamp, mission, clarification = ordered[0]
    proposal_id = str(clarification.get("selected_proposal_id") or "")
    proposal = session.get(ReplanProposal, proposal_id)
    if (
        proposal is None
        or proposal.journey_id != journey_id
        or proposal.status != "PRESENTED"
        or str(proposal.base_plan_version_id)
        != str(active_plan_version_id or "")
    ):
        return None
    plan = session.get(PlanVersion, proposal.proposed_plan_version_id)
    if (
        plan is None
        or plan.journey_id != journey_id
        or plan.status != "CANDIDATE"
        or str(plan.id) == str(active_plan_version_id or "")
    ):
        return None
    return {
        "mission_id": mission.id,
        "clarification_id": str(clarification.get("clarification_id") or ""),
        "decision_epoch": str(clarification.get("decision_epoch") or ""),
        "material_world_epoch": str(clarification.get("material_world_epoch") or ""),
        "candidate_surface_fingerprint": str(
            clarification.get("candidate_surface_fingerprint") or ""
        ),
        "authority_receipt_id": str(
            clarification.get("authority_receipt_id") or ""
        ),
        "selected_proposal_id": proposal.id,
        "selected_plan_version_id": plan.id,
    }


def collective_decision_epoch(
    *,
    journey_id: str,
    mission_id: str,
    trigger_event_id: str,
    base_plan_version_id: str | None,
    options: Iterable[Mapping[str, Any]],
    required_member_ids: Iterable[str],
    decision_relevant_member_ids: Iterable[str] | None = None,
    members: Iterable[Mapping[str, Any]],
    authority_scope_facts: Iterable[Mapping[str, Any]] | None = None,
) -> str:
    """Stable server fingerprint for a collective decision's semantic reality.

    R30 tightens the older identity-only epoch.  Proposal/plan ids are still
    included, but they are not enough: the same database rows must not preserve
    human authority after the actual optional experience or member-fit meaning
    changes.  Keep this projection deterministic and deliberately limited to
    facts already used by the collective decision surface.
    """

    required_ids = sorted({str(item) for item in required_member_ids})
    decision_relevant_ids = sorted(
        {
            str(item)
            for item in (
                decision_relevant_member_ids
                if decision_relevant_member_ids is not None
                else required_ids
            )
        }
    )
    decision_relevant_set = set(decision_relevant_ids)

    normalized_options: list[dict[str, Any]] = []
    for item in options:
        fits = item.get("member_fits") or {}
        normalized_options.append(
            {
                "proposal_id": str(item.get("proposal_id")),
                "plan_id": str(item.get("plan_id")),
                "status": item.get("status"),
                # Bind the choice not only to the option's label/fit, but to
                # the server-computed physical facts that made this future
                # executable when the room opened (and while it remains open).
                "material_world_epoch": item.get("material_world_epoch"),
                "fit_subject_place_ids": sorted(
                    {str(value) for value in (item.get("fit_subject_place_ids") or []) if value}
                ),
                "fit_subject_tags": sorted(
                    {
                        str(value).strip().lower()
                        for value in (item.get("fit_subject_tags") or [])
                        if str(value).strip()
                    }
                ),
                "fit_subject_basis": item.get("fit_subject_basis"),
                "member_fits": {
                    str(member_id): {
                        "effective_fit": (fit or {}).get("effective_fit"),
                        "preference_tier": (fit or {}).get("preference_tier"),
                        "conflict": (fit or {}).get("conflict"),
                    }
                    for member_id, fit in sorted(fits.items(), key=lambda pair: str(pair[0]))
                    if str(member_id) in decision_relevant_set
                },
                "fairness": {
                    "minimum_member_fit": (item.get("fairness") or {}).get("minimum_member_fit"),
                    "maximum_regret": (item.get("fairness") or {}).get("maximum_regret"),
                    "unacceptable_member_ids": sorted(
                        str(value)
                        for value in (item.get("fairness") or {}).get(
                            "unacceptable_member_ids", []
                        )
                    ),
                },
            }
        )

    material = {
        "journey_id": str(journey_id),
        "mission_id": str(mission_id),
        "trigger_event_id": str(trigger_event_id),
        "base_plan_version_id": str(base_plan_version_id) if base_plan_version_id else None,
        "options": normalized_options,
        "required_member_ids": required_ids,
        # Decision relevance and mutation-signing authority are separate
        # facts: read-only members still affect the meaning of consent, but
        # never become required signers merely by being decision-relevant.
        "decision_relevant_member_ids": decision_relevant_ids,
        "members": [
            {
                "member_id": str(item.get("member_id")),
                "role": item.get("role"),
                "capabilities": sorted(item.get("capabilities") or []),
                "preferences": item.get("journey_scoped_explicit_preferences") or [],
                "member_state": item.get("member_state"),
            }
            for item in members
            if str(item.get("member_id")) in decision_relevant_set
        ],
        # R74 adds narrowly scoped authority for the exact R73 shared-stop
        # decision. Keep those signed grant facts in the same existing epoch
        # that governs collective consent; they never become role capabilities.
        "authority_scope_facts": sorted(
            [dict(item) for item in (authority_scope_facts or [])],
            key=lambda item: (str(item.get("member_id") or ""), str(item.get("grant_id") or "")),
        ),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

def collective_material_world_epoch(basis: Mapping[str, Any]) -> str:
    """Hash only server-projected decision-material physical reality.

    Database rows, provider payloads and observation timestamps are intentionally
    projected by the caller before reaching this pure helper.  The hash is
    therefore stable across irrelevant storage churn while changing whenever a
    selected future's physical meaning changes.
    """

    return hashlib.sha256(
        json.dumps(
            basis,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def member_fit(*, preferences: Iterable[Any], candidate_tags: Iterable[str]) -> dict[str, Any]:
    breakdown = verified_interest_preference_breakdown(preferences, candidate_tags=candidate_tags)
    explicit = breakdown.get("current_mission_preference_fit")
    if explicit is None:
        explicit = breakdown.get("explicit_preference_fit")
    learned = breakdown.get("learned_soft_preference_fit")
    effective = breakdown.get("effective_preference_fit")
    return {
        "explicit_contribution": explicit,
        "learned_soft_contribution": learned,
        "effective_fit": 0.5 if effective is None else float(effective),
        "preference_tier": breakdown.get("effective_preference_tier", "NEUTRAL"),
        "conflict": None,
    }


def plan_collective_fit_subject(
    base_activities: Iterable[Mapping[str, Any]] | None,
    candidate_activities: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Return only the optional experience changed by a candidate plan.

    Shared hotels, stations, locked commitments, and unchanged background
    activities are not preference evidence for a new collective choice.  The
    projection is deliberately structural: it compares sequence/place
    identity and never infers a member preference from a display label.
    """
    base_by_sequence = {
        str(item.get("sequence_no") or item.get("sequence") or index): item
        for index, item in enumerate(base_activities or [], start=1)
        if isinstance(item, Mapping)
    }
    changed: list[Mapping[str, Any]] = []
    for index, item in enumerate(candidate_activities or [], start=1):
        if not isinstance(item, Mapping):
            continue
        if bool(item.get("locked")) or bool(item.get("protected")):
            continue
        kind = str(item.get("activity_type") or item.get("type") or "").upper()
        if kind in {"HOTEL", "LODGING", "TRANSPORT", "STATION", "ANCHOR", "C4"}:
            continue
        sequence = str(item.get("sequence_no") or item.get("sequence") or index)
        previous = base_by_sequence.get(sequence)
        current_place = str(item.get("place_id") or "")
        previous_place = str((previous or {}).get("place_id") or "")
        if previous is None or current_place != previous_place:
            if current_place:
                changed.append(item)
    place_ids = list(dict.fromkeys(str(item.get("place_id")) for item in changed if item.get("place_id")))
    tags: list[str] = []
    for item in changed:
        category = str(item.get("category") or "").strip().lower()
        if category:
            tags.append(category)
        for tag in item.get("tags") or []:
            value = str(tag).strip().lower()
            if value:
                tags.append(value)
    return {
        "fit_subject_place_ids": place_ids,
        "fit_subject_tags": sorted(set(tags)),
        "fit_subject_basis": "PLAN_DIFF" if place_ids else "NO_OPTIONAL_PLAN_DELTA",
    }


def collective_mutation_decision(
    mediation: Mapping[str, Any] | None,
    candidate_id: str,
    *,
    resolved_decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the server-side classification to a proposed mutation."""
    classification = str((mediation or {}).get("classification") or "INSUFFICIENT_WORLD_FACTS")
    if classification == "INSUFFICIENT_WORLD_FACTS":
        return {"allowed": False, "code": "COLLECTIVE_WORLD_FACTS_INCOMPLETE", "classification": classification}
    if classification == "AUTHORITY_CONFLICT":
        return {"allowed": False, "code": "COLLECTIVE_MUTATION_AUTHORITY_CONFLICT", "classification": classification}
    if classification == "UNANIMOUS_DOMINANCE":
        allowed = str((mediation or {}).get("candidate_id") or "") == str(candidate_id)
        return {"allowed": allowed, "code": None if allowed else "COLLECTIVE_CANDIDATE_NOT_DOMINANT", "classification": classification}
    if classification == "COMPROMISE_AVAILABLE":
        allowed_ids = {
            str(item) for item in (mediation or {}).get("candidate_ids") or []
        }
        decision = resolved_decision or {}
        human_selected = (
            str(decision.get("selected_candidate_id") or "")
            if str(decision.get("status") or "") == "RESOLVED"
            else ""
        )
        if human_selected:
            # A fresh server-validated collective answer owns the value choice
            # among currently acceptable compromises. The algorithm may define
            # the acceptable set, but it may not override renewed human
            # authority with its own tie-break/ranking.
            allowed = (
                str(candidate_id) in allowed_ids
                and str(candidate_id) == human_selected
            )
        else:
            selected = str(
                (mediation or {}).get("selected_candidate_id") or ""
            )
            allowed = str(candidate_id) in allowed_ids and (
                not selected or str(candidate_id) == selected
            )
        return {
            "allowed": allowed,
            "code": (
                None
                if allowed
                else "COLLECTIVE_CANDIDATE_NOT_ACCEPTABLE_COMPROMISE"
            ),
            "classification": classification,
        }
    if classification == "HUMAN_TRADEOFF_REQUIRED":
        decision = resolved_decision or {}
        allowed = str(decision.get("status") or "") == "RESOLVED" and str(decision.get("selected_candidate_id") or "") == str(candidate_id)
        return {"allowed": allowed, "code": None if allowed else "COLLECTIVE_TRADEOFF_UNRESOLVED", "classification": classification}
    return {"allowed": False, "code": "COLLECTIVE_MEDIATION_UNKNOWN", "classification": classification}


def fairness_receipt(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_member: dict[str, float] = {}
    for candidate in candidates:
        for member_id, fit in (candidate.get("member_fits") or {}).items():
            best_by_member[member_id] = max(best_by_member.get(member_id, 0.0), float(fit.get("effective_fit", 0.5)))
    receipts = []
    for candidate in candidates:
        fits = candidate.get("member_fits") or {}
        values = {key: float(value.get("effective_fit", 0.5)) for key, value in fits.items()}
        regrets = {key: round(best_by_member[key] - value, 3) for key, value in values.items()}
        candidate["fairness"] = {
            "minimum_member_fit": round(min(values.values()), 3) if values else None,
            "maximum_regret": round(max(regrets.values()), 3) if regrets else None,
            "regret_by_member": regrets,
            "mean_fit": round(sum(values.values()) / len(values), 3) if values else None,
            "unacceptable_member_ids": [key for key, value in values.items() if value < 0.35],
        }
        receipts.append(candidate["fairness"])
    return receipts


def classify_group_decision(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    rejected = [item.get("candidate_id") for item in candidates if item.get("status") != "GROUNDED_FEASIBLE"]
    candidates = [item for item in candidates if item.get("status") == "GROUNDED_FEASIBLE"]
    if not candidates or not candidates[0].get("member_fits"):
        return {"classification": "INSUFFICIENT_WORLD_FACTS", "reason": "NO_GROUNDED_FEASIBLE_MEMBER_OPTIONS", "rejected_candidate_ids": rejected}
    fairness_receipt(candidates)
    if len(candidates) == 1 and not candidates[0]["fairness"]["unacceptable_member_ids"]:
        return {"classification": "UNANIMOUS_DOMINANCE", "candidate_id": candidates[0].get("candidate_id")}
    for candidate in candidates:
        current = candidate["member_fits"]
        dominates = True
        strictly_better = False
        for other in candidates:
            if other is candidate:
                continue
            for member_id, fit in current.items():
                value = float(fit.get("effective_fit", 0.5))
                other_value = float((other.get("member_fits") or {}).get(member_id, {}).get("effective_fit", 0.5))
                if value < other_value - 0.05:
                    dominates = False
                if value > other_value + 0.05:
                    strictly_better = True
        if dominates and strictly_better and not candidate["fairness"]["unacceptable_member_ids"]:
            return {"classification": "UNANIMOUS_DOMINANCE", "candidate_id": candidate.get("candidate_id")}
    compromise = [
        item for item in candidates
        if (item.get("fairness") or {}).get("minimum_member_fit", 0.0) >= 0.45
        and (item.get("fairness") or {}).get("maximum_regret", 1.0) <= 0.35
    ]
    if compromise:
        return {
            "classification": "COMPROMISE_AVAILABLE",
            "candidate_ids": [item.get("candidate_id") for item in compromise],
            "selected_candidate_id": max(
                compromise,
                key=lambda item: (
                    (item.get("fairness") or {}).get("minimum_member_fit", 0.0),
                    -(item.get("fairness") or {}).get("maximum_regret", 1.0),
                    (item.get("fairness") or {}).get("mean_fit", 0.0),
                ),
            ).get("candidate_id"),
        }
    relevant = []
    for member_id in candidates[0]["member_fits"]:
        fits = [float(item["member_fits"][member_id]["effective_fit"]) for item in candidates]
        if max(fits) - min(fits) > 0.05 or min(fits) < 0.35:
            relevant.append(member_id)
    return {"classification": "HUMAN_TRADEOFF_REQUIRED", "candidate_ids": [item.get("candidate_id") for item in candidates], "decision_relevant_member_ids": relevant}


def mediation_progress_gate(
    previous_options: Iterable[Mapping[str, Any]] | None,
    current_options: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Require a mediator round to change the executable decision surface.

    Proposal ids are deliberately excluded from the shape comparison: a new
    row containing the same itinerary is not a new future.  A new option must
    introduce a different grounded place/tag shape and improve a measurable
    fairness boundary (or remove an unacceptable member).  The result is a
    compact receipt suitable for durable mission JSON and does not select an
    authority or call a provider.
    """

    def shape(item: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        places = tuple(sorted({str(value) for value in (item.get("fit_subject_place_ids") or []) if value}))
        tags = tuple(sorted({str(value).strip().lower() for value in (item.get("fit_subject_tags") or []) if value}))
        return places, tags

    def fairness(item: Mapping[str, Any]) -> tuple[float, float, set[str]]:
        receipt = item.get("fairness") or {}
        try:
            minimum = float(receipt.get("minimum_member_fit"))
        except (TypeError, ValueError):
            minimum = 0.0
        try:
            regret = float(receipt.get("maximum_regret"))
        except (TypeError, ValueError):
            regret = 1.0
        unacceptable = {str(value) for value in (receipt.get("unacceptable_member_ids") or [])}
        return minimum, regret, unacceptable

    previous = [item for item in (previous_options or []) if isinstance(item, Mapping)]
    current = [item for item in (current_options or []) if isinstance(item, Mapping)]
    previous_shapes = {shape(item) for item in previous}
    # A new database row or proposal id is not a new social choice. Until we
    # carry an explicit world-fact epoch into this receipt, same-shape options
    # fail closed even if they were regenerated later.
    new_options = [item for item in current if shape(item) not in previous_shapes]
    old_min = max((fairness(item)[0] for item in previous), default=0.0)
    old_regret = min((fairness(item)[1] for item in previous), default=1.0)
    old_unacceptable_sets = [fairness(item)[2] for item in previous]
    old_best_unacceptable_count = min(
        (len(values) for values in old_unacceptable_sets),
        default=0,
    )
    old_unacceptable_union = (
        set().union(*old_unacceptable_sets) if old_unacceptable_sets else set()
    )
    improved: list[dict[str, Any]] = []
    for item in new_options:
        new_min, new_regret, new_unacceptable = fairness(item)
        # Compare against the strongest previous boundary, not the union of
        # everyone harmed by different old options. Otherwise a third option
        # that still sacrifices one traveler could look like "progress" only
        # because the two old options sacrificed different travelers.
        non_regressing = (
            new_min >= old_min - 0.001
            and new_regret <= old_regret + 0.001
            and len(new_unacceptable) <= old_best_unacceptable_count
        )
        strictly_improves = (
            new_min > old_min + 0.001
            or new_regret < old_regret - 0.001
            or len(new_unacceptable) < old_best_unacceptable_count
        )
        if non_regressing and strictly_improves:
            improved.append(item)
    return {
        "allowed": bool(improved),
        "code": None if improved else "MEDIATION_NO_PROGRESS",
        "previous_option_shapes": [
            {"places": list(places), "tags": list(tags)}
            for places, tags in sorted(previous_shapes)
        ],
        "new_option_shapes": [
            {"places": list(shape(item)[0]), "tags": list(shape(item)[1])}
            for item in new_options
        ],
        "improved_option_ids": [str(item.get("proposal_id")) for item in improved],
        "previous_minimum_member_fit": round(old_min, 3),
        "previous_maximum_regret": round(old_regret, 3),
        "previous_unacceptable_member_ids": sorted(old_unacceptable_union),
        "previous_best_unacceptable_count": old_best_unacceptable_count,
        "progress_policy": "NON_REGRESSING_PARETO_BOUNDARY_WITH_STRICT_IMPROVEMENT",
        "new_option_count": len(new_options),
    }
