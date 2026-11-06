"""ORD-0236 · 契约门：`RAW_TAGS_ARE_NOT_HARD_EVIDENCE`（仲裁选 (b)，写死契约，不改 evaluator）。

冻结契约（仲裁原文）：
```text
Place.tags_json: display / discovery / taxonomy / raw-provider retention, NOT an evaluator truth source
accessibility evaluator: consumes normalized evidence only
```
所以这三条门钉的是**边界**，不是偶然实现：求值方读什么、不读什么。每条门配自己的杀手（K1/K2/K3），
且每条都带正控——没有正控的"读不到"永真。

契约本身不改任何产品码：本文件 0 处修改 `app/`，只读地喂两种通道并断言差别。
"""

import json

from sqlalchemy import select

from app.db.models import Place
from app.db.session import SessionLocal, ensure_schema
from app.integrations.mock.providers import MockCityDataProvider
from app.replanning.feasibility import _accessibility_metadata, _capability_failure_for_record
from app.services import seed_demo

# 求值方读的"规范化产物"通道（metadata_json 之内）
NORMALIZED_KEYS = ("accessibility", "accessibility_evidence", "osm_tags", "tags")
HARD_CODES = ("STROLLER", "LARGE_LUGGAGE", "STEP_FREE_REQUIRED")
# T3 那条库存等式的范围声明（仲裁 B-3：只属于 sealed-demo inventory，real provider 产证据时按
# provenance/city 分表，不许悄悄把它扩成全产品库存表）。这是"比哪一格"的声明，不是真值名单——
# 两侧集合仍然当场从 fixture 与库里算。
SEALED_DEMO_CITY = "CNX-DEMO"


class _Record:
    """一个地点行的最小替身：只有求值方会去看的那几个字段。"""

    def __init__(self, metadata: str = "{}", tags: str = "[]"):
        self.metadata_json = metadata
        self.tags_json = tags
        self.opening_hours_json = "{}"


def _verdicts(record) -> dict:
    return {code: _capability_failure_for_record(code, record, "place-under-test") for code in HARD_CODES}


def _names(verdicts: dict) -> str:
    return " ".join(failure for failures in verdicts.values() for failure in failures)


RAW_RETENTION_TAGS = json.dumps([
    "stroller_accessible=no", "luggage_accessible=no", "wheelchair=no", "highway=steps", "step_count=12",
])
GENERIC_TAXONOMY_TAGS = json.dumps(["culture", "indoor", "quiet", "outdoor", "bars", "history"])


# ------------------------------------------------------------------------------------------
# T1
# ------------------------------------------------------------------------------------------
def test_t1_raw_tags_column_alone_never_causes_a_hard_verdict():
    """只有 `tags_json`（原始留存）里带无障碍字样时，求值方一条硬性判定都不许生成。

    允许的是 UNKNOWN 一族（缺证据不等于可通行，也不等于冲突）；不允许的是把 raw tags 当事实。
    """

    raw_only = _Record(metadata=json.dumps({"duration_min": 60, "cost_minor": 0}), tags=RAW_RETENTION_TAGS)
    assert _accessibility_metadata(raw_only) is None, (
        f"求值方从原始 tags 列造出了证据：{_accessibility_metadata(raw_only)}"
    )
    verdicts = _verdicts(raw_only)
    for blocker in ("STROLLER_ACCESS_UNAVAILABLE", "LUGGAGE_ACCESS_UNAVAILABLE", "ACCESSIBILITY_CONFLICT"):
        assert blocker not in _names(verdicts), f"raw tags 单独导致了 {blocker}：{verdicts}"
    step_free = verdicts["STEP_FREE_REQUIRED"]
    assert any("ACCESSIBILITY_UNVERIFIED" in failure for failure in step_free), (
        f"缺证据没有落到 ACCESSIBILITY_UNVERIFIED：{verdicts}"
    )
    assert any("NEEDS_LIVE_EVIDENCE" in failure for failure in step_free), (
        f"缺证据没有落到 NEEDS_LIVE_EVIDENCE：{verdicts}"
    )

    # 正控：同一批词走规范化通道 ⇒ 必须立刻产生具名判定，否则上面那条"读不到"是永真。
    # 两条记录分开喂：求值方读到嵌套的 accessibility_evidence 就提前返回，不再看同一 metadata 里的
    # osm_tags（这是既有的遮蔽行为，不是本门要钉的边界），合并喂会静默丢掉 CONFLICT 那一半。
    from_evidence = _names(_verdicts(_Record(metadata=json.dumps({
        "duration_min": 60,
        "accessibility_evidence": {"stroller_accessible": False, "luggage_accessible": False},
    }))))
    for name in ("STROLLER_ACCESS_UNAVAILABLE", "LUGGAGE_ACCESS_UNAVAILABLE"):
        assert name in from_evidence, f"正控失效（规范化证据量不出 {name}）：{from_evidence}"
    from_osm = _names(_verdicts(_Record(metadata=json.dumps({
        "duration_min": 60,
        "osm_tags": {"highway": "steps", "step_count": "12"},
    }))))
    assert "ACCESSIBILITY_CONFLICT" in from_osm, f"正控失效（规范化 osm_tags 量不出冲突）：{from_osm}"


