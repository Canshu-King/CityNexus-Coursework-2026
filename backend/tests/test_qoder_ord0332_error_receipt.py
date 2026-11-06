"""ORD-0332 的门：错误信封真带一枚可复述的回执号，两半都要咬。

号令者要件（09-26 那块）逐条对上：
  ① 单一固定字段名 + 文档（`contracts/ERROR_RECEIPT_ID_V1.md`）；同一请求重发要相等；拿不到就**不写这个键**。
  ③ 后端 pytest 与前端两向读数件各一枚；不得靠 skip 或改既有断言拿绿（本文件是新文件，既有门一字未动）。

这一枚门真正钉的是"**该变的要变、不该变的不能变**"：重发同一发调用 ⇒ 号相等；换路径 / 换幂等键 / 换 code
⇒ 号不等；查询参数换个顺序 ⇒ 还算同一发。只判"有没有一个号"是量不到这层的。
"""
from __future__ import annotations

import ast
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import error_receipt as ER
from app.core.config import settings
from app.db import write_deadline as WD
from app.db.models import Journey
from app.db.session import SessionLocal, engine, ensure_schema, make_engine
from app.main import app, error

client = TestClient(app, raise_server_exceptions=False)
DB_PATH = Path(settings.database_url.replace("sqlite:///", "", 1))
#: 门里自带的字面副本：import 产品常量会让它永远等于自己，量不到漂移（ORD-0281 T-c 那一课）
FIELD_SENT = "error_receipt_id"
RECEIPT_RE = re.compile(r"^CNX-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{8}$")
OUTCOME_ROUTE = "/api/v1/journeys/{journey_id}/outcomes"
MISSING_ROUTE = "/api/v1/journeys/{journey_id}"
MISSING_JID = "00000000-0000-4000-8000-000000000000"
GATE_BUSY_TIMEOUT_MS = 250


@pytest.fixture(autouse=True)
def _clean_seeds() -> None:
    ER.clear_receipt_seed()
    WD.reset_request_budget()
    yield
    ER.clear_receipt_seed()
    WD.reset_request_budget()


@pytest.fixture(scope="module", autouse=True)
def _stage():
    ensure_schema()
    with SessionLocal() as session:
        if not session.scalar(select(func.count()).select_from(Journey)):
            from app.services import seed_demo

            seed_demo(session)


def _journey_ids(n: int = 2) -> list[str]:
    with SessionLocal() as session:
        return [str(x) for x in session.scalars(select(Journey.id)).all()[:n]]


@contextmanager
def _configured(**values):
    saved = {name: getattr(settings, name) for name in values}
    engine.dispose()
    try:
        for name, value in values.items():
            object.__setattr__(settings, name, value)
        engine.dispose()
        yield
    finally:
        for name, value in saved.items():
            object.__setattr__(settings, name, value)
        engine.dispose()


@contextmanager
def _second_writer_holds_the_lock():
    holder = sqlite3.connect(DB_PATH.as_posix(), timeout=0, isolation_level=None)
    holder.execute(f"PRAGMA busy_timeout={GATE_BUSY_TIMEOUT_MS}")
    holder.execute("CREATE TABLE IF NOT EXISTS ord0332_holder (n integer)")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO ord0332_holder VALUES (1)")
    try:
        yield holder
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def _past_budget_503(journey_id: str):
    """走 ORD-0327 那条真路：预算用完 + 丢锁 ⇒ 具名 503（旅客会念出口的那一发）。"""

    with _configured(write_busy_timeout_ms=GATE_BUSY_TIMEOUT_MS, write_deadline_ms=1):
        with _second_writer_holds_the_lock():
            return client.post(OUTCOME_ROUTE.format(journey_id=journey_id),
                               json={"event_type": "USER_ACCEPTED"})


def _rejected_422(journey_id: str):
    return client.post(OUTCOME_ROUTE.format(journey_id=journey_id), json={"event_type": "NOT_A_TYPE"})


def _envelope(response) -> dict:
    assert response.status_code >= 400, (response.status_code, response.text[:200])
    body = response.json()["error"]
    assert FIELD_SENT in body, body
    assert RECEIPT_RE.fullmatch(body[FIELD_SENT]), body
    return body


