"""ORD-0095-1 lane C - the promises this implementation makes that lane F's doors do not cover.

Lane F's seven gates decide whether the mission is done; they are its file and its assertions,
and nothing here weakens or duplicates a judgement they already make.  These doors cover the
three things only the writer can see from the inside, each because the project has been charged
with the corresponding failure before:

* **ordering** - §6 says short read -> release -> provider work -> short write.  A background
  thread that merely *moves* the write lock satisfies none of that, so this file measures it:
  while the job is inside a stage, another connection must still be able to commit.
* **the durable layer** - idempotency has to be a database rule, because a process-local dict
  does not survive a second worker; the door attacks the store directly.
* **the provider boundary** - ORD-0095-1 is scoped to sources already in this build and R1/R6
  are unarbitrated, so "we did not quietly start calling Photon" is only worth anything as a
  door that fails the moment somebody does.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

import app.anywhere_capability as capability_module
import app.catalog_hydration_operations as hydration_module
from app.db.models import CatalogHydrationOperation, Place
from app.db.session import SessionLocal, engine
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))
CAPABILITY = "/api/v1/destinations/capability"
HYDRATIONS = "/api/v1/catalog-hydrations"
UNSEEN_COUNTY = "贡山独龙族怒族自治县"
STAGE_CITY = "CN-QINGPU"
LIVE_HOSTS = (
    "photon.komoot.io",
    "overpass-api.de",
    "overpass.private.coffee",
    "overpass.kumi.systems",
    "nominatim.openstreetmap.org",
    "restapi.amap.com",
    "api.open-meteo.com",
    "api.deepseek.com",
    "valhalla1.openstreetmap.de",
)
ARBITER_STATUSES = {
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "UNAVAILABLE",
    "ABANDON_REQUESTED",
    "ABANDONED",
}
REQUIRED_PROJECTION = (
    "operation_id",
    "fingerprint",
    "reused",
    "status",
    "phase",
    "elapsed_ms",
    "retry_after_ms",
    "status_url",
    "abandon_url",
)


@pytest.fixture()
def seeded(monkeypatch) -> str:
    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, reset.text
    with SessionLocal() as session:
        session.execute(CatalogHydrationOperation.__table__.delete())
        session.commit()
        session.execute(text("DELETE FROM places WHERE city_code = :c"), {"c": STAGE_CITY})
        session.commit()
        assert session.scalar(
            select(func.count(Place.id)).where(Place.city_code == STAGE_CITY)
        ) in (0, None), "SETUP 失败：舞台城市本来就有行，那这几条门什么都没测"
    return str(reset.json()["traveler_id"])


def _place(
    place_id: str,
    category: str,
    *,
    updated_at: str | None = None,
    provider_key: str = "mock-city",
    city_code: str = STAGE_CITY,
) -> None:
    with SessionLocal() as session:
        session.add(
            Place(
                id=place_id,
                provider_key=provider_key,
                external_id=place_id.upper(),
                city_code=city_code,
                name=place_id.replace("place-", "").replace("-", " ").title(),
                category=category,
                latitude=31.1,
                longitude=121.0,
                opening_hours_json=json.dumps({"open": "09:00", "close": "17:00"}),
                tags_json=json.dumps([]),
                metadata_json=json.dumps({"duration_min": 60, "cost_minor": 3000}),
                updated_at=updated_at or datetime.now(TZ).isoformat(),
            )
        )
        session.commit()


def _wait_terminal(operation_id: str, seconds: float = 15.0) -> dict:
    deadline = time.monotonic() + seconds
    body: dict = {}
    while time.monotonic() < deadline:
        poll = client.get(f"{HYDRATIONS}/{operation_id}")
        assert poll.status_code == 200, poll.text
        body = poll.json()
        if body.get("terminal") is True:
            return body
        time.sleep(0.05)
    pytest.fail(f"产品：操作 {operation_id} 在 {seconds}s 内没有进入终态，最后 {str(body)[:200]}")


# --------------------------------------------------------------------------------------
# §6 ordering, measured rather than asserted in prose
# --------------------------------------------------------------------------------------
def test_the_job_never_holds_the_sqlite_writer_across_its_slow_stage(seeded, monkeypatch) -> None:
    """The failure this door exists for: a long write transaction owned by a background job.

    The stay-generation path once held SQLite's only writer for ~115 seconds and moving the work
    to a thread did not fix it.  So: stall the job inside its evaluation stage, and from another
    connection try to commit.  If a transaction were being held, this commit would wait out the
    busy timeout and the door would report the measured wait.
    """

    original_evaluate = hydration_module.evaluate_capability
    inside_stage = threading.Event()
    release = threading.Event()

    def stalling_evaluate(*args, **kwargs):
        body = original_evaluate(*args, **kwargs)
        inside_stage.set()
        # Bounded, so a lost signal fails this door instead of hanging the whole suite.
        assert release.wait(timeout=6.0), "SETUP 失败：门没有放行，说明工作线程卡在别处"
        return body

    monkeypatch.setattr(hydration_module, "evaluate_capability", stalling_evaluate)
    accepted = client.post(HYDRATIONS, json={"query": "Beijing"})
    assert accepted.status_code in (200, 202), accepted.text
    operation_id = accepted.json()["operation_id"]
    assert inside_stage.wait(timeout=10.0), "期望：作业确实走到了评估这一步；实际：10 秒内没进来"

    waited: list[float] = []
    failure: list[str] = []

    def outsider() -> None:
        connection = sqlite3.connect(engine.url.database, timeout=4.0)
        try:
            started = time.perf_counter()
            connection.execute("PRAGMA busy_timeout = 4000")
            connection.execute(
                "INSERT INTO places (id, provider_key, external_id, city_code, name, category,"
                " latitude, longitude, opening_hours_json, tags_json, metadata_json, updated_at)"
                " VALUES ('place-outsider-write','mock-city','OUTSIDER','CNX-DEMO','Outsider',"
                " 'museum',31.1,121.0,'{}','[]','{}','2026-11-01T00:00:00+08:00')"
            )
            connection.commit()
            waited.append((time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            failure.append(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                connection.execute("DELETE FROM places WHERE id = 'place-outsider-write'")
                connection.commit()
                connection.close()
            except Exception:  # noqa: BLE001 - the door already has its answer
                pass

    thread = threading.Thread(target=outsider)
    thread.start()
    thread.join(timeout=12.0)
    release.set()
    thread.join(timeout=8.0)
    assert not thread.is_alive(), "期望：局外写线程能结束；实际：还卡着 —— 写锁被作业握住了"
    assert not failure, f"期望：另一条连接能在作业跑阶段时提交；实际：{failure}"
    assert waited and waited[0] < 2500.0, (
        f"期望：局外提交几乎立刻完成（作业不得跨阶段握写锁）；实际：等了 "
        f"{round(waited[0], 1) if waited else 'never'} ms"
    )
    body = _wait_terminal(operation_id)
    assert body["status"] in ARBITER_STATUSES, body


# --------------------------------------------------------------------------------------
# idempotency lives in the store, not in this process
# --------------------------------------------------------------------------------------
def test_two_open_jobs_for_one_fingerprint_cannot_both_exist(seeded) -> None:
    """Directly against the table: the unique index, not the code path that uses it."""

    row = {
        "fingerprint": "deadbeefdeadbeefdeadbeefdeadbeef01",
        "canonical_destination_id": "CN-TESTCITY",
        "catalog_schema_version": "catalog-v1",
        "readiness_policy_version": "readiness-v1",
        "provider_policy_version": "provider-v1",
        "queued_at": "2026-09-21T00:00:00+00:00",
        "updated_at": "2026-09-21T00:00:00+00:00",
    }
    try:
        with SessionLocal() as session:
            session.add(
                CatalogHydrationOperation(id="hyd-probe-a", status="QUEUED", phase="QUEUED", **row)
            )
            session.commit()
        with SessionLocal() as session:
            session.add(
                CatalogHydrationOperation(id="hyd-probe-b", status="RUNNING", phase=None, **row)
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        # A terminal row must not fence a traveller off forever: "ask again later" is legal.
        with SessionLocal() as session:
            session.execute(
                text("UPDATE catalog_hydration_operations SET status = 'COMPLETED' WHERE id = :i"),
                {"i": "hyd-probe-a"},
            )
            session.commit()
            session.add(
                CatalogHydrationOperation(id="hyd-probe-c", status="QUEUED", phase="QUEUED", **row)
            )
            session.commit()
            open_count = int(
                session.scalar(
                    select(func.count(CatalogHydrationOperation.id)).where(
                        CatalogHydrationOperation.fingerprint == row["fingerprint"],
                        CatalogHydrationOperation.status.in_(
                            ("QUEUED", "RUNNING", "ABANDON_REQUESTED")
                        ),
                    )
                )
                or 0
            )
            assert open_count == 1, (
                f"期望：同一指纹的活动操作至多一条（终态行不计入）；实际：{open_count}"
            )
    finally:
        with SessionLocal() as session:
            session.execute(
                text("DELETE FROM catalog_hydration_operations WHERE id LIKE 'hyd-probe-%'")
            )
            session.commit()


def test_the_second_caller_is_told_the_truth_about_who_started_the_work(seeded) -> None:
    """`reused` is not decoration: one of two callers did not start anything."""

    first = client.post(HYDRATIONS, json={"query": UNSEEN_COUNTY})
    assert first.status_code == 202, first.text
    assert first.json()["reused"] is False, first.json()
    _wait_terminal(first.json()["operation_id"])
    second = client.post(HYDRATIONS, json={"query": UNSEEN_COUNTY})
    assert second.status_code in (200, 202), second.text
    assert second.json()["fingerprint"] == first.json()["fingerprint"], second.json()
    assert second.json()["reused"] is False, (
        "期望：上一条已到终态，所以这次是**新开始**的作业（reused 必须说实话）；实际："
        f"reused={second.json()['reused']!r}"
    )
    _wait_terminal(second.json()["operation_id"])


def test_the_projection_carries_the_lifecycle_and_never_a_percentage(seeded) -> None:
    """Two axes, the arbiter's status words, and no invented denominator."""

    scene = client.get("/api/v1/demo/scene").json()
    journey_id = scene.get("journey_id")
    assert journey_id, f"SETUP 失败：种子里没有 journey，实际 {scene}"
    accepted = client.post(HYDRATIONS, json={"query": "Beijing", "journey_id": journey_id})
    assert accepted.status_code in (200, 202), accepted.text
    body = accepted.json()
    missing = [field for field in REQUIRED_PROJECTION if field not in body]
    assert not missing, (
        f"期望：受理回答就有契约 required 的九个字段；实际缺 {missing}，现有 {sorted(body)}"
    )
    assert body["status"] in ARBITER_STATUSES, body
    assert "progress" not in body, body
    for segment in ("canonical_destination_id", "catalog_schema_version",
                    "readiness_policy_version", "provider_policy_version"):
        assert body.get(segment), f"期望：指纹四段在受理回答里说得出（{segment}）；实际：{sorted(body)}"
    terminal = _wait_terminal(body["operation_id"])
    assert terminal["status"] in ARBITER_STATUSES, terminal
    assert terminal["phase"] in hydration_module.PHASES, terminal
    assert terminal["retry_after_ms"] == 0, (
        f"期望：终态的轮询提示是 0（别再问了）；实际：{terminal['retry_after_ms']}"
    )
    assert terminal["journey_id"] == str(journey_id), terminal


