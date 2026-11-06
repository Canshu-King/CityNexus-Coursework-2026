"""ORD-0522 -- the demo board says, in the product's own words, which hop lost the real places.

The board could already answer `NO_SCENARIO`, which is honest about the stage and silent about the
supply behind it.  These cells read `GET /api/v1/demo/scene` and ask only questions a reader can
check against the row it came from:

  * does the stage name the job, its ending, the stage it stopped supplying in, and per leg whether
    that leg answered at all;
  * is an uncounted reply kept apart from a measured zero;
  * does a stop that has no position get *named* instead of quietly dropping off the map rows;
  * does a stage whose rows are all replay refuse to be called supply;
  * and does none of that cost a provider call, because a read that phones home makes the emptiness
    depend on the moment somebody looked.

Every value asserted here is re-derived from the stored row inside the cell, never copied from the
projection -- a test that repeats the code under test cannot fail, which is not the same as passing.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.anywhere_capability as capability_module
import app.catalog_hydration_operations as hydration_module
from app.db.models import (Activity, CatalogHydrationOperation, Journey, Place,
                            PlanVersion)
from app.db.session import SessionLocal
from app.main import app

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from test_qoder_provider_identity_leg import (  # noqa: E402
    COUNTY_EXTENT,
    RAW_COUNTY,
    _answer,
    _enable,
    _feature,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
SCENE = "/api/v1/demo/scene"
HYDRATIONS = "/api/v1/catalog-hydrations"
SUPPLY_KEYS = {
    "asked", "operation_id", "job_status", "state", "failed_stage", "reason_code", "retryable",
    "leg_states", "counts_as_supply", "stops",
}
STOPS_KEYS = {
    "plan_present", "total", "with_coordinate", "without_coordinate", "reasons", "source_kinds",
}
LEG_KEYS = {"photon", "nominatim", "overpass"}
#: the two vocabularies this projection is allowed to quote, read from the modules that own them
LEG_VOCABULARY = {
    hydration_module.LEG_NOT_ASKED, hydration_module.LEG_CAPTURED,
    hydration_module.LEG_ANSWERED_EMPTY, hydration_module.LEG_NOT_ANSWERED,
}
JOB_STATUSES = set(hydration_module.TERMINAL_STATUSES) | set(hydration_module.OPEN_STATUSES)
#: every reason the board is allowed to speak, taken from the two modules that own them -- so a word
#: invented here would fail its own cell rather than pass a test written around it.
ALLOWED_REASONS = {
    getattr(capability_module, name) for name in dir(capability_module) if name.startswith("REASON_")
} | {
    getattr(capability_module, name) for name in dir(capability_module) if name.startswith("GAP_")
}
OUTCOMES = {
    hydration_module.OUTCOME_PREPARED, hydration_module.OUTCOME_ALREADY_CURRENT,
    hydration_module.OUTCOME_NO_REGISTERED_SOURCE, hydration_module.OUTCOME_PENDING_UNAPPLIED,
    hydration_module.OUTCOME_ABANDONED, hydration_module.OUTCOME_WORKER_LOST,
    hydration_module.OUTCOME_DESTINATION_AMBIGUOUS, hydration_module.OUTCOME_RESOLVER_RUNTIME_FAILURE,
}


@pytest.fixture()
def seeded(monkeypatch):
    """One demo stage on a database this file owns, with no hydration jobs left over."""

    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, reset.text
    with SessionLocal() as session:
        session.execute(CatalogHydrationOperation.__table__.delete())
        session.commit()
    journey_id = reset.json().get("journey_id") or _only_journey_id()
    assert journey_id, "the seed handed back no journey -- these cells would measure nothing"
    yield str(journey_id)
    with SessionLocal() as session:
        session.execute(CatalogHydrationOperation.__table__.delete())
        session.commit()


def _only_journey_id() -> str | None:
    with SessionLocal() as session:
        row = session.scalar(select(Journey).order_by(Journey.created_at.desc()))
    return str(row.id) if row is not None else None


def _store_job(session, journey_id: str, *, status: str, phase: str | None, outcome: str | None,
               attempts: list[dict], reason_code: str | None = None) -> CatalogHydrationOperation:
    """Write a job the way the worker writes one: the envelope, not a shape invented for a test.

    `_failure_payload` puts the extras under `details`; a resolved ending writes them flat. Both
    shapes are what `board_supply` has to read, so both are exercised by different cells below.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    error = {}
    result = {}
    payload = {"provider_attempts": attempts}
    if reason_code:
        payload["reason_code"] = reason_code
    if status == hydration_module.UNAVAILABLE:
        error = {"code": "DESTINATION_IDENTITY_UNRESOLVED", "message": "stubbed for this cell",
                 "message_key": "error.destination_identity_unresolved",
                 "reason_code": reason_code or hydration_module.REASON_NO_CANONICAL_IDENTITY,
                 "details": payload}
    else:
        result = {"outcome": outcome, **payload}
    row = CatalogHydrationOperation(
        id=f"ord0522-{status}-{now[-9:]}",
        fingerprint=f"ord0522-fp-{now[-9:]}",
        canonical_destination_id=None,
        pending_destination_label=RAW_COUNTY,
        resolver_query_normalized=RAW_COUNTY,
        journey_id=journey_id,
        catalog_schema_version=capability_module.CATALOG_SCHEMA_VERSION,
        readiness_policy_version=capability_module.READINESS_POLICY_VERSION,
        provider_policy_version=capability_module.PROVIDER_POLICY_VERSION,
        status=status,
        phase=phase,
        outcome=outcome,
        request_json=json.dumps({"query": RAW_COUNTY}),
        result_json=json.dumps(result, ensure_ascii=False),
        error_json=json.dumps(error, ensure_ascii=False),
        queued_at=now,
        updated_at=now,
    )
    session.add(row)
    session.commit()
    return row


