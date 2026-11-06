from __future__ import annotations

from typing import Sequence

from app.planning.adapter import PlannerVariant


class ORToolsVariantSelector:
    """Small optional bridge proving the OR-Tools dependency boundary.

    This is not the final Journey scheduler. It chooses one already-generated
    variant using the same deterministic objective ordering. The real WP06+
    solver may later model activities/time windows, but every output must still
    pass CityNexus's authoritative feasibility gate.
    """

    planner_key = "ortools-variant-selector"

    def choose(self, variants: Sequence[PlannerVariant]) -> PlannerVariant | None:
        feasible = [item for item in variants if item.feasible]
        if not feasible:
            return None
        try:
            from ortools.sat.python import cp_model
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("ortools is not installed") from exc

        model = cp_model.CpModel()
        choose = [model.new_bool_var(f"choose_{i}") for i in range(len(feasible))]
        model.add(sum(choose) == 1)

        # Lexicographic objective encoded safely for the small bridge:
        # utility dominates disruption; stable key tie-break happens after solve.
        max_disruption = max((item.disruption_count for item in feasible), default=0)
        scale = max_disruption + 2
        objective = sum(
            choose[i] * (item.utility_milli * scale - item.disruption_count)
            for i, item in enumerate(feasible)
        )
        model.maximize(objective)
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        status = solver.solve(model)
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            return None
        selected = [item for i, item in enumerate(feasible) if solver.value(choose[i])]
        return min(selected, key=lambda item: item.key)
