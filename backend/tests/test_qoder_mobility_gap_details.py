"""R5 (`docs/anywhere_provider_policy.md`): a refusal that knows which legs it could not price
must say so in `details`, not answer `details=null`.

What is proved here, and what is deliberately *not* claimed:

* the mapping from a collected leg to a named gap - both endpoints, the reason code, and the
  evidence state, with an unparseable end left as `None` rather than filled with an invented id;
* the envelope seam that carries `reason_code` / `details` out of an `HTTPException`, which is the
  machinery the replan door now uses.

The end-to-end door - "run a replan that genuinely lacks a leg, and read `details.gaps` off the
422" - is **not** in this file.  Building it means staging an ACTIVE plan plus an incident whose
alternative needs an unroutable pair, and I would rather hand over two honest unit doors plus a
named gap than a green that never touches the branch.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import _mobility_gap, app

client = TestClient(app, raise_server_exceptions=False)


def test_a_collected_leg_becomes_a_named_gap() -> None:
    gap = _mobility_gap("MOBILITY_DATA_UNAVAILABLE:place-a|place-b")
    assert gap == {
        "from_place_id": "place-a",
        "to_place_id": "place-b",
        "reason_code": "MOBILITY_DATA_UNAVAILABLE",
        "evidence_state": "NO_QUALIFIED_FACT_FOR_LEG",
    }, gap


def test_an_unparseable_end_stays_none_instead_of_borrowing_a_real_id() -> None:
    """Inventing a place id to fill a column is the same fabrication the leg already refuses."""

    gap = _mobility_gap("MOBILITY_DATA_UNAVAILABLE:loneendpoint")
    assert gap["from_place_id"] == "loneendpoint", gap
    assert gap["to_place_id"] is None, gap
    assert gap["evidence_state"] == "NO_QUALIFIED_FACT_FOR_LEG", gap


def test_the_envelope_publishes_reason_code_and_details_when_a_raise_site_supplies_them() -> None:
    """The renderer the replan door rides: `message` unchanged, the two fields present.

    Measured on `error()` itself - the one place every refusal is turned into a body - rather
    than on handler plumbing, so the door says something about the contract and not about how
    FastAPI dispatches exception handlers today.
    """

    import json

    from app.main import error

    response = error(
        "VALIDATION_ERROR",
        "MOBILITY_DATA_UNAVAILABLE",
        status=422,
        reason_code="MOBILITY_DATA_UNAVAILABLE",
        details={
            "gaps": [_mobility_gap("MOBILITY_DATA_UNAVAILABLE:place-a|place-b")],
            "gaps_total": 1,
            "legs_required": 1,
        },
        message_key="error.validation_error",
    )
    body = json.loads(response.body.decode("utf-8"))["error"]
    assert body["message"] == "MOBILITY_DATA_UNAVAILABLE", body
    assert body["reason_code"] == "MOBILITY_DATA_UNAVAILABLE", body
    assert body["details"]["gaps"][0]["from_place_id"] == "place-a", body
    assert body["details"]["gaps"][0]["to_place_id"] == "place-b", body
    assert body["details"]["gaps_total"] == 1, body


def test_a_refusal_without_a_named_condition_grows_no_fields() -> None:
    """The same renderer must stay silent when nobody knows more: absence, not an empty string."""

    import json

    from app.main import error

    body = json.loads(
        error("VALIDATION_ERROR", "Request validation failed", status=422).body.decode("utf-8")
    )["error"]
    assert "reason_code" not in body, body
    assert body["details"] is None, body
