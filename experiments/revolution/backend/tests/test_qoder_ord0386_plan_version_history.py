"""ORD-0386 步骤一 · 版本历史：只读列表必须带写入时间，且这台服务器确实没有保留上限。

为什么要有这个文件：`GET /journeys/{id}/plans` 早就把每一版都返回了（`backend/app/main.py:5116`
那条 GET 的 docstring 自己写着 "Every version this journey has ever had"），但投影里没有 `created_at`
（改动前 `backend/app/services.py:612` 的 `PlanSchema(...)` 传了 18 个字段，里面没有它——计数方式：
`re.findall(r'^\\s{8}(\\w+)=', <该调用体>, re.M)` 对 `git show HEAD:backend/app/services.py` 的结果），
所以一屏"历史版本"只能靠 `version_no` 排序去猜时间。本文件把这一枚字段钉进契约、投影和响应，并把**上限那一格钉成实测**：
不是"我以为没有上限"，是"跑 N 次生成，行数按候选数增长、version_no 连续、旧行不消失"。

期望值一律**算出来**：行数来自查询前的实际读数加本次返回的候选数，时间来自 DB 列本身，
不写死数字，也不写死 "PASS"。
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Journey, PlanVersion
from app.db.session import SessionLocal
from app.main import app
from app.services import seed_demo

client = TestClient(app)


def _boot() -> None:
    """Tables come up through the product's own demo seed, the same way
    `test_qoder_demo_accessibility_evidence.py:70` brings them up -- never via the shared
    `demo/reset` route, which wipes 52 tables and is guarded off the preview anyway."""

    with SessionLocal() as session:
        seed_demo(session)


_boot()

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"

# CNX-DEMO is the one city in the shipped catalogue that has both places and city_services, so it
# is the only place a generation can actually succeed. Measured on a copy of the preview DB:
# CN-GUANGZHOU has 14 places and 0 city_services, and answers 422 for every candidate.
PROBE_CITY = "CNX-DEMO"
PROBE_WINDOW = ("2026-09-21T02:00:00+00:00", "2026-09-22T10:00:00+00:00")


def _probe_journey(client: TestClient) -> str:
    traveler = client.post(
        "/api/v1/travelers",
        json={"display_name": "ORD-0386 probe", "locale": "zh-CN", "timezone": "Asia/Shanghai"},
    )
    assert traveler.status_code == 201, traveler.text
    journey = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler.json()["id"],
            "title": "ORD-0386 版本历史探针",
            "city_code": PROBE_CITY,
            "timezone": "Asia/Shanghai",
            "start_at": PROBE_WINDOW[0],
            "end_at": PROBE_WINDOW[1],
            "budget": {"currency": "CNY", "amount_minor": 120000},
            "planning_mode": "balanced",
        },
    )
    assert journey.status_code == 201, journey.text
    return str(journey.json()["id"])


def _db_rows(journey_id: str) -> list[PlanVersion]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(PlanVersion)
                .where(PlanVersion.journey_id == journey_id)
                .order_by(PlanVersion.version_no)
            ).all()
        )


def test_1_generated_versions_exist_at_all() -> None:
    """The precondition for every other node: if generation cannot write a row here, the list and
    the cap readings below would be vacuously true. Fail loudly instead."""
    journey_id = _probe_journey(client)
    answer = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 3}
    )
    assert answer.status_code == 201, (
        f"generation refused on the probe city (HTTP {answer.status_code}): {answer.text[:300]}"
    )
    assert len(answer.json()) > 0, "generation answered 201 with an empty list, so nothing is provable"
    assert len(_db_rows(journey_id)) == len(answer.json()), (
        "the number of rows written differs from the number of candidates returned"
    )


def test_2_list_carries_created_at_and_it_is_the_stored_value() -> None:
    journey_id = _probe_journey(client)
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 2}
    )
    assert generated.status_code == 201, generated.text
    listed = client.get(f"/api/v1/journeys/{journey_id}/plans")
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    stored = {row.version_no: row.created_at for row in _db_rows(journey_id)}
    assert len(rows) == len(stored), (
        f"the list returned {len(rows)} rows while the store holds {len(stored)} for this journey"
    )
    missing = [item["version_no"] for item in rows if not item.get("created_at")]
    assert not missing, f"created_at absent on versions {missing}; a history list cannot be read without it"
    wrong = [
        (item["version_no"], item["created_at"], stored.get(item["version_no"]))
        for item in rows
        if str(item["created_at"]) != str(stored.get(item["version_no"]))
    ]
    assert not wrong, f"created_at does not match the stored column on: {wrong}"


def test_3_no_retention_cap_measured_by_repeated_generation() -> None:
    """The order's second requirement, answered with a measured number rather than an impression:
    generate the same operation repeatedly and require the store to keep every version."""
    journey_id = _probe_journey(client)
    rounds = 4
    seen: list[int] = []
    returned: list[int] = []
    for _ in range(rounds):
        answer = client.post(
            f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 2}
        )
        assert answer.status_code == 201, answer.text
        returned.append(len(answer.json()))
        seen.append(len(_db_rows(journey_id)))
    assert all(count > 0 for count in returned), returned
    assert seen == [sum(returned[: index + 1]) for index in range(rounds)], (
        f"row counts did not grow by exactly what each round returned: per-round {returned}, cumulative {seen}"
    )
    numbers = [row.version_no for row in _db_rows(journey_id)]
    assert numbers == list(range(min(numbers), max(numbers) + 1)), (
        f"version_no has gaps, which would mean something deleted history: {numbers}"
    )
    assert len(numbers) == sum(returned), (
        f"expected {sum(returned)} versions after {rounds} rounds, found {len(numbers)}"
    )


def test_4_list_is_unfiltered_across_statuses() -> None:
    """A history screen that quietly drops CANDIDATE rows would understate the retention answer,
    so the list must expose every status the store holds."""
    journey_id = _probe_journey(client)
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 2}
    ).status_code == 201
    statuses_in_db = {row.status for row in _db_rows(journey_id)}
    statuses_listed = {item["status"] for item in client.get(f"/api/v1/journeys/{journey_id}/plans").json()}
    assert statuses_in_db == statuses_listed, (
        f"the store holds {sorted(statuses_in_db)} but the list shows {sorted(statuses_listed)}"
    )


def test_5_contract_declares_the_field_the_route_answers() -> None:
    """The parity gate compares paths and methods only, so a response field can appear in the
    runtime payload and be absent from the formal contract. This node closes that gap for the one
    field this order adds."""
    text = CONTRACT.read_text(encoding="utf-8")
    block = text[text.index("    PlanVersion:") :]
    assert re.search(r"^\s{8}created_at: \{type: \[string, 'null'\], format: date-time\}", block, re.M), (
        "contracts/openapi.yaml PlanVersion does not declare created_at as a nullable date-time"
    )
    journey_id = _probe_journey(client)
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).status_code == 201
    rows = client.get(f"/api/v1/journeys/{journey_id}/plans").json()
    assert rows and "created_at" in rows[0], "the route does not answer the field the contract declares"


def test_6_journey_without_versions_is_empty_not_absent() -> None:
    journey_id = _probe_journey(client)
    with SessionLocal() as session:
        assert session.get(Journey, journey_id) is not None
    rows = client.get(f"/api/v1/journeys/{journey_id}/plans").json()
    assert rows == [], f"a fresh journey should list no versions, got {len(rows)}"
