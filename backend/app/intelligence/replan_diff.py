from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class PlanActivitySnapshot:
    semantic_key: str
    start_at: datetime
    end_at: datetime
    cost_minor: int = 0
    protected: bool = False


@dataclass(frozen=True)
class MovedActivity:
    semantic_key: str
    old_start_at: datetime
    new_start_at: datetime
    delta_minutes: int


@dataclass(frozen=True)
class ReplanDiff:
    kept: tuple[str, ...]
    moved: tuple[MovedActivity, ...]
    removed: tuple[str, ...]
    added: tuple[str, ...]
    preserved_protected: tuple[str, ...]
    cost_delta_minor: int
    old_end_at: datetime | None
    new_end_at: datetime | None


def _group(items: Iterable[PlanActivitySnapshot]) -> dict[str, list[PlanActivitySnapshot]]:
    grouped: dict[str, list[PlanActivitySnapshot]] = {}
    for item in sorted(items, key=lambda a: (a.semantic_key, a.start_at, a.end_at)):
        grouped.setdefault(item.semantic_key, []).append(item)
    return grouped


def diff_plans(
    old: Iterable[PlanActivitySnapshot],
    new: Iterable[PlanActivitySnapshot],
) -> ReplanDiff:
    old_list = list(old)
    new_list = list(new)
    old_groups = _group(old_list)
    new_groups = _group(new_list)

    kept: list[str] = []
    moved: list[MovedActivity] = []
    removed: list[str] = []
    added: list[str] = []
    preserved_protected: list[str] = []

    for key in sorted(set(old_groups) | set(new_groups)):
        old_items = list(old_groups.get(key, []))
        new_items = list(new_groups.get(key, []))
        pair_count = min(len(old_items), len(new_items))
        for i in range(pair_count):
            before, after = old_items[i], new_items[i]
            if before.start_at == after.start_at and before.end_at == after.end_at:
                kept.append(key)
            else:
                delta = int((after.start_at - before.start_at).total_seconds() // 60)
                moved.append(MovedActivity(key, before.start_at, after.start_at, delta))
            if before.protected and after.protected:
                preserved_protected.append(key)
        if len(old_items) > pair_count:
            removed.extend(key for _ in old_items[pair_count:])
        if len(new_items) > pair_count:
            added.extend(key for _ in new_items[pair_count:])

    old_end = max((item.end_at for item in old_list), default=None)
    new_end = max((item.end_at for item in new_list), default=None)
    return ReplanDiff(
        kept=tuple(kept),
        moved=tuple(moved),
        removed=tuple(removed),
        added=tuple(added),
        preserved_protected=tuple(preserved_protected),
        cost_delta_minor=sum(item.cost_minor for item in new_list)
        - sum(item.cost_minor for item in old_list),
        old_end_at=old_end,
        new_end_at=new_end,
    )
