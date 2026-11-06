"""Mode-per-concrete-leg CP-SAT boundary for stay-aware Journey plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ModeChoiceResult:
    selected: tuple[Any, ...]
    objective_value: int
    solver_status: str
    optimizer_kind: str
    unknown_paid_cost_selected: bool = False


def solve_mode_choices(
    facts: Iterable[Any],
    *,
    budget_minor: int,
    max_walking_min: int = 10_000,
    weather_state: str = "NORMAL",
    learned_dimensions: set[str] | None = None,
    allow_unknown_paid: bool = False,
) -> ModeChoiceResult:
    """Choose exactly one available mode for each concrete Journey leg.

    `FREE_KNOWN` is the only nullable-price state eligible for automatic
    selection. A paid mode with `UNKNOWN` cost is retained as evidence but is
    excluded unless the caller explicitly requests a confirmation-gated plan.
    """

    rows = list(facts)
    leg_keys = sorted({f"{row.from_key}->{row.to_key}" for row in rows})
    if not leg_keys:
        return ModeChoiceResult((), 0, "EMPTY", "BOUNDED_DETERMINISTIC")
    by_leg: dict[str, list[Any]] = {key: [] for key in leg_keys}
    for row in rows:
        by_leg[f"{row.from_key}->{row.to_key}"].append(row)
    learned = learned_dimensions or set()

    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return _deterministic(
            by_leg,
            budget_minor,
            max_walking_min,
            weather_state,
            learned,
            allow_unknown_paid,
        )

    model = cp_model.CpModel()
    variables: dict[tuple[str, int], Any] = {}
    terms: list[Any] = []
    cost_terms: list[Any] = []
    walking_terms: list[Any] = []
    for leg_key, choices in by_leg.items():
        literals = []
        for index, row in enumerate(choices):
            literal = model.NewBoolVar(f"mode_{index}_{len(variables)}")
            variables[(leg_key, index)] = literal
            literals.append(literal)
            score = _objective_cost(row, weather_state, learned)
            terms.append(score * literal)
            cost_terms.append(_budget_cost(row) * literal)
            walking_terms.append(int(row.walking_min) * literal)
            if _unknown_paid(row) and not allow_unknown_paid:
                model.Add(literal == 0)
        model.AddExactlyOne(literals)
    model.Add(sum(cost_terms) <= max(0, int(budget_minor)))
    model.Add(sum(walking_terms) <= max(0, int(max_walking_min)))
    model.Minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 0.4
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return _deterministic(
            by_leg,
            budget_minor,
            max_walking_min,
            weather_state,
            learned,
            allow_unknown_paid,
        )
    selected = tuple(
        choices[index]
        for leg_key, choices in by_leg.items()
        for index in range(len(choices))
        if solver.Value(variables[(leg_key, index)])
    )
    return ModeChoiceResult(
        selected=selected,
        objective_value=int(solver.ObjectiveValue()),
        solver_status="OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
        optimizer_kind="ORTOOLS_MODE_AWARE_CP_SAT",
        unknown_paid_cost_selected=any(_unknown_paid(row) for row in selected),
    )


def _objective_cost(row: Any, weather_state: str, learned: set[str]) -> int:
    duration = int(row.duration_min) * 100
    walking = int(row.walking_min) * (42 if "low_walking" in learned else 18)
    transfers = int(row.transfers) * 35
    exposure = int(row.duration_min) * (
        35 if weather_state in {"RAIN", "SEVERE_RAIN"} and row.weather_exposure == "HIGH" else 4
    )
    reliability = int((1.0 - float(row.reliability_score)) * 500)
    # Monetary evidence is a real soft objective, not a synthetic time-to-money
    # conversion.  Keeping the captured minor-unit value visible in the score
    # makes the solver favor metro over a materially more expensive taxi when
    # their post-weather feasibility is otherwise comparable.
    monetary = _budget_cost(row)
    unknown_penalty = 1_000_000 if _unknown_paid(row) else 0
    return duration + walking + transfers + exposure + reliability + monetary + unknown_penalty


def _unknown_paid(row: Any) -> bool:
    return str(row.cost_status) == "UNKNOWN" and bool(getattr(row, "paid_mode", True))


def _budget_cost(row: Any) -> int:
    status = str(row.cost_status)
    if status == "FREE_KNOWN":
        return 0
    if status in {"KNOWN", "KNOWN_CAPTURED", "ESTIMATED"} and row.cost_amount_minor is not None:
        return int(row.cost_amount_minor)
    # Unknown paid cost is not represented as zero. The sentinel makes it
    # infeasible under normal envelopes even when a caller allows it for a
    # confirmation-only proposal.
    return 1_000_000_000


def _deterministic(
    by_leg: dict[str, list[Any]],
    budget_minor: int,
    max_walking_min: int,
    weather_state: str,
    learned: set[str],
    allow_unknown_paid: bool,
) -> ModeChoiceResult:
    selected: list[Any] = []
    for choices in by_leg.values():
        feasible = [
            row
            for row in choices
            if int(row.walking_min) <= max_walking_min
            and (allow_unknown_paid or not _unknown_paid(row))
            and sum(_budget_cost(item) for item in selected) + _budget_cost(row) <= budget_minor
        ]
        if not feasible:
            raise ValueError("no cost-grounded mode satisfies the Journey leg envelope")
        selected.append(min(feasible, key=lambda row: _objective_cost(row, weather_state, learned)))
    return ModeChoiceResult(
        tuple(selected),
        sum(_objective_cost(row, weather_state, learned) for row in selected),
        "DETERMINISTIC",
        "BOUNDED_DETERMINISTIC",
        any(_unknown_paid(row) for row in selected),
    )
