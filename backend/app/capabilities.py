"""Canonical traveler capability and accessibility contract.

Capability interpretation is intentionally small and deterministic.  DeepSeek
may suggest items, but this module owns the vocabulary, severity defaults and
the conversion of explicit capability state into the existing Journey
constraint shape consumed by the feasibility gate.

The module is deliberately independent of FastAPI/SQLAlchemy so it can be
used by semantic compilation, planning and focused contract tests alike.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


CAPABILITY_CODES = (
    "STEP_FREE_REQUIRED",
    "AVOID_STAIRS",
    "MAX_WALKING_PER_LEG_M",
    "MAX_CONTINUOUS_WALKING_MIN",
    "MAX_DAILY_WALKING_M",
    "MIDDAY_REST_WINDOW",
    "REST_EVERY_MIN",
    "INFANT_NAP_WINDOW",
    "TOILET_BREAK_EVERY_MIN",
    "STROLLER",
    "LARGE_LUGGAGE",
    "SLOW_PACE",
    "QUIET_ENVIRONMENT",
    "INDOOR_PREFERRED",
    "SENSORY_LOW_STIMULATION",
)

CAPABILITY_CLASSES = (
    "HARD_ACCESSIBILITY_NEED",
    "CARE_REQUIREMENT",
    "PHYSICAL_LIMIT",
    "TRIP_CONTEXT",
    "SOFT_PREFERENCE",
)
CAPABILITY_SEVERITIES = ("HARD", "STRONG", "SOFT")
CAPABILITY_SCOPES = ("TRAVELER", "JOURNEY")
ACCESSIBILITY_STATES = (
    "ACCESSIBILITY_VERIFIED",
    "ACCESSIBILITY_UNVERIFIED",
    "ACCESSIBILITY_CONFLICT",
    "NEEDS_LIVE_EVIDENCE",
)

HARD_ACCESSIBILITY_CODES = frozenset({"STEP_FREE_REQUIRED", "AVOID_STAIRS"})
CARE_CODES = frozenset(
    {
        "MIDDAY_REST_WINDOW",
        "REST_EVERY_MIN",
        "INFANT_NAP_WINDOW",
        "TOILET_BREAK_EVERY_MIN",
    }
)
PHYSICAL_LIMIT_CODES = frozenset(
    {"MAX_WALKING_PER_LEG_M", "MAX_CONTINUOUS_WALKING_MIN", "MAX_DAILY_WALKING_M"}
)
CONTEXT_CODES = frozenset({"STROLLER", "LARGE_LUGGAGE"})
SOFT_CODES = frozenset(
    {"SLOW_PACE", "QUIET_ENVIRONMENT", "INDOOR_PREFERRED", "SENSORY_LOW_STIMULATION"}
)


def hard_executable_capability_codes() -> frozenset[str]:
    """The capability codes a `HARD` write may actually be held to.

    **Derived, never re-typed**: the vocabulary minus the four soft preferences.  There is
    deliberately no fourth hand-written list here - the moment one exists, the write layer and the
    gate can drift apart and the drift is invisible.  The equality against the feasibility gate's own
    set (`_CAPABILITY_GATE_CODES`) is pinned by a door that imports both sides
    (`test_qoder_ord0219_capability_write_vocabulary.py`), because importing `feasibility` here would
    invert the dependency: `feasibility` imports this module.
    """

    return frozenset(CAPABILITY_CODES) - SOFT_CODES


def capability_write_refusal(code: object, severity: object) -> str | None:
    """Two questions, in order, about a capability a client wants to store.

    1. is `code` one the product has a meaning for at all             -> `UNKNOWN_RULE_CODE`
    2. if `severity` is `HARD`, can the gate hold the plan to it     -> `HARD_RULE_NOT_EXECUTABLE`

    The names stay separate because the fixes are different: "that rule does not exist" versus
    "that rule exists but is a preference, so declaring it hard would later fail the whole plan as
    `UNSUPPORTED_HARD_CONSTRAINT`" (HARNESS 22:31: "只补 severity 会把『静默不执行』变成
    『整趟旅程不可规划』").  `STRONG` is checked like `SOFT` on purpose:
    `capability_constraints` (:911) forwards only `HARD` items to the gate, so a `STRONG`
    preference is already non-gating and must not be refused.
    """

    normalized = str(code or "").strip().upper()
    if normalized not in CAPABILITY_CODES:
        return "UNKNOWN_RULE_CODE"
    if str(severity or "").strip().upper() == "HARD" and normalized not in hard_executable_capability_codes():
        return "HARD_RULE_NOT_EXECUTABLE"
    return None

_HARD_MARKERS = (
    "必须",
    "务必",
    "不能",
    "无法",
    "需要",
    "要求",
    "全程",
    "must",
    "cannot",
    "can't",
    "need",
    "required",
    "require",
    "no stairs",
    "step-free",
    "wheelchair",
    "最多走",
    "最多步行",
    "不超过",
    "不能超过",
)
# Denial / history markers.  These deliberately do NOT include hedges
# ("可能", "maybe"): a hedged first-person request is still a request.  A hedged
# need is downgraded to STRONG + needs_confirmation, never silently erased.
_NEGATED_MARKERS = (
    "没有",
    "不需要",
    "不用",
    "无需",
    "不是",
    "以前",
    "曾经",
    "过去",
    "历史",
    "not wheelchair",
    "no wheelchair need",
    "used to",
    "historically",
    "isn't wheelchair",
    "aren't wheelchair",
    "do not need",
    "don't need",
    "does not need",
    "doesn't need",
    "不是我",
    "不是我要",
)
# A pure hypothetical is never a commitment.
_SPECULATIVE_MARKERS = ("说不定", "假如", "万一", "hypothetically", "if i end up")
# A future/hypothetical state the traveller describes but does not commit to.
_FUTURE_HYPOTHETICAL_MARKERS = (
    "说不定",
    "到时候",
    "万一",
    "if i end up",
    "i'll tell you",
    "i will tell you",
    "if i get",
    "if i had",
    "if i were",
    "if i'm in",
    "if i am in",
    "if i become",
    "if that happens",
    "should i end up",
)
# Politeness / request hedges.  These weaken the *request*, not the need, so the
# phrase is removed and the need is still surfaced (as a candidate).
_REQUEST_HEDGE_PHRASES = (
    "如果可能的话",
    "如果可以的话",
    "如果方便的话",
    "if possible",
    "if you can",
    "if you're able",
    "if at all possible",
)
# A conditional premise combined with a hedged need is a hypothetical, not a
# present requirement.
_CONDITIONAL_MARKERS = ("如果", "假如", "要是", "if ", "when i ", "when ")
# Predicates that actually assert a need or a limitation.  A hedge without one
# of these is a preference ("maybe I should avoid stairs"), not a stated need.
_NEED_ASSERTION_TOKENS = (
    "需要",
    "要坐",
    "要走",
    "要休息",
    "要午休",
    "不能",
    "无法",
    "走不了",
    "走不动",
    "走不远",
    "坐轮椅",
    "用轮椅",
    "必须",
    "务必",
    "必",
    "需",
    "rest",
    "nap",
    "need",
    "require",
    "cannot",
    "can't",
    "can not",
    "must",
    "wheelchair user",
    "walk far",
    "step-free",
    "无障碍",
)
_WEAK_MODAL_MARKERS = ("should", "would", "ought", "prefer", "最好", "应该")
# Trip-context preferences (a stroller, luggage, a slow pace) are still useful
# planning input even inside a conditional or hedged sentence.  They are never
# HARD accessibility needs, so they may be surfaced conservatively.
_CONTEXT_PREFERENCE_TOKENS = (
    "婴儿车",
    "推车",
    "儿童车",
    "stroller",
    "buggy",
    "pram",
    "大箱子",
    "行李箱",
    "行李",
    "luggage",
    "suitcase",
    "慢一点",
    "慢速",
    "slow pace",
    "take it slowly",
    "安静",
    "quiet",
    "室内",
    "indoor",
)
# A hedge weakens a claim; it never deletes it.
_HEDGE_MARKERS = (
    "如果",
    "可能",
    "也许",
    "或许",
    "大概",
    "maybe",
    "might",
    "possibly",
    "perhaps",
    "if ",
    "hopefully",
    "supposedly",
)
# Hearsay is someone else's assessment, not the traveller's own need.
_HEARSAY_MARKERS = (
    "据说",
    "传闻",
    "听说",
    "朋友说",
    "同事说",
    "a friend said",
    "my colleague said",
    "i heard",
    "they say",
    "allegedly",
)
# A travelling companion is not an unrelated third party.  Companion needs must
# never silently become the traveller's HARD need, and must never be dropped.
_COMPANION_MARKERS = (
    "我妈妈",
    "我父亲",
    "我父母",
    "我太太",
    "我妻子",
    "我老公",
    "我丈夫",
    "我孩子",
    "我女儿",
    "我儿子",
    "my mother",
    "my father",
    "my parent",
    "my wife",
    "my husband",
    "my child",
    "my son",
    "my daughter",
    "my partner",
    "my companion",
)
_UNRELATED_THIRD_PARTY_MARKERS = (
    "朋友说",
    "同事说",
    "我朋友",
    "我同事",
    "a friend said",
    "a colleague said",
    "my friend",
    "my colleague",
    "someone else",
    "another person",
    "路人",
)
_THIRD_PARTY_MARKERS = _UNRELATED_THIRD_PARTY_MARKERS + _COMPANION_MARKERS
# Only an explicit exclusion of the traveller makes a clause truly third-party.
_THIRD_PARTY_EXCLUSION_MARKERS = (
    "not me",
    "不是我",
    "不是我要",
    "我不需要",
    "我不坐",
    "i don't need",
    "i do not need",
    "i'm not",
    "i am not",
    "not for me",
)
_FIRST_PERSON_NEED_MARKERS = (
    "我",
    "本人",
    "i need",
    "i want",
    "i require",
    "i cannot",
    "i can't",
    "i can not",
    "i'd like",
    "i would like",
    "for me",
)
_FACILITY_SUBJECT_MARKERS = ("车站", "机场", "酒店", "餐厅", "景点", "博物馆", "station", "airport", "hotel", "restaurant", "museum", "venue", "place")
_FIRST_PERSON_MARKERS = ("我", "本人", "i ", "i'm", "i am", "me", "my ")
_PERMANENCE_MARKERS = ("终身", "长期", "永久", "一辈子", "一直", "每次旅行", "以后每次", "不只是这次", "permanent", "permanently", "every trip", "every journey", "always", "not just this trip")
_THIS_TRIP_MARKERS = ("这次", "本次", "今天", "此次旅行", "this trip", "this journey", "just for today")

# Units are semantic, not cosmetic: a duration is never a distance cap and a
# daily walking budget is never a per-leg cap.
_DISTANCE_UNIT_TOKENS = ("公里", "千米", "km", "kilomet", "米", "公尺", "metre", "meter")
_DURATION_UNIT_TOKENS = ("分钟", "分鐘", "min", "hour", "小时", "小時", "hr")
_DAILY_SCOPE_TOKENS = ("每天", "每日", "daily", "per day", "a day", "each day")
_PER_LEG_SCOPE_TOKENS = ("每段", "每程", "每次", "每腿", "单段", "每一段", "per leg", "each leg")
# Below this a walking bound is not a usable physical limit: ask instead of
# persisting a clean authoritative cap.
_MOBILITY_PLAUSIBILITY_FLOOR_M = 1

HEDGED_NEED_POLICY = "STRONG_NEEDS_CONFIRMATION_CANDIDATE_ONLY"


@dataclass(frozen=True)
class CapabilityItem:
    """A validated, serialisable capability item."""

    code: str
    capability_class: str
    severity: str
    params: Mapping[str, Any]
    scope: str = "JOURNEY"
    source: str = "SEMANTIC"
    provenance: str = "USER_STATED"
    explicit: bool = True
    active: bool = True
    confirmed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "class": self.capability_class,
            "severity": self.severity,
            "params": dict(self.params),
            "scope": self.scope,
            "source": self.source,
            "provenance": self.provenance,
            "explicit": self.explicit,
            "active": self.active,
            "confirmed": self.confirmed,
        }


def capability_class_for(code: str) -> str:
    code = str(code).upper()
    if code in HARD_ACCESSIBILITY_CODES:
        return "HARD_ACCESSIBILITY_NEED"
    if code in CARE_CODES:
        return "CARE_REQUIREMENT"
    if code in PHYSICAL_LIMIT_CODES:
        return "PHYSICAL_LIMIT"
    if code in CONTEXT_CODES:
        return "TRIP_CONTEXT"
    return "SOFT_PREFERENCE"


def default_severity(code: str, *, hard_asserted: bool = False) -> str:
    code = str(code).upper()
    if code in HARD_ACCESSIBILITY_CODES or code in PHYSICAL_LIMIT_CODES:
        return "HARD" if hard_asserted else "STRONG"
    if code in CARE_CODES:
        return "HARD" if hard_asserted else "STRONG"
    if code in CONTEXT_CODES:
        return "STRONG"
    return "SOFT"


def _half_up_int(value: float) -> int:
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def _bounded_number(value: Any, *, minimum: int = 0, maximum: int = 100_000) -> int | None:
    try:
        # Round half-up rather than truncate: a sub-metre bound such as 0.5 m is
        # a real (if unusual) distance, and truncation would silently turn it
        # into zero - a physically impossible walking budget.  Python's builtin
        # round() is banker's rounding (0.5 -> 0), so the intent must be explicit.
        number = _half_up_int(float(value))
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def _time_window(value: str) -> tuple[str, str] | None:
    matches = re.findall(r"(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]?\d", value)
    if len(matches) < 2:
        # Chinese/English hour words ("12 点到 14 点", "1 点到 3 点", "13-15 时").
        matches = re.findall(
            r"([01]?\d|2[0-3])\s*(?:点|時|时|:|：)", value
        ) or re.findall(
            r"\b([01]?\d|2[0-3])\s*(?:h|hours?|o'clock)\b", value, re.I
        )
    if len(matches) < 2:
        return None

    def normalise(item: str) -> str:
        raw = item.replace("：", ":").replace("点", ":00").replace("時", ":00").replace("时", ":00")
        if ":" not in raw:
            raw = f"{raw}:00"
        hour, minute = raw.split(":", 1)
        return f"{int(hour):02d}:{int(minute or 0):02d}"

    start, end = normalise(matches[0]), normalise(matches[1])
    sh, sm = (int(part) for part in start.split(":")); eh, em = (int(part) for part in end.split(":"))
    return (start, end) if eh * 60 + em > sh * 60 + sm else None


_QUANTITY_UNIT = (
    r"(?:公里|千米|km|kilomet(?:er|re)s?|米|公尺|m|分钟|分鐘|min|minutes?|hours?|hrs?)"
)


def _unit_kind(unit: str | None) -> str | None:
    """Classify a parsed unit.  A duration is never a distance."""

    value = (unit or "").casefold().strip()
    if not value:
        return None
    if value in {"分钟", "分鐘", "min", "minute", "minutes", "hour", "hours", "hr", "hrs"}:
        return "duration"
    if value in {
        "公里",
        "千米",
        "km",
        "kilometer",
        "kilometers",
        "kilometre",
        "kilometres",
        "米",
        "公尺",
        "m",
        "metre",
        "metres",
        "meter",
        "meters",
    }:
        return "distance"
    return None


def _token_present(value: str, tokens: Iterable[str]) -> bool:
    return any(token in value for token in tokens)


def _quantity_before(value: str, terms: Iterable[str]) -> tuple[int | None, str | None]:
    """Parse ``<term> ... <number> <unit>`` and return (value_in_metres, unit).

    The unit is returned verbatim so callers enforce unit semantics instead of
    silently accepting a duration as a distance.  ``value`` is already
    distance-normalised (kilometres -> metres); use ``_unit_kind`` to reject a
    duration before treating it as a distance.
    """

    pattern = (
        r"(?:"
        + "|".join(re.escape(term) for term in terms)
        + r").{0,18}?(-?[0-9]{1,8}(?:\.[0-9]+)?)\s*("
        + _QUANTITY_UNIT
        + r")?"
    )
    match = re.search(pattern, value, re.I)
    if not match:
        return None, None
    raw_number = float(match.group(1))
    unit = (match.group(2) or "").casefold() or None
    if raw_number < 0:
        return None, unit
    if unit in {
        "公里",
        "千米",
        "km",
        "kilometer",
        "kilometers",
        "kilometre",
        "kilometres",
    }:
        raw_number *= 1000
    return _bounded_number(raw_number), unit


def _number_before(value: str, terms: Iterable[str]) -> int | None:
    """Backwards-compatible numeric lookup (distance-normalised)."""

    number, _unit = _quantity_before(value, terms)
    return number


def _hard_asserted(value: str) -> bool:
    return any(marker in value for marker in _HARD_MARKERS)


def extract_capabilities_from_text(
    text: str,
    *,
    scope: str = "JOURNEY",
    source: str = "SEMANTIC",
    provenance: str = "USER_STATED",
) -> tuple[dict[str, Any], ...]:
    """Extract bounded capability candidates from natural language.

    This is a conservative fallback and a post-model safety net.  It returns
    meaning only: no provider/evidence claim is generated here.  Negated or
    historical statements return no active capability for the matching clause.
    """

    raw_value = str(text or "").strip().casefold()
    if not raw_value:
        return ()
    effective_scope = scope if scope in CAPABILITY_SCOPES else "JOURNEY"
    if effective_scope == "JOURNEY" and any(marker in raw_value for marker in _PERMANENCE_MARKERS): effective_scope = "TRAVELER"
    elif any(marker in raw_value for marker in _THIS_TRIP_MARKERS): effective_scope = "JOURNEY"
    # Negation/rumour is clause-local.  A historical contrast may still carry
    # a current positive requirement in a later clause, so never discard the
    # entire message merely because one clause contains "used to" or "don't".
    #
    # A decimal point between two digits is NOT a sentence boundary: splitting
    # it turns "1.5 公里" into "1" + "5 km", silently corrupting 1500 m into 1 m.
    clauses = [
        part.strip()
        for part in re.split(
            r"[,，。；;!?！？\n]+|(?<!\d)\.(?!\d)|\bbut\b|\bhowever\b|但|但是|不过",
            raw_value,
        )
        if part.strip()
    ]
    active_clauses: list[tuple[str, bool]] = []
    # A hedge ("如果可能的话我需要..." / "maybe I need a wheelchair") weakens the
    # claim.  Surface it as STRONG + needs_confirmation; never drop it, never
    # let it become HARD authority.
    hedged = False
    for clause in clauses:
        # "no more than 100 m" / "walk no more than" is a bound, not a denial.
        bounded_phrase = bool(
            re.search(r"\bno (?:more|longer|further) than\b|\bnot more than\b", clause)
        )
        negated = any(marker in clause for marker in _NEGATED_MARKERS) or (
            bool(re.search(r"\b(?:don't|do not|doesn't|does not|not|never|no)\b", clause))
            and not bounded_phrase
        )
        speculative = any(marker in clause for marker in _SPECULATIVE_MARKERS) or any(
            marker in clause for marker in _FUTURE_HYPOTHETICAL_MARKERS
        )
        hearsay = any(marker in clause for marker in _HEARSAY_MARKERS)
        hedge = any(marker in clause for marker in _HEDGE_MARKERS)
        # A *request* hedge ("如果可能的话我需要无障碍通道") states a present need with
        # courtesy; a *premise* hedge ("如果累了可能需要轮椅") describes a state that
        # may or may not happen.  Only the latter is speculation.
        request_hedge = any(phrase in clause for phrase in _REQUEST_HEDGE_PHRASES)
        conditional_premise = any(marker in clause for marker in _CONDITIONAL_MARKERS)
        asserts_need = any(token in clause for token in _NEED_ASSERTION_TOKENS)
        weak_modal = any(token in clause for token in _WEAK_MODAL_MARKERS)
        if conditional_premise and hedge and not request_hedge:
            speculative = True
        # A weak modal without an asserted need is a preference, not a need.
        if weak_modal and not asserts_need:
            speculative = True
        if request_hedge:
            # Courtesy has no bearing on *whether* the need exists, only on how
            # strongly it is asserted: keep it, but never promote it to HARD.
            hedge = True
            speculative = False
        first_person = any(marker in clause for marker in _FIRST_PERSON_MARKERS)
        first_person_need = any(marker in clause for marker in _FIRST_PERSON_NEED_MARKERS)
        explicit_exclusion = any(marker in clause for marker in _THIRD_PARTY_EXCLUSION_MARKERS)
        companion = any(marker in clause for marker in _COMPANION_MARKERS)
        unrelated_third_party = any(marker in clause for marker in _UNRELATED_THIRD_PARTY_MARKERS)
        # A facility-subject sentence that reports a *condition* ("the station
        # has an accessible toilet") never asserts a personal need.  A sentence
        # that states the traveller's own requirement at a facility
        # ("I must rest at the hotel between 12:00 and 14:00") is a need.
        facility_only = (
            any(marker in clause for marker in _FACILITY_SUBJECT_MARKERS)
            and not first_person
            and any(
                token in clause
                for token in ("无障碍", "轮椅", "wheelchair", "accessible", "step-free", "step free")
            )
        )
        explicit_limit = any(token in clause for token in ("不能超过", "不超过", "最多走", "最多步行", "走超过", "cannot walk more than", "walk no more than", "walking limit"))
        # A care/rest requirement does not depend on the "might" hedge, but a
        # speculative *accessibility* need does.  Track them separately.
        care_need = any(
            token in clause
            for token in ("午休", "午睡", "休息", "睡午觉", "midday rest", "naptime", "nap", "rest")
        )
        if care_need:
            hedge = False
            speculative = False
        positive_need = any(marker in clause for marker in ("必须", "务必", "现在", "当前", "must", "now", "required", "need to"))
        # Describing a venue is not a statement about the traveller.
        if facility_only:
            continue
        # An unrelated person's need is never the traveller's need, and it is
        # never a candidate either.
        if unrelated_third_party:
            continue
        # Hearsay ("a friend said I might need...") is an assessment, not a need.
        if hearsay:
            continue
        # Pure speculation ("说不定/Ben if I end up in a wheelchair") is not a
        # present commitment and must not manufacture an accessibility need.
        # A trip-context preference (stroller, luggage, slow pace) is different:
        # it is planning input, never a HARD need, so surface it conservatively.
        context_preference = any(token in clause for token in _CONTEXT_PREFERENCE_TOKENS)
        if speculative:
            if not context_preference:
                continue
            hedged = True
        # A denial stays a denial, even when the same clause also carries a
        # hedge or a third-party marker.
        if negated and not positive_need and not explicit_limit:
            continue
        # A companion is the journey's own party, never an unrelated stranger.
        # Their *own* accessibility status must not be promoted to the
        # traveller's HARD need, so keep the clause but suppress accessibility.
        suppress_accessibility = companion or explicit_exclusion
        # A hedge weakens the claim: surface it as STRONG + needs_confirmation.
        if explicit_exclusion or companion or hedge:
            hedged = True
        active_clauses.append((clause, suppress_accessibility))
    if not active_clauses:
        return ()
    value = " ".join(clause for clause, _ in active_clauses)
    # A companion's accessibility status is not the traveller's requirement;
    # care and walking limits still surface.
    accessibility_text = " ".join(
        clause for clause, suppress in active_clauses if not suppress
    )
    hard = _hard_asserted(value) and not hedged
    found: list[CapabilityItem] = []

    def add(code: str, params: Mapping[str, Any] | None = None, *, severity: str | None = None) -> None:
        if any(item.code == code for item in found):
            return
        found.append(
            CapabilityItem(
                code=code,
                capability_class=capability_class_for(code),
                severity=severity or default_severity(code, hard_asserted=hard),
                params=dict(params or {}),
                scope=effective_scope,
                source=source,
                provenance=provenance,
                explicit=True,
                active=True,
                confirmed=False,
            )
        )

    accessibility = any(
        token in accessibility_text
        for token in ("无障碍", "轮椅", "wheelchair", "step-free", "step free", "accessible")
    )
    stairs = any(token in accessibility_text for token in ("楼梯", "台阶", "stairs", "stair"))
    if accessibility and (
        (hard and not hedged)
        or "全程" in accessibility_text
        or "must" in accessibility_text
        or "required" in accessibility_text
    ):
        add("STEP_FREE_REQUIRED", {"requirement": "step_free"})
    elif accessibility:
        add("STEP_FREE_REQUIRED", {"requirement": "step_free", "needs_confirmation": True}, severity="STRONG")
    if stairs and any(token in accessibility_text for token in ("不能", "避免", "不要", "no ", "avoid", "cannot", "can't")):
        add("AVOID_STAIRS", {"avoid": True})

    walking = any(
        token in value
        for token in (
            "少走",
            "少步行",
            "不能走太多",
            "步行少",
            "走路少",
            "不能超过",
            "不超过",
            "最多走",
            "最多步行",
            "走超过",
            "走不了远路",
            "走不远",
            "走不动",
            "不能走远",
            "low walking",
            "walk less",
            "avoid long walks",
            "avoid walking",
            "walking limit",
            "cannot walk more than",
            "walk no more than",
            "no more than",
            "cannot walk",
            "can't walk",
            "can not walk",
            "unable to walk",
            "walk far",
        )
    )
    # "cannot walk" only implies a bounded walking limit when the clause is about
    # the traveller walking, not an accessibility/step-free statement that
    # merely mentions walking in passing.
    if walking and "走" not in value and "walk" in value:
        walking = any(
            token in value
            for token in (
                "walk far",
                "cannot walk",
                "can't walk",
                "can not walk",
                "unable to walk",
                "walk less",
                "less walking",
                "walking limit",
                "avoid walking",
                "avoid long walks",
                "walk no more than",
                "no more than",
            )
        )
    if walking:
        max_leg, leg_unit = _quantity_before(
            value,
            ("每段", "每程", "每腿", "每次", "单段", "每一段", "per leg", "each leg", "under", "less than"),
        )
        if max_leg is None and leg_unit is None:
            max_leg, leg_unit = _quantity_before(value, ("走", "walking", "walk"))
        # A duration is not a distance: "连续走不能超过 20 分钟" describes a
        # continuous-walking duration, not a per-leg distance budget.
        duration_bound = _unit_kind(leg_unit) == "duration"
        # A daily walking budget is not a per-leg budget.
        daily_scope = _token_present(value, _DAILY_SCOPE_TOKENS)
        usable = (
            not duration_bound
            and not daily_scope
            and max_leg is not None
            and max_leg >= _MOBILITY_PLAUSIBILITY_FLOOR_M
        )
        if usable:
            add(
                "MAX_WALKING_PER_LEG_M",
                {"max_m": max_leg},
                severity="HARD" if hard else "STRONG",
            )
        elif not daily_scope and not duration_bound:
            # 0 m / below the plausibility floor / unparseable.  A non-positive
            # distance is not a cap a traveller can be held to, so it is never
            # persisted as ``max_m``; the row is kept only as an explicit
            # clarification request.  Storing ``max_m: 0`` looked like a real
            # constraint and made every leg infeasible.
            params: dict[str, Any] = {"needs_clarification": True}
            if max_leg is not None and max_leg >= 1:
                params["max_m"] = max_leg
            elif max_leg is not None:
                params["stated_m"] = max_leg
            add("MAX_WALKING_PER_LEG_M", params, severity="STRONG")
    continuous, continuous_unit = _quantity_before(
        value, ("连续", "持续", "continuous", "without a break")
    )
    if (
        _unit_kind(continuous_unit) == "duration"
        and continuous is not None
        and any(token in value for token in ("走", "步行", "walking", "walk"))
        and "漫步" not in value
    ):
        add(
            "MAX_CONTINUOUS_WALKING_MIN",
            {"max_min": continuous},
            severity="HARD" if hard else "STRONG",
        )
    daily = _number_before(value, ("每天", "每日", "daily", "per day"))
    if daily is not None and any(token in value for token in ("米", "m ", "metre", "meter")) and _token_present(value, _DAILY_SCOPE_TOKENS):
        add("MAX_DAILY_WALKING_M", {"max_m": daily}, severity="HARD" if hard else "STRONG")

    window = _time_window(value)
    explicit_time_pair = len(re.findall(r"(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]?\d", value)) >= 2
    midday_rest = any(
        token in value
        for token in (
            "午休",
            "中午休息",
            "回酒店休息",
            "回酒店午休",
            "酒店休息",
            "中午睡",
            "midday rest",
            "midday break",
            "rest at noon",
            "rest at the hotel",
            "rest between",
        )
    ) or ("休息" in value and (window is not None or explicit_time_pair))
    if midday_rest:
        params = ({"start": window[0], "end": window[1]} if window else {"needs_clarification": True} if explicit_time_pair else {"start": "12:00", "end": "14:00", "defaulted_window": True})
        add("MIDDAY_REST_WINDOW", params, severity="HARD" if hard and window else "STRONG")
    if any(token in value for token in ("午睡", "睡午觉", "午觉", "nap", "naptime", "siesta")):
        nap_params = (
            {"start": window[0], "end": window[1]}
            if window
            else {"start": "13:00", "end": "15:00", "defaulted_window": True}
        )
        add(
            "INFANT_NAP_WINDOW",
            nap_params,
            severity="HARD" if hard and window else "STRONG",
        )
    rest_every = _number_before(value, ("每", "every", "rest every"))
    if rest_every is not None and any(token in value for token in ("休息", "rest", "break")):
        add("REST_EVERY_MIN", {"interval_min": rest_every}, severity="HARD" if hard else "STRONG")
    toilet_every = _number_before(value, ("每", "every"))
    if toilet_every is not None and any(token in value for token in ("厕所", "卫生间", "toilet", "bathroom")):
        add("TOILET_BREAK_EVERY_MIN", {"interval_min": toilet_every}, severity="HARD" if hard else "STRONG")

    # Severity is named here only for the HARD half, and that half is not decoration:
    # `default_severity` answers "STRONG" for both of these codes whether or not `hard_asserted` is
    # set (measured: both live in CONTEXT_CODES, so it never promotes them), and
    # `capability_constraints` (:911) forwards only HARD items - so an explicitly asserted
    # "我必须推婴儿车 / 我带大箱子" would never reach the accessibility gate that already knows how to
    # read evidence for both (feasibility :818).
    #
    # The non-hard half is left to `default_severity` on purpose (ORD-0234): writing `"STRONG"` here
    # too would put the same answer in two places, and the second copy is the one that goes
    # unnoticed when the first moves.  Earlier text here claimed these codes came back SOFT - that
    # was true before they joined CONTEXT_CODES and is false now, which is exactly the drift a
    # duplicated literal invites.
    #
    # Deliberately *not* touched: `TOILET_BREAK_EVERY_MIN` just above keeps its own literal - it is
    # outside this ruling, and moving it would be somebody else's decision about a different code.
    if any(token in value for token in ("婴儿车", "推车", "stroller", "buggy")):
        add("STROLLER", {"present": True}, severity="HARD" if hard else None)
    if any(token in value for token in ("大箱子", "行李", "luggage", "suitcase", "baggage")):
        add("LARGE_LUGGAGE", {"present": True}, severity="HARD" if hard else None)
    if any(token in value for token in ("慢一点", "慢速", "slow pace", "take it slowly")):
        add("SLOW_PACE", {"preferred": True})
    if any(token in value for token in ("安静", "quiet", "少刺激", "低刺激", "sensory", "low stimulation")):
        add("QUIET_ENVIRONMENT", {"preferred": True})
    if any(token in value for token in ("低刺激", "low stimulation", "sensory-friendly")):
        add("SENSORY_LOW_STIMULATION", {"preferred": True})
    if any(token in value for token in ("室内", "indoor")):
        add("INDOOR_PREFERRED", {"preferred": True})
    return tuple(item.as_dict() for item in found)


def canonicalize_capabilities(
    candidates: Iterable[Mapping[str, Any]] | None,
    *,
    source: str = "SEMANTIC",
    provenance: str = "USER_STATED",
    scope: str = "JOURNEY",
    explicit: bool | None = None,
    confirmed: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Validate model/API candidates and enforce hard-severity invariants."""

    result: list[dict[str, Any]] = []
    seen: dict[tuple[Any, str], dict[str, Any]] = {}
    for raw in candidates or ():
        if not isinstance(raw, Mapping):
            continue
        code = str(raw.get("code") or raw.get("rule_code") or "").upper().strip()
        if code not in CAPABILITY_CODES:
            continue
        klass = capability_class_for(code)
        raw_class = str(raw.get("class") or raw.get("capability_class") or klass).upper()
        if raw_class not in CAPABILITY_CLASSES:
            raw_class = klass
        params = raw.get("params") if isinstance(raw.get("params"), Mapping) else {}
        severity = str(raw.get("severity") or default_severity(code, hard_asserted=False)).upper()
        if severity not in CAPABILITY_SEVERITIES:
            severity = default_severity(code, hard_asserted=False)
        # A hard accessibility capability may not be downgraded by a model or
        # by an API client that mislabeled its class.
        # Accessibility requirements are never downgraded.  A structured
        # model candidate that identifies the requirement is sufficient to
        # preserve HARD semantics; only explicit negative/disputed language
        # is allowed to suppress the candidate before this boundary.
        if code in HARD_ACCESSIBILITY_CODES:
            severity = "HARD"
        item = CapabilityItem(
            code=code,
            capability_class=klass if code in HARD_ACCESSIBILITY_CODES else raw_class,
            severity=severity,
            params=dict(params),
            scope=str(raw.get("scope") or scope).upper() if str(raw.get("scope") or scope).upper() in CAPABILITY_SCOPES else "JOURNEY",
            source=str(raw.get("source") or source),
            provenance=str(raw.get("provenance") or provenance),
            explicit=bool(raw.get("explicit", True) if explicit is None else explicit),
            active=bool(raw.get("active", True)),
            confirmed=bool(raw.get("confirmed", confirmed)),
        )
        payload = item.as_dict()
        merge_key = (payload.get("scope"), code)
        previous = seen.get(merge_key)
        if previous is None:
            seen[merge_key] = payload
            result.append(payload)
        else:
            # Deterministic merge: HARD wins, then confirmed, then the first
            # stable parameter payload.  Duplicate codes never survive.
            if payload["severity"] == "HARD" or previous["severity"] != "HARD":
                previous["severity"] = payload["severity"]
            previous["confirmed"] = bool(previous.get("confirmed") or payload.get("confirmed"))
            incoming_params = dict(payload.get("params") or {}); existing_params = dict(previous.get("params") or {})
            for key in ("max_m", "max_min", "interval_min"):
                left = _bounded_number(existing_params.get(key), minimum=1); right = _bounded_number(incoming_params.get(key), minimum=1)
                if left is not None and right is not None: existing_params[key] = min(left, right)
                elif left is None and right is not None: existing_params[key] = right
            for key, value in incoming_params.items(): existing_params.setdefault(key, value)
            previous["params"] = existing_params
    return tuple(result)