def test_a_job_stopped_before_its_worker_starts_still_closes_itself(seeded, monkeypatch) -> None:
    """Measured defect, kept closed: an abandon with no worker left must not orphan the row.

    `ABANDON_REQUESTED` is an open status, so a row left in it is not merely unfinished - the
    partial unique index keeps pointing every later request with the same fingerprint at a job
    with no worker, and each of those callers waits for something that can never arrive.

    The schedule is now pinned instead of raced.  The previous version fired an abandon at an
    arbitrary moment after the POST and asserted that the abandon won, so which of two legal
    endings the store held depended on thread interleaving, and this door was the file's
    rotating red - a gate that can fail because of its own timing is not evidence either way.
    Patching `dispatch` out makes "no worker will ever run this" a stated fact rather than a hope.
    """

    with monkeypatch.context() as stage:
        stage.setattr(hydration_module, "dispatch", lambda operation_id: None)
        accepted = client.post(HYDRATIONS, json={"query": "上海"})
        assert accepted.status_code in (200, 202), accepted.text
        operation_id = accepted.json()["operation_id"]
        abandoned = client.post(f"{HYDRATIONS}/{operation_id}/abandon")
        assert abandoned.status_code == 200, abandoned.text
    terminal = _wait_terminal(operation_id, seconds=8.0)
    assert terminal["status"] == "ABANDONED", (
        f"期望：没有 worker 会被叫起的作业，受理放弃时就地收口；实际：{terminal['status']} / "
        f"phase {terminal['phase']} / 无结果={not terminal['result']}"
    )
    assert not terminal["result"], "期望：放弃了就不要写出结果；实际：" + str(terminal["result"])[:160]
    assert terminal["outcome"] == hydration_module.OUTCOME_ABANDONED, terminal
    following = client.post(HYDRATIONS, json={"query": "上海"})
    assert following.status_code in (200, 202), following.text
    assert following.json()["reused"] is False, (
        "期望：终态之后的同指纹请求是新作业，不是被塞给一条已经没有 worker 的死作业；实际："
        f"reused={following.json()['reused']} / operation={following.json()['operation_id']}"
    )
    assert following.json()["operation_id"] != operation_id, following.json()
    _wait_terminal(following.json()["operation_id"], seconds=8.0)


