from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

V1_TOOL_NAMES = (
    "inspect_journey",
    "inspect_twin",
    "list_replans",
    "search_city_services",
    "request_replan",
    "accept_replan",
)

V2_TOOL_NAMES = V1_TOOL_NAMES + (
    "replace_activity",
    "reschedule_activity",
    "create_booking",
    "cancel_refundable_booking",
    "request_commitment_change",
)

SAFE_ARGUMENT_NAMES = frozenset(
    {
        "request_id",
        "agent_id",
        "journey_id",
        "proposal_id",
        "approval_id",
        "trigger_event_id",
        "activity_id",
        "replacement_activity_id",
        "replacement_place_id",
        "booking_id",
        "commitment_id",
        "start_at",
        "end_at",
        "provider_key",
        "reason",
        "candidate_count",
        "city_code",
        "q",
        "category",
        "service_type",
    }
)


@dataclass(frozen=True)
class HermesReplanProposalView:
    """Stable semantic contract exposed to Hermes for each replan candidate."""

    proposal_id: str
    proposed_plan_version_id: str
    status: str
    hard_constraints_passed: bool
    locked_commitments_preserved: bool
    utility_score: float
    candidate_family: str
    kept: tuple[str, ...]
    moved: tuple[dict[str, Any], ...]
    removed: tuple[str, ...]
    added: tuple[str, ...]
    cost_delta_minor: int
    travel_delta_min: int
    end_time_delta_min: int
    reason_codes: tuple[str, ...]
    protected_preservation: dict[str, Any]
    feasibility_envelope: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "proposed_plan_version_id": self.proposed_plan_version_id,
            "status": self.status,
            "hard_constraints_passed": self.hard_constraints_passed,
            "locked_commitments_preserved": self.locked_commitments_preserved,
            "utility_score": self.utility_score,
            "candidate_family": self.candidate_family,
            "kept": list(self.kept),
            "moved": [dict(item) for item in self.moved],
            "removed": list(self.removed),
            "added": list(self.added),
            "cost_delta_minor": self.cost_delta_minor,
            "travel_delta_min": self.travel_delta_min,
            "end_time_delta_min": self.end_time_delta_min,
            "reason_codes": list(self.reason_codes),
            "protected_preservation": dict(self.protected_preservation),
            "feasibility_envelope": dict(self.feasibility_envelope),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "HermesReplanProposalView":
        top_fields = (
            "id",
            "proposed_plan_version_id",
            "status",
            "hard_constraints_passed",
            "locked_commitments_preserved",
            "utility_score",
        )
        missing_top = [key for key in top_fields if payload.get(key) is None]
        if missing_top:
            raise ValueError(f"replan proposal is missing required fields: {missing_top}")
        diff = payload["diff"]
        diff_fields = (
            "candidate_family",
            "kept",
            "moved",
            "removed",
            "added",
            "cost_delta_minor",
            "travel_delta_min",
            "end_time_delta_min",
            "reason_codes",
            "preserved_protected",
        )
        missing_diff = [key for key in diff_fields if diff.get(key) is None]
        if missing_diff:
            raise ValueError(f"replan proposal diff is missing required fields: {missing_diff}")
        evidence = diff["verified_evidence"]
        commitment_preservation = evidence["commitment_preservation"]
        envelope = diff["after_feasibility_envelope"]
        required_envelope = (
            "budget_remaining_minor",
            "journey_end_slack_min",
            "preserved_commitment_count",
            "risk_reason_codes",
        )
        missing_envelope = [key for key in required_envelope if envelope.get(key) is None]
        if missing_envelope:
            raise ValueError("replan proposal is missing required feasibility envelope fields")
        if any(
            commitment_preservation.get(key) is None
            for key in ("preserved", "protected_count")
        ):
            raise ValueError("replan proposal is missing protected-preservation evidence")
        return cls(
            proposal_id=str(payload["id"]),
            proposed_plan_version_id=str(payload["proposed_plan_version_id"]),
            status=str(payload["status"]),
            hard_constraints_passed=bool(payload["hard_constraints_passed"]),
            locked_commitments_preserved=bool(payload["locked_commitments_preserved"]),
            utility_score=float(payload["utility_score"]),
            candidate_family=str(diff["candidate_family"]),
            kept=tuple(str(item) for item in diff["kept"]),
            moved=tuple(dict(item) for item in diff["moved"]),
            removed=tuple(str(item) for item in diff["removed"]),
            added=tuple(str(item) for item in diff["added"]),
            cost_delta_minor=int(diff["cost_delta_minor"]),
            travel_delta_min=int(diff["travel_delta_min"]),
            end_time_delta_min=int(diff["end_time_delta_min"]),
            reason_codes=tuple(str(item) for item in diff["reason_codes"]),
            protected_preservation={
                "preserved_protected": list(diff["preserved_protected"]),
                "preserved": bool(commitment_preservation["preserved"]),
                "protected_count": int(commitment_preservation["protected_count"]),
            },
            feasibility_envelope={key: envelope[key] for key in required_envelope},
        )


@dataclass(frozen=True)
class MCPToolDescriptor:
    name: str
    description: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description}


@dataclass(frozen=True)
class MCPToolCall:
    request_id: str
    agent_id: str
    arguments: Mapping[str, Any]

    def payload(self) -> dict[str, Any]:
        payload = {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            **dict(self.arguments),
        }
        unknown = sorted(set(payload) - SAFE_ARGUMENT_NAMES)
        if unknown:
            raise ValueError(f"unknown CityNexus Agent arguments: {unknown}")
        return payload