# --------------------------------------------------------------------------- ① 字段名单一、固定、有文档
def test_R1_the_field_name_is_single_and_documented() -> None:
    assert ER.RECEIPT_FIELD == FIELD_SENT
    doc = Path(__file__).resolve().parents[2] / "contracts" / "ERROR_RECEIPT_ID_V1.md"
    text = doc.read_text(encoding="utf-8")
    assert text.count(FIELD_SENT) >= 3, "文档没把这个字段名讲清楚"
    assert "not written" in text and RECEIPT_RE.pattern in text, "缺了'拿不到就不写'或形状定义"
    root = Path(__file__).resolve().parents[2]          # 本门的导出树根（backend 的上一级）
    # 按 AST 的**字符串字面量**数，不按文本：注释里写一次字段名是文档，代码里写第二次才是漂移
    owners = []
    for f in sorted((root / "backend" / "app").rglob("*.py")):
        nodes = ast.parse(f.read_text(encoding="utf-8")).body
        hit = any(isinstance(n, ast.Constant) and n.value == FIELD_SENT
                  for sub in ast.walk(ast.Module(body=nodes, type_ignores=[])) for n in [sub])
        if hit:
            owners.append(f.relative_to(root).as_posix())
    assert owners == ["backend/app/error_receipt.py"], (
        f"字段名被抄出了第二处 ⇒ 单一固定这条已经破了：{owners}")


# ------------------------------------------------------------- ① 有 id：真 HTTP 一发，形状对、可复述
def test_R2_a_real_rejection_carries_a_recitable_id() -> None:
    jid = _journey_ids(1)[0]
    healthy = client.post(OUTCOME_ROUTE.format(journey_id=jid), json={"event_type": "USER_ACCEPTED"})
    assert healthy.status_code == 201, (healthy.status_code, healthy.text[:200])
    body = _envelope(_rejected_422(jid))
    assert body["code"] == "VALIDATION_ERROR"
    assert body[FIELD_SENT] != body["trace_id"], "两枚号各管各的事，不许互相冒充"


def test_R3_the_same_call_resends_to_the_same_id_while_the_trace_does_not() -> None:
    jid = _journey_ids(1)[0]
    first, second = _past_budget_503(jid), _past_budget_503(jid)
    assert first.status_code == second.status_code == 503
    a, b = _envelope(first), _envelope(second)
    assert a[FIELD_SENT] == b[FIELD_SENT], (a[FIELD_SENT], b[FIELD_SENT])
    assert a["trace_id"] != b["trace_id"], "trace_id 若能相等，这枚门就是在比同一发响应而不是同一发调用"


# ------------------------------------------------- ① 种子读的是调用本身：路径/幂等键/code 要把它分开
def test_R4_the_seed_reads_the_call_itself_not_the_moment() -> None:
    one = _journey_ids(1)[0]
    other_path = f"/api/v1/journeys/{_journey_ids(1)[0][:-1]}9/outcomes"
    assert other_path != f"/api/v1/journeys/{one}/outcomes"
    base = ER.ReceiptSeed("POST", f"/api/v1/journeys/{one}/outcomes", "a=1&b=2", None)
    assert ER.receipt_id_for(base, "C") == ER.receipt_id_for(
        ER.ReceiptSeed("POST", base.path, "a=1&b=2", None), "C")
    ids = {
        ER.receipt_id_for(base, "C"),
        ER.receipt_id_for(ER.ReceiptSeed("POST", other_path, "a=1&b=2", None), "C"),
        ER.receipt_id_for(ER.ReceiptSeed("POST", base.path, "a=1&b=2", "k1"), "C"),
        ER.receipt_id_for(ER.ReceiptSeed("POST", base.path, "a=1&b=2", "k2"), "C"),
        ER.receipt_id_for(ER.ReceiptSeed("GET", base.path, "a=1&b=2", None), "C"),
        ER.receipt_id_for(base, "OTHER"),
    }
    assert len(ids) == 6, f"路径/键/两枚不同的键/方法/code 没全把号分开：{ids}"
    assert ER.canonical_query([("b", "2"), ("a", "1")]) == "a=1&b=2"
    assert ER.canonical_query([("a", "1"), ("a", "2")]) == "a=1&a=2"