def capability_constraints(capabilities: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Translate active canonical capabilities into existing gate constraints."""

    constraints: list[dict[str, Any]] = []
    for item in canonicalize_capabilities(capabilities, source="CAPABILITY_PROJECTION"):
        if not item.get("active"):
            continue
        severity = str(item.get("severity") or "SOFT").upper()
        # Only HARD items are feasibility gates.  STRONG/SOFT remain available
        # to ranking and explanation through the original capability payload.
        if severity != "HARD":
            continue
        code = str(item["code"])
        params = dict(item.get("params") or {})
        constraints.append(
            {
                "kind": "HARD",
                "category": str(item.get("class") or capability_class_for(code)),
                "rule_code": code,
                "params": params,
                "active": True,
                "capability": item,
            }
        )
    return constraints


def summarize_capabilities(capabilities: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    items = [dict(item) for item in canonicalize_capabilities(capabilities)]
    return {
        "items": items,
        "hard": [item for item in items if item.get("severity") == "HARD"],
        "strong": [item for item in items if item.get("severity") == "STRONG"],
        "soft": [item for item in items if item.get("severity") == "SOFT"],
        "unknown_accessibility_is_not_accessible": True,
    }


def accessibility_state(value: Any) -> str:
    """Normalize provider/frozen accessibility metadata without inference."""

    if not isinstance(value, Mapping):
        return "ACCESSIBILITY_UNVERIFIED"
    status = str(value.get("evidence_status") or value.get("status") or "").upper()
    if status in ACCESSIBILITY_STATES:
        return status
    if status in {"VERIFIED", "KNOWN_ACCESSIBLE", "ACCESSIBLE", "TRUE"}:
        return "ACCESSIBILITY_VERIFIED"
    if status in {"CONFLICT", "KNOWN_INACCESSIBLE", "INACCESSIBLE", "FALSE", "STAIRS"}:
        return "ACCESSIBILITY_CONFLICT"
    if value.get("step_free") is True or value.get("wheelchair_access") is True:
        return "ACCESSIBILITY_VERIFIED"
    if value.get("step_free") is False or value.get("wheelchair_access") is False or value.get("stairs") is True:
        return "ACCESSIBILITY_CONFLICT"
    return "ACCESSIBILITY_UNVERIFIED"


def normalize_osm_tags(value: Any) -> dict[str, str]:
    """Accept provider tags as mappings or OSM-style key=value sequences."""
    if isinstance(value, Mapping):
        result: dict[str, str] = {}
        for key, raw in value.items():
            if isinstance(raw, (list, tuple, set)):
                raw = next((part for part in raw if not isinstance(part, (Mapping, list, tuple, set))), "")
            result[str(key).strip().lower()] = str(raw).strip().lower()
        return result
    result: dict[str, str] = {}
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, Mapping):
                result.update(normalize_osm_tags(item))
                continue
            text = str(item)
            if "=" in text:
                key, raw = text.split("=", 1)
                result[key.strip().lower()] = raw.strip().lower()
    return result


def _count_positive(value: Any) -> bool:
    """True when a step-count style value asserts at least one step.

    Accepts a numeric count ("5") and the boolean forms providers actually
    publish ("yes" / "true") so ``steps=yes`` is read as stairs rather than as
    an unrelated tag.
    """

    if isinstance(value, (list, tuple, set)):
        value = next((part for part in value if part not in (None, "")), None)
    if value is None or isinstance(value, bool):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"yes", "true", "1", "stairs", "steps"}:
        return True
    if text in {"no", "false", "0", ""}:
        return False
    try:
        return int(text) > 0
    except (TypeError, ValueError):
        return False


def evaluate_accessibility_requirement(code: str, facts: Any) -> str:
    """Evaluate only evidence that is about the requested requirement.

    Evidence scope is semantic, not cosmetic: a toilet-scoped, ramp-scoped or
    elevator-scoped provider fact never proves a step-free place or route.
    Nested ``osm_tags`` / ``tags`` and top-level facts are read consistently.
    """

    code = str(code).upper()
    if not isinstance(facts, Mapping):
        facts = {}
    tags = normalize_osm_tags(facts.get("osm_tags") or facts.get("tags") or facts)

    def norm(value: Any) -> str:
        return str(value).strip().casefold()

    def truth(key: str) -> bool:
        """Provider evidence positively asserting the named capability."""

        return norm(facts.get(key)) in {"yes", "true", "1", "verified", "accessible"} or norm(
            tags.get(key)
        ) in {"yes", "true", "1"}

    def false(key: str) -> bool:
        return norm(facts.get(key)) in {"no", "false", "0", "inaccessible", "stairs"} or norm(
            tags.get(key)
        ) in {"no", "false", "0"}

    def any_true(*keys: str) -> bool:
        return any(truth(key) for key in keys)

    def any_false(*keys: str) -> bool:
        return any(false(key) for key in keys)

    # A positive provider verdict is authoritative even when the same payload
    # also carries an unrelated partial tag (for example ramp=yes).
    evidence_status = norm(facts.get("evidence_status") or facts.get("status"))
    if evidence_status in {"verified", "known_accessible", "accessible"}:
        return "ACCESSIBILITY_VERIFIED"
    if evidence_status in {"conflict", "known_inaccessible", "inaccessible", "stairs"}:
        return "ACCESSIBILITY_CONFLICT"
    if truth("step_free_verified") or truth("route_step_free_verified"):
        return "ACCESSIBILITY_VERIFIED"

    is_route = norm(facts.get("subject") or facts.get("scope")) in {"route", "path", "itinerary"}

    if code == "STEP_FREE_REQUIRED":
        stair_evidence = (
            any_true("stairs", "has_stairs")
            or norm(tags.get("highway")) == "steps"
            or norm(facts.get("highway")) == "steps"
            or _count_positive(facts.get("step_count"))
            or _count_positive(facts.get("steps"))
            or _count_positive(tags.get("step_count"))
            or _count_positive(tags.get("steps"))
        )
        if stair_evidence:
            return "ACCESSIBILITY_CONFLICT"
        if is_route:
            if any_true("route_step_free", "step_free_route", "route_step_free_verified"):
                return "ACCESSIBILITY_VERIFIED"
            return "NEEDS_LIVE_EVIDENCE"
        if any_false("step_free", "step_free_verified"):
            return "ACCESSIBILITY_CONFLICT"
        if any_true("step_free"):
            return "ACCESSIBILITY_VERIFIED"
        return "ACCESSIBILITY_UNVERIFIED"
    if code == "AVOID_STAIRS":
        if any_true("stairs", "has_stairs") or norm(tags.get("highway")) == "steps":
            return "ACCESSIBILITY_CONFLICT"
        if any_false("stairs", "has_stairs") or any_true("step_free", "step_free_verified"):
            return "ACCESSIBILITY_VERIFIED"
        return "ACCESSIBILITY_UNVERIFIED"
    if code == "WHEELCHAIR_PLACE_ACCESS":
        if any_true("wheelchair", "wheelchair_access"):
            return "ACCESSIBILITY_VERIFIED"
        if any_false("wheelchair", "wheelchair_access"):
            return "ACCESSIBILITY_CONFLICT"
    return accessibility_state(facts)