# ------------------------------------------------------------------------------------------
# T2
# ------------------------------------------------------------------------------------------
def test_t2_the_same_fact_through_the_normalized_producer_does_name_the_blocker():
    """同一件事实在规范化产物里时，具名判定必须出现——这条钉的是"边界不是漏判"。"""

    per_code = {
        "STROLLER": ({"accessibility_evidence": {"stroller_accessible": False}}, "STROLLER_ACCESS_UNAVAILABLE"),
        "LARGE_LUGGAGE": ({"accessibility_evidence": {"luggage_accessible": False}}, "LUGGAGE_ACCESS_UNAVAILABLE"),
        "STEP_FREE_REQUIRED": ({"osm_tags": {"highway": "steps"}}, "ACCESSIBILITY_CONFLICT"),
    }
    for code, (metadata, expected) in per_code.items():
        record = _Record(metadata=json.dumps(metadata))
        assert _accessibility_metadata(record), f"{code} 的规范化产物没被求值方读到：{metadata}"
        failures = _capability_failure_for_record(code, record, "place-under-test")
        assert any(expected in failure for failure in failures), (
            f"{code} 走规范化通道却得不到 {expected}，实际 {failures}"
        )


# ------------------------------------------------------------------------------------------
# T3
# ------------------------------------------------------------------------------------------
def test_t3_display_taxonomy_tags_never_become_accessibility_evidence():
    """分类/展示词（culture / indoor / quiet / bars…）不是证据。

    范围（仲裁 B-3 钉死）：这条库存等式**只属于 sealed-demo inventory**。两侧都只取
    `city_code == CNX-DEMO` 的地点；将来 real provider 也产 `accessibility_evidence` 时，
    要按 provenance/city 另开一格，不许把这条悄悄扩成全产品库存表。
    """

    taxonomy_only = _Record(metadata=json.dumps({"duration_min": 30}), tags=GENERIC_TAXONOMY_TAGS)
    assert _accessibility_metadata(taxonomy_only) is None, (
        f"展示用 taxonomy 被当成了无障碍证据：{_accessibility_metadata(taxonomy_only)}"
    )

    ensure_schema()
    with SessionLocal() as session:
        seed_demo(session)
        rows = session.scalars(select(Place)).all()

    sealed = [row for row in rows if row.city_code == SEALED_DEMO_CITY]
    outside = [row for row in rows if row.city_code != SEALED_DEMO_CITY]
    assert outside, (
        f"库里没有任何非 {SEALED_DEMO_CITY} 的地点 ⇒ 「按城分表」这句是空话，先查 fixture"
    )
    carrying = sorted(row.id for row in sealed if _accessibility_metadata(row))
    tagged_without_evidence = sorted(
        row.id for row in sealed
        if any("=" in str(token) or str(token).lower() in {"quiet", "indoor", "outdoor", "bars"}
               for token in json.loads(row.tags_json or "[]"))
        and row.id not in carrying
    )
    declared = sorted(
        item["id"] for item in MockCityDataProvider().places()
        if item.get("city_code") == SEALED_DEMO_CITY
        and isinstance(item.get("accessibility"), dict) and item["accessibility"]
    )
    assert declared and carrying, (
        "生产者一个地点都没声明 / 求值方一个地点都看不见 ⇒ 下面那条等式是空对空，"
        f"它钉不住任何东西（declared={declared} carrying={carrying}）。"
        "这条不是防意外：把本文件放到没有 demo 证据生产者的树上（主线 f5a6b9a 就是这个形态）跑，"
        "两侧都是 0，等式照样成立。"
    )
    assert carrying == declared, (
        f"求值方看得见的证据地点应等于生产者声明的集合（仅限 {SEALED_DEMO_CITY}）：{carrying} vs {declared}"
    )
    assert tagged_without_evidence, (
        "封存演示城里没有一个「只带 tags、没有规范化产物」的地点 ⇒ 这条比较是空断言，先查 fixture"
    )
