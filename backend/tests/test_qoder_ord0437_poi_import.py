"""ORD-0437: the POI text/link import chain, tested at the two places it can actually be wrong.

1. The parser is local, so it can be pinned without a server: which forms a blob of text carries, and
   what happens to a line that matches nothing (it stays un-addable and says so) versus a line that
   matches two of our places (nobody may pick for the traveller).
2. The write is where the order's red line lives. `加入` means the record comes back from the
   projection the plan is drawn from, and a second tap on the same selection must not create a second
   visit -- so this module reads `/experience` after the write and counts rows, rather than trusting
   the 201.

No provider is configured or called anywhere here: the parse response's `source_channel` is asserted
to be the local parser, which is the property the red line asks for.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models import Place
from app.db.session import SessionLocal
from app.poi_import import (
    FORM_LINK_ONLY,
    FORM_PASTED_TEXT,
    FORM_SCREENSHOT,
    STATUS_AMBIGUOUS,
    STATUS_RESOLVED,
    STATUS_UNRESOLVED,
    counts,
    detect_forms,
    extract,
    resolve,
)
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)

_CATALOGUE = [
    {"id": "p1", "name": "Riverside Museum", "latitude": 39.1, "longitude": 117.2,
     "coordinate_system": "WGS84", "aliases": ()},
    {"id": "p2", "name": "Old Town Gate", "latitude": 39.11, "longitude": 117.21,
     "coordinate_system": "WGS84", "aliases": ()},
    {"id": "p3", "name": "Museum of Modern Art", "latitude": 39.12, "longitude": 117.22,
     "coordinate_system": "WGS84", "aliases": ()},
]


def test_forms_are_named_without_being_fetched() -> None:
    assert FORM_PASTED_TEXT in detect_forms("上午去了 Riverside Museum")
    forms = detect_forms("https://www.xiaohongshu.com/explore/abc")
    assert forms == (FORM_LINK_ONLY,), forms
    assert FORM_SCREENSHOT in detect_forms("看这张截图 shot.png")
    assert detect_forms("   ") == ("EMPTY_INPUT",)


def test_a_line_that_matches_nothing_stays_unaddable() -> None:
    items = extract("咕咕镇不存在\nRiverside Museum 晚上好看")
    resolved = resolve(items, _CATALOGUE)
    by_status = {r.title[:4]: r.status for r in resolved}
    tally = counts(resolved)
    assert tally["extracted"] == 2
    assert tally["resolved"] == 1, resolved
    assert tally["unresolved"] == 1, by_status
    assert [r.place_id for r in resolved if r.status == STATUS_RESOLVED] == ["p1"]


def test_two_matches_become_the_travellers_choice_not_a_guess() -> None:
    resolved = resolve(extract("Museum"), [{"id": "p1", "name": "Riverside Museum",
                                           "latitude": 1, "longitude": 2,
                                           "coordinate_system": "WGS84", "aliases": ()},
                                          {"id": "p3", "name": "Museum of Modern Art",
                                           "latitude": 1, "longitude": 2,
                                           "coordinate_system": "WGS84", "aliases": ()}])
    assert len(resolved) == 1
    assert resolved[0].status == STATUS_AMBIGUOUS, resolved
    assert resolved[0].place_id is None
    assert counts(resolved)["addable"] == 0


def test_parse_door_reports_the_link_it_refused_to_open() -> None:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    body = {"text": "想再去一次 Riverside Museum\nhttps://mp.weixin.qq.com/s/AAA 这段正文没贴\n咕咕镇"}
    response = client.post(f"/api/v1/journeys/{journey_id}/poi-import/parse", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source_channel"] == "LOCAL_TEXT_PARSER"
    assert payload["counts"]["extracted"] >= 3, payload["counts"]
    assert payload["counts"]["unresolved"] >= 1, payload["items"]
    assert FORM_LINK_ONLY in payload["unsupported_forms"], payload
    assert payload["link_hosts"] == ["mp.weixin.qq.com"], payload["link_hosts"]
    addable = [i for i in payload["items"] if i["addable"]]
    assert all(i["status"] == STATUS_RESOLVED for i in addable)
    assert all(i["place_id"] for i in addable)


def _active_plan(client, journey_id: str) -> dict:
    """The demo journey arrives with no plan, so the traveller's own door makes one first."""
    plans = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()
    assert plans, "the deterministic generator produced no plan"
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plans[0]["id"]}/select").status_code == 200
    return plans[0]


