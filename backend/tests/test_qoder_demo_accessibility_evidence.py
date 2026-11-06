"""ORD-0219 ②③④ · 封存演示的无障碍证据生产者，走正常 Place -> evidence -> feasibility 消费链。

为什么要有这个文件：解析器补上 severity 之后（`5730004`），门确实会读 `stroller_accessible` /
`luggage_accessible`，但**没有任何生产者写过这两个键**——实测 `seed_demo` 灌出的 16 个地点里带无障碍
证据的 = 0/16（HARNESS `EXP-008` 追加，我在本树自己的临时库里复算同为 0）。证据永远读到 None，
就既不挡也不放。本文件把生产者钉进产品路径，并要求**删除/翻转 demo 证据字段 ⇒ 这里有节点变红**
（仲裁 `5778570154` 第 5 步）。

冻结边界（仲裁原文）在下面的节点里逐条可测：
* 只覆盖 sealed/demo-owned 地点（`CNX-DEMO`）；非封存城市 0 命中；
* provenance 由服务端代码盖章（`source_kind=DEMO`），fixture 里的任何字段都无法把它变成 provider truth；
* 消费走正常读取路径（`app.replanning.feasibility` 认的那个 metadata 形状），feasibility 里没有 demo 特例；
* 缺证据 = UNKNOWN，不许默认成 accessible。

期望值一律**算出来**：冲突地点来自 fixture 与 DB 行的实际读数，候选数来自 `_templates()` 的实际内容，
不写死数字，也不写死 "PASS"。
"""

import ast
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.capabilities import (
    accessibility_state,
    evaluate_accessibility_requirement,
    extract_capabilities_from_text,
)
from app.db.models import Activity, Journey, Place
from app.db.session import SessionLocal
from app.integrations.mock.providers import MockCityDataProvider
from app.main import app
from app.replanning.feasibility import _accessibility_metadata
import app.services as services_module
from app.services import _demo_accessibility_evidence, _templates, seed_demo

client = TestClient(app)

DEMO_CITY = "CNX-DEMO"
FACT_KEYS = ("stroller_accessible", "luggage_accessible")
HARD_SENTENCES = {
    "STROLLER": "我们带婴儿车，这是必须的。",
    "LARGE_LUGGAGE": "我们拉一个大箱子，这是必须的。",
}
NAMED_BLOCKER = {
    "STROLLER": "STROLLER_ACCESS_UNAVAILABLE",
    "LARGE_LUGGAGE": "LUGGAGE_ACCESS_UNAVAILABLE",
}


def _fixture_claims() -> dict[str, dict[str, bool]]:
    """What the deterministic demo fixture asserts, read from the fixture itself."""

    claims: dict[str, dict[str, bool]] = {}
    for item in MockCityDataProvider().places():
        claimed = item.get("accessibility") or {}
        facts = {
            key: claimed[key]
            for key in FACT_KEYS
            if isinstance(claimed.get(key), bool)
        }
        if facts:
            claims[item["id"]] = facts
    return claims


def _seed() -> tuple[str, str]:
    """Bootstrap through the product's own demo seed (never the shared reset route)."""

    with SessionLocal() as session:
        seed_demo(session)
        journey = session.execute(select(Journey)).scalars().first()
    assert journey is not None, "SETUP 失败：seed_demo 之后库里没有 journey"
    assert journey.city_code == DEMO_CITY, (
        f"SETUP：本门的舞台是封存演示城 {DEMO_CITY}，实际 {journey.city_code}"
    )
    return journey.id, journey.traveler_id


def _evidence(place_id: str) -> dict:
    with SessionLocal() as session:
        row = session.get(Place, place_id)
    assert row is not None, f"封存演示地点 {place_id} 不在库里"
    assert row.city_code == DEMO_CITY, f"{place_id} 不属于封存城市 {DEMO_CITY}"
    return _accessibility_metadata(row) or {}


def demo_city_place_ids() -> list[str]:
    """该城现在真的有哪几个地点（从库里读，不写死 id）。"""

    with SessionLocal() as session:
        rows = session.execute(
            select(Place.id).where(Place.city_code == DEMO_CITY)
        ).all()
    return sorted(str(row[0]) for row in rows)


