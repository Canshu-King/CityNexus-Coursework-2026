"""ORD-0106 lane F (续) - 矩阵的三个单位问题：一秒、一片、一条直线。

三条门，全部进程内假 provider，一个字节都不出网（§17C）：

* G13 单位 - Valhalla 的 ``/route`` 与 ``/sources_to_targets`` 都按 **秒** 返回 ``time``、按
  **公里** 返回 ``length``/``distance``。产品在三个地方各自除 60（``valhalla_http.py``
  ``:225`` ``:275``），**没有任何东西钉住单位**：把秒当分钟用，15 分钟的腿会写成 1 分钟；反过来
  会写成 900 分钟 —— 两者都是一个"看着正常"的数字。假 provider 只自证"我们也算了一遍"，所以这条
  门真正的判据是**物理速度带**：1.5 km 走 20 分钟 = 4.5 km/h；任何一次单位错都把速度推到
  0.05 或 270 km/h。
* G14 上限 - 实测：N=10 ⇒ 200、100 cells、1018 ms；N=11 ⇒ 400、``error_code`` 150。上限是
  **cells ≤ 100**，不是错误文案里那个 "locations" 的字面数（今天的调用形状是方阵，两者恰好同解，
  所以按文案预检下去必然是错的：它落在一块 4×20=80 cells 的合法矩形上就判错了）。
* G15 直线 - 一条腿没有事实时，产品必须说「没有」（逐腿 ``MOBILITY_DATA_UNAVAILABLE`` 与投影里的
  ``legs_hard_eligible`` / ``legs_total``），而不是拿两点直线距离除一个速度常量。
"""

from __future__ import annotations

import ast
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app as app_package  # 只为拿到 app/ 的真实目录
from app.core.config import settings
from app.db.models import Activity, Journey, PlanVersion
from app.db.session import SessionLocal
from app.integrations.mobility import runtime
from app.integrations.mobility.valhalla_http import (
    ValhallaHTTPMobilityProvider,
    ValhallaProviderError,
)
from app.main import (
    MobilityDataUnavailable,
    _activity_routing_key,
    _mobility_evidence_coverage,
    _replan_estimate,
    app,
)
from app.replanning.feasibility import (
    FeasibilityActivity,
    FeasibilityContext,
    evaluate_feasibility,
)
from app.replanning.provider_snapshots import (
    EXPLICIT_FROZEN_FIXTURE,
    LIVE_PROVIDER_CAPTURE,
    SYNTHETIC_DEFAULT,
    MobilitySnapshot,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)

#: 刻意用假域名：§17C 的护栏（G8a）扫的就是 provider 字面量，这条门不该请它开例外。
FAKE_BASE = "http://provider-shape.local"
#: 实测上限（annex §9）：单位是 cells，不是 locations。
MAX_CELLS = 100
#: 一次真能步动的距离：1.5 km；Valhalla 的秒口径 = 1200 s ⇒ 20 分钟 ⇒ 4.5 km/h。
WALK_SECONDS = 1200.0
WALK_KILOMETRES = 1.5
#: 矩阵每格：900 s / 0.75 km ⇒ 15 分钟 / 750 m。
CELL_SECONDS = 900
CELL_KILOMETRES = 0.75

APP_ROOT = Path(app_package.__file__).resolve().parent
MINUTE_LIKE = re.compile(r"(travel|duration|minute|_eta\b|eta_)", re.IGNORECASE)
RADIUS_VALUES = {6371000.0, 6371000, 6_371_000.0, 6_371_000, 6371.0, 6371.0061}


# ======================================================================================
# G13 · 单位
# ======================================================================================
def _place_lookup(place_id: str) -> dict[str, float]:
    return {"latitude": 39.9, "longitude": 116.4}


def _provider(handler: Callable[[httpx.Request], httpx.Response]) -> ValhallaHTTPMobilityProvider:
    return ValhallaHTTPMobilityProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url=FAKE_BASE,
        place_lookup=_place_lookup,
    )


def _assert_walk_speed_is_human(label: str, distance_m: Any, duration_min: Any) -> None:
    """单位错了必然露馅：这一判据不读实现里的常数，只读物理。"""

    assert distance_m and duration_min, (
        f"期望：{label} 同时给出距离与时长，才算一次可核对的测量；实际："
        f"distance_m={distance_m!r} duration_min={duration_min!r} —— 缺一个，速度带判据本身就失效了"
    )
    speed = (float(distance_m) / 1000.0) / (int(duration_min) / 60.0)
    assert 1.5 <= speed <= 9.0, (
        f"期望：{label} 的步行速度落在人速带 1.5–9.0 km/h（1.5 km / 20 分钟 = 4.5 km/h）；"
        f"实际：{speed:.2f} km/h（distance_m={distance_m!r}, duration_min={duration_min!r}）。"
        "这只会由单位错产生：把秒当分钟 ⇒ 约 270 km/h；把分钟再除一次 60 ⇒ 约 0.05 km/h。"
        "valhalla_http.py 的 :99 / :225 / :275 任何一处 /60.0 被删或被加两遍，红的就是这里"
    )


