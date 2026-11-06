from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from demo_stage import demo_reset_headers
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.agent.orchestrator import HermesJourneyAgent
from app.agent.runtime import DeepSeekJourneyAgent, _result_for_model
from app.core.config import settings as runtime_settings
from app.db.models import (
    Activity,
    AgentRun,
    Event,
    Journey,
    JourneyMission,
    Place,
    RecoveryEpoch,
)
from app.db.session import SessionLocal
from app.integrations.ai.copilot_contract import classify_copilot_message
from app.integrations.ai.event_targeting import resolve_event_target
from app.recovery_epoch import RECOVERY_EPOCH_SUPERSEDED
from app.agent.mission import compile_mission
from app.capabilities import (
    extract_capabilities_from_text,
    preference_is_explicitly_excluded,
    preference_is_positive,
)

client = TestClient(main_module.app)


def test_default_hermes_model_order_is_international_then_domestic_fallbacks():
    assert runtime_settings.hermes_model == "cn:glm-5.3-flash"
    assert runtime_settings.hermes_model_fallbacks == (
        "cn:glm-5.3-flash",
        "cn:deepseek-v4.1-flash",
    )


def test_r32_external_wait_does_not_hold_database_transaction(monkeypatch):
    """A provider wait must leave concurrent provider ingestion free to write."""
    from app.agent.mission import create_mission

    journey_id = _selected_journey()
    agent = object.__new__(DeepSeekJourneyAgent)
    agent.model_key = "transaction-boundary-fixture"
    calls = []
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        mission = create_mission(
            session, journey, command="Inspect the current Journey.",
            requested_autonomy="PLAN_ONLY", trigger_event_id=None,
        )
        session.commit()

        def complete(messages):
            assert not session.in_transaction()
            calls.append(len(messages))
            if len(calls) == 1:
                return {
                    "content": "", "tool_calls": [{
                        "id": "transaction-inspection", "type": "function",
                        "function": {"name": "inspect_journey", "arguments": json.dumps({"journey_id": journey_id})},
                    }],
                }
            return {"content": "Inspection complete.", "tool_calls": []}

        monkeypatch.setattr(agent, "_complete", complete)
        agent.run(session, journey=journey, mission=mission)
    assert len(calls) >= 2


def test_r32_executed_mutation_terminalizes_before_self_supersession(
    monkeypatch,
):
    """A successful mutation ends the run on its server receipt.

    Closing the bound epoch is a successful recovery handover, not evidence
    that the run became stale.  No extra model turn may create a synthetic
    successor epoch and relabel the successful run SUPERSEDED.
    """

    from app.agent.gateway import AgentToolGateway
    from app.agent.mission import create_mission
    from app.recovery_epoch import (
        COMMITTED,
        commit_recovery_epoch,
        current_open_recovery_epoch,
    )

    journey_id = _selected_journey()
    agent = object.__new__(DeepSeekJourneyAgent)
    agent.model_key = "r32-terminal-mutation-fixture"
    complete_calls = 0

    def tool_message(name: str, arguments: dict[str, object], call_id: str):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        }

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        mission = create_mission(
            session,
            journey,
            command="Inspect, then execute one authorized recovery mutation.",
            requested_autonomy="EXECUTE_WITHIN_BOUNDS",
            trigger_event_id=None,
        )
        session.commit()

        def complete(messages):
            nonlocal complete_calls
            complete_calls += 1
            if complete_calls == 1:
                return tool_message(
                    "inspect_journey",
                    {"journey_id": journey_id},
                    "inspect-before-mutation",
                )
            if complete_calls == 2:
                return tool_message(
                    "accept_replan",
                    {
                        "journey_id": journey_id,
                        "proposal_id": str(uuid4()),
                    },
                    "execute-mutation",
                )
            raise AssertionError(
                "runtime requested another model turn after executed mutation"
            )

        def dispatch(self, tool_name, request):
            if tool_name == "inspect_journey":
                return {
                    "tool": tool_name,
                    "decision": "ALLOW_AUTO",
                    "journey": {"id": journey_id},
                }
            assert tool_name == "accept_replan"
            epoch = current_open_recovery_epoch(self.session, journey_id)
            assert epoch is not None
            commit_recovery_epoch(
                self.session,
                epoch,
                reason="TEST_AUTHORIZED_MUTATION",
            )
            self.session.commit()
            return {
                "tool": tool_name,
                "decision": "ALLOW_AUTO",
                "executed": True,
                "proposal_id": str(request.proposal_id),
            }

        monkeypatch.setattr(agent, "_complete", complete)
        monkeypatch.setattr(AgentToolGateway, "dispatch", dispatch)
        result = agent.run(
            session,
            journey=journey,
            mission=mission,
        )

    assert complete_calls == 2
    assert result["status"] == "COMPLETED"
    assert result["completion_contract"]["kind"] == "MUTATION_EXECUTED"
    assert [step["tool_name"] for step in result["tool_steps"]] == [
        "inspect_journey",
        "accept_replan",
    ]
    assert not any(
        step["tool_name"] == "recovery_epoch_guard"
        for step in result["tool_steps"]
    )

    with SessionLocal() as db:
        persisted = db.get(AgentRun, result["run_id"])
        assert persisted is not None
        assert persisted.status == "COMPLETED"
        epoch = db.get(RecoveryEpoch, persisted.recovery_epoch_id)
        assert epoch is not None
        assert epoch.status == COMMITTED