def _attempt(endpoint: str, *, answered: bool, http_status: int | None,
             features: int | None = None, candidates: int | None = None,
             reason: str | None = None) -> dict:
    return {
        "endpoint": endpoint, "answered": answered, "http_status": http_status,
        "elapsed_ms": 12.0, "reason": reason, "retry_after_s": None,
        "error": None if answered else "stubbed", "features_viewed": features,
        "candidates_before_filter": candidates, "drop_summary": None,
    }


def _scene_supply() -> dict:
    read = client.get(SCENE)
    assert read.status_code == 200, read.text
    body = read.json()
    assert "supply" in body, f"盘面没那一格，返回键是：{sorted(body)}"
    return body["supply"]


def _row_of(operation_id: str) -> CatalogHydrationOperation:
    with SessionLocal() as session:
        row = session.scalar(
            select(CatalogHydrationOperation).where(CatalogHydrationOperation.id == operation_id)
        )
    assert row is not None, f"作业 {operation_id} 不在库里"
    return row


# ---------------------------------------------------------------------------------------------
def test_S01_shape_is_constant(seeded) -> None:
    """正控：空盘面也得有同一格、同样的键，而且是「没人问过」而不是「供应商说不行」。"""

    with SessionLocal() as session:
        session.execute(Journey.__table__.delete())
        session.execute(Activity.__table__.delete())
        session.commit()
    supply = _scene_supply()
    assert set(supply) == SUPPLY_KEYS, sorted(supply)
    assert set(supply["stops"]) == STOPS_KEYS, sorted(supply["stops"])
    assert set(supply["leg_states"]) == LEG_KEYS, sorted(supply["leg_states"])
    assert supply["asked"] is False and supply["operation_id"] is None
    assert supply["state"] == hydration_module.LEG_NOT_ASKED, supply["state"]
    assert supply["counts_as_supply"] is False
    assert supply["failed_stage"] is None, "没有作业就没有失败阶段可指"
    assert supply["stops"]["total"] == 0 and supply["stops"]["plan_present"] is False


