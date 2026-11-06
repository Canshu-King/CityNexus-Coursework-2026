from __future__ import annotations

import pytest
from test_solver_reference_contract import _problem

import app.planning.solver_runtime as runtime
from app.planning.ortools_replan_solver import ORToolsRun, ORToolsUnavailable
from app.planning.reference_replan_solver import enumerate_reference_candidates


def test_deterministic_mode_never_attempts_ortools(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        runtime,
        "solve_replacement_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    result = runtime.generate_solver_candidates(_problem(), mode="deterministic", candidate_count=2)

    assert result.candidates == ()
    assert result.engine == "DETERMINISTIC_HEURISTIC"
    assert result.fallback_reason == "SOLVER_DISABLED"
    assert result.attempted_solves == 0


def test_expected_missing_dependency_becomes_controlled_fallback(monkeypatch: pytest.MonkeyPatch):
    def unavailable(*_args, **_kwargs):
        raise ORToolsUnavailable("missing")

    monkeypatch.setattr(runtime, "solve_replacement_candidates", unavailable)
    result = runtime.generate_solver_candidates(_problem(), mode="auto", candidate_count=2)

    assert result.candidates == ()
    assert result.terminal_status == "UNAVAILABLE"
    assert result.fallback_reason == "ORTOOLS_UNAVAILABLE"


def test_successful_solver_attempt_exposes_auditable_evidence(monkeypatch: pytest.MonkeyPatch):
    candidates = enumerate_reference_candidates(_problem(), candidate_count=2)
    candidates = tuple(
        type(item)(
            **{
                **item.__dict__,
                "generator_kind": "ORTOOLS_CP_SAT",
                "solver_status": "OPTIMAL",
            }
        )
        for item in candidates
    )
    monkeypatch.setattr(
        runtime,
        "solve_replacement_candidates",
        lambda *_args, **_kwargs: ORToolsRun(candidates, "OPTIMAL", 2),
    )

    result = runtime.generate_solver_candidates(_problem(), mode="ortools", candidate_count=2)
    evidence = result.evidence_for(result.candidates[0])

    assert result.used_solver
    assert result.fallback_reason is None
    assert evidence["kind"] == "ORTOOLS_CP_SAT"
    assert evidence["mobility_snapshot_id"] == "mobility-snapshot-1"
    assert evidence["problem_fingerprint"] == result.problem_fingerprint


def test_controlled_zero_candidate_status_falls_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        runtime,
        "solve_replacement_candidates",
        lambda *_args, **_kwargs: ORToolsRun((), "UNKNOWN", 1),
    )

    result = runtime.generate_solver_candidates(_problem(), mode="auto", candidate_count=2)

    assert result.candidates == ()
    assert result.fallback_reason == "ORTOOLS_UNKNOWN"
    assert result.attempted_solves == 1


def test_unexpected_solver_programming_error_is_not_silently_hidden(
    monkeypatch: pytest.MonkeyPatch,
):
    def broken(*_args, **_kwargs):
        raise RuntimeError("programming defect")

    monkeypatch.setattr(runtime, "solve_replacement_candidates", broken)

    with pytest.raises(RuntimeError, match="programming defect"):
        runtime.generate_solver_candidates(_problem(), mode="auto", candidate_count=2)


def test_invalid_solver_mode_fails_closed():
    with pytest.raises(ValueError, match="unsupported solver mode"):
        runtime.generate_solver_candidates(_problem(), mode="magic", candidate_count=1)