def test_r32_shock_during_provider_wait_preempts_before_stale_tool(monkeypatch):
    """A model response born under R1 is discarded if R2 appears in flight."""

    from app.agent.mission import create_mission

    journey_id = _selected_journey()
    agent = object.__new__(DeepSeekJourneyAgent)
    agent.model_key = "r32-provider-race-fixture"
    complete_calls = 0

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        mission = create_mission(
            session,
            journey,
            command="Inspect the current Journey and recover if needed.",
            requested_autonomy="EXECUTE_WITHIN_BOUNDS",
            trigger_event_id=None,
        )
        session.commit()

        def complete(messages):
            nonlocal complete_calls
            complete_calls += 1
            assert complete_calls == 1
            # Simulate the server receiving a materially new provider fact
            # while the external model call is in flight.  This is a separate
            # durable transaction, just like the live R32 strike.
            with SessionLocal() as concurrent:
                concurrent.add(
                    Event(
                        id=str(uuid4()),
                        journey_id=journey_id,
                        provider_key="r32-concurrent-provider",
                        provenance="PROVIDER_VERIFIED",
                        event_type="WEATHER",
                        severity="MAJOR",
                        status="DETECTED",
                        affected_activity_id=None,
                        occurred_at=datetime.now(timezone.utc).isoformat(),
                        detected_at=datetime.now(timezone.utc).isoformat(),
                        resolved_at=None,
                        payload_json=json.dumps(
                            {
                                "incident_fingerprint": (
                                    "r32-shock-during-provider-wait"
                                )
                            },
                            sort_keys=True,
                        ),
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
                concurrent.commit()
            # This stale response must never reach Gateway dispatch.
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "stale-inspection",
                        "type": "function",
                        "function": {
                            "name": "inspect_journey",
                            "arguments": json.dumps(
                                {"journey_id": journey_id}
                            ),
                        },
                    }
                ],
            }

        def must_not_dispatch(*args, **kwargs):
            raise AssertionError(
                "stale provider response reached Gateway dispatch"
            )

        monkeypatch.setattr(agent, "_complete", complete)
        monkeypatch.setattr(
            "app.agent.gateway.AgentToolGateway.dispatch",
            must_not_dispatch,
        )
        result = agent.run(
            session,
            journey=journey,
            mission=mission,
        )

    assert complete_calls == 1
    assert result["status"] == "SUPERSEDED"
    assert result["completion_contract"]["kind"] == "SUPERSEDED"
    assert result["completion_contract"]["reason_code"] == (
        RECOVERY_EPOCH_SUPERSEDED
    )
    assert [step["tool_name"] for step in result["tool_steps"]] == [
        "recovery_epoch_guard"
    ]
    guard = result["tool_steps"][0]
    assert guard["status"] == "SUPERSEDED"
    assert guard["result"]["code"] == RECOVERY_EPOCH_SUPERSEDED
    assert guard["result"]["model_response_discarded"] is True

    with SessionLocal() as db:
        persisted = db.get(AgentRun, result["run_id"])
        assert persisted is not None
        assert persisted.status == "SUPERSEDED"
        assert persisted.failure_reason == RECOVERY_EPOCH_SUPERSEDED


def test_negative_indoor_preference_does_not_compile_as_indoor_preferred(monkeypatch):
    command = "我喜欢户外自然景观，不喜欢剧场和室内馆，路线松弛不要赶。"
    capabilities = extract_capabilities_from_text(command)
    assert "INDOOR_PREFERRED" not in {item["code"] for item in capabilities}
    monkeypatch.setattr("app.agent.mission._resolve_protected_activities", lambda *_args: ())

    spec = compile_mission(
        None,
        type(
            "JourneyStub",
            (),
            {"budget_currency": "CNY", "active_plan_version_id": None},
        )(),
        command,
        requested_autonomy="EXECUTE_WITHIN_BOUNDS",
    )
    assert "PREFER_INDOOR" not in spec.objective_codes
    assert "PREFER_OUTDOOR" in spec.objective_codes


