"""ORD-0359 - "the AI is not answering" stops being one word for three facts.

Measured at the base commit `b6a8ea5c625d7e3e82f7348fad57d595a8a403c0`, `backend/app/main.py`
answered `/health` with `"ai": "enabled" if settings.ai_enabled and settings.ai_api_key else
"disabled"`.  An operator who switched the feature off, a store nobody installed a credential in,
and a provider that looked at the request and refused it all came back as the same string, so the
screen had no way to tell them apart and the sentence it had lying around for an unexplained
failure was 「网络或后端没有响应」 (`frontend/src/features/planning/PlanWithAiSheet.tsx`) - an
excuse about the traveller's connection for a fact about this server.

These tests are written so the shortcuts cannot pass:

* the three facts must produce three *different* words, in one test that goes red the moment
  anybody collapses the vocabulary back to one (`test_the_three_causes_are_three_different_words`);
* the operator's switch must never be reported as the provider's refusal or as a network fault
  (`test_the_operators_switch_is_never_a_refusal_and_never_a_network_fault`);
* an unrecognised server sentence names nothing - refusing to guess is a required behaviour, not
  a gap (`test_a_reason_this_build_does_not recognise_names_nothing`);
* the two fields that already exist (`ai`, `deepseek_configured`) must come out byte-identical,
  because an additive contract that silently changed them would break the clients we have;
* nothing depends on a credential: every arm sets the settings it means to test, and the module
  is green both with `CITYNEXUS_AI_ENABLED=false` and with the environment left as the shell has
  it (see the block for how to reproduce both runs).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agent import ai_availability as mod
from app.agent.ai_availability import (
    AI_DISABLED_BY_OPS,
    AI_NOT_CONFIGURED,
    AI_PROVIDER_REFUSED,
    AI_CONFIGURED_UNVERIFIED,
    ai_availability_state,
    ai_state_from_failure_reason,
)
from app.agent.runtime_identity import runtime_status
from app.main import app

client = TestClient(app)


def _configure(monkeypatch, *, enabled: bool, key: str | None) -> None:
    """Replace the settings object the producer reads, so no arm inherits this machine's env.

    `app.core.config.settings` is a frozen dataclass whose fields are read at import time, so
    patching a field raises `FrozenInstanceError`; the producer holds the object in its own
    namespace, and replacing that is both possible and enough - `/health`, `runtime_status()` and
    the mission payload all reach the state through this one function.
    """

    monkeypatch.setattr(
        mod,
        "settings",
        SimpleNamespace(
            ai_enabled=enabled,
            ai_api_key=key,
            hermes_api_url=None,
            hermes_api_key=None,
        ),
        raising=False,
    )


# --------------------------------------------------------------- R1 three names, one place


def test_the_three_causes_are_three_different_words(monkeypatch) -> None:
    """The collapse-must-go-red arm.

    One assertion per cause, then a set comparison: any implementation that returns the same word
    for two of these facts fails here even if every individual value looks plausible.
    """

    _configure(monkeypatch, enabled=False, key="test-key-present")
    operators_switch = ai_availability_state()

    _configure(monkeypatch, enabled=True, key=None)
    missing_credential = ai_availability_state()

    _configure(monkeypatch, enabled=True, key="test-key-present")
    provider_said_no = ai_availability_state(provider_refused=True)

    assert operators_switch == AI_DISABLED_BY_OPS, operators_switch
    assert missing_credential == AI_NOT_CONFIGURED, missing_credential
    assert provider_said_no == AI_PROVIDER_REFUSED, provider_said_no
    assert len({operators_switch, missing_credential, provider_said_no}) == 3, (
        operators_switch,
        missing_credential,
        provider_said_no,
    )
    # and the fourth name is not one of the three complaints
    assert ai_availability_state() == AI_CONFIGURED_UNVERIFIED


def test_the_producer_is_the_only_place_that_invents_a_word(monkeypatch) -> None:
    """Every server door must read the same word for the same fact."""

    _configure(monkeypatch, enabled=False, key=None)

    health = client.get("/api/v1/health").json()
    status = runtime_status()

    assert health["ai_state"] == AI_DISABLED_BY_OPS, health
    assert status["ai_state"] == AI_DISABLED_BY_OPS, status
    assert health["ai_state"] == status["ai_state"], (health, status)


def test_the_old_fields_came_out_unchanged(monkeypatch) -> None:
    """只加不改不删: `ai` and the two booleans keep exactly their old shapes.

    Without this arm, "additive" would be a claim about intent rather than a measured field.
    """

    _configure(monkeypatch, enabled=False, key=None)

    health = client.get("/api/v1/health").json()
    status = runtime_status()

    assert health["ai"] == "disabled", health
    assert "enabled" not in (health["ai"],), health
    assert status["deepseek_configured"] is False, status
    assert status["hermes_compat_configured"] is False, status
    # the new key is a sibling, not a replacement
    assert set(["status", "database", "ai"]).issubset(health), health


def test_ai_state_vocabulary_is_closed(monkeypatch) -> None:
    """A door may not coin a fifth word, and the value may not carry credential material."""

    _configure(monkeypatch, enabled=True, key="test-key-present")

    for kwargs in ({}, {"provider_refused": True}):
        value = ai_availability_state(**kwargs)
        assert value in mod.AI_STATES, value
        assert "key" not in value.lower() and "token" not in value.lower(), value


# ------------------------------------------- R2 the operator's switch is not someone's fault


def test_the_operators_switch_is_never_a_refusal_and_never_a_network_fault(monkeypatch) -> None:
    """The second mutate arm.

    Rewriting DISABLED_BY_OPS as a provider refusal (or as any network-flavoured word, which is
    what the screen used it as) has to fail here: the recorded reason wins over the switch, and
    the switch on its own never lands in the refusal name.
    """

    _configure(monkeypatch, enabled=False, key="test-key-present")

    assert ai_availability_state() == AI_DISABLED_BY_OPS
    assert ai_availability_state() != AI_PROVIDER_REFUSED

    # a switch that is off does not rename a refusal this server actually recorded
    refused = ai_state_from_failure_reason("DeepSeek Agent completion failed")
    assert refused == AI_PROVIDER_REFUSED, refused

    # and no state name reads like a connection problem
    for state in mod.AI_STATES:
        lowered = state.lower()
        for network_word in ("network", "offline", "unreachable", "connection"):
            assert network_word not in lowered, (state, network_word)


def test_a_missing_credential_is_named_as_a_missing_credential(monkeypatch) -> None:
    """「缺凭据，不关你的事」 - the fact is about this server, and it is not the traveller's."""

    _configure(monkeypatch, enabled=True, key=None)

    assert ai_availability_state() == AI_NOT_CONFIGURED
    assert ai_state_from_failure_reason("DeepSeek API key is not configured") == AI_NOT_CONFIGURED