def test_added_only_when_the_plan_gives_the_row_back() -> None:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    _active_plan(client, journey_id)
    before = client.get(f"/api/v1/journeys/{journey_id}/experience").json()
    plan = before.get("active_plan") or {}
    before_ids = {a["id"] for a in plan.get("activities", [])}
    already_there = {a.get("place_id") for a in plan.get("activities", [])}
    body = {
        "text": "Riverside Museum\nCentral Food Hall\nOld Town Gate\nDesign Library\nNexus City Park"
    }
    parsed = client.post(f"/api/v1/journeys/{journey_id}/poi-import/parse", json=body).json()
    fresh = [
        i["place_id"]
        for i in parsed["items"]
        if i["addable"] and i["place_id"] and i["place_id"] not in already_there
    ]
    assert parsed["counts"]["resolved"] >= 1, parsed
    if not fresh:
        # every matched place is already on this plan, which is a property of the demo fixture and
        # not of this door; say so rather than asserting a write that would land as already_present.
        assert parsed["counts"]["resolved"] == len(already_there & {i["place_id"] for i in parsed["items"] if i["addable"]})
        return
    added = client.post(
        f"/api/v1/journeys/{journey_id}/poi-import/add",
        json={"place_ids": fresh[:1]},
    )
    assert added.status_code == 201, added.text
    written = added.json()
    assert written["day_source"] in {"JOURNEY_FIRST_DAY", "TRAVELLER_CHOSEN"}
    assert written["readback_path"].endswith("/experience")
    assert written["added"], written
    projected = client.get(f"/api/v1/journeys/{journey_id}/experience").json()
    server_ids = {a["id"] for a in (projected.get("active_plan") or {}).get("activities", [])}
    assert {a["id"] for a in written["added"]} <= server_ids, "the write claimed rows the projection does not have"

    replay = client.post(
        f"/api/v1/journeys/{journey_id}/poi-import/add",
        json={"place_ids": fresh[:1], "day": written["day"]},
    )
    assert replay.status_code == 201, replay.text
    again = replay.json()
    assert again["added"] == [], again
    assert again["already_present"] == fresh[:1], again
    after = client.get(f"/api/v1/journeys/{journey_id}/experience").json()
    after_ids = {a["id"] for a in (after.get("active_plan") or {}).get("activities", [])}
    assert len(after_ids - before_ids) == len(written["added"]), "a replay created a second visit"