def test_abandoning_indoor_path_does_not_compile_as_indoor_preferred():
    command = "明确放弃剧场和室内馆路径，优先户外自然景观。"
    assert preference_is_explicitly_excluded(command, ("室内", "indoor"))
    assert not preference_is_positive(command, ("室内", "indoor"))


def test_agent_search_bridges_unseeded_live_journey_to_provider_discovery(monkeypatch):
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    city_code = f"R14-UNSEEDED-{uuid4().hex[:8]}"
    place_id = f"real-place-{uuid4().hex}"
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        journey.city_code = city_code
        session.commit()

    def fake_capture(session, journey, **kwargs):
        session.add(
            Place(
                id=place_id,
                provider_key="overpass",
                external_id="overpass:node/1414",
                city_code=journey.city_code,
                name="Unseeded Park",
                category="park",
                latitude=26.8,
                longitude=100.2,
                opening_hours_json=json.dumps({"status": "UNKNOWN"}),
                tags_json=json.dumps(["outdoor", "park", "real_city"]),
                metadata_json=json.dumps(
                    {"discovery_mode": "live", "cost_evidence": {"status": "UNKNOWN"}}
                ),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        return {
            "mode": "live",
            "provenance": "LIVE_PROVIDER",
            "runtime_provenance": "LIVE_PROVIDER",
            "provider_status": {"nominatim": "CAPTURED", "overpass": "CAPTURED"},
            "candidates": [{"id": place_id}],
        }

    monkeypatch.setattr(
        "app.core.config.settings",
        replace(runtime_settings, real_city_enabled=True, real_city_mode="live"),
    )
    monkeypatch.setattr(
        "app.integrations.discovery.real_city.capture_real_city_candidates", fake_capture
    )
    with SessionLocal() as session:
        result = main_module.AgentToolGateway(session).search_city_services(
            main_module.AgentToolCall(
                request_id="r14-unseeded-search",
                journey_id=journey_id,
                q="park",
            )
        )

    assert result["discovery"]["provenance"] == "LIVE_PROVIDER"
    assert result["discovery"]["candidate_count"] == 1
    assert result["places"][0]["id"] == place_id
    assert result["places"][0]["provider_key"] == "overpass"


def _selected_journey() -> str:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    selected = client.post(
        f"/api/v1/journeys/{journey_id}/plans/{generated.json()[0]['id']}/select"
    )
    assert selected.status_code == 200, selected.text
    return journey_id


def test_natural_language_command_persists_mission_and_external_boundary():
    if runtime_settings.ai_api_key:
        pytest.skip("live model environment is configured")
    journey_id = _selected_journey()
    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={
            "command": "下午海河沿线有交通管制，我不想淋雨，游船和晚餐别动，最多多花50元。你自己重新安排。",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mission"]["requested_autonomy"] == "EXECUTE_WITHIN_BOUNDS"
    assert "PREFER_INDOOR" in body["mission"]["objective_codes"]
    assert body["mission"]["max_cost_delta_minor"] == 5000
    assert body["mission"]["protected_activity_ids"]
    assert body["run"]["status"] == "SKIPPED_EXTERNAL_DEPENDENCY"
    with SessionLocal() as session:
        mission = session.get(JourneyMission, body["mission"]["id"])
        run = session.get(AgentRun, body["run"]["run_id"])
        event = session.get(Event, body["event_id"])
        assert mission is not None
        assert run is not None
        assert event is not None
        assert event.provenance == "USER_REPORTED"
        assert event.event_type == "TRAFFIC_DELAY"


def test_agent_command_reuses_existing_actionable_event_for_same_current_activity():
    journey_id = _selected_journey()
    started = client.post(f"/api/v1/journeys/{journey_id}/start")
    assert started.status_code == 200, started.text
    current_activity_id = client.get(f"/api/v1/journeys/{journey_id}").json()[
        "current_activity_id"
    ]
    original = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": current_activity_id,
            "payload": {"reason": "existing closure for idempotency test"},
        },
    )
    assert original.status_code == 201, original.text
    original_id = original.json()["id"]

    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={
            "command": "现在下一站临时闭馆，请基于这条已经记录的闭馆事件提出重规划，只生成方案，不接受。",
            "requested_autonomy": "PLAN_ONLY",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["event_id"] == original_id
    events = client.get(f"/api/v1/journeys/{journey_id}/events")
    assert events.status_code == 200, events.text
    assert [item["id"] for item in events.json()] == [original_id]


def test_named_current_disruption_targets_named_future_activity():
    journey_id = _selected_journey()
    started = client.post(f"/api/v1/journeys/{journey_id}/start")
    assert started.status_code == 200, started.text
    interpretation = classify_copilot_message("丽江金塔景区临时关闭了，请重新安排。")
    assert interpretation.entities["subject_text"] == "丽江金塔景区"

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        activities = list(
            session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == journey.active_plan_version_id)
                .order_by(Activity.sequence_no)
            )
        )
        assert len(activities) >= 2
        named = activities[-1]
        target = resolve_event_target(
            session,
            journey,
            event_type="CLOSURE",
            semantic={
                "effective_time": "NOW",
                "entities": {"subject_text": named.title},
            },
            now=datetime.now(timezone.utc),
        )
        assert target.activity_id == named.id
        assert target.resolution == "AUTHORITATIVE_NAMED_ACTIVITY"


