"""Advisory traveller day rhythm used by the deterministic planner."""

from __future__ import annotations

import re
from dataclasses import dataclass, is_dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence


DEFAULT_WAKE_MINUTE = 7 * 60 + 30
DEFAULT_BED_MINUTE = 22 * 60 + 30
_CLOCK = r"(?:\d{1,2}(?::\d{2})?|[零〇一二两三四五六七八九十]+)"
_ZH_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass(frozen=True)
class DayRhythm:
    wake_minute: int
    bed_minute: int
    skip_breakfast: bool = False
    brunch: bool = False
    late_night: bool = False
    source: str = "DEFAULT"
    evidence: tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        return self.source == "DEFAULT"

    @property
    def overnight_minutes(self) -> int:
        """Return the sleep interval from bedtime to the next wake time."""

        return (self.wake_minute - self.bed_minute) % (24 * 60)

    def as_dict(self) -> dict[str, Any]:
        return {
            "wake_minute": self.wake_minute,
            "bed_minute": self.bed_minute,
            "wake_hhmm": _hhmm(self.wake_minute),
            "bed_hhmm": _hhmm(self.bed_minute),
            "skip_breakfast": self.skip_breakfast,
            "brunch": self.brunch,
            "late_night": self.late_night,
            "source": self.source,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "DayRhythm | None":
        if not isinstance(raw, dict):
            return None
        try:
            wake = int(raw["wake_minute"])
            bed = int(raw["bed_minute"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= wake < 24 * 60 and 0 <= bed < 24 * 60):
            return None
        return cls(
            wake_minute=wake,
            bed_minute=bed,
            skip_breakfast=bool(raw.get("skip_breakfast")),
            brunch=bool(raw.get("brunch")),
            late_night=bool(raw.get("late_night")),
            source=str(raw.get("source") or "PERSISTED"),
            evidence=tuple(str(item) for item in raw.get("evidence", []) or []),
        )


def _hhmm(minute: int) -> str:
    minute %= 24 * 60
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _clock(token: str) -> int | None:
    token = token.strip()
    if ":" in token:
        hour, minute = token.split(":", 1)
        return int(hour) * 60 + int(minute)
    if token.isdigit():
        hour = int(token)
        return hour * 60 if 0 <= hour <= 24 else None
    if token == "十":
        return 600
    if token.startswith("十"):
        return (10 + _ZH_DIGITS.get(token[1:], 0)) * 60
    if token.endswith("十"):
        return _ZH_DIGITS.get(token[0], 0) * 10 * 60
    return _ZH_DIGITS.get(token, 0) * 60


def _parse_clock(patterns: Iterable[re.Pattern[str]], text: str) -> tuple[int | None, str | None]:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            parsed = _clock(match.group(1))
            if parsed is not None:
                return parsed, match.group(0).strip()
    return None, None


def extract_day_rhythm(text: str) -> DayRhythm | None:
    if not text or not text.strip():
        return None
    lowered = text.casefold()
    wake, wake_evidence = _parse_clock(
        (
            re.compile(rf"({_CLOCK})\s*点\s*(?:才|多)?\s*(?:起|醒|起床|睡醒)"),
            re.compile(rf"(?:wake\s+up|get\s+up|waking\s+up)\s*(?:at|around|about)?\s*({_CLOCK})", re.I),
        ),
        text,
    )
    bed, bed_evidence = _parse_clock(
        (
            re.compile(rf"(?:凌晨|半夜|晚上)?\s*({_CLOCK})\s*点\s*(?:才|多)?\s*(?:睡|睡觉|睡下|上床)"),
            re.compile(rf"(?:bed|sleep|lights\s+out)\s*(?:at|around|about)?\s*({_CLOCK})", re.I),
        ),
        text,
    )
    late_bed = any(item in lowered for item in ("很晚才睡", "折腾到很晚", "睡得很晚", "熬夜", "夜猫子", "stay up late", "late nights", "night owl", "sleep late"))
    late_wake = any(item in lowered for item in ("起得很晚", "睡到中午", "中午才起", "睡到自然醒", "不爱早起", "不想早起", "sleep in", "lie in", "sleep until noon", "not a morning person", "no early mornings"))
    evidence = [item for item in (wake_evidence, bed_evidence) if item]
    if late_bed:
        evidence.append("late-night sleep")
    if late_wake:
        evidence.append("late wake")
    if wake is None and (late_wake or late_bed):
        wake = 10 * 60 + 30
    if bed is None and (late_bed or late_wake):
        bed = 90
    if wake is None and bed is None:
        return None
    skip_breakfast = any(item in lowered for item in ("早餐不用", "不用订早餐", "不吃早餐", "不用早餐", "跳过早餐", "no breakfast", "skip breakfast"))
    brunch = any(item in lowered for item in ("早午餐", "brunch"))
    late_night = any(item in lowered for item in ("夜宵", "夜市", "夜生活", "晚上还想出去", "夜游", "late night", "nightlife", "night market", "supper"))
    if wake is None:
        wake = (bed + (24 * 60 - DEFAULT_BED_MINUTE) + DEFAULT_WAKE_MINUTE) % (24 * 60)
    if bed is None:
        bed = (wake - DEFAULT_WAKE_MINUTE + DEFAULT_BED_MINUTE) % (24 * 60)
    if brunch:
        skip_breakfast = True
    if skip_breakfast:
        evidence.append("skip breakfast")
    if brunch:
        evidence.append("brunch")
    if late_night:
        evidence.append("late-night activity")
    return DayRhythm(
        wake_minute=wake % (24 * 60),
        bed_minute=bed % (24 * 60),
        skip_breakfast=skip_breakfast,
        brunch=brunch,
        late_night=late_night,
        source="EXTRACTED_FROM_USER_TEXT",
        evidence=tuple(dict.fromkeys(evidence)),
    )


def rhythm_from_preferences(preferences: Any) -> DayRhythm | None:
    if not isinstance(preferences, dict):
        return None
    return DayRhythm.from_dict(preferences.get("day_rhythm"))


def _protected(node: Any) -> bool:
    if bool(getattr(node, "locked", False)):
        return True
    if str(getattr(node, "role", "")) in {"ARRIVAL", "DEPARTURE", "CHECK_IN", "CHECK_OUT", "LUGGAGE_DROP"}:
        return True
    metadata = getattr(node, "metadata", {}) or {}
    return bool(metadata.get("commitment_ids") or metadata.get("reservation_id") or metadata.get("booking_id"))


def _inside_waking_window(node: Any, rhythm: DayRhythm) -> bool:
    start = datetime.fromisoformat(str(node.start_at))
    end = datetime.fromisoformat(str(node.end_at))
    start_minute = start.hour * 60 + start.minute
    end_minute = end.hour * 60 + end.minute
    if end.date() > start.date():
        end_minute += 24 * 60
    wake = rhythm.wake_minute
    bed = rhythm.bed_minute
    if bed <= wake:
        bed += 24 * 60
    if start_minute < wake:
        start_minute += 24 * 60
        end_minute += 24 * 60
    return wake <= start_minute and end_minute <= bed


def apply_day_rhythm(nodes: Sequence[Any], rhythm: DayRhythm | None) -> list[Any]:
    """Filter optional nodes; protected anchors and reservations are untouched."""

    if rhythm is None or rhythm.is_default:
        return list(nodes)
    result: list[Any] = []
    for node in nodes:
        if str(getattr(node, "role", "")) == "STAY":
            result.append(_retime_overnight(node, rhythm))
        elif _protected(node) or _inside_waking_window(node, rhythm):
            result.append(node)
        elif rhythm.skip_breakfast and "breakfast" in str(getattr(node, "title", "")).casefold():
            continue
    return result


def _retime_overnight(node: Any, rhythm: DayRhythm) -> Any:
    start = datetime.fromisoformat(str(node.start_at))
    bedtime = start.replace(hour=rhythm.bed_minute // 60, minute=rhythm.bed_minute % 60, second=0, microsecond=0)
    if bedtime < start - timedelta(hours=12):
        bedtime += timedelta(days=1)
    if is_dataclass(node):
        return replace(
            node,
            start_at=bedtime.isoformat(),
            end_at=(bedtime + timedelta(minutes=rhythm.overnight_minutes)).isoformat(),
        )
    node.start_at = bedtime.isoformat()
    node.end_at = (bedtime + timedelta(minutes=rhythm.overnight_minutes)).isoformat()
    return node