def test_S02_seeded_stage_names_the_job_and_legs(seeded) -> None:
    """反例：作业说 photon 答了个空、nominatim 根本没答，盘面必须逐腿照抄，不许糊成一句「不可用」。"""

    photon = _attempt("https://photon.komoot.io/api/", answered=True, http_status=200,
                      features=4, candidates=0)
    nominatim = _attempt("https://nominatim.openstreetmap.org/search", answered=False,
                         http_status=None, reason="PROVIDER_TIMEOUT")
    with SessionLocal() as session:
        row = _store_job(session, seeded, status=hydration_module.UNAVAILABLE,
                         phase="DISCOVERING_POIS", outcome=hydration_module.OUTCOME_NO_REGISTERED_SOURCE,
                         attempts=[photon, nominatim])
        row_id, outcome, phase = row.id, row.outcome, row.phase
    supply = _scene_supply()
    assert supply["asked"] is True and supply["operation_id"] == row_id, supply
    assert supply["state"] == outcome == hydration_module.OUTCOME_NO_REGISTERED_SOURCE
    assert supply["failed_stage"] == phase == "DISCOVERING_POIS"
    assert supply["counts_as_supply"] is False
    assert supply["leg_states"] == {
        "photon": hydration_module.LEG_ANSWERED_EMPTY,
        "nominatim": hydration_module.LEG_NOT_ANSWERED,
        # 这一腿在注水作业里从来不是被问的对象：说 NOT_ASKED，不许猜它超时了
        "overpass": hydration_module.LEG_NOT_ASKED,
    }, supply["leg_states"]
    assert supply["job_status"] == hydration_module.UNAVAILABLE
    assert supply["job_status"] in JOB_STATUSES


def test_S03_uncounted_reply_is_not_a_zero(seeded) -> None:
    """答了但没有计数的腿必须是 None，不许被折算成 ANSWERED_EMPTY（量到的 0 与没量到是两件事）。"""

    with SessionLocal() as session:
        _store_job(session, seeded, status=hydration_module.UNAVAILABLE,
                   phase="DISCOVERING_POIS", outcome=hydration_module.OUTCOME_NO_REGISTERED_SOURCE,
                   attempts=[_attempt("https://photon.komoot.io/api/", answered=True,
                                      http_status=200, features=None, candidates=None)])
    supply = _scene_supply()
    assert supply["leg_states"]["photon"] is None, supply["leg_states"]
    assert supply["leg_states"]["photon"] != hydration_module.LEG_ANSWERED_EMPTY