def test_agent_runtime_uses_model_selected_tool_loop_and_reloadable_trace(monkeypatch):
    journey_id = _selected_journey()
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-inspect-journey",
                        "type": "function",
                        "function": {
                            "name": "inspect_journey",
                            "arguments": json.dumps({"journey_id": journey_id}),
                        },
                    }
                ],
            }
        elif len(calls) == 2:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-inspect-twin",
                        "type": "function",
                        "function": {
                            "name": "inspect_twin",
                            "arguments": json.dumps({"journey_id": journey_id}),
                        },
                    }
                ],
            }
        else:
            message = {
                "role": "assistant",
                "content": "Inspected the current journey and twin; no mutation is needed.",
            }
        return httpx.Response(200, json={"choices": [{"message": message}]})

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    fake_agent = object.__new__(DeepSeekJourneyAgent)
    fake_agent.model_key = "deepseek-chat"
    fake_agent._client = fake_client
    fake_agent._owns_client = False
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(runtime_settings, ai_api_key="test-key", ai_model="deepseek/deepseek-chat"),
    )
    monkeypatch.setattr(main_module, "DeepSeekJourneyAgent", lambda: fake_agent)

    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={
            "command": "先检查当前行程和实时状态，再告诉我是否需要调整。",
            "requested_autonomy": "PLAN_ONLY",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [step["tool_name"] for step in body["run"]["tool_steps"]] == [
        "inspect_journey",
        "inspect_twin",
    ]
    assert body["run"]["status"] == "PARTIAL"
    assert body["run"]["completion_contract"]["kind"] == "UNRESOLVED"
    # A prose-only answer is not a server terminal fact. The strategy-neutral
    # supervisor gives one bounded extra model turn without naming a next tool.
    assert len(calls) == 4

    reloaded = client.get(f"/api/v1/journeys/{journey_id}/agent/runs/{body['run']['run_id']}")
    assert reloaded.status_code == 200, reloaded.text
    assert [step["tool_name"] for step in reloaded.json()["tool_steps"]] == [
        "inspect_journey",
        "inspect_twin",
    ]


def test_r32_runtime_persists_recovery_supersession_as_terminal(monkeypatch):
    journey_id = _selected_journey()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "r32-stale-inspect",
                        "type": "function",
                        "function": {
                            "name": "inspect_journey",
                            "arguments": json.dumps(
                                {"journey_id": journey_id}
                            ),
                        },
                    }
                ],
            }
        else:
            message = {
                "role": "assistant",
                "content": "This turn should not be needed after supersession.",
            }
        return httpx.Response(
            200, json={"choices": [{"message": message}]}
        )

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    fake_agent = object.__new__(DeepSeekJourneyAgent)
    fake_agent.model_key = "cn:deepseek-v4.1-flash"
    fake_agent._client = fake_client
    fake_agent._owns_client = False

    def superseded_dispatch(self, tool_name, request):
        raise HTTPException(
            409,
            detail={
                "code": RECOVERY_EPOCH_SUPERSEDED,
                "stale_recovery_epoch_id": "r1",
                "current_recovery_epoch_id": "r2",
                "stale_generation": 1,
                "current_generation": 2,
            },
        )

    monkeypatch.setattr(
        "app.agent.gateway.AgentToolGateway.dispatch",
        superseded_dispatch,
    )
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(
            runtime_settings,
            ai_api_key="test-key",
            ai_model="cn:deepseek-v4.1-flash",
        ),
    )
    monkeypatch.setattr(
        main_module, "DeepSeekJourneyAgent", lambda: fake_agent
    )

    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={
            "command": "Inspect current reality and recover if needed.",
            "requested_autonomy": "PLAN_ONLY",
        },
    )
    assert response.status_code == 200, response.text
    run = response.json()["run"]
    assert run["status"] == "SUPERSEDED"
    assert calls == 1
    assert run["tool_steps"][-1]["status"] == "SUPERSEDED"
    assert (
        run["tool_steps"][-1]["result"]["code"]
        == RECOVERY_EPOCH_SUPERSEDED
    )

    with SessionLocal() as db:
        persisted = db.get(AgentRun, run["run_id"])
        assert persisted is not None
        assert persisted.status == "SUPERSEDED"
        assert persisted.failure_reason == RECOVERY_EPOCH_SUPERSEDED