def test_a_worker_that_wakes_on_an_abandoned_row_closes_it_instead_of_running_it(
    seeded, monkeypatch
) -> None:
    """The other half of the same promise, also scheduled rather than raced.

    Nothing was dispatched, the traveller stopped the job, and this time the endpoint is told a
    worker exists - so the durable row is left in `ABANDON_REQUESTED`, which is the state the
    worker's own first branch has to deal with.  Dispatching afterwards makes that branch the
    measured subject instead of a lucky interleaving.
    """

    real_dispatch = hydration_module.dispatch
    monkeypatch.setattr(hydration_module, "dispatch", lambda operation_id: None)
    monkeypatch.setattr(hydration_module, "has_worker", lambda operation_id: True)
    accepted = client.post(HYDRATIONS, json={"query": "上海"})
    operation_id = accepted.json()["operation_id"]
    abandoned = client.post(f"{HYDRATIONS}/{operation_id}/abandon")
    assert abandoned.status_code == 200, abandoned.text
    assert abandoned.json()["recorded_status"] == "ABANDON_REQUESTED", abandoned.json()
    monkeypatch.setattr(hydration_module, "has_worker", lambda operation_id: False)
    real_dispatch(operation_id)
    terminal = _wait_terminal(operation_id, seconds=8.0)
    assert terminal["status"] == "ABANDONED", (
        f"期望：接手时行已被放弃的 worker 只能就地收口，不许跑阶段机；实际：{terminal['status']} / "
        f"phase {terminal['phase']}"
    )
    assert not terminal["result"], terminal