def test_a_reason_this_build_does_not_recognise_names_nothing(monkeypatch) -> None:
    """No invented attribution: an unrecognised sentence yields None, not the nearest state."""

    _configure(monkeypatch, enabled=True, key="test-key-present")

    assert ai_state_from_failure_reason("something the planner said") is None
    assert ai_state_from_failure_reason(None) is None
    assert ai_state_from_failure_reason("") is None


# ----------------------------------------------------------------- R4 the mission keeps its own words


def test_a_mission_that_stored_its_reason_is_read_from_that_reason(monkeypatch) -> None:
    """The state beside `failure_reason` must be the mission's own, not the current settings.

    Otherwise a run that was refused by the provider would be re-labelled NOT_CONFIGURED the
    moment an operator toggled the switch, which is the same scapegoat in a new coat.
    """

    from app.agent.mission import mission_payload
    from app.db.models import Journey, JourneyMission, Traveler
    from app.db.session import SessionLocal

    _configure(monkeypatch, enabled=False, key=None)
    now = datetime.now(timezone.utc).isoformat()
    traveler_id, journey_id, mission_id = str(uuid4()), str(uuid4()), str(uuid4())
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="ORD-0359 probe",
                email=None,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            Journey(
                id=journey_id,
                traveler_id=traveler_id,
                title="ORD-0359",
                city_code="CN-XIAMEN",
                timezone="Asia/Shanghai",
                start_at=now,
                end_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                budget_currency="CNY",
                budget_amount_minor=90000,
                planning_mode="balanced",
                status="DRAFT",
                active_plan_version_id=None,
                current_activity_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            JourneyMission(
                id=mission_id,
                journey_id=journey_id,
                traveler_id=traveler_id,
                user_text="plan it",
                status="DEGRADED",
                failure_reason="DeepSeek Agent completion failed",
                created_at=now,
                completed_at=now,
            )
        )
        session.commit()
        mission = session.get(JourneyMission, mission_id)
        payload = mission_payload(mission)

    assert payload["failure_reason"] == "DeepSeek Agent completion failed", payload
    assert payload["ai_state"] == AI_PROVIDER_REFUSED, payload
    # the switch is off in this arm, and that must not have overwritten the run's own reason
    assert payload["ai_state"] != AI_DISABLED_BY_OPS, payload


# ------------------------------------------------ ORD-0359 r3: one word per cause, everywhere


