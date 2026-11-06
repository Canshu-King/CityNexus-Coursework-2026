"""ORD-0500: the footprint doors name the trip they cannot find, without printing an id.

The measured starting shape, read on this lane's own base tree at tip
`b604f50d1645d028c2dd1d4406caee8198a3bc2d` (the very bytes the base face serves), and kept in
`review/ord0500-checkin/receipts/gen9_doors_wt23-wt22-mutgen9.json`, probe case
`write_door_unknown_journey`:

    POST /api/v1/journeys/<uuid>/checkins   -> http_status 404, body_bytes 235
        message      "Journey c4315bf1-cc5d-48d0-92b3-4d1ad255c279 was not found"
        details      null
        reason_code  null

Three things wrong in one answer, all of them the family this project keeps having to fix: a uuid
inside a traveller's sentence, no name to switch on, and a null where the contract says a reason
rides.  It came from the shared `require()` helper (`backend/app/main.py`), which every door
uses, so the fix is inside the footprint doors' own journey read -- and these arms keep it there.

Nothing here calls `POST /api/v1/demo/reset`, and nothing runs against a shared store: the engine is
pointed at a one-off file by the runner (`CITYNEXUS_DATABASE_URL`), the way the sibling
`tests/test_qoder_ord0476_checkins.py` insists on it.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_qoder_ord0476_checkins import _stage  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _envelope(response):
    return (response.json() or {}).get("error") or {}


@pytest.fixture(scope="module")
def staged():
    """One trip and one planned activity of its own, staged the way the sibling suite stages them.

    Reusing `test_qoder_ord0476_checkins._stage` instead of writing a second fixture is deliberate:
    the door-vs-index claim is only testable against the same rows the duplicate door's own arms use,
    and two fixtures drifting apart is how a guard ends up asserted against a fixture nobody ships.
    """

    return _stage("ord0500")


def test_both_footprint_doors_refuse_an_unknown_trip_by_name():
    """The write door and the read door answer the same way, because they resolve one journey."""

    missing = str(uuid.uuid4())
    write = client.post(f"/api/v1/journeys/{missing}/checkins",
                        json={"activity_id": str(uuid.uuid4())})
    read = client.get(f"/api/v1/journeys/{missing}/checkins")
    assert write.status_code == 404, write.text
    assert read.status_code == 404, read.text
    for response in (write, read):
        error = _envelope(response)
        assert error.get("reason_code") == "CHECKIN_JOURNEY_UNKNOWN", response.text
        # `details: null` was half of the defect: a refusal carries the field it is about.
        assert isinstance(error.get("details"), dict), response.text
        assert error["details"].get("field") == "journey_id", response.text


def test_the_refusal_sentence_carries_no_identifier_of_any_kind():
    """A uuid is not a word a traveller can act on; it must not reach the message either."""

    missing = str(uuid.uuid4())
    error = _envelope(client.post(f"/api/v1/journeys/{missing}/checkins",
                                  json={"activity_id": str(uuid.uuid4())}))
    message = error.get("message") or ""
    assert message, "an empty refusal sentence answers nothing"
    assert UUID_RE.search(message) is None, (
        "the traveller-facing sentence still interpolates an id: %r" % message
    )
    assert missing not in message
    assert UUID_RE.search(json.dumps(error.get("details"), ensure_ascii=False)) is None, (
        "details carries a raw id where a field name is enough"
    )


def test_the_name_is_written_into_the_hand_contract_too():
    """A name the app spec says and the hand contract does not is drift an integrator eats."""

    text = CONTRACT.read_text(encoding="utf-8")
    assert "CHECKIN_JOURNEY_UNKNOWN" in text, (
        "the door refuses by a name the contract never says, so a consumer cannot code against it"
    )


def test_a_known_trip_is_still_asked_about_its_target(staged):
    """The positive control: the named journey refusal must not have swallowed the working path.

    The journey exists and the activity does not, so the answer has to be about the TARGET.  Were the
    new name to swallow the door, this would read `CHECKIN_JOURNEY_UNKNOWN` for a trip that is
    plainly there.
    """

    journey_id = staged["own"]["journey"]
    answer = client.post(f"/api/v1/journeys/{journey_id}/checkins",
                         json={"activity_id": str(uuid.uuid4())})
    error = _envelope(answer)
    assert answer.status_code == 404, answer.text
    assert error.get("reason_code") == "CHECKIN_ACTIVITY_UNKNOWN", answer.text
    assert isinstance(error.get("details"), dict) and error["details"], answer.text
    assert UUID_RE.search(error.get("message") or "") is None, answer.text


def test_the_duplicate_answer_comes_from_the_door_and_not_from_the_index(staged):
    """Which layer refused is a fact the sentence alone cannot carry.

    Two guards refuse a second tap on the same stop, both of them on this tip: the door looks
    its own table up first (`backend/app/main.py`), and the `UNIQUE (journey_id,
    target_key)` index catches a race that lookup cannot see (`:4299`), which is the only
    branch that adds `caught_by: unique_index` (`:4316`).  A screen-side check of the
    sentence cannot tell those apart -- delete the door's
    lookup and a sentence-only arm still passes.  This arm pins the layer, so it is the one that
    goes red under that mutation, while the sibling spec keeps the durable half.
    """

    journey_id, activity_id = staged["own"]["journey"], staged["own"]["activity"]
    first = client.post(f"/api/v1/journeys/{journey_id}/checkins",
                        json={"activity_id": activity_id})
    assert first.status_code == 201, first.text
    second = client.post(f"/api/v1/journeys/{journey_id}/checkins",
                         json={"activity_id": activity_id})
    error = _envelope(second)
    assert second.status_code == 409, second.text
    assert error.get("reason_code") == "CHECKIN_DUPLICATE_STAMP", second.text
    details = error.get("details") or {}
    assert "caught_by" not in details, (
        "the index answered this duplicate, not the door's own lookup: %r" % (details,)
    )
    # the moment the screen interpolates into 「这一站已经打过卡了（服务器记的是 …）」
    assert details.get("existing_stamped_at"), second.text