def test_abandon_reports_a_request_not_a_death(seeded) -> None:
    """A job already on the wire is not dead, and a finished one cannot be un-finished."""

    accepted = client.post(HYDRATIONS, json={"query": "Beijing"})
    assert accepted.status_code in (200, 202), accepted.text
    operation_id = accepted.json()["operation_id"]
    abandoned = client.post(f"{HYDRATIONS}/{operation_id}/abandon")
    assert abandoned.status_code == 200, abandoned.text
    first = abandoned.json()
    assert first["status"] in ARBITER_STATUSES, first
    again = client.post(f"{HYDRATIONS}/{operation_id}/abandon")
    assert again.status_code == 200, again.text
    assert again.json()["status"] in ARBITER_STATUSES, again.json()
    terminal = _wait_terminal(operation_id)
    error = terminal.get("error") or {}
    assert error.get("code") != "HYDRATION_WORKER_DIED", (
        f"期望：这条门不许把『worker 死掉』当成可接受的终态（它就是这样藏住了非法迁移）；实际："
        f"{str(error.get('message'))[:200]}"
    )
    if terminal["status"] == "ABANDONED":
        assert not terminal["result"], (
            f"期望：放弃了就不要写出结果；实际：{str(terminal['result'])[:160]}"
        )
    else:
        assert terminal["status"] in {"COMPLETED", "UNAVAILABLE", "FAILED"}, terminal


# --------------------------------------------------------------------------------------
# the terminal write must close the job it was given, not kill the thread that owns it
#
# Measured at `116a031`, deterministic, throwaway database, no network:
#   POST /api/v1/catalog-hydrations {"query":"Beijing"} -> 202 (canonical CN-BEIJING)
#   terminal: status=FAILED outcome=None phase=FINALIZING
#             error.code=HYDRATION_WORKER_DIED
#             error.message='TransitionRefused: ABANDON_REQUESTED -> COMPLETED is not a legal transition'
# The final write declared `from_statuses=(RUNNING, ABANDON_REQUESTED)` while the table allows
# ABANDON_REQUESTED -> {ABANDONED, FAILED} only, and `mark` validates every *declared* source before
# it touches the store - so the pair raised on every resolved job regardless of the row's state.  No
# door in this file saw it because the abandon doors accept FAILED as a terminal state, and the one
# door that asserts `outcome == NO_REGISTERED_SOURCE` was pointed at a city that never resolves, so
# it took the early structural branch and never reached the line that dies.
# --------------------------------------------------------------------------------------
def test_a_destination_the_catalog_resolves_closes_its_job_and_not_its_worker(seeded) -> None:
    """A resolved destination's job must end with a real outcome, not with a dead worker."""

    accepted = client.post(HYDRATIONS, json={"query": "Beijing"})
    assert accepted.status_code in (200, 202), accepted.text
    assert accepted.json()["canonical_destination_id"] == "CN-BEIJING", accepted.json()
    terminal = _wait_terminal(accepted.json()["operation_id"])
    error = terminal.get("error") or {}
    assert error.get("code") != "HYDRATION_WORKER_DIED", (
        f"期望：终态写入不许把 worker 打死（打死=作业永远拿不到结果）；实际："
        f"{str(error.get('message'))[:200]}"
    )
    assert terminal["status"] == "COMPLETED", (
        f"期望：北京有 8 行目录，作业如实收口为 COMPLETED；实际："
        f"{terminal['status']} / outcome={terminal.get('outcome')} / phase={terminal.get('phase')}"
    )
    assert terminal["outcome"] == hydration_module.OUTCOME_ALREADY_CURRENT, terminal
    survey = (terminal.get("result") or {}).get("survey") or {}
    assert survey.get("places_held") == 8, (
        f"期望：结果里的计数来自这次真的读到的行；实际：{survey}"
    )


def test_a_destination_the_provider_resolves_reaches_the_terminal_write_too(
    seeded, monkeypatch
) -> None:
    """The ⑤ 后半 that matters to a traveller: a provider-named county must get a closed job.

    Same machinery as the seam's own doors (mocked transport, so nothing leaves the process), driven
    through the normal product entrance.  This is the case the Commander hit live with 贡山县 and the
    one the receipt's `hydration_accept_status: 202` could not see: acceptance is not closure.
    """

    from test_qoder_provider_identity_leg import (
        COUNTY_CANONICAL,
        COUNTY_EXTENT,
        RAW_COUNTY,
        _answer,
        _enable,
        _feature,
    )

    _enable(monkeypatch)
    _answer(
        monkeypatch,
        features=(_feature(osm_id=3289208, osm_type="R", name=RAW_COUNTY, extent=COUNTY_EXTENT),),
    )
    accepted = client.post(HYDRATIONS, json={"query": RAW_COUNTY})
    assert accepted.status_code in (200, 202), accepted.text
    body = accepted.json()
    assert body["canonical_destination_id"] == COUNTY_CANONICAL, body
    terminal = _wait_terminal(body["operation_id"])
    error = terminal.get("error") or {}
    assert error.get("code") != "HYDRATION_WORKER_DIED", (
        f"期望：解析成功后这条腿自己起的作业不许死在终态写入上；实际：{str(error.get('message'))[:200]}"
    )
    assert terminal["status"] == "UNAVAILABLE", (
        f"期望：有真身份但本构建无可登记来源 ⇒ 如实停在 UNAVAILABLE（有名字、没材料）；实际："
        f"{terminal['status']} / outcome={terminal.get('outcome')}"
    )
    assert terminal["outcome"] == hydration_module.OUTCOME_NO_REGISTERED_SOURCE, terminal
    assert terminal["canonical_destination_id"] == COUNTY_CANONICAL, terminal


