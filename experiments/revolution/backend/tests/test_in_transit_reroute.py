import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.execution.clock import execution_clock
from app.execution.in_transit_reroute import (
    classify_transit_deviation,
    deviation_threshold_m,
    distance_to_route_m,
    in_transit_projection,
    reroute_origin_authority,
    reroute_semantic_key,
    route_coordinates,
)


def test_not_en_route_is_inactive():
    decision = classify_transit_deviation(
        activity_status="IN_PROGRESS",
        location_source="FOREGROUND_WEB",
        location_fresh=True,
        accuracy_m=20,
        route_shape_available=True,
        distance_to_route_m=500,
    )
    assert decision.state == "INACTIVE"


def test_missing_location_is_explicit():
    decision = classify_transit_deviation(
        activity_status="EN_ROUTE",
        location_source=None,
        location_fresh=False,
        accuracy_m=None,
        route_shape_available=True,
        distance_to_route_m=None,
    )
    assert decision.state == "LOCATION_UNAVAILABLE"


def test_missing_route_shape_never_fakes_deviation():
    decision = classify_transit_deviation(
        activity_status="EN_ROUTE",
        location_source="FOREGROUND_WEB",
        location_fresh=True,
        accuracy_m=25,
        route_shape_available=False,
        distance_to_route_m=None,
    )
    assert decision.state == "ROUTE_SHAPE_UNAVAILABLE"
    assert decision.material is False


def test_foreground_location_far_from_route_is_advisory_only():
    decision = classify_transit_deviation(
        activity_status="EN_ROUTE",
        location_source="FOREGROUND_WEB",
        location_fresh=True,
        accuracy_m=30,
        route_shape_available=True,
        distance_to_route_m=500,
    )
    assert decision.state == "ADVISORY_DEVIATION"
    assert decision.authoritative is False
    assert decision.material is False


def test_trusted_single_outlier_does_not_immediately_become_incident():
    decision = classify_transit_deviation(
        activity_status="EN_ROUTE",
        location_source="DEVICE_ATTESTED",
        location_fresh=True,
        accuracy_m=20,
        route_shape_available=True,
        distance_to_route_m=500,
        consecutive_deviation_count=1,
    )
    assert decision.state == "POSSIBLE_VERIFIED_DEVIATION"
    assert decision.material is False


def test_trusted_persistent_deviation_can_be_authoritative():
    decision = classify_transit_deviation(
        activity_status="EN_ROUTE",
        location_source="LOCATION_ATTESTED",
        location_fresh=True,
        accuracy_m=20,
        route_shape_available=True,
        distance_to_route_m=500,
        consecutive_deviation_count=2,
    )
    assert decision.state == "VERIFIED_DEVIATION"
    assert decision.authoritative is True
    assert decision.material is True


def test_near_target_prefers_arrival_flow():
    decision = classify_transit_deviation(
        activity_status="EN_ROUTE",
        location_source="DEVICE_ATTESTED",
        location_fresh=True,
        accuracy_m=20,
        route_shape_available=True,
        distance_to_route_m=500,
        consecutive_deviation_count=2,
        near_target=True,
    )
    assert decision.state == "ARRIVAL_READY"
    assert decision.material is False


def test_dynamic_threshold_respects_accuracy():
    assert deviation_threshold_m(accuracy_m=10) == 150.0
    assert deviation_threshold_m(accuracy_m=80) == 240.0


def test_reroute_origin_authority_keeps_foreground_advisory():
    assert reroute_origin_authority("FOREGROUND_WEB") == "ADVISORY_ROUTE"
    assert reroute_origin_authority("DEVICE_ATTESTED") == "TRUSTED_CURRENT_LOCATION_ROUTE"


def test_reroute_semantic_key_is_stable_for_same_location_generation():
    first = reroute_semantic_key(
        journey_id="j1",
        activity_id="a1",
        location_observation_id="loc1",
        target_place_id="p1",
        mode="walk",
    )
    second = reroute_semantic_key(
        journey_id="j1",
        activity_id="a1",
        location_observation_id="loc1",
        target_place_id="p1",
        mode="WALK",
    )
    assert first == second