def _plan_stops(plan_id: str) -> list[str]:
    with SessionLocal() as session:
        rows = (
            session.query(Activity)
            .filter(Activity.plan_version_id == plan_id)
            .order_by(Activity.sequence_no)
            .all()
        )
    return [str(row.place_id) for row in rows if row.place_id]


def _conflicting_stops(plan_id: str, fact_key: str) -> list[str]:
    """The stops this plan actually visits whose demo evidence says False."""

    return [
        place_id
        for place_id in _plan_stops(plan_id)
        if _evidence(place_id).get(fact_key) is False
    ]


def _force_both_facts_false(plan_id: str) -> str:
    """把该候选第一个有无障碍证据的地点在我自己的一次性库里改成两把都 False，并回读证明改动落地。"""

    target = next((place_id for place_id in _plan_stops(plan_id) if _evidence(place_id)), None)
    assert target, f"{plan_id} 的地点里没有任何一条无障碍证据可改"
    with SessionLocal() as session:
        row = session.get(Place, target)
        metadata = json.loads(row.metadata_json or "{}")
        evidence = dict(metadata.get("accessibility_evidence") or {})
        evidence.update({"stroller_accessible": False, "luggage_accessible": False})
        metadata["accessibility_evidence"] = evidence
        row.metadata_json = json.dumps(metadata, ensure_ascii=False)
        session.commit()
    read_back = _evidence(target)
    assert read_back.get("stroller_accessible") is False and read_back.get("luggage_accessible") is False, (
        f"改动没落地（回读 {read_back}）：那条双具名断言就成了空跑"
    )
    return target


def _fact_key_for(code: str) -> str:
    return {"STROLLER": "stroller_accessible", "LARGE_LUGGAGE": "luggage_accessible"}[code]


def _generate() -> list[str]:
    response = client.post("/api/v1/journeys/%s/plans/generate" % _JOURNEY, json={})
    assert response.status_code == 201, f"plans/generate 期望 201，实际 {response.status_code} {response.text[:200]}"
    body = response.json()
    entries = body if isinstance(body, list) else (body.get("plans") or [])
    return [str(entry.get("id") or entry.get("plan_version_id")) for entry in entries]


def _confirm_capability(code: str) -> list[dict]:
    """State the requirement the way the product hears it: natural language, then /confirm."""

    items = [
        {**dict(item), "confirmed": True}
        for item in extract_capabilities_from_text(HARD_SENTENCES[code], scope="TRAVELER")
    ]
    assert {item.get("code") for item in items} >= {code}, (
        f"解析器没把「{HARD_SENTENCES[code]}」读成 {code}，实际 {items}"
    )
    assert all(item.get("severity") == "HARD" for item in items), (
        f"硬性措辞被降级，实际 severity={[i.get('severity') for i in items]}（期望全部 HARD）"
    )
    response = client.post(
        "/api/v1/travelers/%s/capabilities/confirm" % _TRAVELER, json=items
    )
    assert response.status_code == 200, f"capabilities/confirm 实际 {response.status_code} {response.text[:200]}"
    return items


def _select(plan_id: str):
    return client.post("/api/v1/journeys/%s/plans/%s/select" % (_JOURNEY, plan_id), json={})


_JOURNEY, _TRAVELER = _seed()


# ------------------------------------------------------------------------------------------
# 生产者本身：证据落到库里、provenance 由代码盖章、且只落在封存城市
# ------------------------------------------------------------------------------------------
def test_demo_producer_lands_every_claimed_fact_with_demo_provenance():
    claims = _fixture_claims()
    assert claims, (
        "demo fixture 一个无障碍事实都没有声明 ⇒ 生产者无米下锅，这条门就是空的（先修 fixture）"
    )
    for place_id, facts in claims.items():
        evidence = _evidence(place_id)
        for key, value in facts.items():
            assert evidence.get(key) is value, (
                f"{place_id}.{key} 期望 fixture 声明的 {value}，库里读到 {evidence.get(key)!r}"
            )
        assert evidence.get("source_kind") == "DEMO", (
            f"{place_id} 的无障碍证据没有 DEMO 出处标记，实际 {evidence.get('source_kind')!r}"
        )
        assert "live" not in json.dumps(evidence).lower(), (
            f"{place_id} 的证据里出现了 live/provider 字样：{evidence}"
        )
        assert "evidence_status" not in evidence, (
            f"{place_id} 带着全局 evidence_status，会把 STEP_FREE 一族一起判掉：{evidence}"
        )