def test_g13_route_summary_time_is_seconds_and_becomes_minutes_not_seconds():
    """``/route`` 的 ``trip.summary.time``（:83 读秒）⇒ :99 除 60、:100 公里化米。"""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={"trip": {"summary": {"time": WALK_SECONDS, "length": WALK_KILOMETRES}, "legs": []}},
        )

    estimate = _provider(handler).estimate("place-a", "place-b")
    assert seen == ["/route"], f"SETUP 失败：这条门只该喂 /route，实际打到 {seen}"
    assert estimate.duration_min == 20, (
        f"期望：1200 秒 ⇒ 20 分钟（:99 的 ceil(seconds / 60.0)）；实际：{estimate.duration_min}。"
        "读到 1200 是漏除（秒当分钟用），读到 1 是多除了一次"
    )
    assert estimate.distance_m == 1500, (
        f"期望：length 1.5 公里 ⇒ 1500 m（:100 的 * 1000）；实际：{estimate.distance_m!r}"
    )
    _assert_walk_speed_is_human("/route", estimate.distance_m, estimate.duration_min)


def test_g13b_both_matrix_shapes_convert_the_same_seconds_to_the_same_minutes():
    """两种响应形状走两条不同代码：:275（cells）与 :225（durations 方阵）。

    单位判据对两条都要成立 —— 少除一次 60 只发生在其中一条上时，另一条会替它遮掩。
    """

    def cells_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        rows, cols = len(payload["sources"]), len(payload["targets"])
        return httpx.Response(
            200,
            json={
                "sources_to_targets": [
                    [
                        {"to_index": target, "time": CELL_SECONDS, "distance": CELL_KILOMETRES}
                        for target in range(cols)
                    ]
                    for _ in range(rows)
                ]
            },
        )

    def durations_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        rows, cols = len(payload["sources"]), len(payload["targets"])
        return httpx.Response(
            200,
            json={
                "durations": [[CELL_SECONDS] * cols for _ in range(rows)],
                "distances": [[CELL_KILOMETRES] * cols for _ in range(rows)],
            },
        )

    keys = ["place-0", "place-1", "place-2"]
    from_cells = _provider(cells_handler).matrix(keys)
    from_durations = _provider(durations_handler).matrix(keys)
    for label, matrix in (("cells 形状", from_cells), ("durations 形状", from_durations)):
        cells = [cell for row in matrix for cell in row if cell is not None]
        assert cells, f"期望：{label} 解析出单元格；实际：全 None —— 形状被静默吞掉，那什么门都过得去"
        assert {cell.duration_min for cell in cells} == {15}, (
            f"期望：{label} 的 900 秒 ⇒ 每格 15 分钟；实际："
            f"{sorted({cell.duration_min for cell in cells})}"
        )
    primary = [cell for row in from_cells for cell in row if cell is not None]
    assert {cell.distance_m for cell in primary} == {750}, (
        f"期望：主形状（产品自己发 verbose:false 拿到的 cells，:251 读到 distance）0.75 公里 ⇒ "
        f"750 m；实际：{sorted({cell.distance_m for cell in primary})}"
    )
    _assert_walk_speed_is_human("/sources_to_targets（cells 形状）", primary[0].distance_m, primary[0].duration_min)
    minutes = _provider(cells_handler).matrix_minutes(keys)
    values = [value for row in minutes for value in row if value is not None]
    assert values and set(values) == {15}, (
        f"期望：matrix_minutes 与 matrix 同源同值（15）；实际：{sorted(set(values))}"
    )


