"""ORD-0287 · K8 回填守卫在**权威线**上的回归载体（只引 `45b0a5e2` 上真有的东西）。

为什么要有这一格：守卫 `IdempotencyKey.response_json.is_(None)` 的行为已经在落地代被量过两次
（候选代 7c806fab5 的 `test_G9`，与 ORD-0282 的两进程 rig），但权威线上没有任何一格守着它 ——
一枚裸奔进线的守卫，下一次重写 Phase B 时没人拦。这一格就是那枚拦头。

只依赖权威线：本文件 import 的是 `app.main._execute_plan_generation_claimed`（Phase B 本体，
落地代就在 main.py:4736），**不 import** `_fill_plan_generation_receipts` —— 那枚缝只在候选代，
按 ORD-0287 第 0 条实测，引用它会让本文件在权威线上 ImportError。
形制：单进程、pytest 一次跑、库由 conftest 给（ORD-0211 那座），两格互控：
  K1 负例 = 别人已填的两把键 ⇒ 必须抛**现成那句**具名 409、别人字节逐字不动、不许多写候选；
  K2 正控 = 我自己占的两把 NULL ⇒ 必须真填上并留回我的答案（K1 不会因为"永远失败"而假绿）。
断言里的句子是本文件自带的字面副本（不 import 产品常量：那样它永远等于自己，测不到漂移）。
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.commerce.idempotency import canonical_request_signature
from app.core.config import settings
from app.db.models import Journey, Place
from app.db.session import SessionLocal, ensure_schema
from app.main import _execute_plan_generation_claimed, app
from app.schemas import GeneratePlansRequest
from app.services import seed_demo

client = TestClient(app, raise_server_exceptions=False)
DB_PATH = Path(settings.database_url.replace("sqlite:///", "", 1))
DEMO_CITY = "CNX-DEMO"
CMD_A = "我护照丢了，请告诉我怎么办。"
#: 现成那句具名 409（本文件自带的字面副本；产品那边改了，这一格就该红）
SENT_INCOMPLETE_RECEIPT = "agent idempotency record is incomplete"

_STAGE: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _demo_stage() -> None:
    """舞台：目录 + 一条可抄的演示旅程（缺才灌，走产品自己的 in-process seed，绝不 demo/reset）。"""

    def _read():
        with SessionLocal() as session:
            places = int(session.scalar(
                select(func.count()).select_from(Place).where(Place.city_code == DEMO_CITY)) or 0)
            journey = session.scalars(
                select(Journey).where(Journey.city_code == DEMO_CITY).order_by(Journey.created_at)
            ).first()
            return places, journey

    ensure_schema()
    places, journey = _read()
    if places == 0 or journey is None:
        with SessionLocal() as session:
            seed_demo(session)
        places, journey = _read()
    assert places, f"SETUP：{DEMO_CITY} 目录空 ⇒ 两格都会变成『没东西可生成』的红，那不是本单要判的问题"
    assert journey is not None, f"SETUP：{DEMO_CITY} 没有可抄的演示旅程 ⇒ 本门拒绝自造窗口"
    _STAGE.clear()
    _STAGE.update({
        "city_code": journey.city_code, "timezone": journey.timezone,
        "start_at": journey.start_at, "end_at": journey.end_at,
        "planning_mode": journey.planning_mode,
        "budget": {"amount_minor": journey.budget_amount_minor,
                   "currency": journey.budget_currency},
    })


def _setup_journey() -> str:
    traveler = client.post("/api/v1/travelers",
                           json={"display_name": "ord0287", "timezone": "Asia/Shanghai"})
    assert traveler.status_code == 201, traveler.text
    journey = client.post("/api/v1/journeys", json={
        "traveler_id": traveler.json()["id"], "title": f"ord0287-{uuid.uuid4().hex[:8]}", **_STAGE})
    assert journey.status_code == 201, journey.text
    return str(journey.json()["id"])


def _mission_id(jid: str) -> str:
    """真走一次 agent command 拿**真** mission id（编造的 id 会让审计关联会说谎）。"""

    response = client.post(f"/api/v1/journeys/{jid}/agent/commands",
                           json={"command": CMD_A, "requested_autonomy": "PLAN_ONLY"})
    assert response.status_code == 200, response.text
    mission = (response.json().get("mission") or {})
    assert mission.get("id"), f"没有 mission 就没有续跑身份可绑：{response.text[:200]}"
    return str(mission["id"])


def _durable(sql: str, args: tuple = ()) -> list[tuple]:
    """门外第二条 sqlite3 直连：只承认已提交到盘上的东西。"""

    assert DB_PATH.is_file(), f"SETUP：读不到 {DB_PATH} ⇒ durable 读数无从判定，不许当通过"
    with contextlib.closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        return list(conn.execute(sql, args))


def _durable_write(sql: str, args: tuple) -> None:
    """前置状态必须**真落盘**（`closing()` 不 commit ⇒ 前一类的空对空绿就死在这里）。"""

    with contextlib.closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        conn.execute(sql, args)
        conn.commit()


def _receipt(key: str) -> tuple[str, int | None, str | None]:
    rows = _durable("SELECT response_status, response_json FROM idempotency_keys WHERE key = ?",
                    (key,))
    assert len(rows) <= 1, f"SETUP：一把键至多一行回执，实际 {len(rows)}"
    if not rows:
        return "absent", None, None
    status, body = rows[0]
    return ("filled" if body else "unfilled"), status, body


def _plan_count(jid: str) -> int:
    return int(_durable("SELECT count(*) FROM plan_versions WHERE journey_id = ?", (jid,))[0][0])


def _claim_both(keys: list[str], signature: str, filled_with: dict | None) -> None:
    """把两把键按想要的状态真落盘（filled_with=None ⇒ 我占了没答；否则 ⇒ 别人已答完）。"""

    created = datetime.now(timezone.utc).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    payload = None if filled_with is None else json.dumps(filled_with, sort_keys=True)
    status = None if filled_with is None else 201
    insert = ("INSERT INTO idempotency_keys (key, operation, request_hash, response_status, "
              "response_json, created_at, expires_at) VALUES (?,?,?,?,?,?,?)")
    for key in keys:
        _durable_write(insert, (key, "plans.generate", signature, status, payload, created, expires))


def _phase_b(jid: str, mission: str, keys: list[str]):
    """进 Phase B 本体：与产品自己那两处调用同参（journey / request / keys / continuation_id / now）。"""

    with SessionLocal() as session:
        journey = session.scalars(select(Journey).where(Journey.id == jid)).one()
        request = GeneratePlansRequest(candidate_count=3, mission_id=mission)
        now = datetime.now(timezone.utc)
        return _execute_plan_generation_claimed(journey, request, keys, f"cont:{mission}", now)


def _identity(jid: str, mission: str, transport: str) -> tuple[list[str], str]:
    """两把键 + 签名材料，全部按产品同一构造（业务键 = cont:<mission>，签名不含键本身）。"""

    keys = [f"plans.generate:{transport}", f"plans.generate:cont:{mission}"]
    signature = canonical_request_signature({"journey_id": str(jid), "mission_id": mission,
                                             "planning_mode": _STAGE["planning_mode"],
                                             "candidate_count": 3, "place_ids": None})
    return keys, signature


def test_K1_a_receipt_another_writer_already_filled_is_never_rewritten_and_refuses_by_name() -> None:
    """owner=K1（这枚守卫的负例）：两把键都住着别人的答案 ⇒ 一行都不许动，并且必须抛现成那句具名 409。

    为什么不是 HTTP 面：Phase A 的唯一索引让第二个抢键的人在门前就被挡回去（拿 409/回放），
    根本走不到回填语句 —— 红队因此判 `HTTP_FACE_CANNOT_REACH_K8`（ORD-0282 在落地代也量到了）。
    所以这一格直接进 Phase B 本体，量的就是那半个条件。跨进程那一面归 verifier（HJ-P3 / ORD-0282），
    这一格不替它宣称已闭。
    """

    from fastapi import HTTPException

    jid = _setup_journey()
    mission = _mission_id(jid)
    keys, signature = _identity(jid, mission, f"o287k1-{uuid.uuid4().hex[:10]}")
    foreign = {"plans": [{"id": f"someone-elses-batch-{uuid.uuid4().hex[:8]}"}]}
    _claim_both(keys, signature, filled_with=foreign)
    before = {k: _receipt(k) for k in keys}
    for key in keys:
        assert before[key][0] == "filled", f"SETUP：{key} 没落成已填态 ⇒ 这一格会读空"
    plans_before = _plan_count(jid)

    with pytest.raises(HTTPException) as caught:
        _phase_b(jid, mission, keys)
    assert caught.value.status_code == 409, (
        f"K1：少填一把键必须吃现成的 409，实际 {caught.value.status_code} / {caught.value.detail}")
    assert caught.value.detail == SENT_INCOMPLETE_RECEIPT, (
        f"K1：409 必须说那句具名的话（不许另造第二套失败表达）：{caught.value.detail!r}")

    for key in keys:
        state, status, body = _receipt(key)
        assert (state, status, body) == before[key], (
            f"K1：别人那一份回执被动过了 {key}：{before[key]} → {(state, status, body)}")
        assert json.loads(body) == foreign, f"K1：{key} 现在住的不是别人的答案，就是冒充"
    assert _plan_count(jid) == plans_before, (
        f"K1：被拒的那一发不该留下候选，实际 {plans_before} → {_plan_count(jid)}"
        "（守卫拆掉时会翻倍生成，正是 ORD-0282 量到的那条副作用）")


def test_K2_my_own_unfilled_claims_do_get_filled_and_hold_my_answer() -> None:
    """owner=K1 的正控：两把我自己占的 NULL 必须被真填上并留回我的答案。

    没有这一格，K1 可以靠"永远失败"假绿（什么都拒 ⇒ 谁也覆盖不了我）。它只测回填通路本身，
    不判守卫 ⇒ 守卫在位与拆掉两种树上都该绿；因此 K1 变红而 K2 仍绿，才能归因到那一行。
    """

    jid = _setup_journey()
    mission = _mission_id(jid)
    keys, signature = _identity(jid, mission, f"o287k2-{uuid.uuid4().hex[:10]}")
    _claim_both(keys, signature, filled_with=None)
    for key in keys:
        assert _receipt(key)[0] == "unfilled", f"SETUP：{key} 该是我占的未填态：{_receipt(key)}"

    answer = _phase_b(jid, mission, keys)
    for key in keys:
        state, status, body = _receipt(key)
        assert state == "filled", f"K2：我占的那把必须填成完成态，实际 {state} / {status}"
        assert status == (422 if "refusal" in answer else 201), (
            f"K2：回执状态该跟着这一发的答案走，实际 {status} 对 {sorted(answer)}")
        assert json.loads(body) == answer, "K2：盘上那一行住着的不是这一发的答案"
    assert _plan_count(jid) >= 0 and keys[1].startswith("plans.generate:cont:")
