"""ORD-0327 的门：一发请求里的写，等待到底有没有上界；两半都要咬。

判据（号令者 04:05 四条里，这一枚管得住断言的那两条）：
  ② 双向 —— 预算内：原错误原样上抛，HTTP 面上**不得出现新代码**；预算外：必须具名，且不得是裸 500。
  ③ 不改既有断言换绿、不 skip：本文件是新文件；`test_qoder_ord0296_write_deadline.py` 那 11 格一字未动。

形状说明（第一版在这里错过两次，写死给后来人）：
  * WAL 下**读锁不挡提交**，挡提交的是另一发没提交的写事务 ⇒ 争用要由第二个写者制造，不是读者。
  * 一个请求的写语句不全在 `commit()` 那一刻离开：候选树 `app/main.py:718`（切出来的那一代是 :674）
    在路由中途就 flush。所以本门判的是**边界上那一个判据**（`write_deadline.boundary_verdict`），
    不是 commit 外面那一层壳。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.db import write_deadline as WD
from app.db.models import Journey
from app.db.session import SessionLocal, engine, ensure_schema, make_engine
from app.main import app
from app.services import seed_demo

client = TestClient(app, raise_server_exceptions=False)
DB_PATH = Path(settings.database_url.replace("sqlite:///", "", 1))
#: 门里自带的字面副本：import 产品常量会让它永远等于自己，量不到漂移（ORD-0281 T-c 那一课）
CODE_SENT = "SERVER_WRITE_DEADLINE_EXCEEDED"
LOCK_SENTENCES = ("database is locked", "database is busy")
PROBE_TABLE = "ord0327_write_probe"
HOLDER_TABLE = "ord0327_writer_holder"
OUTCOME_ROUTE = "/api/v1/journeys/{journey_id}/outcomes"
#: 争用时 SQLite 自己掐着等这么久。生产默认 30000 不动；门里压短只为不拖套件，不改语义
GATE_BUSY_TIMEOUT_MS = 400


@pytest.fixture(autouse=True)
def _offline_precondition() -> None:
    """外部 AI 开着 ⇒ 这一发的行为不在我想量的那一发上：红，不是 skip。"""

    if settings.ai_enabled:
        raise AssertionError("CITYNEXUS_AI_ENABLED must be off for the ORD-0327 doors")


@pytest.fixture(autouse=True)
def _clean_clock() -> None:
    """每格自己收时钟：上一格的预算不许漏给下一格。"""

    WD.reset_request_budget()
    yield
    WD.reset_request_budget()


@pytest.fixture(scope="module", autouse=True)
def _stage():
    """自带窗口与自带表：不碰 `demo/reset`（那扇门删 52 张表），收尾也不给套件留野表。"""

    ensure_schema()
    with SessionLocal() as session:
        if not session.scalar(select(func.count()).select_from(Journey)):
            seed_demo(session)
    own = make_engine(settings.database_url)
    with own.begin() as conn:
        conn.exec_driver_sql(f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE} (n integer)")
        conn.exec_driver_sql(f"CREATE TABLE IF NOT EXISTS {HOLDER_TABLE} (n integer)")
        conn.exec_driver_sql(f"INSERT INTO {HOLDER_TABLE} VALUES (0)")
    yield
    with own.begin() as conn:
        conn.exec_driver_sql(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        conn.exec_driver_sql(f"DROP TABLE IF EXISTS {HOLDER_TABLE}")
    own.dispose()


def _journey_id() -> str:
    with SessionLocal() as session:
        return str(session.scalars(select(Journey.id)).first())


def _outcome_rows(journey_id: str) -> int:
    """这一条腿的持久面读的是哪一库哪一表：`{DB_PATH}` 的 `journey_outcome_events.journey_id`。"""

    probe = make_engine(settings.database_url)
    try:
        with probe.connect() as conn:
            return int(conn.exec_driver_sql(
                "SELECT count(*) FROM journey_outcome_events WHERE journey_id = ?", (journey_id,)
            ).scalar() or 0)
    finally:
        probe.dispose()


def _envelope(body: dict) -> dict:
    """`main.py:495` 的信封把整块装在 `error` 底下；按字面读键名，读不到就是这一格红。"""

    assert "error" in body, str(body)[:200]
    return body["error"]


@contextmanager
def _configured(**values):
    """改设置前后各弃一次连接池：pragma 是连接级的，别把门里的短等待带给别人。"""

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
    """另一发没提交的写事务：WAL 下这才是挡得住本方写语句的那一枚锁。"""

    holder = sqlite3.connect(DB_PATH.as_posix(), timeout=0, isolation_level=None)
    holder.execute(f"PRAGMA busy_timeout={GATE_BUSY_TIMEOUT_MS}")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(f"INSERT INTO {HOLDER_TABLE} VALUES (1)")
    try:
        yield holder
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def _raw_contended_write():
    """没挂时钟的一次写（作业/脚本那一形），把抛出来的东西原样交回本格判。"""

    assert WD.active_budget() is None, "这一格要量的是没挂时钟的那一发"
    with SessionLocal() as session:
        session.execute(text(f"PRAGMA busy_timeout={GATE_BUSY_TIMEOUT_MS}"))
        with _second_writer_holds_the_lock():
            try:
                session.execute(text(f"INSERT INTO {PROBE_TABLE} VALUES (1)"))
            except BaseException as exc:
                return exc
    raise AssertionError("争用没成立（写成功了）——这一格的判据不成立")


def _post_outcome(journey_id: str):
    return client.post(OUTCOME_ROUTE.format(journey_id=journey_id),
                       json={"event_type": "USER_ACCEPTED"})


# ------------------------------------------------------------------ 默认档：只声明一次，且被启动门管住
def test_W1_the_default_scenario_is_declared_once_and_covered_by_the_door() -> None:
    report = WD.configuration_verdict()
    assert WD.SCENARIO_DEFAULT_WRITE in WD.SCENARIOS
    assert WD.SCENARIO_DEFAULT_WRITE not in (WD.SCENARIO_AGENT_COMMANDS, WD.SCENARIO_PLANS_GENERATE)
    assert list(report["per_scenario"]).count(WD.SCENARIO_DEFAULT_WRITE) == 1, report["per_scenario"]
    one = report["per_scenario"][WD.SCENARIO_DEFAULT_WRITE]
    assert one["verdict"] == WD.CONFIG_OK and one["reachable"] is True, one
    assert one["deadline_ms"] > one["busy_timeout_ms"], one


def test_W2_the_door_refuses_a_default_deadline_at_or_below_the_lock_wait() -> None:
    """反向半：默认档压到 busy_timeout 之下 ⇒ 门红，并且点得出是 default.write 那一格。"""

    busy = WD.busy_timeout_ms()
    with _configured(write_busy_timeout_ms=busy, write_deadline_ms=busy):
        verdict = WD.scenario_verdict(WD.SCENARIO_DEFAULT_WRITE)
        assert verdict["verdict"] == WD.CONFIG_NOT_ABOVE_BUSY, verdict
        with pytest.raises(WD.WriteDeadlineConfigurationError) as got:
            WD.assert_configuration_reachable()
        assert WD.CONFIG_NOT_ABOVE_BUSY in str(got.value)
        assert WD.SCENARIO_DEFAULT_WRITE in str(got.value), "门红了却说不出是哪一档，等于没牙"
    with _configured(write_busy_timeout_ms=busy, write_deadline_ms=busy + 1):
        assert WD.scenario_verdict(WD.SCENARIO_DEFAULT_WRITE)["verdict"] == WD.CONFIG_OK


# -------------------------------------------------- ② 前半之一：没挂时钟的那一发，错误不许被换
def test_W3_without_an_armed_clock_the_old_error_propagates_untouched() -> None:
    exc = _raw_contended_write()
    assert isinstance(exc, OperationalError) and not isinstance(exc, WD.WriteDeadlineExceeded), repr(exc)
    assert any(p in str(exc).casefold() for p in LOCK_SENTENCES), str(exc)[:160]
    assert CODE_SENT not in str(exc)
    assert WD.boundary_verdict(exc) is None, "作业/脚本那一发没有请求时钟，边界不许给它新名字"


# --------------------------------------------- ② 的算术：边界判据四形，每一形都要正反各咬一次
def test_W4_the_boundary_judge_needs_a_clock_a_lost_lock_and_an_expired_budget() -> None:
    contention = OperationalError("INSERT ...", {}, Exception("database is locked"))
    other = OperationalError("INSERT ...", {}, Exception("no such table: nope"))

    assert WD.boundary_verdict(contention) is None, "没挂时钟"

    budget = WD.start_request_budget(WD.SCENARIO_DEFAULT_WRITE)
    assert WD.boundary_verdict(contention) is None, "预算还没用完 ⇒ 今天的错误，原样"
    assert WD.boundary_verdict(other) is None, "不是丢锁的错，换了名就是洗白一个 bug"

    budget.started_monotonic -= (budget.deadline_ms + 500) / 1000.0
    named = WD.boundary_verdict(contention)
    assert isinstance(named, WD.WriteDeadlineExceeded), repr(named)
    assert named.code == CODE_SENT and named.scenario == WD.SCENARIO_DEFAULT_WRITE
    assert named.details()["elapsed_ms"] >= named.details()["deadline_ms"] > 0, named.details()
    assert any(p in named.details()["server_sentence"].casefold() for p in LOCK_SENTENCES)
    # 预算已经用完了，非丢锁的错仍然不许被改名 —— 这一格是 M3 那一臂逼出来的
    assert WD.boundary_verdict(other) is None, "期限到了也把 bug 洗成了 503"
    # 具名的是"这一发挂的那一档"，不是门里写死的默认档：两枚已具名场景走同一判据
    WD.reset_request_budget()
    plans = WD.start_request_budget(WD.SCENARIO_PLANS_GENERATE)
    plans.started_monotonic -= (plans.deadline_ms + 500) / 1000.0
    assert WD.boundary_verdict(contention).scenario == WD.SCENARIO_PLANS_GENERATE


# ------------------------------------------------------ ② 后半：真 HTTP 面，预算外必须具名、非裸 500
def test_W5_http_past_the_budget_answers_named_and_persists_nothing() -> None:
    jid = _journey_id()
    before = _outcome_rows(jid)
    with _configured(write_busy_timeout_ms=GATE_BUSY_TIMEOUT_MS, write_deadline_ms=1):
        told = WD.busy_timeout_ms()
        with _second_writer_holds_the_lock():
            over = _post_outcome(jid)
    assert over.status_code == 503, (over.status_code, over.text[:200])
    envelope = _envelope(over.json())
    assert envelope.get("code") == CODE_SENT, str(envelope)[:200]
    assert envelope.get("message_key") == "error.server_write_deadline_exceeded", envelope
    details = envelope.get("details") or {}
    assert details.get("scenario") == WD.SCENARIO_DEFAULT_WRITE, details
    assert details.get("deadline_ms") == 1, details
    assert details.get("elapsed_ms") >= details.get("deadline_ms"), details
    assert details.get("busy_timeout_ms") == told == GATE_BUSY_TIMEOUT_MS, details
    assert any(p in str(envelope.get("message", "")).casefold() for p in LOCK_SENTENCES), str(envelope)[:200]
    assert _outcome_rows(jid) == before, f"失败的一发不该在 {DB_PATH} 的 journey_outcome_events 里留下行"


def test_W6_http_inside_the_budget_keeps_todays_face_with_no_new_name() -> None:
    """预算内：不许出现新代码。今天它本来就是框架的裸 500 —— 这一格钉的是"我没把它改成别的"。"""

    jid = _journey_id()
    before = _outcome_rows(jid)
    default = WD.deadline_ms(WD.SCENARIO_DEFAULT_WRITE)
    with _configured(write_busy_timeout_ms=GATE_BUSY_TIMEOUT_MS, write_deadline_ms=default):
        with _second_writer_holds_the_lock():
            inside = _post_outcome(jid)
    assert inside.status_code == 500, (inside.status_code, inside.text[:200])
    assert CODE_SENT not in inside.text, inside.text[:200]
    assert _outcome_rows(jid) == before, f"预算内失败的一发也不许留半行：{DB_PATH}"


# ------------------------------- 界是"每发请求一枚"，且不洗白：预算小到手写的健康写也照样落库
def test_W7_a_spent_budget_names_only_the_request_that_lost_the_lock() -> None:
    jid = _journey_id()
    before = _outcome_rows(jid)
    with _configured(write_busy_timeout_ms=GATE_BUSY_TIMEOUT_MS, write_deadline_ms=1):
        with _second_writer_holds_the_lock():
            first = _post_outcome(jid)
        after_failure = _outcome_rows(jid)
        healthy = _post_outcome(jid)                     # 同一枚小预算，没有争用
    assert first.status_code == 503, (first.status_code, first.text[:200])
    assert healthy.status_code == 201, (healthy.status_code, healthy.text[:200])
    assert CODE_SENT not in healthy.text
    assert after_failure == before, "失败那一发不该留行"
    assert _outcome_rows(jid) == before + 1, (
        f"健康那一发的行没落到 {DB_PATH} 的 journey_outcome_events："
        f"{before}→{_outcome_rows(jid)}")
    WD.reset_request_budget()
    outside = _raw_contended_write()                    # 出了请求，时钟不跟出去
    assert isinstance(outside, OperationalError) and CODE_SENT not in str(outside)