def test_write_without_an_active_plan_is_refused_by_name() -> None:
    """A fresh demo journey has no plan yet; the door must say that, not half-write a row.

    Measured: this is a 409 whose reason_code is NO_ACTIVE_PLAN, so the panel can name the state
    instead of showing a spinner that never ends.
    """
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    response = client.post(
        f"/api/v1/journeys/{journey_id}/poi-import/add", json={"place_ids": ["place-riverside-museum"]}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["reason_code"] == "NO_ACTIVE_PLAN", response.text


def _seed_place(place_id: str, city_code: str, name: str, category: str) -> None:
    """A row of our own for the two refusals below, written the way the catalogue tests write one."""
    with SessionLocal() as session:
        session.add(
            Place(
                id=place_id,
                provider_key="mock-city",
                external_id=place_id.upper(),
                city_code=city_code,
                name=name,
                category=category,
                latitude=31.24,
                longitude=121.5,
                opening_hours_json=json.dumps({"open": "09:00", "close": "17:00"}),
                tags_json=json.dumps([]),
                metadata_json=json.dumps({"duration_min": 60, "cost_minor": 0}),
                updated_at="2026-10-01T00:00:00+08:00",
            )
        )
        session.commit()


def _drop_places(*place_ids: str) -> None:
    with SessionLocal() as session:
        session.execute(delete(Place).where(Place.id.in_(list(place_ids))))
        session.commit()


def test_a_schedule_node_is_not_offered_as_a_place_to_visit() -> None:
    """`Arrival` / check-in rows are places in the table but not places to visit.

    The door reads the same category filter the city-content route already applies, so a pasted word
    that only matches a schedule node comes back UNRESOLVED while a real museum in the same city still
    resolves -- the second row is the positive control that keeps this from passing on an empty
    catalogue.
    """
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    city = client.get(f"/api/v1/journeys/{journey_id}/experience").json()["journey"]["city_code"]
    _seed_place("place-ord0437-checkin", city, "Check in Ord Gate Hotel", "check_in")
    _seed_place("place-ord0437-harbour", city, "Ord Harbour Museum", "museum")
    try:
        parsed = client.post(
            f"/api/v1/journeys/{journey_id}/poi-import/parse",
            json={"text": "Check in Ord Gate Hotel\nOrd Harbour Museum"},
        ).json()
        by_title = {i["title"]: i for i in parsed["items"]}
        assert by_title["Check in Ord Gate Hotel"]["status"] == STATUS_UNRESOLVED, by_title
        assert by_title["Check in Ord Gate Hotel"]["addable"] is False, by_title
        assert by_title["Ord Harbour Museum"]["status"] == STATUS_RESOLVED, by_title
        assert by_title["Ord Harbour Museum"]["addable"] is True, by_title
    finally:
        _drop_places("place-ord0437-checkin", "place-ord0437-harbour")


def test_a_name_written_with_a_middle_dot_is_split_into_two_segments() -> None:
    """The limitation this order's gate should keep honest rather than paper over.

    `·` is a segment separator here, so a place whose own name contains one arrives as two lines and
    matches nothing. Measured on this build's catalogue: the only rows whose names carry a `·` are the
    check-in / stay nodes (CN-TIANJIN), which this door refuses to offer anyway -- so no addable place
    is lost by the rule today. If a city's real POI names ever grow one, this test is the note that
    says the segmentation has to become line-aware before that city is promised to anyone.
    """
    items = extract("Check in · Ord Gate Hotel")
    assert [i.title for i in items] == ["Check in", "Ord Gate Hotel"], items


def test_the_write_refuses_a_schedule_node_and_a_foreign_city_row() -> None:
    """The read door cannot offer these, and the write is the authority that says so too.

    Both refusals are 422 `IMPORT_PLACE_NOT_VISITABLE` before anything is appended: a caller holding
    a place id must not be able to land a hotel check-in on the plan, or pull another city's row into
    this journey's itinerary.
    """
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    city = client.get(f"/api/v1/journeys/{journey_id}/experience").json()["journey"]["city_code"]
    _active_plan(client, journey_id)
    _seed_place("place-ord0437-stay", city, "Overnight · Ord Gate Hotel", "stay")
    _seed_place("place-ord0437-abroad", "CNX-ORD0437-ELSEWHERE", "Far Away Pier", "museum")
    try:
        for place_id in ("place-ord0437-stay", "place-ord0437-abroad"):
            response = client.post(
                f"/api/v1/journeys/{journey_id}/poi-import/add", json={"place_ids": [place_id]}
            )
            assert response.status_code == 422, (place_id, response.text)
            assert response.json()["error"]["reason_code"] == "IMPORT_PLACE_NOT_VISITABLE", (
                place_id,
                response.text,
            )
        projected = client.get(f"/api/v1/journeys/{journey_id}/experience").json()
        titles = [a["title"] for a in (projected.get("active_plan") or {}).get("activities", [])]
        assert "Overnight · Ord Gate Hotel" not in titles and "Far Away Pier" not in titles, titles
        # the same door still accepts a real place, so the refusals above are not a broken route
        accepted = client.post(
            f"/api/v1/journeys/{journey_id}/poi-import/add",
            json={"place_ids": ["place-riverside-museum"]},
        )
        assert accepted.status_code == 201, accepted.text
    finally:
        _drop_places("place-ord0437-stay", "place-ord0437-abroad")