def test_an_abandon_from_another_process_resolves_the_terminal_write_as_abandoned(
    seeded, monkeypatch
) -> None:
    """A job abandoned by a writer this process cannot see still has to close, honestly.

    The row is moved to `ABANDON_REQUESTED` straight in the store at a real stage boundary (the
    survey), and the process-local abandonment registry is forced to say "no request here" - which is
    exactly the cross-process case `PROCESS_LOCAL_ONLY` admits.  The terminal write must then resolve
    into the named abandoned branch rather than raising on a transition the table forbids.
    """

    accepted = client.post(HYDRATIONS, json={"query": "Beijing"})
    operation_id = accepted.json()["operation_id"]
    original_survey = hydration_module._survey

    def racing_survey(session, canonical_destination_id):
        moved = hydration_module.mark(
            session,
            operation_id,
            hydration_module.ABANDON_REQUESTED,
            from_statuses=(hydration_module.RUNNING,),
            abandon_requested_at=hydration_module.now_iso(),
        )
        assert moved is True, "SETUP 失败：竞态注入没改成 ABANDON_REQUESTED，这条门什么都没测"
        return original_survey(session, canonical_destination_id)

    monkeypatch.setattr(hydration_module, "_survey", racing_survey)
    monkeypatch.setattr(hydration_module, "abandonment_requested", lambda _oid: False)
    terminal = _wait_terminal(operation_id)
    assert terminal["status"] == "ABANDONED", (
        f"期望：被别处放弃的作业终态只能是 ABANDONED，且 worker 不抛；实际："
        f"{terminal['status']} / outcome={terminal.get('outcome')} / "
        f"error={str((terminal.get('error') or {}).get('message'))[:160]}"
    )
    assert not terminal["result"], (
        f"期望：放弃了就不要把结果写出去；实际：{str(terminal['result'])[:160]}"
    )
    assert terminal["outcome"] == hydration_module.OUTCOME_ABANDONED, terminal


def test_an_abandon_whose_own_write_never_landed_still_closes_the_job(seeded, monkeypatch) -> None:
    """The second shape of the same hole: the request was recorded, the row never moved.

    `abandon_operation` swallows a write that the store refused (another writer held the SQLite lock)
    and the endpoint still answers 200 - so the row can be sitting in `RUNNING` with this process
    holding an abandonment request nobody else can see.  Before `_close_abandoned` the worker's
    closing write named only `ABANDON_REQUESTED` as its source, matched zero rows, and the job stayed
    open with a spinner behind it.  The repair must go through the states the table allows, so this
    door refuses both shortcuts: widening the table, and letting the job end as anything but
    `ABANDONED`.
    """

    inside_survey = threading.Event()
    released = threading.Event()
    accepted = client.post(HYDRATIONS, json={"query": "Beijing"})
    operation_id = accepted.json()["operation_id"]
    original_survey = hydration_module._survey

    def pausing_survey(session, canonical_destination_id):
        inside_survey.set()
        released.wait(timeout=10.0)
        return original_survey(session, canonical_destination_id)

    blocked = {"left": 1}
    original_mark = hydration_module.mark

    def refusing_mark(session, oid, to_status, **kw):
        if to_status == hydration_module.ABANDON_REQUESTED and blocked["left"]:
            blocked["left"] -= 1
            raise hydration_module.TransitionRefused("simulated: the store refused this write")
        return original_mark(session, oid, to_status, **kw)

    monkeypatch.setattr(hydration_module, "_survey", pausing_survey)
    monkeypatch.setattr(hydration_module, "mark", refusing_mark)
    assert inside_survey.wait(timeout=10.0), "SETUP 失败：worker 没进到 survey 阶段，这条门什么都没测"
    abandoned = client.post(f"{HYDRATIONS}/{operation_id}/abandon")
    assert abandoned.status_code == 200, abandoned.text
    assert abandoned.json()["recorded_status"] == "RUNNING", (
        f"SETUP 失败：期望这次放弃的落库被拒（持久值仍应是 RUNNING；`status` 是本进程已经知道的"
        f"请求，`recorded_status` 才是库里的行）；实际：recorded="
        f"{abandoned.json().get('recorded_status')} / reported={abandoned.json().get('status')}"
        f" / 被拒次数 {blocked['left']}"
    )
    released.set()
    with monkeypatch.context() as inner:
        inner.setattr(hydration_module, "mark", original_mark)
        terminal = _wait_terminal(operation_id, seconds=10.0)
    assert blocked["left"] == 0, "SETUP 失败：那次拒绝没有真的发生"
    assert terminal["status"] == "ABANDONED", (
        f"期望：落库被拒的放弃请求仍要经合法两步收口成 ABANDONED；实际："
        f"{terminal['status']} / outcome={terminal.get('outcome')} / "
        f"error={str((terminal.get('error') or {}).get('message'))[:160]}"
    )
    assert not terminal["result"], terminal
    assert terminal["outcome"] == hydration_module.OUTCOME_ABANDONED, terminal


