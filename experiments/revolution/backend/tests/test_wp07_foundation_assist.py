from __future__ import annotations

from datetime import datetime

from app.db.session import SessionLocal
from app.integrations.ai.copilot_contract import VerifiedCopilotContext, validate_proposed_action
from app.integrations.context.open_meteo import WeatherRisk
from app.integrations.context.weather_ingestion import (
    ingest_provider_verified_weather_event,
    record_provider_degradation,
    resolve_provider_degradation,
)
from app.integrations.context.weather_snapshot import (
    freeze_weather_risks,
    provider_verified_event_payload,
)
from app.main import app
from app.planning.replan_candidate_problem import ReplanCandidateEvidence, take_feasible
from app.replanning.opportunity_window_family import (
    WindowReplacementInput,
    evaluate_window_replacement,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


def test_real_f3_window_evidence_uses_inbound_outbound_and_safety_once():
    result = evaluate_window_replacement(
        now=datetime.fromisoformat("2026-10-10T13:20:00+08:00"),
        next_commitment_start=datetime.fromisoformat("2026-10-10T15:30:00+08:00"),
        safety_buffer_min=15,
        budget_remaining_minor=10000,
        candidate=WindowReplacementInput(
            key="place-design-library",
            visit_minutes=60,
            inbound_travel_min=15,
            outbound_travel_min=20,
            cost_minor=2000,
            opens_at=datetime.fromisoformat("2026-10-10T09:30:00+08:00"),
            closes_at=datetime.fromisoformat("2026-10-10T19:00:00+08:00"),
        ),
    )
    assert result.fits is True
    assert result.remaining_buffer_min == 20


def test_f3_window_rejects_closed_or_unaffordable_candidates():
    common = {
        "now": datetime.fromisoformat("2026-10-10T13:20:00+08:00"),
        "next_commitment_start": datetime.fromisoformat("2026-10-10T15:30:00+08:00"),
        "safety_buffer_min": 15,
    }
    closed = evaluate_window_replacement(
        **common,
        budget_remaining_minor=10000,
        candidate=WindowReplacementInput(
            key="closed-place",
            visit_minutes=40,
            inbound_travel_min=10,
            outbound_travel_min=10,
            cost_minor=100,
            opens_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
            closes_at=datetime.fromisoformat("2026-10-10T14:00:00+08:00"),
        ),
    )
    unaffordable = evaluate_window_replacement(
        **common,
        budget_remaining_minor=100,
        candidate=WindowReplacementInput(
            key="expensive-place",
            visit_minutes=40,
            inbound_travel_min=10,
            outbound_travel_min=10,
            cost_minor=101,
            opens_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
            closes_at=datetime.fromisoformat("2026-10-10T19:00:00+08:00"),
        ),
    )
    assert closed.fits is False
    assert "CLOSED_BEFORE_VISIT_END" in closed.failures
    assert unaffordable.fits is False
    assert "BUDGET_EXCEEDED" in unaffordable.failures


def test_weather_snapshot_creates_server_owned_provider_verified_evidence():
    snapshot = freeze_weather_risks(
        [
            WeatherRisk(
                code="HEAVY_RAIN_RISK",
                severity="HIGH",
                at=datetime.fromisoformat("2026-10-10T14:00:00+08:00"),
                evidence={"precipitation_probability": 85, "weather_code": 65},
            )
        ],
        captured_at="2026-10-10T13:55:00+08:00",
    )
    event = provider_verified_event_payload(snapshot.facts[0])
    assert event["provider_key"] == "open-meteo"
    assert event["provenance"] == "PROVIDER_VERIFIED"
    assert event["payload"]["provider_evidence"]["precipitation_probability"] == 85


def test_weather_ingestion_persists_provider_verified_event_server_side():
    from fastapi.testclient import TestClient

    client = TestClient(app)
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    snapshot = freeze_weather_risks(
        [
            WeatherRisk(
                code="HEAVY_RAIN_RISK",
                severity="HIGH",
                at=datetime.fromisoformat("2026-10-10T14:00:00+08:00"),
                evidence={"precipitation_probability": 90},
            )
        ],
        captured_at="2026-10-10T13:55:00+08:00",
    )
    with SessionLocal() as session:
        event = ingest_provider_verified_weather_event(
            session, journey_id=ids["journey_id"], fact=snapshot.facts[0]
        )
        session.commit()
        assert event.provider_key == "open-meteo"
        assert event.provenance == "PROVIDER_VERIFIED"


def test_provider_recovery_resolves_outage_and_restores_twin_ready_projection():
    from fastapi.testclient import TestClient

    client = TestClient(app)
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    with SessionLocal() as session:
        record_provider_degradation(
            session,
            journey_id=jid,
            detail="Open-Meteo timeout",
            occurred_at="2026-10-10T13:00:00+08:00",
        )
        session.commit()
    degraded = client.get(
        f"/api/v1/journeys/{jid}/twin", params={"now": "2026-10-10T13:00:00+08:00"}
    ).json()
    assert degraded["projection_status"] == "DEGRADED"
    assert degraded["context"]["degraded_providers"] == ["open-meteo"]
    with SessionLocal() as session:
        resolved = resolve_provider_degradation(
            session, journey_id=jid, resolved_at="2026-10-10T13:05:00+08:00"
        )
        session.commit()
        assert len(resolved) == 1
        assert resolved[0].status == "RESOLVED"
    ready = client.get(
        f"/api/v1/journeys/{jid}/twin", params={"now": "2026-10-10T13:00:00+08:00"}
    ).json()
    assert ready["projection_status"] == "READY"
    assert ready["context"]["degraded_providers"] == []


def test_verified_candidate_ranker_excludes_infeasible_and_has_stable_ties():
    rows = [
        ReplanCandidateEvidence("bad", "F0", False, 0.99, 1, 0),
        ReplanCandidateEvidence("b", "F3", True, 0.80, 2, 1),
        ReplanCandidateEvidence("a", "F0", True, 0.80, 1, 0),
        ReplanCandidateEvidence("c", "F1", True, 0.79, 0, 2),
    ]
    selected = take_feasible(rows, candidate_count=2)
    assert [item.key for item in selected] == ["a", "b"]


def test_copilot_contract_forbids_direct_mutation_actions():
    context = VerifiedCopilotContext(
        journey_id="j1",
        journey_status="ACTIVE",
        reason_codes=("COMMITMENT_PRESERVED",),
        evidence={"cost_delta_minor": 600},
    )
    assert context.prompt_payload()["mutation_policy"] == "NO_DIRECT_STATE_MUTATION"
    assert validate_proposed_action("REQUEST_REPLAN") == "REQUEST_REPLAN"
    try:
        validate_proposed_action("SET_COMMITMENT_C4")
    except ValueError:
        pass
    else:
        raise AssertionError("direct mutation action should be rejected")
