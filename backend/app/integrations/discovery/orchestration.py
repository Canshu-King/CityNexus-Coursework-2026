"""V13.4 provider-truth discovery orchestration over the existing provider mesh.

DeepSeek chooses meaning and soft ranking. CityNexus resolves anchors, owns
provider identities, and keeps feasibility and composition deterministic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Activity, Commitment, Journey, JourneyTripContext, Place, WebObservation
from app.execution.traveler_twin import planning_preference_items, refresh_snapshot, snapshot_payload
from app.integrations.ai.responses import DeepSeekResponsesClient, DeepSeekResponsesError
from app.integrations.ai.web_research import WebResearchProvider
from app.integrations.discovery.real_city import (
    capture_real_city_candidates,
    discovery_anchor_options,
)
from app.integrations.inbox.semantic_intake import journey_memory_payload


DISCOVERY_INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "anchor_key": {
            "type": "string",
            "enum": ["CURRENT_ACTIVITY", "HOTEL", "ARRIVAL", "UPCOMING_COMMITMENT", "DEPARTURE"],
        },
        "time_window": {
            "type": "object",
            "properties": {
                "start_at": {"type": "string"},
                "end_at": {"type": "string"},
            },
            "required": ["start_at", "end_at"],
            "additionalProperties": False,
        },
        "queries": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "categories": {"type": "array", "items": {"type": "string"}},
                    "radius_m": {"type": "integer", "minimum": 100, "maximum": 5000},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query", "categories", "radius_m", "limit"],
                "additionalProperties": False,
            },
        },
        "ranking_priorities": {"type": "array", "items": {"type": "string"}},
        "avoid": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
    },
    "required": [
        "goal", "anchor_key", "time_window", "queries", "ranking_priorities", "avoid", "explanation"
    ],
    "additionalProperties": False,
}


CANDIDATE_CURATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "semantic_fit": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                    "soft_concerns": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["candidate_id", "semantic_fit", "reason", "soft_concerns"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ranked"],
    "additionalProperties": False,
}


ANCHOR_KEYS = {"CURRENT_ACTIVITY", "HOTEL", "ARRIVAL", "UPCOMING_COMMITMENT", "DEPARTURE"}


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _clamp(value: Any, lower: int, upper: int, default: int) -> int:
    try:
        return max(lower, min(upper, int(value)))
    except (TypeError, ValueError):
        return default


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _preference_payload(session: Session, journey: Journey) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in planning_preference_items(session, journey.traveler_id, journey.id):
        items.append(
            {
                "category": str(getattr(item, "category", "interest")),
                "key": str(getattr(item, "preference_key", "")),
                "value": _load(getattr(item, "value_json", ""), None),
                "weight": float(getattr(item, "weight", 0.0)),
                "source": str(getattr(item, "source", "EXPLICIT")),
            }
        )
    return items


def build_discovery_context(
    session: Session,
    journey: Journey,
    *,
    goal: str,
    anchors: list[dict[str, Any]],
    weather: dict[str, Any],
) -> dict[str, Any]:
    snapshot = refresh_snapshot(session, journey.traveler_id)
    commitments = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
        ).order_by(Commitment.level.desc(), Commitment.id)
    ).all()
    return {
        "goal": goal,
        "journey": {
            "id": journey.id,
            "city_code": journey.city_code,
            "timezone": journey.timezone,
            "start_at": journey.start_at,
            "end_at": journey.end_at,
            "budget": {
                "amount_minor": journey.budget_amount_minor,
                "currency": journey.budget_currency,
            },
        },
        "anchors": [
            {
                "anchor_key": item["anchor_key"],
                "source": item["source"],
                "location": item.get("location"),
                "latitude": item["latitude"],
                "longitude": item["longitude"],
                "at": item.get("at"),
            }
            for item in anchors
        ],
        "journey_memory": journey_memory_payload(session, journey),
        "traveler_twin": snapshot_payload(snapshot),
        "preferences": _preference_payload(session, journey),
        "protected_commitments": [
            {
                "id": item.id,
                "kind": item.kind,
                "level": item.level,
                "external_ref": item.external_ref,
                "policy": _load(item.policy_json, {}),
            }
            for item in commitments[:12]
        ],
        "weather": weather,
    }


def _default_intent(goal: str, journey: Journey, anchors: list[dict[str, Any]]) -> dict[str, Any]:
    text = goal.casefold()
    culture = any(token in text for token in ("culture", "museum", "art", "history", "文化"))
    indoor = any(token in text for token in ("rain", "indoor", "weather", "雨", "室内"))
    low_walking = any(token in text for token in ("low walking", "less walking", "步行少", "少走"))
    anchor_key = "ARRIVAL"
    if any(item["anchor_key"] == "CURRENT_ACTIVITY" for item in anchors):
        anchor_key = "CURRENT_ACTIVITY"
    elif any(item["anchor_key"] == "UPCOMING_COMMITMENT" for item in anchors) and any(
        token in text for token in ("before", "cruise", "commitment", "17:30", "之前")
    ):
        anchor_key = "UPCOMING_COMMITMENT"
    elif not any(item["anchor_key"] == "ARRIVAL" for item in anchors):
        anchor_key = anchors[0]["anchor_key"] if anchors else "ARRIVAL"
    goal_code = "RAIN_FRIENDLY_CULTURE" if culture and indoor else (
        "CULTURE" if culture else ("INDOOR_LEISURE" if indoor else "CITY_DISCOVERY")
    )
    priorities = (["culture"] if culture else []) + (["low_walking"] if low_walking else [])
    priorities += (["indoor"] if indoor else [])
    priorities += ["commitment_preservation", "budget"]
    anchor_at = next((item.get("at") for item in anchors if item["anchor_key"] == anchor_key), None)
    start_at = anchor_at or journey.start_at
    end_at = journey.end_at
    for item in anchors:
        if item["anchor_key"] == "UPCOMING_COMMITMENT" and item.get("at"):
            end_at = item["at"]
            break
    query = "museum cultural centre" if culture else ("indoor cafe" if indoor else goal[:80])
    return {
        "goal": goal_code,
        "anchor_key": anchor_key,
        "time_window": {"start_at": start_at, "end_at": end_at},
        "queries": [
            {
                "query": query,
                "categories": ["museum", "arts_centre"] if culture else ["cafe", "library"],
                "radius_m": 3000,
                "limit": 8,
            }
        ],
        "ranking_priorities": priorities,
        "avoid": ["long_outdoor_walk"] if low_walking or indoor else [],
        "explanation": "Bounded semantic discovery around a server-known Journey anchor.",
    }


def _normalize_intent(
    raw: dict[str, Any],
    *,
    fallback: dict[str, Any],
    anchors: list[dict[str, Any]],
    radius_m: int,
    limit: int,
) -> dict[str, Any]:
    allowed = {item["anchor_key"] for item in anchors} & ANCHOR_KEYS
    anchor_key = str(raw.get("anchor_key") or fallback["anchor_key"]).upper()
    if anchor_key not in allowed:
        anchor_key = fallback["anchor_key"] if fallback["anchor_key"] in allowed else next(iter(allowed), "ARRIVAL")
    raw_queries = raw.get("queries") if isinstance(raw.get("queries"), list) else []
    queries: list[dict[str, Any]] = []
    for item in raw_queries[:4]:
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            continue
        queries.append(
            {
                "query": str(item["query"]).strip()[:120],
                "categories": [str(value)[:40] for value in (item.get("categories") or [])[:6]],
                "radius_m": min(radius_m, _clamp(item.get("radius_m"), 100, 5000, radius_m)),
                "limit": min(limit, _clamp(item.get("limit"), 1, 10, limit)),
            }
        )
    if not queries:
        queries = [dict(fallback["queries"][0])]
        queries[0]["radius_m"] = min(radius_m, _clamp(queries[0].get("radius_m"), 100, 5000, radius_m))
        queries[0]["limit"] = min(limit, _clamp(queries[0].get("limit"), 1, 10, limit))
    window = raw.get("time_window") if isinstance(raw.get("time_window"), dict) else {}
    start_at = str(window.get("start_at") or fallback["time_window"]["start_at"])
    end_at = str(window.get("end_at") or fallback["time_window"]["end_at"])
    return {
        "goal": str(raw.get("goal") or fallback["goal"])[:80],
        "anchor_key": anchor_key,
        "time_window": {"start_at": start_at, "end_at": end_at},
        "queries": queries,
        "ranking_priorities": [str(item)[:48] for item in (raw.get("ranking_priorities") or fallback["ranking_priorities"])[:8]],
        "avoid": [str(item)[:48] for item in (raw.get("avoid") or fallback["avoid"])[:8]],
        "explanation": str(raw.get("explanation") or fallback["explanation"])[:300],
    }


def compile_discovery_intent(
    session: Session,
    journey: Journey,
    *,
    goal: str,
    anchors: list[dict[str, Any]],
    weather: dict[str, Any],
    client: DeepSeekResponsesClient | None,
    radius_m: int,
    limit: int,
) -> dict[str, Any]:
    fallback = _default_intent(goal, journey, anchors)
    context = build_discovery_context(
        session, journey, goal=goal, anchors=anchors, weather=weather
    )
    provider = "SEALED_REPLAY_SEMANTIC_FALLBACK"
    model = None
    raw = fallback
    if client is not None:
        result = client.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are the CityNexus Discovery Intent Compiler. Interpret the traveler's "
                        "natural-language goal using the supplied Journey facts. Return only the "
                        "requested JSON Schema. Choose only symbolic anchor_key values supplied by "
                        "the server. Never output coordinates, place IDs, or invented provider facts. "
                        "Keep queries between one and four and prioritize protected commitments."
                    ),
                },
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            reasoning_effort="low",
            response_schema=DISCOVERY_INTENT_SCHEMA,
            response_schema_name="citynexus_discovery_intent",
        )
        parsed = json.loads(result.text)
        raw = parsed if isinstance(parsed, dict) else fallback
        provider = "DEEPSEEK_RESPONSES"
        model = getattr(client, "model", None)
    intent = _normalize_intent(
        raw,
        fallback=fallback,
        anchors=anchors,
        radius_m=_clamp(radius_m, 100, 5000, 3000),
        limit=_clamp(limit, 1, 10, 8),
    )
    intent["provider"] = provider
    intent["model"] = model
    intent["context_fingerprint"] = hashlib.sha256(
        json.dumps(context, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return intent


def _candidate_score(candidate: dict[str, Any], priorities: list[str]) -> float:
    searchable = " ".join(
        [
            str(candidate.get("name") or ""),
            str(candidate.get("category") or ""),
            " ".join(str(item) for item in candidate.get("tags") or []),
        ]
    ).casefold()
    matches = sum(1 for item in priorities if str(item).casefold().replace("_", " ") in searchable)
    return min(1.0, 0.48 + 0.12 * matches)


def curate_candidates(
    *,
    candidates: list[dict[str, Any]],
    intent: dict[str, Any],
    context: dict[str, Any],
    client: DeepSeekResponsesClient | None,
) -> dict[str, Any]:
    known = {str(item.get("id")): item for item in candidates if item.get("id")}
    provider = "SEALED_REPLAY_SEMANTIC_FALLBACK"
    model = None
    raw_ranked: list[dict[str, Any]] = []
    if client is not None and known:
        payload = {
            "intent": intent,
            "traveler_twin": context.get("traveler_twin"),
            "protected_commitments": context.get("protected_commitments"),
            "candidates": [
                {
                    "candidate_id": key,
                    "name": item.get("name"),
                    "category": item.get("category"),
                    "tags": item.get("tags"),
                    "opening_hours": item.get("opening_hours"),
                    "cost_evidence": item.get("cost_evidence"),
                    "distance_m": item.get("distance_m"),
                }
                for key, item in known.items()
            ],
        }
        result = client.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are the CityNexus Candidate Curator. Rank only candidate_id values in "
                        "the supplied server list. Return semantic fit and a concise user-facing reason. "
                        "Do not reject or override deterministic opening-hours, cost, route, or commitment "
                        "facts; those are handled by CityNexus."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            reasoning_effort="low",
            response_schema=CANDIDATE_CURATOR_SCHEMA,
            response_schema_name="citynexus_candidate_curation",
        )
        parsed = json.loads(result.text)
        raw_ranked = parsed.get("ranked", []) if isinstance(parsed, dict) else []
        provider = "DEEPSEEK_RESPONSES"
        model = getattr(client, "model", None)
    if not raw_ranked:
        raw_ranked = [
            {
                "candidate_id": key,
                "semantic_fit": _candidate_score(item, intent.get("ranking_priorities", [])),
                "reason": "Provider-grounded candidate matches the bounded Journey discovery intent.",
                "soft_concerns": [],
            }
            for key, item in known.items()
        ]
    ranked: list[dict[str, Any]] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for item in raw_ranked:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "")
        if candidate_id not in known:
            if candidate_id:
                rejected.append(candidate_id)
            continue
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        ranked.append(
            {
                "candidate_id": candidate_id,
                "semantic_fit": _confidence(item.get("semantic_fit")),
                "reason": str(item.get("reason") or "")[:300],
                "soft_concerns": [str(value)[:120] for value in (item.get("soft_concerns") or [])[:6]],
            }
        )
    ranked.sort(key=lambda item: (-item["semantic_fit"], item["candidate_id"]))
    return {
        "provider": provider,
        "model": model,
        "ranked": ranked,
        "rejected_candidate_ids": sorted(set(rejected)),
    }


def deterministic_candidate_gate(
    candidates: list[dict[str, Any]],
    ranked: dict[str, Any],
    *,
    require_known_hours: bool = True,
) -> dict[str, Any]:
    known = {str(item.get("id")): item for item in candidates if item.get("id")}
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in ranked.get("ranked", []):
        candidate = known.get(str(row.get("candidate_id")))
        if candidate is None:
            rejected.append({"candidate_id": row.get("candidate_id"), "reason": "UNKNOWN_CANDIDATE_ID"})
            continue
        hours = candidate.get("opening_hours") if isinstance(candidate.get("opening_hours"), dict) else {}
        hours_status = str(hours.get("status") or "UNKNOWN")
        cost = candidate.get("cost_evidence") if isinstance(candidate.get("cost_evidence"), dict) else {}
        cost_status = str(cost.get("status") or "UNKNOWN")
        if hours_status == "CLOSED":
            rejected.append({"candidate_id": candidate["id"], "reason": "OPENING_HOURS_CLOSED"})
            continue
        if require_known_hours and hours_status in {"UNKNOWN", "RAW_UNPARSED"}:
            rejected.append({"candidate_id": candidate["id"], "reason": "OPENING_HOURS_UNKNOWN"})
            continue
        if cost_status == "UNKNOWN":
            rejected.append({"candidate_id": candidate["id"], "reason": "COST_UNKNOWN"})
            continue
        eligible.append(
            {
                **candidate,
                "semantic_fit": row.get("semantic_fit", 0.0),
                "curator_reason": row.get("reason", ""),
                "soft_concerns": row.get("soft_concerns", []),
            }
        )
    return {
        "passed": bool(eligible),
        "eligible": eligible,
        "rejected": rejected,
        "authority": "DETERMINISTIC_PROVIDER_FACT_GATE",
    }


def _capture_sourced_web_evidence(
    session: Session,
    journey: Journey,
    candidates: list[dict[str, Any]],
    client: DeepSeekResponsesClient | None,
) -> list[str]:
    """Persist returned source observations without promoting model claims."""

    if client is None or not settings.deepseek_web_search_enabled:
        return []
    researcher = WebResearchProvider(client)
    observation_ids: list[str] = []
    for candidate in candidates[:4]:
        hours = candidate.get("opening_hours") if isinstance(candidate.get("opening_hours"), dict) else {}
        cost = candidate.get("cost_evidence") if isinstance(candidate.get("cost_evidence"), dict) else {}
        if str(hours.get("status") or "UNKNOWN") not in {"UNKNOWN", "RAW_UNPARSED"} and str(
            cost.get("status") or "UNKNOWN"
        ) != "UNKNOWN":
            continue
        city_hint = str(journey.city_code or "").split("-", 1)[-1].replace("_", " ")
        context = session.get(JourneyTripContext, journey.id)
        arrival = _load(context.arrival_anchor_json if context else None, {})
        arrival_hint = str(arrival.get("location") or "").strip()
        query = (
            f"{candidate.get('name', 'venue')} {arrival_hint or city_hint} official opening hours "
            "ticket price; return source pages only"
        )
        drafts = researcher.search(query)
        if not drafts:
            continue
        rows: list[dict[str, Any]] = []
        for draft in drafts:
            existing = session.scalar(
                select(WebObservation).where(
                    WebObservation.journey_id == journey.id,
                    WebObservation.evidence_fingerprint == draft.evidence_fingerprint,
                )
            )
            if existing is None:
                existing = WebObservation(
                    id=str(uuid4()),
                    journey_id=journey.id,
                    query=draft.query,
                    url=draft.url,
                    domain=draft.domain,
                    title=draft.title,
                    source_class="OPEN_WEB",
                    provider_key=draft.provider_key,
                    model_key=draft.model_key,
                    captured_at=draft.captured_at,
                    evidence_fingerprint=draft.evidence_fingerprint,
                    content_excerpt=draft.content_excerpt,
                    authority_status="NON_AUTHORITATIVE",
                    created_at=draft.captured_at,
                )
                session.add(existing)
            observation_ids.append(existing.id)
            rows.append(
                {
                    "id": existing.id,
                    "url": draft.url,
                    "domain": draft.domain,
                    "title": draft.title,
                    "source_class": "OPEN_WEB",
                    "excerpt": draft.content_excerpt,
                }
            )
        if rows:
            metadata = dict(candidate.get("metadata") or {})
            metadata["sourced_web_evidence"] = {
                "observations": rows,
                "authority": "NON_AUTHORITATIVE_UNTIL_FACT_COMPLETION",
            }
            candidate["metadata"] = metadata
    session.flush()
    return list(dict.fromkeys(observation_ids))


def _weather_summary(
    mode: str,
    journey: Journey,
    anchor: dict[str, Any] | None = None,
    time_window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    configured = Path(settings.real_city_replay_snapshot_path)
    root = Path(__file__).resolve().parents[4]
    path = configured if configured.is_absolute() else root / configured
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    weather = payload.get("weather") if isinstance(payload.get("weather"), dict) else {}
    hourly = weather.get("hourly") if isinstance(weather.get("hourly"), dict) else {}
    window = time_window if isinstance(time_window, dict) else {}
    start_at = str(window.get("start_at") or journey.start_at)
    end_at = str(window.get("end_at") or journey.end_at)
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    relevant_indexes = [
        index
        for index, value in enumerate(times)
        if start_at[:13] <= str(value)[:13] <= end_at[:13]
    ]
    coverage_status = "CAPTURED_WINDOW"
    if not relevant_indexes:
        relevant_indexes = list(range(len(times)))
        coverage_status = "SEALED_REPLAY_OUT_OF_REQUEST_WINDOW"
    relevant_hourly: dict[str, Any] = {}
    for key, values in hourly.items():
        if isinstance(values, list):
            relevant_hourly[key] = [values[index] for index in relevant_indexes if index < len(values)]
        else:
            relevant_hourly[key] = values
    return {
        "provider": weather.get("provider") or "open-meteo",
        "captured_at": payload.get("captured_at") or datetime.now(timezone.utc).isoformat(),
        "provenance": "SEALED_REAL_REPLAY" if mode == "captured_replay" else "REPLAY",
        "state": (
            "RAIN_PREFERRED_CONTEXT"
            if coverage_status == "CAPTURED_WINDOW"
            else "WEATHER_WINDOW_UNCERTAIN"
        ),
        "coordinates": {
            "latitude": anchor.get("latitude") if anchor else None,
            "longitude": anchor.get("longitude") if anchor else None,
            "source": anchor.get("source") if anchor else None,
        },
        "time_window": {"start_at": start_at, "end_at": end_at},
        "coverage_status": coverage_status,
        "evidence": {**weather, "hourly": relevant_hourly},
    }


def orchestrate_discovery(
    session: Session,
    journey: Journey,
    *,
    goal: str,
    mode: str,
    radius_m: int,
    limit: int,
    client: DeepSeekResponsesClient | None,
    require_fully_grounded: bool = False,
) -> dict[str, Any]:
    anchors = discovery_anchor_options(session, journey)
    weather = _weather_summary(mode, journey, anchors[0] if anchors else None)
    intent = compile_discovery_intent(
        session,
        journey,
        goal=goal,
        anchors=anchors,
        weather=weather,
        client=client,
        radius_m=radius_m,
        limit=limit,
    )
    anchor = next((item for item in anchors if item["anchor_key"] == intent["anchor_key"]), None)
    if anchor is None and anchors:
        anchor = anchors[0]
    weather = _weather_summary(mode, journey, anchor, intent.get("time_window"))
    discovery_runs = [
        capture_real_city_candidates(
            session,
            journey,
            query=query["query"],
            radius_m=query["radius_m"],
            limit=query["limit"],
            mode=mode,
        )
        for query in intent["queries"][:4]
    ]
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for run in discovery_runs:
        for candidate in run.get("candidates", []):
            candidate_id = str(candidate.get("id") or "")
            if candidate_id and candidate_id not in candidates_by_id:
                candidates_by_id[candidate_id] = candidate
    first_run = discovery_runs[0] if discovery_runs else {
        "mode": mode,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": None,
        "provider_status": {},
        "provenance": "REPLAY",
        "provider_evidence": {},
    }
    discovery = {
        **first_run,
        "candidates": list(candidates_by_id.values()),
        "provider_status": {
            **(first_run.get("provider_status") or {}),
            "queries": [run.get("provider_status", {}) for run in discovery_runs],
            "query_count": len(discovery_runs),
        },
        "provider_evidence": first_run.get("provider_evidence", {}),
    }
    web_observation_ids = _capture_sourced_web_evidence(
        session,
        journey,
        discovery["candidates"] if isinstance(discovery.get("candidates"), list) else [],
        client if mode == "live" else None,
    )
    context = build_discovery_context(
        session, journey, goal=goal, anchors=anchors, weather=weather
    )
    curator = curate_candidates(
        candidates=discovery.get("candidates", []),
        intent=intent,
        context=context,
        client=client,
    )
    if not curator["ranked"] and discovery.get("candidates"):
        curator = {
            **curator,
            "ranked": [
                {
                    "candidate_id": item["id"],
                    "semantic_fit": _candidate_score(item, intent.get("ranking_priorities", [])),
                    "reason": "Server-grounded candidate retained after the semantic response contained no known candidate ID.",
                    "soft_concerns": ["MODEL_CANDIDATE_IDS_REJECTED"],
                }
                for item in discovery["candidates"]
                if item.get("id")
            ],
            "semantic_fallback": "SERVER_GROUNDED_CANDIDATE_SET",
        }
    gate = deterministic_candidate_gate(discovery.get("candidates", []), curator)
    protected_place_ids: list[str] = []
    for commitment in session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
            Commitment.level == "C4_Anchored",
        )
    ).all():
        activity = session.get(Activity, commitment.activity_id) if commitment.activity_id else None
        policy = _load(commitment.policy_json, {})
        place_id = activity.place_id if activity else str(policy.get("semantic_key") or "")
        if place_id and session.get(Place, place_id) is not None:
            protected_place_ids.append(place_id)
    selected = [item["id"] for item in gate["eligible"][:2]]
    if protected_place_ids:
        selected = selected[:1] + [item for item in protected_place_ids if item not in selected]
    elif len(selected) < 2:
        selected = [item["id"] for item in gate["eligible"][:2]]
    replay = {
        "bundle_id": f"v13.5:{journey.id}:{discovery.get('captured_at')}",
        "mode": discovery.get("mode"),
        "provenance": discovery.get("runtime_provenance") or discovery.get("provenance"),
        "provider_status": discovery.get("provider_status", {}),
        "sealed": mode in {"replay", "captured_replay"},
    }
    evidence = {
        "version": "V13.5",
        "intent": intent,
        "anchor": anchor,
        "anchor_options": anchors,
        "journey_memory": context.get("journey_memory"),
        "traveler_twin": context.get("traveler_twin"),
        "provider_discovery": replay,
        "provider_mobility": discovery.get("provider_evidence", {}).get("mobility"),
        "provider_weather": discovery.get("provider_evidence", {}).get("weather"),
        "web_observation_ids": web_observation_ids,
        "candidates": discovery.get("candidates", []),
        "curation": curator,
        "deterministic_gate": gate,
        "weather": weather,
        "selected_place_ids": selected,
        "require_fully_grounded": require_fully_grounded,
        "runtime_label": (
            "SEALED_REAL_REPLAY"
            if mode == "captured_replay"
            else ("SEALED_PROVIDER_REPLAY" if replay["sealed"] else "LIVE_PROVIDER_CAPTURE")
        ),
    }
    return {
        "journey_id": journey.id,
        "intent": intent,
        "anchor": anchor,
        "anchor_options": anchors,
        "weather": weather,
        "discovery": discovery,
        "curation": curator,
        "feasibility": gate,
        "selected_place_ids": selected,
        "replay": replay,
        "require_fully_grounded": require_fully_grounded,
        "web_observation_ids": web_observation_ids,
        "evidence": evidence,
    }
