"""Permanent regressions for six real defects fixed in the V19.2.2 worktree.

Each test is written to fail if its fix is reverted:

1. ``GET /api/v1/places`` must hide server-owned structural Places unless
   ``include_structural=true``.
2. ``_generic_compose_plans`` must spread explicit POIs across the trip's
   calendar days instead of cramming them into day 1.
3. ``_reschedule_with_carried`` must advance a candidate that lands on the
   previous day into the flexible stop's own day instead of skipping the free
   interval and raising ``NO_SAME_DAY_SLOT``.
4. ``generate_stay_options`` must catch a per-offer ``StayItineraryConflict``,
   record it in ``rejected`` and keep producing options for the other offers.
5. A manually entered ``STRUCTURED_JSON`` booking must reach semantic intake,
   be tagged ``artifact_source="USER_ENTERED"``, have free-text endpoints
   normalised, and be auto-applicable.
6. ``_replan_estimate`` must capture a missing mobility pair on demand instead
   of letting a ``KeyError`` escape.

Everything runs offline against the shared test database.  No production code
is imported twice and ``importlib.reload`` is never used.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.stay_mobility as sm
from app.db.models import (
    AccommodationOfferSnapshot,
    Activity,
    Journey,
    Place,
    PlanVersion,
)
from app.db.session import SessionLocal
from app.main import _replan_estimate, app
from app.stay_mobility import (
    GraphNode,
    StayItineraryConflict,
    _reschedule_with_carried,
    build_candidate_nodes,
    generate_stay_options,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = "+08:00"


def _tag(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _reset() -> dict[str, str]:
    response = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert response.status_code == 200, response.text
    return response.json()


def _create_journey(
    traveler_id: str,
    *,
    city_code: str,
    start_at: str,
    end_at: str,
    title: str = "regression journey",
) -> str:
    response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler_id,
            "title": title,
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": start_at,
            "end_at": end_at,
            "budget": {"amount_minor": 300000, "currency": "CNY"},
            "stay_budget": {"amount_minor": 100000, "currency": "CNY"},
            "party_size": 2,
            "occupancy": {"adults": 2, "rooms": 1},
            "arrival_anchor": {"at": start_at, "location": "Arrival"},
            "departure_anchor": {"at": end_at, "location": "Departure"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _place(
    *,
    place_id: str,
    provider_key: str,
    category: str,
    name: str,
    city_code: str,
    latitude: float = 24.48,
    longitude: float = 118.09,
    cost_minor: int = 0,
    duration_min: int = 60,
    external_id: str | None = None,
) -> Place:
    return Place(
        id=place_id,
        provider_key=provider_key,
        external_id=external_id or f"node/{place_id}",
        city_code=city_code,
        name=name,
        category=category,
        latitude=latitude,
        longitude=longitude,
        opening_hours_json=json.dumps({"open": "00:00", "close": "23:59"}),
        tags_json=json.dumps(["regression"]),
        metadata_json=json.dumps({"cost_minor": cost_minor, "duration_min": duration_min}),
        updated_at="2026-09-17T00:00:00+00:00",
    )


def _node(key, role, start, end, *, locked=False, lat=24.50, lon=118.10) -> GraphNode:
    return GraphNode(
        key=key,
        title=key,
        role=role,
        start_at=start,
        end_at=end,
        latitude=lat,
        longitude=lon,
        place_key=key,
        locked=locked,
    )


# ---------------------------------------------------------------------------
# 1. structural Places are hidden from the city content search
# ---------------------------------------------------------------------------
def test_search_places_hides_server_owned_structural_places():
    _reset()
    city_code = _tag("CNX-STRUCT").upper()
    tag = uuid.uuid4().hex[:8]
    structural_id = f"structural-{tag}"
    normal_id = f"normal-{tag}"

    with SessionLocal() as session:
        session.add(
            _place(
                place_id=structural_id,
                provider_key="citynexus-v9-domain",
                category="check_in",
                name="Hotel Check In",
                city_code=city_code,
            )
        )
        session.add(
            _place(
                place_id=normal_id,
                provider_key="overpass",
                category="attraction",
                name="Normal Attraction",
                city_code=city_code,
            )
        )
        session.commit()

    default = client.get("/api/v1/places", params={"city_code": city_code})
    assert default.status_code == 200, default.text
    default_ids = {item["id"] for item in default.json()}
    assert default_ids == {normal_id}, (
        f"server-owned structural Places leaked into the city search: {sorted(default_ids)}"
    )

    structural = client.get(
        "/api/v1/places",
        params={"city_code": city_code, "include_structural": "true"},
    )
    assert structural.status_code == 200, structural.text
    structural_ids = {item["id"] for item in structural.json()}
    assert structural_ids == {normal_id, structural_id}, (
        f"include_structural=true must expose the structural Place too: {sorted(structural_ids)}"
    )


# ---------------------------------------------------------------------------
# 2. a multi-day generic compose actually uses more than one day
# ---------------------------------------------------------------------------
def test_generic_compose_spreads_multi_day_stops_across_days():
    ids = _reset()
    city_code = _tag("CNX-GEN").upper()
    journey_id = _create_journey(
        ids["traveler_id"],
        city_code=city_code,
        start_at=f"2026-10-09T08:00:00{TZ}",
        end_at=f"2026-10-11T20:00:00{TZ}",
        title="three day generic compose",
    )
    tag = uuid.uuid4().hex[:8]
    place_ids: list[str] = []
    with SessionLocal() as session:
        for index in range(8):
            place_id = f"gen-{tag}-{index}"
            place_ids.append(place_id)
            session.add(
                _place(
                    place_id=place_id,
                    provider_key="overpass",
                    category="attraction",
                    name=f"Generic POI {index}",
                    city_code=city_code,
                    latitude=24.48 + index * 0.004,
                    longitude=118.09 + index * 0.004,
                    cost_minor=0,
                    duration_min=60,
                )
            )
        session.commit()

    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={"candidate_count": 1, "place_ids": place_ids},
    )
    assert response.status_code == 201, response.text
    plan = response.json()[0]
    activities = plan["activities"]
    assert len(activities) == 8
    dates = {item["start_at"][:10] for item in activities}
    assert len(dates) > 1, (
        f"the generic composer crammed every stop into one calendar day: {sorted(dates)}"
    )
    for item in activities:
        assert item["start_at"] >= f"2026-10-09T00:00:00{TZ}"
        assert item["end_at"] <= f"2026-10-11T20:00:00{TZ}"


# ---------------------------------------------------------------------------
# 3. a day-2 stop is advanced into its own day, not skipped
# ---------------------------------------------------------------------------
def test_day_two_stop_advances_from_previous_evening_interval(monkeypatch):
    """The free interval begins on the previous evening (check-in).

    Reverting the fix makes the loop ``continue`` on that interval, so the
    day-2 stop has no slot and a false ``NO_SAME_DAY_SLOT`` conflict is raised.
    """

    monkeypatch.setattr(sm, "_travel_minutes", lambda a, b, journey=None: 30)
    arrival = _dt(f"2026-11-06T10:00:00{TZ}")
    departure = _dt(f"2026-11-07T20:00:00{TZ}")
    anchors = [
        _node(
            "arrival",
            "ARRIVAL",
            f"2026-11-06T10:00:00{TZ}",
            f"2026-11-06T10:20:00{TZ}",
            locked=True,
        ),
        _node(
            "checkin",
            "CHECK_IN",
            f"2026-11-06T20:00:00{TZ}",
            f"2026-11-06T20:20:00{TZ}",
            locked=True,
        ),
        _node(
            "departure", "DEPARTURE", f"2026-11-07T19:50:00{TZ}", departure.isoformat(), locked=True
        ),
    ]
    carried = [
        _node("day2", "ACTIVITY", f"2026-11-07T10:00:00{TZ}", f"2026-11-07T11:00:00{TZ}"),
    ]

    out = _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)
    placed = next(node for node in out if node.key == "day2")
    assert _dt(placed.start_at).date() == _dt("2026-11-07").date(), (
        f"the day-2 stop was not kept on its own local date: {placed.start_at}"
    )
    assert _dt(placed.end_at) - _dt(placed.start_at) == _dt("2026-11-07T11:00:00+08:00") - _dt(
        "2026-11-07T10:00:00+08:00"
    )


# ---------------------------------------------------------------------------
# 4. one conflicting offer must not suppress every stay option
# ---------------------------------------------------------------------------
def _seed_stay_conflict_journey(traveler_id: str) -> tuple[str, str]:
    """A Tianjin journey with one carried day-2 stop and one bad offer."""

    journey_id = _create_journey(
        traveler_id,
        city_code="CN-TIANJIN",
        start_at=f"2026-11-06T10:00:00{TZ}",
        end_at=f"2026-11-08T20:00:00{TZ}",
        title="stay conflict isolation",
    )
    tag = uuid.uuid4().hex[:8]
    bad_offer_id = f"bad-offer-{tag}"
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        place = _place(
            place_id=f"stay-poi-{tag}",
            provider_key="overpass",
            category="attraction",
            name="Stay POI",
            city_code=journey.city_code,
            latitude=39.13,
            longitude=117.20,
        )
        session.add(place)
        session.flush()
        plan = PlanVersion(
            id=f"stay-plan-{tag}",
            journey_id=journey_id,
            version_no=1,
            planning_mode="balanced",
            status="ACTIVE",
            source="INITIAL",
            total_cost_minor=0,
            currency="CNY",
            score_breakdown_json="{}",
            utility_score=0.9,
            estimated_duration_min=60,
            created_at="2026-09-17T00:00:00+00:00",
        )
        session.add(plan)
        session.flush()
        session.add(
            Activity(
                id=f"stay-act-{tag}",
                plan_version_id=plan.id,
                sequence_no=1,
                activity_type="visit",
                title="Stay POI",
                place_id=place.id,
                start_at=f"2026-11-07T10:00:00{TZ}",
                end_at=f"2026-11-07T11:00:00{TZ}",
                travel_from_prev_min=0,
                estimated_cost_minor=0,
                currency="CNY",
                status="PLANNED",
                locked=False,
                metadata_json=json.dumps({"semantic_key": place.id, "role": "ACTIVITY"}),
                created_at="2026-09-17T00:00:00+00:00",
                updated_at="2026-09-17T00:00:00+00:00",
            )
        )
        journey.active_plan_version_id = plan.id
        session.add(
            AccommodationOfferSnapshot(
                id=bad_offer_id,
                journey_id=journey_id,
                provider_key="citynexus-city-fixture",
                external_offer_id="haihe-riverside",
                property_name="Haihe Riverside Hotel",
                latitude=39.1321,
                longitude=117.2056,
                coordinate_crs="WGS84",
                room_type="City twin",
                occupancy_json="{}",
                check_in_at=f"2026-11-06T14:00:00{TZ}",
                check_out_at=f"2026-11-06T20:00:00{TZ}",
                nightly_amount_minor=18000,
                taxes_amount_minor=0,
                total_amount_minor=36000,
                currency="CNY",
                cancellation_policy_json="{}",
                luggage_drop_json=json.dumps({"available": True}),
                refundable=True,
                evidence_json="{}",
                captured_at="2026-09-17T00:00:00+00:00",
                expires_at=None,
                status="CAPTURED",
            )
        )
        session.commit()
    return journey_id, bad_offer_id


def test_conflicting_offer_does_not_suppress_other_stay_options(monkeypatch):
    # Deterministic transition cost: the assertion is about per-offer conflict
    # isolation, not the distance heuristic.
    monkeypatch.setattr(sm, "_travel_minutes", lambda a, b, journey=None: 30)
    ids = _reset()
    journey_id, bad_offer_id = _seed_stay_conflict_journey(ids["traveler_id"])

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        with pytest.raises(StayItineraryConflict):
            build_candidate_nodes(
                session,
                journey,
                session.get(AccommodationOfferSnapshot, bad_offer_id),
            )

        options = generate_stay_options(session, journey)
        rejected = [
            (getattr(offer, "external_offer_id", None), reason)
            for offer, reason in getattr(generate_stay_options, "last_rejected", [])
        ]
        offered_snapshot_ids = {option.accommodation_offer_id for option in options}
        session.rollback()

    assert len(options) >= 2, (
        "one conflicting property suppressed every stay option: "
        f"{len(options)} produced, rejected={rejected}"
    )
    assert bad_offer_id not in offered_snapshot_ids, (
        "the conflicting offer must not have produced a selectable option"
    )
    assert ("haihe-riverside", "STAY_ITINERARY_CONFLICT") in rejected, (
        "the conflicting offer was not recorded as rejected: " + repr(rejected)
    )


# ---------------------------------------------------------------------------
# 5. a manually entered booking reaches smart-apply
# ---------------------------------------------------------------------------
def test_structured_user_booking_reaches_auto_apply_and_smart_apply():
    ids = _reset()
    journey_id = ids["journey_id"]
    tag = uuid.uuid4().hex[:8]

    imported = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={
            "artifact_type": "STRUCTURED_JSON",
            "payload": {
                "reservation": {
                    "reservation_type": "RAIL",
                    "provider_key": "user-confirmed-rail",
                    "confirmation_ref": f"REG-STRUCT-{tag}",
                    "origin": "Shanghai Hongqiao",
                    "destination": "Tianjin West",
                    "scheduled_departure_at": f"2026-10-09T03:30:00{TZ}",
                    "scheduled_arrival_at": f"2026-10-09T08:00:00{TZ}",
                    "price_amount_minor": 55300,
                    "currency": "CNY",
                    "price_status": "KNOWN",
                }
            },
        },
    )
    assert imported.status_code == 201, imported.text
    draft = imported.json()
    extracted = draft["extracted"]
    assert extracted.get("artifact_source") == "USER_ENTERED", (
        "structured payloads must be tagged as user-entered: "
        + repr(extracted.get("artifact_source"))
    )
    assert extracted.get("origin") == {"name": "Shanghai Hongqiao"}
    assert extracted.get("destination") == {"name": "Tianjin West"}

    draft_id = draft["id"]
    patched = client.patch(
        f"/api/v1/journeys/{journey_id}/inbox/{draft_id}",
        json={"anchor_role": "ARRIVAL"},
    )
    assert patched.status_code == 200, patched.text
    patched_draft = patched.json()
    smart = patched_draft["smart_intake"]
    assert smart.get("decision") in {"AUTO_APPLY", "APPLY_WITH_NOTICE"}, (
        "a user-entered structured booking stayed stuck in review: " + repr(smart)
    )
    match = patched_draft["extracted"].get("candidate_journey_match") or {}
    assert match.get("match") == "MATCH"
    assert match.get("confidence") == pytest.approx(1.0)

    applied = client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft_id}/smart-apply")
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "APPLIED", applied.text


# ---------------------------------------------------------------------------
# 6. a missing frozen mobility pair is captured on demand
# ---------------------------------------------------------------------------
def test_replan_estimate_captures_missing_pair_on_demand(monkeypatch):
    captured: dict[str, object] = {}

    class _MissingPairMobility:
        def __init__(self) -> None:
            self.calls = 0

        def estimate_minutes(self, origin, destination):
            self.calls += 1
            raise KeyError(f"no frozen evidence for {origin}->{destination}")

    class _Snapshot:
        def estimate_minutes(self, origin, destination):
            captured["resolved_pair"] = (origin, destination)
            return 37

    class _Result:
        def __init__(self) -> None:
            self.snapshot = _Snapshot()

    def fake_capture(session, journey, pairs, *, operation_key, **_kwargs):
        captured["pairs"] = tuple(pairs)
        captured["operation_key"] = operation_key
        return _Result()

    monkeypatch.setattr(main_module, "capture_runtime_mobility_snapshot", fake_capture)

    mobility = _MissingPairMobility()
    result = _replan_estimate(
        None,
        SimpleNamespace(),
        mobility,
        "origin-x",
        "dest-y",
        operation_key="replan-leg:regression",
    )

    assert result == 37, "the on-demand capture was not used"
    assert mobility.calls == 1
    assert captured["pairs"] == (("origin-x", "dest-y"),)
    assert captured["resolved_pair"] == ("origin-x", "dest-y")
