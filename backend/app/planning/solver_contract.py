from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class SolverAnchor:
    """A protected semantic/time anchor such as a durable C4 commitment."""

    semantic_key: str
    location_key: str
    start_min: int
    duration_min: int

    def __post_init__(self) -> None:
        if not self.semantic_key or not self.location_key:
            raise ValueError("solver anchor requires semantic and location keys")
        if self.start_min < 0 or self.duration_min <= 0:
            raise ValueError("solver anchor time is invalid")


@dataclass(frozen=True)
class SolverReplacementOption:
    """Pure facts for one deterministic replacement option.

    The option does not contain ORM objects, provider clients or FastAPI state.
    All times are integer minutes from Journey.start_at.
    """

    option_key: str
    location_key: str
    duration_min: int
    cost_minor: int
    earliest_start_min: int
    latest_end_min: int
    original_start_min: int
    preference_milli: int = 0

    def __post_init__(self) -> None:
        if not self.option_key or not self.location_key:
            raise ValueError("solver option requires stable keys")
        if self.duration_min <= 0:
            raise ValueError("solver option duration must be positive")
        if self.cost_minor < 0:
            raise ValueError("solver option cost cannot be negative")
        if self.earliest_start_min < 0 or self.latest_end_min <= self.earliest_start_min:
            raise ValueError("solver option opening window is invalid")
        if not 0 <= self.preference_milli <= 1000:
            raise ValueError("solver option preference_milli must be 0..1000")


@dataclass(frozen=True)
class ReplanSolverProblem:
    """Operation-scoped immutable input for the first real solver increment.

    V1 intentionally models the disrupted mutable slot before the next protected
    anchor. Existing activities outside this slot are copied unchanged and are
    still checked by CityNexus's authoritative feasibility gate after materialization.
    """

    journey_id: str
    operation_key: str
    now_min: int
    journey_end_min: int
    budget_remaining_minor: int
    current_location_key: str
    next_anchor: SolverAnchor
    replacement_options: tuple[SolverReplacementOption, ...]
    frozen_mobility_snapshot_id: str
    travel_minutes: Mapping[tuple[str, str], int]
    completed_prefix_keys: tuple[str, ...] = ()
    safety_buffer_min: int = 15
    allow_drop: bool = True

    def __post_init__(self) -> None:
        if not self.journey_id or not self.operation_key:
            raise ValueError("solver problem requires journey and operation keys")
        if self.now_min < 0 or self.journey_end_min <= self.now_min:
            raise ValueError("solver problem horizon is invalid")
        if self.budget_remaining_minor < 0:
            raise ValueError("solver budget cannot be negative")
        if not self.current_location_key:
            raise ValueError("solver problem requires current location")
        if not self.frozen_mobility_snapshot_id:
            raise ValueError("solver problem requires frozen mobility snapshot id")
        if self.safety_buffer_min < 0:
            raise ValueError("solver safety buffer cannot be negative")
        if self.next_anchor.start_min > self.journey_end_min:
            raise ValueError("protected anchor lies outside Journey horizon")
        option_keys = [item.option_key for item in self.replacement_options]
        if len(option_keys) != len(set(option_keys)):
            raise ValueError("solver replacement option keys must be unique")
        if any(
            not 0 <= item.original_start_min <= self.journey_end_min
            for item in self.replacement_options
        ):
            raise ValueError("solver option original start lies outside Journey horizon")
        copied: dict[tuple[str, str], int] = {}
        for pair, minutes in dict(self.travel_minutes).items():
            if len(pair) != 2 or not pair[0] or not pair[1]:
                raise ValueError("solver travel pair is invalid")
            value = int(minutes)
            if value < 0:
                raise ValueError("solver travel minutes cannot be negative")
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
        """Return a stable JSON-friendly representation for provenance/fingerprints."""

        return {
            "journey_id": self.journey_id,
            "operation_key": self.operation_key,
            "now_min": self.now_min,
            "journey_end_min": self.journey_end_min,
            "budget_remaining_minor": self.budget_remaining_minor,
            "current_location_key": self.current_location_key,
            "next_anchor": {
                "semantic_key": self.next_anchor.semantic_key,
                "location_key": self.next_anchor.location_key,
                "start_min": self.next_anchor.start_min,
                "duration_min": self.next_anchor.duration_min,
            },
            "replacement_options": [
                {
                    "option_key": item.option_key,
                    "location_key": item.location_key,
                    "duration_min": item.duration_min,
                    "cost_minor": item.cost_minor,
                    "earliest_start_min": item.earliest_start_min,
                    "latest_end_min": item.latest_end_min,
                    "original_start_min": item.original_start_min,
                    "preference_milli": item.preference_milli,
                }
                for item in self.replacement_options
            ],
            "frozen_mobility_snapshot_id": self.frozen_mobility_snapshot_id,
            "travel_minutes": [
                {"origin": origin, "destination": destination, "minutes": minutes}
                for (origin, destination), minutes in sorted(self.travel_minutes.items())
            ],
            "completed_prefix_keys": list(self.completed_prefix_keys),
            "safety_buffer_min": self.safety_buffer_min,
            "allow_drop": self.allow_drop,
        }


def solver_problem_fingerprint(problem: ReplanSolverProblem) -> str:
    raw = json.dumps(problem.to_payload(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def solver_candidate_fingerprint(problem: ReplanSolverProblem, assignment: str) -> str:
    raw = f"{solver_problem_fingerprint(problem)}|{assignment}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class SolverCandidate:
    option_key: str | None
    location_key: str | None
    start_min: int | None
    end_min: int | None
    cost_minor: int
    inbound_travel_min: int
    outbound_travel_min: int
    schedule_shift_min: int
    preference_milli: int
    dropped: bool
    fingerprint: str
    generator_kind: str
    solver_status: str
    generator_rank: int

    def __post_init__(self) -> None:
        if self.dropped:
            if (
                self.option_key is not None
                or self.start_min is not None
                or self.end_min is not None
            ):
                raise ValueError("drop candidate cannot carry a selected option schedule")
        else:
            if self.option_key is None or self.location_key is None:
                raise ValueError("solver candidate requires a selected option")
            if self.start_min is None or self.end_min is None or self.end_min <= self.start_min:
                raise ValueError("solver candidate schedule is invalid")
        if self.generator_rank < 0:
            raise ValueError("solver candidate rank cannot be negative")

    @property
    def objective_key(self) -> tuple[int, int, int, int, str]:
        return (
            1 if self.dropped else 0,
            self.schedule_shift_min,
            self.inbound_travel_min + self.outbound_travel_min,
            -self.preference_milli,
            self.option_key or "~DROP",
        )
