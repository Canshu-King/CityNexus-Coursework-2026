from __future__ import annotations

import json
from types import SimpleNamespace

import app.agent.mission as mission_module
from app.agent import delegation_policy


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


def _open_policy(monkeypatch):
    monkeypatch.setattr(
        delegation_policy,
        "get_or_create_policy",
        lambda _session, _traveler_id: object(),
    )
    monkeypatch.setattr(
        delegation_policy,
        "policy_payload",
        lambda _row: {
            "max_auto_replan_cost_delta_minor": 100000,
            "max_auto_loss_minor": 100000,
            "max_auto_schedule_drift_min": 1440,
            "version": 1,
            "mode": "AUTONOMOUS",
        },
    )


def _base_spec() -> mission_module.MissionSpec:
    return mission_module.MissionSpec(
        objective_codes=("PROTECT_EXPLICIT_ACTIVITIES",),
        protected_activity_ids=("act-dinner",),
        mutable_activity_ids=(),
        max_cost_delta_minor=5000,
        max_loss_minor=0,
        max_schedule_drift_min=30,
        preferences={},
        requested_autonomy="EXECUTE_WITHIN_BOUNDS",
    )


def test_v5_01_model_cannot_expand_explicit_numeric_authority(monkeypatch):
    _open_policy(monkeypatch)
    monkeypatch.setattr(mission_module, "compile_mission", lambda *args, **kwargs: _base_spec())
    monkeypatch.setattr(
        mission_module,
        "_model_draft",
        lambda _command: mission_module.MissionIntentDraft(
            max_cost_delta_minor=50000,
            max_loss_minor=10000,
            max_schedule_drift_min=300,
            confidence=0.9,
            reasoning_summary="incorrectly widened authority",
        ),
    )
    monkeypatch.setattr(mission_module, "_resolve_model_references", lambda *args, **kwargs: ())

    row = mission_module.create_mission(
        _FakeSession(),
        SimpleNamespace(id="j1", traveler_id="t1"),
        command="晚餐别动，最多多花五十元，最多延后三十分钟。",
        requested_autonomy="EXECUTE_WITHIN_BOUNDS",
        trigger_event_id=None,
    )

    resolved = json.loads(row.resolved_intent_json)
    assert resolved["max_cost_delta_minor"] <= 5000
    assert resolved["max_loss_minor"] <= 0
    assert resolved["max_schedule_drift_min"] <= 30


def test_v5_02_model_mutable_guess_cannot_erase_explicit_protection(monkeypatch):
    _open_policy(monkeypatch)
    monkeypatch.setattr(mission_module, "compile_mission", lambda *args, **kwargs: _base_spec())
    monkeypatch.setattr(
        mission_module,
        "_model_draft",
        lambda _command: mission_module.MissionIntentDraft(
            mutable_activity_references=["dinner"],
            max_cost_delta_minor=5000,
            max_schedule_drift_min=30,
            confidence=0.9,
            reasoning_summary="incorrectly interpreted dinner as mutable",
        ),
    )
    monkeypatch.setattr(
        mission_module,
        "_resolve_model_references",
        lambda _session, _journey, refs: ("act-dinner",) if refs else (),
    )

    row = mission_module.create_mission(
        _FakeSession(),
        SimpleNamespace(id="j1", traveler_id="t1"),
        command="晚餐别动，最多多花五十元。",
        requested_autonomy="EXECUTE_WITHIN_BOUNDS",
        trigger_event_id=None,
    )

    protected = set(json.loads(row.protected_activity_ids_json))
    mutable = set(json.loads(row.mutable_activity_ids_json))
    assert "act-dinner" in protected
    assert "act-dinner" not in mutable


def test_v5_03_unresolved_model_references_are_persisted(monkeypatch):
    _open_policy(monkeypatch)
    monkeypatch.setattr(mission_module, "compile_mission", lambda *args, **kwargs: _base_spec())
    monkeypatch.setattr(
        mission_module,
        "_model_draft",
        lambda _command: mission_module.MissionIntentDraft(
            protected_activity_references=["不存在的月宫"],
            max_cost_delta_minor=5000,
            max_schedule_drift_min=30,
            confidence=0.7,
            reasoning_summary="reference could not be grounded",
        ),
    )
    monkeypatch.setattr(mission_module, "_resolve_model_references", lambda *args, **kwargs: ())

    row = mission_module.create_mission(
        _FakeSession(),
        SimpleNamespace(id="j1", traveler_id="t1"),
        command="不存在的月宫别动，最多多花五十元。",
        requested_autonomy="EXECUTE_WITHIN_BOUNDS",
        trigger_event_id=None,
    )

    unresolved = json.loads(row.unresolved_references_json)
    assert "不存在的月宫" in unresolved