def test_demo_producer_reaches_exactly_the_places_the_fixture_claims():
    claims = set(_fixture_claims())
    with SessionLocal() as session:
        rows = session.query(Place).all()
    carrying = {
        row.id
        for row in rows
        if isinstance(json.loads(row.metadata_json or "{}").get("accessibility_evidence"), dict)
    }
    assert carrying == claims, (
        f"带证据的地点集合必须等于 fixture 声明的集合：库里 {sorted(carrying)} vs fixture {sorted(claims)}"
    )


def test_demo_evidence_stays_out_of_non_sealed_cities():
    with SessionLocal() as session:
        others = session.query(Place).filter(Place.city_code != DEMO_CITY).all()
    assert others, "库里没有非封存城市地点，这条负例就成了空断言"
    leaked = [
        row.id
        for row in others
        if "accessibility_evidence" in json.loads(row.metadata_json or "{}")
    ]
    assert not leaked, f"demo 无障碍证据泄进了非封存城市：{leaked}"


def test_missing_demo_evidence_is_unknown_and_never_implicit_access():
    claims = set(_fixture_claims())
    with SessionLocal() as session:
        untouched = [
            row for row in session.query(Place).filter(Place.city_code == DEMO_CITY).all()
            if row.id not in claims
        ]
    assert untouched, f"{DEMO_CITY} 每个地点都有证据了，UNKNOWN 这一格就无法演示"
    for row in untouched:
        evidence = _accessibility_metadata(row) or {}
        for key in FACT_KEYS:
            assert key not in evidence, (
                f"{row.id} 没被 fixture 声明却有 {key}={evidence.get(key)!r}：缺证据不能变成事实"
            )


def test_demo_producer_refuses_non_boolean_and_provenance_keys_from_the_fixture():
    """生产者只搬运白名单布尔值：出处由代码盖章，改 JSON 改不出 provider truth。"""

    projected = _demo_accessibility_evidence(
        {
            "id": "place-synthetic",
            "accessibility": {
                "stroller_accessible": True,
                "luggage_accessible": "no",       # 非布尔：不搬运
                "evidence_status": "VERIFIED",    # 全局裁决：不搬运
                "source_kind": "LIVE_PROVIDER_CAPTURE",  # 伪造出处：由代码覆盖
                "wheelchair": True,               # 白名单外：不搬运
            },
        }
    )
    assert projected == {
        "stroller_accessible": True,
        "source_kind": "DEMO",
        "source": "data/mock/places.json#place-synthetic",
    }, f"生产者搬运了不该搬的东西，实际 {projected}"


def test_a_single_stroller_fact_never_launderes_the_record_into_step_free_verified():
    """C 的移植件①（仲裁 00:44）：单点婴儿车事实绝不能把整条记录说成"无台阶已验证"。

    这条只管求值，不管键列表（那是上一条的归属），这样"白名单被放宽"能指名到它自己。
    """

    hostile = {
        "id": "place-synthetic",
        "accessibility": {
            "stroller_accessible": True,
            "step_free": True,
            "evidence_status": "VERIFIED",
            "source_kind": "LIVE_PROVIDER_CAPTURE",
        },
    }
    projected = _demo_accessibility_evidence(hostile) or {}
    verdicts = {
        "accessibility_state": accessibility_state(projected),
        "STEP_FREE_REQUIRED": evaluate_accessibility_requirement("STEP_FREE_REQUIRED", projected),
    }
    assert "ACCESSIBILITY_VERIFIED" not in verdicts.values(), (
        f"单点婴儿车事实把整条记录洗成已验证：{projected} -> {verdicts}"
    )
    # 正控：这些键一旦真的穿过，读取链就会量出 VERIFIED——所以上面那条不是永真断言。
    crossed = {key: value for key, value in hostile["accessibility"].items() if key != "source_kind"}
    assert evaluate_accessibility_requirement("STEP_FREE_REQUIRED", crossed) == "ACCESSIBILITY_VERIFIED", (
        f"正控失效（读取链根本量不出 VERIFIED），这条 tripwire 就成了永真：{crossed}"
    )