def test_R4b_a_real_query_reorder_over_http_is_the_same_call() -> None:
    a = _envelope(client.get(f"{MISSING_ROUTE.format(journey_id=MISSING_JID)}?x=1&y=2"))
    b = _envelope(client.get(f"{MISSING_ROUTE.format(journey_id=MISSING_JID)}?y=2&x=1"))
    assert a[FIELD_SENT] == b[FIELD_SENT], (a[FIELD_SENT], b[FIELD_SENT])
    c = _envelope(client.get(f"{MISSING_ROUTE.format(journey_id=MISSING_JID)}?x=1&y=3"))
    assert c[FIELD_SENT] != a[FIELD_SENT], "换了查询参数还是同一发调用的号 = 种子没读 query"


# -------------------------------------------------------------------- ① 拿不到就不写键：空串是假号
def test_R5_without_a_request_the_key_is_absent_not_empty() -> None:
    assert ER.current_receipt_seed() is None
    envelope = json.loads(error("ORD0332_NO_CONTEXT", "outside a request", status=503).body)["error"]
    assert FIELD_SENT not in envelope, envelope
    assert envelope.get(FIELD_SENT, "键不存在") != "", "空串会被屏幕读成『有个号，只是空的』"
    assert envelope["trace_id"] and envelope["code"] == "ORD0332_NO_CONTEXT", envelope


def test_R6_the_log_carries_the_same_id_and_the_database_carries_no_receipt_table(caplog) -> None:
    jid = _journey_ids(1)[0]
    with caplog.at_level(logging.WARNING, logger="app.main"):
        mine = _envelope(_past_budget_503(jid))[FIELD_SENT]
    assert any(mine in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records][-3:]
    assert any("route=POST" in r.getMessage() and "status=503" in r.getMessage() for r in caplog.records)
    probe = make_engine(settings.database_url)
    try:
        with probe.connect() as conn:
            tables = int(conn.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name LIKE '%receipt%'"
            ).scalar() or 0)
    finally:
        probe.dispose()
    assert tables == 0, "回执不落库是设计：失败的写会回滚，同事务存的号会跟着一起消失"


def test_R7_the_envelope_keeps_every_field_it_already_had() -> None:
    body = _envelope(_rejected_422(_journey_ids(1)[0]))
    for key in ("code", "message", "message_key", "trace_id", "details"):
        assert key in body, body
    assert body["details"]["errors"][0]["type"] == "literal_error", body["details"]


def test_R8_the_seed_is_read_off_the_real_request_not_just_the_dataclass() -> None:
    """A2 那一臂逼出来的格：`ReceiptSeed` 手搓得再对，也没人证明 `seed_for_request` 真去读了那头。

    同一发调用带两枚不同的幂等键 ⇒ 两枚号（键参与）；同键重发 ⇒ 同一枚号；换一枚无关的头 ⇒
    还是同一枚号（只有具名的那几件进种子，不是"请求越长越不像同一发"）。
    """

    jid = _journey_ids(1)[0]

    def rejected(key: str, extra: dict | None = None):
        headers = {"Idempotency-Key": key}
        headers.update(extra or {})
        return _envelope(client.post(OUTCOME_ROUTE.format(journey_id=jid),
                                     json={"event_type": "NOT_A_TYPE"}, headers=headers))

    a = rejected("ord0332-k-a")
    b = rejected("ord0332-k-b")
    a_again = rejected("ord0332-k-a", {"Accept-Language": "und"})
    assert a[FIELD_SENT] != b[FIELD_SENT], (a[FIELD_SENT], b[FIELD_SENT])
    assert a[FIELD_SENT] == a_again[FIELD_SENT], "换一枚没进种子的头就换号 ⇒ 种子读得比契约还宽"
    assert a["trace_id"] != a_again["trace_id"], "trace_id 仍该是每发信封一枚，两枚号不许合体"