def test_route_geometry_accepts_provider_linestring_without_substituting_a_chord():
    route = route_coordinates(
        json.dumps(
            {
                "type": "LineString",
                "coordinates": [[139.0, 35.0], [139.01, 35.0], [139.01, 35.01]],
            }
        )
    )
    near_first_segment = distance_to_route_m(
        latitude=35.0005,
        longitude=139.005,
        route=route,
    )
    assert near_first_segment is not None and near_first_segment < 60


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _TransitSession:
    def __init__(self, *, activity, leg, observation, target):
        self.activity = activity
        self.leg = leg
        self.observation = observation
        self.target = target

    def get(self, model, key):
        if key == self.activity.id:
            return self.activity
        if key == self.target.id:
            return self.target
        return None

    def scalar(self, _statement):
        return self.leg

    def scalars(self, _statement):
        rows = self.observation if isinstance(self.observation, list) else [self.observation]
        return _Rows(rows)


def _transit_fixture(now, *, captured_at):
    journey = SimpleNamespace(
        id="journey-1",
        status="ACTIVE",
        current_activity_id="activity-1",
        active_plan_version_id="plan-1",
    )
    activity = SimpleNamespace(
        id="activity-1",
        plan_version_id="plan-1",
        status="EN_ROUTE",
        title="Next stop",
        place_id="place-1",
        metadata_json=json.dumps({"en_route_at": (now - timedelta(minutes=4)).isoformat()}),
    )
    leg = SimpleNamespace(
        journey_id="journey-1",
        plan_version_id="plan-1",
        to_activity_id="activity-1",
        sequence_no=1,
        geometry_json=json.dumps([[139.0, 35.0], [139.01, 35.0]]),
        crs="WGS84",
        provider_key="valhalla-http",
        provenance="LIVE_PROVIDER_CAPTURE",
        provider_snapshot_id=None,
        mode="WALK",
    )
    observation = SimpleNamespace(
        id="location-1",
        journey_id="journey-1",
        latitude=35.005,
        longitude=139.005,
        accuracy_m=20.0,
        captured_at=captured_at,
        source="FOREGROUND_WEB",
    )
    target = SimpleNamespace(id="place-1", latitude=35.02, longitude=139.02)
    return journey, activity, _TransitSession(
        activity=activity, leg=leg, observation=observation, target=target
    )


def test_operator_transit_projection_marks_browser_deviation_advisory_only():
    now = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
    journey, _activity, session = _transit_fixture(
        now, captured_at=(now - timedelta(minutes=1)).isoformat()
    )
    transit = in_transit_projection(session, journey, now=now)
    assert transit["activity_status"] == "EN_ROUTE"
    assert transit["state"] == "ADVISORY_DEVIATION"
    assert transit["can_reroute"] is True
    assert transit["authoritative"] is False
    assert "latitude" not in transit and "longitude" not in transit


def test_operator_transit_projection_uses_shared_execution_clock_by_default():
    now = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
    journey, _activity, session = _transit_fixture(
        now, captured_at=(now - timedelta(minutes=1)).isoformat()
    )
    with execution_clock.freeze(now):
        transit = in_transit_projection(session, journey)
    assert transit["location_age_seconds"] == 60
    assert transit["location_fresh"] is True
    assert transit["state"] == "ADVISORY_DEVIATION"


def test_operator_transit_projection_ignores_location_from_before_departure():
    now = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
    journey, _activity, session = _transit_fixture(
        now, captured_at=(now - timedelta(minutes=6)).isoformat()
    )
    transit = in_transit_projection(session, journey, now=now)
    assert transit["state"] == "LOCATION_UNAVAILABLE"
    assert transit["location_observation_id"] is None
    assert transit["can_reroute"] is False


def test_unknown_or_non_wgs84_leg_geometry_is_not_used_for_deviation():
    now = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
    journey, _activity, session = _transit_fixture(
        now, captured_at=(now - timedelta(minutes=1)).isoformat()
    )
    session.leg.crs = "GCJ02"
    transit = in_transit_projection(session, journey, now=now)
    assert transit["state"] == "ROUTE_SHAPE_UNAVAILABLE"
    assert transit["route_shape_available"] is False
    assert transit["can_reroute"] is True
    assert transit["distance_to_route_m"] is None

    session.leg.crs = "WGS84"
    session.leg.provenance = "UNKNOWN"
    transit = in_transit_projection(session, journey, now=now)
    assert transit["state"] == "ROUTE_SHAPE_UNAVAILABLE"
    assert transit["route_shape_available"] is False