# ------------------------------------------------------------------------------------------
# 消费链：真实封存 demo journey，两向都测
# ------------------------------------------------------------------------------------------
def _two_way(code: str) -> None:
    fact_key = _fact_key_for(code)
    _seed()
    plans = _generate()
    assert plans, "没有任何候选方案，两向门成了空断言"
    _confirm_capability(code)
    blocked_expected = {
        plan_id: _conflicting_stops(plan_id, fact_key) for plan_id in plans
    }
    assert any(blocked_expected.values()), (
        f"三个候选里没有一个含 {fact_key}=False 的地点 ⇒ 负例不存在，门是空的"
    )
    assert any(not stops for stops in blocked_expected.values()), (
        f"每个候选都含冲突地点 ⇒ 正例不存在，门会误伤整趟"
    )
    for plan_id, stops in blocked_expected.items():
        response = _select(plan_id)
        if stops:
            expected = "%s:%s" % (NAMED_BLOCKER[code], stops[0])
            assert response.status_code == 422, (
                f"{plan_id} 含冲突证据 {stops}，期望 422 具名阻塞，实际 {response.status_code} {response.text[:160]}"
            )
            assert expected in response.text, (
                f"{plan_id} 期望响应里点名 {expected}，实际 {response.text[:200]}"
            )
        else:
            assert response.status_code == 200, (
                f"{plan_id} 全部地点满足或未知（{fact_key} 无 False），却被挡：{response.status_code} {response.text[:200]}"
            )


def test_hard_stroller_names_the_blocker_and_does_not_misfire():
    """负证据 ⇒ STROLLER_ACCESS_UNAVAILABLE:<地点>；正/无证据 ⇒ 200，不误伤。"""

    _two_way("STROLLER")


def test_hard_luggage_names_the_blocker_and_does_not_misfire():
    """同上，行李码走 `LUGGAGE_ACCESS_UNAVAILABLE`（六码里只有这两码在门名单内）。"""

    _two_way("LARGE_LUGGAGE")


def test_generation_never_persists_a_candidate_that_conflicts_with_a_hard_capability():
    """约束先落在文件上时，权威门在落库之前就把它看得懂的候选筛掉（feasibility 里没有 demo 特例）。"""

    code = "STROLLER"
    fact_key = _fact_key_for(code)
    _seed()
    _confirm_capability(code)
    plans = _generate()
    assert plans, "带硬性婴儿车要求时一个候选都没有 ⇒ 整趟不可规划，正是 HARNESS 警告的形状"
    clean_templates = [
        template
        for template in _templates()
        if not any(_evidence(place_id).get(fact_key) is False for place_id, *_ in template)
    ]
    conflicting_templates = [
        template for template in _templates() if template not in clean_templates
    ]
    assert conflicting_templates and clean_templates, (
        f"这条门要求生成阶段真的筛掉过东西：无冲突模板 {len(conflicting_templates)} 个 / "
        f"有冲突模板 {len(clean_templates)} 个 ⇒ 一边为空就是空断言"
    )
    conflicting = [plan_id for plan_id in plans if _conflicting_stops(plan_id, fact_key)]
    assert not conflicting, (
        f"这些候选含 {fact_key}=False 的地点却被生成出来了：{conflicting}"
    )
    assert len(plans) == len(clean_templates), (
        f"候选数应等于无冲突模板数：实际 {len(plans)} vs 期望 {len(clean_templates)}"
    )


# ------------------------------------------------------------------------------------------
# SILENT_CANDIDATE_LOSS 的 API 半边：被筛掉的候选必须留下具名持久事实，且从"前端已经在调"的读取面能拿到
# ------------------------------------------------------------------------------------------
def _rejection_events() -> list[dict]:
    """读 `GET /journeys/{id}/agent-feed`（前端 citynexus.ts:857 调的就是它）。"""

    feed = client.get("/api/v1/journeys/%s/agent-feed" % _JOURNEY, params={"limit": 200})
    assert feed.status_code == 200, f"agent-feed 实际 {feed.status_code} {feed.text[:200]}"
    body = feed.json()
    events = body if isinstance(body, list) else (body.get("events") or body.get("items") or [])
    assert isinstance(events, list), f"agent-feed 形状不认识：{str(body)[:200]}"
    return [
        event for event in events
        if isinstance(event, dict) and event.get("event_type") == "plans.candidate_rejected"
    ]


