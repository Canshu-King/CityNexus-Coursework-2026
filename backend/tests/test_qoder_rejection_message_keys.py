"""ORD-0055 - a rejection has to say which rule it is, not just what a developer wrote.

Measured on the authoritative production build (`576fbf6`, read off the screen): the wizard
was entirely in Chinese, and two 422s rendered as "The trip is longer than the supported
journey window of 7 days. Shorten the dates and try again." and "Request validation failed -
body.end_at: Input should be a valid date".  A traveller with a phone cannot act on those,
and the frontend cannot translate them safely either, because the only handle was prose that
pydantic rewords between versions.

The contract these gates pin:

* every error envelope carries `message_key`, and the key is derived from the *code* or the
  rule *type* - never from `msg`;
* `details.errors[].loc` still decides which field is at fault, and nothing here moves that
  judgement into the frontend;
* the English `message` survives as a log line and as an unknown-key fallback, which is why
  the keys are asserted to be present *and* the messages unchanged;
* an unexpected `ValueError` is still a 500 - naming rejections must not become a way to
  launder a bug into a tidy-looking 4xx.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.temporal import MAX_JOURNEY_DAYS
from app.main import (
    TEMPORAL_MESSAGE_KEYS,
    TEMPORAL_REJECTIONS,
    VALIDATION_MESSAGE_KEYS,
    app,
    temporal_rejection,
    validation_message_key,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))
KEY_SHAPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def _traveler() -> str:
    return client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["traveler_id"]


def _at(day: int, hour: int = 8) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=TZ)


def _body(**override: object) -> dict:
    start = _at(26)
    payload = {
        "traveler_id": str(uuid4()),
        "title": "keys",
        "city_code": "CNX-DEMO",
        "timezone": "Asia/Shanghai",
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(days=1, hours=8)).isoformat(),
        "budget": {"amount_minor": 20000, "currency": "CNY"},
        "planning_mode": "balanced",
    }
    payload.update(override)
    return payload


def _error_of(response) -> dict:
    assert response.status_code in (404, 409, 422), f"{response.status_code} {response.text[:200]}"
    body = response.json()["error"]
    assert isinstance(body, dict), response.text[:400]
    return body


# ------------------------------------------------------------------- the named refusals


def test_the_window_refusal_carries_the_key_a_screen_needs() -> None:
    traveler = _traveler()
    response = client.post(
        "/api/v1/journeys",
        json=_body(
            traveler_id=traveler,
            end_at=(_at(26) + timedelta(days=MAX_JOURNEY_DAYS + 1, hours=8)).isoformat(),
        ),
    )
    body = _error_of(response)
    assert body["message_key"] == "journey.days_out_of_bounds", body
    # ORD-0033's contract is untouched by this order: the code and the numbers stay, and the
    # key sits beside them rather than replacing them.
    assert body["code"] == "V13_JOURNEY_DAYS_OUT_OF_BOUNDS", body
    assert body["details"]["actual_days"] == MAX_JOURNEY_DAYS + 2, body["details"]
    assert body["details"]["max_days"] == MAX_JOURNEY_DAYS, body["details"]
    assert "7 days" in body["message"], body["message"]
    assert body["trace_id"], body


def test_the_anchor_order_refusal_has_its_own_key() -> None:
    """Two temporal rejections, two keys: one screen message cannot serve both."""

    response = asyncio.run(temporal_rejection(None, ValueError("V13_INVALID_ANCHOR_WINDOW")))
    body = json.loads(response.body.decode("utf-8"))["error"]
    assert response.status_code == 422, response.status_code
    assert body["message_key"] == "journey.anchor_order", body
    assert body["details"]["reason"] == "V13_INVALID_ANCHOR_WINDOW", body
    assert body["message"] == TEMPORAL_REJECTIONS["V13_INVALID_ANCHOR_WINDOW"], body


def test_the_two_temporal_maps_describe_one_domain() -> None:
    """No rejection may be named for HTTP while staying untranslatable, and vice versa."""

    assert set(TEMPORAL_MESSAGE_KEYS) == set(TEMPORAL_REJECTIONS), (
        sorted(TEMPORAL_MESSAGE_KEYS),
        sorted(TEMPORAL_REJECTIONS),
    )
    keys = list(TEMPORAL_MESSAGE_KEYS.values())
    assert len(set(keys)) == len(keys), keys
    for key in keys:
        assert KEY_SHAPE.match(key), key


# ------------------------------------------------------------- field-level validation


def test_a_field_level_422_names_the_field_by_loc_and_the_rule_by_key() -> None:
    _traveler()
    response = client.post("/api/v1/journeys", json=_body(end_at="not-a-date"))
    body = _error_of(response)
    assert body["message_key"] == "request.invalid", body
    errors = body["details"]["errors"]
    assert errors, body["details"]
    first = errors[0]
    # The two halves of the arbitration's field-mapping rule, in one response: `loc` says
    # where, `message_key`/`type` say what.  Neither is derived from the other.
    assert first["loc"] == ["body", "end_at"], first
    assert first["message_key"] == VALIDATION_MESSAGE_KEYS[first["type"]], first
    assert KEY_SHAPE.match(first["message_key"]), first
    assert first["msg"], "the developer string stays for logs and for unknown-key fallback"


def test_a_missing_field_is_named_by_its_type_not_its_english_words() -> None:
    _traveler()
    payload = _body()
    del payload["city_code"]
    response = client.post("/api/v1/journeys", json=payload)
    errors = _error_of(response)["details"]["errors"]
    missing = [item for item in errors if "city_code" in item["loc"]]
    assert missing, errors
    assert missing[0]["type"] == "missing", missing[0]
    assert missing[0]["message_key"] == "validation.field_missing", missing[0]


def test_the_key_cannot_be_reached_by_matching_the_message() -> None:
    """Same `type`, different words, same key - which is the whole point of the rule."""

    from_reworded = validation_message_key(
        {"loc": ["body", "end_at"], "msg": "Totally different prose someone shipped", "type": "missing"}
    )
    from_plain = validation_message_key({"loc": ["body", "end_at"], "msg": "Field required", "type": "missing"})
    assert from_reworded == from_plain == "validation.field_missing"
    # An unlisted rule type still gets a key, so a new pydantic rule cannot arrive as
    # an unlabelled English string; and no message text leaks into it.
    novel = validation_message_key({"loc": [], "msg": "Input should be a valid date", "type": "value_error.novel"})
    assert novel == "validation.value.error.novel", novel
    assert KEY_SHAPE.match(novel), novel
    assert validation_message_key({"loc": [], "msg": "x", "type": None}) == "validation.unknown"


# ------------------------------------------------------------------- the rest of the doors


def test_a_missing_resource_is_named_without_baking_its_id_into_the_key() -> None:
    journey_id = uuid4()
    body = _error_of(client.get(f"/api/v1/journeys/{journey_id}"))
    assert body["message_key"] == "resource.not_found", body
    assert body["code"] == "NOT_FOUND", body
    # The identifier belongs to `message` - the id is still there for a human chasing a log -
    # and must not reach the lookup key, because a key per journey would leave every 404
    # untranslatable again.
    assert str(journey_id) in body["message"], body["message"]
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", body["message_key"]), body


def test_every_rejection_this_lane_can_produce_carries_a_key() -> None:
    """Sweep the shapes a wizard can actually hit, and fail if one answer has no key."""

    traveler = _traveler()
    responses = [
        client.post("/api/v1/journeys", json=_body(traveler_id=traveler, end_at="nope")),
        client.post(
            "/api/v1/journeys",
            json=_body(
                traveler_id=traveler,
                end_at=(_at(26) + timedelta(days=MAX_JOURNEY_DAYS + 3)).isoformat(),
            ),
        ),
        client.get(f"/api/v1/journeys/{uuid4()}"),
        client.get("/api/v1/journeys/00000000-0000-0000-0000-000000000000/experience"),
    ]
    for response in responses:
        assert response.status_code >= 400, response.status_code
        body = response.json()["error"]
        key = body.get("message_key")
        assert isinstance(key, str) and key, f"no key on {response.status_code}: {body}"
        assert KEY_SHAPE.match(key), f"malformed key {key!r}"


def test_an_envelope_that_names_no_key_still_answers_with_one() -> None:
    """`error()` is called from many doors that were not touched by this order.

    They all inherit the code-derived default, which is what keeps a client from meeting an
    answer with nothing to look up; deleting that fallback must not leave this file green.
    """

    from app.main import error

    body = json.loads(error("LEGACY_DOOR_CLOSED", "still english", status=400).body)["error"]
    assert body["message_key"] == "error.legacy_door_closed", body
    assert KEY_SHAPE.match(body["message_key"]), body
    assert body["message"] == "still english", body


def test_an_unexpected_failure_is_still_a_500_and_not_given_a_tidy_key() -> None:
    """Naming rejections must not become a way to make a bug look like a user error."""

    try:
        asyncio.run(
            temporal_rejection(None, ValueError("V13_JOURNEY_DAYS_OUT_OF_BOUNDSX:9"))
        )
    except ValueError as exc:
        assert "OUT_OF_BOUNDSX" in str(exc), exc
    else:
        raise AssertionError("a near-miss rejection code was laundered into a 422")
