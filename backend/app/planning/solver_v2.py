from __future__ import annotations

"""Deterministic whole-horizon Solver V2.

The V1 solver chooses one replacement before one protected anchor.  V2 keeps
the same important boundary -- it only consumes primitive facts from a frozen
mobility snapshot -- but searches the remaining mutable slots together.  The
CP-SAT is the primary optimizer.  The bounded enumerator remains an honest
fallback for installations without OR-Tools or for an indeterminate solver
result; fallback plans retain their distinct generator provenance.
"""

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

VALID_OPTION_ACTIONS = frozenset({"KEEP", "REPLACE"})


@dataclass(frozen=True)
class SolverV2Anchor:
    key: str
    location_key: str
    start_min: int
    duration_min: int

    def __post_init__(self) -> None:
        if not self.key or not self.location_key:
            raise ValueError("Solver V2 anchor requires stable keys")
        if self.start_min < 0 or self.duration_min <= 0:
            raise ValueError("Solver V2 anchor time is invalid")

    @property
    def end_min(self) -> int:
        return self.start_min + self.duration_min


@dataclass(frozen=True)
class SolverV2Option:
    option_key: str
    location_key: str
    action: str
    duration_min: int
    cost_minor: int
    earliest_start_min: int
    latest_end_min: int
    original_start_min: int
    original_cost_minor: int
    preference_milli: int = 0
    risk_penalty_milli: int = 0

    def __post_init__(self) -> None:
        if not self.option_key or not self.location_key:
            raise ValueError("Solver V2 option requires stable keys")
        if self.action not in VALID_OPTION_ACTIONS:
            raise ValueError(f"unsupported Solver V2 option action: {self.action}")
        if self.duration_min <= 0:
            raise ValueError("Solver V2 option duration must be positive")
        if self.cost_minor < 0 or self.original_cost_minor < 0:
            raise ValueError("Solver V2 option cost cannot be negative")
        if self.earliest_start_min < 0:
            raise ValueError("Solver V2 option opening start cannot be negative")
        if self.latest_end_min <= self.earliest_start_min:
            raise ValueError("Solver V2 option opening window is invalid")
        if self.original_start_min < 0:
            raise ValueError("Solver V2 option original start cannot be negative")
        if not 0 <= self.preference_milli <= 1000:
            raise ValueError("Solver V2 preference_milli must be 0..1000")
        if self.risk_penalty_milli < 0:
            raise ValueError("Solver V2 risk penalty cannot be negative")


@dataclass(frozen=True)
class SolverV2Slot:
    slot_key: str
    sequence_no: int
    original_location_key: str
    original_start_min: int
    original_end_min: int
    original_cost_minor: int
    options: tuple[SolverV2Option, ...]
    must_change: bool = False
    allow_drop: bool = True

    def __post_init__(self) -> None:
        if not self.slot_key or not self.original_location_key:
            raise ValueError("Solver V2 slot requires stable keys")
        if self.sequence_no < 0:
            raise ValueError("Solver V2 slot sequence cannot be negative")
        if self.original_start_min < 0 or self.original_end_min <= self.original_start_min:
            raise ValueError("Solver V2 slot original schedule is invalid")
        if self.original_cost_minor < 0:
            raise ValueError("Solver V2 slot original cost cannot be negative")
        option_keys = [item.option_key for item in self.options]
        if len(option_keys) != len(set(option_keys)):
            raise ValueError("Solver V2 option keys must be unique within a slot")
        if not self.must_change and not any(item.action == "KEEP" for item in self.options):
            raise ValueError("unchanged Solver V2 slot requires a KEEP option")


