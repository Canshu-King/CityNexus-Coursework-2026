"""ORD-0191 尾巴 E - the arbitration's named mobility gap has to come out of the product.

Ruling A set tonight's acceptance bar as `HYDRATABLE` plus the named gap
`MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE`.  The commander measured on 2026-09-22 that the string had
**zero** occurrences in `app/` and the contracts: it existed as a literal inside one door, which is
the shape where a ruling reads as satisfied while no answer ever says it.  These doors are written so
that cannot happen again - each one distinguishes the gap from the two claims it is easy to confuse
with it:

* an axis with material that is only sealed-fixture or synthetic: **the gap**, capability still
  `HYDRATABLE`, because preparation is legitimate and a hard commitment is not;
* an axis with nothing at all: `MISSING`, and **no gap** - claiming a gap would assert material that
  does not exist;
* an axis with a captured live route: **no gap**, because the reason it was missing was the class.

The third case is why the emitter is not decoration: a door that only ever sees the string emitted
cannot tell "named the gap" from "printed the string".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

import app.anywhere_capability as capability_module
from app.db.models import CatalogHydrationOperation, MobilityFact, Place
from app.db.session import SessionLocal
from app.main import app
from app.replanning.provider_snapshots import EXPLICIT_FROZEN_FIXTURE, LIVE_PROVIDER_CAPTURE
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))
CAPABILITY = "/api/v1/destinations/capability"
HYDRATIONS = "/api/v1/catalog-hydrations"
GAP = capability_module.GAP_MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE
STAGE_CITY = "CN-BEIJING"


@pytest.fixture()
def seeded(monkeypatch) -> str:
    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, reset.text
    with SessionLocal() as session:
        session.execute(CatalogHydrationOperation.__table__.delete())
        session.execute(
            MobilityFact.__table__.delete().where(
                MobilityFact.provider_key == "gap-door-fixture"
            )
        )
        session.commit()
    yield str(reset.json()["journey_id"])
    with SessionLocal() as session:
        session.execute(
            MobilityFact.__table__.delete().where(
                MobilityFact.provider_key == "gap-door-fixture"
            )
        )
        session.execute(CatalogHydrationOperation.__table__.delete())
        session.commit()


def _answer(query: str) -> dict:
    read = client.get(CAPABILITY, params={"query": query})
    assert read.status_code == 200, read.text
    return read.json()


def _pair_in(city_code: str) -> tuple[str, str]:
    with SessionLocal() as session:
            ids = list(
                session.scalars(
                    select(Place.id).where(Place.city_code == city_code).order_by(Place.id)
                ).all()
            )
    assert len(ids) >= 2, f"SETUP 失败：{city_code} 在种子里不足 2 行，这条门什么都没测"
    return str(ids[0]), str(ids[1])


def _add_live_captured_fact(journey_id: str, city_code: str) -> tuple[str, str]:
    """One genuinely captured route between two of this city's places.

    Written straight into the store because the point is what the evaluator reads, not how the row
    arrived; the class is the provider layer's own `LIVE_PROVIDER_CAPTURE` rather than a string this
    file invents.
    """

    origin, destination = _pair_in(city_code)
    stamp = datetime.now(TZ).isoformat()
    with SessionLocal() as session:
        session.add(
            MobilityFact(
                id=f"gap-door-{origin}-{destination}",
                journey_id=journey_id,
                option_id=None,
                from_key=origin,
                to_key=destination,
                mode="WALK",  # the table pins the enum; "walking" is refused by a CHECK

                duration_min=17,
                distance_m=1400,
                cost_amount_minor=None,
                currency=None,
                cost_status="UNKNOWN",
                walking_min=17,
                transfers=0,
                weather_exposure="NONE",
                reliability_score=0.9,
                provider_key="gap-door-fixture",
                provenance=LIVE_PROVIDER_CAPTURE,
                geometry_json="{}",
                crs="WGS84",
                paid_mode=False,
                evidence_json="{}",
                captured_at=stamp,
                expires_at=None,
            )
        )
        session.commit()
    return origin, destination


def test_a_frozen_only_mobility_axis_names_the_gap_and_stays_hydratable(seeded) -> None:
    """有材料、但材料是密封 fixture ⇒ 具名 gap；能力仍是 HYDRATABLE（准备合法、硬承诺不合法）。"""

    body = _answer(STAGE_CITY)
    mobility = body["coverage_detail"]["mobility"]
    assert body["destination_state"] == "RESOLVED", body["destination_state"]
    assert body["place_count"] > 0, f"SETUP 失败：{STAGE_CITY} 没有目录行，这条门什么都测不到"
    assert mobility["candidate_count"] > 0, mobility
    assert mobility["source_classes"] == [EXPLICIT_FROZEN_FIXTURE], mobility
    assert mobility["hard_eligible"] is False, mobility
    assert body["hard_gaps"] == [GAP], f"期望产品自己发射具名 gap；实际：{body['hard_gaps']} / {mobility}"
    assert body["capability"] == "HYDRATABLE", (
        f"期望：仲裁 A 的口径是 HYDRATABLE + 具名 gap；实际：{body['capability']}"
    )
    assert body["recommended_action"] == capability_module.ACTION_START_HYDRATION, body


def test_the_gap_survives_into_the_stored_job_not_only_the_response(seeded) -> None:
    """作业是一周后唯一还有人读的东西：具名 gap 必须也在库里，不能只在回答里闪过一下。"""

    body = _answer(STAGE_CITY)
    assert body["hard_gaps"] == [GAP], body["hard_gaps"]
    posted = client.post(HYDRATIONS, json={"query": STAGE_CITY})
    assert posted.status_code in (200, 202), posted.text
    operation_id = posted.json()["operation_id"]
    row = None
    import time

    for _ in range(120):
        with SessionLocal() as session:
            row = session.scalar(
                select(CatalogHydrationOperation).where(
                    CatalogHydrationOperation.id == operation_id
                )
            )
            if row is not None and row.status in {
                "COMPLETED", "FAILED", "UNAVAILABLE", "ABANDONED"
            }:
                break
        time.sleep(0.05)
    assert row is not None and row.status in {"COMPLETED", "UNAVAILABLE"}, row
    result = json.loads(row.result_json or "{}")
    evidence = result.get("mobility_evidence") or {}
    assert evidence.get("hard_gaps") == [GAP], (
        f"期望：作业载荷里也带具名 gap；实际键名 {sorted(result)} / mobility_evidence={evidence}"
    )
    assert evidence.get("hard_eligible") is False, evidence
    assert evidence.get("source_classes") == [EXPLICIT_FROZEN_FIXTURE], evidence


def test_an_axis_with_nothing_in_it_is_MISSING_and_claims_no_gap(seeded) -> None:
    """空的 mobility 轴不许借这个 gap——那等于凭空声称『这里有材料，只是不够硬』。"""

    body = _answer("上海")
    mobility = body["coverage_detail"]["mobility"]
    assert body["place_count"] == 0, (
        f"SETUP 失败：上海在种子里有 {body['place_count']} 行，这条门就不再代表空轴；"
        f"mobility={mobility}"
    )
    assert mobility["state"] == capability_module.MISSING, mobility
    assert mobility["candidate_count"] == 0, mobility
    assert mobility["source_classes"] == [], mobility
    assert body["hard_gaps"] == [], (
        f"期望：没有材料就没有『具名 gap』可谈；实际：{body['hard_gaps']} / {mobility}"
    )


def test_a_live_captured_route_removes_the_gap_it_named(seeded) -> None:
    """这条才是让发射器不能是装饰的：证据类别一变，gap 必须自己消失。"""

    journey_id = seeded
    before = _answer(STAGE_CITY)
    assert before["hard_gaps"] == [GAP], before["hard_gaps"]
    _add_live_captured_fact(journey_id, STAGE_CITY)
    after = _answer(STAGE_CITY)
    mobility = after["coverage_detail"]["mobility"]
    assert LIVE_PROVIDER_CAPTURE in mobility["source_classes"], mobility
    assert mobility["hard_eligible"] is True, mobility
    assert after["hard_gaps"] == [], (
        f"期望：有了真捕获的路线，gap 就该消失（否则它只是一句常印的话）；实际：{after['hard_gaps']}"
    )
    assert after["capability"] == "HYDRATABLE", after["capability"]


def test_a_destination_that_never_resolved_claims_no_hard_gap(seeded) -> None:
    """没定出地点 ⇒ 既没有材料也没有轴可谈，gap 列表必须是空（也别顺手发明第五格）。"""

    body = _answer("zzxqvw-不存在的县")
    assert body["destination_state"] == "NO_MATCH", body["destination_state"]
    assert body["canonical_destination_id"] is None, body
    assert body["hard_gaps"] == [], body["hard_gaps"]
    assert body["capability"] == "UNAVAILABLE", body["capability"]


def test_an_eligibility_question_nobody_answered_claims_no_gap(seeded, monkeypatch) -> None:
    """报告里没有"合不合格"这一项时，不许把它读成"不合格"，也不许崩。

    This is the fail-closed half of the emitter, and it is reachable: a resolved destination whose
    catalog really holds rows and whose mobility axis really has legs, with the eligibility answer
    taken out of the report. Silence is not a negative - claiming the gap here would be the same
    overstatement in the other direction as calling an empty axis a gap.
    """

    from app.db.session import SessionLocal as _Session

    real = capability_module.coverage_detail

    def withholding(session, city_code):
        detail = real(session, city_code)
        mobility = dict(detail.get("mobility") or {})
        mobility.pop("hard_eligible", None)
        mobility.pop("source_classes", None)
        detail["mobility"] = mobility
        return detail

    monkeypatch.setattr(capability_module, "coverage_detail", withholding)
    with _Session() as session:
        body = capability_module.evaluate_capability(session, STAGE_CITY)
    assert body["destination_state"] == "RESOLVED", body["destination_state"]
    assert body["place_count"] > 0, body["place_count"]
    assert int(body["coverage_detail"]["mobility"]["candidate_count"]) > 0, body["coverage_detail"]
    assert "hard_eligible" not in body["coverage_detail"]["mobility"], body["coverage_detail"]
    assert body["hard_gaps"] == [], (
        f"期望：没人回答合格性问题时不许替它回答；实际：{body['hard_gaps']}"
    )