# --------------------------------------------------------------------------------------
# the boundary that must not be crossed quietly (R1 / R6 unarbitrated)
# --------------------------------------------------------------------------------------
def test_nothing_leaves_the_process_while_the_deterministic_plane_answers(seeded, monkeypatch) -> None:
    """ORD-0095-1 is scoped to sources already in this build; a silent provider is a lie.

    Every real transport call is intercepted: a host the product knows about fails the door.
    The spy proves itself at the end by making one such call and requiring the interception to
    have been recorded - otherwise "no egress" would just be an instrument that never ran.
    """

    seen: list[str] = []
    original = httpx.HTTPTransport.handle_request

    def spy(self, request, *args, **kwargs):
        host = str(request.url.host or "")
        seen.append(host)
        if host in LIVE_HOSTS:
            raise AssertionError(f"期望：本笔范围内不出网；实际：{request.url}")
        return original(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", spy)
    for query in ("Beijing", UNSEEN_COUNTY, "上海", "CNX-DEMO"):
        assert client.get(CAPABILITY, params={"query": query}).status_code == 200, query
        accepted = client.post(HYDRATIONS, json={"query": query})
        assert accepted.status_code in (200, 202), accepted.text
        _wait_terminal(accepted.json()["operation_id"])
    with pytest.raises(AssertionError, match="本笔范围内不出网"):
        httpx.get("https://photon.komoot.io/api/?q=instrument-selftest", timeout=1.0)
    assert "photon.komoot.io" in seen, (
        f"期望：拦截器真的挂上了（自我触发的出网要被记录到）；实际：{seen}"
    )
    assert not [host for host in seen if host in LIVE_HOSTS and host != "photon.komoot.io"], seen


def test_a_resolver_that_could_not_be_reached_does_not_make_a_destination_unavailable(seeded) -> None:
    """R1 as a door: a network fact must never be laundered into a structural verdict.

    0095-1 has no resolver call to fail, so the rule is pinned where the decision is made - with
    the observation the 0095-2 path would record - so that build inherits the door, not an
    accident.
    """

    with SessionLocal() as session:
        body = capability_module.evaluate_capability(
            session,
            "Beijing",
            provider_observations=[{"status": "UNREACHABLE", "stage": "resolving"}],
        )
    assert body["capability"] != "UNAVAILABLE", (
        f"期望：解析器到不了是运行时事实，目的地保持可重试（R1）；实际：{body['capability']}"
        f" / reason {body['reason_code']}"
    )
    assert body["provider_status"] == [{"status": "UNREACHABLE", "stage": "resolving"}], body


def test_an_observation_is_either_one_of_the_four_or_absent(seeded) -> None:
    """No UNKNOWN, no empty string, no invented `OK`: absence is how "not measured" speaks."""

    with SessionLocal() as session:
        body = capability_module.evaluate_capability(session, "Beijing")
    assert "provider_status" not in body, (
        f"期望：没有观测就没有这个字段（禁止编 OK/UNKNOWN/空值）；实际：{body.get('provider_status')!r}"
    )
    with SessionLocal() as session:
        invented = capability_module.evaluate_capability(
            session,
            "Beijing",
            provider_observations=[{"status": "OK"}, {"status": "TELEPATHY"}, {"status": ""}],
        )
    assert [item["status"] for item in invented["provider_status"]] == ["OK"], (
        f"期望：四值之外的观测被丢掉而不是转述；实际：{invented['provider_status']}"
    )


# --------------------------------------------------------------------------------------
# the readiness policy is a policy, not an arithmetic accident
# --------------------------------------------------------------------------------------
def test_a_city_the_catalog_already_holds_is_counted_from_that_catalog(seeded) -> None:
    """The defect the Commander measured: a seeded city answering six times MISSING.

    Beijing has eight rows in `places` on a seeded stage, so an answer of `candidate_count=0` on
    every axis is not "this city has nothing" - it is the evaluator failing to read the material
    it was given, and a front-end that trusts it would offer to hydrate a city that is already
    plannable (§13/G2 asks the opposite: the existing journey stays plannable).
    """

    listed = client.get("/api/v1/places", params={"city_code": "CN-BEIJING"}).json()
    assert len(listed) == 8, (
        f"SETUP 失败：种子里北京不是 8 行，那这条门什么都证明不了；实际 {len(listed)} 行"
    )
    body = client.get(CAPABILITY, params={"query": "CN-BEIJING"}).json()
    assert body["destination_resolution"] == "catalog", body["destination_resolution"]
    assert body["coverage_detail"]["attractions"]["candidate_count"] > 0, body["coverage_detail"]
    assert body["coverage"]["attractions"] == "READY", body["coverage"]
    assert body["capability"] != "UNAVAILABLE", (
        f"期望：库里就有的城市不得被答成『结构性不可用』；实际：{body['capability']} / "
        f"{body['reason_code']} / {body['coverage']}"
    )
    assert body["provenance"]["source_kinds"] == ["GOLDEN_FIXTURE"], body["provenance"]
    # §7: each axis names the class that filled it, so a fixture answer can never be read as a
    # fetched one - and an axis with nothing behind it says so with an empty list, not `OK`.
    assert body["coverage_detail"]["lodging"]["source_kinds"] == [], body["coverage_detail"]["lodging"]


def test_material_the_policy_cannot_name_is_reported_instead_of_counted_as_zero(seeded) -> None:
    """A category nobody maps must show up, or an unrecognised row reads as an empty catalog."""

    _place("place-strange", "pet_grooming")
    with SessionLocal() as session:
        body = capability_module.evaluate_capability(session, STAGE_CITY)
    assert body["coverage"]["attractions"] == "MISSING", body["coverage"]
    assert body["unclassified_categories"] == {"pet_grooming": 1}, body["unclassified_categories"]
    # Reporting it is not the same as counting it: unclassified material cannot raise an intent.
    assert body["capability"] != "READY", body["capability"]


def test_one_place_in_a_city_cannot_read_as_ready(seeded) -> None:
    """`places.length > 0` is the rule the arbitration forbids by name."""

    for index, category in enumerate(("museum", "cafe", "bar", "hotel")):
        _place(f"place-one-{index}", category)
    with SessionLocal() as session:
        body = capability_module.evaluate_capability(session, STAGE_CITY)
    assert body["capability"] != "READY", (
        f"期望：每类只有一个候选的城市不得报 READY（min_ready 高于 1）；实际："
        f"{body['capability']} / {body['coverage']}"
    )
    assert body["coverage"]["attractions"] == "PARTIAL", body["coverage"]


def test_a_complete_but_ancient_catalog_is_not_ready_either(seeded) -> None:
    """Stale is inside the definition of HYDRATABLE, so it cannot be READY (v0.2).

    Asserted on the stale half only: this stage city holds no explicit mobility fact, so its
    `mobility` category is MISSING whatever else is true, and a whole-city verdict would be
    decided by the fixture rather than by the rule under test.
    """

    ancient = (datetime.now(TZ) - timedelta(days=400)).isoformat()
    for index, category in enumerate(("museum", "heritage", "landmark", "restaurant", "bar", "hotel")):
        _place(f"place-old-{index}", category, updated_at=ancient)
    with SessionLocal() as session:
        body = capability_module.evaluate_capability(session, STAGE_CITY)
    assert body["capability"] != "READY", (
        f"期望：目录整体过期时不得报 READY；实际：{body['capability']} / {body['coverage']}"
    )
    stale_flags = {key: value["stale"] for key, value in body["coverage_detail"].items()}
    assert any(stale_flags.values()), f"期望：过期这件事要被说出来；实际：{stale_flags}"


def test_only_the_server_policy_can_raise_a_destination_to_ready(seeded, monkeypatch) -> None:
    """A READY answer must name the version that decided it, and track the facts.

    Beijing genuinely cannot be READY in this build (`lodging` is empty and no registered source
    can fill it), so rather than editing a shared city's rows out from under another lane's door,
    the evaluator is handed two synthetic coverage states and must answer differently for each.
    Same code, different facts - that is what "policy" means.
    """

    def states(**overrides):
        base = {
            key: {
                "state": "READY",
                "candidate_count": 5,
                "min_ready": 2,
                "source_kinds": ["GOLDEN_FIXTURE"],
                "stale": False,
            }
            for key in capability_module.COVERAGE_KEYS
        }
        for key, value in overrides.items():
            base[key] = {**base[key], **value}
        return base

    original = capability_module.coverage_detail
    monkeypatch.setattr(capability_module, "coverage_detail", lambda *_a, **_k: states())
    with SessionLocal() as session:
        ready = capability_module.evaluate_capability(session, "Beijing")
    assert ready["capability"] == "READY", ready["capability"]
    assert ready["readiness_policy_version"] == capability_module.READINESS_POLICY_VERSION, ready
    assert ready["missing_categories"] == [], ready["missing_categories"]

    monkeypatch.setattr(
        capability_module,
        "coverage_detail",
        lambda *_a, **_k: states(lodging={"state": "MISSING", "candidate_count": 0}),
    )
    with SessionLocal() as session:
        not_ready = capability_module.evaluate_capability(session, "Beijing")
    assert not_ready["capability"] == "HYDRATABLE", (
        f"期望：一个分类 MISSING 就不得 READY（READY 是六类全过的结果）；实际："
        f"{not_ready['capability']} / {not_ready['coverage']}"
    )
    assert not_ready["missing_categories"] == ["lodging"], not_ready["missing_categories"]

    monkeypatch.setattr(capability_module, "coverage_detail", original)
    with SessionLocal() as session:
        real_beijing = capability_module.evaluate_capability(session, "Beijing")
    assert real_beijing["capability"] == "HYDRATABLE", (
        f"期望：真实的北京今天就是不够全（本构建没有能填 lodging 的来源）—— 上面那个合成态"
        f"没把真城市也举成 READY；实际：{real_beijing['capability']} / {real_beijing['coverage']}"
    )


def test_provenance_reports_the_class_a_fact_arrived_with_and_never_a_better_one(seeded) -> None:
    """Normalisation may reshape a row; promoting `SIMULATED` to `REMOTE_PROVIDER` is not shaping."""

    _place("place-golden", "cafe")
    _place("place-sim", "museum", provider_key="citynexus-demo")
    _place("place-remote", "heritage", provider_key="overpass")
    with SessionLocal() as session:
        rows = session.scalars(select(Place).where(Place.city_code == STAGE_CITY)).all()
        kinds = {row.id: capability_module._source_kind(row) for row in rows}
        body = capability_module.evaluate_capability(session, STAGE_CITY)
    assert kinds == {
        "place-golden": "GOLDEN_FIXTURE",
        "place-sim": "SIMULATED",
        "place-remote": "REMOTE_PROVIDER",
    }, kinds
    assert set(body["provenance"]["source_kinds"]) == {
        "GOLDEN_FIXTURE",
        "REMOTE_PROVIDER",
        "SIMULATED",
    }, body["provenance"]
    assert body["provenance"]["upgraded_by_normalisation"] is False, body["provenance"]


def test_geometry_is_recorded_as_wgs84_and_the_provider_s_system_stays_separate(seeded) -> None:
    """R4: one field for what we store, another for what we were handed."""

    _place("place-crs", "museum")
    with SessionLocal() as session:
        row = session.scalar(select(Place).where(Place.id == "place-crs"))
        assert row is not None
        assert row.coordinate_system == "WGS84", row.coordinate_system
        assert row.source_coordinate_system is None, row.source_coordinate_system
        row.source_coordinate_system = "GCJ-02"
        session.commit()
        again = session.scalar(select(Place).where(Place.id == "place-crs"))
        assert again is not None
        assert again.coordinate_system == "WGS84", again.coordinate_system
        assert again.source_coordinate_system == "GCJ-02", again.source_coordinate_system


def test_an_unresolved_query_never_gets_a_confident_looking_identity(seeded) -> None:
    """An id that reads like a city code would be believed by everything downstream.

    ORD-0167 (arbiter `e4b7faa3`) tightened what "honest placeholder" means here: carrying the
    question in the identity column was still carrying the question in the identity column.  The
    answer must be *no identity at all*, with the words kept in their own field.
    """

    with SessionLocal() as session:
        body = capability_module.evaluate_capability(session, UNSEEN_COUNTY)
    assert body["canonical_destination_id"] is None, (
        f"期望：解析不出目的地时不发明身份，canonical_destination_id=null；实际："
        f"{body['canonical_destination_id']!r}"
    )
    assert str(body["pending_destination_label"]) == UNSEEN_COUNTY, (
        f"期望：用户原文逐字进 pending_destination_label；实际："
        f"{body['pending_destination_label']!r}"
    )
    for key, value in body.items():
        if key in {"query", "pending_destination_label", "resolver_query_normalized"}:
            continue
        assert not (isinstance(value, str) and UNSEEN_COUNTY in value), (
            f"期望：原文只出现在这三栏里；实际字段 {key!r} 也带着它（{value!r}）"
        )
    assert body["destination_state"] == "NO_MATCH", body["destination_state"]
    assert body["destination_resolution"] == "unresolved", body["destination_resolution"]
    assert body["capability"] == "UNAVAILABLE", body["capability"]
    assert body["reason_code"] not in capability_module.TRANSIENT_REASONS, body["reason_code"]
    assert body["recommended_action"], "期望：说不清目的地也要给一条出路；实际：没有 recommended_action"
    with SessionLocal() as session:
        other = capability_module.evaluate_capability(session, UNSEEN_COUNTY + " ")
    assert other["fingerprint"] == body["fingerprint"], (
        "期望：同一个问题（只差空白）落到同一个幂等指纹——收敛靠请求指纹，不靠伪造身份；实际："
        f"{str(other['fingerprint'])[:16]!r} vs {str(body['fingerprint'])[:16]!r}"
    )
    assert other["canonical_destination_id"] is None


def test_a_hydration_cannot_invent_catalog_rows_it_did_not_fetch(seeded) -> None:
    """The job reports what it found, and `places_added` here is provably nothing.

    This is the door that keeps 0095-1 honest if anybody later wants the operation to look
    productive: the only way it can add a row is by applying a registered source, and for a
    destination with no registered source there is none to apply.
    """

    accepted = client.post(HYDRATIONS, json={"query": STAGE_CITY})
    assert accepted.status_code in (200, 202), accepted.text
    terminal = _wait_terminal(accepted.json()["operation_id"])
    with SessionLocal() as session:
        held = int(
            session.scalar(select(func.count(Place.id)).where(Place.city_code == STAGE_CITY)) or 0
        )
    assert held == 0, f"期望：水合没有凭空造出地点；实际：库里多了 {held} 行"
    assert terminal["outcome"] == hydration_module.OUTCOME_NO_REGISTERED_SOURCE, terminal
    # Which branch answered matters: `CN-QINGPU` is not a destination this build can name, so this
    # UNAVAILABLE is the structural ending taken before the terminal write.  A destination that *is*
    # named and still has no material is the other door
    # (`test_a_destination_the_provider_resolves_reaches_the_terminal_write_too`), and that is the
    # one which reaches `FINALIZING`.
    assert terminal["phase"] == "DISCOVERING_POIS", (
        f"期望：本门测的是『点不出名字』那条早退支路（停在 DISCOVERING_POIS）；实际 phase="
        f"{terminal['phase']}——如果它变成了 FINALIZING，说明解析变了，本门的断言就不再代表它以为的东西"
    )
    result = terminal.get("result") or {}
    assert terminal["status"] == "UNAVAILABLE", (
        f"期望：点不出名字 ⇒ 本构建没有任何可登记来源可填 ⇒ 如实停在 UNAVAILABLE（不是 COMPLETED 的空壳）；"
        f"实际：{terminal['status']} / {terminal.get('outcome')} / {str(result)[:160]}"
    )
