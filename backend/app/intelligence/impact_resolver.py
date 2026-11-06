from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Activity, Event, ImpactAssessment, Journey, Place
from app.integrations.ai.impact_provider import (
    DeepSeekImpactProvider,
    ImpactProvider,
    ImpactProviderError,
)
from app.intelligence.impact_contract import (
    ImpactActivityContext,
    ImpactCandidate,
    ImpactResolution,
)
from app.intelligence.impact_validation import _geometry_scope_ref, validate_impact_candidates

_PLACE_ALIAS_OVERRIDES: dict[str, tuple[str, ...]] = {
    "place-river-cruise": ("海河游船", "海河沿线游船", "River Cruise", "Haihe Cruise"),
    "place-tech-art": ("科技艺术中心", "Tech Art", "Technology Art Center"),
    "place-riverside-museum": ("河畔博物馆", "Riverside Museum"),
}


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _place_context(place: Place | None) -> dict[str, Any]:
    if place is None:
        return {}
    return {
        "place_id": place.id,
        "name": place.name,
        "category": place.category,
        "tags": _json_object(place.tags_json)
        if place.tags_json.startswith("{")
        else json.loads(place.tags_json or "[]"),
        "latitude": place.latitude,
        "longitude": place.longitude,
    }


def _place_aliases(place: Place | None) -> tuple[str, ...]:
    if place is None:
        return ()
    try:
        raw_tags = json.loads(place.tags_json or "[]")
    except json.JSONDecodeError:
        raw_tags = []
    if isinstance(raw_tags, dict):
        raw_tags = [*raw_tags.keys(), *raw_tags.values()]
    if not isinstance(raw_tags, list):
        return ()
    aliases = [str(item).strip() for item in raw_tags if str(item).strip()]
    aliases.extend(_PLACE_ALIAS_OVERRIDES.get(place.id, ()))
    return tuple(dict.fromkeys(aliases))


def _activity_context(
    activity: Activity,
    *,
    place: Place | None,
    is_current: bool,
    is_c4: bool,
) -> ImpactActivityContext:
    return ImpactActivityContext(
        activity_id=activity.id,
        sequence_no=activity.sequence_no,
        title=activity.title,
        activity_type=activity.activity_type,
        place_id=activity.place_id or activity.city_service_id,
        place_name=place.name if place else None,
        place_category=place.category if place else None,
        latitude=place.latitude if place else None,
        longitude=place.longitude if place else None,
        start_at=datetime.fromisoformat(activity.start_at),
        end_at=datetime.fromisoformat(activity.end_at),
        status=activity.status,
        locked=bool(activity.locked),
        is_current=is_current,
        is_c4=is_c4,
        city_service_id=activity.city_service_id,
        place_aliases=_place_aliases(place),
    )


def build_impact_context(
    *,
    event: Event,
    journey: Journey,
    activities: Iterable[Activity],
    places_by_id: dict[str, Place],
    c4_activity_ids: set[str] | None = None,
) -> tuple[dict[str, Any], tuple[ImpactActivityContext, ...]]:
    payload = _json_object(event.payload_json)
    contexts_list: list[ImpactActivityContext] = []
    for activity in activities:
        if isinstance(activity, ImpactActivityContext):
            contexts_list.append(activity)
            continue
        contexts_list.append(
            _activity_context(
                activity,
                place=places_by_id.get(activity.place_id or ""),
                is_current=activity.id == journey.current_activity_id,
                is_c4=activity.id in (c4_activity_ids or set()),
            )
        )
    contexts = tuple(contexts_list)
    context = {
        "event": {
            "event_id": event.id,
            "event_type": event.event_type,
            "severity": event.severity,
            "occurred_at": event.occurred_at,
            "provider_key": event.provider_key,
            "provenance": event.provenance,
            "payload": payload,
        },
        "journey": {
            "journey_id": journey.id,
            "timezone": journey.timezone,
            "city_code": journey.city_code,
        },
        "candidate_activities": [
            {
                "activity_id": item.activity_id,
                "sequence_no": item.sequence_no,
                "title": item.title,
                "activity_type": item.activity_type,
                "place_id": item.place_id,
                "place_name": item.place_name,
                "place_aliases": list(item.place_aliases),
                "city_service_id": item.city_service_id,
                "place_category": item.place_category,
                "coordinates": {
                    "latitude": item.latitude,
                    "longitude": item.longitude,
                },
                "start_at": item.start_at.isoformat(),
                "end_at": item.end_at.isoformat(),
                "status": item.status,
                "locked": item.locked,
                "is_current": item.is_current,
                "is_c4": item.is_c4,
            }
            for item in contexts
        ],
    }
    return context, contexts


