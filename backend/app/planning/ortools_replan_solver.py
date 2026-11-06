from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.planning.solver_contract import (
    ReplanSolverProblem,
    SolverCandidate,
    solver_candidate_fingerprint,
)


class ORToolsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ORToolsRun:
    candidates: tuple[SolverCandidate, ...]
    terminal_status: str
    attempted: int


def _status_name(cp_model: Any, status: int) -> str:
    names = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    return names.get(status, str(status))


def solve_replacement_candidates(
    problem: ReplanSolverProblem,
    *,
    candidate_count: int,
    random_seed: int = 1,
    max_time_seconds: float = 1.0,
) -> ORToolsRun:
    """Solve the V1 replacement-window problem with CP-SAT.

    This module is intentionally optional. The caller owns fallback and MUST
    still run every materialized output through CityNexus deterministic
    feasibility before creating a PRESENTED ReplanProposal.
    """

    if candidate_count < 1:
        raise ValueError("candidate_count must be positive")
    if max_time_seconds <= 0:
        raise ValueError("max_time_seconds must be positive")
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ORToolsUnavailable("ortools is not installed") from exc

    excluded_options: set[str] = set()
    exclude_drop = False
    candidates: list[SolverCandidate] = []
    terminal_status = "INFEASIBLE"
    attempted = 0

    for rank in range(candidate_count):
        attempted += 1
        model = cp_model.CpModel()
        selectors: dict[str, Any] = {}
        starts: dict[str, Any] = {}
        shifts: dict[str, Any] = {}
        travel_costs: dict[str, int] = {}
        available_options = []
        anchor = problem.next_anchor

        for option in sorted(problem.replacement_options, key=lambda item: item.option_key):
            if option.option_key in excluded_options:
                continue
            try:
                inbound = problem.travel(problem.current_location_key, option.location_key)
                outbound = problem.travel(option.location_key, anchor.location_key)
            except KeyError:
                continue
            earliest = max(problem.now_min + inbound, option.earliest_start_min)
            latest_start = min(
                option.latest_end_min - option.duration_min,
                anchor.start_min - problem.safety_buffer_min - outbound - option.duration_min,
                problem.journey_end_min - option.duration_min,
            )
            if earliest > latest_start or option.cost_minor > problem.budget_remaining_minor:
                continue

            selected = model.new_bool_var(f"selected_{len(available_options)}")
            start = model.new_int_var(0, problem.journey_end_min, f"start_{len(available_options)}")
            shift = model.new_int_var(0, problem.journey_end_min, f"shift_{len(available_options)}")
            model.add(start >= earliest).only_enforce_if(selected)
            model.add(start <= latest_start).only_enforce_if(selected)
            # Pin unused variables to a stable value so the model has no irrelevant freedom.
            model.add(start == option.original_start_min).only_enforce_if(selected.Not())
            model.add_abs_equality(shift, start - option.original_start_min)
            selectors[option.option_key] = selected
            starts[option.option_key] = start
            shifts[option.option_key] = shift
            travel_costs[option.option_key] = inbound + outbound
            available_options.append((option, inbound, outbound))

        drop = None
        direct_to_anchor = None
        if problem.allow_drop and not exclude_drop:
            try:
                direct_to_anchor = problem.travel(
                    problem.current_location_key, problem.next_anchor.location_key
                )
            except KeyError:
                direct_to_anchor = None
            if (
                direct_to_anchor is not None
                and problem.now_min + direct_to_anchor + problem.safety_buffer_min
                <= problem.next_anchor.start_min
            ):
                drop = model.new_bool_var("drop")

        choice_vars = list(selectors.values()) + ([drop] if drop is not None else [])
        if not choice_vars:
            terminal_status = "INFEASIBLE"
            break
        model.add(sum(choice_vars) == 1)
        model.add(
            sum(
                selectors[option.option_key] * option.cost_minor
                for option, _inbound, _outbound in available_options
            )
            <= problem.budget_remaining_minor
        )

        # V1 objective mirrors SolverCandidate.objective_key lexicographically:
        # keep an activity, minimize schedule movement, then frozen travel, then
        # maximize already-verified preference, then stable option key.  Dynamic
        # integer weights make the hierarchy explicit instead of relying on
        # coincidental constants (for example, a 1-minute travel delta must not
        # tie a 1000-point preference delta). Final CityNexus verified scoring
        # still ranks post-gate proposals.
        sorted_options = sorted(
            (item[0] for item in available_options),
            key=lambda item: item.option_key,
        )
        option_rank = {item.option_key: rank for rank, item in enumerate(sorted_options)}
        tie_width = max(2, len(sorted_options) + 1)
        preference_width = 1001 * tie_width
        travel_weight = preference_width + 1
        max_travel = max(travel_costs.values(), default=0)
        shift_weight = (max_travel + 1) * travel_weight + preference_width + tie_width
        drop_penalty = (
            (problem.journey_end_min + 1) * shift_weight
            + (max_travel + 1) * travel_weight
            + preference_width
            + tie_width
        )

        objective_terms = []
        if drop is not None:
            objective_terms.append(drop * drop_penalty)
        for option, _inbound, _outbound in available_options:
            selected = selectors[option.option_key]
            # shift is zero only when original start is chosen. Because the unused
            # start is pinned to original_start_min, it contributes zero when not selected.
            objective_terms.append(shifts[option.option_key] * shift_weight)
            objective_terms.append(selected * travel_costs[option.option_key] * travel_weight)
            objective_terms.append(selected * (1000 - option.preference_milli) * tie_width)
            objective_terms.append(selected * option_rank[option.option_key])
        model.minimize(sum(objective_terms))

        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = int(random_seed)
        solver.parameters.max_time_in_seconds = float(max_time_seconds)
        # Deterministic-time budget makes repeated CI runs less wall-clock sensitive.
        if hasattr(solver.parameters, "max_deterministic_time"):
            solver.parameters.max_deterministic_time = float(max_time_seconds)
        status = solver.solve(model)
        terminal_status = _status_name(cp_model, status)
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            break

        chosen = [key for key, selected in selectors.items() if solver.value(selected)]
        if chosen:
            key = min(chosen)
            option, inbound, outbound = next(
                item for item in available_options if item[0].option_key == key
            )
            start = int(solver.value(starts[key]))
            candidate = SolverCandidate(
                option_key=option.option_key,
                location_key=option.location_key,
                start_min=start,
                end_min=start + option.duration_min,
                cost_minor=option.cost_minor,
                inbound_travel_min=inbound,
                outbound_travel_min=outbound,
                schedule_shift_min=abs(start - option.original_start_min),
                preference_milli=option.preference_milli,
                dropped=False,
                fingerprint=solver_candidate_fingerprint(problem, f"{option.option_key}@{start}"),
                generator_kind="ORTOOLS_CP_SAT",
                solver_status=terminal_status,
                generator_rank=rank,
            )
            candidates.append(candidate)
            excluded_options.add(key)
            continue

        if drop is not None and solver.value(drop):
            candidates.append(
                SolverCandidate(
                    option_key=None,
                    location_key=None,
                    start_min=None,
                    end_min=None,
                    cost_minor=0,
                    inbound_travel_min=0,
                    outbound_travel_min=int(direct_to_anchor or 0),
                    schedule_shift_min=0,
                    preference_milli=0,
                    dropped=True,
                    fingerprint=solver_candidate_fingerprint(problem, "DROP"),
                    generator_kind="ORTOOLS_CP_SAT",
                    solver_status=terminal_status,
                    generator_rank=rank,
                )
            )
            exclude_drop = True
            continue

        terminal_status = "UNKNOWN"
        break

    return ORToolsRun(tuple(candidates), terminal_status, attempted)