@dataclass(frozen=True)
class SolverV2Problem:
    journey_id: str
    operation_key: str
    now_min: int
    journey_end_min: int
    budget_ceiling_minor: int
    base_cost_minor: int
    current_location_key: str
    slots: tuple[SolverV2Slot, ...]
    protected_anchors: tuple[SolverV2Anchor, ...]
    frozen_mobility_snapshot_id: str
    travel_minutes: Mapping[tuple[str, str], int]
    safety_buffer_min: int = 15
    max_schedule_drift_min: int | None = None
    top_k: int = 3
    max_combinations: int = 100_000

    def __post_init__(self) -> None:
        if not self.journey_id or not self.operation_key:
            raise ValueError("Solver V2 problem requires journey and operation keys")
        if self.now_min < 0 or self.journey_end_min <= self.now_min:
            raise ValueError("Solver V2 horizon is invalid")
        if self.budget_ceiling_minor < 0 or self.base_cost_minor < 0:
            raise ValueError("Solver V2 budget cannot be negative")
        if self.base_cost_minor > self.budget_ceiling_minor:
            raise ValueError("Solver V2 base cost exceeds budget ceiling")
        if not self.current_location_key:
            raise ValueError("Solver V2 current location is required")
        if not self.frozen_mobility_snapshot_id:
            raise ValueError("Solver V2 requires a frozen mobility snapshot")
        if self.safety_buffer_min < 0:
            raise ValueError("Solver V2 safety buffer cannot be negative")
        if self.max_schedule_drift_min is not None and self.max_schedule_drift_min < 0:
            raise ValueError("Solver V2 schedule drift cannot be negative")
        if self.top_k < 1 or self.max_combinations < 1:
            raise ValueError("Solver V2 search bounds must be positive")
        slot_keys = [item.slot_key for item in self.slots]
        if len(slot_keys) != len(set(slot_keys)):
            raise ValueError("Solver V2 slot keys must be unique")
        anchor_keys = [item.key for item in self.protected_anchors]
        if len(anchor_keys) != len(set(anchor_keys)):
            raise ValueError("Solver V2 anchor keys must be unique")
        if any(
            left.sequence_no >= right.sequence_no for left, right in zip(self.slots, self.slots[1:])
        ):
            raise ValueError("Solver V2 slots must be ordered by sequence")
        if any(
            anchor.start_min < self.now_min or anchor.end_min > self.journey_end_min
            for anchor in self.protected_anchors
        ):
            raise ValueError("Solver V2 protected anchor lies outside remaining horizon")
        copied: dict[tuple[str, str], int] = {}
        for pair, minutes in dict(self.travel_minutes).items():
            if len(pair) != 2 or not pair[0] or not pair[1]:
                raise ValueError("Solver V2 travel pair is invalid")
            value = int(minutes)
            if value < 0:
                raise ValueError("Solver V2 travel minutes cannot be negative")
            copied[(str(pair[0]), str(pair[1]))] = value
        object.__setattr__(self, "travel_minutes", MappingProxyType(copied))

    def travel(self, origin: str, destination: str) -> int:
        if origin == destination:
            return 0
        try:
            return int(self.travel_minutes[(origin, destination)])
        except KeyError as exc:
            raise KeyError(f"missing frozen mobility pair: {origin}|{destination}") from exc

    def to_payload(self) -> dict[str, object]:
        return {
            "journey_id": self.journey_id,
            "operation_key": self.operation_key,
            "now_min": self.now_min,
            "journey_end_min": self.journey_end_min,
            "budget_ceiling_minor": self.budget_ceiling_minor,
            "base_cost_minor": self.base_cost_minor,
            "current_location_key": self.current_location_key,
            "slots": [
                {
                    "slot_key": slot.slot_key,
                    "sequence_no": slot.sequence_no,
                    "original_location_key": slot.original_location_key,
                    "original_start_min": slot.original_start_min,
                    "original_end_min": slot.original_end_min,
                    "original_cost_minor": slot.original_cost_minor,
                    "must_change": slot.must_change,
                    "allow_drop": slot.allow_drop,
                    "options": [
                        {
                            "option_key": option.option_key,
                            "location_key": option.location_key,
                            "action": option.action,
                            "duration_min": option.duration_min,
                            "cost_minor": option.cost_minor,
                            "earliest_start_min": option.earliest_start_min,
                            "latest_end_min": option.latest_end_min,
                            "original_start_min": option.original_start_min,
                            "original_cost_minor": option.original_cost_minor,
                            "preference_milli": option.preference_milli,
                            "risk_penalty_milli": option.risk_penalty_milli,
                        }
                        for option in slot.options
                    ],
                }
                for slot in self.slots
            ],
            "protected_anchors": [
                {
                    "key": anchor.key,
                    "location_key": anchor.location_key,
                    "start_min": anchor.start_min,
                    "duration_min": anchor.duration_min,
                }
                for anchor in self.protected_anchors
            ],
            "frozen_mobility_snapshot_id": self.frozen_mobility_snapshot_id,
            "travel_minutes": [
                {"origin": origin, "destination": destination, "minutes": minutes}
                for (origin, destination), minutes in sorted(self.travel_minutes.items())
            ],
            "safety_buffer_min": self.safety_buffer_min,
            "max_schedule_drift_min": self.max_schedule_drift_min,
            "top_k": self.top_k,
            "max_combinations": self.max_combinations,
        }


