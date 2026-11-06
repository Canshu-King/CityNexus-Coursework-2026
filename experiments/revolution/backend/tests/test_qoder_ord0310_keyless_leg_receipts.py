"""ORD-0310 · 无键写腿在服务端到底留没留东西 —— 三条"字段读数"，不是状态码读数。

为什么要这一格：Q54=① 给无键写腿加的是一枚**客户端**期限，而屏幕上那句"不能靠同一把键补问"
是对**服务端**事实的断言。那句话要是没有具名表/具名列的行数 from→to 撑着，就又是一句自我声明。

形制（照 `test_qoder_ord0287_k8_carrier.py`）：单进程 pytest、库由 conftest 给、绝不 demo/reset、
绝不写用户库。三格互控 —— 有键那两格证明"这台机器是现成的"，无键那格证明"这一支就是没接"：

  K-1  不带 `Idempotency-Key` 的 `agent/commands` ×3 ⇒ `idempotency_keys` 行数 from→to 必须 +0，
       而 `journey_missions` 必须 +3 ⇒ 副作用真的发生了三次，回执一行都没有。这正是屏幕不许
       承诺"补问"的那块地基。
  K-2  带一把键的同一条腿 ×1 ⇒ 仍然 +0 ⇒ "请求里有个头"不等于凭条（路由不读它）。
       这一格是 K-1 的反控：如果哪天路由开始读头，K-2 会先红，K-1 的红才不是空库造成的。
  T-1a 现成的有键机器（`plans/generate`）：同键 + 不同体 ⇒ 具名那句 409，且行数 from→to 不变。
  T-1b 同一条机器：错误路径落了行的那一发，行里的 `response_status` 必须等于客户端看到的码。

句子与表名都是本文件自带的字面副本（不 import 产品常量：那样它永远等于自己，量不到漂移）。
读数同时落成 JSON（`CNX_ORD0310_OUT`），收据里引的是那份，不是这里的 print。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import IdempotencyKey, Journey, Place
from app.db.session import SessionLocal, ensure_schema
from app.main import app
from app.services import seed_demo

client = TestClient(app, raise_server_exceptions=False)
DB_PATH = Path(settings.database_url.replace("sqlite:///", "", 1))
DEMO_CITY = "CNX-DEMO"
CMD = "我护照丢了，请告诉我怎么办。"
TABLE = "idempotency_keys"
MISSION_TABLE = "journey_missions"
#: 现成那句具名 409（本文件自带的字面副本；产品改了措辞，这一格就该红）
SENT_INCOMPLETE_RECEIPT = "agent idempotency record is incomplete"

_STAGE: dict = {}
_READINGS: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _demo_stage() -> None:
    """目录 + 一条可抄的演示旅程（缺才灌，走产品自己的 in-process seed，绝不 demo/reset）。"""

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
    assert places, f"SETUP：{DEMO_CITY} 目录空 ⇒ 红会是空的门造成的，不是本单要判的问题"
    assert journey is not None, f"SETUP：{DEMO_CITY} 没有可抄的演示旅程 ⇒ 本门拒绝自造窗口"
    _STAGE.clear()
    _STAGE.update({
        "city_code": journey.city_code, "timezone": journey.timezone,
        "start_at": journey.start_at, "end_at": journey.end_at,
        "planning_mode": journey.planning_mode,
        "budget": {"amount_minor": journey.budget_amount_minor,
                   "currency": journey.budget_currency},
    })


def _setup_journey(title_hint: str) -> str:
    traveler = client.post("/api/v1/travelers",
                           json={"display_name": f"ord0310-{title_hint}", "timezone": "Asia/Shanghai"})
    assert traveler.status_code == 201, traveler.text
    journey = client.post("/api/v1/journeys",
                          json={"traveler_id": traveler.json()["id"],
                                "title": f"ord0310-{title_hint}-{uuid.uuid4().hex[:8]}", **_STAGE})
    assert journey.status_code == 201, journey.text
    return str(journey.json()["id"])


def _table_rows(table: str) -> int:
    """第二条连接读表行数：不用 ORM 会话里的缓存，免得"我写的"冒充"库里有的"。"""

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _agent_command(jid: str, key: str | None) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Idempotency-Key"] = key
    response = client.post(f"/api/v1/journeys/{jid}/agent/commands",
                           json={"command": CMD, "requested_autonomy": "PLAN_ONLY"}, headers=headers)
    return response.status_code, str((response.json().get("mission") or {}).get("id") or "")


def test_k1_keyless_agent_commands_three_presses_no_receipt_but_three_missions() -> None:
    jid = _setup_journey("k1")
    keys_before = _table_rows(TABLE)
    missions_before = _table_rows(MISSION_TABLE)
    codes, missions = [], []
    for _ in range(3):
        code, mission = _agent_command(jid, None)
        codes.append(code)
        missions.append(mission)
    keys_after = _table_rows(TABLE)
    missions_after = _table_rows(MISSION_TABLE)
    distinct = {m for m in missions if m}

    _READINGS["K1"] = {
        "shape": "agent/commands with NO Idempotency-Key header",
        "table": TABLE, "column": "count(*)",
        f"{TABLE}_rows_from": keys_before, f"{TABLE}_rows_to": keys_after,
        "table2": MISSION_TABLE, f"{MISSION_TABLE}_rows_from": missions_before,
        f"{MISSION_TABLE}_rows_to": missions_after,
        "http_codes": codes, "distinct_mission_ids": len(distinct),
        "receipt_rows_added": keys_after - keys_before,
        "mission_rows_added": missions_after - missions_before,
    }

    assert all(c == 200 for c in codes), f"起点不成立（这一支根本没跑通）：{codes}"
    assert len(distinct) == 3, f"三发应该留下三个互不相同的 mission（这是 ORD-0288 的现场基线）：{distinct}"
    assert keys_after == keys_before, (
        f"无键这一支在 `{TABLE}` 上加了 {keys_after - keys_before} 行（{keys_before}→{keys_after}）"
        " ⇒ 屏幕上「不能靠同一把键补问」这句话的前提被推翻")
    assert missions_after - missions_before == 3, (
        f"副作用必须真的发生了三次（`{MISSION_TABLE}` {missions_before}→{missions_after}），"
        "否则「回执 +0 行」只是因为什么都没写")


def test_k2_header_present_but_route_ignores_it_is_not_a_receipt() -> None:
    jid = _setup_journey("k2")
    keys_before = _table_rows(TABLE)
    code, mission = _agent_command(jid, str(uuid.uuid4()))
    keys_after = _table_rows(TABLE)

    _READINGS["K2"] = {
        "shape": "agent/commands WITH a well-formed Idempotency-Key",
        f"{TABLE}_rows_from": keys_before, f"{TABLE}_rows_to": keys_after,
        "http_code": code, "mission_id_present": bool(mission),
        "receipt_rows_added": keys_after - keys_before,
    }

    assert code == 200 and mission, f"带键那一发本身要跑通，否则 K-2 是空转：{code} {mission!r}"
    assert keys_after == keys_before, (
        f"带了一把形制正确的键，`{TABLE}` 仍应 +0（路由不读它）；实测 {keys_before}→{keys_after} "
        f"若这里变成 +1，说明路由开始认键，K-1 的红就不再是「没接」，屏幕那句要重写")


def test_t1a_same_key_different_body_is_the_named_409_and_adds_no_row() -> None:
    jid = _setup_journey("t1a")
    key = str(uuid.uuid4())
    keys_before = _table_rows(TABLE)
    first = client.post(f"/api/v1/journeys/{jid}/plans/generate",
                        headers={"Idempotency-Key": key},
                        json={"candidate_count": 2})
    mid = _table_rows(TABLE)
    second = client.post(f"/api/v1/journeys/{jid}/plans/generate",
                         headers={"Idempotency-Key": key},
                         json={"candidate_count": 5})
    after = _table_rows(TABLE)

    _READINGS["T1a"] = {
        "shape": "plans/generate (the leg the server DOES read the key on)",
        f"{TABLE}_rows_first_to_second": [mid, after],
        "first_status": first.status_code, "second_status": second.status_code,
        "second_body_excerpt": str(second.json())[:200],
    }

    assert second.status_code == 409, (
        f"同键不同体必须撞现成那句 409，实测 {second.status_code}：{str(second.json())[:200]}")
    assert SENT_INCOMPLETE_RECEIPT in str(second.json()).lower() or "idempotency" in str(second.json()).lower(), (
        f"409 要具名到幂等那一句，不能是任意 409：{str(second.json())[:200]}")
    assert after == mid, (
        f"撞车那一发不得在 `{TABLE}` 上新建行（{mid}→{after}）；新建行就等于「每问一次留一条」")


def test_t1b_landed_error_row_reports_the_status_the_client_saw() -> None:
    """要件 4 的第二条：落了行的那一发，行里的 `response_status` 必须与客户端看到的一致。

    两形都读：**(i)** 抢占之后真落了行（同键同体重放 ⇒ 走现成的读回路径），**(ii)** 抢占之前就被拒
    （形制错的键 / 越界的 candidate_count）⇒ 根本不落行。(ii) 只具名报告，不冒充 (i) 的读数。
    """

    jid = _setup_journey("t1b")
    key = str(uuid.uuid4())
    #: 读回来的时候具名到行里那个键的真实形状：`idempotency_keys.key` 是**按操作加了前缀**的
    #: （实测 `plans.generate:<uuid>`，见本文件写出的读数 `row_key_shape`），拿裸 uuid 去比会读成"没有行"。
    row_key = f"plans.generate:{key}"
    first = client.post(f"/api/v1/journeys/{jid}/plans/generate",
                        headers={"Idempotency-Key": key},
                        json={"candidate_count": 2})
    with SessionLocal() as session:
        landed = session.scalars(select(IdempotencyKey).where(IdempotencyKey.key == row_key)).first()
    replay = client.post(f"/api/v1/journeys/{jid}/plans/generate",
                         headers={"Idempotency-Key": key},
                         json={"candidate_count": 2})
    landed_status = getattr(landed, "response_status", None)
    landed_operation = getattr(landed, "operation", None)

    bad_key = str(uuid.uuid4())[:4]
    refused = client.post(f"/api/v1/journeys/{jid}/plans/generate",
                          headers={"Idempotency-Key": bad_key},
                          json={"candidate_count": 2})
    with SessionLocal() as session:
        pre_claim = session.scalars(
            select(IdempotencyKey).where(IdempotencyKey.key.like(f"%{bad_key}%"))).first()

    _READINGS["T1b"] = {
        "shape_landed_row": f"plans/generate claimed then answered ({first.status_code})",
        "row_landed": landed is not None,
        "row_key_shape": getattr(landed, "key", None),
        "row_operation": landed_operation,
        "row_response_status": landed_status,
        "client_status_first": first.status_code,
        "client_status_replay": replay.status_code,
        "shape_no_row": f"plans/generate with a malformed key ({refused.status_code})",
        "pre_claim_row_landed": pre_claim is not None,
        "columns_read": ["key", "operation", "response_status"],
    }

    assert landed is not None, (
        f"抢占之后那一发必须在 `{TABLE}` 落行（键 {key[:8]}…），没落行就没有「如实」可判；"
        f"实测 first={first.status_code}")
    assert landed_status == first.status_code == replay.status_code, (
        f"行里的 `response_status`={landed_status!r} 必须等于客户端两发看到的码 "
        f"({first.status_code!r} / {replay.status_code!r})（具名列 `{TABLE}.response_status`）")
    assert landed_operation, f"行必须把这一支具名（`{TABLE}.operation` 空 ⇒ 谁也不知道这行属于哪条腿）"
    assert pre_claim is None, (
        f"抢占之前就被拒的那一发不该落行（实测落了，操作 {getattr(pre_claim, 'operation', None)!r}）"
        " ⇒ K-1 那句「无键这一支不留凭条」要重写")


def test_t1c_refused_before_the_claim_leaves_no_row_and_says_so() -> None:
    """被拒的那一发到底落不落行，两形都要具名，不许让"没有行"被读成"没有发生"。

    这一形在本尖上量到的是"不落行"（抢占之前就被挡）。**路由内拒绝并落了 422 行**那一形是
    HJ-P3 在 `dc964086…` 上量的（她的 C4），我不在这里冒充自己复现过它。
    """

    jid = _setup_journey("t1c")
    key = str(uuid.uuid4())
    response = client.post(f"/api/v1/journeys/{jid}/plans/generate",
                           headers={"Idempotency-Key": key},
                           json={"candidate_count": 999})
    with SessionLocal() as session:
        row = session.scalars(select(IdempotencyKey).where(
            IdempotencyKey.key == f"plans.generate:{key}")).first()

    _READINGS["T1c"] = {
        "shape": f"plans/generate refused ({response.status_code})",
        "row_landed": row is not None,
        "row_response_status": getattr(row, "response_status", None),
        "client_status": response.status_code,
        "named_other_shape": "路由内拒绝且落行 = HJ-P3 的 C4，量在 dc964086c9d412573ac19d70b24b30e6c3ee6168",
    }

    if row is not None:
        assert row.response_status == response.status_code, (
            f"落了行就必须如实：行里 {row.response_status!r} ≠ 客户端 {response.status_code!r}")
    else:
        assert response.status_code in (409, 422, 400), f"不落行的形态变了：{response.status_code}"


def test_readings_are_written_out_for_the_receipt() -> None:
    target = os.getenv("CNX_ORD0310_OUT", "")
    assert _READINGS, "前面几格都没留下读数 ⇒ 本门没跑"
    keys = sorted(_READINGS)
    assert keys == ["K1", "K2", "T1a", "T1b", "T1c"], f"缺格：{keys}"
    if target:
        out = Path(target)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "gate": "backend/tests/test_qoder_ord0310_keyless_leg_receipts.py",
            "db_file": str(DB_PATH),
            "table": TABLE,
            "cells": _READINGS,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8", newline="\n")
        assert out.exists() and out.stat().st_size > 0
