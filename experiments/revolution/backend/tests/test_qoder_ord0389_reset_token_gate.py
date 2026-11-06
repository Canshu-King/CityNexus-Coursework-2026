"""ORD-0389: `POST /api/v1/demo/reset` needs a named token, and refusing must cost nothing.

Measured cause: at 03:55 two `POST /api/v1/demo/reset HTTP/1.1 200 OK` landed on the shared
preview backend (`D:/moodle/_qoder_preview_20260920/run/cmdr_hop1_0018.log:411` and `:432`) and
the scenario people were looking at was replaced.  Nothing in the code identified the caller, and
nothing could have: the only door was `demo_mode_enabled()`, and the preview is *started* with
`CITYNEXUS_DEMO_MODE=true` (`run/cmdr_hop1_backend.bat`).  So on the one deployment that matters
the switch that made the route possible was also the whole authorization for it.

The contract these arms pin:

* the product default is "no token configured", which makes the route refuse every caller -- an
  out-of-the-box deployment cannot be emptied through it at all;
* a missing token and a wrong token are two named codes (`RESET_TOKEN_REQUIRED`,
  `RESET_TOKEN_INVALID`), neither of which is a bare 403 or a borrowed generic code;
* neither refusal may touch anything: no row deleted, and not even `stop_autorun()`, which is a
  process-global side effect a refused request has no business causing;
* the deployment's configuration is not observable through this route -- "no token configured"
  and "token did not match" answer the same shape, because the first is otherwise a free probe
  for "can this box be reset".
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import Activity, Journey, PlanVersion, Traveler
from app.db.session import SessionLocal
from app.main import (
    DEMO_RESET_ENV,
    DEMO_RESET_HEADER,
    app,
    demo_reset_token_denied,
)

client = TestClient(app)
TABLES = ("travelers", "journeys", "plan_versions", "activities")
RIGHT = "ord0389-arm-token"


def _counts() -> dict[str, int]:
    with SessionLocal() as session:
        return {
            "travelers": session.scalar(select(func.count(Traveler.id))) or 0,
            "journeys": session.scalar(select(func.count(Journey.id))) or 0,
            "plan_versions": session.scalar(select(func.count(PlanVersion.id))) or 0,
            "activities": session.scalar(select(func.count(Activity.id))) or 0,
        }


def _journey_ids() -> list[str]:
    with SessionLocal() as session:
        return sorted(str(r) for r in session.scalars(select(Journey.id)).all())


@pytest.fixture()
def _stage_on(monkeypatch) -> None:
    """Demo mode on, and a token configured, so the flag guard cannot be the one answering."""

    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    monkeypatch.setenv(DEMO_RESET_ENV, RIGHT)


def _error_of(response):
    assert response.status_code == 403, response.text
    return response.json()["error"]


def test_a_reset_without_a_token_is_refused_and_deletes_nothing(_stage_on) -> None:
    """Acceptance A: the named 403, and the library byte-identical across the refusal."""

    # Seed something real first, so "nothing was deleted" is a claim about rows that exist.
    assert client.post(
        "/api/v1/demo/reset", headers={DEMO_RESET_HEADER: RIGHT}
    ).status_code == 200
    before, before_ids = _counts(), _journey_ids()
    assert before["journeys"] > 0, "the arm is vacuous unless a journey exists to lose"

    body = _error_of(client.post("/api/v1/demo/reset"))

    assert body["code"] == "RESET_TOKEN_REQUIRED", body
    assert body["message_key"] == "error.reset_token_required", body
    assert body["details"]["header"] == DEMO_RESET_HEADER, body
    assert body["details"]["tables_protected"] is True, body
    assert _counts() == before, "a refused reset moved a row count"
    assert _journey_ids() == before_ids, "a refused reset moved the journey set"


def test_refusing_calls_not_even_the_process_global_guardian_stop(_stage_on, monkeypatch) -> None:
    """`stop_autorun()` is a side effect, and the guard sits ahead of it on purpose."""

    calls = []
    monkeypatch.setattr("app.main.stop_autorun", lambda *a, **k: calls.append("stop_autorun"))

    client.post("/api/v1/demo/reset")
    client.post("/api/v1/demo/reset", headers={DEMO_RESET_HEADER: "nope"})

    assert calls == [], f"both refusals must stop nothing; got {calls}"
    assert client.post(
        "/api/v1/demo/reset", headers={DEMO_RESET_HEADER: RIGHT}
    ).status_code == 200
    assert calls == ["stop_autorun"], "the accepted reset must still stop the guardian"


def test_a_wrong_token_is_refused_named_and_deletes_nothing(_stage_on) -> None:
    """Acceptance C: a presented-but-wrong token is its own code, not a generic 403."""

    before, before_ids = _counts(), _journey_ids()

    for wrong in ("", RIGHT[:-1], RIGHT.upper(), RIGHT + "x", "Bearer " + RIGHT):
        body = _error_of(client.post("/api/v1/demo/reset", headers={DEMO_RESET_HEADER: wrong}))
        # `""` never reaches the mismatch arm: an empty header is "not presented" to Starlette.
        assert body["code"] == (
            "RESET_TOKEN_REQUIRED" if wrong == "" else "RESET_TOKEN_INVALID"
        ), (wrong, body)
        assert body["details"]["tables_protected"] is True, body

    assert _counts() == before and _journey_ids() == before_ids


def test_missing_and_wrong_are_the_same_shape_because_configuration_is_not_a_public_answer(
    _stage_on,
) -> None:
    """The two refusals differ in code and agree in everything a caller can compare."""

    missing = _error_of(client.post("/api/v1/demo/reset"))
    wrong = _error_of(client.post("/api/v1/demo/reset", headers={DEMO_RESET_HEADER: "x"}))

    assert set(missing) == set(wrong), "one envelope, not two shapes"
    assert missing["code"] != wrong["code"], "but they are separately nameable"
    assert missing["details"] == wrong["details"], (
        "details must not reveal whether this deployment configured a token: an unconfigured "
        "box answers RESET_TOKEN_INVALID to every presented token, which is the same body a "
        "real mismatch produces"
    )
    assert RIGHT not in str(wrong) and RIGHT not in str(missing), "the token leaks"


def test_an_unconfigured_deployment_refuses_every_token_it_could_be_given(monkeypatch) -> None:
    """Requirement 2: out of the box this route is not usable by anyone."""

    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    monkeypatch.delenv(DEMO_RESET_ENV, raising=False)

    assert os_environ_has_no_reset_token(), "the product default must be unset"

    refused = client.post("/api/v1/demo/reset", headers={DEMO_RESET_HEADER: "anything"})
    assert refused.status_code == 403, refused.text
    assert _error_of(refused)["code"] == "RESET_TOKEN_INVALID", refused.text
    assert _error_of(client.post("/api/v1/demo/reset"))["code"] == "RESET_TOKEN_REQUIRED"


def os_environ_has_no_reset_token() -> bool:
    import os

    return not os.environ.get(DEMO_RESET_ENV)


def test_the_flag_guard_still_answers_first_when_demo_mode_is_off(monkeypatch) -> None:
    """Order is part of the contract: an existing pinned behaviour must not change code."""

    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: False)
    monkeypatch.setenv(DEMO_RESET_ENV, RIGHT)

    body = _error_of(client.post("/api/v1/demo/reset", headers={DEMO_RESET_HEADER: RIGHT}))
    assert body["code"] == "DEMO_MODE_REQUIRED", body
    assert body["message_key"] == "demo.reset_disabled", body


def test_the_pure_gate_refuses_unset_without_reading_a_secret() -> None:
    """The decision is one function, so both states are checkable without a live server."""

    assert demo_reset_token_denied(None) is not None
    assert demo_reset_token_denied("")[0] == "RESET_TOKEN_REQUIRED"
    assert demo_reset_token_denied("anything")[0] == "RESET_TOKEN_INVALID"


def test_reset_with_the_right_token_actually_deletes_and_reseeds(_stage_on) -> None:
    """Acceptance B: the door really opens.

    The fixture is frozen on purpose (`services.seed_demo`: "two reset/seed runs produce the same
    logical rows"), so the demo ids repeat and comparing them across two resets proves nothing.
    What does prove it is a row the fixture does not know about: plant one, reset, and show it is
    gone while the scenario came back.
    """

    assert client.post(
        "/api/v1/demo/reset", headers={DEMO_RESET_HEADER: RIGHT}
    ).status_code == 200

    planted = "ord0389-not-in-the-fixture"
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=planted,
                display_name="planted by ORD-0389",
                email=None,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                created_at="2026-09-28T00:00:00+08:00",
                updated_at="2026-09-28T00:00:00+08:00",
            )
        )
        session.commit()
    before = _counts()
    assert planted in _traveler_ids(), "the planted row is not in the store the route will read"

    response = client.post("/api/v1/demo/reset", headers={DEMO_RESET_HEADER: RIGHT})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "reset", payload
    after = _counts()

    assert planted not in _traveler_ids(), (
        "reset accepted the token but did not delete: the row the fixture does not own survived"
    )
    assert payload["traveler_id"] in _traveler_ids(), "reset did not reseed the demo traveler"
    assert payload["journey_id"] in _journey_ids(), "reset did not reseed the demo journey"
    assert after["travelers"] == before["travelers"] - 1, (
        "travelers %s -> %s, expected the planted row to be the only difference"
        % (before["travelers"], after["travelers"])
    )
    # Measured on this route: reset reseeds exactly one traveler and one journey.
    # plan_versions/activities stay at 0 on purpose -- `test_qoder_simulation_mode.py` pins that a
    # fixture door "may reset state and nothing else", because seeding a plan from here once leaked
    # a SELECTED plan into GET /plans and turned 35 gates red.
    assert after["travelers"] == 1 and after["journeys"] == 1, after
    assert after["plan_versions"] == 0, "reset must not seed a plan (see simulation-mode gate)"
    assert after["activities"] == 0, after


def _traveler_ids() -> list[str]:
    with SessionLocal() as session:
        return sorted(str(r) for r in session.scalars(select(Traveler.id)).all())