def test_agent_runtime_nudges_explicit_accept_request_after_model_prose(monkeypatch):
    journey_id = _selected_journey()
    calls = 0

    def tool_message(name: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = tool_message("inspect_journey", {"journey_id": journey_id}, "accept-journey")
        elif calls == 2:
            message = tool_message("list_replans", {"journey_id": journey_id}, "accept-list")
        elif calls == 4:
            message = tool_message(
                "accept_replan",
                {"journey_id": journey_id, "proposal_id": str(uuid4())},
                "accept-action",
            )
        else:
            message = {"role": "assistant", "content": "I have completed the requested action."}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    fake_agent = object.__new__(DeepSeekJourneyAgent)
    fake_agent.model_key = "cn:glm-5.3-flash"
    fake_agent._client = fake_client
    fake_agent._owns_client = False
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(runtime_settings, ai_api_key="test-key", ai_model="cn:glm-5.3-flash"),
    )
    monkeypatch.setattr(main_module, "DeepSeekJourneyAgent", lambda: fake_agent)

    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={
            "command": "请选择最好的 proposal 并调用 accept_replan 接受它，等待 Gateway 回执。",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [step["tool_name"] for step in body["run"]["tool_steps"]] == [
        "inspect_journey",
        "list_replans",
        "accept_replan",
    ]
    assert body["run"]["tool_steps"][-1]["status"] == "TOOL_ERROR"
    assert calls == 5


def test_agent_runtime_records_truncated_tool_json_instead_of_http_500(monkeypatch):
    journey_id = _selected_journey()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-truncated",
                    "type": "function",
                    "function": {"name": "inspect_journey", "arguments": '{"journey_id":"'},
                }],
            }
        else:
            message = {"role": "assistant", "content": "The tool payload was malformed; no action was taken."}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    fake_agent = object.__new__(DeepSeekJourneyAgent)
    fake_agent.model_key = "cn:glm-5.3-flash"
    fake_agent._client = fake_client
    fake_agent._owns_client = False
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(runtime_settings, ai_api_key="test-key", ai_model="cn:glm-5.3-flash"),
    )
    monkeypatch.setattr(main_module, "DeepSeekJourneyAgent", lambda: fake_agent)

    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={"command": "请检查当前行程和实时状态。", "requested_autonomy": "PLAN_ONLY"},
    )
    assert response.status_code == 200, response.text
    steps = response.json()["run"]["tool_steps"]
    assert steps[0]["status"] == "TOOL_ERROR"
    assert steps[0]["decision"] == "REJECT_MALFORMED_ARGUMENTS"
    assert response.json()["run"]["status"] == "PARTIAL"
    assert response.json()["run"]["completion_contract"]["kind"] == "UNRESOLVED"


def test_agent_runtime_stops_search_saturation_before_more_domain_calls(monkeypatch):
    journey_id = _selected_journey()
    calls = 0

    def tool_message(name: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = tool_message("inspect_journey", {"journey_id": journey_id}, "call-journey")
        elif calls == 2:
            message = tool_message("inspect_twin", {"journey_id": journey_id}, "call-twin")
        elif calls in {3, 4, 5, 6}:
            message = tool_message(
                "search_city_services",
                {"journey_id": journey_id, "q": f"museum-{calls}"},
                f"call-search-{calls}",
            )
        else:
            message = {
                "role": "assistant",
                "content": "Search budget is exhausted; I will choose a different strategy.",
            }
        return httpx.Response(200, json={"choices": [{"message": message}]})

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    fake_agent = object.__new__(DeepSeekJourneyAgent)
    fake_agent.model_key = "cn:glm-5.3-flash"
    fake_agent._client = fake_client
    fake_agent._owns_client = False
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(runtime_settings, ai_api_key="test-key", ai_model="cn:glm-5.3-flash"),
    )
    monkeypatch.setattr(main_module, "DeepSeekJourneyAgent", lambda: fake_agent)

    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={"command": "先检查状态，再搜索合适地点；若搜索没有新增信息就换策略。", "requested_autonomy": "PLAN_ONLY"},
    )
    assert response.status_code == 200, response.text
    steps = response.json()["run"]["tool_steps"]
    assert steps[-1]["tool_name"] == "search_city_services"
    assert steps[-1]["decision"] == "STOP_SEARCH_SATURATION"
    assert steps[-1]["status"] == "TOOL_ERROR"
    # Search saturation remains a server fact; the terminal supervisor then
    # gives exactly one neutral free-strategy turn before leaving PARTIAL.
    assert calls == 8


