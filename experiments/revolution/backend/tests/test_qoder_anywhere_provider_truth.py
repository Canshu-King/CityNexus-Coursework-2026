"""ORD-0106 lane F - the two lessons lane B paid for, plus the count-is-not-capability rule.

Nothing in this file reaches the internet: every provider shape is a constructed exception or
an in-process fake response. That is §17C's own rule applied to the doors that were written
*because* of a provider incident, so it would be embarrassing for the guard to need a network.

Three doors:

* G10 坐标完整性 - B measured that an Overpass `out center` verbosity **does not carry node
  coordinates**: the first version got 11 of 24 rows back, every missing row was a node, and
  **nothing raised an error**. A silent drop of 13 places is the same family as a 200 that
  means "no results", so the rule here is: node / way / relation must all come out with a
  usable coordinate, and the row count that goes in must equal the count that comes out.
* G11 失败码不得塌缩 - B measured that 429 and 5xx both collapse into `HTTPStatusError`, a
  timeout is `ReadTimeout`, a reset is `RemoteProtocolError`, a malformed body is
  `RuntimeError`, and none of them can be told apart by the `<?xml` prefix. Six shapes must
  produce six *stable* codes and no exception class name may reach a user-facing field.
  This runs against lane C's shipped mapping (`live_failure_reason`, d481464), so it can bite.
* G12 数量不能当能力 - B measured 塔什库尔干: 255 elements, **72% `natural=peak`**, BAR=0.
  A big element count with empty nightlife is not READY. Per arbiter v0.2, READY must come
  from a versioned server-side readiness policy, never from `places.length > 0`.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.integrations.discovery.real_city import (
    LIVE_FAILURE_REASONS,
    live_failure_reason,
)
from app.main import app

client = TestClient(app)

CAPABILITY = "/api/v1/destinations/capability"
HYDRATIONS = "/api/v1/catalog-hydrations"
TAQORGAN = "塔什库尔干塔吉克自治县"
UNSEEN_COUNTY = "贡山独龙族怒族自治县"


def _router_absent(response) -> bool:
    if response.status_code != 404:
        return False
    try:
        return response.json().get("detail") == "Not Found"
    except Exception:  # noqa: BLE001
        return False


def _summary(response) -> str:
    try:
        return f"{response.status_code} / {str(response.json())[:200]}"
    except Exception:  # noqa: BLE001
        return f"{response.status_code} / <non-json>"


def _require(operation: str, response) -> None:
    assert not _router_absent(response), (
        f"期望：{operation} 已实现（这条门量的是它规范化后的产物）；实际：{_summary(response)} —— "
        "路由级 404。这条红是给车道 C 的，不是环境噪声；不许 skip，也不许改成量一个不存在的私有函数"
    )


# --------------------------------------------------------------------------------------
# G10 · 坐标完整性：node / way / relation 都必须带可用坐标，一行都不许静默丢
# --------------------------------------------------------------------------------------
def test_g10_every_normalised_place_carries_a_usable_coordinate_including_nodes():
    """§17 教训一：verbosity 不吃 node 坐标 ⇒ 11/24 行、无报错，就是静默丢数据。"""
    _require("POST /api/v1/catalog-hydrations", client.post(HYDRATIONS, json={"query": UNSEEN_COUNTY}))
    # 上面这条受理之后才有得量：水合产出的地点集合必须逐 kind 有坐标。
    listing = client.get(CAPABILITY, params={"query": UNSEEN_COUNTY})
    _require("GET /api/v1/destinations/capability", listing)
    body = listing.json()
    places = body.get("places") or body.get("catalog") or []
    assert places, (
        f"期望：规范化目录里至少有地点可查（B 实测 Overpass `out center` 会返回 node/way/relation 三类）；"
        f"实际：空，响应键 {sorted(body)[:12]}"
    )

    def _coordinate(item: dict):
        for key in ("latitude", "longitude"):
            if item.get(key) is None:
                return None
        return (item["latitude"], item["longitude"])

    kinds = {}
    missing: list[str] = []
    for item in places:
        kind = str(item.get("source_kind_geometry") or item.get("geometry") or item.get("osm_type") or "UNKNOWN")
        kinds[kind] = kinds.get(kind, 0) + 1
        if _coordinate(item) is None:
            missing.append(f"{kind}:{item.get('source_identity') or item.get('id') or '?'}")
    assert not missing, (
        f"期望：三类要素（node/way/relation）每一个都有可用坐标，一行都不许静默少 —— "
        f"B 的实测形状是 24 行里只有 11 行、差的 13 行全是 node 且**没有任何报错**；"
        f"实际：{len(missing)} 行没坐标：{missing[:6]}"
    )
    for kind in ("node", "way", "relation"):
        assert kinds.get(kind, 0) > 0, (
            f"期望：规范化结果里 {kind} 类存在（这正是被静默丢掉的那一类）；实际分布 {kinds}"
        )
    declared = body.get("discovered_count")
    if declared is not None:
        assert declared == len(places), (
            f"期望：进多少行出多少行（缺行必须说出来，不能少一行还报成功）；"
            f"实际：声明 {declared}，实到 {len(places)}"
        )


# --------------------------------------------------------------------------------------
# G11 · 失败码不得塌缩：六种形状 ⇒ 六个稳定 reason_code，用户面不得出现异常类名
# --------------------------------------------------------------------------------------
def _request() -> httpx.Request:
    """一个用不到的地址：这些异常对象只为让映射函数读到 status_code，不发出任何请求。

    刻意不写 provider 域名 —— §17C 的护栏（G8a）扫的是"字面量旁边有调用形状"，
    而这条门不该让护栏去为它开例外；放宽护栏比换个 URL 贵得多。
    """

    return httpx.Request("GET", "http://provider-shape.local/api/interpreter")


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = _request()
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=httpx.Response(code, request=request))


SHAPES = [
    ("429 限流", _status_error(429)),
    ("5xx 服务器错误", _status_error(503)),
    ("读取超时", httpx.ReadTimeout("timed out", request=_request())),
    ("连接被重置", httpx.RemoteProtocolError("peer closed connection without sending complete message body")),
    ("结构非法（可解析但不是我们的形状）", RuntimeError("malformed payload: <?xml version='1.0'?><error>")),
    ("零结果（200 但一个要素都没有）", RuntimeError("NO_JOURNEY_DERIVED_CITY_ANCHOR")),
]


def test_g11_six_provider_shapes_must_not_collapse_into_one_reason_code():
    """§17 教训二：B 实测它们今天全挤在 HTTPStatusError / Timeout / Protocol / RuntimeError 里。"""
    mapped = {}
    for label, error in SHAPES:
        code = live_failure_reason(error)
        assert code in LIVE_FAILURE_REASONS, (
            f"期望：{label} 归到已登记的稳定 reason_code（不是裸字符串）；实际：{code!r}"
        )
        assert "Error" not in code and "Exception" not in code, (
            f"期望：reason_code 里不得出现异常类名（用户面不得被要求读 Java 式类名）；实际：{code!r}"
        )
        mapped.setdefault(code, []).append(label)

    collapsed = {code: labels for code, labels in mapped.items() if len(labels) > 1}
    assert not collapsed, (
        f"期望：{len(SHAPES)} 种形状 ⇒ {len(SHAPES)} 个不同 reason_code（§14：塌缩会毁掉可诊断性，"
        f"运维与界面都没法分开处理『该重试』与『数据坏了』）；"
        f"实际塌缩：{ {code: labels for code, labels in collapsed.items()} }。"
        "现状读数：5xx 与 非法结构 都落 PROVIDER_BAD_RESPONSE；零结果只能靠一个异常里的哨兵字符串拿到 "
        "PROVIDER_NO_RESULTS —— 真 200 空要素不是异常，走不到这条码。请车道 C 按形状补码，"
        "不要反过来放宽这条门"
    )


def test_g11b_user_facing_reason_never_quotes_an_exception_class():
    """Same measurement from the other side: what a screen is allowed to render."""
    for label, error in SHAPES:
        code = live_failure_reason(error)
        assert not any(cls in code for cls in ("HTTPStatusError", "ReadTimeout", "RemoteProtocolError", "RuntimeError")), (
            f"期望：{label} 的可分支代码里没有客户端类名（它会随 HTTP 库变化，也是 §14 禁止的原始堆栈回声）；"
            f"实际：{code!r}"
        )


# --------------------------------------------------------------------------------------
# G12 · 数量不是能力：72% natural=peak、BAR=0 的 255 个要素不得判成 READY
# --------------------------------------------------------------------------------------
def test_g12_a_large_element_count_cannot_promote_a_destination_to_ready():
    """仲裁 v0.2：READY 出自带版本的 readiness_policy 判定，不是 places.length > 0。"""
    listing = client.get(CAPABILITY, params={"query": TAQORGAN})
    _require("GET /api/v1/destinations/capability", listing)
    body = listing.json()
    coverage = body.get("coverage") or {}
    assert coverage, (
        f"期望：能力回答带六分类覆盖度（否则 READY 无从判起）；实际键 {sorted(body)[:12]}"
    )
    non_ready = {key: value for key, value in coverage.items() if value not in {"READY"}}
    if non_ready:
        assert body.get("capability") != "READY", (
            f"期望：只要有一个分类不是 READY 就不能整体 READY（B 实测 {TAQORGAN}：255 要素、"
            f"72% natural=peak、BAR=0 ⇒ 夜生活为空不是 READY）；"
            f"实际：capability={body.get('capability')!r} 而 {non_ready}"
        )
    if body.get("capability") == "READY":
        assert body.get("readiness_policy_version"), (
            "期望：READY 说得出判它的是哪一版 readiness_policy（v0.2 禁的就是无版本魔法数）；"
            f"实际：字段缺失，键 {sorted(body)[:12]}"
        )
        total = body.get("element_count")
        if total is not None:
            assert total > 0, (
                f"期望：数量只做诊断、不做判据；实际：READY 且 element_count={total!r}"
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
