from __future__ import annotations

from dataclasses import dataclass

from app.planning.ortools_replan_solver import (
    ORToolsUnavailable,
    solve_replacement_candidates,
)
from app.planning.solver_contract import (
    ReplanSolverProblem,
    SolverCandidate,
    solver_problem_fingerprint,
)

VALID_SOLVER_MODES = frozenset({"deterministic", "ortools", "auto", "v2"})


@dataclass(frozen=True)
class SolverAttempt:
    candidates: tuple[SolverCandidate, ...]
    engine: str
    terminal_status: str
    fallback_reason: str | None
    attempted_solves: int
    problem_fingerprint: str
    mobility_snapshot_id: str

    @property
    def used_solver(self) -> bool:
        return self.engine == "ORTOOLS_CP_SAT" and bool(self.candidates)

    def evidence_for(self, candidate: SolverCandidate) -> dict[str, object]:
        if candidate not in self.candidates:
            raise ValueError("candidate does not belong to this solver attempt")
        return {
            "kind": candidate.generator_kind,
            "solver_status": candidate.solver_status,
            "terminal_status": self.terminal_status,
            "problem_fingerprint": self.problem_fingerprint,
            "mobility_snapshot_id": self.mobility_snapshot_id,
            "assignment_fingerprint": candidate.fingerprint,
            "rank": candidate.generator_rank,
        }


def normalize_solver_mode(raw: str | None) -> str:
    value = (raw or "deterministic").strip().lower()
    if value not in VALID_SOLVER_MODES:
        raise ValueError(f"unsupported solver mode: {raw!r}")
    return value


def generate_solver_candidates(
    problem: ReplanSolverProblem,
    *,
    mode: str | None,
    candidate_count: int,
    random_seed: int = 1,
    max_time_seconds: float = 1.0,
) -> SolverAttempt:
    """Attempt optional OR-Tools generation without owning product fallback.

    Empty `candidates` means the caller should use the existing deterministic
    heuristic path. Unexpected programming errors intentionally propagate; only
    expected solver availability/status conditions become controlled fallback.
    """

    normalized = normalize_solver_mode(mode)
    fingerprint = solver_problem_fingerprint(problem)
    base = {
        "problem_fingerprint": fingerprint,
        "mobility_snapshot_id": problem.frozen_mobility_snapshot_id,
    }
    if normalized == "deterministic":
        return SolverAttempt(
            candidates=(),
            engine="DETERMINISTIC_HEURISTIC",
            terminal_status="DISABLED",
            fallback_reason="SOLVER_DISABLED",
            attempted_solves=0,
            **base,
        )

    try:
        run = solve_replacement_candidates(
            problem,
            candidate_count=candidate_count,
            random_seed=random_seed,
            max_time_seconds=max_time_seconds,
        )
    except ORToolsUnavailable:
        return SolverAttempt(
            candidates=(),
            engine="DETERMINISTIC_HEURISTIC",
            terminal_status="UNAVAILABLE",
            fallback_reason="ORTOOLS_UNAVAILABLE",
            attempted_solves=0,
            **base,
        )

    if run.terminal_status in {"UNKNOWN", "MODEL_INVALID"}:
        return SolverAttempt(
            candidates=(),
            engine="DETERMINISTIC_HEURISTIC",
            terminal_status=run.terminal_status,
            fallback_reason=f"ORTOOLS_{run.terminal_status}",
            attempted_solves=run.attempted,
            **base,
        )

    if not run.candidates:
        return SolverAttempt(
            candidates=(),
            engine="DETERMINISTIC_HEURISTIC",
            terminal_status=run.terminal_status,
            fallback_reason=f"ORTOOLS_{run.terminal_status}",
            attempted_solves=run.attempted,
            **base,
        )

    return SolverAttempt(
        candidates=run.candidates,
        engine="ORTOOLS_CP_SAT",
        terminal_status=run.terminal_status,
        fallback_reason=None,
        attempted_solves=run.attempted,
        **base,
    )