def test_a_dropped_candidate_leaves_a_named_fact_on_a_route_the_frontend_already_calls():
    """两向：断言在档 ⇒ 被丢的候选留下具名事实；什么都没断言 ⇒ 一个具名事实都不许出现。"""

    _seed()
    plain = client.post("/api/v1/journeys/%s/plans/generate" % _JOURNEY, json={})
    assert plain.status_code == 201, f"无断言时生成期望 201，实际 {plain.status_code} {plain.text[:200]}"
    assert len(plain.json()) == len(_templates()), "无断言就不该少候选"
    assert _rejection_events() == [], (
        f"什么都没冲突却写出了具名拒绝事实：{_rejection_events()}"
    )

    _seed()
    _confirm_capability("STROLLER")
    generated = client.post("/api/v1/journeys/%s/plans/generate" % _JOURNEY, json={})
    assert generated.status_code == 201, f"带硬性婴儿车要求仍要能出可用方案（不许抛错）：{generated.status_code} {generated.text[:200]}"
    kept = generated.json()
    assert len(kept) < len(_templates()), (
        f"本单要的就是「有候选被筛掉」这一格：{len(kept)} vs {len(_templates())}"
    )
    events = _rejection_events()
    assert len(events) == len(_templates()) - len(kept), (
        f"被丢几个候选就该有几条具名事实：{len(events)} vs 期望 {len(_templates()) - len(kept)}"
    )
    for event in events:
        facts = event.get("facts") or {}
        assert "CANDIDATE_REJECTED_BY_FEASIBILITY_GATE" in (facts.get("reason_codes") or []), (
            f"具名事实没有可寻址的 reason code：{facts}"
        )
        assert facts.get("failures"), f"具名事实里一个门自己叫出的失败名都没有：{facts}"
        assert "STROLLER" in (facts.get("hard_rule_codes") or []), (
            f"在档的硬性规则没进事实：{facts.get('hard_rule_codes')}"
        )


def test_two_hard_constraints_each_name_their_own_blocker():
    """两条 HARD 同时在档：一个候选可能同时撞两把（同一地点也算），具名必须与 expected 结构相等。

    红队臂 1 打出的正是我原先的隐含假设"每候选只撞一把"：产品在同一地点两把都冲突时
    会同时点两个名字（正确），而我的 `另一把的名字不得出现` 判据把它判成误红。
    判据改成"具名当且仅当 expected 非空"⇒ 只有**多报了 expected 之外的名字**才红。
    """

    codes = ("STROLLER", "LARGE_LUGGAGE")
    _seed()
    plans = _generate()
    assert len(plans) == len(_templates()), f"SETUP：无断言时应有 {len(_templates())} 个候选，实际 {len(plans)}"
    for code in codes:
        _confirm_capability(code)
    expected = {
        plan_id: {code: _conflicting_stops(plan_id, _fact_key_for(code)) for code in codes}
        for plan_id in plans
    }
    assert any(expected[plan_id]["STROLLER"] for plan_id in plans), "没有候选与 STROLLER 冲突，这条就成了空断言"
    assert any(expected[plan_id]["LARGE_LUGGAGE"] for plan_id in plans), "没有候选与 LARGE_LUGGAGE 冲突，这条就成了空断言"
    seen = {code: 0 for code in codes}
    for plan_id in plans:
        response = _select(plan_id)
        for code in codes:
            named = NAMED_BLOCKER[code] in response.text
            places = expected[plan_id][code]
            assert named == bool(places), (
                f"{plan_id} 的具名与证据不符：{code} expected={places} 名字出现={named} "
                f"（{response.status_code} {response.text[:220]}）"
            )
            seen[code] += 1 if places else 0
        if not any(expected[plan_id].values()):
            assert response.status_code == 200, (
                f"无任何冲突的候选被挡了：{response.status_code} {response.text[:200]}"
            )
    assert all(seen[code] for code in codes), f"两把具名各至少要被观察到一次：{seen}"

    # 同一地点撞两把（红队臂 1 的形状）：两个名字都必须在。
    # 改的是我自己的一次性库，不碰 fixture、不碰产品默认值。
    target_plan = plans[0]
    forced_stop = _force_both_facts_false(target_plan)
    again = _select(target_plan)
    assert again.status_code == 422, f"两把都冲突的候选竟然可选：{again.status_code} {again.text[:200]}"
    for code in codes:
        assert f"{NAMED_BLOCKER[code]}:{forced_stop}" in again.text, (
            f"同一地点 {forced_stop} 两把都 False，响应里却少了 {code} 的具名：{again.text[:260]}"
        )