def _stage_plan(session, journey_id: str, *, provenance: str, broken_stops: int = 0) -> str:
    """Give the stage a plan of its own, with stops whose places are of the class under test.

    The frozen seed deliberately ships a DRAFT journey with no plan at all, so a cell that wants to
    count stops has to raise one.  Written through the model rather than a raw insert, and the plan
    becomes the journey's active pointer the way a selection would make it.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    plan = PlanVersion(
        id=f"ord0522-plan-{provenance}-{now[-9:]}", journey_id=journey_id, version_no=1,
        planning_mode="balanced", status="ACTIVE", source="INITIAL",
        total_cost_minor=0, currency="CNY", estimated_duration_min=60, created_at=now,
    )
    session.add(plan)
    #: flushed by hand in dependency order: the model declares foreign keys but no relationships, so
    #: the unit of work has no idea that the places must exist before a stop names one.
    session.flush()
    stops = []
    for index in range(2):
        place = Place(
            id=f"ord0522-place-{index}-{now[-9:]}", provider_key="overpass",
            external_id=f"osm/node/{1000 + index}", city_code="CN-XIAMEN",
            name=f"stop {index}", category="museum", latitude=24.48, longitude=118.08,
            opening_hours_json="{}", tags_json="[]",
            metadata_json=json.dumps({"provenance": provenance}), updated_at=now,
        )
        session.add(place)
        stops.append(place)
    session.flush()
    for index, place in enumerate(stops):
        session.add(Activity(
            id=f"ord0522-activity-{index}-{now[-9:]}", plan_version_id=plan.id, sequence_no=index + 1,
            activity_type="sight", title=f"stop {index}", place_id=place.id,
            start_at="2026-10-10T09:00:00+08:00", end_at="2026-10-10T10:00:00+08:00",
            travel_from_prev_min=10, estimated_cost_minor=0, currency="CNY", status="NOT_STARTED",
            locked=False, metadata_json="{}", created_at=now, updated_at=now,
        ))
    for index in range(broken_stops):
        # A stop with no place identity is the shape this store can actually reach: `activities.place_id`
        # is `ondelete="SET NULL"` (models.py) and `places.latitude` is NOT NULL, so "the place row
        # is gone but the stop still names it" cannot exist here -- that arm is left unmeasured on
        # purpose rather than forced through a row the schema refuses.
        session.add(Activity(
            id=f"ord0522-broken-{index}-{now[-9:]}", plan_version_id=plan.id,
            sequence_no=100 + index, activity_type="sight", title=f"broken {index}",
            place_id=None, start_at="2026-10-10T11:00:00+08:00",
            end_at="2026-10-10T12:00:00+08:00", travel_from_prev_min=0,
            estimated_cost_minor=0, currency="CNY", status="NOT_STARTED",
            locked=False, metadata_json="{}", created_at=now, updated_at=now,
        ))
    journey = session.get(Journey, journey_id)
    journey.active_plan_version_id = plan.id
    session.commit()
    return plan.id


# ---------------------------------------------------------------------------------------------
def test_S04_dropped_stops_are_named_not_hidden(seeded) -> None:
    """不许默默少行程：能落脚的站与落不了脚的站要一起报出来，并且带上产品自己那句理由。"""

    with SessionLocal() as session:
        plan_id = _stage_plan(session, seeded, provenance="LIVE_PROVIDER", broken_stops=2)
        from sqlalchemy import func as sa_func

        null_place = int(session.scalar(
            select(sa_func.count(Activity.id)).where(Activity.plan_version_id == plan_id,
                                                    Activity.place_id.is_(None))
        ) or 0)
    supply = _scene_supply()
    stops = supply["stops"]
    assert set(stops) == STOPS_KEYS
    assert stops["plan_present"] is True
    assert stops["total"] == 4 and stops["with_coordinate"] == 2, stops
    # 落不了脚的站数是从库里直接数出来的同一枚数：少报一枚就是「默默少行程」的那个坑
    assert stops["without_coordinate"] == null_place == 2, (stops, null_place)
    assert stops["reasons"], f"少了站却不给理由，就是这一单要修的形状：{stops}"
    for reason in stops["reasons"]:
        assert reason in ALLOWED_REASONS, f"盘面报出一个不属于任何词表的理由：{reason}"
    # 每一枚落不了脚的站都带着产品自己那句理由，而且这句理由属于既有词表 —— 屏上不许只剩「没有内容」
    assert stops["reasons"] == [capability_module.REASON_NO_CANONICAL_IDENTITY], stops["reasons"]


def test_S05_replay_is_never_supply(seeded) -> None:
    """反例：作业说准备好了，但这一屏的地点全是从前的 replay —— 那一格仍不许算供给。"""

    with SessionLocal() as session:
        _stage_plan(session, seeded, provenance="REPLAY")
        _store_job(session, seeded, status=hydration_module.COMPLETED, phase="FINALIZING",
                   outcome=hydration_module.OUTCOME_PREPARED,
                   attempts=[_attempt("https://photon.komoot.io/api/", answered=True,
                                      http_status=200, features=9, candidates=9)])
    supply = _scene_supply()
    assert supply["stops"]["source_kinds"] == ["REPLAY"], supply["stops"]
    assert supply["state"] == hydration_module.OUTCOME_PREPARED
    assert supply["counts_as_supply"] is False, "全屏 replay 还被当供给，就是这一单要堵的口"

    # 正控：同一棵树只换地点的来路，那一格才许亮起来 —— 证明上面那条不是恒假
    with SessionLocal() as session:
        for place in session.scalars(select(Place).where(Place.id.like("ord0522-place-%"))).all():
            place.metadata_json = json.dumps({"provenance": "LIVE_PROVIDER"})
        session.commit()
    again = _scene_supply()
    assert again["stops"]["source_kinds"] == ["REMOTE_PROVIDER"], again["stops"]
    assert again["counts_as_supply"] is True, again


def test_S06_the_read_asks_nobody(seeded, monkeypatch) -> None:
    """开机门似的一条：盘面这一读不许打任何一跳。把三条腿的客户端都换成会炸的假件，读数照常。"""

    import app.integrations.discovery.real_city as real_city_module

    def _boom(*_a, **_kw):
        raise AssertionError("盘面的一次读取打到了供应商")

    monkeypatch.setattr(real_city_module, "PhotonCityAnchor", _boom)
    monkeypatch.setattr(real_city_module, "NominatimGeocoder", _boom)
    monkeypatch.setattr(real_city_module, "OverpassPOIProvider", _boom)
    monkeypatch.setattr(capability_module, "_provider_transport", _boom)
    with SessionLocal() as session:
        _store_job(session, seeded, status=hydration_module.UNAVAILABLE,
                   phase="RESOLVING_DESTINATION",
                   outcome=hydration_module.OUTCOME_RESOLVER_RUNTIME_FAILURE,
                   attempts=[], reason_code=capability_module.REASON_RESOLVER_UNAVAILABLE)
    supply = _scene_supply()
    assert supply["state"] == hydration_module.OUTCOME_RESOLVER_RUNTIME_FAILURE
    assert supply["retryable"] is True, "可重试的运行时失败被报成终态，等于让人一直点"
    assert supply["failed_stage"] == "RESOLVING_DESTINATION"
    assert set(supply["leg_states"]) == LEG_KEYS


def test_S07_no_second_vocabulary(seeded) -> None:
    """词表那一半：盘面说出口的每个值都得是原主集合里的成员，一个自造词都不许出现。"""

    with SessionLocal() as session:
        _store_job(session, seeded, status=hydration_module.COMPLETED, phase="FINALIZING",
                   outcome=hydration_module.OUTCOME_ALREADY_CURRENT,
                   attempts=[_attempt("https://overpass-api.de/api/interpreter", answered=True,
                                      http_status=200, features=None, candidates=12)])
    supply = _scene_supply()
    assert supply["state"] in OUTCOMES, supply["state"]
    assert supply["job_status"] in JOB_STATUSES, supply["job_status"]
    allowed_stages = set(hydration_module.PHASES) | {None}
    assert supply["failed_stage"] in allowed_stages, supply["failed_stage"]
    for leg, state in supply["leg_states"].items():
        assert leg in LEG_KEYS
        assert state is None or state in LEG_VOCABULARY, f"{leg} 说了个新词：{state}"
    assert supply["leg_states"]["overpass"] == hydration_module.LEG_CAPTURED
    assert supply["counts_as_supply"] is True
    assert supply["stops"]["plan_present"] in (True, False)


def test_S08_the_door_is_reachable_through_the_product(seeded, monkeypatch) -> None:
    """真门一趟：用产品自己的入口投递一次注水作业，再看盘面是不是照抄那一行写的。"""

    _enable(monkeypatch)
    _answer(monkeypatch, features=(_feature(osm_id=3289208, osm_type="R", name=RAW_COUNTY,
                                            extent=COUNTY_EXTENT),))
    posted = client.post(HYDRATIONS, json={"query": RAW_COUNTY, "journey_id": seeded})
    assert posted.status_code in (200, 202), posted.text
    operation_id = posted.json()["operation_id"]
    row = _wait_terminal(operation_id)
    supply = _scene_supply()
    assert supply["operation_id"] == row.id, (supply, row.id)
    assert supply["state"] == str(row.outcome or row.status)
    assert supply["job_status"] == row.status
    assert supply["failed_stage"] is None if row.outcome in hydration_module.OUTCOMES_WITH_SUPPLY \
        else supply["failed_stage"] == row.phase
    stored = json.loads(row.result_json or "{}") or json.loads(row.error_json or "{}")
    attempts = stored.get("provider_attempts") or (stored.get("details") or {}).get("provider_attempts")
    expected = hydration_module.leg_states(list(attempts or []))
    assert supply["leg_states"] == expected, (supply["leg_states"], expected, attempts)
    assert set(supply["leg_states"]) == LEG_KEYS


def _wait_terminal(operation_id: str) -> CatalogHydrationOperation:
    import time

    for _ in range(200):
        row = _row_of(operation_id)
        if row.status in hydration_module.TERMINAL_STATUSES:
            return row
        time.sleep(0.05)
    pytest.fail(f"作业 {operation_id} 没进终态，最后 {row.status} / phase {row.phase}")
