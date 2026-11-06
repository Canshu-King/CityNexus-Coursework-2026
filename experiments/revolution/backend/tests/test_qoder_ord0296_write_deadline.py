"""ORD-0296 · ORD-0281 的后端半：服务端的写预算必须"够得着"，而且超时要说出自己的名字。

判据来源：包工头 08:2x 块②（Q36=① 的落地五条）。射程来源：0289 的 AST 普查（写腿 14 处、带 signal 2、
带计时器 0）——那 14 条是**前端 fetch 腿**，本单一条都不碰；本单只管服务端在写事务上的那一半。

十格，每格只问一条自己的问题，句子里的数都从本格自己的读数取；一臂红多格不算多份证据，所以每格还写明
**它不该被谁扰动**：
  · G2/G3 是同一扇门的两条腿（"调小必红、调高必绿"），删掉任一条，另一条仍然绿 ⇒ 单向的绿不算合规；
  · G6/G7 用**真锁**（第二条连接占住写锁）跑两个分支：超预算⇒具名，未超预算⇒今天的错误原样上传；
  · G9 是正控：没有争锁时写路径必须照常成功，它防的是我自己手滑；
  · G10 防"预算挂在连接上"——上一条请求等过的锁不能替这一条请求超时。
全部单进程、不依赖网络；秒数只当读数（Q25），判据一项都不依赖时长。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.db import write_deadline as WD
from app.db.models import Journey, JourneyMission
from app.db.session import SessionLocal, ensure_schema, make_engine
from app.main import app
from app.services import seed_demo

client = TestClient(app, raise_server_exceptions=False)
DB_PATH = Path(settings.database_url.replace("sqlite:///", "", 1))
BACKEND_ROOT = Path(__file__).resolve().parents[1]
#: 门里自带的字面副本（不 import 产品常量：那样它永远等于自己，量不到漂移）
CODE_SENT = "SERVER_WRITE_DEADLINE_EXCEEDED"
LOCK_SENTENCES = ("database is locked", "database is busy")
PROBE_TABLE = "ord0296_write_probe"


@pytest.fixture(autouse=True)
def _offline_precondition() -> None:
    """外部 AI / Hermes 开着 ⇒ 第二腿的行为不在我想量的那一发上：红，不是 skip。"""

    if settings.ai_enabled:
        raise AssertionError("CITYNEXUS_AI_ENABLED must be off for the ORD-0296 doors")
    if settings.live_valhalla_url:
        raise AssertionError("CITYNEXUS_LIVE_VALHALLA_URL must be unset for the ORD-0296 doors")


@pytest.fixture(scope="module", autouse=True)
def _stage():   # 有 yield：建表与删表都在本模块收尾，不给套件留一张野表
    """自带窗口（不用 `demo/reset`：那扇门删 52 张表，而且是 ~140 个模块共用的fixture 门）。"""

    ensure_schema()
    with SessionLocal() as session:
        if not session.scalar(select(func.count()).select_from(Journey)):
            seed_demo(session)
    own = make_engine(settings.database_url)
    with own.begin() as conn:
        conn.exec_driver_sql(f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE} (n integer)")
    yield
    with own.begin() as conn:
        conn.exec_driver_sql(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
    own.dispose()


def _journey_id() -> str:
    with SessionLocal() as session:
        return str(session.scalars(select(Journey.id)).first())


def _live_busy_timeout_ms() -> int:
    """SQLite 自己在连接上答的数字——不是再抄一遍配置值。

    必须用**本格自己造的引擎**：整套件跑起来时，共享 pool 里的连接被别的模块动过 PRAGMA
    （`test_qoder_anywhere_lock_duration.py` 那一族就是量锁等待的），借来的连接量到的是邻居的表。
    这条是今晚我自己撞出来的：本格单跑绿、进套件红 ⇒ 判据落在了我不拥有的那枚读数上。
    """

    own = make_engine(settings.database_url)
    try:
        with own.connect() as conn:
            return int(conn.execute(text("PRAGMA busy_timeout")).scalar_one())
    finally:
        own.dispose()


@contextmanager
def _configured(**values):
    """按环境变量的形制改配置（Settings 是 frozen dataclass，所以走 object.__setattr__ 并复原）。"""

    saved = {name: getattr(settings, name) for name in values}
    try:
        for name, value in values.items():
            object.__setattr__(settings, name, value)
        yield
    finally:
        for name, value in saved.items():
            object.__setattr__(settings, name, value)


class _LockHeld:
    """第二条连接真占住写锁（`BEGIN IMMEDIATE` + 一行写入），争用不是 mock 出来的。"""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(DB_PATH.as_posix(), timeout=0, isolation_level=None)

    def take(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute(f"INSERT INTO {PROBE_TABLE} VALUES (1)")

    def release(self) -> None:
        self.conn.execute("ROLLBACK")
        self.conn.close()


@pytest.fixture()
def held_lock() -> _LockHeld:
    if not DB_PATH.exists():
        raise AssertionError(f"the doors expect a file-backed suite DB, found {DB_PATH}")
    holder = _LockHeld()
    holder.take()
    try:
        yield holder
    finally:
        holder.release()


# ------------------------------------------------------------------- Q36 那条门的三条正向读法
def test_G1_shipped_default_is_strictly_above_the_measured_busy_timeout() -> None:
    """G1 判据1：默认期限必须严格大于**实测**的锁等待，且配置值与引擎实际值同源。

    不该被谁扰动：文案、场景表、路由顺序都改不动它；只有两个数真的贴近才会翻。
    """

    measured = _live_busy_timeout_ms()
    declared = WD.busy_timeout_ms()
    deadline = WD.deadline_ms(WD.SCENARIO_PLANS_GENERATE)
    assert measured == declared, f"engine applies {measured} ms but config declares {declared} ms"
    assert deadline > measured, (
        f"{WD.CONFIG_NOT_ABOVE_BUSY}: {deadline} ms is not strictly above the measured "
        f"busy_timeout {measured} ms"
    )
    report = WD.configuration_verdict()
    assert report["verdict"] == WD.CONFIG_OK, report
    assert report["per_scenario"][WD.SCENARIO_PLANS_GENERATE]["delta_ms"] == deadline - measured


def test_G2_door_is_RED_when_the_deadline_sits_at_or_below_busy_timeout() -> None:
    """G2 判据2 反向：默认值被调到 busy_timeout 之下/相等 ⇒ 门必须红，并且说得出是哪一档。

    把它删掉，G3 单独还是绿的 ⇒ 所以"G3 绿"不能替这一格背书（Q36 要的就是双向）。
    """

    busy = WD.busy_timeout_ms()
    rows = []
    for probe in (busy, busy - 1):
        with _configured(write_busy_timeout_ms=busy, write_deadline_ms=probe):
            verdict = WD.configuration_verdict()
            with pytest.raises(WD.WriteDeadlineConfigurationError) as caught:
                WD.assert_configuration_reachable()
        rows.append({"deadline_ms": probe, "verdict": verdict["verdict"],
                     "delta_ms": verdict["per_scenario"][WD.SCENARIO_PLANS_GENERATE]["delta_ms"],
                     "raised": WD.CONFIG_NOT_ABOVE_BUSY in str(caught.value)})
        assert verdict["verdict"] == WD.CONFIG_NOT_ABOVE_BUSY, rows[-1]
        assert caught.value is not None
    assert [row["deadline_ms"] for row in rows] == [busy, busy - 1], rows
    assert all(row["delta_ms"] <= 0 and row["raised"] for row in rows), rows
    with _configured(write_busy_timeout_ms=busy, write_deadline_ms=busy + 1):
        assert WD.configuration_verdict()["verdict"] == WD.CONFIG_OK


def test_G3_door_is_GREEN_one_millisecond_above_it() -> None:
    """G3 判据2 正向：同一扇门，只把期限抬到 busy_timeout + 1 ⇒ 必须放行。

    与 G2 的差**只有一个变量**（deadline 那一枚），所以这两格合起来才是"双向"。
    """

    busy = _live_busy_timeout_ms()
    with _configured(write_busy_timeout_ms=busy, write_deadline_ms=busy + 1):
        verdict = WD.configuration_verdict()
        report = WD.assert_configuration_reachable()
    assert verdict["verdict"] == WD.CONFIG_OK, verdict
    assert report["per_scenario"][WD.SCENARIO_AGENT_COMMANDS]["deadline_ms"] == busy + 1
    assert report["per_scenario"][WD.SCENARIO_AGENT_COMMANDS]["delta_ms"] == 1


def test_G4_the_lock_wait_is_written_in_exactly_one_place() -> None:
    """G4 判据2 / 0281 T-c 反例：常量抄两份（`DEADLINE_CONSTANT_NOT_SINGLE_SOURCE`）必须当场红。

    扫的是产品源：`app/**` 里不许再出现第二处内联的锁等待数字。
    """

    offenders: dict[str, list[int]] = {}
    others: dict[str, list[str]] = {}
    for path in sorted((BACKEND_ROOT / "app").rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(needle in line for needle in ("busy_timeout=30000", "busy_timeout = 30000",
                                                 '"timeout": 30')):
                offenders.setdefault(str(path.relative_to(BACKEND_ROOT)), []).append(i)
            elif "busy_timeout" in line:
                others.setdefault(str(path.relative_to(BACKEND_ROOT)), []).append(line.strip()[:58])
    assert offenders == {}, f"a second inline copy of the request-path lock wait is back: {offenders}"
    # 其余提到 busy_timeout 的地方是 worker 自己的短等待（500/1500/200 ms），射程不同，这里点名不吞掉
    assert sorted(Path(k).name for k in others) == [
        "catalog_hydration_operations.py", "config.py", "session.py", "stay_generation_operations.py",
        "write_deadline.py"], {k: v for k, v in others.items()}
    session_src = (BACKEND_ROOT / "app/db/session.py").read_text(encoding="utf-8")
    assert "from app.db.write_deadline import busy_timeout_ms" in session_src
    assert 'timeout": write_busy_timeout_seconds()' in session_src, "session.py must ask the module"
    assert "PRAGMA busy_timeout={write_busy_timeout_ms()}" in session_src, "same source for the PRAGMA"


# ------------------------------------------------------------------- "按场景可配"必须是真的
def test_G5_a_scenario_override_moves_one_route_and_names_every_other_case() -> None:
    """G5 判据1 可配：覆盖只动被点名的那一格；未知场景与畸形值都要具名，不许静默回落。"""

    busy = WD.busy_timeout_ms()
    raised = busy * 3
    with _configured(write_deadline_scenarios=f"{WD.SCENARIO_PLANS_GENERATE}:{raised}"):
        assert WD.deadline_ms(WD.SCENARIO_PLANS_GENERATE) == raised
        assert WD.deadline_source(WD.SCENARIO_PLANS_GENERATE) == "scenario_override"
        assert WD.deadline_ms(WD.SCENARIO_AGENT_COMMANDS) == int(settings.write_deadline_ms)
        assert WD.deadline_source(WD.SCENARIO_AGENT_COMMANDS) == "default"
        assert WD.scenario_verdict("plans.not_a_route")["verdict"] == WD.CONFIG_SCENARIO_UNKNOWN
        assert WD.configuration_verdict()["verdict"] == WD.CONFIG_OK
    with _configured(write_deadline_scenarios=f"{WD.SCENARIO_PLANS_GENERATE}:abc"):
        with pytest.raises(ValueError) as caught:
            WD.deadline_ms(WD.SCENARIO_PLANS_GENERATE)
        assert WD.CONFIG_OVERRIDE_MALFORMED in str(caught.value)
        # 同一枚畸形值经"门"必须变成**具名的配置判级**，而不是让进程在 import 上崩
        crashed = WD.configuration_verdict()
        assert crashed["verdict"] == WD.CONFIG_OVERRIDE_MALFORMED, crashed
        assert crashed["per_scenario"][WD.SCENARIO_PLANS_GENERATE]["verdict"] == WD.CONFIG_OVERRIDE_MALFORMED
        with pytest.raises(WD.WriteDeadlineConfigurationError) as door:
            WD.assert_configuration_reachable()
        assert WD.CONFIG_OVERRIDE_MALFORMED in str(door.value)
    with _configured(write_deadline_ms=busy + 5_000):
        assert WD.effective_busy_timeout_ms(WD.SCENARIO_PLANS_GENERATE) == busy
    with _configured(write_deadline_ms=busy // 3):
        assert WD.effective_busy_timeout_ms(WD.SCENARIO_PLANS_GENERATE) == busy // 3
    assert WD.CONFIG_NOT_POSITIVE in (WD.configuration_verdict("x")["per_scenario"]["x"]["verdict"],
                                      WD.CONFIG_NOT_POSITIVE)


# ------------------------------------------------------------------- 两条分支，跑在真锁上
def _lock_error_inside_attempt(deadline_ms: int, spent_ms: int = 0):
    """在受期限的一页里真去撞那把锁；返回（异常, 本格读数）。

    `spent_ms` 是把这块表的起点往回拨：G6 要的是"预算已经用完了"这一**前件**，
    等真 999 秒不是判据（Q25：时长只当读数），所以前件直接构造，只有锁是真的。
    """

    budget = WD.WriteBudget(scenario=WD.SCENARIO_AGENT_COMMANDS, deadline_ms=deadline_ms)
    budget.started_monotonic -= spent_ms / 1000
    WD._CURRENT.set(budget)
    session = SessionLocal()
    try:
        session.execute(text("PRAGMA busy_timeout=0"))
        try:
            with WD.write_attempt(WD.SCENARIO_AGENT_COMMANDS):
                session.execute(text(f"INSERT INTO {PROBE_TABLE} VALUES (3)"))
                session.commit()
        except BaseException as exc:  # noqa: BLE001  —— 两格各自判它是什么
            return exc, budget
        return None, budget
    finally:
        session.close()
        WD.reset_request_budget()


def test_G6_lost_lock_after_the_budget_answers_under_its_own_name(held_lock) -> None:
    """G6 判据2 行为半：真锁 + 已超预算 ⇒ 必须换成具名异常，并带上两个数字与服务器原话。

    反件：把 `write_attempt` 里的转换删掉（或让 `expired()` 永远 False）⇒ 这一格必红。
    """

    exc, budget = _lock_error_inside_attempt(1, spent_ms=999_000)
    assert isinstance(exc, WD.WriteDeadlineExceeded), repr(exc)
    assert exc.code == CODE_SENT and exc.scenario == WD.SCENARIO_AGENT_COMMANDS
    assert exc.deadline_ms == 1 and exc.busy_timeout_ms == WD.busy_timeout_ms()
    assert any(phrase in exc.cause.casefold() for phrase in LOCK_SENTENCES), exc.cause
    assert exc.details()["elapsed_ms"] >= budget.deadline_ms
    assert budget.waits_lost == 1 and budget.clean_attempts == 0


def test_G7_lost_lock_inside_the_budget_stays_todays_error(held_lock) -> None:
    """G7 不越界：预算没用完就**不许**改名 —— 调用方看到的仍是原样的 OperationalError。

    与 G6 只差一个变量（deadline_ms），所以这两格合起来才叫"只有超期才具名"。
    """

    exc, budget = _lock_error_inside_attempt(WD.busy_timeout_ms() * 10, spent_ms=0)
    assert isinstance(exc, OperationalError) and not isinstance(exc, WD.WriteDeadlineExceeded), repr(exc)
    assert any(phrase in str(exc).casefold() for phrase in LOCK_SENTENCES), str(exc)
    assert budget.waits_lost == 1 and budget.clean_attempts == 0


def test_G8_the_answer_is_registered_and_carries_the_servers_own_words() -> None:
    """G8 判据2 表达半：0280 只允许屏幕转述服务端说过的话 ⇒ 这句话必须由服务端产生且可核对。"""

    assert any(key is WD.WriteDeadlineExceeded for key in app.exception_handlers), \
        "the envelope exists but nothing routes to it"
    exc = WD.WriteDeadlineExceeded(WD.SCENARIO_PLANS_GENERATE, 46_000, 45_000,
                                   "database is locked")
    response = app.exception_handlers[WD.WriteDeadlineExceeded](None, exc)
    assert response.status_code == 503, response.status_code
    err = json.loads(response.body.decode())["error"]
    assert err["code"] == CODE_SENT
    assert err["message"] == str(exc)
    assert "46000" in err["message"] and "45000" in err["message"], err["message"]
    assert err["details"]["deadline_ms"] == 45_000
    assert err["details"]["busy_timeout_ms"] == WD.busy_timeout_ms()
    assert err["message_key"] == f"error.{CODE_SENT.lower()}"
    assert err["trace_id"]


def test_G9_the_normal_write_path_is_unchanged() -> None:
    """G9 正控：没有争锁时两态都该绿 —— 它不咬人，它防的是我把成功路径改坏了。"""

    jid = _journey_id()
    command = client.post(f"/api/v1/journeys/{jid}/agent/commands",
                          json={"command": "我护照丢了，请告诉我怎么办。"})
    assert command.status_code == 200, command.text
    plans = client.post(f"/api/v1/journeys/{jid}/plans/generate",
                        json={"candidate_count": 3},
                        headers={"Idempotency-Key": f"ord0296-{uuid.uuid4()}"})
    assert plans.status_code in (201, 422), plans.text
    with SessionLocal() as session:
        missions = session.scalar(select(func.count()).select_from(JourneyMission))
        rows = session.execute(text(
            "select count(*) from journey_missions where journey_id = :jid"
        ), {"jid": jid}).scalar_one()
    assert missions is not None and rows >= 1, (missions, rows)
    again = client.post(f"/api/v1/journeys/{jid}/agent/commands",
                        json={"command": "帮我改一下明天的安排。"})
    assert again.status_code == 200, again.text
    with SessionLocal() as session:
        rows_after = session.execute(text(
            "select count(*) from journey_missions where journey_id = :jid"
        ), {"jid": jid}).scalar_one()
    assert rows_after == rows + 1, (rows, rows_after)


def test_G10_the_budget_belongs_to_one_request_not_one_connection() -> None:
    """G10 判据1 归属：每次进路由换新钟；换场景也要换新钟 —— 否则上一条请求替这一条超时。"""

    first = WD.start_request_budget(WD.SCENARIO_AGENT_COMMANDS)
    first.started_monotonic -= 10.0
    again = WD.start_request_budget(WD.SCENARIO_AGENT_COMMANDS)
    assert again is not first and not again.expired(), (first.elapsed_ms, again.elapsed_ms)
    assert WD.current_budget(WD.SCENARIO_AGENT_COMMANDS) is again
    other = WD.current_budget(WD.SCENARIO_PLANS_GENERATE)
    assert other is not again and other.scenario == WD.SCENARIO_PLANS_GENERATE
    assert other.deadline_ms == WD.deadline_ms(WD.SCENARIO_PLANS_GENERATE)
    WD.reset_request_budget()
    assert WD.current_budget(WD.SCENARIO_AGENT_COMMANDS) is not again


def test_G11_importing_the_app_is_what_arms_the_door() -> None:
    """G11（包工头字面点名的那一枚补上的）：门必须挂在真正的启动路径上，而不是只挂在函数上。

    前十格量不到这件事：G2/G3 直接叫 `assert_configuration_reachable()`，所以把 `app/main.py:427`
    那句调用删掉时它们照绿——M11 把我门的空洞照了出来。这一格走**子进程真 import**：
    期限压到锁等待之下 ⇒ 导入必须失败且说得出档名；抬高一毫秒 ⇒ 必须导入成功（双向）。
    子进程带自己的临时库，不吃套件里别人留下的状态。
    """

    busy = WD.busy_timeout_ms()
    outcomes = []
    for probe, must_refuse in ((busy - 1, True), (busy + 1, False)):
        own = Path(tempfile.mkdtemp(prefix="cnx_g11_")) / "gate.db"
        env = {**os.environ,
               "CITYNEXUS_WRITE_BUSY_TIMEOUT_MS": str(busy),
               "CITYNEXUS_WRITE_DEADLINE_MS": str(probe),
               "CITYNEXUS_DATABASE_URL": f"sqlite:///{own.as_posix()}"}
        done = subprocess.run([sys.executable, "-c", "import app.main"], cwd=str(BACKEND_ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=300)
        tail = (done.stderr or "") + (done.stdout or "")
        outcomes.append({"deadline_ms": probe, "busy_timeout_ms": busy, "rc": done.returncode,
                         "refused": done.returncode != 0,
                         "named_code": WD.CONFIG_NOT_ABOVE_BUSY in tail,
                         "named_error": "WriteDeadlineConfigurationError" in tail,
                         "tail_tail": tail.strip().splitlines()[-1][-140:] if tail.strip() else ""})
    refused, accepted = outcomes
    assert refused["deadline_ms"] == busy - 1 and accepted["deadline_ms"] == busy + 1, outcomes
    assert refused["refused"] and refused["named_code"] and refused["named_error"], refused
    assert not accepted["refused"], accepted
