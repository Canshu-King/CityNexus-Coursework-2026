"""ORD-0272 · `POST /journeys/{id}/plans/generate` 的续跑身份：一次生成只属于一次 mission。

八格（G1–G7 为批准的判据，G8 见下），每格只问一条问题（`owner=` 写在名字与断言里）；一臂红多格 ≠
多份证据，所以每格还写明**它不该被谁扰动**。判一律落在**行**与**投影字段**上，不落在状态码上
（0262 那一族的病就在这）。
批准的判据是 G1–G7；`G8` 是本车道自加的一格，理由写在它自己的 docstring 里（产码多写了归属检查）。

形制说明（先看这条再引用任何绿）：
  · 本文件全部**单进程**（TestClient 与测试同进程）⇒ "双进程/两设备"用的是**不同连接 + 不带客户端键**
    的等价形状，真正的跨进程复现归 verifier（HJ-P3）与跨进程门，不在此宣布。
  · 前提：外部 AI 与 Hermes 关着（否则第二腿的行为不在我想量的那一发上）——开着就**红**，不 skip。
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import Journey, Place
from app.db.session import SessionLocal, ensure_schema
from app.main import app
from app.services import seed_demo

client = TestClient(app, raise_server_exceptions=False)
DB_PATH = Path(settings.database_url.replace("sqlite:///", "", 1))
DEMO_CITY = "CNX-DEMO"
CMD_A = "我护照丢了，请告诉我怎么办。"
#: 让 `generate_plans` 必然走拒绝那一支的输入（schema 要求 place_ids 至少两枚，一枚会先撞 422 校验）
BAD_PLACES = ["ord0272-no-such-place-1", "ord0272-no-such-place-2"]
#: 门内自带的字面副本（不 import 产品常量：那样它永远等于自己，测不到漂移）
SENT_DIFFERENT_REQUEST = "same idempotency key used for a different request"
SENT_INCOMPLETE_RECEIPT = "agent idempotency record is incomplete"


@pytest.fixture(autouse=True)
def _offline_precondition() -> None:
    """外部 AI / Hermes 就绪 ⇒ durable 读数不成立（远端 run 不在提交边界内）：红，不是 skip。"""

    from app.integrations.hermes.native_client import native_hermes_status

    assert not settings.ai_api_key, "SETUP：这台机器开着外部 AI ⇒ 本门量不到我想量的那一发"
    assert not native_hermes_status().get("ready"), (
        "SETUP：Hermes native 就绪 ⇒ 外部 run 落在提交边界之外，durable 读数不成立")


#: 舞台（整个模块一次，取自产品自己灌出来的那趟演示旅程）：见 `_demo_stage` 的理由
_STAGE: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _demo_stage() -> None:
    """前置：本门每一发都要**真生成**出候选，所以冻结舞台（目录 + 旅程窗口）必须先在场。

    为什么不自己写日期和预算：`generate_plans` 对"这一城 authored 的那几个日序模板"才可行，
    自造窗口会得到 `No feasible plan candidates` —— 那是 fixture 在假红，不是本单的契约破了。
    所以窗口/预算/模式**从产品自己 seed 出来的旅程上抄**，抄不到就红并说明去哪查。

    为什么不是 `demo/reset`：那扇门删 52 张表（`services.reset_all`），而且是 ~140 个模块共用的
    舞台门；本单纪律是它 0 次。改走产品自己的 in-process seed（与我已被接受的 ORD-0236 /
    无障碍证据门同形），并且**只在缺东西时**才触发——整套件跑时更早的模块通常已经灌好。
    """

    def _read() -> tuple[int, object]:
        with SessionLocal() as session:
            places = int(session.scalar(
                select(func.count()).select_from(Place)
                .where(Place.city_code == DEMO_CITY)) or 0)
            journey = session.scalars(
                select(Journey).where(Journey.city_code == DEMO_CITY)
                .order_by(Journey.created_at)).first()
            return places, journey

    ensure_schema()
    places, journey = _read()
    if places == 0 or journey is None:
        with SessionLocal() as session:
            seed_demo(session)
        places, journey = _read()
    assert places, (
        f"SETUP：{DEMO_CITY} 目录里一个地点都没有 ⇒ 七格会一起变成『没东西可生成』的红，"
        "那不是本单要判的问题；先查 seed 是否被禁用")
    assert journey is not None, (
        f"SETUP：{DEMO_CITY} 没有可抄的演示旅程 ⇒ 本门拒绝自造窗口（那只会造出假红）")
    _STAGE.clear()
    _STAGE.update({
        "city_code": journey.city_code,
        "timezone": journey.timezone,
        "start_at": journey.start_at,
        "end_at": journey.end_at,
        "planning_mode": journey.planning_mode,
        "budget": {"amount_minor": journey.budget_amount_minor,
                   "currency": journey.budget_currency},
    })


def _setup_journey() -> str:
    traveler = client.post("/api/v1/travelers",
                           json={"display_name": "ord0272", "timezone": "Asia/Shanghai"})
    assert traveler.status_code == 201, traveler.text
    journey = client.post("/api/v1/journeys", json={
        "traveler_id": traveler.json()["id"], "title": f"ord0272-{uuid.uuid4().hex[:8]}",
        **_STAGE})
    assert journey.status_code == 201, journey.text
    return str(journey.json()["id"])


def _mission_id(jid: str) -> str:
    """第二腿的上一腿：真走一次 agent command，拿到**真** mission id（不编造）。"""

    response = client.post(f"/api/v1/journeys/{jid}/agent/commands",
                           json={"command": CMD_A, "requested_autonomy": "PLAN_ONLY"})
    assert response.status_code == 200, response.text
    mission = (response.json().get("mission") or {})
    assert mission.get("id"), f"没有 mission 就没有续跑身份可绑：{response.text[:200]}"
    return str(mission["id"])


def _generate(jid: str, *, mission_id: str | None = None, key: str | None = None,
              candidate_count: int = 3, place_ids: list[str] | None = None):
    body: dict = {"candidate_count": candidate_count}
    if mission_id is not None:
        body["mission_id"] = mission_id
    if place_ids is not None:
        body["place_ids"] = place_ids
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(f"/api/v1/journeys/{jid}/plans/generate", json=body, headers=headers)


def _plan_rows(jid: str) -> list[str]:
    with contextlib.closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT id FROM plan_versions WHERE journey_id=? ORDER BY created_at, id", (jid,))]


def _durable(sql: str, args: tuple = ()) -> list[tuple]:
    """门外第二条 sqlite3 直连：只承认"已提交到盘上"的东西。"""

    assert DB_PATH.is_file(), f"SETUP：读不到 {DB_PATH} ⇒ durable 读数无从判定，不许当通过"
    with contextlib.closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        return list(conn.execute(sql, args))


def _durable_write(sql: str, args: tuple) -> None:
    """把前置状态**真**落盘：`closing()` 不 commit ⇒ 前一版这里插的占位行被回滚，
    于是这一格读到的"未回填态"其实是"根本没这行"——那是空对空的绿。"""

    with contextlib.closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        conn.execute(sql, args)
        conn.commit()


def _receipt(key: str) -> tuple[str, int | None, dict | None]:
    """三态而不是两态：`absent` / `unfilled`（占了没跑完）/ `filled`。合成两态就是把"两态可区分"这格读空。"""

    rows = _durable("SELECT response_status, response_json FROM idempotency_keys WHERE key = ?",
                    (key,))
    assert len(rows) <= 1, f"SETUP：一把键应有至多一行回执，实际 {len(rows)}"
    if not rows:
        return "absent", None, None
    status, body = rows[0]
    return ("filled" if body else "unfilled"), status, (json.loads(body) if body else None)


def _ids_of(payload) -> list[str]:
    return [str(item["id"]) for item in (payload if isinstance(payload, list) else [])]


def _message_of(response) -> str:
    return str(response.json()["error"]["message"])


def _feed_facts(jid: str, mission_id: str) -> dict:
    """owner 可读回：只看既有 feed 投影里那条链接事件的 `facts`。"""

    feed = client.get(f"/api/v1/journeys/{jid}/agent-feed", params={"limit": 200})
    assert feed.status_code == 200, feed.text
    for event in feed.json()["events"]:
        facts = event.get("facts") or {}
        if facts.get("continuation_id") == f"cont:{mission_id}":
            return facts
    return {}


# ---------------------------------------------------------------------------- G1
def test_G1_one_mission_generates_once_and_a_plain_resend_replays_the_same_plans() -> None:
    """owner=G1：同一 mission 重复请求 ⇒ 库里**不多一批**，两次响应的方案 id 逐位相同。"""

    jid = _setup_journey()
    mission = _mission_id(jid)
    before = _plan_rows(jid)
    first = _generate(jid, mission_id=mission)
    assert first.status_code == 201, first.text
    after_first = _plan_rows(jid)
    assert len(after_first) > len(before), f"G1：第一发就该产出候选，实际 {before} → {after_first}"

    again = _generate(jid, mission_id=mission)
    assert again.status_code == 201, again.text
    assert _ids_of(again.json()) == _ids_of(first.json()), (
        f"G1：重发必须回放**同一批**方案，拿到 {_ids_of(first.json())} vs {_ids_of(again.json())}")
    assert _plan_rows(jid) == after_first, (
        f"G1：重发不能再生一批，前 {after_first} → 后 {_plan_rows(jid)}")
    state, status, body = _receipt(f"plans.generate:cont:{mission}")
    assert (state, status) == ("filled", 201) and body, (
        f"G1：回执必须已填成完成态，实际 state={state} status={status}")


# ---------------------------------------------------------------------------- G2
def test_G2_the_two_states_of_one_claim_answer_differently_and_neither_reruns_generation(
        monkeypatch) -> None:
    """owner=G2：同一把键的"占了没跑完"与"已回填"必须给出**不同**的答案，且都不许重跑生成。

    形制（避免空对空）：先用**真**请求得到回执，再把盘上那一行抹成 NULL（生产里就是崩在
    Phase B 之前），此时第二发必须具名 fail-closed；然后把**自己抓回来的**回执原样写回，
    同一把键又必须回放同一批方案。两态若不可区分，这两条断言必红其中一条。
    """

    import app.main as product

    reached: list[str] = []
    real_generate = product.generate_plans

    def spy(*args, **kwargs):
        reached.append("generate_plans was reached")
        return real_generate(*args, **kwargs)

    monkeypatch.setattr("app.main.generate_plans", spy)

    jid = _setup_journey()
    mission = _mission_id(jid)
    key = f"plans.generate:cont:{mission}"
    first = _generate(jid, mission_id=mission)
    assert first.status_code == 201, f"SETUP：门 A 需要一个真回执当底子，实际 {first.text[:180]}"
    state, status, body = _receipt(key)
    assert (state, status) == ("filled", 201), f"SETUP：回执没落盘，读到 {state}"
    rows = _plan_rows(jid)
    _durable_write("UPDATE idempotency_keys SET response_status=NULL, response_json=NULL "
                   "WHERE key = ?", (key,))
    assert _receipt(key)[0] == "unfilled", "SETUP：抹成未回填没落盘 ⇒ 这一格会读空"
    reached.clear()

    abandoned = _generate(jid, mission_id=mission)
    assert abandoned.status_code == 409, (
        f"G2：占了没跑完的键必须 fail closed，实际 {abandoned.status_code} "
        f"{abandoned.text[:180]}")
    assert _message_of(abandoned) == SENT_INCOMPLETE_RECEIPT, (
        f"G2：未回填态必须说现成那句具名 409，实际 {_message_of(abandoned)[:180]!r}")
    assert reached == [], (
        f"G2：未回填态却让第二发重跑了生成 ⇒ 会把既有副作用再做一遍：{reached}")
    assert _plan_rows(jid) == rows, "G2：被拒的一发不许先产出候选再报错"
    assert _receipt(key)[0] == "unfilled", "G2：拒绝方不许把自己没跑完的事实写回别人的占位"

    _durable_write("UPDATE idempotency_keys SET response_status=?, response_json=? WHERE key = ?",
                   (status, json.dumps(body, sort_keys=True), key))
    answered = _generate(jid, mission_id=mission)
    assert answered.status_code == 201, (
        f"G2：同一把键回填之后必须答得出（与未回填态区分不开）：{answered.text[:180]}")
    assert _ids_of(answered.json()) == _ids_of(first.json()), (
        f"G2：回放必须指回那一批，拿到 {_ids_of(first.json())} vs {_ids_of(answered.json())}")
    assert _plan_rows(jid) == rows, f"G2：回放不能再生一批：{rows} → {_plan_rows(jid)}"
    assert reached == [], f"G2：回填态也不该重跑生成：{reached}"


# ---------------------------------------------------------------------------- G3
def test_G3_second_device_with_no_client_header_still_gets_the_same_answer() -> None:
    """owner=G3：清 storage / 第二设备 = **不带任何客户端键**，只带 mission ⇒ 仍回放同一结果。"""

    jid = _setup_journey()
    mission = _mission_id(jid)
    first = _generate(jid, mission_id=mission, key=f"tab1-{uuid.uuid4().hex[:12]}")
    assert first.status_code == 201, first.text
    rows = _plan_rows(jid)

    second_device = _generate(jid, mission_id=mission)          # 没有 Idempotency-Key
    assert second_device.status_code == 201, second_device.text
    assert _ids_of(second_device.json()) == _ids_of(first.json()), (
        "G3 **键不能是客户端造的**：换设备（无 header）必须从 mission 派生出同一把键并回放同一批方案，"
        f"拿到 {_ids_of(first.json())} vs {_ids_of(second_device.json())}")
    assert _plan_rows(jid) == rows, f"G3：第二设备不能再生一批，{rows} → {_plan_rows(jid)}"


# ---------------------------------------------------------------------------- G4
def test_G4_two_identities_stay_two_doors_and_both_use_the_canonical_409() -> None:
    """owner=G4：同 mission 换语义、同 header 键换语义 ⇒ 都是现成那句 mismatch，逐字 `==`，都不留新候选。

    两扇门分别钉"两把键不得合并"：mission 侧丢一个键，业务语义就没法区分；header 侧丢一个键，
    传输层重发就没法区分。只量其中一侧的判据会让"合并成一个键"这种实现活下来（K2 的载体在此）。
    """

    jid = _setup_journey()
    mission = _mission_id(jid)
    assert _generate(jid, mission_id=mission, candidate_count=3).status_code == 201
    rows = _plan_rows(jid)

    conflict = _generate(jid, mission_id=mission, candidate_count=5)
    assert conflict.status_code == 409, f"G4：换意图必须拒，实际 {conflict.status_code}"
    assert _message_of(conflict) == SENT_DIFFERENT_REQUEST, (
        f"G4：必须逐字沿用现成那句（不新造词），实际 {_message_of(conflict)[:180]!r}")
    assert _plan_rows(jid) == rows, "G4：被拒的一发不许先把候选写进库"

    # 第二扇门：只有 header 键、没有 mission —— 传输层那一侧必须同样能区分
    plain = _setup_journey()
    key = f"g4-{uuid.uuid4().hex[:12]}"
    first = _generate(plain, key=key, candidate_count=3)
    assert first.status_code == 201, first.text
    plain_rows = _plan_rows(plain)
    resent = _generate(plain, key=key, candidate_count=3)
    assert resent.status_code == 201 and _ids_of(resent.json()) == _ids_of(first.json()), (
        f"G4：同一把传输键、同一份正文必须回放那一批，拿到 {_ids_of(first.json())} vs "
        f"{_ids_of(resent.json())} ⇒ 键没参与身份")
    swapped = _generate(plain, key=key, candidate_count=5)
    assert swapped.status_code == 409, (
        f"G4：同一把键换正文必须拒，实际 {swapped.status_code} {swapped.text[:160]}")
    assert _message_of(swapped) == SENT_DIFFERENT_REQUEST, (
        f"G4：header 那一侧也得说现成那句，实际 {_message_of(swapped)[:180]!r}")
    assert _plan_rows(plain) == plain_rows, (
        f"G4：键侧两发都不许多起一批：{plain_rows} → {_plan_rows(plain)}")


# ---------------------------------------------------------------------------- G5
def test_G5_requests_with_neither_mission_nor_header_keep_todays_shape() -> None:
    """owner=G5：两个都不带 ⇒ 今天的形状一字不改（每发各自一批候选，键不参与）。"""

    jid = _setup_journey()
    keys_before = {r[0] for r in _durable(
        "SELECT key FROM idempotency_keys WHERE operation = 'plans.generate'")}
    one = _generate(jid)
    two = _generate(jid)
    assert one.status_code == 201 and two.status_code == 201, (one.text[:160], two.text[:160])
    assert _ids_of(one.json()) and _ids_of(one.json()) != _ids_of(two.json()), (
        "G5：不带身份就该保持旧语义（各起一批候选）；现在它们合并了 ⇒ 默认路径被我改了")
    keys_after = {r[0] for r in _durable(
        "SELECT key FROM idempotency_keys WHERE operation = 'plans.generate'")}
    assert keys_after == keys_before, (
        f"G5：无键无 mission 时一发都不该留下回执行，多出 {sorted(keys_after - keys_before)}")
    # 两条渲染路径必须说同一套字段：否则"复用现成信封"只是口号
    three = _generate(jid, mission_id=_mission_id(jid))
    assert three.status_code == 201, three.text
    assert set(map(str, one.json()[0].keys())) == set(map(str, three.json()[0].keys())), (
        "G5：带 continuation 的那一发字段词汇变了 ⇒ 我给同一件事造了第二种正文")


# ---------------------------------------------------------------------------- G6
def test_G6_no_second_failure_vocabulary_is_introduced() -> None:
    """owner=G6：本单不新增错误码；失败面仍是现成那一族（0262 的病就是客户端自造 HTTP 顶掉服务器原话）。"""

    main_py = (Path(__file__).resolve().parents[1] / "app/main.py").read_text(encoding="utf-8")
    introduced = [code for code in re_codes(main_py)
                  if code not in re_codes_at_base()]
    assert introduced == [], (
        f"G6：本单引入了新错误码 {introduced} ⇒ 第二套失败表达（仲裁禁止）")
    envelope = client.get("/api/v1/journeys/" + str(uuid.uuid4()) + "/plans").json()
    assert set(envelope["error"].keys()) >= {"code", "message", "message_key", "trace_id"}, (
        f"G6：现成信封的键形变了，客户端就没法只靠它：{sorted(envelope.get('error', {}))}")

    # 活体那一半：同一件不可行的事走两条路径，必须说**同一句话**（静态正则证不了这个）
    jid = _setup_journey()
    mission = _mission_id(jid)
    rows_before = _plan_rows(jid)
    unowned = _generate(jid, place_ids=list(BAD_PLACES))
    owned = _generate(jid, mission_id=mission, place_ids=list(BAD_PLACES))
    assert unowned.status_code == owned.status_code == 422, (
        f"G6：两条路径的状态码都该是现成那个 422，实际 {unowned.status_code}/{owned.status_code}")
    assert _message_of(owned) == _message_of(unowned), (
        f"G6：continuation 路径改写了服务器原话：{owned.json()['error']['message'][:160]!r} vs "
        f"{unowned.json()['error']['message'][:160]!r}")
    named = {side: payload.json()["error"].get("reason_code", "<缺键>")
             for side, payload in (("无身份", unowned), ("continuation", owned))}
    assert named["无身份"] == named["continuation"] == "PLACES_NOT_IN_CATALOG", (
        f"G6：具名理由漂移了 {named}（`reason_code` 住在 error 里面，实测）")
    assert set(owned.json()["error"].keys()) == set(unowned.json()["error"].keys()), (
        "G6：两条路径的信封键形不一样 ⇒ 实际是两套渲染")
    again = _generate(jid, mission_id=mission, place_ids=list(BAD_PLACES))
    assert again.status_code == 422 and _message_of(again) == _message_of(owned), (
        f"G6：被拒的那一发也得可回放，实际 {again.status_code} {again.text[:160]}")
    assert _plan_rows(jid) == rows_before, (
        f"G6：不可行的请求不该留下候选：{rows_before} → {_plan_rows(jid)}")
    state, status, _ = _receipt(f"plans.generate:cont:{mission}")
    assert (state, status) == ("filled", 422), (
        f"G6：失败回执必须是已回填的 422（不是未回填、也不是 201），实际 {state}/{status}")


def re_codes(text: str) -> set[str]:
    """`error("<CODE>", ...)` 里出现过的具名码（同一把尺量改前改后，才叫不新增）。"""

    import re

    return set(re.findall(r'error\(\s*"([A-Z][A-Z0-9_]{3,})"', text))


def re_codes_at_base() -> set[str]:
    """权威基线（c79e2b4）上同一把尺的读数 —— 从 git 现取，不抄我的记忆。"""

    import subprocess

    out = subprocess.run(["git", "-C", r"D:/moodle/_qoder_integration_20260919", "show",
                          "c79e2b4:backend/app/main.py"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert out.returncode == 0, f"SETUP：取不到基线 main.py：{out.stderr[:160]}"
    return re_codes(out.stdout)


# ---------------------------------------------------------------------------- G7
def test_G7_the_feed_projection_names_the_mission_and_the_plans_it_produced() -> None:
    """owner=G7（(甲) 口径）：只证"字段存在且与 mission 稳定关联"；feed 升序截断另单具名。"""

    jid = _setup_journey()
    mission = _mission_id(jid)
    generated = _generate(jid, mission_id=mission)
    assert generated.status_code == 201, generated.text
    produced = _ids_of(generated.json())

    facts = _feed_facts(jid, mission)
    assert facts, (
        "G7：feed 投影里读不到这条 continuation 事件 ⇒ 不能用「分页截断」解释（本 journey 事件很少），"
        "那就是没写进 after_json")
    assert facts.get("mission_id") == mission, f"G7：mission 关联错了：{facts.get('mission_id')}"
    assert facts.get("continuation_id") == f"cont:{mission}", f"G7：续跑身份不稳定：{facts}"
    assert facts.get("plan_version_ids") == produced, (
        f"G7：同一条事件必须指回**这批**方案（不许再靠同秒对齐）：{facts.get('plan_version_ids')} "
        f"vs {produced}")
    rows = set(_plan_rows(jid))
    assert set(produced) <= rows, "G7：投影说的方案必须在盘上真存在（防空断言）"


# ---------------------------------------------------------------------------- G8 (本车道自加)
def test_G8_a_mission_that_is_not_this_journeys_is_not_an_identity() -> None:
    """owner=G8：**本车道自加的一格**（批准的判据是 G1–G7；因为产码多写了归属检查，就得给它载体）。

    续跑身份若接受编造的 id 或别家旅程的 mission，盘上会多出一条会说谎的
    `plans.continuation.linked` —— 而 Q12 要 owner 读的正是它。两例都必须**在占位之前**被拒。
    """

    jid = _setup_journey()
    foreign_jid = _setup_journey()
    foreign_mission = _mission_id(foreign_jid)
    rows_before = _plan_rows(jid)
    keys_before = {r[0] for r in _durable(
        "SELECT key FROM idempotency_keys WHERE operation = 'plans.generate'")}

    for bad, label in ((str(uuid.uuid4()), "编造"), (foreign_mission, "别家旅程的")):
        refused = _generate(jid, mission_id=bad)
        assert refused.status_code == 404, f"G8：{label} mission 必须按不存在答，实际 {refused.text[:160]}"
        assert _message_of(refused) == f"JourneyMission {bad} was not found", (
            f"G8：{label}那一例没说现成那句 404，实际 {_message_of(refused)[:160]!r}")
    assert _plan_rows(jid) == rows_before, (
        f"G8：被拒的两发一行候选都不许多：{rows_before} → {_plan_rows(jid)}")
    keys_after = {r[0] for r in _durable(
        "SELECT key FROM idempotency_keys WHERE operation = 'plans.generate'")}
    assert keys_after == keys_before, (
        f"G8：身份不成立就不该先占位（占了即成别人的锁），多出 {sorted(keys_after - keys_before)}")