def test_g13d_distance_must_survive_whichever_shape_the_provider_answers(
    ):
    """同一格信息不得因为响应形状而被丢掉：:178 那条分支传了 distances，:187 那条没传。

    实测：喂 durations + distances，duration 规规矩矩换算，distance 全变成 None。
    这条门是给后端的（``valhalla_http.py`` 少传一个参数），不是环境噪声。
    """

    def durations_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        rows, cols = len(payload["sources"]), len(payload["targets"])
        return httpx.Response(
            200,
            json={
                "durations": [[CELL_SECONDS] * cols for _ in range(rows)],
                "distances": [[CELL_KILOMETRES] * cols for _ in range(rows)],
            },
        )

    def cells_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        rows, cols = len(payload["sources"]), len(payload["targets"])
        return httpx.Response(
            200,
            json={
                "sources_to_targets": [
                    [
                        {"to_index": target, "time": CELL_SECONDS, "distance": CELL_KILOMETRES}
                        for target in range(cols)
                    ]
                    for _ in range(rows)
                ]
            },
        )

    keys = ["place-0", "place-1", "place-2"]
    from_cells = [
        cell for row in _provider(cells_handler).matrix(keys) for cell in row if cell is not None
    ]
    from_durations = [
        cell
        for row in _provider(durations_handler).matrix(keys)
        for cell in row
        if cell is not None
    ]
    assert from_cells and from_durations, "SETUP 失败：任一形状一个格子都没解析出来，比较无从成立"
    dropped = [cell for cell in from_durations if cell.distance_m is None]
    assert not dropped, (
        f"期望：provider 已经把 distances 一起给出来了，两种形状的 distance_m 都得是 750 m —— "
        f"cells 形状 {len(from_cells)} 格全有，durations 形状 {len(dropped)}/{len(from_durations)} 格丢了；"
        "根因在 valhalla_http.py:185-187：这条分支调 _normalize_duration_matrix 时没把 "
        "payload 里的 distances 传下去（:178 那条嵌套分支传了）。距离掉了，速度带判据、"
        "里程口径与「这条腿走了多远」就全部失去来源 —— 请后端补那一个参数，"
        "不要反过来把这条门改成「允许 None」"
    )
    assert {cell.distance_m for cell in from_durations} == {
        cell.distance_m for cell in from_cells
    }, "期望：同一批物理事实，两种形状规范化后一模一样"


