"""ORD-0520 follow-on (lane 89f2e2d6): the capability door echoes a composite where it means a kind.

The places projection in `backend/app/anywhere_capability.py` built `osm_type` by splitting the
stored identity on a single colon.  The identity is written by several legs and the one this build
actually supplies uses a slash: `data/replay/tianjin_real_snapshot.json` carries
`way/1464199917`, `node/5172762221` and three more, and those rows reach the table unmodified
through `backend/app/integrations/discovery/real_city.py` (`external_id=str(row["external_id"])`).
For that shape the colon split returns the WHOLE identity, so the door answered
`osm_type = "way/1464199917"` -- neither a kind nor a gap.

That is why it is worth fixing and not just tidying: `backend/tests/test_qoder_anywhere_provider_truth.py`
bucketises the same rows by kind (its own reading, published in my g10 block, is
`{'way/1464199917': 1, 'way/73422999': 1, 'node/5172762221': 1, ...}`) and then requires a `node`
key to be present.  A bucket that can only ever be named after a whole identity is a second,
independent reason that arm stays red -- separate from the two supply reasons I named there.

These arms read the DOOR, not the helper: they ask what a client sees for each identity shape a
writer in this build produces, and they require an identity the door cannot classify to answer a
named null instead of guessing.  An unprovider-prefixed identity (`nominatim:way/4242`) is
deliberately in that last group: the door's answer is null rather than a parse of a nested segment,
so this arm pins the decision as well as the behaviour.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.models import Place
from app.db.session import SessionLocal, ensure_schema
from app.main import app

CAPABILITY = "/api/v1/destinations/capability"
CITY_QUERY = "天津市"
CITY_CODE = "CN-TIANJIN"
OSM_KINDS = ("node", "way", "relation")
client = TestClient(app)


def _seed(external_id: str, place_id: str) -> None:
    ensure_schema()
    with SessionLocal() as session:
        session.add(
            Place(
                id=place_id,
                provider_key="openstreetmap-replay",
                external_id=external_id,
                city_code=CITY_CODE,
                name=place_id,
                category="museum",
                latitude=39.1,
                longitude=117.2,
                opening_hours_json=json.dumps({}),
                tags_json=json.dumps([]),
                metadata_json=json.dumps({}),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        session.commit()


def _row_for(external_id: str) -> dict:
    answer = client.get(CAPABILITY, params={"query": CITY_QUERY})
    assert answer.status_code == 200, answer.text
    rows = [p for p in (answer.json().get("places") or []) if p.get("source_identity") == external_id]
    assert len(rows) == 1, [p.get("source_identity") for p in answer.json().get("places") or []]
    return rows[0]


def test_a1_a_slash_shaped_way_identity_answers_a_bare_kind() -> None:
    """The replay supply's real shape (`way/<id>`) must answer `way`, not echo the pair."""

    _seed("way/1464199917", "ord0520osmt-way")
    kind = _row_for("way/1464199917").get("osm_type")
    assert kind == "way", (
        f"期望：门对 `way/1464199917` 回答裸类名 way，因为按类分桶的读法只有这个词；"
        f"实际：{kind!r} —— 既不是类名也不是空，是没认出斜杠就整串回吐"
    )


def test_a2_a_slash_shaped_node_identity_answers_a_bare_kind() -> None:
    """`node/<id>` is the class the g10 lesson says gets silently dropped; it needs its own name."""

    _seed("node/5172762221", "ord0520osmt-node")
    kind = _row_for("node/5172762221").get("osm_type")
    assert kind == "node", f"期望 node；实际 {kind!r}"


def test_a3_an_identity_that_is_not_an_osm_element_answers_a_named_null() -> None:
    """Mock catalogue ids (`P001`) are not OSM elements: absence must stay absence."""

    _seed("P001", "ord0520osmt-mock-id")
    kind = _row_for("P001").get("osm_type")
    assert kind is None, (
        f"期望：认不出类名时回答 null，读的人一眼知道『这一格没有』；实际：{kind!r} —— "
        "它把身份串的一截当成了类名，读者会把它当成一个从没存在过的桶"
    )


def test_a4_a_provider_prefixed_identity_is_answered_null_not_parsed_into_a_kind() -> None:
    """Pinned decision: the door names a kind only for `<kind>/<id>`, and does not go digging.

    `backend/app/integrations/discovery/real_city.py` writes `<provider>:<osm_type>/<osm_id>` for
    the online legs, so `nominatim:way/4242` is a shape the build can produce.  Answering `way` for
    it would mean the door asserting a second, undocumented grammar; answering null says exactly
    what the door knows.  This arm exists so that if someone later chooses to parse the nested
    segment, they have to change a stated expectation rather than slip it through.
    """

    _seed("nominatim:way/4242", "ord0520osmt-prefixed")
    kind = _row_for("nominatim:way/4242").get("osm_type")
    assert kind is None, f"期望 null（不去拆带前缀的第二段）；实际 {kind!r}"


def test_a5_the_bare_kind_arms_bite_when_the_projection_echoes_the_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterexample: put the old behaviour back at runtime and require this door to be wrong."""

    from app import anywhere_capability as module

    def echo(external_id: object) -> str:
        return str(external_id).lower()

    monkeypatch.setattr(module, "_place_geometry_kind", echo, raising=False)
    _seed("way/73422999", "ord0520osmt-counterexample")
    kind = _row_for("way/73422999").get("osm_type")
    assert kind == "way/73422999", (
        f"期望：把归一化换回整串回吐时，这一臂必须看见合成串（证明 a1/a2 真在咬归一化，不是恒绿）；"
        f"实际：{kind!r}"
    )
