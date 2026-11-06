"""ORD-0106 顺手② · 车道 F - 库里有的城市，不得因为多打了一个「市」就成了没有能力的地方。

Commander 在 `fed1861` 量到 `GET /destinations/capability?query=CN-BEIJING` 报 candidate_count=0、
六轴全 MISSING。我在 `4922f6f` 复跑，读数不一样，但**同一件事的另一种写法一样成立**，而且更常见：

    query=北京   -> destination_resolution=catalog, place_count=8, capability=HYDRATABLE
    query=北京市 -> destination_resolution=unresolved, place_count=0, 六轴全 MISSING,
                    capability=UNAVAILABLE, canonical_destination_id=unresolved:15ef4634d57e865f

机制（只读不猜，行号给全）：``anywhere_capability.py:186-194`` 的别名集合是
{city_code, 去掉 CN- 前缀, `_CITY_DISPLAY_NAMES` 里那条光杆名字}，而 ``_normalise``（:143-145）
只去空白/连字符/句点，**不去行政区后缀**；``_TRAVELER_FACING_LABELS``（:119-121）今天只有
CNX-DEMO 一条。旅客与一切官方名称写的正是「北京市/上海市/天津市」，于是走到 :217-226 第 3 步：
拿查询文本 sha1 造一个 ``unresolved:<digest>`` 身份，再把结构性结论报成 UNAVAILABLE。

三道判据都红在今天的实现上，且都不要求车道 C 支持"任何拼法"——只要求：
库里已经有地点的那座城市，它的**两种写法必须问出同一个身份、同一份覆盖度、同一个 capability**。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.anywhere_capability import LEGACY_PENDING_PREFIX
from app.db.models import Place
from app.db.session import SessionLocal
from app.main import _CITY_DISPLAY_NAMES, app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)

COVERAGE_AXES = ("attractions", "food", "nightlife", "lodging", "mobility", "booking")
#: 行政区后缀：旅客最常跟着打的那一个字。
SUFFIXES = ("市", "市 ", " 市")


def _stage() -> None:
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, f"SETUP 失败：/api/v1/demo/reset 实际 {reset.status_code}"


def _held_city_counts() -> dict[str, int]:
    with SessionLocal() as session:
        rows = session.execute(
            select(Place.city_code, func.count(Place.id)).group_by(Place.city_code)
        ).all()
    return {str(code): int(count or 0) for code, count in rows if code}


def _capability(query: str) -> dict[str, object]:
    response = client.get("/api/v1/destinations/capability", params={"query": query})
    assert response.status_code == 200, (
        f"SETUP 失败：capability({query!r}) 期望 200，实际 {response.status_code} / "
        f"{response.text[:180]}"
    )
    return response.json()


def _municipality_cases() -> list[tuple[str, str, str, int]]:
    """(city_code, 裸名, 带「市」的写法, 库里地点数) —— 只取库里真有行的那几座。"""

    _stage()
    held = _held_city_counts()
    assert held, "SETUP 失败：库里一个 Place 都没有，这条门无从对照（请修舞台，不要放宽判据）"
    cases: list[tuple[str, str, str, int]] = []
    for city_code, label in sorted(_CITY_DISPLAY_NAMES.items()):
        code = f"CN-{label and city_code}"
        if code not in held or held[code] <= 0:
            continue
        bare = _capability(str(label))
        if bare.get("destination_resolution") != "catalog" or bare.get("canonical_destination_id") != code:
            # 裸名都问不出目录身份，说明这座城不在这条门的对照面上（例如只靠别名命中的 CNX-DEMO）。
            continue
        cases.append((code, str(label), f"{label}市", int(held[code])))
    assert cases, (
        f"SETUP 失败：库里带行的城市（{sorted(held)}）里没有一个能用裸名解析到目录身份 —— "
        "对照面塌了，这条门会假绿；请把舞台 seed 修好，不要放宽判据"
    )
    return cases


# --------------------------------------------------------------------------------------
# G19a · 库里有的城市，覆盖度不得因为写法而归零
# --------------------------------------------------------------------------------------
def test_g19a_a_catalogued_city_must_not_answer_zero_coverage_under_its_official_name():
    """同一个 city_code、库里真有 8 行地点，换一种写法就六轴全 MISSING ⇒ 红。"""

    for city_code, label, suffixed, rows in _municipality_cases():
        plain = _capability(label)
        variant = _capability(suffixed)
        assert int(plain.get("place_count") or 0) == rows, (
            f"SETUP 失败：裸名 {label!r} 报 place_count={plain.get('place_count')!r}，"
            f"而库里 {city_code} 实有 {rows} 行 —— 对照基线就不对，后面的比较没有意义"
        )
        all_missing = [
            axis for axis in COVERAGE_AXES
            if (variant.get("coverage") or {}).get(axis) == "MISSING"
        ]
        assert int(variant.get("place_count") or 0) > 0 or all_missing != list(COVERAGE_AXES), (
            f"期望：{city_code} 在库里有 {rows} 个地点，那么问「{suffixed}」（{label} 的官方全称写法）"
            f"不得报 place_count=0 且六轴全 MISSING；实际：place_count="
            f"{variant.get('place_count')!r}、MISSING 轴 {all_missing}、capability="
            f"{variant.get('capability')!r}、destination_resolution="
            f"{variant.get('destination_resolution')!r} —— 而同一个 city_code 问「{label}」得到 "
            f"place_count={plain.get('place_count')!r}、capability={plain.get('capability')!r}。"
            "根因 anywhere_capability.py:186-194 的别名集合不含行政区后缀变体，"
            "_normalise(:143) 也不处理后缀；屏幕于是对一座有 8 个地点的城说「这里什么都没有」"
        )


# --------------------------------------------------------------------------------------
# G19b · 一处地方一个身份：不得凭查询文本 mint 一个新 canonical id
# --------------------------------------------------------------------------------------
def test_g19b_two_spellings_of_one_city_must_not_mint_two_canonical_identities():
    """文本派生的身份会被当成目的地身份用：指纹、幂等、并发收敛全建在它上面。

    `23bdf9f` 之后产品不再写 sha1 摘要，换成了 ``pending:<归一化查询原文>``——摘要那半解决了，
    **"同一条问句的两种拼法必须是同一个身份"没解决**，所以这条门仍该红；它现在盯的是原文进身份
    （仲裁：raw text is not identity），断言因此比原来更强：无论摘要还是原文都不许当身份。
    """

    for city_code, label, suffixed, rows in _municipality_cases():
        plain = _capability(label)
        variant = _capability(suffixed)
        assert not str(variant.get("canonical_destination_id") or "").startswith(
            LEGACY_PENDING_PREFIX
        ), (
            f"期望：库里 {city_code} 有 {rows} 行地点时，问「{suffixed}」不得拿到一个由查询文本"
            f"派生的身份（无论摘要还是原文）；实际：canonical_destination_id="
            f"{variant.get('canonical_destination_id')!r}（method=unresolved）。§17D/G3 要的是"
            "**同指纹收敛到同一操作**，而文本派生的身份会让 北京/北京市 被当成两座城市去水合、"
            "去幂等、去缓存"
        )
        assert variant.get("canonical_destination_id") == plain.get("canonical_destination_id"), (
            f"期望：两种写法同一个 canonical_destination_id（{plain.get('canonical_destination_id')!r}）；"
            f"实际：「{label}」→ {plain.get('canonical_destination_id')!r}，"
            f"「{suffixed}」→ {variant.get('canonical_destination_id')!r}"
        )
        assert variant.get("fingerprint") == plain.get("fingerprint"), (
            f"期望：指纹四段（canonical_destination_id + 两个版本 + provider_policy）在两种写法下同值；"
            f"实际：{str(plain.get('fingerprint'))[:60]!r} vs {str(variant.get('fingerprint'))[:60]!r}"
        )


# --------------------------------------------------------------------------------------
# G19c · 结构性能力不是拼写的函数（仲裁 R1：运行时事实不得洗成结构结论）
# --------------------------------------------------------------------------------------
def test_g19c_structural_capability_must_not_depend_on_how_the_city_is_spelled(monkeypatch):
    """「认不出这个写法」是查询侧的运行时事实，不能被报成"这座城市永远做不了"。"""

    for city_code, label, suffixed, rows in _municipality_cases():
        plain = _capability(label)
        variant = _capability(suffixed)
        assert variant.get("capability") == plain.get("capability"), (
            f"期望：同一 city_code 的 capability 与写法无关（结构性三态按仲裁 v0.2 只描述结构能力）；"
            f"实际：「{label}」= {plain.get('capability')!r}，「{suffixed}」= "
            f"{variant.get('capability')!r}，库里有 {rows} 行地点。"
            "把解析未命中报成 UNAVAILABLE 是 R1 禁的那类 laundering：用户会以为这地方没救，"
            "而正确动作是 HYDRATABLE + 具名 reason（NO_CANONICAL_IDENTITY 或补别名）"
        )
        assert variant.get("capability") != "UNAVAILABLE", (
            f"期望：库里真有 {rows} 行地点的城市不得被判定为结构性 UNAVAILABLE；实际："
            f"「{suffixed}」→ UNAVAILABLE，missing_categories={variant.get('missing_categories')}"
        )


# --------------------------------------------------------------------------------------
# G19d · 门的对照面：capability 归零只能发生在库里真没有地点的时候
# --------------------------------------------------------------------------------------
def test_g19d_zero_coverage_is_only_legitimate_when_the_store_really_has_nothing():
    """反向半边，防止有人把修法做成"一律回 8 行"：真的空城必须照样报零。"""

    _stage()
    held = _held_city_counts()
    #: 产品词表里能命名、但库里没有行的城市 —— 这才是 MISSING/零覆盖该出现的形状。
    empty_code, empty_label = next(
        (
            (f"CN-{code}", str(label))
            for code, label in sorted(_CITY_DISPLAY_NAMES.items())
            if f"CN-{code}" not in held
        ),
        (None, None),
    )
    assert empty_code, (
        f"SETUP 失败：词表里每一座城在库里都有行（{sorted(held)}）—— 这条反向判据失去对照面，"
        "请删一条 seed 或换一台舞台，不要用 skip 绕过"
    )
    answer = _capability(empty_label)
    assert int(answer.get("place_count") or 0) == 0, (
        f"期望：库里没有 {empty_code} 的地点时 place_count 就是 0（不许为了绿 G19a 而编数）；"
        f"实际：{answer.get('place_count')!r}"
    )
    assert answer.get("destination_resolution") in {"vocabulary", "unresolved"}, (
        f"期望：一座没行的城不能凭 catalog 身份回答；实际："
        f"{answer.get('destination_resolution')!r} / {answer.get('canonical_destination_id')!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
