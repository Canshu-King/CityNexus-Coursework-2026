"""V4.1 visual reservation extraction; output remains a confirmation draft."""

from __future__ import annotations

import json
from typing import Any

from app.integrations.ai.responses import DeepSeekResponsesClient, DeepSeekResponsesError


RESERVATION_INTERPRETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reservation_type": {"type": ["string", "null"]},
        "confirmation_ref": {"type": ["string", "null"]},
        "origin": {"type": ["object", "null"], "additionalProperties": True},
        "destination": {"type": ["object", "null"], "additionalProperties": True},
        "scheduled_departure_at": {"type": ["string", "null"]},
        "scheduled_arrival_at": {"type": ["string", "null"]},
        "price_amount_minor": {"type": ["integer", "null"], "minimum": 0},
        "currency": {"type": ["string", "null"]},
        "anchor_role": {
            "type": "string",
            "enum": ["ARRIVAL", "DEPARTURE", "MID_TRIP", "UNKNOWN"],
        },
        "anchor_role_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "payment_status": {"type": "string"},
        "refundability": {"type": "string"},
        "suggested_commitment_level": {"type": "string"},
        "semantic_target": {"type": "object", "additionalProperties": True},
        "candidate_journey_match": {
            "type": "object",
            "properties": {
                "match": {"type": "string", "enum": ["MATCH", "MISMATCH", "UNKNOWN"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
            "required": ["match", "confidence", "reason"],
            "additionalProperties": False,
        },
        "uncertain_fields": {"type": "array", "items": {"type": "string"}},
        "reasoning_summary": {"type": "string"},
        "field_provenance": {"type": "object", "additionalProperties": True},
    },
    "required": [
        "reservation_type", "confirmation_ref", "origin", "destination",
        "scheduled_departure_at", "scheduled_arrival_at", "price_amount_minor",
        "currency", "anchor_role", "anchor_role_confidence", "payment_status",
        "refundability", "suggested_commitment_level", "semantic_target",
        "candidate_journey_match", "uncertain_fields", "reasoning_summary",
        "field_provenance",
    ],
    "additionalProperties": False,
}


def extract_reservation_image(
    client: DeepSeekResponsesClient,
    image_url: str,
    *,
    hint: str = "booking confirmation",
    journey_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = client.complete(
        [
            {
                "role": "system",
                "content": (
                    "Act as the CityNexus Journey Semantic Interpreter. Read this travel document "
                    "image and return only JSON with reservation_type, confirmation_ref, origin, "
                    "destination, scheduled_departure_at, scheduled_arrival_at, price_amount_minor, "
                    "currency, anchor_role, anchor_role_confidence, payment_status, refundability, "
                    "suggested_commitment_level, semantic_target, candidate_journey_match, "
                    "uncertain_fields, reasoning_summary, and field_provenance. candidate_journey_match "
                    "must contain match=MATCH|MISMATCH|UNKNOWN, confidence, and a short reason. Allowed anchor_role "
                    "values are ARRIVAL, DEPARTURE, MID_TRIP, and UNKNOWN. Use UNKNOWN when the "
                    "evidence is genuinely uncertain. Never invent a reference, price, time, or "
                    "payment status. Keep reasoning_summary short and do not return hidden reasoning. "
                    "Use the supplied Journey context to interpret meaning, but let the server resolve "
                    "identity and enforce conflicts."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{hint}\nJourney context (server facts): "
                            f"{json.dumps(journey_context or {}, ensure_ascii=False)}"
                        ),
                    },
                    {"type": "input_image", "image_url": image_url},
                ],
            },
        ],
        reasoning_effort="low",
        response_schema=RESERVATION_INTERPRETER_SCHEMA,
        response_schema_name="citynexus_journey_semantic_interpretation",
    )
    try:
        parsed = json.loads(result.text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeepSeekResponsesError("visual reservation extraction was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise DeepSeekResponsesError("visual reservation extraction was not an object")
    return parsed


def extract_reservation_text(
    client: DeepSeekResponsesClient,
    text: str,
    *,
    journey_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = client.complete(
        [
            {
                "role": "system",
                "content": (
                    "Act as the CityNexus Journey Semantic Interpreter. Extract the travel artifact "
                    "and return only JSON with reservation_type, confirmation_ref, origin, destination, "
                    "scheduled_departure_at, scheduled_arrival_at, price_amount_minor, currency, "
                    "anchor_role, anchor_role_confidence, payment_status, refundability, "
                    "suggested_commitment_level, semantic_target, candidate_journey_match, "
                    "uncertain_fields, reasoning_summary, and field_provenance. candidate_journey_match "
                    "must contain match=MATCH|MISMATCH|UNKNOWN, confidence, and a short reason. Allowed anchor_role "
                    "values are ARRIVAL, DEPARTURE, MID_TRIP, and UNKNOWN. Infer the role when the "
                    "evidence is strong and use UNKNOWN when it is not. Never invent facts. Keep "
                    "reasoning_summary short, do not return hidden reasoning, and use Journey context "
                    "for semantic interpretation only; the server owns identity, conflicts, and mutation."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Journey context (server facts): {json.dumps(journey_context or {}, ensure_ascii=False)}\n"
                    f"Artifact text:\n{text}"
                ),
            },
        ],
        reasoning_effort="low",
        response_schema=RESERVATION_INTERPRETER_SCHEMA,
        response_schema_name="citynexus_journey_semantic_interpretation",
    )
    try:
        parsed = json.loads(result.text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeepSeekResponsesError("text reservation extraction was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise DeepSeekResponsesError("text reservation extraction was not an object")
    return parsed
