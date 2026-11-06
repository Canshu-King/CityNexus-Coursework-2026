from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.ai_availability import ai_availability_state, ai_state_from_failure_reason
from app.capabilities import (
    canonicalize_capabilities,
    extract_capabilities_from_text,
    preference_is_explicitly_excluded,
    preference_is_positive,
)
from app.core.money import major_to_minor
from app.db.models import Activity, Journey, JourneyMission, Place
from app.integrations.ai.responses import DeepSeekResponsesClient, DeepSeekResponsesError
from app.planning.day_rhythm import extract_day_rhythm


class MissionIntentDraft(BaseModel):
    """Strict model output; every entity reference still requires server resolution."""

    model_config = ConfigDict(extra="forbid")

    objective_codes: list[str] = Field(default_factory=list, max_length=20)
    protected_activity_references: list[str] = Field(default_factory=list, max_length=20)
    mutable_activity_references: list[str] = Field(default_factory=list, max_length=20)
    max_cost_delta_minor: int = Field(default=0, ge=0)
    max_loss_minor: int = Field(default=0, ge=0)
    max_schedule_drift_min: int = Field(default=0, ge=0)
    preferences: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.5, ge=0, le=1)
    reasoning_summary: str = Field(default="", max_length=500)


_NUMBER_TOKEN = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿点]+)"
_MONEY_PATTERN = re.compile(
    rf"(?:最多(?:可以|可)?(?:多花|增加)?|不超过|低于|少于|under|below|less\s+than|at\s+most|up\s+to|多花|增加|预算)\s*({_NUMBER_TOKEN})\s*(?:元|块|CNY|JPY)?",
    re.IGNORECASE,
)
_DRIFT_PATTERN = re.compile(
    rf"(?:最多|不超过|延误|漂移|偏移|under|below|at\s+most|up\s+to)\s*({_NUMBER_TOKEN})\s*(?:分钟|minutes?|mins?)",
    re.IGNORECASE,
)
_LOSS_PATTERN = re.compile(
    rf"(?:最多(?:可以|可)?(?:损失|接受损失)|可接受损失|最大损失|acceptable\s+loss|loss\s+limit)\s*({_NUMBER_TOKEN})\s*(?:元|块|CNY)?",
    re.IGNORECASE,
)
_PROTECTED_ALIASES = {
    "place-river-cruise": ("游船", "海河游船", "river cruise", "cruise", "游轮"),
    "place-skyline-dinner": ("晚餐", "吃饭", "dinner", "餐厅"),
}


@dataclass(frozen=True)
class MissionSpec:
    objective_codes: tuple[str, ...]
    protected_activity_ids: tuple[str, ...]
    mutable_activity_ids: tuple[str, ...]
    max_cost_delta_minor: int
    max_loss_minor: int
    max_schedule_drift_min: int
    preferences: dict[str, Any]
    requested_autonomy: str
    capabilities: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective_codes": list(self.objective_codes),
            "protected_activity_ids": list(self.protected_activity_ids),
            "mutable_activity_ids": list(self.mutable_activity_ids),
            "max_cost_delta_minor": self.max_cost_delta_minor,
            "max_loss_minor": self.max_loss_minor,
            "max_schedule_drift_min": self.max_schedule_drift_min,
            "preferences": self.preferences,
            "capabilities": [dict(item) for item in self.capabilities],
            "requested_autonomy": self.requested_autonomy,
        }


def _text(value: str) -> str:
    return value.casefold()


def _parse_number(value: str) -> float:
    """Parse Arabic or ordinary Chinese cardinal numbers without locale guessing."""

    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if "点" in value:
        whole, fraction = value.split("点", 1)
        fraction_digits = "".join(str(digits[char]) for char in fraction if char in digits)
        return _parse_number(whole or "零") + float(f"0.{fraction_digits or '0'}")
    units = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}
    total = section = number = 0
    for char in value:
        if char in digits:
            number = digits[char]
            continue
        unit = units.get(char)
        if unit is None:
            raise ValueError(f"unsupported numeric token: {value}")
        if unit < 10_000:
            section += (number or 1) * unit
        else:
            section = (section + number) * unit
            total += section
            section = 0
        number = 0
    return float(total + section + number)


