"""DeepSeek-first semantic compiler for free-text Copilot requests.

DeepSeek supplies bounded meaning.  This module validates that meaning and
maps it to a safe CityNexus route; it never resolves model-supplied IDs or
turns model-only facts into domain state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from app.capabilities import canonicalize_capabilities, extract_capabilities_from_text


SEMANTIC_INTENTS = (
    "EXPLAIN_PLAN",
    "WHAT_IF",
    "VERIFY_FACT",
    "DISCOVER",
    "PREFERENCE_CHANGE",
    "REPLAN_REQUEST",
    "DISRUPTION_REPORT",
    "PERSONAL_ASSISTANCE",
    "UNKNOWN",
)
SEMANTIC_ASSERTION_STATUSES = (
    "ASSERTED",
    "HYPOTHETICAL",
    "UNVERIFIED",
    "NEGATED",
    "HISTORICAL",
    "UNKNOWN",
)
SEMANTIC_EFFECTIVE_TIMES = ("PAST", "NOW", "FUTURE", "UNKNOWN")
SEMANTIC_EVENT_TYPES = (
    "WEATHER",
    "TRAFFIC_DELAY",
    "CLOSURE",
    "SCHEDULE_DELAY",
    "PERSONAL",
    "NONE",
)
SEMANTIC_ACTIONS = (
    "EXPLAIN",
    "SIMULATE",
    "VERIFY_EVIDENCE",
    "SEARCH",
    "UPDATE_PREFERENCE",
    "REQUEST_REPLAN",
    "REQUEST_ASSISTANCE",
    "NONE",
)
CITY_EVENT_TYPES = frozenset({"WEATHER", "TRAFFIC_DELAY", "CLOSURE", "SCHEDULE_DELAY"})


SEMANTIC_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intent",
        "assertion_status",
        "effective_time",
        "event_type",
        "requested_action",
        "confidence",
        "reasoning_summary",
        "entities",
        "constraints",
    ],
    "properties": {
        "intent": {"type": "string", "enum": list(SEMANTIC_INTENTS)},
        "assertion_status": {
            "type": "string",
            "enum": list(SEMANTIC_ASSERTION_STATUSES),
        },
        "effective_time": {
            "type": "string",
            "enum": list(SEMANTIC_EFFECTIVE_TIMES),
        },
        "event_type": {"type": "string", "enum": list(SEMANTIC_EVENT_TYPES)},
        "requested_action": {
            "type": "string",
            "enum": list(SEMANTIC_ACTIONS),
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning_summary": {"type": "string", "maxLength": 500},
        "entities": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "subject_kind": {"type": ["string", "null"]},
                "subject_text": {"type": ["string", "null"]},
                "temporal_expression": {"type": ["string", "null"]},
            },
        },
        "constraints": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "capabilities": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "severity", "params"],
                "properties": {
                    "code": {"type": "string"},
                    "class": {"type": "string"},
                    "severity": {"type": "string", "enum": ["HARD", "STRONG", "SOFT"]},
                    "params": {"type": "object", "additionalProperties": True},
                    "scope": {"type": "string", "enum": ["TRAVELER", "JOURNEY"]},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class SemanticEnvelope:
    intent: str
    assertion_status: str
    effective_time: str
    event_type: str
    requested_action: str
    confidence: float
    reasoning_summary: str
    entities: Mapping[str, Any]
    constraints: tuple[Mapping[str, Any], ...]
    capabilities: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SemanticEnvelope | None":
        required = set(SEMANTIC_ENVELOPE_SCHEMA["required"])
        optional = {"capabilities"}
        if not required.issubset(set(value)) or (set(value) - required - optional):
            return None
        intent = str(value.get("intent") or "")
        assertion_status = str(value.get("assertion_status") or "")
        effective_time = str(value.get("effective_time") or "")
        event_type = str(value.get("event_type") or "")
        requested_action = str(value.get("requested_action") or "")
        if intent not in SEMANTIC_INTENTS:
            return None
        if assertion_status not in SEMANTIC_ASSERTION_STATUSES:
            return None
        if effective_time not in SEMANTIC_EFFECTIVE_TIMES:
            return None
        if event_type not in SEMANTIC_EVENT_TYPES:
            return None
        if requested_action not in SEMANTIC_ACTIONS:
            return None
        try:
            confidence = float(value["confidence"])
        except (TypeError, ValueError):
            return None
        if not 0 <= confidence <= 1:
            return None
        reasoning_summary = value.get("reasoning_summary")
        if not isinstance(reasoning_summary, str) or len(reasoning_summary) > 500:
            return None
        entities = value.get("entities")
        constraints = value.get("constraints")
        capabilities = value.get("capabilities", [])
        if not isinstance(entities, dict) or not isinstance(constraints, list):
            return None
        if not all(isinstance(item, dict) for item in constraints):
            return None
        if not isinstance(capabilities, list) or not all(
            isinstance(item, dict) for item in capabilities
        ):
            return None
        canonical_capabilities = canonicalize_capabilities(
            capabilities, source="DEEPSEEK", provenance="MODEL_INTERPRETED"
        )
        return cls(
            intent=intent,
            assertion_status=assertion_status,
            effective_time=effective_time,
            event_type=event_type,
            requested_action=requested_action,
            confidence=confidence,
            reasoning_summary=reasoning_summary,
            entities=dict(entities),
            constraints=tuple(dict(item) for item in constraints),
            capabilities=tuple(canonical_capabilities),
        )

    @classmethod
    def from_json(cls, raw_text: str) -> "SemanticEnvelope | None":
        text = raw_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return cls.from_mapping(value) if isinstance(value, dict) else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "assertion_status": self.assertion_status,
            "effective_time": self.effective_time,
            "event_type": self.event_type,
            "requested_action": self.requested_action,
            "confidence": self.confidence,
            "reasoning_summary": self.reasoning_summary,
            "entities": dict(self.entities),
            "constraints": [dict(item) for item in self.constraints],
            "capabilities": [dict(item) for item in self.capabilities],
        }


@dataclass(frozen=True)
class SemanticPolicyDecision:
    route_intent: str
    action: str | None
    event_type: str | None
    create_event: bool
    verify_evidence: bool
    reason: str


def apply_semantic_policy(envelope: SemanticEnvelope) -> SemanticPolicyDecision:
    """Map a valid envelope to an existing safe CityNexus route."""

    if envelope.assertion_status == "HYPOTHETICAL":
        return SemanticPolicyDecision(
            "WHAT_IF", "SIMULATE", None, False, False, "hypothetical request remains read-only"
        )
    if envelope.assertion_status == "UNVERIFIED":
        return SemanticPolicyDecision(
            "VERIFY_FACT",
            "VERIFY_EVIDENCE",
            None,
            False,
            True,
            "unverified claim requires evidence before any Event",
        )
    if envelope.assertion_status in {"NEGATED", "HISTORICAL"}:
        return SemanticPolicyDecision(
            "UNKNOWN", None, None, False, False, "negated or historical statement is not a live Event"
        )
    if envelope.intent == "PERSONAL_ASSISTANCE" or envelope.event_type == "PERSONAL":
        return SemanticPolicyDecision(
            "PERSONAL_ASSISTANCE",
            "REQUEST_ASSISTANCE",
            None,
            False,
            False,
            "personal assistance does not create a city Event",
        )
    if (
        envelope.intent == "DISRUPTION_REPORT"
        and envelope.assertion_status == "ASSERTED"
        and envelope.effective_time in {"NOW", "FUTURE"}
        and envelope.event_type in CITY_EVENT_TYPES
    ):
        return SemanticPolicyDecision(
            "DISRUPTION_REPORT",
            "REQUEST_REPLAN",
            envelope.event_type,
            True,
            False,
            "asserted city disruption enters the existing recovery gate",
        )
    if envelope.intent == "VERIFY_FACT" or envelope.requested_action == "VERIFY_EVIDENCE":
        return SemanticPolicyDecision(
            "VERIFY_FACT", "VERIFY_EVIDENCE", None, False, True, "evidence verification requested"
        )
    action = envelope.requested_action if envelope.requested_action != "NONE" else None
    return SemanticPolicyDecision(
        envelope.intent,
        action,
        None,
        False,
        False,
        envelope.reasoning_summary or "structured semantic request",
    )


def compile_semantic_envelope(
    client: Any, message: str, verified_context: Mapping[str, Any]
) -> SemanticEnvelope | None:
    """Call DeepSeek once and accept only the bounded structured envelope."""

    messages = [
        {
            "role": "system",
            "content": (
                "You are the CityNexus semantic compiler. Return only the supplied JSON Schema. "
                "Interpret the user's natural language, including assertion status and effective time. "
                "Use UNKNOWN when meaning is ambiguous. Never invent server IDs, coordinates, prices, "
                "opening hours, route times, commitments, bookings, or successful mutations. "
                "If the user states a capability or accessibility need, include only a bounded capability "
                "item with code, severity, and params. Do not claim a place or route is accessible; "
                "accessibility evidence belongs to CityNexus provider or frozen facts. "
                "reasoning_summary must be concise and user-safe; do not reveal hidden reasoning."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"message": message, "verified_context": dict(verified_context)},
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    result = client.complete(
        messages,
        reasoning_effort="none",
        response_schema=SEMANTIC_ENVELOPE_SCHEMA,
        response_schema_name="citynexus_semantic_envelope",
    )
    envelope = SemanticEnvelope.from_json(result.text)
    if envelope is None:
        return None
    # A model omission must not erase a user-stated hard need.  This local
    # extraction adds bounded meaning only; provider accessibility evidence
    # remains a CityNexus-owned concern.
    local = extract_capabilities_from_text(message, scope="JOURNEY")
    merged = canonicalize_capabilities(
        [*envelope.capabilities, *local], source="SEMANTIC", provenance="USER_STATED"
    )
    if merged == envelope.capabilities:
        return envelope
    return SemanticEnvelope(
        intent=envelope.intent,
        assertion_status=envelope.assertion_status,
        effective_time=envelope.effective_time,
        event_type=envelope.event_type,
        requested_action=envelope.requested_action,
        confidence=envelope.confidence,
        reasoning_summary=envelope.reasoning_summary,
        entities=envelope.entities,
        constraints=envelope.constraints,
        capabilities=tuple(merged),
    )