def _deterministic_candidates(
    event: Event,
    activities: tuple[ImpactActivityContext, ...],
) -> tuple[list[ImpactCandidate], str | None]:
    payload = _json_object(event.payload_json)
    if event.affected_activity_id:
        return [
            ImpactCandidate(
                activity_id=event.affected_activity_id,
                impact_type={
                    "CLOSURE": "CLOSURE",
                    "WEATHER": "WEATHER_RISK",
                    "TRAFFIC_DELAY": "DELAY",
                    "SCHEDULE_DELAY": "DELAY",
                }.get(event.event_type, "ACCESS_RISK"),
                confidence=1.0,
                reason_codes=["STRUCTURED_ACTIVITY_REFERENCE"],
                evidence_refs=["event.affected_activity_id"],
                human_explanation="The event contains a server-validated Activity reference.",
            )
        ], "deterministic"
    place_id = payload.get("affected_place_id") or payload.get("place_id")
    if place_id:
        matches = [item for item in activities if item.place_id == str(place_id)]
        if matches:
            return [
                ImpactCandidate(
                    activity_id=item.activity_id,
                    impact_type="ACCESS_RISK",
                    confidence=1.0,
                    reason_codes=["STRUCTURED_PLACE_REFERENCE"],
                    evidence_refs=["event.payload.affected_place_id"],
                    human_explanation="The event place matches a place on the active Journey plan.",
                )
                for item in matches
            ], "deterministic"
    if payload.get("disruption_polygon") or payload.get("polygon"):
        geometry_candidates = [
            ImpactCandidate(
                activity_id=item.activity_id,
                impact_type="ACCESS_RISK",
                confidence=0.85,
                reason_codes=["DETERMINISTIC_GEOMETRY_SCOPE"],
                evidence_refs=[_geometry_scope_ref(item, payload) or "event.payload.polygon"],
                human_explanation="The server-owned Activity point or route intersects the disruption polygon.",
            )
            for item in activities
            if _geometry_scope_ref(item, payload) is not None
        ]
        if geometry_candidates:
            return geometry_candidates, "deterministic-geometry"
    # A traveller names a venue in words ("鼓浪屿风琴博物馆临时闭馆").  Resolve it
    # deterministically against the plan before consulting any model, so the
    # same command cannot resolve on one run and come back unresolved on the
    # next.  Never guess: an ambiguous name produces no candidate, which the
    # caller reports as a clarification rather than a silent pick.
    named = _named_venue_query(payload)
    if named:
        matches = _match_named_venue(named, activities)
        if len(matches) == 1:
            item = matches[0]
            return [
                ImpactCandidate(
                    activity_id=item.activity_id,
                    impact_type={
                        "CLOSURE": "CLOSURE",
                        "WEATHER": "WEATHER_RISK",
                        "TRAFFIC_DELAY": "DELAY",
                        "SCHEDULE_DELAY": "DELAY",
                    }.get(event.event_type, "ACCESS_RISK"),
                    confidence=0.95,
                    reason_codes=["DETERMINISTIC_NAMED_VENUE_MATCH"],
                    evidence_refs=["event.payload.named_venue", "activity.title"],
                    human_explanation=(
                        f"The named venue matches one stop on the active plan: {item.title}."
                    ),
                )
            ], "deterministic-name-match"
        if len(matches) > 1:
            # Ambiguous: report every candidate so the traveller (or the caller)
            # can choose, instead of picking the first one.
            return [
                ImpactCandidate(
                    activity_id=item.activity_id,
                    impact_type="ACCESS_RISK",
                    confidence=0.5,
                    reason_codes=["AMBIGUOUS_NAMED_VENUE"],
                    evidence_refs=["event.payload.named_venue", "activity.title"],
                    human_explanation=(
                        f"The named venue matches more than one stop: {item.title}."
                    ),
                )
                for item in matches
            ], "deterministic-name-ambiguous"
    return [], None


