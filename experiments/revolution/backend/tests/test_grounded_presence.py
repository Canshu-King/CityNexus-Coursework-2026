from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.db.models import Activity, LocationObservation, Place
from app.execution.presence import presence_projection


class _Session:
    def __init__(self, *, activity, place, location):
        self.activity = activity
        self.place = place
        self.location = location

    def get(self, model, row_id):
        if model is Activity and self.activity is not None and str(self.activity.id) == str(row_id):
            return self.activity
        if model is Place and self.place is not None and str(self.place.id) == str(row_id):
            return self.place
        return None

    def scalar(self, _statement):
        if self.location is None:
            return None
        parameters = set(_statement.compile().params.values())
        if str(self.location.journey_id) not in {str(item) for item in parameters}:
            return None
        if str(self.location.journey_member_id) not in {str(item) for item in parameters}:
            return None
        return self.location


def _journey(activity_id="a1"):
    return SimpleNamespace(
        id="j1",
        active_plan_version_id="p1",
        current_activity_id=activity_id,
    )


def _activity():
    return SimpleNamespace(
        id="a1",
        plan_version_id="p1",
        place_id="place-1",
        status="ACTIVE",
    )


def _place():
    return SimpleNamespace(id="place-1", latitude=35.0, longitude=135.0)


def _location(now, *, source="FOREGROUND_WEB", latitude=35.0001, longitude=135.0001, accuracy=20.0):
    return SimpleNamespace(
        id="loc-1",
        journey_id="j1",
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy,
        captured_at=now.isoformat(),
        source=source,
        journey_member_id="member-1",
    )


def test_foreground_location_near_target_is_advisory_only():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    result = presence_projection(
        _Session(activity=_activity(), place=_place(), location=_location(now)),
        _journey(),
        viewer_member_id="member-1",
        now=now,
    )

    assert result["state"] == "NEAR_TARGET"
    assert result["authority"] == "ADVISORY_FOREGROUND"
    assert result["arrival_transition_allowed"] is False
    assert result["reason_code"] == "FOREGROUND_LOCATION_NEAR_TARGET_NOT_EXECUTION_TRUTH"


def test_trusted_location_near_target_is_eligible_not_auto_transitioned():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    result = presence_projection(
        _Session(
            activity=_activity(),
            place=_place(),
            location=_location(now, source="DEVICE_ATTESTED"),
        ),
        _journey(),
        viewer_member_id="member-1",
        now=now,
    )

    assert result["state"] == "VERIFIED_AT_TARGET"
    assert result["authority"] == "TRUSTED_LOCATION"
    assert result["arrival_transition_allowed"] is True


def test_stale_location_fails_closed():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    stale = _location(now - timedelta(minutes=10))
    result = presence_projection(
        _Session(activity=_activity(), place=_place(), location=stale),
        _journey(),
        viewer_member_id="member-1",
        now=now,
    )

    assert result["state"] == "LOCATION_STALE"
    assert result["arrival_transition_allowed"] is False


def test_inaccurate_location_fails_closed():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    imprecise = _location(now, accuracy=250.0)
    result = presence_projection(
        _Session(activity=_activity(), place=_place(), location=imprecise),
        _journey(),
        viewer_member_id="member-1",
        now=now,
    )

    assert result["state"] == "LOCATION_IMPRECISE"
    assert result["arrival_transition_allowed"] is False


def test_foreground_location_far_from_target_is_not_an_incident():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    far = _location(now, latitude=35.1, longitude=135.1)
    result = presence_projection(
        _Session(activity=_activity(), place=_place(), location=far),
        _journey(),
        viewer_member_id="member-1",
        now=now,
    )

    assert result["state"] == "AWAY_FROM_TARGET"
    assert result["authority"] == "ADVISORY_FOREGROUND"
    assert result["arrival_transition_allowed"] is False


def test_location_from_another_journey_is_not_considered():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    foreign = _location(now)
    foreign.journey_id = "some-other-journey"
    result = presence_projection(
        _Session(activity=_activity(), place=_place(), location=foreign),
        _journey(),
        viewer_member_id="member-1",
        now=now,
    )

    assert result["state"] == "LOCATION_UNAVAILABLE"
    assert result["location_observation_id"] is None
    assert result["arrival_transition_allowed"] is False


def test_activity_without_exact_place_is_unbound():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    unbound = _activity()
    unbound.place_id = None
    result = presence_projection(
        _Session(activity=unbound, place=_place(), location=_location(now)),
        _journey(),
        viewer_member_id="member-1",
        now=now,
    )

    assert result["state"] == "TARGET_UNBOUND"
    assert result["arrival_transition_allowed"] is False


def test_unknown_activity_id_is_not_rebound_to_another_place():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    result = presence_projection(
        _Session(activity=_activity(), place=_place(), location=_location(now)),
        _journey(),
        viewer_member_id="member-1",
        target_activity_id="activity-from-another-plan",
        now=now,
    )

    assert result["state"] == "TARGET_UNBOUND"
    assert result["arrival_transition_allowed"] is False


def test_location_projection_requires_the_matching_signed_member():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    location = _location(now)
    session = _Session(activity=_activity(), place=_place(), location=location)

    unscoped = presence_projection(session, _journey(), now=now)
    other_member = presence_projection(
        session, _journey(), viewer_member_id="member-2", now=now
    )

    assert unscoped["state"] == "LOCATION_UNAVAILABLE"
    assert unscoped["reason_code"] == "SIGNED_MEMBER_IDENTITY_REQUIRED"
    assert other_member["state"] == "LOCATION_UNAVAILABLE"
    assert unscoped["location_observation_id"] is None
    assert other_member["location_observation_id"] is None