def test_the_positive_word_cannot_be_read_as_a_working_model(monkeypatch) -> None:
    """r2 emitted `READY` for a store holding a garbage credential string.

    Measured on a private backend at 4ef864cfa49b805e3d41b7f079f5fb4184ccec3d: a deliberately fake
    key produced `ai=enabled, ai_state=READY`. "Ready" is a promise about the next request, and the
    only fact available at that moment is that some string is configured, so the vocabulary now
    says the weaker thing. This arm fails if the old word comes back or if the new one is
    reintroduced as a claim of working.
    """

    _configure(monkeypatch, enabled=True, key="definitely-not-a-real-key")

    state = ai_availability_state()

    assert state == "CONFIGURED_UNVERIFIED", state
    assert state != "READY", state
    # Substring checks are the trap here: `CONFIGURED_UNVERIFIED` legitimately contains
    # "verified", so a naive `"verified" not in state` fails on the correct answer. Compare whole
    # words, and require the negation to be present rather than implying success by absence.
    assert state.split("_")[-1] != "READY", state
    assert "UNVERIFIED" in state, state
    assert state.upper() not in {"READY", "AVAILABLE", "OK", "WORKING"}, state


def test_health_and_a_run_read_route_answer_with_the_same_word(monkeypatch) -> None:
    """The order's requirement: the read route and the health endpoint give one word."""

    _configure(monkeypatch, enabled=False, key=None)

    health = client.get("/api/v1/health").json()
    status = runtime_status()

    assert health["ai_state"] == "DISABLED_BY_OPS", health
    assert status["ai_state"] == health["ai_state"], (status["ai_state"], health["ai_state"])


def test_a_stored_reason_and_the_state_table_round_trip(monkeypatch) -> None:
    """The sentence a run stores is written from the table and reads back into the same state.

    Before r3 the skip path stored a fixed literal whatever the reason was, so a run could claim a
    missing credential while the server's own switch word said something else.
    """

    from app.agent.ai_availability import reason_sentence

    _configure(monkeypatch, enabled=False, key="test-key-present")

    state = ai_availability_state()
    sentence = reason_sentence(state)
    read_back = ai_state_from_failure_reason(sentence)

    assert state == AI_DISABLED_BY_OPS, state
    assert sentence == "AI is switched off by the operator", sentence
    assert read_back == state, (sentence, read_back)
    # sentences already stored by the previous tip still resolve - rows are real user state
    assert ai_state_from_failure_reason("DeepSeek API key is not configured") == AI_NOT_CONFIGURED
    assert ai_state_from_failure_reason("DeepSeek Agent completion failed") == AI_PROVIDER_REFUSED


def test_reason_sentence_refuses_to_invent_a_word_for_an_unknown_state() -> None:
    import pytest

    from app.agent.ai_availability import reason_sentence

    with pytest.raises(ValueError):
        reason_sentence("SOMETHING_ELSE")


def test_the_skip_path_stores_this_state_not_a_fixed_literal(monkeypatch) -> None:
    """The arm that makes the wiring, not the vocabulary table, the thing under test.

    `unavailable_run_payload` used to write one literal sentence whatever the reason was. Driving
    it with the operator switch off (and a credential present, so the old literal would have been
    wrong in two ways at once) proves the stored reason comes from the same call `/health` makes.
    """

    from app.agent.runtime import unavailable_run_payload
    from app.db.models import Journey, JourneyMission, Traveler
    from app.db.session import SessionLocal
    from app.agent.ai_availability import reason_sentence

    _configure(monkeypatch, enabled=False, key="a-key-is-present")
    now = datetime.now(timezone.utc).isoformat()
    traveler_id, journey_id, mission_id = str(uuid4()), str(uuid4()), str(uuid4())
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id, display_name="ORD-0359 r3", email=None, locale="zh-CN",
                timezone="Asia/Shanghai", created_at=now, updated_at=now,
            )
        )
        session.flush()
        session.add(
            Journey(
                id=journey_id, traveler_id=traveler_id, title="ORD-0359 r3",
                city_code="CN-XIAMEN", timezone="Asia/Shanghai", start_at=now,
                end_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                budget_currency="CNY", budget_amount_minor=90000, planning_mode="balanced",
                status="DRAFT", active_plan_version_id=None, current_activity_id=None,
                created_at=now, updated_at=now,
            )
        )
        session.flush()
        mission = JourneyMission(
            id=mission_id, journey_id=journey_id, traveler_id=traveler_id,
            user_text="plan it", status="SKIPPED_EXTERNAL_DEPENDENCY",
            created_at=now,
        )
        session.add(mission)
        session.commit()
        payload = unavailable_run_payload(session, journey=session.get(Journey, journey_id),
                                          mission=session.get(JourneyMission, mission_id))
        session.rollback()

    assert payload["ai_state"] == AI_DISABLED_BY_OPS, payload
    assert payload["failure_reason"] == reason_sentence(AI_DISABLED_BY_OPS), payload
    assert payload["failure_reason"] != "DeepSeek API key is not configured", payload
    assert payload["mission"]["ai_state"] == AI_DISABLED_BY_OPS, payload["mission"]