def _named_venue_query(payload: dict[str, Any]) -> str:
    """The venue name the traveller stated, if any."""

    for key in ("named_venue", "venue_name", "place_name", "affected_place_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalise_venue(value: str) -> str:
    text = str(value or "").casefold()
    for junk in ("临时", "闭馆", "关闭", "停业", "装修", "维修", "的", "了",
                 "temporarily", "closed", "closure", "shut", "the", "is"):
        text = text.replace(junk, " ")
    return " ".join(text.split())


def _match_named_venue(
    query: str, activities: tuple[ImpactActivityContext, ...]
) -> list[ImpactActivityContext]:
    """Deterministic venue matching: exact title/alias first, then containment."""

    needle = _normalise_venue(query)
    if not needle:
        return []
    exact: list[ImpactActivityContext] = []
    partial: list[ImpactActivityContext] = []
    for item in activities:
        names = {_normalise_venue(item.title), _normalise_venue(item.place_name or "")}
        names |= {_normalise_venue(alias) for alias in item.place_aliases}
        names.discard("")
        if needle in names:
            exact.append(item)
            continue
        if any(name and (needle in name or name in needle) for name in names):
            partial.append(item)
    return exact or partial


def _build_provider() -> ImpactProvider | None:
    if not settings.ai_enabled or settings.impact_provider.lower() != "deepseek":
        return None
    if not settings.ai_api_key:
        return None
    return DeepSeekImpactProvider(
        api_key=settings.ai_api_key,
        model=settings.ai_model,
        base_url=settings.deepseek_api_url,
        timeout_seconds=settings.impact_timeout_seconds,
    )


def resolve_impact(
    *,
    event: Event,
    journey: Journey,
    activities: Iterable[Activity],
    places_by_id: dict[str, Place],
    c4_activity_ids: set[str] | None = None,
    provider: ImpactProvider | None = None,
) -> ImpactResolution:
    context, activity_contexts = build_impact_context(
        event=event,
        journey=journey,
        activities=activities,
        places_by_id=places_by_id,
        c4_activity_ids=c4_activity_ids,
    )
    deterministic_candidates, deterministic_provider = _deterministic_candidates(
        event, activity_contexts
    )
    provider = provider or _build_provider()
    candidates = deterministic_candidates
    provider_key = deterministic_provider or "deterministic-fallback"
    model_key = "server-rules" if deterministic_provider else "disabled"
    status = "DETERMINISTIC"
    uncertain = False
    semantic_reason = "Structured event mapping was sufficient."
    failure_code: str | None = None
    response_hash: str | None = None

    if not candidates and provider is not None:
        try:
            provider_result = provider.analyze(context)
            candidates = list(provider_result.response.candidates)
            provider_key = provider.provider_key
            model_key = provider.model_key
            status = "MODEL_INFERRED"
            uncertain = provider_result.response.uncertain
            semantic_reason = provider_result.response.semantic_reason
            response_hash = provider_result.response_hash
        except (ImpactProviderError, ValueError, TypeError) as exc:
            status = "FALLBACK"
            provider_key = getattr(provider, "provider_key", "unknown")
            model_key = getattr(provider, "model_key", "unknown")
            failure_code = type(exc).__name__.upper()
            semantic_reason = "AI impact inference was unavailable; deterministic flow continued."

    payload = _json_object(event.payload_json)
    validation = validate_impact_candidates(
        candidates=candidates,
        activities=activity_contexts,
        event_payload={
            **payload,
            # This is copied from the persisted Event row, never from model
            # output. It lets deterministic and model paths share the same
            # server-owned corroboration contract.
            "_event_affected_activity_id": event.affected_activity_id,
        },
    )
    if status == "MODEL_INFERRED" and validation.selected_activity_id is not None:
        final_status = {
            "AI_ONLY": "AI_ONLY_SELECTED",
            "CORROBORATED": "CORROBORATED_AI",
            "VALIDATED_IMPACT": "VALIDATED_AI",
        }.get(validation.selected_stage or "AI_ONLY", "AI_ONLY_SELECTED")
    elif status == "MODEL_INFERRED":
        final_status = "AI_REJECTED_FALLBACK"
    else:
        final_status = validation.status if validation.selected_activity_id else status

    stage_rank = {
        "AI_ONLY": 0,
        "STRUCTURALLY_ELIGIBLE": 1,
        "CORROBORATED": 2,
        "VALIDATED_IMPACT": 3,
    }
    maturity_stage = validation.selected_stage
    if maturity_stage is None and validation.candidates:
        maturity_stage = max(
            (item.corroboration_stage for item in validation.candidates),
            key=lambda item: stage_rank[item],
        )
    secondary_risk_activity_ids = tuple(
        item.activity_id
        for item in validation.candidates
        if item.actionable and item.activity_id != validation.selected_activity_id
    )
    return ImpactResolution(
        event_id=event.id,
        journey_id=journey.id,
        provider_key=provider_key,
        model_key=model_key,
        status=final_status,
        selected_activity_id=validation.selected_activity_id,
        validation=validation,
        uncertain=uncertain,
        semantic_reason=semantic_reason,
        failure_code=failure_code,
        response_hash=response_hash,
        maturity_stage=maturity_stage,
        selected_corroboration_refs=validation.selected_corroboration_refs,
        independent_evidence_count=validation.selected_independent_evidence_count,
        context=context,
        primary_recovery_target_id=validation.selected_activity_id,
        secondary_risk_activity_ids=secondary_risk_activity_ids,
    )


def persist_impact_assessment(session: Session, resolution: ImpactResolution) -> ImpactAssessment:
    validation = resolution.validation
    row = ImpactAssessment(
        id=str(uuid4()),
        event_id=resolution.event_id,
        journey_id=resolution.journey_id,
        provider_key=resolution.provider_key,
        model_key=resolution.model_key,
        status=resolution.status,
        maturity_stage=resolution.maturity_stage or "AI_ONLY",
        selected_corroboration_refs_json=json.dumps(
            list(resolution.selected_corroboration_refs), sort_keys=True
        ),
        independent_evidence_count=resolution.independent_evidence_count,
        uncertain=resolution.uncertain,
        selected_activity_id=resolution.selected_activity_id,
        primary_recovery_target_id=resolution.primary_recovery_target_id,
        secondary_risk_activity_ids_json=json.dumps(
            list(resolution.secondary_risk_activity_ids), sort_keys=True
        ),
        candidates_json=json.dumps(
            [
                {
                    "activity_id": item.activity_id,
                    "impact_type": item.impact_type,
                    "confidence": item.confidence,
                    "reason_codes": list(item.reason_codes),
                    "evidence_refs": list(item.evidence_refs),
                    "human_explanation": item.human_explanation,
                    "actionable": item.actionable,
                    "corroboration_stage": item.corroboration_stage,
                    "corroboration_refs": list(item.corroboration_refs),
                    "independent_evidence_count": item.independent_evidence_count,
                    "minimum_evidence_required": item.minimum_evidence_required,
                    "lifecycle_path": list(item.lifecycle_path),
                }
                for item in validation.candidates
            ],
            ensure_ascii=False,
            sort_keys=True,
        ),
        validated_activity_ids_json=json.dumps(
            [
                item.activity_id
                for item in validation.candidates
                if item.corroboration_stage == "VALIDATED_IMPACT" and item.actionable
            ],
            sort_keys=True,
        ),
        rejected_activity_ids_json=json.dumps(
            list(validation.rejected_activity_ids), sort_keys=True
        ),
        reason_codes_json=json.dumps(list(validation.reason_codes), sort_keys=True),
        semantic_reason=resolution.semantic_reason,
        failure_code=resolution.failure_code,
        response_hash=resolution.response_hash,
        created_at=datetime.now().astimezone().isoformat(),
    )
    session.add(row)
    return row


def impact_payload(row: ImpactAssessment) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "journey_id": row.journey_id,
        "provider_key": row.provider_key,
        "model_key": row.model_key,
        "status": row.status,
        "maturity_stage": row.maturity_stage,
        "selected_corroboration_refs": json.loads(row.selected_corroboration_refs_json or "[]"),
        "independent_evidence_count": int(row.independent_evidence_count or 0),
        "uncertain": bool(row.uncertain),
        "selected_activity_id": row.selected_activity_id,
        "primary_recovery_target_id": row.primary_recovery_target_id,
        "secondary_risk_activity_ids": json.loads(row.secondary_risk_activity_ids_json or "[]"),
        "candidates": json.loads(row.candidates_json or "[]"),
        "validated_activity_ids": json.loads(row.validated_activity_ids_json or "[]"),
        "rejected_activity_ids": json.loads(row.rejected_activity_ids_json or "[]"),
        "reason_codes": json.loads(row.reason_codes_json or "[]"),
        "semantic_reason": row.semantic_reason,
        "failure_code": row.failure_code,
        "created_at": row.created_at,
    }