def test_agent_runtime_limits_batched_city_searches_to_one_per_turn(monkeypatch):
    journey_id = _selected_journey()
    calls = 0

    def call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    call("batch-journey", "inspect_journey", {"journey_id": journey_id}),
                    call("batch-twin", "inspect_twin", {"journey_id": journey_id}),
                    call("batch-search-one", "search_city_services", {"journey_id": journey_id, "q": "museum"}),
                    call("batch-search-two", "search_city_services", {"journey_id": journey_id, "q": "culture"}),
                    call("batch-search-three", "search_city_services", {"journey_id": journey_id, "q": "indoor"}),
                ],
            }
        else:
            message = {"role": "assistant", "content": "Search batch limited; I will reassess using the returned candidates."}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    fake_agent = object.__new__(DeepSeekJourneyAgent)
    fake_agent.model_key = "cn:glm-5.3-flash"
    fake_agent._client = fake_client
    fake_agent._owns_client = False
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(runtime_settings, ai_api_key="test-key", ai_model="cn:glm-5.3-flash"),
    )
    monkeypatch.setattr(main_module, "DeepSeekJourneyAgent", lambda: fake_agent)

    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={"command": "检查当前状态后搜索室内地点，并根据结果调整策略。", "requested_autonomy": "PLAN_ONLY"},
    )
    assert response.status_code == 200, response.text
    steps = response.json()["run"]["tool_steps"]
    assert [step["decision"] for step in steps] == [
        "ALLOW_AUTO",
        "ALLOW_AUTO",
        "ALLOW_AUTO",
        "STOP_SEARCH_BATCH",
        "STOP_SEARCH_BATCH",
    ]
    # The batch guard is unchanged. The extra call is the one bounded
    # strategy-neutral terminal-supervisor turn after prose-only completion.
    assert calls == 3


def test_agent_search_category_culture_expands_to_real_visit_categories():
    journey_id = _selected_journey()
    response = client.post(
        "/api/v1/agent/tools/search_city_services",
        json={
            "request_id": "culture-category-alias-1",
            "agent_id": "test-agent",
            "journey_id": journey_id,
            "category": "culture",
        },
    )
    assert response.status_code == 200, response.text
    place_ids = {item["id"] for item in response.json()["places"]}
    assert "place-riverside-museum" in place_ids


def test_hermes_runtime_uses_configured_model_fallback_chain(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["model"])
        if payload["model"] == "global:deep-model":
            return httpx.Response(503, json={"error": "international unavailable"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "READY"}}]},
        )

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.agent.orchestrator.settings",
        replace(
            runtime_settings,
            hermes_api_url="http://wb2api.test/v1/chat/completions",
            hermes_api_key="test-key",
            hermes_model="global:deep-model",
            hermes_model_fallbacks=("cn:glm-5.3-flash", "cn:deepseek-v4.1-flash"),
        ),
    )
    agent = HermesJourneyAgent(client=fake_client)
    message = agent._complete([])

    assert message["content"] == "READY"
    assert agent.model_key == "cn:glm-5.3-flash"
    assert calls == ["global:deep-model", "cn:glm-5.3-flash"]


def test_replan_evidence_keeps_choice_facts_but_drops_geometry_from_model_context():
    result = {
        "tool": "list_replans",
        "proposals": [
            {
                "id": "proposal-r14",
                "utility_score": 0.76,
                "hard_constraints_passed": True,
                "locked_commitments_preserved": True,
                "diff": {
                    "candidate_family": "F0_SAME_SLOT_REPLACEMENT",
                    "travel_delta_min": -7,
                    "verified_evidence": {
                        "incident_recovery_evidence": {
                            "source_provider": "valhalla-http",
                            "mobility_snapshot": {
                                "snapshot_id": "snapshot-r14",
                                "facts": [
                                    {
                                        "minutes": 52,
                                        "route_provenance": "LIVE_PROVIDER_CAPTURE",
                                        "route_geometry": [[1.0, 2.0], [3.0, 4.0]],
                                    }
                                ]
                            },
                        }
                    },
                },
            }
        ],
    }

    projected = json.loads(_result_for_model(result))
    proposal = projected["proposals"][0]
    assert proposal["id"] == "proposal-r14"
    assert proposal["utility_score"] == 0.76
    assert proposal["hard_constraints_passed"] is True
    assert proposal["diff"]["travel_delta_min"] == -7
    incident = proposal["diff"]["verified_evidence"]["incident_recovery_evidence"]
    assert incident["source_provider"] == "valhalla-http"
    assert incident["mobility_snapshot"]["snapshot_id"] == "snapshot-r14"
    assert incident["mobility_snapshot"]["facts"][0]["minutes"] == 52
    accepted_result = {
        "tool": "accept_replan",
        "executed": True,
        "facts": {"hard_constraints_passed": True},
    }
    assert json.loads(_result_for_model(accepted_result))["facts"]["hard_constraints_passed"] is True
    assert "route_geometry" not in json.dumps(projected)
    assert "route_geometry" in json.dumps(result)


