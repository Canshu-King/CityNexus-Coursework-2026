"""Unified traveller attention projection for the Quiet Journey Operator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class AttentionCandidate:
    kind: str
    priority: int
    blocking: bool
    semantic_id: str
    title: str
    body: str
    action_label: str | None = None
    action_type: str | None = None
    source_ref: dict[str, Any] | None = None


_PRIORITY = {
    "TRANSACTION_PARTIAL_ATTENTION": 0,
    "APPROVAL_REQUIRED": 10,
    "COLLECTIVE_RESPONSE_REQUIRED": 11,
    "INTENT_CLARIFICATION_REQUIRED": 12,
    "REALITY_CONFLICT_REQUIRED": 8,
    "INBOX_BINDING_REQUIRED": 13,
    "CONFIRM_ARRIVAL": 20,
    "CONFIRM_BEGIN": 21,
    "CONFIRM_COMPLETE": 22,
    "CONFIRM_DEPARTURE": 23,
    "ROUTE_CHECK_REQUIRED": 30,
    "MORNING_ATTENTION": 31,
    "OPERATOR_DEGRADED": 40,
}


def priority_for(kind: str) -> int:
    return _PRIORITY.get(str(kind).upper(), 99)


def choose_primary_attention(candidates: Iterable[AttentionCandidate]) -> AttentionCandidate | None:
    """Choose the one traveller interruption that deserves focus now."""
    rows = list(candidates)
    if not rows:
        return None
    rows.sort(key=lambda item: (item.priority, 0 if item.blocking else 1, item.semantic_id))
    return rows[0]


def attention_from_operator_projection(operator: dict[str, Any]) -> dict[str, Any] | None:
    """Derive attention strictly from existing product truth; no writes or provider calls."""

    candidates: list[AttentionCandidate] = []

    operations = operator.get("operations") or {}
    if str(operations.get("status") or "").upper() in {"PARTIALLY_APPLIED", "NEEDS_ATTENTION"}:
        plan_id = str(operations.get("plan_id") or "")
        candidates.append(
            AttentionCandidate(
                kind="TRANSACTION_PARTIAL_ATTENTION",
                priority=priority_for("TRANSACTION_PARTIAL_ATTENTION"),
                blocking=True,
                semantic_id=f"transaction:{plan_id}:{operations.get('status')}",
                title="A trip change only finished partway",
                body="Review what completed and what still needs attention before I continue.",
                action_label="Review transaction",
                action_type="REVIEW_TRANSACTION",
                source_ref={"execution_plan_id": plan_id},
            )
        )

    approval = operator.get("approval") or {}
    if approval.get("required"):
        approval_id = str(approval.get("approval_id") or "")
        candidates.append(
            AttentionCandidate(
                kind="APPROVAL_REQUIRED",
                priority=priority_for("APPROVAL_REQUIRED"),
                blocking=True,
                semantic_id=f"approval:{approval_id}",
                title="CityNexus needs your approval",
                body="I found a change I cannot apply without your authority.",
                action_label=str(approval.get("action_label") or "Review change"),
                action_type="REVIEW_APPROVAL",
                source_ref={"approval_id": approval_id},
            )
        )

    collective = operator.get("collective") or {}
    if str(collective.get("state") or "").upper() == "WAITING_FOR_MEMBERS" and not bool(
        collective.get("viewer_has_responded")
    ):
        mission_id = str(collective.get("mission_id") or "")
        epoch = str(collective.get("decision_epoch") or "")
        viewer = str(collective.get("viewer_member_id") or "")
        candidates.append(
            AttentionCandidate(
                kind="COLLECTIVE_RESPONSE_REQUIRED",
                priority=priority_for("COLLECTIVE_RESPONSE_REQUIRED"),
                blocking=True,
                semantic_id=f"collective:{mission_id}:{epoch}:{viewer}",
                title="The shared trip needs your choice",
                body="Your response is required before CityNexus can continue the shared change.",
                action_label="Choose",
                action_type="RESPOND_COLLECTIVE",
                source_ref={"mission_id": mission_id, "decision_epoch": epoch},
            )
        )

    for intent in operator.get("intents") or []:
        if str(intent.get("status") or "").upper() == "NEEDS_CLARIFICATION" or str(
            intent.get("evaluation_state") or ""
        ).upper() == "NEEDS_CLARIFICATION":
            intent_id = str(intent.get("id") or "")
            candidates.append(
                AttentionCandidate(
                    kind="INTENT_CLARIFICATION_REQUIRED",
                    priority=priority_for("INTENT_CLARIFICATION_REQUIRED"),
                    blocking=True,
                    semantic_id=f"intent:{intent_id}:clarification",
                    title="One quick question",
                    body=str(
                        (intent.get("clarification") or {}).get("question")
                        or "I need one detail before I can safely steer the rest of this Journey."
                    ),
                    action_label="Answer",
                    action_type="ANSWER_INTENT_CLARIFICATION",
                    source_ref={"intent_id": intent_id},
                )
            )

    inbox = operator.get("inbox") or {}
    latest_inbox = inbox.get("latest") or {}
    if latest_inbox.get("status") == "BINDING_REQUIRED":
        item_id = str(latest_inbox.get("id") or "")
        candidates.append(AttentionCandidate(
            kind="INBOX_BINDING_REQUIRED", priority=priority_for("INBOX_BINDING_REQUIRED"),
            blocking=True, semantic_id=f"inbox:{item_id}:binding",
            title="Which reservation changed?",
            body="I received a booking update, but I need one detail before I can apply it.",
            action_label="Choose reservation", action_type="BIND_INBOX_ITEM",
            source_ref={"inbox_item_id": item_id},
        ))

    reality_conflict = operator.get("reality_conflict") or {}
    if reality_conflict.get("status") == "UNRESOLVED_MATERIAL_CONFLICT":
        conflict_id = str(reality_conflict.get("id") or "")
        candidates.append(AttentionCandidate(
            kind="REALITY_CONFLICT_REQUIRED", priority=priority_for("REALITY_CONFLICT_REQUIRED"),
            blocking=True, semantic_id=f"reality-conflict:{conflict_id}:{reality_conflict.get('generation')}",
            title="I have two different departure times for the same train",
            body=str(reality_conflict.get("body") or "I’m holding off on replanning until I know which departure time is real."),
            action_label="Choose departure time", action_type="RESOLVE_REALITY_CONFLICT",
            source_ref={"conflict_id": conflict_id},
        ))

    flow = operator.get("flow") or {}
    flow_state = str(flow.get("state") or "").upper()
    flow_map = {
        "ARRIVAL_READY": (
            "CONFIRM_ARRIVAL",
            "You’re near your stop",
            "Confirm when you arrive.",
            "I’m here",
            "CONFIRM_ARRIVAL",
        ),
        "READY_TO_BEGIN": (
            "CONFIRM_BEGIN",
            "Ready to start?",
            "Tell CityNexus when you begin this activity.",
            "Start",
            "CONFIRM_BEGIN",
        ),
        "READY_TO_COMPLETE": (
            "CONFIRM_COMPLETE",
            "Finished here?",
            "Mark this stop done when you’re ready to move on.",
            "Done",
            "CONFIRM_COMPLETE",
        ),
        "LEAVE_NOW": (
            "CONFIRM_DEPARTURE",
            "It’s time to leave",
            "Confirm when you are actually on the way.",
            "I’m leaving",
            "CONFIRM_DEPARTURE",
        ),
    }
    if flow_state in flow_map:
        kind, title, body, action_label, action_type = flow_map[flow_state]
        current = flow.get("current") or {}
        activity_id = str(current.get("id") or "")
        candidates.append(
            AttentionCandidate(
                kind=kind,
                priority=priority_for(kind),
                blocking=False,
                semantic_id=f"flow:{activity_id}:{flow_state}",
                title=title,
                body=body,
                action_label=action_label,
                action_type=action_type,
                source_ref={"activity_id": activity_id},
            )
        )

    route = flow.get("route") or {}
    if str(route.get("state") or "").upper() in {"ROUTE_NEEDED", "UNAVAILABLE"}:
        next_activity = flow.get("next") or flow.get("current") or {}
        target_id = str(next_activity.get("id") or "")
        candidates.append(
            AttentionCandidate(
                kind="ROUTE_CHECK_REQUIRED",
                priority=priority_for("ROUTE_CHECK_REQUIRED"),
                blocking=False,
                semantic_id=f"route:{target_id}",
                title="I need a route before I can tell you when to leave",
                body="Check the next route to ground the timing.",
                action_label="Check route",
                action_type="CHECK_ROUTE",
                source_ref={"activity_id": target_id},
            )
        )

    overnight = operator.get("overnight") or {}
    if str(overnight.get("state") or "").upper() == "MORNING_NEEDS_ATTENTION":
        cycle = str(overnight.get("cycle_key") or overnight.get("local_date") or "")
        candidates.append(
            AttentionCandidate(
                kind="MORNING_ATTENTION",
                priority=priority_for("MORNING_ATTENTION"),
                blocking=False,
                semantic_id=f"morning:{cycle}",
                title="Your first stop needs one check",
                body="I prepared the morning, but one item still needs your attention.",
                action_label="Review morning",
                action_type="REVIEW_MORNING",
                source_ref={"cycle_key": overnight.get("cycle_key")},
            )
        )

    brain = operator.get("brain") or {}
    if str(brain.get("state") or "").upper() in {
        "ATTENTION_REQUIRED",
        "UNCONFIGURED",
        "TEMPORARILY_UNAVAILABLE",
    } and not candidates:
        candidates.append(
            AttentionCandidate(
                kind="OPERATOR_DEGRADED",
                priority=priority_for("OPERATOR_DEGRADED"),
                blocking=False,
                semantic_id=f"brain:{brain.get('state')}",
                title="Journey Operator needs attention",
                body=str(brain.get("headline") or "Planning is temporarily limited."),
                action_label=None,
                action_type=None,
                source_ref={"brain_state": brain.get("state")},
            )
        )

    primary = choose_primary_attention(candidates)
    if primary is None:
        return None
    return {
        "kind": primary.kind,
        "title": primary.title,
        "body": primary.body,
        "action_label": primary.action_label,
        "action_type": primary.action_type,
        "priority": primary.priority,
        "blocking": primary.blocking,
        "semantic_id": primary.semantic_id,
        "source_ref": primary.source_ref or {},
        "created_at": _created_at_for(primary, operator),
        "options": _options_for(primary, operator),
        "pending_count": len(candidates),
    }


def _created_at_for(primary: AttentionCandidate, operator: dict[str, Any]) -> str | None:
    intent_id = (primary.source_ref or {}).get("intent_id")
    if not intent_id:
        return None
    for intent in operator.get("intents") or []:
        if str(intent.get("id")) == str(intent_id):
            return intent.get("created_at")
    return None


def _options_for(primary: AttentionCandidate, operator: dict[str, Any]) -> list[dict[str, str]]:
    conflict_id = (primary.source_ref or {}).get("conflict_id")
    if conflict_id:
        conflict = operator.get("reality_conflict") or {}
        if str(conflict.get("id") or "") == str(conflict_id):
            return [{"value": str(item["claim_id"]), "label": str(item["label"])}
                    for item in conflict.get("choices") or [] if item.get("claim_id") and item.get("label")]
    inbox_item_id = (primary.source_ref or {}).get("inbox_item_id")
    if inbox_item_id:
        latest = (operator.get("inbox") or {}).get("latest") or {}
        if str(latest.get("id") or "") == str(inbox_item_id):
            return [{"value": str(item["id"]), "label": str(item["label"])}
                    for item in latest.get("candidates") or [] if item.get("id") and item.get("label")]
    intent_id = (primary.source_ref or {}).get("intent_id")
    if not intent_id:
        return []
    for intent in operator.get("intents") or []:
        if str(intent.get("id")) == str(intent_id):
            clarification = intent.get("clarification") or {}
            options = clarification.get("options") or []
            return [
                {"value": str(item["value"]), "label": str(item["label"])}
                for item in options
                if isinstance(item, dict) and item.get("value") and item.get("label")
            ]
    return []