def test_a_refusal_with_no_surviving_candidate_names_which_rules_broke_them():
    """红队臂 2 的形状：三把 HARD 同时在档 ⇒ 泛化 422 不能只报计数，必须点名是谁把它撞死的。

    第三把按红队做法用 API 直写（绕开解析器），所以这条同时钉住"直写的约束也会具名"。
    """

    codes = ("STROLLER", "LARGE_LUGGAGE", "STEP_FREE_REQUIRED")
    _seed()
    payload = []
    for text in (HARD_SENTENCES["STROLLER"], HARD_SENTENCES["LARGE_LUGGAGE"]):
        payload += [
            {**dict(item), "confirmed": True}
            for item in extract_capabilities_from_text(text, scope="TRAVELER")
        ]
    payload.append({
        "code": "STEP_FREE_REQUIRED",
        "class": "HARD_ACCESSIBILITY_NEED",
        "severity": "HARD",
        "params": {},
        "confirmed": True,
    })
    confirmed = client.post(
        "/api/v1/travelers/%s/capabilities/confirm" % _TRAVELER, json=payload
    )
    assert confirmed.status_code == 200, f"三把 HARD 直写期望 200，实际 {confirmed.status_code} {confirmed.text[:200]}"
    on_file = {row.get("code") for row in confirmed.json()["capabilities"] if row.get("severity") == "HARD"}
    assert set(codes) <= on_file, f"三把硬性约束没都落库：{sorted(on_file)}"

    generated = client.post("/api/v1/journeys/%s/plans/generate" % _JOURNEY, json={})
    assert generated.status_code == 422, f"三个候选都该被撞死，期望 422，实际 {generated.status_code}"
    error = generated.json().get("error", {})
    details = error.get("details") or {}
    assert details.get("reason_code", error.get("reason_code")), "拒答连 reason code 都没有"
    rejections = details.get("candidate_rejections")
    assert isinstance(rejections, list) and rejections, (
        f"422 的 details 里只有计数、没有逐候选具名：{sorted(details)}"
    )
    assert len(rejections) == details.get("day_sequences_available"), (
        f"具名条数应等于被评估的候选数：{len(rejections)} vs {details.get('day_sequences_available')}"
    )
    named_codes = {code for entry in rejections for code in (entry.get("hard_rule_codes") or [])}
    assert set(codes) <= named_codes, f"在档的硬性规则没能全部具名：{sorted(named_codes)}"
    for code, fact_key in (("STROLLER", "stroller_accessible"), ("LARGE_LUGGAGE", "luggage_accessible")):
        expected_places = {
            place_id
            for place_id in demo_city_place_ids()
            if _evidence(place_id).get(fact_key) is False
        }
        assert expected_places, f"演示城里没有 {fact_key}=False 的地点，这一格成了空断言"
        text = json.dumps(rejections, ensure_ascii=False)
        assert any(
            f"{NAMED_BLOCKER[code]}:{place_id}" in text for place_id in expected_places
        ), f"具名事实里没有 {code} 的阻塞名（期望点名 {sorted(expected_places)}）：{text[:300]}"
    for entry in rejections:
        assert (entry.get("failures") or []) and (entry.get("hard_rule_codes") or []), (
            f"存在一条无名的拒答事实：{entry}"
        )