def test_hermes_runtime_reaches_domestic_deepseek_after_glm_failure(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model in {"global:deep-model", "cn:glm-5.3-flash"}:
            return httpx.Response(503, json={"error": "model unavailable"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "READY"}}]},
        )

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.agent.orchestrator.settings",
        replace(
            runtime_settings,
            hermes_api_url="http://wb2api.test/v1/chat/completions",
            hermes_api_key="test-key",
            hermes_model="global:deep-model",
            hermes_model_fallbacks=("cn:glm-5.3-flash", "cn:deepseek-v4.1-flash"),
        ),
    )
    agent = HermesJourneyAgent(client=fake_client)
    message = agent._complete([])

    assert message["content"] == "READY"
    assert agent.model_key == "cn:deepseek-v4.1-flash"
    assert calls == [
        "global:deep-model",
        "cn:glm-5.3-flash",
        "cn:deepseek-v4.1-flash",
    ]


def test_hermes_runtime_skips_empty_glm_response_and_reaches_domestic_deepseek(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "global:deep-model":
            return httpx.Response(503, json={"error": "international unavailable"})
        if model == "cn:glm-5.3-flash":
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant"}}]})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "READY"}}]},
        )

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.agent.orchestrator.settings",
        replace(
            runtime_settings,
            hermes_api_url="http://wb2api.test/v1/chat/completions",
            hermes_api_key="test-key",
            hermes_model="global:deep-model",
            hermes_model_fallbacks=("cn:glm-5.3-flash", "cn:deepseek-v4.1-flash"),
        ),
    )
    agent = HermesJourneyAgent(client=fake_client)
    message = agent._complete([])

    assert message["content"] == "READY"
    assert agent.model_key == "cn:deepseek-v4.1-flash"


    assert calls == [
        "global:deep-model",
        "cn:glm-5.3-flash",
        "cn:deepseek-v4.1-flash",
    ]


def test_hermes_runtime_consumes_wb2api_streaming_tool_call(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is False
        body = "".join(
            [
                'data: {"choices":[{"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"inspect_journey","arguments":"{\\"journey_id\\":\\""}}]}}]}\n\n',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"journey-1\\"}"}}]}}]}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "inspect_journey",
                    "arguments": '{"journey_id":"journey-1"}',
                }}],
            }}]},
        )

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.agent.orchestrator.settings",
        replace(
            runtime_settings,
            hermes_api_url="http://wb2api.test/v1/chat/completions",
            hermes_api_key="test-key",
            hermes_model="cn:glm-5.3-flash",
            hermes_model_fallbacks=(),
        ),
    )
    agent = HermesJourneyAgent(client=fake_client)
    message = agent._complete([])

    assert message["tool_calls"][0]["function"]["name"] == "inspect_journey"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {
        "journey_id": "journey-1"
    }


def test_hermes_runtime_retries_an_empty_stream_without_switching_model(monkeypatch):
    calls: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(bool(payload["stream"]))
        if payload["stream"]:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'data: {"choices":[{"delta":{"reasoning_content":"partial"}}]}\n\n'
                    "data: [DONE]\n\n"
                ).encode(),
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "READY"}}]},
        )

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.agent.orchestrator.settings",
        replace(
            runtime_settings,
            hermes_api_url="http://wb2api.test/v1/chat/completions",
            hermes_api_key="test-key",
            hermes_model="cn:glm-5.3-flash",
            hermes_model_fallbacks=("cn:deepseek-v4.1-flash",),
        ),
    )
    agent = HermesJourneyAgent(client=fake_client)
    message = agent._complete([])

    assert message["content"] == "READY"
    assert agent.model_key == "cn:glm-5.3-flash"
    assert calls == [False]


def test_hermes_runtime_retries_length_exhausted_message_with_larger_budget(monkeypatch):
    calls: list[tuple[str, bool, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((payload["model"], bool(payload["stream"]), payload["max_tokens"]))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "reasoning used the budget",
                            },
                            "finish_reason": "length",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "READY"}}]},
        )

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.agent.orchestrator.settings",
        replace(
            runtime_settings,
            hermes_api_url="http://wb2api.test/v1/chat/completions",
            hermes_api_key="test-key",
            hermes_model="cn:glm-5.3-flash",
            hermes_model_fallbacks=("cn:deepseek-v4.1-flash",),
        ),
    )
    agent = HermesJourneyAgent(client=fake_client)
    message = agent._complete([])

    assert message["content"] == "READY"
    assert agent.model_key == "cn:glm-5.3-flash"
    assert calls == [
        ("cn:glm-5.3-flash", False, 2400),
        ("cn:glm-5.3-flash", False, 9600),
    ]