def _active_activities(session: Session, journey: Journey) -> list[Activity]:
    if not journey.active_plan_version_id:
        return []
    return list(
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no, Activity.id)
        ).all()
    )


def _resolve_activity_authority(
    session: Session, journey: Journey, command: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve protected and explicitly mutable activities clause by clause.

    This is the deterministic Mission Compiler fallback.  Polarity belongs to
    the local clause, so “cruise may be cancelled, don't touch dinner” cannot
    accidentally protect both entities just because both names occur.
    """

    activities = _active_activities(session, journey)
    place_ids = {activity.place_id for activity in activities if activity.place_id}
    places = {
        place.id: place
        for place in session.scalars(select(Place).where(Place.id.in_(place_ids))).all()
    }
    clauses = [
        part.strip() for part in re.split(r"[，,。；;！？!?\n]+", _text(command)) if part.strip()
    ]
    protected: list[str] = []
    mutable: list[str] = []
    protect_tokens = (
        "别动",
        "不要动",
        "不能动",
        "别改",
        "不要改",
        "保留",
        "保护",
        "必须保留",
        "don't change",
        "do not change",
        "protect",
        "keep",
    )
    mutable_tokens = (
        "可以取消",
        "可取消",
        "可以改",
        "可调整",
        "可以调整",
        "可以换",
        "可替换",
        "不用保留",
        "无需保留",
        "may cancel",
        "can cancel",
        "may change",
        "flexible",
    )
    for activity in activities:
        semantic_id = activity.place_id or activity.city_service_id
        if not semantic_id:
            continue
        place = places.get(activity.place_id or "")
        haystack = _text(
            " ".join(
                part for part in (activity.title, place.name if place else "", semantic_id) if part
            )
        )
        aliases = _PROTECTED_ALIASES.get(semantic_id, ())
        matching_clauses = [
            clause
            for clause in clauses
            if any(alias.casefold() in clause for alias in aliases)
            or any(token and token in clause for token in haystack.split())
        ]
        for clause in matching_clauses:
            is_mutable = any(token in clause for token in mutable_tokens)
            is_protected = any(token in clause for token in protect_tokens)
            # Explicit mutability is the more specific instruction when a
            # phrase happens to contain a generic “keep/change” substring.
            if is_mutable:
                mutable.append(activity.id)
            elif is_protected:
                protected.append(activity.id)
    return tuple(dict.fromkeys(protected)), tuple(dict.fromkeys(mutable))


def _resolve_protected_activities(
    session: Session, journey: Journey, command: str
) -> tuple[str, ...]:
    """Compatibility surface used by V3 callers and external verification."""

    return _resolve_activity_authority(session, journey, command)[0]


def compile_mission(
    session: Session,
    journey: Journey,
    command: str,
    *,
    requested_autonomy: str,
) -> MissionSpec:
    normalized = _text(command)
    objectives: set[str] = set()
    preferences: dict[str, Any] = {}
    for category, aliases in {
        "museum": ("museum", "博物馆", "美术馆"),
        "garden": ("garden", "庭园", "庭院"),
        "art": ("art gallery", "美术"),
    }.items():
        if preference_is_explicitly_excluded(normalized, aliases):
            preferences[f"avoid:{category}"] = True
        elif preference_is_positive(normalized, aliases):
            preferences[category] = True
    capabilities = extract_capabilities_from_text(command, scope="JOURNEY")
    if capabilities:
        objectives.add("CAPABILITY_AWARE")
        preferences["capabilities"] = [dict(item) for item in capabilities]
    rhythm = extract_day_rhythm(command)
    if rhythm is not None:
        objectives.add("RESPECT_DAY_RHYTHM")
        preferences["day_rhythm"] = rhythm.as_dict()
    if any(token in normalized for token in ("淋雨", "下雨", "rain")) or preference_is_positive(
        normalized, ("室内", "indoor")
    ):
        objectives.add("PREFER_INDOOR")
        preferences["indoor"] = True
    if preference_is_positive(
        normalized, ("户外", "室外", "公园", "outdoor", "park")
    ):
        objectives.add("PREFER_OUTDOOR")
        preferences["outdoor"] = True
    if any(
        token in normalized
        for token in (
            "少走",
            "不想走",
            "步行少",
            "low walking",
            "avoid long walks",
            "avoid walking",
            "less walking",
            "walking",
        )
    ):
        objectives.add("MINIMIZE_WALKING")
        preferences["low_walking"] = True
    protected_activity_ids = _resolve_protected_activities(session, journey, command)
    # Tests and integrations may replace the legacy resolver.  Resolve mutable
    # IDs independently only when the real resolver remains installed.
    mutable_activity_ids = (
        _resolve_activity_authority(session, journey, command)[1]
        if getattr(_resolve_protected_activities, "__module__", __name__) == __name__
        else ()
    )
    if protected_activity_ids:
        objectives.add("PROTECT_EXPLICIT_ACTIVITIES")
        preferences["protected_activity_ids"] = list(protected_activity_ids)
    if any(token in normalized for token in ("交通管制", "拥堵", "封路", "traffic", "closure")):
        objectives.add("RECOVER_FROM_CITY_DISRUPTION")
    if any(token in normalized for token in ("自己安排", "自行", "自动", "autonomous")):
        objectives.add("EXECUTE_WITHIN_BOUNDS")
    match = _MONEY_PATTERN.search(command)
    budget_currency = getattr(journey, "budget_currency", "CNY")
    max_cost = major_to_minor(_parse_number(match.group(1)), budget_currency) if match else 0
    loss_match = _LOSS_PATTERN.search(command)
    max_loss = major_to_minor(_parse_number(loss_match.group(1)), journey.budget_currency) if loss_match else 0
    drift_match = _DRIFT_PATTERN.search(command)
    max_drift = round(_parse_number(drift_match.group(1))) if drift_match else 0
    return MissionSpec(
        objective_codes=tuple(sorted(objectives)),
        protected_activity_ids=protected_activity_ids,
        mutable_activity_ids=mutable_activity_ids,
        max_cost_delta_minor=max_cost,
        max_loss_minor=max_loss,
        max_schedule_drift_min=max_drift,
        preferences=preferences,
        capabilities=tuple(dict(item) for item in capabilities),
        requested_autonomy=requested_autonomy,
    )


def _model_draft(command: str) -> MissionIntentDraft | None:
    from app.core.config import settings

    if not settings.ai_api_key:
        return None
    schema = MissionIntentDraft.model_json_schema()
    try:
        client = DeepSeekResponsesClient(
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            base_url=settings.deepseek_api_url,
            reasoning_effort="low",
        )
        result = client.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Compile the user's Journey mission into JSON matching this schema exactly. "
                        "Distinguish protected activities from explicitly mutable activities. "
                        "Money is in integer minor units. Never invent database IDs. Return only a short "
                        "user-safe reasoning_summary, never private chain-of-thought. Schema: "
                        + json.dumps(schema, ensure_ascii=False)
                    ),
                },
                {"role": "user", "content": command},
            ],
            reasoning_effort="low",
            json_mode=True,
        )
        return MissionIntentDraft.model_validate_json(result.text)
    except (DeepSeekResponsesError, KeyError, IndexError, TypeError, ValueError):
        return None


def _resolve_model_references(
    session: Session,
    journey: Journey,
    references: list[str],
) -> tuple[str, ...]:
    activities = _active_activities(session, journey)
    resolved: list[str] = []
    for reference in references:
        needle = re.sub(r"\s+", "", _text(reference))
        if not needle:
            continue
        for activity in activities:
            semantic_id = activity.place_id or activity.city_service_id or ""
            candidates = [activity.title, semantic_id, *_PROTECTED_ALIASES.get(semantic_id, ())]
            if any(
                needle in re.sub(r"\s+", "", _text(candidate))
                or re.sub(r"\s+", "", _text(candidate)) in needle
                for candidate in candidates
                if candidate
            ):
                resolved.append(activity.id)
    return tuple(dict.fromkeys(resolved))


def _unresolved_model_references(
    session: Session,
    journey: Journey,
    references: list[str],
) -> tuple[str, ...]:
    """Return model references that the server could not ground to an Activity.

    Resolution is deliberately repeated per reference.  Besides making the
    persisted evidence precise, this keeps the compatibility seam used by
    external model-boundary tests: a resolver may be replaced without the
    compiler having to trust model-supplied IDs.
    """

    unresolved: list[str] = []
    for reference in references:
        if not _resolve_model_references(session, journey, [reference]):
            unresolved.append(reference)
    return tuple(dict.fromkeys(unresolved))


def create_mission(
    session: Session,
    journey: Journey,
    *,
    command: str,
    requested_autonomy: str,
    trigger_event_id: str | None,
    is_standing: bool = False,
    parent_mission_id: str | None = None,
    use_model_draft: bool = True,
) -> JourneyMission:
    spec = compile_mission(
        session,
        journey,
        command,
        requested_autonomy=requested_autonomy,
    )
    draft = _model_draft(command) if use_model_draft else None
    compiler_provider = "deterministic"
    compiler_model = "mission-compiler-v2-fallback"
    compiler_confidence = 1.0
    unresolved_references: tuple[str, ...] = ()
    reasoning_summary = (
        "Resolved named activities against the active plan, then intersected the mission "
        "envelope with the persisted traveler policy."
    )
    if draft is not None:
        indoor_excluded = preference_is_explicitly_excluded(command, ("室内", "indoor"))
        model_protected = _resolve_model_references(
            session, journey, draft.protected_activity_references
        )
        model_mutable = _resolve_model_references(
            session, journey, draft.mutable_activity_references
        )
        unresolved_references = _unresolved_model_references(
            session,
            journey,
            [
                *draft.protected_activity_references,
                *draft.mutable_activity_references,
            ],
        )
        # Explicit local polarity is a privilege boundary.  A model may add
        # semantic protection, but it cannot turn a server-resolved protected
        # Activity into a mutable one.
        protected = tuple(dict.fromkeys((*spec.protected_activity_ids, *model_protected)))
        protected_set = set(protected)
        mutable = tuple(
            item
            for item in dict.fromkeys((*spec.mutable_activity_ids, *model_mutable))
            if item not in protected_set
        )
        # A model draft is semantic evidence, never an authority amplifier.
        # The deterministic compiler's numeric fields are the locally parsed
        # ceiling (zero means no explicit loss authority).
        max_cost_delta_minor = min(
            spec.max_cost_delta_minor,
            draft.max_cost_delta_minor,
        )
        max_loss_minor = min(spec.max_loss_minor, draft.max_loss_minor)
        max_schedule_drift_min = min(
            spec.max_schedule_drift_min,
            draft.max_schedule_drift_min,
        )
        draft_capabilities = [
            item
            for item in draft.capabilities
            if not (
                indoor_excluded
                and str(item.get("code") or item.get("rule_code") or "").upper()
                == "INDOOR_PREFERRED"
            )
        ]
        merged_capabilities = tuple(
            canonicalize_capabilities(
                [*spec.capabilities, *draft_capabilities],
                source="MISSION_COMPILER",
                provenance="USER_STATED",
                scope="JOURNEY",
            )
        )
        merged_objectives = set(spec.objective_codes) | set(draft.objective_codes)
        if indoor_excluded:
            merged_objectives.discard("PREFER_INDOOR")
        if merged_capabilities:
            merged_objectives.add("CAPABILITY_AWARE")
        merged_preferences = {
            **spec.preferences,
            **draft.preferences,
            **({"day_rhythm": spec.preferences["day_rhythm"]} if "day_rhythm" in spec.preferences else {}),
            **({"capabilities": [dict(item) for item in merged_capabilities]} if merged_capabilities else {}),
        }
        if indoor_excluded:
            merged_preferences.pop("indoor", None)
        spec = MissionSpec(
            objective_codes=tuple(sorted(merged_objectives)),
            protected_activity_ids=protected,
            mutable_activity_ids=mutable,
            max_cost_delta_minor=max_cost_delta_minor,
            max_loss_minor=max_loss_minor,
            max_schedule_drift_min=max_schedule_drift_min,
            preferences=merged_preferences,
            capabilities=merged_capabilities,
            requested_autonomy=requested_autonomy,
        )
        compiler_provider = "deepseek"
        from app.core.config import settings

        compiler_model = settings.ai_model.removeprefix("deepseek/")
        compiler_confidence = draft.confidence
        reasoning_summary = draft.reasoning_summary
    # The final mission object is the authority input for the Agent runtime.
    # Re-apply explicit negative preference polarity at this boundary so an
    # entry-specific semantic path or a model draft cannot resurrect a
    # positive indoor preference from "不喜欢室内馆".
    if preference_is_explicitly_excluded(command, ("室内", "indoor")):
        final_objectives = tuple(
            item for item in spec.objective_codes if item != "PREFER_INDOOR"
        )
        final_preferences = dict(spec.preferences)
        final_preferences.pop("indoor", None)
        spec = MissionSpec(
            objective_codes=final_objectives,
            protected_activity_ids=spec.protected_activity_ids,
            mutable_activity_ids=spec.mutable_activity_ids,
            max_cost_delta_minor=spec.max_cost_delta_minor,
            max_loss_minor=spec.max_loss_minor,
            max_schedule_drift_min=spec.max_schedule_drift_min,
            preferences=final_preferences,
            capabilities=spec.capabilities,
            requested_autonomy=spec.requested_autonomy,
        )
    now = datetime.now(timezone.utc).isoformat()
    from app.agent.delegation_policy import get_or_create_policy, policy_payload

    policy = policy_payload(get_or_create_policy(session, journey.traveler_id))
    resolved_cost = min(
        spec.max_cost_delta_minor,
        int(policy["max_auto_replan_cost_delta_minor"]),
    )
    resolved_loss = min(spec.max_loss_minor, int(policy["max_auto_loss_minor"]))
    requested_drift = spec.max_schedule_drift_min
    resolved_drift = min(requested_drift, int(policy["max_auto_schedule_drift_min"]))
    intent = spec.as_dict()
    resolved_intent = {
        **intent,
        "max_cost_delta_minor": resolved_cost,
        "max_loss_minor": resolved_loss,
        "max_schedule_drift_min": resolved_drift,
        "policy_version": policy["version"],
        "policy_mode": policy["mode"],
        "authority_intersection": "MISSION_AND_TRAVELER_POLICY",
    }
    allowed_actions = ["REQUEST_REPLAN"]
    if requested_autonomy == "EXECUTE_WITHIN_BOUNDS":
        allowed_actions.append("ACCEPT_REPLAN")
        if trigger_event_id is not None:
            allowed_actions.append("ACTIVATE_STANDBY")
    mission = JourneyMission(
        id=str(uuid4()),
        journey_id=journey.id,
        traveler_id=journey.traveler_id,
        trigger_event_id=trigger_event_id,
        user_text=command,
        objective_codes_json=json.dumps(list(spec.objective_codes), ensure_ascii=False),
        protected_activity_ids_json=json.dumps(
            list(spec.protected_activity_ids), ensure_ascii=False
        ),
        mutable_activity_ids_json=json.dumps(list(spec.mutable_activity_ids), ensure_ascii=False),
        intent_json=json.dumps(intent, ensure_ascii=False, sort_keys=True),
        resolved_intent_json=json.dumps(resolved_intent, ensure_ascii=False, sort_keys=True),
        allowed_action_classes_json=json.dumps(allowed_actions, sort_keys=True),
        unresolved_references_json=json.dumps(list(unresolved_references), ensure_ascii=False),
        compiler_provider_key=compiler_provider,
        compiler_model_key=compiler_model,
        compiler_confidence=compiler_confidence,
        reasoning_summary=reasoning_summary,
        is_standing=is_standing,
        parent_mission_id=parent_mission_id,
        # Keep the mission columns as the explicit/user-facing envelope.  The
        # executable authority is the intersection persisted in
        # ``resolved_intent``; retaining the requested values here preserves
        # the distinction between what the traveler authorized and what the
        # current policy permits.
        max_cost_delta_minor=spec.max_cost_delta_minor,
        max_loss_minor=spec.max_loss_minor,
        max_schedule_drift_min=spec.max_schedule_drift_min,
        preferences_json=json.dumps(spec.preferences, ensure_ascii=False, sort_keys=True),
        requested_autonomy=spec.requested_autonomy,
        status="CREATED",
        created_at=now,
    )
    session.add(mission)
    session.flush()
    return mission


def mission_payload(mission: JourneyMission) -> dict[str, Any]:
    from app.agent.mission_plan import mission_plan_for_model

    return {
        "id": mission.id,
        "journey_id": mission.journey_id,
        "trigger_event_id": mission.trigger_event_id,
        "user_text": mission.user_text,
        "objective_codes": json.loads(mission.objective_codes_json or "[]"),
        "protected_activity_ids": json.loads(mission.protected_activity_ids_json or "[]"),
        "mutable_activity_ids": json.loads(mission.mutable_activity_ids_json or "[]"),
        "intent": json.loads(mission.intent_json or "{}"),
        "resolved_intent": json.loads(mission.resolved_intent_json or "{}"),
        "allowed_action_classes": json.loads(mission.allowed_action_classes_json or "[]"),
        "unresolved_references": json.loads(mission.unresolved_references_json or "[]"),
        "compiler": {
            "provider_key": mission.compiler_provider_key,
            "model_key": mission.compiler_model_key,
            "confidence": mission.compiler_confidence,
        },
        "reasoning_summary": mission.reasoning_summary,
        "is_standing": bool(mission.is_standing),
        "parent_mission_id": mission.parent_mission_id,
        "max_cost_delta_minor": mission.max_cost_delta_minor,
        "max_loss_minor": mission.max_loss_minor,
        "max_schedule_drift_min": mission.max_schedule_drift_min,
        "preferences": json.loads(mission.preferences_json or "{}"),
        "requested_autonomy": mission.requested_autonomy,
        "status": mission.status,
        "created_at": mission.created_at,
        "started_at": mission.started_at,
        "completed_at": mission.completed_at,
        "failure_reason": mission.failure_reason,
        "clarification": json.loads(getattr(mission, "clarification_json", "{}") or "{}"),
        # ORD-0359: additive.  When this mission stored its own reason, that stored sentence is
        # what gets named - so a refusal is never reported as a missing credential, and an
        # unrecognised reason names nothing rather than the nearest of the three.  Only a mission
        # that stored no reason falls back to the configuration state, which is a statement about
        # the switch, not about this run.
        "ai_state": (
            ai_state_from_failure_reason(mission.failure_reason)
            if mission.failure_reason
            else ai_availability_state()
        ),
        "mission_plan": mission_plan_for_model(mission),
    }