def test_g13c_an_impossible_duration_is_refused_not_rescaled():
    """「看着不对」的输入必须被拒绝，不能被悄悄修成一个顺眼的分钟数。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"trip": {"summary": {"time": -5, "length": WALK_KILOMETRES}, "legs": []}},
        )

    with pytest.raises(ValhallaProviderError) as refused:
        _provider(handler).estimate("place-a", "place-b")
    assert "ValhallaRouteEstimate" not in str(refused.value), (
        f"期望：拒绝理由说清是哪一项不对，不把内部类型端给用户；实际：{refused.value}"
    )


# ======================================================================================
# G14 · 分片预检
# ======================================================================================
class MatrixProbe:
    """一个照 100 cells 规矩回 400 的假 Valhalla，同时记下每个被发出的请求。"""

    def __init__(self, error_body: Mapping[str, Any] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.error_body = dict(
            error_body
            or {
                "error_code": 150,
                "status_code": 400,
                "error": "Too many locations for this matrix request",
            }
        )

    @property
    def matrix_requests(self) -> list[dict[str, Any]]:
        return [item for item in self.requests if item["path"] == "/sources_to_targets"]

    def handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith("/status"):
                return httpx.Response(200, json={"version": "3.5", "has_live_traffic": False})
            payload = json.loads(request.content.decode("utf-8"))
            sources = payload.get("sources") or []
            targets = payload.get("targets") or []
            cells = len(sources) * len(targets)
            self.requests.append(
                {
                    "path": request.url.path,
                    "sources": len(sources),
                    "targets": len(targets),
                    "cells": cells,
                    "costing": payload.get("costing"),
                }
            )
            if cells > MAX_CELLS:
                return httpx.Response(400, json=self.error_body)
            return httpx.Response(
                200,
                json={
                    "sources_to_targets": [
                        [
                            {"to_index": target, "time": CELL_SECONDS, "distance": CELL_KILOMETRES}
                            for target in range(len(targets))
                        ]
                        for _ in range(len(sources))
                    ]
                },
            )

        return handle


def _nodes(count: int) -> list[dict[str, Any]]:
    return [
        {
            "key": f"node-{index}",
            "role": "ACTIVITY",
            "place_id": f"place-{index}",
            "external_id": None,
            "latitude": 39.9 + index * 0.001,
            "longitude": 116.4 + index * 0.001,
        }
        for index in range(count)
    ]


class _SettingsOverride:
    """``settings`` 是 frozen dataclass，改不了字段 —— 换掉 runtime 看到的那个引用。"""

    def __init__(self, **overrides: Any) -> None:
        object.__setattr__(self, "_overrides", dict(overrides))

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(settings, name)


def _capture(monkeypatch, probe: MatrixProbe, count: int) -> dict[str, Any]:
    """驱动产品自己的 ``capture_journey_mobility_matrix``，只把它的 socket 换成内存假响应。"""

    monkeypatch.setattr(
        runtime, "settings", _SettingsOverride(live_valhalla_url=FAKE_BASE)
    )
    transport = httpx.MockTransport(probe.handler())

    def factory(**kwargs: Any) -> httpx.Client:
        kwargs.pop("timeout", None)
        return httpx.Client(transport=transport, **kwargs)

    monkeypatch.setattr(runtime, "httpx", SimpleNamespace(Client=factory))
    return runtime.capture_journey_mobility_matrix(
        _nodes(count), operation_key="lane-f-matrix-truth"
    )


def test_g14a_an_oversized_matrix_fails_closed_and_invents_no_facts(monkeypatch):
    """N=11 撞 400 时今天已经成立的是「不许留下产物」——守住它别退化成半张矩阵。"""

    probe = MatrixProbe()
    result = _capture(monkeypatch, probe, 11)
    assert probe.matrix_requests, "SETUP 失败：假 provider 一次都没被调用，这条门是空门"
    assert result["status"] == "FAILED", (
        f"期望：越界的矩阵判 FAILED（NOT_CONFIGURED 说明 live_valhalla_url 没换上来，"
        f"那样这条门什么都能通过）；实际：{result['status']}"
    )
    assert result["facts"] == [], (
        f"期望：一次被拒的矩阵不得留下任何 travel 事实 —— 半张矩阵比没有矩阵更危险；"
        f"实际：{len(result['facts'])} 条"
    )
    assert result["route_provenance"] == "NONE" and result["time_basis"] == "NOT_CAPTURED", (
        f"期望：失败时来源标签回到「没有来源」，不得留着 LIVE_CAPTURE 的空壳；实际："
        f"{result['route_provenance']} / {result['time_basis']}"
    )
    assert len(result["nodes"]) == 11, (
        f"期望：被拒的是请求形状，不是行程内容 —— 节点清单必须原样保留；"
        f"实际：{len(result['nodes'])} 个"
    )
    assert result.get("error_type") == "ValhallaProviderError", (
        f"期望：error_type 点名受控异常，不是空串也不是裸 500；实际：{result.get('error_type')!r}"
    )
    assert len(probe.matrix_requests) == 1, (
        f"期望：第一片就被拒 ⇒ 不再对同一批节点补打 costing（今天 WALK 抛错直接结束）；"
        f"实际：{[item['costing'] for item in probe.matrix_requests]}"
    )


def test_g14b_no_request_may_carry_more_than_100_cells_and_every_pair_survives(monkeypatch):
    """分片是后端要交的活：先按 cells 预检，再按 ⌈N/10⌉² 拆片，一格都不许少。

    今天 ``runtime.py`` 把全部 key 一次塞进 ``provider.matrix(keys)``：N=20 ⇒ 400 cells ⇒
    400/150 ⇒ 整次捕获 FAILED。**这条门现在是红的**，它是收口条件，不许 skip，也不许改成
    「按错误文案里的 locations 数」预检 —— 那个字面数在方阵上同解、在矩形上判错。
    """

    probe = MatrixProbe()
    count = 20
    result = _capture(monkeypatch, probe, count)
    over = sorted({item["cells"] for item in probe.matrix_requests if item["cells"] > MAX_CELLS}, reverse=True)
    assert not over, (
        f"期望：每个发往 provider 的矩阵请求 ≤ {MAX_CELLS} 个 cells（实测 N=10 ⇒ 100 cells / 200 / "
        f"1018 ms；N=11 ⇒ 121 cells / 400 / error_code 150）；实际越界 cells：{over} —— "
        f"runtime.py:232 把 {count} 个节点一次发出 = {count * count} cells，provider 必然 400。"
        "预检要按 **cells = sources × targets** 算，不是按文案里那个字面 \"locations\"；"
        "出网前分片或明确拒绝都行，拿整趟行程去赌一次 400 不行"
    )
    assert result["status"] == "CAPTURED", (
        f"期望：{count} 个节点分片之后应当真的拿到事实；实际：{result['status']}"
    )
    costings = sorted({str(item["costing"]) for item in probe.matrix_requests})
    assert costings == ["auto", "pedestrian"], (
        f"期望：两种 costing 各成一套分片（runtime.py:216）；实际：{costings}"
    )
    expected_tiles = math.ceil(count / 10) ** 2
    tiles_by_costing = {
        costing: sum(1 for item in probe.matrix_requests if item["costing"] == costing)
        for costing in costings
    }
    assert all(tiles >= expected_tiles for tiles in tiles_by_costing.values()), (
        f"期望：每种 costing ≥ ⌈{count}/10⌉² = {expected_tiles} 片；实际：{tiles_by_costing} —— "
        "片数不足就是丢格，不是省下的请求"
    )
    keys = [str(node["key"]) for node in result["nodes"]]
    pairs = {(item["from_key"], item["to_key"], item["mode"]) for item in result["facts"]}
    missing = [
        (left, right, mode)
        for left in keys
        for right in keys
        for mode in ("WALK", "AUTO")
        if left != right and (left, right, mode) not in pairs
    ]
    assert not missing, (
        f"期望：分片后有序成对事实仍覆盖 {count}×{count - 1}×2 个组合；实际缺 {len(missing)} 格，"
        f"例如 {missing[:4]} —— 「只切第一片」能骗过上面那条 ≤100 cells，骗不过这条"
    )


def test_g14c_the_verdict_must_not_depend_on_the_providers_wording(monkeypatch):
    """上限是数字不是句子：换一句文案（并把 "locations" 一词去掉）判定必须一模一样。"""

    with_locations = MatrixProbe()
    reworded = MatrixProbe(
        error_body={"error_code": 150, "status_code": 400, "error": "リクエストが大きすぎます"}
    )
    assert "locations" in json.dumps(with_locations.error_body)
    assert "locations" not in json.dumps(reworded.error_body), "SETUP 失败：两份文案没真正不同"
    first = _capture(monkeypatch, with_locations, 11)
    second = _capture(monkeypatch, reworded, 11)
    assert with_locations.matrix_requests and reworded.matrix_requests, "SETUP 失败：一次请求都没发出"
    assert [item["cells"] for item in with_locations.matrix_requests] == [
        item["cells"] for item in reworded.matrix_requests
    ], "SETUP 失败：两次发出的形状不同，比较无从成立"
    for key in ("status", "error_type", "supported_modes", "route_provenance", "time_basis"):
        assert first[key] == second[key], (
            f"期望：分类只由 cells 数决定 —— provider 换一句话就换一个结论，等于把可诊断性外包给文案；"
            f"字段 {key}：{first[key]!r} vs {second[key]!r}"
        )
    assert first["facts"] == [] and second["facts"] == [], (
        f"期望：两种文案下都不留事实；实际：{len(first['facts'])} / {len(second['facts'])}"
    )


# ======================================================================================
# G15 · 直线距离不得冒充 travel_minutes
# ======================================================================================
def _app_source_files() -> list[Path]:
    return sorted(path for path in APP_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _function_graph() -> tuple[dict[tuple[str, str], set[str]], set[tuple[str, str]]]:
    """(调用边, 直接持有地球半径常数的函数)。模块内直接调用 + 全仓唯一名跨模块解析。"""

    edges: dict[tuple[str, str], set[str]] = {}
    radius_users: set[tuple[str, str]] = set()
    for path in _app_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = path.stem
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            owner = (module, node.name)
            called: set[str] = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name):
                        called.add(child.func.id)
                    elif isinstance(child.func, ast.Attribute):
                        called.add(child.func.attr)
                if isinstance(child, ast.Constant) and child.value in RADIUS_VALUES:
                    radius_users.add(owner)
            edges[owner] = called
    return edges, radius_users


def _suspects() -> list[str]:
    edges, radius_users = _function_graph()
    by_name: dict[str, list[tuple[str, str]]] = {}
    for owner in edges:
        by_name.setdefault(owner[1], []).append(owner)
    unique = {name: group[0] for name, group in by_name.items() if len(group) == 1}
    tainted = set(radius_users)
    grew = True
    while grew:
        grew = False
        for owner, called in edges.items():
            if owner in tainted:
                continue
            module = owner[0]
            for callee in called:
                same_module = (module, callee) in tainted
                elsewhere = callee in unique and unique[callee] != owner and unique[callee] in tainted
                if same_module or elsewhere:
                    tainted.add(owner)
                    grew = True
                    break
    return sorted(f"{module}.{name}" for module, name in tainted if MINUTE_LIKE.search(name))


def test_g15b_no_great_circle_helper_reaches_a_travel_time_name():
    """地球半径只许待在「距离」里：任何产出 travel/duration/minute 的函数都不许够到它。

    今天实测的持有者是 ``real_city._distance_m`` 与 ``impact_validation._haversine_meters``，
    两者返回米。``stay_mobility._travel_minutes`` 刻意不在这张图上 —— 它只问 ``_mode_fact``。
    这张图是一根绊线（改名能绕过它），真正的防线是 G15a/G15c 的行为判据。
    """

    _, radius_users = _function_graph()
    assert radius_users, (
        "SETUP 失败：app/ 里已经找不到地球半径常数了 —— 这条绊线失去可绕过的对象，"
        "请连同这条门一起删掉，不要留一扇空门"
    )
    offenders = _suspects()
    assert not offenders, (
        f"期望：直线距离（地球半径）不得流进任何名字含 travel/duration/minute 的产出；"
        f"实际：{offenders} —— 距离除一个速度常量就是编出来的 travel_minutes。"
        "缺这条腿应该说 MOBILITY_DATA_UNAVAILABLE（G15a/G15c），不该给一个看着合理的分钟数"
    )
    for module, name in sorted(radius_users):
        assert not MINUTE_LIKE.search(name), (
            f"期望：直接持有半径常数的 {module}.{name}() 自己是「距离」口径；"
            "实际它的名字里就带着时长"
        )


def test_g15a_a_leg_with_no_sourced_fact_has_no_number_at_any_door():
    """三扇门：来源 allowlist、replan 取值口、可行性闸 —— 都只能说「没有」，不能说「大约」。"""

    pair = ("stop-a", "stop-b")
    #: 800 m 直线 ÷ 4.7 km/h ≈ 6 分钟 —— 正是最容易被"顺手估一下"塞进来的那个值。
    for quality in (None, SYNTHETIC_DEFAULT, "LEGACY_UNTAGGED"):
        snapshot = MobilitySnapshot.from_minutes(
            source_provider_key="lane-f-fake",
            minutes={pair: 6},
            captured_at="2026-09-21T00:00:00+00:00",
            fact_quality=quality,
        )
        assert snapshot.estimate_minutes(*pair) == 6, (
            "SETUP 失败：原始访问器连值都取不到，下面的 allowlist 判据就无从对比 —— 空门"
        )
        with pytest.raises(KeyError) as refused:
            snapshot.hard_minutes(*pair)
        assert "not eligible" in str(refused.value), (
            f"期望：来源标签 {quality!r} 的腿被硬可行性拒绝，理由是「来源不合格」；"
            f"实际：{refused.value}"
        )
    for quality in (LIVE_PROVIDER_CAPTURE, EXPLICIT_FROZEN_FIXTURE):
        admitted = MobilitySnapshot.from_minutes(
            source_provider_key="lane-f-fake",
            minutes={pair: 6},
            captured_at="2026-09-21T00:00:00+00:00",
            fact_quality=quality,
        )
        assert admitted.hard_minutes(*pair) == 6, (
            f"期望：{quality} 的腿正常放行 —— allowlist 若是空的，上面那条拒绝就成了巧合通过"
        )

    with SessionLocal() as session:
        journey_id = _seeded_active_journey_id()
        journey = session.get(Journey, journey_id)
        assert journey is not None, f"SETUP 失败：取不到旅程 {journey_id}"
        empty = MobilitySnapshot(
            facts={},
            source_provider_key="lane-f-empty",
            captured_at="2026-09-21T00:00:00+00:00",
        )
        with pytest.raises(MobilityDataUnavailable) as unavailable:
            _replan_estimate(
                session,
                journey,
                empty,
                pair[0],
                pair[1],
                operation_key="lane-f-matrix-truth",
            )
        assert str(unavailable.value) == "MOBILITY_DATA_UNAVAILABLE:stop-a|stop-b", (
            f"期望：逐腿点名两端 —— 这条腿不可用，不是整个行程不可规划；实际：{unavailable.value}"
        )

    missing = _evaluate_leg({})
    assert "MOBILITY_DATA_UNAVAILABLE:stop-a|stop-b" in missing.failures, (
        f"期望：可行性闸对缺的腿逐腿点名；实际：{missing.failures}"
    )
    assert not any(item.startswith("INSUFFICIENT_TRAVEL_GAP") for item in missing.failures), (
        f"期望：没有事实时不得判「来不及」（那需要先有一个数）；实际：{missing.failures}"
    )
    sealed = _evaluate_leg({pair: 20}, quality=EXPLICIT_FROZEN_FIXTURE)
    assert not any(item.startswith("MOBILITY_DATA_UNAVAILABLE") for item in sealed.failures), (
        f"SETUP 失败：补上带来源的腿之后仍报不可用 —— 上面那半是巧合通过；实际：{sealed.failures}"
    )


def _evaluate_leg(minutes: Mapping[tuple[str, str], int], *, quality: str | None = None):
    ids = ["stop-a", "stop-b"]
    snapshot = MobilitySnapshot.from_minutes(
        source_provider_key="lane-f-gate",
        minutes={tuple(key): int(value) for key, value in minutes.items()},
        captured_at="2026-09-21T00:00:00+00:00",
        fact_quality=quality,
    )
    tz = timezone(timedelta(hours=8))
    start = datetime(2026, 11, 10, 9, 0, tzinfo=tz)
    activities = [
        FeasibilityActivity(
            key="stop-a",
            place_id="stop-a",
            start_at=start,
            end_at=start + timedelta(minutes=60),
            cost_minor=0,
        ),
        FeasibilityActivity(
            key="stop-b",
            place_id="stop-b",
            start_at=start + timedelta(minutes=100),
            end_at=start + timedelta(minutes=160),
            cost_minor=0,
        ),
    ]
    context = FeasibilityContext(
        journey_id="lane-f-journey",
        city_code="CN-BEIJING",
        journey_start=start - timedelta(hours=2),
        journey_end=start + timedelta(hours=12),
        budget_amount_minor=1_000_000,
        budget_currency="CNY",
        constraints=(),
        places={
            item: {
                "id": item,
                "name": item,
                "latitude": 39.9,
                "longitude": 116.4,
                "category": "SIGHT",
            }
            for item in ids
        },
        services={},
        mobility_provider=snapshot,
        plan_currency="CNY",
    )
    return evaluate_feasibility(context, activities)


def test_g15c_coverage_counts_the_unusable_leg_instead_of_guessing_it():
    """投影只数「有据可查」的腿：写成直线估算值的那一条，必须从 eligible 里掉出去。"""

    with SessionLocal() as session:
        journey_id = _seeded_active_journey_id()
        plan = _active_plan(session, journey_id)
        keys = _plan_routing_keys(session, plan)
        legs = [(left, right) for left, right in zip(keys, keys[1:]) if left != right]
        assert len(legs) >= 2, (
            f"SETUP 失败：这条行程只有 {len(legs)} 条腿，差一就是噪声 —— "
            "请让舞台 seed 一趟两站以上的行程，不要放宽判据"
        )

        all_sealed = _coverage_of(session, plan, _evidence(plan, legs, {pair: EXPLICIT_FROZEN_FIXTURE for pair in legs}))
        assert all_sealed["legs_total"] == len(legs), (
            f"期望：legs_total 按行程自身的执行顺序数出 {len(legs)}；实际：{all_sealed['legs_total']}"
        )
        assert all_sealed["legs_hard_eligible"] == len(legs), (
            f"SETUP 失败：全部来源合格时 eligible 应为 {len(legs)}，实际 "
            f"{all_sealed['legs_hard_eligible']} —— 基线不对，下面的「差一」判据没有意义"
        )

        victim = legs[0]
        one_fabricated = _coverage_of(
            session,
            plan,
            _evidence(
                plan,
                legs,
                {
                    pair: SYNTHETIC_DEFAULT if pair == victim else EXPLICIT_FROZEN_FIXTURE
                    for pair in legs
                },
                fabricate={victim},
            ),
        )
        assert one_fabricated["legs_total"] == len(legs), (
            "期望：换证据来源不改行程本身 —— legs_total 必须不动；"
            f"实际：{one_fabricated['legs_total']} vs {len(legs)}"
        )
        assert one_fabricated["legs_hard_eligible"] == len(legs) - 1, (
            f"期望：把 {victim[0]} → {victim[1]} 这条腿的值写成 6（800 m 直线除速度的结果）、"
            f"标签换成 {SYNTHETIC_DEFAULT}，eligible 应从 {len(legs)} 掉到 {len(legs) - 1}；"
            f"实际：{one_fabricated['legs_hard_eligible']} —— 掉不到就说明投影相信了一个没有来源的"
            "分钟数，那正是「直线距离冒充 travel_minutes」"
        )

        one_absent = _coverage_of(
            session,
            plan,
            _evidence(
                plan,
                legs,
                {pair: EXPLICIT_FROZEN_FIXTURE for pair in legs if pair != victim},
                drop={victim},
            ),
        )
        assert one_absent["legs_hard_eligible"] == len(legs) - 1, (
            f"期望：『这条腿根本没有事实』与『有事实但来源不合格』在投影里等价（都算未覆盖）—— "
            f"absent 应为 {len(legs) - 1}；实际：{one_absent['legs_hard_eligible']}，"
            f"对照 fabricated={one_fabricated['legs_hard_eligible']}。缺一条还给出数，就是编的"
        )

        all_fabricated = _coverage_of(
            session,
            plan,
            _evidence(plan, legs, {pair: SYNTHETIC_DEFAULT for pair in legs}, fabricate=set(legs)),
        )
        assert all_fabricated["legs_hard_eligible"] == 0, (
            f"期望：整趟都没有合格事实时 eligible 归 0（面板就该说 mobility unavailable）；"
            f"实际：{all_fabricated['legs_hard_eligible']} / 总 {all_fabricated['legs_total']} —— "
            "只要有一个数留下，它就是编的"
        )
        assert all_fabricated["legs_total"] == len(legs), "期望：全部未覆盖也不改腿的总数"
        session.rollback()


def test_g15d_the_scene_read_reports_the_same_two_counts():
    """面板只能从这两个数说话：读端点不得自己凑一个「一切正常」。"""

    with SessionLocal() as session:
        journey_id = _seeded_active_journey_id()
        plan = _active_plan(session, journey_id)
        plan_id = str(plan.id)
        coverage = _mobility_evidence_coverage(session, plan)
    scene = client.get("/api/v1/demo/scene")
    assert scene.status_code == 200, f"SETUP 失败：/api/v1/demo/scene 实际 {scene.status_code}"
    body = scene.json()
    assert body.get("plan_version_id") == plan_id, (
        f"SETUP 失败：scene 说的是另一个方案（{body.get('plan_version_id')!r} ≠ {plan_id!r}），"
        "下面的两数比较就成了张冠李戴"
    )
    for field, value in (
        ("mobility_legs_total", coverage["legs_total"]),
        ("mobility_legs_hard_eligible", coverage["legs_hard_eligible"]),
        ("evidence_snapshot_id", coverage["snapshot_id"]),
        ("evidence_reference_consistent", coverage["reference_consistent"]),
    ):
        assert body.get(field) == value, (
            f"期望：scene 的 {field} 就是投影的那一个（{value!r}），读端点不重算不修饰；"
            f"实际：{body.get(field)!r}"
        )
    assert coverage["legs_hard_eligible"] <= coverage["legs_total"], (
        f"期望：eligible ≤ total；实际：{coverage['legs_hard_eligible']} > {coverage['legs_total']}"
    )


def _seeded_active_journey_id() -> str:
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, f"SETUP 失败：/api/v1/demo/reset 实际 {reset.status_code}"
    scene = client.get("/api/v1/demo/scene")
    assert scene.status_code == 200, f"SETUP 失败：/api/v1/demo/scene 实际 {scene.status_code}"
    journey_id = scene.json().get("journey_id")
    assert journey_id, "SETUP 失败：reset 之后 scene 里没有 journey_id"
    return str(journey_id)


def _active_plan(session, journey_id: str) -> PlanVersion:
    """舞台 seed 出来的旅程没有方案：走真实路由生成并选中，别手搓一张证据表当基线。"""

    journey = session.get(Journey, journey_id)
    assert journey is not None, f"SETUP 失败：旅程 {journey_id} 不存在"
    if not journey.active_plan_version_id:
        generated = client.post(
            f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
        )
        assert generated.status_code == 201, (
            f"SETUP 失败：plans/generate 期望 201，实际 {generated.status_code} / "
            f"{generated.text[:180]}"
        )
        candidates = generated.json()
        first = candidates[0] if isinstance(candidates, list) else candidates
        plan_id = str(first.get("id") or "")
        assert plan_id, f"SETUP 失败：生成结果里没有 plan id：{str(first)[:180]}"
        selected = client.post(f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select")
        assert selected.status_code == 200, (
            f"SETUP 失败：plans/{{id}}/select 期望 200，实际 {selected.status_code} / "
            f"{selected.text[:180]}"
        )
        session.expire_all()
        journey = session.get(Journey, journey_id)
    assert journey is not None and journey.active_plan_version_id, (
        "SETUP 失败：生成并选中之后仍然没有 active_plan_version_id —— 计数门无从谈起，"
        "请修舞台，不要放宽判据"
    )
    plan = session.get(PlanVersion, journey.active_plan_version_id)
    assert plan is not None, "SETUP 失败：active_plan_version_id 指向一个不存在的方案"
    return plan


def _plan_routing_keys(session, plan: PlanVersion) -> list[str]:
    activities = session.scalars(
        select(Activity)
        .where(Activity.plan_version_id == plan.id)
        .order_by(Activity.sequence_no)
    ).all()
    return [str(key) for key in (_activity_routing_key(item) for item in activities) if key]


def _evidence(
    plan: PlanVersion,
    legs: list[tuple[str, str]],
    qualities: Mapping[tuple[str, str], str],
    *,
    fabricate: set[tuple[str, str]] | None = None,
    drop: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """按这趟行程自己的腿，现造一份密封证据；每格来源标签由调用方指定。"""

    fabricate = fabricate or set()
    drop = drop or set()
    facts = []
    for pair in legs:
        if pair in drop:
            continue
        quality = qualities.get(pair)
        facts.append(
            {
                "origin_key": pair[0],
                "destination_key": pair[1],
                "minutes": 6 if pair in fabricate else 20,
                "provider": "lane-f-authored",
                "captured_at": "2026-09-21T00:00:00+00:00",
                "degraded": False,
                "fact_quality": quality,
            }
        )
    return {
        "snapshot_id": plan.mobility_snapshot_id or "lane-f-authored-snapshot",
        "source_provider": "lane-f-authored",
        "captured_at": "2026-09-21T00:00:00+00:00",
        "degraded": False,
        "facts": facts,
    }


def _coverage_of(session, plan: PlanVersion, evidence: dict[str, Any]) -> dict[str, Any]:
    plan.mobility_evidence_json = json.dumps(evidence, ensure_ascii=False)
    session.flush()
    return _mobility_evidence_coverage(session, plan)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