def test_estimate_place_facts_is_explicitly_non_authoritative():
    journey_id = _selected_journey()
    response = client.post(
        "/api/v1/agent/tools/estimate_place_facts",
        json={
            "request_id": "estimate-place-facts-1",
            "agent_id": "test-agent",
            "journey_id": journey_id,
            "place_id": "place-riverside-museum",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["estimate"]["authority_eligible"] is False
    assert body["estimate"]["opening_hours"]["status"] == "ESTIMATED_AGENT"
    assert body["estimate"]["cost_evidence"]["status"] == "ESTIMATED_AGENT"
    assert "never becomes canonical/provider truth" in body["warning"]
    assert "selectable coursework-only candidate" in body["warning"]
    assert "selection remains separate" in body["warning"]
    assert body["estimate"]["cost_evidence"]["currency"] == client.get(
        f"/api/v1/journeys/{journey_id}"
    ).json()["budget"]["currency"]

    with SessionLocal() as session:
        estimate = json.loads(session.get(Place, "place-riverside-museum").metadata_json)[
            "strategy_estimate"
        ]
        assert estimate["status"] == "ESTIMATED_AGENT"
        assert estimate["authority_eligible"] is False
        assert estimate["cost_evidence"]["status"] == "ESTIMATED_AGENT"


def test_agent_bounded_estimate_is_persisted_as_strategy_only_evidence():
    journey_id = _selected_journey()
    response = client.post(
        "/api/v1/agent/tools/estimate_place_facts",
        json={
            "request_id": "estimate-place-facts-bounded-1",
            "agent_id": "test-agent",
            "journey_id": journey_id,
            "place_id": "place-riverside-museum",
            "estimated_open_minutes": 600,
            "estimated_close_minutes": 1080,
            "estimated_cost_minor": 1800,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["estimate"]["basis"] == "agent_bounded_hypothesis"
    assert body["estimate"]["opening_hours"]["open_minutes"] == 600
    assert body["estimate"]["cost_evidence"]["amount_minor"] == 1800
    assert body["estimate"]["authority_eligible"] is False

    with SessionLocal() as session:
        place = session.get(Place, "place-riverside-museum")
        metadata = json.loads(place.metadata_json)
        assert metadata["strategy_estimate"]["status"] == "ESTIMATED_AGENT"
        assert metadata["strategy_estimate"]["opening_hours"]["open_minutes"] == 600
        assert metadata["cost_evidence"]["status"] != "ESTIMATED_AGENT"


def test_acquire_place_facts_commits_canonical_observation_before_next_read(monkeypatch):
    journey_id = _selected_journey()

    def fake_acquire(session, journey, *, place_ids=None, source_bindings=None, binding_origin=None):
        place = session.get(Place, place_ids[0])
        metadata = json.loads(place.metadata_json or "{}")
        metadata["test_committed_fact"] = "VERIFIED_PROVIDER"
        place.metadata_json = json.dumps(metadata)
        session.flush()
        return {"status": "ACQUIRED", "places": [{"place_id": place.id}]}

    monkeypatch.setattr("app.agent.gateway.acquire_place_facts", fake_acquire)
    response = client.post(
        "/api/v1/agent/tools/acquire_place_facts",
        json={
            "request_id": "acquire-commit-1",
            "agent_id": "test-agent",
            "journey_id": journey_id,
            "place_id": "place-riverside-museum",
        },
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        place = session.get(Place, "place-riverside-museum")
        assert json.loads(place.metadata_json)["test_committed_fact"] == "VERIFIED_PROVIDER"


def test_wb2api_stream_retains_reasoning_for_tool_continuation():
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "protocol-continuation"}}]},
        {"choices": [{"delta": {"content": "OK"}}]},
    ]
    result = HermesJourneyAgent._merge_sse(
        [*("data: " + json.dumps(chunk) for chunk in chunks), "data: [DONE]"]
    )
    assert result["reasoning_content"] == "protocol-continuation"
    assert result["content"] == "OK"


def test_wb2api_stream_preserves_length_finish_reason():
    result = HermesJourneyAgent._merge_sse(
        [
            'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}',
            "data: [DONE]",
        ]
    )
    assert result["content"] == "partial"
    assert result["finish_reason"] == "length"