def test_two_fresh_trusted_off_route_points_are_required_for_verified_deviation():
    now = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
    journey, _activity, session = _transit_fixture(
        now, captured_at=(now - timedelta(minutes=1)).isoformat()
    )
    latest = session.observation
    latest.source = "LOCATION_ATTESTED"
    previous = SimpleNamespace(
        id="location-0",
        journey_id=latest.journey_id,
        latitude=latest.latitude,
        longitude=latest.longitude,
        accuracy_m=latest.accuracy_m,
        captured_at=(now - timedelta(minutes=2)).isoformat(),
        source="LOCATION_ATTESTED",
    )
    session.observation = [latest, previous]
    transit = in_transit_projection(session, journey, now=now)
    assert transit["consecutive_deviation_count"] == 2
    assert transit["state"] == "VERIFIED_DEVIATION"
    assert transit["authoritative"] is True
    assert transit["material"] is True


def test_reroute_endpoint_uses_current_observation_and_keeps_active_plan(monkeypatch):
    from app import main as main_module
    from app.core.config import settings
    from app.db.models import (
        Activity,
        AutopilotEnrollment,
        Commitment,
        Event as EventModel,
        GuardianRun,
        Journey,
        JourneyLeg,
        MobilityFact,
        Place,
        PlanVersion,
        RouteAcquisitionOperation,
        Traveler,
    )
    from app.db.session import SessionLocal, ensure_schema

    ensure_schema()
    traveler_id, journey_id, plan_id = str(uuid4()), str(uuid4()), str(uuid4())
    activity_id, place_id = str(uuid4()), str(uuid4())
    next_activity_id, next_place_id = str(uuid4()), str(uuid4())
    next_fact_id = str(uuid4())
    now = datetime.now(timezone.utc)
    departed_at = now - timedelta(minutes=2)
    stamp = now.isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="Transit test",
                email=None,
                locale="en-US",
                timezone="Asia/Tokyo",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        session.commit()
        journey = Journey(
            id=journey_id,
            traveler_id=traveler_id,
            title="In-transit test",
            city_code="NARA",
            timezone="Asia/Tokyo",
            start_at=stamp,
            end_at=(now + timedelta(hours=4)).isoformat(),
            budget_currency="JPY",
            budget_amount_minor=10000,
            planning_mode="balanced",
            status="ACTIVE",
            active_plan_version_id=None,
            current_activity_id=None,
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(journey)
        session.commit()
        session.add(
            AutopilotEnrollment(
                journey_id=journey_id,
                enabled=True,
                interval_seconds=60,
                provider_mesh_json="{}",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        session.add(
            PlanVersion(
                id=plan_id,
                journey_id=journey_id,
                version_no=1,
                planning_mode="balanced",
                status="ACTIVE",
                source="INITIAL",
                total_cost_minor=0,
                currency="JPY",
                estimated_duration_min=30,
                created_at=stamp,
            )
        )
        session.add(
            Place(
                id=place_id,
                provider_key="test-provider",
                external_id="target-1",
                city_code="NARA",
                name="Exact target",
                category="museum",
                latitude=35.02,
                longitude=139.02,
                opening_hours_json="{}",
                tags_json="[]",
                metadata_json="{}",
                updated_at=stamp,
            )
        )
        session.add(
            Place(
                id=next_place_id,
                provider_key="test-provider",
                external_id="protected-stop-1",
                city_code="NARA",
                name="Protected stop",
                category="station",
                latitude=35.03,
                longitude=139.03,
                opening_hours_json="{}",
                tags_json="[]",
                metadata_json="{}",
                updated_at=stamp,
            )
        )
        session.commit()
        activity = Activity(
            id=activity_id,
            plan_version_id=plan_id,
            sequence_no=1,
            activity_type="VISIT",
            title="Exact target",
            place_id=place_id,
            city_service_id=None,
            start_at=stamp,
            end_at=(now + timedelta(hours=1)).isoformat(),
            travel_from_prev_min=15,
            estimated_cost_minor=0,
            currency="JPY",
            status="NOT_STARTED",
            locked=False,
            metadata_json="{}",
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(activity)
        session.commit()
        protected_activity = Activity(
            id=next_activity_id,
            plan_version_id=plan_id,
            sequence_no=2,
            activity_type="TRANSPORT",
            title="Protected stop",
            place_id=next_place_id,
            city_service_id=None,
            start_at=(now + timedelta(hours=4)).isoformat(),
            end_at=(now + timedelta(hours=4, minutes=30)).isoformat(),
            travel_from_prev_min=20,
            estimated_cost_minor=0,
            currency="JPY",
            status="NOT_STARTED",
            locked=True,
            metadata_json="{}",
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(protected_activity)
        session.add(
            MobilityFact(
                id=next_fact_id,
                journey_id=journey_id,
                option_id=None,
                from_key=place_id,
                to_key=next_place_id,
                mode="WALK",
                duration_min=20,
                distance_m=1000,
                cost_amount_minor=None,
                currency="JPY",
                cost_status="UNKNOWN",
                walking_min=20,
                transfers=0,
                weather_exposure="UNKNOWN",
                reliability_score=0.9,
                provider_key="valhalla-http",
                provenance="CAPTURED_REPLAY",
                geometry_json="[]",
                crs="WGS84",
                paid_mode=False,
                evidence_json="{}",
                captured_at=stamp,
                expires_at=None,
            )
        )
        session.add(
            Commitment(
                id=str(uuid4()),
                journey_id=journey_id,
                activity_id=next_activity_id,
                kind="BOOKING",
                level="C4_Anchored",
                status="ACTIVE",
                monetary_value_minor=0,
                monetary_value_status="KNOWN",
                currency="JPY",
                external_ref=None,
                policy_json=json.dumps({"safety_buffer_min": 10}),
                created_at=stamp,
                updated_at=stamp,
            )
        )
        session.flush()
        journey.active_plan_version_id = plan_id
        journey.current_activity_id = activity_id
        session.add(
            JourneyLeg(
                id=str(uuid4()),
                journey_id=journey_id,
                plan_version_id=plan_id,
                sequence_no=2,
                from_activity_id=activity_id,
                to_activity_id=next_activity_id,
                from_key=place_id,
                to_key=next_place_id,
                mode="WALK",
                depart_at=(now + timedelta(hours=1)).isoformat(),
                arrive_at=(now + timedelta(hours=1, minutes=20)).isoformat(),
                duration_min=20,
                distance_m=1000,
                cost_amount_minor=None,
                currency="JPY",
                cost_status="UNKNOWN",
                walking_min=20,
                reliability_score=0.9,
                weather_exposure="UNKNOWN",
                geometry_json="[]",
                crs="WGS84",
                provider_key="valhalla-http",
                provenance="CAPTURED_REPLAY",
                provider_snapshot_id=next_fact_id,
                captured_at=stamp,
                expires_at=None,
            )
        )
        session.add(
            JourneyLeg(
                id=str(uuid4()),
                journey_id=journey_id,
                plan_version_id=plan_id,
                sequence_no=1,
                from_activity_id=None,
                to_activity_id=activity_id,
                from_key="previous-place",
                to_key=place_id,
                mode="WALK",
                depart_at=departed_at.isoformat(),
                arrive_at=(departed_at + timedelta(minutes=15)).isoformat(),
                duration_min=15,
                distance_m=1000,
                cost_amount_minor=None,
                currency="JPY",
                cost_status="UNKNOWN",
                walking_min=15,
                reliability_score=1.0,
                weather_exposure="UNKNOWN",
                geometry_json=json.dumps([[139.0, 35.0], [139.01, 35.0]]),
                crs="WGS84",
                provider_key="valhalla-http",
                provenance="LIVE_PROVIDER_CAPTURE",
                provider_snapshot_id=None,
                captured_at=departed_at.isoformat(),
                expires_at=None,
            )
        )
        session.commit()

    captured_requests = []

    # Exercise the real route-operation boundary with no Valhalla configured.
    # Strict mock mobility must fail closed instead of looking like a reroute.
    from app.integrations.mobility import runtime as mobility_runtime
    from app import route_session_operations as route_operations

    monkeypatch.setattr(
        mobility_runtime,
        "settings",
        SimpleNamespace(live_valhalla_url=None),
    )
    monkeypatch.setattr(
        route_operations,
        "settings",
        SimpleNamespace(live_valhalla_url=None, real_city_cache_ttl_seconds=3600),
    )
    monkeypatch.setattr(main_module, "settings", SimpleNamespace(traveler_auth_secret=None))
    client = TestClient(main_module.app)
    departure_response = client.post(
        f"/api/v1/journeys/{journey_id}/activities/{activity_id}/depart"
    )
    assert departure_response.status_code == 200
    location_response = client.post(
        f"/api/v1/journeys/{journey_id}/location",
        json={
            "latitude": 35.005,
            "longitude": 139.005,
            "accuracy_m": 20,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert location_response.status_code == 201
    location_id = location_response.json()["id"]
    location_captured_at = location_response.json()["captured_at"]
    operator_response = client.get(f"/api/v1/journeys/{journey_id}/operator")
    assert operator_response.status_code == 200
    assert operator_response.json()["transit"]["state"] == "ADVISORY_DEVIATION"
    assert operator_response.json()["transit"]["authoritative"] is False

    unavailable_response = client.post(
        f"/api/v1/journeys/{journey_id}/in-transit/reroute",
        json={"location_observation_id": location_id},
    )
    assert unavailable_response.status_code == 200
    unavailable = unavailable_response.json()
    with SessionLocal() as session:
        operation = session.get(RouteAcquisitionOperation, unavailable["operation_id"])
        operation_request = json.loads(operation.request_json)
        assert unavailable["reroute_state"] == "IN_TRANSIT_REROUTE_PROVIDER_DEGRADED"
        assert unavailable["status"] == "UNAVAILABLE"
        assert unavailable["active_plan_unchanged"] is True
        assert unavailable["deviation_event_id"] is None
        assert session.query(EventModel).filter_by(
            journey_id=journey_id, event_type="TRAFFIC_DELAY"
        ).count() == 0
        assert session.query(GuardianRun).filter_by(journey_id=journey_id).count() == 0
        assert operation_request["origin_key"] == f"location-observation:{location_id}"
        assert "latitude" not in operation_request and "longitude" not in operation_request

    # The ready-path assertions below use a controlled provider receipt, not a
    # claim that this machine has a live routing provider configured.

    def fake_submit(_session, _journey, route_request):
        captured_requests.append(route_request)
        return SimpleNamespace(id="route-op"), False

    monkeypatch.setattr(main_module, "submit_route_operation", fake_submit)
    monkeypatch.setattr(
        main_module,
        "route_operation_payload",
        lambda _row, reused=False: {
            "status": "COMPLETED",
            "outcome": "ROUTE_READY",
            "reused": reused,
            "result": {
                "duration_min": 12,
                "provider": "valhalla-http",
                "provenance": "LIVE_PROVIDER_CAPTURE",
            },
        },
    )
    response = client.post(
        f"/api/v1/journeys/{journey_id}/in-transit/reroute",
        json={"location_observation_id": location_id},
    )
    assert response.status_code == 200
    response = response.json()
    with SessionLocal() as session:
        assert response["reroute_state"] == "IN_TRANSIT_REROUTE_ADVISORY_READY"
        assert response["active_plan_unchanged"] is True
        assert response["deviation_event_id"] is None
        assert response["current_activity_status"] == "EN_ROUTE"
        assert response["origin"]["coordinates_exposed"] is False
        assert response["provenance_statement"] == "FOREGROUND_WEB_IS_ADVISORY"
        assert response["commitment_risk"]["state"] == "WITHIN_BUFFER"
        assert response["commitment_risk"]["next_commitment"]["title"] == "Protected stop"
        assert response["commitment_risk"]["future_mobility_basis"] == "FROZEN_PLAN_EVIDENCE_NOT_FUTURE_LIVE_TRAFFIC"
        assert session.get(Journey, journey_id).active_plan_version_id == plan_id
        assert session.query(Activity).filter_by(id=activity_id).one().status == "EN_ROUTE"
        assert captured_requests[0].origin_key == f"location-observation:{location_id}"
        assert captured_requests[0].target_key == place_id
        assert captured_requests[0].target_activity_id == activity_id
        assert captured_requests[0].requested_mode == "WALK"
        assert captured_requests[0].semantic_payload()["origin_evidence"] == {
            "location_observation_id": location_id,
            "source": "FOREGROUND_WEB",
            "captured_at": location_captured_at,
            "accuracy_m": 20.0,
            "authority": "ADVISORY_ROUTE",
        }
        assert session.query(JourneyLeg).filter_by(plan_version_id=plan_id, to_activity_id=activity_id).one().geometry_json == json.dumps([[139.0, 35.0], [139.01, 35.0]])

        transit_truth = {
            "state": "VERIFIED_DEVIATION",
            "authoritative": True,
            "material": True,
            "location_source": "LOCATION_ATTESTED",
            "location_observation_id": location_id,
            "location_captured_at": stamp,
            "location_accuracy_m": 20.0,
            "distance_to_route_m": 500.0,
            "deviation_threshold_m": 150.0,
            "route_geometry": {"basis": "ACTIVE_JOURNEY_LEG", "provenance": "LIVE_PROVIDER_CAPTURE"},
        }
        at_risk = {
            "state": "COMMITMENT_AT_RISK",
            "next_commitment": {
                "commitment_id": session.query(Commitment).filter_by(journey_id=journey_id).one().id,
                "activity_id": next_activity_id,
                "title": "Protected stop",
            },
        }
        event = main_module._persist_trusted_transit_deviation_event(
            session,
            journey=session.get(Journey, journey_id),
            activity=session.get(Activity, activity_id),
            active_leg_id=session.query(JourneyLeg).filter_by(to_activity_id=activity_id).one().id,
            transit=transit_truth,
            commitment_risk=at_risk,
            reroute_operation_id="route-op-1",
            route_provider="valhalla-http",
            route_provenance="LIVE_PROVIDER_CAPTURE",
        )
        session.commit()
        assert event.affected_activity_id == activity_id
        duplicate = main_module._persist_trusted_transit_deviation_event(
            session,
            journey=session.get(Journey, journey_id),
            activity=session.get(Activity, activity_id),
            active_leg_id=session.query(JourneyLeg).filter_by(to_activity_id=activity_id).one().id,
            transit={
                **transit_truth,
                "location_observation_id": "new-trusted-sample-same-incident",
            },
            commitment_risk=at_risk,
            reroute_operation_id="route-op-2",
            route_provider="valhalla-http",
            route_provenance="LIVE_PROVIDER_CAPTURE",
        )
        assert duplicate.id == event.id
        assert session.query(EventModel).filter_by(
            journey_id=journey_id,
            provider_key="journey-in-transit-route",
            event_type="TRAFFIC_DELAY",
        ).count() == 1

        # Once the incident has genuinely closed, a later verified deviation
        # on the same edge is a new incident rather than being swallowed by
        # historical evidence.
        event.status = "RESOLVED"
        session.commit()
        reopened = main_module._persist_trusted_transit_deviation_event(
            session,
            journey=session.get(Journey, journey_id),
            activity=session.get(Activity, activity_id),
            active_leg_id=session.query(JourneyLeg).filter_by(to_activity_id=activity_id).one().id,
            transit={
                **transit_truth,
                "location_observation_id": "trusted-sample-after-resolution",
            },
            commitment_risk=at_risk,
            reroute_operation_id="route-op-3",
            route_provider="valhalla-http",
            route_provenance="LIVE_PROVIDER_CAPTURE",
        )
        assert reopened.id != event.id
        session.commit()

        advisory = main_module._persist_trusted_transit_deviation_event(
            session,
            journey=session.get(Journey, journey_id),
            activity=session.get(Activity, activity_id),
            active_leg_id="leg-1",
            transit={**transit_truth, "location_source": "FOREGROUND_WEB", "authoritative": False, "material": False},
            commitment_risk=at_risk,
            reroute_operation_id="route-op-web",
            route_provider="valhalla-http",
            route_provenance="LIVE_PROVIDER_CAPTURE",
        )
        assert advisory is None
        assert session.query(EventModel).filter_by(
            journey_id=journey_id,
            provider_key="journey-in-transit-route",
            event_type="TRAFFIC_DELAY",
        ).count() == 2
