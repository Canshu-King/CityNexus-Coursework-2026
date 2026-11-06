"""ORD-0106 继续 · 车道 F - 发现层的三件「今天不存在」的事。

车道 B 的 ORD-0109 实测把这三条钉出来了，本文件把它们变成门（全部假 provider、零出网）：

* G16 TTL 必须有人读 - `settings.real_city_cache_ttl_seconds=900` 只在
  ``real_city.py:964-966`` **写** ``RealPlaceCandidate.expires_at``、:1004 抄进 evidence_json，
  全仓没有任何一处读它来决定「这次还要不要出网」。B 量的结果就是暖趟与冷趟同耗时
  （242.9 s vs 238.3 s）。判据用**计数 client**：同一 journey + 同一 provider + 未过期 ⇒ 第二次
  外呼必须为 0，且第二次的产出不得变少（否则「0 次」可以靠「什么都不返回」蒙过去）。
* G17 发现范围必须来自行政区 extent - ``real_city.py:716`` 把 ``radius_m`` 硬夹成
  ``min(radius_m, 5000)`` ⇒ 「水合一个县」实际是「搜县城 5 公里圈」。判据不规定写法：
  发出去的几何必须配得上问的那块地方，且两块不同尺度的地方必须问出两个不同的问题 ——
  一个与请求无关的固定数两头都过不了。
* G18 没问的问题不得拿默认答案凑 - ``:701-711`` 的关键词分支里没有 `nightlife`，它落到默认集合
  ``cafe|restaurant|library|arts_centre|theatre`` ⇒ 屏幕上那句「夜生活」其实是咖啡馆的查询结果。
  判据：每个覆盖分类要么有自己的 tag 分支，要么明确 MISSING；不得借默认集合还魂。

三条今天**全是红的**，而且红得有读数 —— 不许 skip、不许放宽成「看起来会缓存」。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.integrations.discovery.real_city as real_city
from app.core.config import settings
from app.db.models import Journey, RealPlaceCandidate
from app.db.session import SessionLocal
from app.integrations.discovery.real_city import OverpassPOIProvider, capture_real_city_candidates
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)

#: 假域名：§17C 的护栏（G8a）扫的就是 provider 字面量，本文件一个字都不带真域名。
FAKE_BASE = "http://provider-shape.local"
UTC = timezone.utc
CATEGORY_QUERY = "咖啡馆"
#: 仲裁 v0.2 的六个覆盖分类（与附件、G12 同一套词表）。
COVERAGE_CATEGORIES = {
    "attractions": "景点",
    "food": "餐厅",
    "nightlife": "夜生活",
    "lodging": "住宿",
    "mobility": "地铁站",
    "booking": "门票预约",
}
#: ``real_city.py:699`` 那句默认集合，抄来是为了**比对**，不是为了使用。
DEFAULT_TAGS = '"amenity"~"cafe|restaurant|library|arts_centre|theatre"'
CAFE_ELEMENT = {
    "type": "node",
    "id": 7001,
    "lat": 39.9042,
    "lon": 116.4074,
    "tags": {"amenity": "cafe", "name": "假数据咖啡馆", "opening_hours": "Mo-Fr 09:00-18:00"},
}


class _SettingsOverride:
    """``settings`` 是 frozen dataclass，改不了字段 —— 换掉被测模块看到的那个引用。"""

    def __init__(self, **overrides: Any) -> None:
        object.__setattr__(self, "_overrides", dict(overrides))

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(settings, name)


class _HttpxShim:
    """只替掉 ``Client``；模块里 ``httpx.HTTPError`` 之类的判类必须还是真的。"""

    HTTPError = httpx.HTTPError
    TimeoutException = httpx.TimeoutException
    ConnectError = httpx.ConnectError

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class CallLog:
    """每一次外呼都记下来：method / path / 查询文本；响应是可控的假数据。"""

    def __init__(self, elements: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.elements = list(elements if elements is not None else [CAFE_ELEMENT])

    def __len__(self) -> int:
        return len(self.calls)

    @property
    def interpreter(self) -> list[dict[str, Any]]:
        return [item for item in self.calls if item["path"].endswith("/interpreter")]

    def query_texts(self) -> list[str]:
        return [str(item["body"]) for item in self.interpreter]

    def handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def handle(request: httpx.Request) -> httpx.Response:
            body = request.content.decode("utf-8") if request.content else ""
            parsed = parse_qs(body).get("data", [""])
            self.calls.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "body": parsed[0] if parsed and parsed[0] else body,
                    "params": dict(request.url.params),
                }
            )
            if request.url.path.endswith("/interpreter"):
                return httpx.Response(200, json={"elements": list(self.elements)})
            return httpx.Response(
                200,
                json=[
                    {
                        "osm_type": "relation",
                        "osm_id": 1,
                        "lat": "39.9042",
                        "lon": "116.4074",
                        "display_name": "假数据城, 中国",
                        "type": "city",
                        "class": "place",
                    }
                ],
            )

        return handle

    def fake_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler()))

    def install_on_module(self, monkeypatch) -> "CallLog":
        """把 ``real_city`` 里两个 provider 构造函数自己创建的 client 换成内存计数版。"""

        log = self

        def factory(**kwargs: Any) -> httpx.Client:
            kwargs.pop("timeout", None)
            kwargs.pop("trust_env", None)
            return httpx.Client(transport=httpx.MockTransport(log.handler()))

        monkeypatch.setattr(real_city, "httpx", _HttpxShim(Client=factory))
        monkeypatch.setattr(
            real_city,
            "settings",
            _SettingsOverride(
                real_city_enabled=True, nominatim_url=FAKE_BASE, overpass_url=FAKE_BASE
            ),
        )
        return self


def _stage_journey_id() -> str:
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, f"SETUP 失败：/api/v1/demo/reset 实际 {reset.status_code}"
    scene = client.get("/api/v1/demo/scene")
    assert scene.status_code == 200, f"SETUP 失败：/api/v1/demo/scene 实际 {scene.status_code}"
    journey_id = str(scene.json().get("journey_id"))
    with SessionLocal() as session:
        assert session.get(Journey, journey_id) is not None, f"SETUP 失败：旅程 {journey_id} 不存在"
    return journey_id


def _candidate_rows(journey_id: str) -> list[RealPlaceCandidate]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(RealPlaceCandidate).where(RealPlaceCandidate.journey_id == journey_id)
            ).all()
        )


def _is_future(row: RealPlaceCandidate) -> bool:
    raw = str(row.expires_at or "")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment > datetime.now(UTC)


def _capture(journey_id: str, log: CallLog, monkeypatch, *, radius_m: int = 2500) -> int:
    """跑一次真实编排（live 模式），返回这段时间里计数到的外呼次数。"""

    log.install_on_module(monkeypatch)
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        before = len(log)
        capture_real_city_candidates(
            session, journey, query=CATEGORY_QUERY, radius_m=radius_m, limit=5, mode="live"
        )
        calls = len(log) - before
        session.commit()
    return calls


# --------------------------------------------------------------------------------------
# G16 · TTL 必须有人读
# --------------------------------------------------------------------------------------
def test_g16a_the_counting_client_sees_every_outbound_call_or_the_zero_proves_nothing(monkeypatch):
    """仪器先自证：冷趟必须看到 ≥1 次外呼，否则 G16b 的「0 次」什么都不是证据。"""

    journey_id = _stage_journey_id()
    log = CallLog()
    calls = _capture(journey_id, log, monkeypatch)
    assert calls > 0, (
        "SETUP 失败：计数 client 一次都没记录到外呼 —— 那不是「缓存生效」，是仪器没接上。"
        "没有这条自证，0 次可以靠断网、靠抛错、靠返回空来凑"
    )
    assert all(item["path"] for item in log.calls), "SETUP 失败：外呼记录没有 path，无法归因"
    assert log.query_texts(), (
        "SETUP 失败：没抓到任何 /interpreter 查询文本 —— G17/G18 的判据同样会失去对象"
    )


def test_g16b_an_unexpired_candidate_set_must_answer_without_going_outside(monkeypatch):
    """同一 journey、同一 provider、`expires_at` 还在未来 ⇒ 第二次外呼必须为 0。

    今天红：``expires_at`` 只有写（``real_city.py:966``）没有读。修法归后端 —— 按
    (journey_id, provider_key, 分类问题) 命中未过期候选就直接用库存。不许改成「写过字段就算过」。
    """

    journey_id = _stage_journey_id()
    cold_calls = _capture(journey_id, CallLog(), monkeypatch)
    assert cold_calls > 0, "SETUP 失败：冷趟没出网，暖趟没有可比对象"
    rows = _candidate_rows(journey_id)
    assert rows, (
        "SETUP 失败：冷趟之后这张旅程零候选落库（materialize 没走到还是舞台没 anchor？）—— "
        "暖趟无从谈起，先修前提，不要放宽判据"
    )
    unexpired = [row for row in rows if _is_future(row)]
    assert unexpired, (
        f"SETUP 失败：落库 {len(rows)} 行里没有一行 expires_at 在未来（TTL="
        f"{settings.real_city_cache_ttl_seconds}s）—— 前提不成立，判据会假通过"
    )

    warm_log = CallLog()
    warm_calls = _capture(journey_id, warm_log, monkeypatch)
    assert warm_calls == 0, (
        f"期望：{len(unexpired)}/{len(rows)} 行未过期 ⇒ 第二次外呼 0 次；实际：又发了 {warm_calls} 次 "
        f"({[item['path'] for item in warm_log.calls]})。今天没人读 expires_at —— 写了不等于读了，"
        "这正是 B 那对 242.9 s / 238.3 s 的机制"
    )
    after = _candidate_rows(journey_id)
    assert len(after) >= len(rows), (
        f"期望：0 次外呼不得以「少给结果」为代价（{len(rows)} → {len(after)} 行）；"
        "命中缓存之后答案变少，就是把缓存写成了丢数据"
    )


def test_g16c_an_expired_candidate_set_must_go_back_to_the_provider(monkeypatch):
    """反向半边：过期之后必须重新出网 —— 否则「读 TTL」能靠永远不出网假装通过。

    这条今天是**绿的**，但它绿在「根本没人读 TTL」上。后端修完 G16b 之后这条才真正开始承重，
    所以两道必须一起交。
    """

    journey_id = _stage_journey_id()
    _capture(journey_id, CallLog(), monkeypatch)
    rows = _candidate_rows(journey_id)
    assert rows, "SETUP 失败：冷趟没落候选，过期场景无从构造"
    with SessionLocal() as session:
        for row in session.scalars(
            select(RealPlaceCandidate).where(RealPlaceCandidate.journey_id == journey_id)
        ):
            row.expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        session.commit()
    assert not any(_is_future(row) for row in _candidate_rows(journey_id)), (
        "SETUP 失败：expires_at 没被改到过去 —— 前提失效，这条判据会假绿"
    )
    warm_log = CallLog()
    calls = _capture(journey_id, warm_log, monkeypatch)
    assert calls > 0, (
        "期望：过期之后回到 provider（否则缓存变成永久快照，屏幕上写的是去年今天的城市）；"
        "实际：0 次外呼"
    )


# --------------------------------------------------------------------------------------
# G17 · 发现范围必须来自行政区 extent
# --------------------------------------------------------------------------------------
def _asked(provider_query: str) -> str:
    match = re.search(r"nwr\[(.+?)\]\(", provider_query)
    return match.group(1) if match else provider_query[:120]


def _emitted_radius(provider_query: str) -> int | None:
    match = re.search(r"\(around:(\d+),", provider_query)
    return int(match.group(1)) if match else None


def _overpass_query(monkeypatch, query: str, radius_m: int) -> str:
    """直接驱动 provider 的 search，抓它真正发给 Overpass 的那句话。"""

    log = CallLog()
    monkeypatch.setattr(
        real_city,
        "settings",
        _SettingsOverride(real_city_enabled=True, overpass_url=FAKE_BASE, nominatim_url=FAKE_BASE),
    )
    provider = OverpassPOIProvider(client=log.fake_client(), base_url=FAKE_BASE)
    provider.search(query, latitude=39.9042, longitude=116.4074, radius_m=radius_m, limit=5)
    provider.close()
    texts = log.query_texts()
    assert texts, "SETUP 失败：provider 一个 /interpreter 查询都没发出去，判据没有对象"
    return texts[0]


def test_g17a_the_discovery_range_must_honour_the_extent_asked_for_not_a_five_km_clamp(monkeypatch):
    """发出去的几何必须配得上问的那块地方。

    ``real_city.py:716`` 的 ``around:{min(radius_m, 5000)}`` 把 12 km（一个县的尺度）压成 5 km 圈。
    今天红。修法不限（bbox / ``area[...]`` 多边形 / 按 extent 计算都行），固定一个与请求无关的数不行。
    """

    text = _overpass_query(monkeypatch, "景点", 12000)
    emitted = _emitted_radius(text)
    uses_area = "bbox" in text or "area[" in text
    assert emitted is not None or uses_area, (
        f"SETUP 失败：查询里既没有 around 半径也没有 bbox/area，判据无从成立：{text[:200]}"
    )
    assert uses_area or (emitted is not None and emitted >= 12000), (
        f"期望：调用方要 12000 m 的范围（一个县的尺度），发出去的几何必须 ≥ 这个数，或改用行政区"
        f"多边形/bbox；实际：``around:{emitted}``（{text[:120]}）。根因 real_city.py:716 "
        "min(radius_m, 5000) —— 「水合一个县」现在是「搜县城 5 公里圈」，县域里的乡镇一律看不见，"
        "能力回答会把 PARTIAL 说成 READY"
    )


def test_g17b_destinations_of_different_sizes_must_not_ask_the_same_question(monkeypatch):
    """两块不同尺度的地方必须问出两个不同的问题：一个固定半径做不到这件事。"""

    wide = _emitted_radius(_overpass_query(monkeypatch, "景点", 12000))
    narrow = _emitted_radius(_overpass_query(monkeypatch, "景点", 3000))
    if wide is not None and narrow is not None:
        assert wide != narrow, (
            f"期望：extent 12 km 与 extent 3 km 两块目的地，发出去的范围必须不同；"
            f"实际：around:{wide} vs around:{narrow} —— 两者都固定在同一个数上，说明范围不是"
            "「这块地方」的属性，而是代码里的常数（今天实测都是 5000）"
        )
    else:
        assert wide is None and narrow is None, (
            f"期望：要么两条都走行政区几何（没有 around），要么两条都按 extent 给半径；"
            f"实际：一条 {wide} 一条 {narrow} —— 同一次改动里混了两种口径"
        )


# --------------------------------------------------------------------------------------
# G18 · 没问的问题不得拿默认答案凑
# --------------------------------------------------------------------------------------
def test_g18a_nightlife_must_not_be_answered_with_a_cafe_query(monkeypatch):
    """``:701-711`` 的关键词表里没有 nightlife ⇒ 它拿到的正是那句默认集合。"""

    text = _overpass_query(monkeypatch, "夜生活", 2500)
    asked = _asked(text)
    assert asked != DEFAULT_TAGS, (
        f"期望：问「夜生活」就得问夜生活的 tag（bar/nightclub/pub…），问不到就明确报 MISSING；"
        f"实际发出去的是默认集合 {DEFAULT_TAGS} —— 屏幕上那句「这里的夜生活」底下是咖啡馆的查询结果。"
        "根因 real_city.py:701-711 没有 nightlife 分支"
    )
    assert re.search(r"bar|nightclub|pub|酒吧|夜店", asked), (
        f"期望：nightlife 的 tag 表达式点名夜生活要素；实际：{asked}"
    )


def test_g18b_every_coverage_category_must_have_its_own_branch_or_say_missing(monkeypatch):
    """六个覆盖分类逐条字面量比对：不得有两条共用那句默认集合。

    今天的分母（实测，写进红里以免被当成全仓结论）：``attractions / food / lodging`` 有分支，
    ``nightlife / mobility / booking`` 三类一字不差落到默认集合 ⇒ **3/6 塌缩**。
    """

    asked_by_category = {
        category: _asked(_overpass_query(monkeypatch, word, 2500))
        for category, word in COVERAGE_CATEGORIES.items()
    }
    defaulted = sorted(
        category for category, tags in asked_by_category.items() if tags == DEFAULT_TAGS
    )
    assert not defaulted, (
        f"期望：{len(COVERAGE_CATEGORIES)} 个分类各有自己的 tag 分支（问不到就报 MISSING，"
        f"不要借默认集合还魂）；实际落默认集合：{defaulted}。这就是 B 那条「其实没查过的夜生活」"
        "的成因：默认集合不是「不知道」，它是一份看起来像答案的数据"
    )
    grouped: dict[str, list[str]] = {}
    for category, tags in asked_by_category.items():
        grouped.setdefault(tags, []).append(category)
    shared = {tags: sorted(group) for tags, group in grouped.items() if len(group) > 1}
    assert not shared, (
        f"期望：不同分类不得共用同一句 provider 查询（共用了就无法从回答反推是哪一类问出的）；"
        f"实际：{sorted(shared.values())}"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
