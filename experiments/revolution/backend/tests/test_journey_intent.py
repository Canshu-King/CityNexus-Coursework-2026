from datetime import datetime, timezone

import pytest

from app.agent.journey_intent import (
    canonical_intent_dimension,
    expire_journey_intents,
    intent_scopes_overlap,
    intent_is_active,
    resolve_intent_targets,
    resolve_intent_scope,
    should_require_clarification,
)


NOW = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)


def test_rest_of_today_uses_journey_local_timezone():
    scope = resolve_intent_scope(
        scope_kind="REST_OF_TODAY",
        now=NOW,
        timezone_name="Asia/Tokyo",
    )
    assert scope.local_date == "2026-10-07"
    assert scope.ends_at == "2026-10-07T15:00:00+00:00"


def test_next_morning_is_future_scoped_not_immediately_active():
    scope = resolve_intent_scope(
        scope_kind="NEXT_MORNING",
        now=NOW,
        timezone_name="Asia/Tokyo",
    )
    assert scope.local_date == "2026-10-08"
    assert scope.starts_at == "2026-10-07T20:00:00+00:00"
    assert scope.ends_at == "2026-10-08T03:00:00+00:00"


def test_remaining_journey_may_span_overnight():
    scope = resolve_intent_scope(
        scope_kind="REMAINING_JOURNEY",
        now=NOW,
        timezone_name="Asia/Tokyo",
        journey_end_at="2026-10-10T09:00:00+00:00",
    )
    assert scope.ends_at == "2026-10-10T09:00:00+00:00"
    assert scope.local_date is None


def test_unknown_scope_fails_closed():
    with pytest.raises(ValueError, match="UNRESOLVED_INTENT_SCOPE"):
        resolve_intent_scope(
            scope_kind="SOMETIME",
            now=NOW,
            timezone_name="Asia/Tokyo",
        )


def test_expired_temporary_intent_is_not_active():
    assert intent_is_active(
        status="ACTIVE",
        starts_at="2026-10-06T00:00:00+00:00",
        ends_at="2026-10-07T01:59:59+00:00",
        now=NOW,
    ) is False


def test_active_temporary_intent_is_active_inside_scope():
    assert intent_is_active(
        status="ACTIVE",
        starts_at="2026-10-07T00:00:00+00:00",
        ends_at="2026-10-07T15:00:00+00:00",
        now=NOW,
    ) is True


def test_temporary_pace_and_walking_dimensions_are_not_global_preference_keys():
    assert canonical_intent_dimension(intent_kind="PACE") == "journey-intent:pace"
    assert canonical_intent_dimension(intent_kind="WALKING_LIMIT") == "journey-intent:walking"


def test_accessibility_need_is_not_flattened_to_soft_journey_intent():
    assert canonical_intent_dimension(intent_kind="ACCESSIBILITY_NEED") == "capability"


def test_ambiguous_goal_requires_clarification():
    assert should_require_clarification(
        confidence=0.9,
        scope_resolved=True,
        constraint_count=0,
        intent_kind="GENERAL_GOAL",
    ) is True


def test_grounded_bounded_intent_can_proceed():
    assert should_require_clarification(
        confidence=0.86,
        scope_resolved=True,
        constraint_count=2,
        intent_kind="WALKING_LIMIT",
    ) is False


def test_rest_of_today_targets_only_future_non_current_activities_in_local_timezone():
    activities = [
        {"id": "current", "sequence_no": 1, "status": "IN_PROGRESS", "start_at": "2026-10-07T01:00:00+00:00"},
        {"id": "today-next", "sequence_no": 2, "status": "PLANNED", "start_at": "2026-10-07T04:00:00+00:00"},
        {"id": "tomorrow", "sequence_no": 3, "status": "PLANNED", "start_at": "2026-10-07T16:00:00+00:00"},
        {"id": "done", "sequence_no": 4, "status": "COMPLETED", "start_at": "2026-10-07T05:00:00+00:00"},
    ]
    targets = resolve_intent_targets(
        scope_kind="REST_OF_TODAY",
        activities=activities,
        now=NOW,
        timezone_name="Asia/Tokyo",
        current_activity_id="current",
    )
    assert targets == ["today-next"]


def test_next_stop_scope_expires_when_its_server_resolved_target_is_terminal():
    from types import SimpleNamespace

    row = SimpleNamespace(
        status="ACTIVE",
        scope_end_at=None,
        scope_kind="NEXT_STOP",
        target_activity_ids_json='["stop-2"]',
        expired_at=None,
        updated_at=None,
    )

    class Result:
        def all(self):
            return [row]

    class Session:
        def scalars(self, _query):
            return Result()

        def get(self, _model, _key):
            return SimpleNamespace(status="COMPLETED")

        def flush(self):
            pass

    changed = expire_journey_intents(
        Session(),
        SimpleNamespace(id="journey-1", status="ACTIVE"),
        now=NOW,
    )
    assert changed == 1
    assert row.status == "EXPIRED"
    assert row.expired_at == NOW.isoformat()


def test_overlapping_temporary_scopes_can_supersede_same_dimension():
    assert intent_scopes_overlap(
        left_kind="REST_OF_TODAY",
        left_start="2026-10-07T00:00:00+00:00",
        left_end="2026-10-08T00:00:00+00:00",
        left_targets=["a", "b"],
        right_kind="REMAINING_JOURNEY",
        right_start="2026-10-07T02:00:00+00:00",
        right_end="2026-10-09T00:00:00+00:00",
        right_targets=["b", "c"],
    )