@dataclass(frozen=True)
class SolverV2Assignment:
    slot_key: str
    option_key: str | None
    action: str
    location_key: str | None
    start_min: int | None
    end_min: int | None
    cost_minor: int
    travel_from_prev_min: int
    schedule_drift_min: int
    preference_milli: int
    risk_penalty_milli: int


@dataclass(frozen=True)
class SolverV2Plan:
    assignments: tuple[SolverV2Assignment, ...]
    protected_anchors: tuple[SolverV2Anchor, ...]
    total_cost_minor: int
    cost_delta_minor: int
    travel_total_min: int
    changed_activity_count: int
    risk_score: int
    objective_breakdown: Mapping[str, int]
    fingerprint: str
    generator_kind: str
    generator_rank: int

    @property
    def assignment_summary(self) -> tuple[tuple[str, str, int | None], ...]:
        return tuple((item.slot_key, item.action, item.start_min) for item in self.assignments)


def solver_v2_problem_fingerprint(problem: SolverV2Problem) -> str:
    encoded = json.dumps(problem.to_payload(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _assignment_fingerprint(
    problem: SolverV2Problem, assignments: tuple[SolverV2Assignment, ...]
) -> str:
    payload = [
        {
            "slot_key": item.slot_key,
            "option_key": item.option_key,
            "action": item.action,
            "start_min": item.start_min,
            "end_min": item.end_min,
        }
        for item in assignments
    ]
    raw = f"{solver_v2_problem_fingerprint(problem)}|{json.dumps(payload, sort_keys=True)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class _Partial:
    assignments: tuple[SolverV2Assignment, ...]
    cursor_time: int
    cursor_location: str
    anchor_index: int
    total_cost_minor: int
    travel_total_min: int
    changed_activity_count: int
    risk_score: int
    preference_milli: int
    drift_total_min: int


def _ordered_anchors(problem: SolverV2Problem) -> tuple[SolverV2Anchor, ...]:
    return tuple(sorted(problem.protected_anchors, key=lambda item: (item.start_min, item.key)))


def _advance_anchors(
    problem: SolverV2Problem,
    anchors: tuple[SolverV2Anchor, ...],
    *,
    cursor_time: int,
    cursor_location: str,
    anchor_index: int,
    preferred_start: int,
) -> tuple[int, str, int] | None:
    """Consume anchors that are before the next slot in the original order."""

    while anchor_index < len(anchors):
        anchor = anchors[anchor_index]
        if anchor.end_min > preferred_start:
            break
        if cursor_time > anchor.start_min:
            return None
        try:
            travel = problem.travel(cursor_location, anchor.location_key)
        except KeyError:
            return None
        if cursor_time + travel + problem.safety_buffer_min > anchor.start_min:
            return None
        cursor_time = anchor.end_min
        cursor_location = anchor.location_key
        anchor_index += 1
    return cursor_time, cursor_location, anchor_index


def _schedule_option(
    problem: SolverV2Problem,
    anchors: tuple[SolverV2Anchor, ...],
    partial: _Partial,
    slot: SolverV2Slot,
    option: SolverV2Option,
) -> tuple[_Partial, ...]:
    advanced = _advance_anchors(
        problem,
        anchors,
        cursor_time=partial.cursor_time,
        cursor_location=partial.cursor_location,
        anchor_index=partial.anchor_index,
        preferred_start=option.original_start_min,
    )
    if advanced is None:
        return ()
    cursor_time, cursor_location, anchor_index = advanced
    try:
        travel = problem.travel(cursor_location, option.location_key)
    except KeyError:
        return ()
    earliest = max(cursor_time + travel, problem.now_min, option.earliest_start_min)
    latest_start = min(
        option.latest_end_min - option.duration_min,
        problem.journey_end_min - option.duration_min,
    )
    if earliest > latest_start:
        return ()
    start = max(earliest, min(option.original_start_min, latest_start))
    end = start + option.duration_min
    drift = abs(start - option.original_start_min)
    if problem.max_schedule_drift_min is not None and drift > problem.max_schedule_drift_min:
        return ()
    if anchor_index < len(anchors):
        next_anchor = anchors[anchor_index]
        if option.original_start_min < next_anchor.end_min:
            if start >= next_anchor.start_min:
                return ()
            try:
                outbound = problem.travel(option.location_key, next_anchor.location_key)
            except KeyError:
                return ()
            if end + outbound + problem.safety_buffer_min > next_anchor.start_min:
                return ()
    total_cost = partial.total_cost_minor + option.cost_minor
    if total_cost > problem.budget_ceiling_minor:
        return ()
    changed = partial.changed_activity_count + int(option.action != "KEEP")
    assignment = SolverV2Assignment(
        slot_key=slot.slot_key,
        option_key=option.option_key,
        action=option.action,
        location_key=option.location_key,
        start_min=start,
        end_min=end,
        cost_minor=option.cost_minor,
        travel_from_prev_min=travel,
        schedule_drift_min=drift,
        preference_milli=option.preference_milli,
        risk_penalty_milli=option.risk_penalty_milli,
    )
    return (
        _Partial(
            assignments=partial.assignments + (assignment,),
            cursor_time=end,
            cursor_location=option.location_key,
            anchor_index=anchor_index,
            total_cost_minor=total_cost,
            travel_total_min=partial.travel_total_min + travel,
            changed_activity_count=changed,
            risk_score=partial.risk_score + option.risk_penalty_milli,
            preference_milli=partial.preference_milli + option.preference_milli,
            drift_total_min=partial.drift_total_min + drift,
        ),
    )


def _drop_option(
    problem: SolverV2Problem,
    anchors: tuple[SolverV2Anchor, ...],
    partial: _Partial,
    slot: SolverV2Slot,
) -> tuple[_Partial, ...]:
    advanced = _advance_anchors(
        problem,
        anchors,
        cursor_time=partial.cursor_time,
        cursor_location=partial.cursor_location,
        anchor_index=partial.anchor_index,
        preferred_start=slot.original_start_min,
    )
    if advanced is None:
        return ()
    cursor_time, cursor_location, anchor_index = advanced
    return (
        _Partial(
            assignments=partial.assignments
            + (
                SolverV2Assignment(
                    slot_key=slot.slot_key,
                    option_key=None,
                    action="DROP",
                    location_key=None,
                    start_min=None,
                    end_min=None,
                    cost_minor=0,
                    travel_from_prev_min=0,
                    schedule_drift_min=slot.original_end_min - slot.original_start_min,
                    preference_milli=0,
                    risk_penalty_milli=slot.original_cost_minor,
                ),
            ),
            cursor_time=cursor_time,
            cursor_location=cursor_location,
            anchor_index=anchor_index,
            total_cost_minor=partial.total_cost_minor,
            travel_total_min=partial.travel_total_min,
            changed_activity_count=partial.changed_activity_count + 1,
            risk_score=partial.risk_score + slot.original_cost_minor,
            preference_milli=partial.preference_milli,
            drift_total_min=partial.drift_total_min,
        ),
    )


def _finish_anchors(
    problem: SolverV2Problem,
    anchors: tuple[SolverV2Anchor, ...],
    partial: _Partial,
) -> _Partial | None:
    cursor_time, cursor_location, anchor_index = (
        partial.cursor_time,
        partial.cursor_location,
        partial.anchor_index,
    )
    travel_total = partial.travel_total_min
    while anchor_index < len(anchors):
        anchor = anchors[anchor_index]
        if cursor_time > anchor.start_min:
            return None
        try:
            travel = problem.travel(cursor_location, anchor.location_key)
        except KeyError:
            return None
        if cursor_time + travel + problem.safety_buffer_min > anchor.start_min:
            return None
        cursor_time = anchor.end_min
        cursor_location = anchor.location_key
        travel_total += travel
        anchor_index += 1
    if cursor_time > problem.journey_end_min:
        return None
    return _Partial(
        assignments=partial.assignments,
        cursor_time=cursor_time,
        cursor_location=cursor_location,
        anchor_index=anchor_index,
        total_cost_minor=partial.total_cost_minor,
        travel_total_min=travel_total,
        changed_activity_count=partial.changed_activity_count,
        risk_score=partial.risk_score,
        preference_milli=partial.preference_milli,
        drift_total_min=partial.drift_total_min,
    )


def _plan_sort_key(plan: SolverV2Plan) -> tuple[object, ...]:
    return (
        plan.risk_score,
        plan.changed_activity_count,
        plan.objective_breakdown["schedule_drift_min"],
        plan.travel_total_min,
        max(0, plan.cost_delta_minor),
        -plan.objective_breakdown["preference_milli"],
        tuple(
            (item.slot_key, item.option_key or "~DROP", item.start_min or -1)
            for item in plan.assignments
        ),
    )


def _to_plan(
    problem: SolverV2Problem,
    partial: _Partial,
    *,
    rank: int,
) -> SolverV2Plan:
    original_cost = problem.base_cost_minor + sum(
        item.original_cost_minor for item in problem.slots
    )
    cost_delta = partial.total_cost_minor - original_cost
    breakdown = {
        "risk_penalty_milli": partial.risk_score,
        "changed_activity_count": partial.changed_activity_count,
        "schedule_drift_min": partial.drift_total_min,
        "travel_min": partial.travel_total_min,
        "cost_delta_minor": cost_delta,
        "preference_milli": partial.preference_milli,
        "protected_anchor_count": len(problem.protected_anchors),
    }
    assignments = tuple(partial.assignments)
    return SolverV2Plan(
        assignments=assignments,
        protected_anchors=problem.protected_anchors,
        total_cost_minor=partial.total_cost_minor,
        cost_delta_minor=cost_delta,
        travel_total_min=partial.travel_total_min,
        changed_activity_count=partial.changed_activity_count,
        risk_score=partial.risk_score,
        objective_breakdown=MappingProxyType(breakdown),
        fingerprint=_assignment_fingerprint(problem, assignments),
        generator_kind="CITYNEXUS_SOLVER_V2_ENUMERATION",
        generator_rank=rank,
    )


def _solve_v2_enumeration(problem: SolverV2Problem) -> tuple[SolverV2Plan, ...]:
    """Return deterministic Top-K feasible whole-horizon plans.

    The function performs no provider, database or network work.  Missing
    frozen travel facts make a branch unavailable rather than silently
    becoming zero minutes.  The caller remains responsible for materializing
    a plan through the authoritative CityNexus feasibility gate.
    """

    anchors = _ordered_anchors(problem)
    initial = _Partial(
        assignments=(),
        cursor_time=problem.now_min,
        cursor_location=problem.current_location_key,
        anchor_index=0,
        total_cost_minor=problem.base_cost_minor,
        travel_total_min=0,
        changed_activity_count=0,
        risk_score=0,
        preference_milli=0,
        drift_total_min=0,
    )
    partials = [initial]
    visited = 0
    for slot in problem.slots:
        next_partials: list[_Partial] = []
        options = tuple(sorted(slot.options, key=lambda item: item.option_key))
        for partial in partials:
            for option in options:
                visited += 1
                if visited > problem.max_combinations:
                    break
                if slot.must_change and option.action == "KEEP":
                    continue
                next_partials.extend(_schedule_option(problem, anchors, partial, slot, option))
            if slot.allow_drop and not slot.must_change:
                visited += 1
                if visited <= problem.max_combinations:
                    next_partials.extend(_drop_option(problem, anchors, partial, slot))
        partials = next_partials
        if not partials or visited > problem.max_combinations:
            break

    completed: list[SolverV2Plan] = []
    for partial in partials:
        finished = _finish_anchors(problem, anchors, partial)
        if finished is None:
            continue
        completed.append(_to_plan(problem, finished, rank=0))
    completed.sort(key=_plan_sort_key)
    unique: list[SolverV2Plan] = []
    seen: set[str] = set()
    for plan in completed:
        if plan.fingerprint in seen:
            continue
        seen.add(plan.fingerprint)
        unique.append(
            SolverV2Plan(
                assignments=plan.assignments,
                protected_anchors=plan.protected_anchors,
                total_cost_minor=plan.total_cost_minor,
                cost_delta_minor=plan.cost_delta_minor,
                travel_total_min=plan.travel_total_min,
                changed_activity_count=plan.changed_activity_count,
                risk_score=plan.risk_score,
                objective_breakdown=plan.objective_breakdown,
                fingerprint=plan.fingerprint,
                generator_kind=plan.generator_kind,
                generator_rank=len(unique),
            )
        )
        if len(unique) >= problem.top_k:
            break
    return tuple(unique)


class _CpSatUnavailable(RuntimeError):
    pass


def _solve_v2_cp_sat(problem: SolverV2Problem) -> tuple[SolverV2Plan, ...]:
    """Solve fixed-order journey slots with optional keep/replace/drop choices."""

    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs
        raise _CpSatUnavailable("OR-Tools is not installed") from exc

    # Nodes retain the journey's semantic order. Anchors are immutable and
    # slots choose one optional interval (or an explicit drop literal).
    ordered_nodes: list[tuple[str, object]] = [
        *(("slot", slot) for slot in problem.slots),
        *(("anchor", anchor) for anchor in problem.protected_anchors),
    ]
    ordered_nodes.sort(
        key=lambda item: (
            item[1].original_start_min if item[0] == "slot" else item[1].start_min,
            0 if item[0] == "anchor" else 1,
            item[1].slot_key if item[0] == "slot" else item[1].key,
        )
    )
    excluded: list[tuple[int, ...]] = []
    plans: list[SolverV2Plan] = []

    for rank in range(problem.top_k):
        model = cp_model.CpModel()
        slot_state: dict[str, dict[str, object]] = {}
        all_choice_literals: list[object] = []
        choice_ordinals: list[tuple[object, int]] = []
        risk_terms: list[object] = []
        changed_terms: list[object] = []
        drift_terms: list[object] = []
        travel_terms: list[object] = []
        cost_terms: list[object] = []
        preference_terms: list[object] = []

        for slot_index, slot in enumerate(problem.slots):
            choices: list[tuple[str | None, object]] = []
            options: dict[str, dict[str, object]] = {}
            for option_index, option in enumerate(
                sorted(slot.options, key=lambda item: item.option_key)
            ):
                selected = model.NewBoolVar(f"choose_{slot_index}_{option_index}")
                start = model.NewIntVar(
                    option.earliest_start_min,
                    option.latest_end_min - option.duration_min,
                    f"start_{slot_index}_{option_index}",
                )
                end = model.NewIntVar(
                    option.earliest_start_min + option.duration_min,
                    option.latest_end_min,
                    f"end_{slot_index}_{option_index}",
                )
                model.Add(end == start + option.duration_min).OnlyEnforceIf(selected)
                model.Add(start >= problem.now_min).OnlyEnforceIf(selected)
                model.Add(end <= problem.journey_end_min).OnlyEnforceIf(selected)
                raw_drift = model.NewIntVar(
                    0, problem.journey_end_min, f"raw_drift_{slot_index}_{option_index}"
                )
                drift = model.NewIntVar(
                    0, problem.journey_end_min, f"drift_{slot_index}_{option_index}"
                )
                model.AddAbsEquality(raw_drift, start - option.original_start_min)
                model.Add(drift == raw_drift).OnlyEnforceIf(selected)
                model.Add(drift == 0).OnlyEnforceIf(selected.Not())
                if problem.max_schedule_drift_min is not None:
                    model.Add(drift <= problem.max_schedule_drift_min).OnlyEnforceIf(selected)
                if slot.must_change and option.action == "KEEP":
                    model.Add(selected == 0)
                choices.append((option.option_key, selected))
                all_choice_literals.append(selected)
                choice_ordinals.append((selected, option_index + 1))
                risk_terms.append(option.risk_penalty_milli * selected)
                changed_terms.append(int(option.action != "KEEP") * selected)
                drift_terms.append(drift)
                cost_terms.append(option.cost_minor * selected)
                preference_terms.append(option.preference_milli * selected)
                options[option.option_key] = {
                    "option": option,
                    "selected": selected,
                    "start": start,
                    "end": end,
                    "drift": drift,
                }
            drop = None
            if slot.allow_drop and not slot.must_change:
                drop = model.NewBoolVar(f"drop_{slot_index}")
                choices.append((None, drop))
                all_choice_literals.append(drop)
                choice_ordinals.append((drop, len(choices)))
                risk_terms.append(slot.original_cost_minor * drop)
                changed_terms.append(drop)
            model.AddExactlyOne(literal for _key, literal in choices)
            slot_state[slot.slot_key] = {
                "slot": slot,
                "choices": choices,
                "options": options,
                "drop": drop,
            }

        model.Add(problem.base_cost_minor + sum(cost_terms) <= problem.budget_ceiling_minor)
        for vector in excluded:
            model.Add(
                sum(lit for lit, value in zip(all_choice_literals, vector) if value)
                <= sum(vector) - 1
            )

        def node_variants(
            kind: str, node: object, state_by_slot: dict[str, dict[str, object]] = slot_state
        ) -> list[tuple[object | None, str, object, object]]:
            if kind == "anchor":
                return [(None, node.location_key, node.start_min, node.end_min)]
            state = state_by_slot[node.slot_key]
            return [
                (
                    data["selected"],
                    data["option"].location_key,
                    data["start"],
                    data["end"],
                )
                for data in state["options"].values()
            ]

        transition_no = 0

        def conjunction(literals: list[object], active_model=model) -> object | None:
            nonlocal transition_no
            if not literals:
                return None
            transition_no += 1
            active = active_model.NewBoolVar(f"transition_{transition_no}")
            active_model.AddBoolAnd(literals).OnlyEnforceIf(active)
            active_model.AddBoolOr([active, *[literal.Not() for literal in literals]])
            return active

        # Connect each selected node to the next selected node. Intermediate
        # optional slots must be dropped; an intermediate anchor breaks the pair.
        for right_index, (right_kind, right_node) in enumerate(ordered_nodes):
            previous_nodes = ordered_nodes[:right_index]
            if not previous_nodes or all(kind == "slot" for kind, _node in previous_nodes):
                prefix_drops = [slot_state[node.slot_key]["drop"] for kind, node in previous_nodes]
                if all(item is not None for item in prefix_drops):
                    for right_lit, right_location, right_start, _right_end in node_variants(
                        right_kind, right_node
                    ):
                        enforcement = [item for item in prefix_drops if item is not None]
                        if right_lit is not None:
                            enforcement.append(right_lit)
                        try:
                            travel = problem.travel(problem.current_location_key, right_location)
                        except KeyError:
                            if right_lit is not None:
                                model.Add(right_lit == 0)
                            continue
                        active = conjunction(enforcement)
                        buffer = problem.safety_buffer_min if right_kind == "anchor" else 0
                        constraint = model.Add(right_start >= problem.now_min + travel + buffer)
                        if active is not None:
                            constraint.OnlyEnforceIf(active)
                            travel_terms.append(travel * active)
            for left_index in range(right_index):
                middle = ordered_nodes[left_index + 1 : right_index]
                if any(kind == "anchor" for kind, _node in middle):
                    continue
                middle_drops = [slot_state[node.slot_key]["drop"] for kind, node in middle]
                if any(item is None for item in middle_drops):
                    continue
                left_kind, left_node = ordered_nodes[left_index]
                for left_lit, left_location, _left_start, left_end in node_variants(
                    left_kind, left_node
                ):
                    for right_lit, right_location, right_start, _right_end in node_variants(
                        right_kind, right_node
                    ):
                        enforcement = [item for item in middle_drops if item is not None]
                        if left_lit is not None:
                            enforcement.append(left_lit)
                        if right_lit is not None:
                            enforcement.append(right_lit)
                        try:
                            travel = problem.travel(left_location, right_location)
                        except KeyError:
                            if enforcement:
                                model.Add(sum(enforcement) <= len(enforcement) - 1)
                            else:
                                model.Add(False)
                            continue
                        active = conjunction(enforcement)
                        buffer = problem.safety_buffer_min if right_kind == "anchor" else 0
                        model.Add(right_start >= left_end + travel + buffer).OnlyEnforceIf(
                            active if active is not None else []
                        )
                        if active is not None:
                            travel_terms.append(travel * active)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 5.0
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        expressions = [
            sum(risk_terms),
            sum(changed_terms),
            sum(drift_terms),
            sum(travel_terms),
            sum(cost_terms),
            -sum(preference_terms),
        ]
        status = None
        for expression in expressions:
            model.Minimize(expression)
            status = solver.Solve(model)
            if status in {cp_model.UNKNOWN, cp_model.MODEL_INVALID}:
                raise _CpSatUnavailable(f"CP-SAT returned {solver.StatusName(status)}")
            if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
                return tuple(plans)
            model.Add(expression == int(solver.Value(expression)))
        tie_break = sum(literal * ordinal for literal, ordinal in choice_ordinals)
        model.Minimize(tie_break)
        status = solver.Solve(model)
        if status in {cp_model.UNKNOWN, cp_model.MODEL_INVALID}:
            raise _CpSatUnavailable(f"CP-SAT returned {solver.StatusName(status)}")
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            return tuple(plans)

        vector = tuple(int(solver.Value(literal)) for literal in all_choice_literals)
        excluded.append(vector)
        assignments: list[SolverV2Assignment] = []
        cursor_location = problem.current_location_key
        travel_total = 0
        for kind, node in ordered_nodes:
            if kind == "anchor":
                travel = problem.travel(cursor_location, node.location_key)
                travel_total += travel
                cursor_location = node.location_key
                continue
            state = slot_state[node.slot_key]
            selected_data = next(
                (data for data in state["options"].values() if solver.Value(data["selected"])),
                None,
            )
            if selected_data is None:
                assignments.append(
                    SolverV2Assignment(
                        slot_key=node.slot_key,
                        option_key=None,
                        action="DROP",
                        location_key=None,
                        start_min=None,
                        end_min=None,
                        cost_minor=0,
                        travel_from_prev_min=0,
                        schedule_drift_min=node.original_end_min - node.original_start_min,
                        preference_milli=0,
                        risk_penalty_milli=node.original_cost_minor,
                    )
                )
                continue
            option = selected_data["option"]
            travel = problem.travel(cursor_location, option.location_key)
            start = int(solver.Value(selected_data["start"]))
            end = int(solver.Value(selected_data["end"]))
            assignments.append(
                SolverV2Assignment(
                    slot_key=node.slot_key,
                    option_key=option.option_key,
                    action=option.action,
                    location_key=option.location_key,
                    start_min=start,
                    end_min=end,
                    cost_minor=option.cost_minor,
                    travel_from_prev_min=travel,
                    schedule_drift_min=abs(start - option.original_start_min),
                    preference_milli=option.preference_milli,
                    risk_penalty_milli=option.risk_penalty_milli,
                )
            )
            travel_total += travel
            cursor_location = option.location_key
        assignments.sort(
            key=lambda item: next(
                slot.sequence_no for slot in problem.slots if slot.slot_key == item.slot_key
            )
        )
        assignment_tuple = tuple(assignments)
        total_cost = problem.base_cost_minor + sum(item.cost_minor for item in assignment_tuple)
        original_cost = problem.base_cost_minor + sum(
            item.original_cost_minor for item in problem.slots
        )
        risk = sum(item.risk_penalty_milli for item in assignment_tuple)
        changed = sum(item.action != "KEEP" for item in assignment_tuple)
        breakdown = MappingProxyType(
            {
                "risk_penalty_milli": risk,
                "changed_activity_count": changed,
                "schedule_drift_min": sum(item.schedule_drift_min for item in assignment_tuple),
                "travel_min": travel_total,
                "cost_delta_minor": total_cost - original_cost,
                "preference_milli": sum(item.preference_milli for item in assignment_tuple),
                "protected_anchor_count": len(problem.protected_anchors),
            }
        )
        plans.append(
            SolverV2Plan(
                assignments=assignment_tuple,
                protected_anchors=problem.protected_anchors,
                total_cost_minor=total_cost,
                cost_delta_minor=total_cost - original_cost,
                travel_total_min=travel_total,
                changed_activity_count=changed,
                risk_score=risk,
                objective_breakdown=breakdown,
                fingerprint=_assignment_fingerprint(problem, assignment_tuple),
                generator_kind="ORTOOLS_CP_SAT_V2",
                generator_rank=rank,
            )
        )
    return tuple(plans)


def solve_v2(problem: SolverV2Problem) -> tuple[SolverV2Plan, ...]:
    """Return deterministic Top-K plans using CP-SAT, with labeled fallback."""

    try:
        return _solve_v2_cp_sat(problem)
    except _CpSatUnavailable:
        return _solve_v2_enumeration(problem)