def test_the_named_reason_points_at_the_stop_whose_demo_evidence_conflicted():
    """事实里点名的地点必须就是「该候选自己的 stop 且证据为 False」那个——不许写死。"""

    fact_key = _fact_key_for("STROLLER")
    _seed()
    _confirm_capability("STROLLER")
    generated = client.post("/api/v1/journeys/%s/plans/generate" % _JOURNEY, json={})
    assert generated.status_code == 201, f"SETUP：生成实际 {generated.status_code}"
    events = _rejection_events()
    assert events, "没有任何具名拒绝事实，这条归因就成了空断言"
    for event in events:
        facts = event.get("facts") or {}
        stops = [str(place_id) for place_id in (facts.get("stops") or [])]
        assert stops, f"事实里没留下候选自己的地点清单：{facts}"
        blocking = [place_id for place_id in stops if _evidence(place_id).get(fact_key) is False]
        assert blocking, (
            f"这个被丢的候选里没有一个地点的 {fact_key} 是 False，那它凭什么被丢：{stops} -> {facts.get('failures')}"
        )
        for code in facts.get("hard_rule_codes") or []:
            if code != "STROLLER":
                continue
            named = [
                failure for failure in (facts.get("failures") or [])
                if any(place_id in str(failure) for place_id in blocking)
            ]
            assert named, (
                f"事实点名的冲突地点与实际证据不符：期望提到 {blocking} 之一，实际 {facts.get('failures')}"
            )


# ------------------------------------------------------------------------------------------
# 静态独占守卫（仲裁 5788528772 补的那一格）
# ------------------------------------------------------------------------------------------
PRODUCER_SYMBOL = "_demo_accessibility_evidence"


def _producer_references(root, excluded_names=()):
    """每一条"root 下的产品码引用了 DEMO producer"的证据，按 AST 节点类型命名。

    只认 import / 名字使用 / 属性使用三种真实引用：注释不进 AST，docstring 是 Constant 节点，
    所以这条扫描对"文里提到这个名字"天然免疫——两个方向都有见证，见下面那条门。
    """

    hits = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or path.name in excluded_names:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(a.name == PRODUCER_SYMBOL for a in node.names):
                kind = "import_from"
            elif isinstance(node, ast.Name) and node.id == PRODUCER_SYMBOL:
                kind = "name"
            elif isinstance(node, ast.Attribute) and node.attr == PRODUCER_SYMBOL:
                kind = "attribute"
            else:
                continue
            hits.append({"file": str(path.relative_to(root)), "line": node.lineno, "kind": kind})
    return hits


def test_the_demo_producer_stays_reachable_only_from_the_demo_seed(tmp_path):
    """`backend/app/**` 里除 `services.py` 外，任何产品模块都不许引用 `_demo_accessibility_evidence`。

    运行时那几条门证的是"今天种下的数据不外泄"；这条钉的是"DEMO producer 只能 seed-only，
    不许日后变成 provider/catalog 的真相助手"。0 处产品码改动，也不逐字搬 C 的测试
    （它的符号是 demo_accessibility_evidence，这里活着的是带下划线的那个）。
    """

    app_root = Path(services_module.__file__).resolve().parent
    assert (app_root / "services.py").is_file(), (
        f"定位不到产品包目录，这条门会扫到零个文件却报绿：{app_root}"
    )
    scanned = [p.relative_to(app_root) for p in app_root.rglob("*.py") if "__pycache__" not in p.parts]
    assert len(scanned) > 5, f"只扫到 {len(scanned)} 个产品模块，扫描面本身可疑"

    hits = _producer_references(app_root, excluded_names=("services.py",))
    assert not hits, (
        f"DEMO producer 被 services.py 之外的产品码用掉了，它会变成 provider/catalog 的真相助手：{hits}"
    )

    # 正控 A：名字只出现在模块文档串与注释里——裸 substring 会误伤，AST 不伤。
    prose = tmp_path / "mentions_it_in_prose.py"
    prose.write_text(
        "'A module that only says _demo_accessibility_evidence in its prose.'\n"
        "# and again in a comment: _demo_accessibility_evidence\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    assert prose.read_text(encoding="utf-8").count(PRODUCER_SYMBOL) == 2, "正控前提：文里确实两处提到这个名字"
    assert _producer_references(tmp_path) == [], (
        "文档串或注释里的名字被当成了引用，这条守卫会误伤纯文案改动"
    )

    # 正控 B：真实 import 必须被抓到，否则"扫不到"与"没有引用"是同一种读法。
    prose.unlink()
    reach = tmp_path / "reaches_for_it.py"
    reach.write_text(
        "from app.services import _demo_accessibility_evidence\n\nFOO = _demo_accessibility_evidence\n",
        encoding="utf-8",
    )
    caught = _producer_references(tmp_path)
    assert len(caught) == 2 and {c["kind"] for c in caught} == {"import_from", "name"}, (
        f"扫描器抓不到真实引用，那它的绿色就不算证据：{caught}"
    )
