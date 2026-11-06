from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.db.models import AccommodationOfferSnapshot, JourneyLaunchContext, JourneyTripContext, StayJourneyOption
from app.execution.launch_origin import grounded_launch_origin, launch_context_projection, launch_edge_projection


class _Rows:
    def __init__(self, row):
        self.row = row

    def all(self):
        return [self.row] if self.row is not None else []


class _Session:
    def __init__(self, *, location=None, stay_option=None, stay_offer=None, trip_context=None, launch_contract=None):
        self.location = location
        self.stay_option = stay_option
        self.stay_offer = stay_offer
        self.trip_context = trip_context
        self.launch_contract = launch_contract

    def scalar(self, statement):
        text = str(statement)
        if "location_observations" in text:
            return self.location
        if "stay_journey_options" in text:
            return self.stay_option
        return None

    def get(self, model, row_id):
        if model is AccommodationOfferSnapshot and self.stay_offer is not None:
            return self.stay_offer if str(self.stay_offer.id) == str(row_id) else None
        if model is JourneyTripContext:
            return self.trip_context
        if model is JourneyLaunchContext:
            return self.launch_contract
        return None


class _FactRows:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class _FactSession(_Session):
    def __init__(self, fact):
        super().__init__()
        self.fact = fact

    def scalars(self, statement):
        return _FactRows(self.fact)

def _journey():
    return SimpleNamespace(id="j1")


def _target(now):
    return SimpleNamespace(
        id="a1",
        start_at=(now + timedelta(hours=2)).isoformat(),
        place_id="target-place",
        title="Target",
    )


def test_fresh_foreground_location_wins_but_stays_advisory():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    location = SimpleNamespace(
        id="loc1",
        latitude=35.0,
        longitude=135.0,
        accuracy_m=20.0,
        captured_at=now.isoformat(),
    )
    result = grounded_launch_origin(
        _Session(location=location),
        _journey(),
        target_activity=_target(now),
        now=now,
    )

    assert result["source"] == "FOREGROUND_LOCATION"
    assert result["authority"] == "ADVISORY_FOREGROUND"


def test_selected_stay_covering_target_beats_arrival_anchor():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    option = SimpleNamespace(
        id="stay-option",
        accommodation_offer_id="offer1",
        status="SELECTED",
        rank=1,
    )
    offer = SimpleNamespace(
        id="offer1",
        property_name="Hotel",
        latitude=34.9,
        longitude=135.7,
        check_in_at=(now - timedelta(hours=8)).isoformat(),
        check_out_at=(now + timedelta(hours=12)).isoformat(),
        captured_at=(now - timedelta(hours=24)).isoformat(),
        provider_key="hotel-provider",
    )
    context = SimpleNamespace(
        arrival_anchor_json='{"at":"2026-10-05T23:00:00+00:00","location":"Station","latitude":34.8,"longitude":135.6}'
    )
    result = grounded_launch_origin(
        _Session(stay_option=option, stay_offer=offer, trip_context=context),
        _journey(),
        target_activity=_target(now),
        now=now,
    )

    assert result["source"] == "SELECTED_STAY"
    assert result["reason_code"] == "SELECTED_STAY_COVERS_TARGET_START"


def test_future_hotel_checkin_does_not_replace_arrival_anchor():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    option = SimpleNamespace(
        id="stay-option",
        accommodation_offer_id="offer1",
        status="SELECTED",
        rank=1,
    )
    offer = SimpleNamespace(
        id="offer1",
        property_name="Hotel",
        latitude=34.9,
        longitude=135.7,
        check_in_at=(now + timedelta(hours=6)).isoformat(),
        check_out_at=(now + timedelta(hours=30)).isoformat(),
        captured_at=now.isoformat(),
        provider_key="hotel-provider",
    )
    context = SimpleNamespace(
        arrival_anchor_json=(
            '{"at":"2026-10-06T08:00:00+00:00","location":"Station",'
            '"latitude":34.8,"longitude":135.6,"reservation_id":"r1"}'
        )
    )
    result = grounded_launch_origin(
        _Session(stay_option=option, stay_offer=offer, trip_context=context),
        _journey(),
        target_activity=_target(now),
        now=now,
    )

    assert result["source"] == "ARRIVAL_ANCHOR"


def test_no_grounded_source_stays_unknown():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    result = grounded_launch_origin(
        _Session(),
        _journey(),
        target_activity=_target(now),
        now=now,
    )

    assert result["state"] == "UNKNOWN_ORIGIN"
    assert result["origin_key"] is None


def test_unknown_origin_never_invents_launch_edge():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    result = launch_context_projection(_Session(), _journey(), target_activity=_target(now), now=now)

    assert result["origin"]["state"] == "UNKNOWN_ORIGIN"
    assert result["edge"]["state"] == "LAUNCH_EDGE_UNAVAILABLE"
    assert result["edge"]["duration_min"] is None


def test_exact_mobility_fact_is_the_only_launch_edge_match():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    fact = SimpleNamespace(
        id="fact1",
        provider_key="provider",
        provenance="LIVE_CAPTURE",
        captured_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        duration_min=18,
        distance_m=1200,
        mode="WALK",
        walking_min=18,
        evidence_json='{"degraded": false}',
        geometry_json='{}',
    )
    origin = {"state": "READY", "origin_key": "stay-offer:offer1"}
    target = _target(now)
    result = launch_edge_projection(
        _FactSession(fact),
        _journey(),
        origin=origin,
        target_activity=target,
        now=now,
    )

    assert result["state"] == "LAUNCH_ROUTE_READY"
    assert result["duration_min"] == 18
    assert result["target_key"] == "target-place"


def test_executing_previous_activity_beats_confirmed_planned_origin():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    contract = SimpleNamespace(
        authority_kind="USER_CONFIRMED_PLANNED_ORIGIN",
        target_activity_id="a1",
        place_id="planned-place",
        display_name="Planned start",
        latitude=34.6,
        longitude=135.8,
        valid_from=None,
        valid_until=(now + timedelta(days=1)).isoformat(),
    )
    previous = SimpleNamespace(id="previous", place_id="executed-place", title="Current stop")
    result = grounded_launch_origin(
        _Session(launch_contract=contract),
        _journey(),
        target_activity=_target(now),
        previous_activity=previous,
        now=now,
    )

    assert result["source"] == "PREVIOUS_ACTIVITY"
    assert result["origin_key"] == "executed-place"
