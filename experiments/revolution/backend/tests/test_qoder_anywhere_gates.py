"""ORD-0094 lane F - the seven hard gates of CITYNEXUS-ANYWHERE, as failing evidence.

Mission §19 says this mission is not done because "Beijing works". These seven doors are the
shape of that sentence turned into assertions, written **before** the implementation exists,
so that lane C/D cannot define "done" as whatever their code happens to do.

Rules this file obeys, per the order:
* nothing here is skipped, marked xfail, or relaxed into a tautology - a vacuous gate is worse
  than no gate, which is the failure this project has been charged with repeatedly;
* every assertion message states 期望 and 实际, and every door has a precondition assert in
  front of it, so a red caused by an unfixed fixture can never be read as a red about the
  product;
* no provider network: `tests/conftest.py` strips credentials and AI is off, so these doors
  exercise backend truth only.

Status at authoritative HEAD `4c8c911` (measured, see
`backend/tests/test_qoder_anywhere_contract_parity.py`): G1-G4 and G6b/G7 are red because the
four operations they need do not exist; G5 is red because an AI run that changes nothing still
has no machine-readable outcome; G6a is green and is only a contract-shape guard, labelled as
such so nobody mistakes it for a behaviour proof.
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

DRAFT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "openapi-anywhere-draft.yaml"

CAPABILITY = "/api/v1/destinations/capability"
HYDRATIONS = "/api/v1/catalog-hydrations"
# 任务书硬要求的"从未预录的县"，车道 E 也用它做 CASE3。
UNSEEN_COUNTY = "贡山独龙族怒族自治县"
COVERAGE_KEYS = {"attractions", "food", "nightlife", "lodging", "mobility", "booking"}
# 词表按 arbiter/anywhere-contract@0c0aa82 的 contracts/anywhere_v1.openapi.yaml：
# 裁决字段叫 `capability`，三态、无 UNKNOWN（"量不出来"走 ReasonCode）。
CAPABILITY_FIELD = "capability"
STATES = {"READY", "HYDRATABLE", "UNAVAILABLE"}
COVERAGE_STATES = {"READY", "PARTIAL", "MISSING"}
HYDRATION_STATUSES = {
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "UNAVAILABLE",
    "ABANDON_REQUESTED",
    "ABANDONED",
}
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "UNAVAILABLE", "ABANDONED"}
ACCEPTED_CODES = {200, 202}
# R2：运行时观测四值。"没有观测"必须靠缺席表达，不许出现 UNKNOWN/空串/编出来的 OK。
PROVIDER_STATUSES = {"OK", "RATE_LIMITED", "UNREACHABLE", "PARTIAL"}
# R1：解析器故障是运行时事实，不得把目的地永久判成结构不可用。
TRANSIENT_REASONS = {"DESTINATION_RESOLVER_UNAVAILABLE", "PROVIDER_RATE_LIMITED", "PROVIDER_TIMEOUT",
                     "PROVIDER_NETWORK_FAILURE", "PROVIDER_SERVER_ERROR", "CATALOG_PREPARING"}
# G3 要拿它去查持久层的唯一索引；表名由车道 C 定，但必须在 inspector 里查得到。
HYDRATION_TABLE_HINTS = ("catalog_hydration", "hydration")
OUTCOME_STATUSES = {"PLAN_CREATED", "NO_ACTION", "BLOCKED"}


def _router_absent(response) -> bool:
    """Starlette's own no-such-route answer; the app's refusals look different and mean more."""
    if response.status_code != 404:
        return False
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return False
    return isinstance(body, dict) and body.get("detail") == "Not Found"


def _summary(response) -> str:
    try:
        return f"{response.status_code} / {str(response.json())[:220]}"
    except Exception:  # noqa: BLE001
        return f"{response.status_code} / <non-json>"


def _require_endpoint(operation: str, response) -> None:
    """Precondition assert shared by every door that needs the target contract."""
    assert not _router_absent(response), (
        f"期望：{operation} 已实现（任务书 §6 的四个端点之一）；"
        f"实际：{_summary(response)} —— 路由级 404，即后端根本没有这个操作。"
        "这条红是车道 C 的收口条件，不是环境噪声，也不许用 skip 绕开"
    )


def _seeded_journey_id() -> str:
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, (
        f"SETUP 失败：/api/v1/demo/reset 期望 200，实际 {_summary(reset)}。"
        "conftest 会把测试进程声明成模拟舞台；若这条红，先查环境，不要改产品语义"
    )
    scene = client.get("/api/v1/demo/scene")
    assert scene.status_code == 200, f"SETUP 失败：/api/v1/demo/scene 实际 {_summary(scene)}"
    journey_id = scene.json().get("journey_id")
    assert journey_id, f"SETUP 失败：reset 之后 scene 里没有 journey_id，实际 {_summary(scene)}"
    return str(journey_id)


# --------------------------------------------------------------------------------------
# G1 · capability truth
# --------------------------------------------------------------------------------------
def test_g1_destination_capability_comes_from_the_backend_not_from_a_count():
    """§2: READY / HYDRATABLE / UNAVAILABLE is backend truth, never a place count or a string."""
    response = client.get(CAPABILITY, params={"query": "Beijing"})
    _require_endpoint("GET /api/v1/destinations/capability", response)

    body = response.json()
    verdict = body.get(CAPABILITY_FIELD)
    assert verdict in STATES, (
        f"期望：`{CAPABILITY_FIELD}` ∈ {sorted(STATES)}（arbiter 契约 v1 就这三态；"
        f"『量不出来』不是第四态，而是 ReasonCode）；"
        f"实际：{CAPABILITY_FIELD}={verdict!r}，完整响应 {_summary(response)}"
    )
    coverage = body.get("coverage")
    assert isinstance(coverage, dict) and set(coverage) == COVERAGE_KEYS, (
        f"期望：coverage 六个分类一个不少（缺键是违约，不是「没有数据」）；实际：{coverage!r}"
    )
    wrong_values = {k: v for k, v in coverage.items() if v not in COVERAGE_STATES}
    assert not wrong_values, (
        f"期望：每个分类取值 ∈ {sorted(COVERAGE_STATES)}；实际：{wrong_values}"
    )
    # R2：观测值只能落在这四个里；"没查过"必须是**没有这条观测**，不是一个编出来的 OK。
    observations = body.get("provider_status") or body.get("provider_observations") or []
    invented = [
        item for item in observations
        if (item.get("status") if isinstance(item, dict) else item) not in PROVIDER_STATUSES
    ]
    assert not invented, (
        f"期望：provider_status 取值 ∈ {sorted(PROVIDER_STATUSES)}（缺观测=不出现，不得伪造 OK/UNKNOWN/空值）；"
        f"实际：{invented}"
    )
    # R1：解析器暂时不可达不是结构性 UNAVAILABLE，必须是可重试的那一条。
    if verdict == "UNAVAILABLE":
        assert body.get("reason_code") not in TRANSIENT_REASONS, (
            f"期望：UNAVAILABLE 只用于『没有可追溯来源』这种结构结论（R1）；"
            f"实际：UNAVAILABLE + reason_code={body.get('reason_code')!r} —— "
            "解析器/网络暂时不可达应保持可重试，不得把目的地永久判死"
        )
    # READY 的判据是带版本的服务端策略，不是任何无版本魔法数。
    if verdict == "READY":
        assert body.get("readiness_policy_version"), (
            f"期望：READY 必须说得出是哪一版 readiness_policy 判的（v0.2：只有服务端评估器能提升）；"
            f"实际：字段缺失，键 {sorted(body)[:12]}"
        )
    assert body.get("reason_code"), (
        f"期望：状态伴随机器可读 reason_code，供界面查表而不是匹配英文句子（§11）；"
        f"实际：reason_code={body.get('reason_code')!r}"
    )


# --------------------------------------------------------------------------------------
# G2 · existing journey recovery
# --------------------------------------------------------------------------------------
def test_g2_an_existing_journey_becomes_plannable_without_being_recreated():
    """§13 hard requirement: the user's journey id survives hydration; no delete/recreate."""
    journey_id = _seeded_journey_id()

    hydration = client.post(HYDRATIONS, json={"query": UNSEEN_COUNTY, "journey_id": journey_id})
    _require_endpoint("POST /api/v1/catalog-hydrations", hydration)
    operation_id = hydration.json().get("operation_id")
    assert operation_id, f"期望：202 返回 operation_id；实际：{_summary(hydration)}"

    status = client.get(f"{HYDRATIONS}/{operation_id}")
    _require_endpoint("GET /api/v1/catalog-hydrations/{operation_id}", status)
    operation = status.json()
    assert operation.get("phase"), (
        f"期望：投影里有真实 phase（§6 状态机的名字，不是百分比）；实际：{_summary(status)}"
    )
    assert operation.get("status") in HYDRATION_STATUSES, (
        f"期望：还有独立的 lifecycle 轴 status ∈ {sorted(HYDRATION_STATUSES)}"
        f"（arbiter v1：只有 phase 分不清『放弃了但还在跑』与『跑完了』）；"
        f"实际：status={operation.get('status')!r}"
    )
    missing_fields = [f for f in ("operation_id", "fingerprint", "reused", "elapsed_ms", "retry_after_ms")
                      if f not in operation]
    assert not missing_fields, (
        f"期望：契约 required 的字段都在（v1 把 status_url/abandon_url 等也列为 required）；"
        f"实际缺 {missing_fields}，现有键 {sorted(operation)[:14]}"
    )
    assert "progress" not in status.json(), (
        f"期望：没有 progress 百分比字段（§6 原话 No fake percentage）；"
        f"实际：出现了 progress={status.json().get('progress')!r}"
    )

    generate = client.post(f"/api/v1/journeys/{journey_id}/plans/generate")
    scene_after = client.get("/api/v1/demo/scene").json()
    assert generate.status_code in (200, 201), (
        f"期望：水合完成后，**同一个** journey_id（{journey_id}）能出计划（§13）；"
        f"实际：plans/generate 返回 {_summary(generate)}，"
        f"scene 里的 journey 仍是 {scene_after.get('journey_id')!r} / 状态 {scene_after.get('journey_status')!r}"
    )
    assert str(scene_after.get("journey_id")) == journey_id, (
        "期望：旅程身份不变；实际：scene 说台上换成了 "
        f"{scene_after.get('journey_id')!r} —— 悄悄重建一张就是假通过"
    )


# --------------------------------------------------------------------------------------
# G3 · hydration idempotency + concurrency  (ORD-0224: re-scoped to ONE open generation)
# --------------------------------------------------------------------------------------
# The arbitration of 2026-09-23 splits what used to be one sentence into two doors, two
# mutations and two sign-offs:
#
#   * **operation identity** -> `G3_CONCURRENT_OPEN_GENERATION` (the first door below): callers
#     that are demonstrably inside one OPEN/admission window must be handed one durable job.
#   * **provider-work dedupe** -> `tests/test_qoder_ord213_ask_store_before_world.py::test_w1_…`:
#     how many times one question gets resolved, which is a different claim.
#
# Keeping them apart matters because they have different killers: moving the reuse lookup behind
# `resolve_destination` costs provider work while five callers still converge, so that mutation
# is evidence for the dedupe door only and must never be offered as g3's reverse mutation
# (commander ORD-0224 step 6).
#
# The retired node `test_g3_five_concurrent_hydrations_of_one_county_converge_on_one_operation`
# opened with a **single** POST and then fired five more.  That first request starts a job which
# can reach a terminal status before its successors arrive, and under the signed tail-A rule a
# terminal job must be answered by a new one - so the node was counting two generations as one,
# which is the instability the arbitration attributed to the instrument rather than the product.
# Its later-request half is not lost: it is the third door below, on its own name.
# No freshness window is introduced anywhere in this file.

import threading

from app.db.models import CatalogHydrationOperation
from app.db.session import SessionLocal
from sqlalchemy import delete, select, text
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


def _own_question(tag: str) -> str:
    """A per-door question, so no door measures another door's leftover worker.

    Still an *unnamed* destination (the resolver knows no place by this name), so the code path under
    test is unchanged; the suffix survives `_normalise`, so each door really does get its own
    fingerprint. Without this, the five g3 doors share one row set and a worker released by door N is
    still running inside door N+1 (measured: mutant Y1a reddened `test_g3e_…` only in the combined
    run shape, never when that door ran alone).
    """

    return f"{UNSEEN_COUNTY}{tag}"


def _pending_fingerprint(query: str) -> str:
    from app.anywhere_capability import pending_request_fingerprint

    return pending_request_fingerprint(query)


def _clear_operations_for(fingerprint: str) -> int:
    """Hygiene for the one table these three doors count - by fingerprint, never globally.

    Measured, not assumed: `/api/v1/demo/reset` runs `services.reset_all`, which deletes 52 tables,
    and `catalog_hydration_operations` is not one of them (0 hits in `app/services.py`), so rows
    left by earlier doors in this file are still in the suite DB.  "Exactly one operation for this
    key" is only meaningful from a known-zero start for that key.
    """

    with SessionLocal() as session:
        removed = session.execute(
            delete(CatalogHydrationOperation).where(
                CatalogHydrationOperation.fingerprint == fingerprint
            )
        ).rowcount
        session.commit()
    return int(removed or 0)


def _rows_for(fingerprint: str) -> list[tuple[str, str, str | None, str | None, str | None]]:
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(CatalogHydrationOperation).where(
                    CatalogHydrationOperation.fingerprint == fingerprint
                )
            ).all()
        )
    return [
        (str(r.id), str(r.status), r.queued_at, r.started_at, r.finished_at) for r in rows
    ]


def test_g3_concurrent_open_generation_five_callers_share_one_operation(monkeypatch) -> None:
    """G3_CONCURRENT_OPEN_GENERATION: five callers in one OPEN window, one durable job.

    Concurrency is *constructed and witnessed*, never hoped for: a `threading.Barrier(5)` keeps
    every caller from leaving until all five are ready, and the product's own worker really is
    dispatched - it parks on a latch inside `_run_operation`, before it can move the row out of
    `QUEUED`.  `worker_started` is the state witness that the window held a live job; if it were
    never set, this door would have silently degenerated into "there was no worker at all", and
    that is asserted against.  No sleep is used as a synchroniser anywhere here.
    """

    import app.catalog_hydration_operations as operations

    question = _own_question("门A")
    fingerprint = _pending_fingerprint(question)
    _clear_operations_for(fingerprint)

    worker_started = threading.Event()
    hold_worker = threading.Event()
    original_run = operations._run_operation

    def parked_worker(operation_id: str):
        worker_started.set()
        hold_worker.wait(10)
        return original_run(operation_id)

    monkeypatch.setattr(operations, "_run_operation", parked_worker)

    gate = threading.Barrier(5)
    results: list[dict] = []
    collect = threading.Lock()

    def caller(index: int) -> None:
        gate.wait()
        response = client.post(HYDRATIONS, json={"query": question})
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = {}
        with collect:
            results.append(
                {
                    "index": index,
                    "code": response.status_code,
                    "operation_id": body.get("operation_id"),
                    "fingerprint": body.get("fingerprint"),
                    "reused": body.get("reused"),
                    "body": body,
                }
            )

    threads = [threading.Thread(target=caller, args=(i,)) for i in range(5)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(results) == 5, (
            f"期望：五个 caller 都返回（barrier 放行后无人卡死）；实际只有 {len(results)} 个返回，"
            f"活着没回来的线程={[t.name for t in threads if t.is_alive()]}"
        )
        assert worker_started.is_set(), (
            "前提被破坏：这五发之间根本没有活着的 worker（`_run_operation` 从未进入）⇒ 本门退化成"
            "『没有 worker 所以没收敛』的空断言；实际 worker 进入过="
            f"{worker_started.is_set()}"
        )
        codes = sorted(item["code"] for item in results)
        assert all(code in ACCEPTED_CODES for code in codes), (
            f"期望：同键并发一律被受理（{sorted(ACCEPTED_CODES)}，**没有 409**——撞指纹要复用而不是"
            f"把人推开）；实际状态码 {codes}"
        )
        ids = {str(item["operation_id"]) for item in results}
        assert len(ids) == 1, (
            f"期望：五枚回执是**同一个** operation_id（一个开放代次=一枚持久作业）；实际 {len(ids)} 个："
            f"逐请求={[(item['index'], item['operation_id'], item['reused']) for item in sorted(results, key=lambda r: r['index'])]}"
        )
        winners = [item for item in results if item["reused"] is False]
        joiners = [item for item in results if item["reused"] is True]
        assert len(winners) == 1 and len(joiners) == 4, (
            f"期望：恰好 1 个 winner + 4 个 reuse（幂等必须说实话：『我没开始，是接上别人的』）；"
            f"实际 reused=False {len(winners)} 个 / reused=True {len(joiners)} 个，"
            f"逐请求 reused={[item['reused'] for item in sorted(results, key=lambda r: r['index'])]}"
        )
        rows = _rows_for(fingerprint)
        assert len(rows) == 1, (
            f"期望：本代次在库里只有 1 枚持久作业（durable 层，不是内存里的一次握手）；"
            f"实际 {len(rows)} 行 {[(r[0][:8], r[1]) for r in rows]}"
        )
        assert rows[0][1] == "QUEUED", (
            f"期望：判词取回时那枚作业仍然是 OPEN（窗口整段都开着，所以五发谈不上跨代次）；"
            f"实际状态 {rows[0][1]}（queued_at={rows[0][2]} started_at={rows[0][3]} finished_at={rows[0][4]}）"
        )
        fingerprints = {item["fingerprint"] for item in results}
        assert fingerprints == {fingerprint} and None not in fingerprints, (
            f"期望：五枚回执说出的键就是登记着的那一把（同一代次可核对）；实际 "
            f"{sorted(str(f)[:12] for f in fingerprints)} vs 库 {fingerprint[:12]}"
        )
        missing_pieces = [
            field
            for field in ("canonical_destination_id", "catalog_schema_version",
                          "readiness_policy_version", "provider_policy_version")
            if field not in results[0]["body"]
        ]
        assert not missing_pieces, (
            "期望：受理回答里能看出指纹四段 canonical_destination_id + catalog_schema_version + "
            f"readiness_policy_version + provider_policy_version（v0.2 的 durable fingerprint）；"
            f"实际缺 {missing_pieces}，现有键 {sorted(results[0]['body'])[:14]}"
        )
    finally:
        hold_worker.set()


def test_g3b_open_window_control_without_any_worker_dispatch(monkeypatch) -> None:
    """The control reading: same five-way burst, no worker at all, zero dispatches - and it still converges.

    Written separately on purpose. If only the latched door existed, nobody could tell whether
    `1 winner + 4 reuse` came from the admission logic or from the latch itself.
    """

    import app.catalog_hydration_operations as operations

    question = _own_question("门B")
    fingerprint = _pending_fingerprint(question)
    _clear_operations_for(fingerprint)

    dispatches: list[str] = []
    original_dispatch = operations.dispatch

    def counting_dispatch(operation_id: str) -> None:
        dispatches.append(str(operation_id))
        return original_dispatch(operation_id)

    monkeypatch.setattr(operations, "dispatch", counting_dispatch)

    gate = threading.Barrier(5)
    seen: list[tuple[int, str, bool]] = []
    collect = threading.Lock()

    def caller(index: int) -> None:
        gate.wait()
        with SessionLocal() as session:
            row, reused = operations.submit_operation(
                session, question, start_worker=False
            )
            outcome = (index, str(row.id), bool(reused))
        with collect:
            seen.append(outcome)

    threads = [threading.Thread(target=caller, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(seen) == 5, f"期望：五发都返回；实际 {len(seen)}"
    assert {o[1] for o in seen} == {seen[0][1]}, (
        f"期望：无 worker 的开放窗口里五发同一枚作业；实际逐请求 {sorted(seen)}"
    )
    assert sum(1 for o in seen if not o[2]) == 1, (
        f"期望：恰好一个 winner；实际 reused=False 有 {sum(1 for o in seen if not o[2])} 个 {sorted(seen)}"
    )
    assert dispatches == [], (
        f"期望：`start_worker=False` 这一形制下 0 次 worker 派发（否则『窗口开着』就不成立了）；"
        f"实际 {dispatches}"
    )
    assert len(_rows_for(fingerprint)) == 1, (
        f"期望：库里 1 行；实际 {_rows_for(fingerprint)}"
    )


def test_g3c_tail_a_terminal_job_is_answered_by_a_new_generation_not_reuse() -> None:
    """TAIL_A_NEW_GENERATION: once A is terminal, the next same-key request opens B.

    This is the half the retired g3 node was silently mixing into the five-way burst, and it is a
    *separate* rule with the opposite direction of travel: inside an open window you must reuse,
    after the window closes you must not.  Note what it also proves about freshness: the follow-up
    is issued in the same instant A closes - measured, printed below - and still gets a new job.
    Any "reuse the terminal result while it is fresh" window would turn this door red, which is
    why the arbitration's rejected option is not smuggled back in as a green here.
    """

    import time

    import app.catalog_hydration_operations as operations

    question = _own_question("门C")
    fingerprint = _pending_fingerprint(question)
    _clear_operations_for(fingerprint)

    with SessionLocal() as session:
        first, first_reused = operations.submit_operation(
            session, question, start_worker=False
        )
        a_id, a_status = str(first.id), str(first.status)
    assert first_reused is False and a_status == "QUEUED", (
        f"前提：A 是这一代的开始者；实际 reused={first_reused} status={a_status}"
    )

    operations._run_operation(a_id)  # the worker body, synchronously: no sleep, no gamble
    with SessionLocal() as session:
        closed = session.get(CatalogHydrationOperation, a_id)
        assert str(closed.status) in operations.TERMINAL_STATUSES, (
            f"前提：A 确实走到终态；实际 {closed.status}（outcome={closed.outcome}，"
            f"finished_at={closed.finished_at}）"
        )
        a_terminal_status, a_finished_at = str(closed.status), closed.finished_at

    then = time.monotonic()
    with SessionLocal() as session:
        second, second_reused = operations.submit_operation(
            session, question, start_worker=False
        )
        b_id, b_status = str(second.id), str(second.status)
    waited_ms = round((time.monotonic() - then) * 1000, 3)

    assert second_reused is False and b_id != a_id, (
        f"期望：A 已终态 ⇒ 后来的同键请求**不得**偷偷复用 A，必须另开一代；实际 reused={second_reused}，"
        f"A={a_id[:8]}/{a_terminal_status} 拿到 {b_id[:8]}/{b_status}"
    )
    rows = _rows_for(fingerprint)
    assert len(rows) == 2, f"期望：A（终态）+ B（开放）两行；实际 {rows}"
    by_id = {row[0]: row for row in rows}
    assert by_id[a_id][1] == a_terminal_status, (
        f"期望：开了 B 不会把 A 从终态拽回来；实际 A 现在 {by_id[a_id][1]}"
    )
    assert by_id[b_id][1] == "QUEUED", f"期望：B 是新的开放代次；实际 {by_id[b_id][1]}"
    print(
        f"\n[tail-A 读数] A={a_id[:8]} {a_terminal_status} finished_at={a_finished_at} → "
        f"B={b_id[:8]} {by_id[b_id][1]}，B 的提交在 A 终态后 {waited_ms} ms 内发出（没有 freshness window）"
    )


def test_g3e_admission_race_no_caller_may_see_another_before_commit(monkeypatch) -> None:
    """The strict admission window: nobody can see an open row at decision time, so only the durable layer can save them.

    Five callers each get their first two `find_open_operation` answers replaced by "nothing open" -
    which is precisely what the earliest interleaving looks like to a caller that has to decide alone.
    The third and later calls are NOT blinded, and that is the point: the only remaining way to hand
    five callers one operation is the partial unique index rejecting the losers plus the product's own
    read-back inside the `IntegrityError` handler. So this door measures the mechanism, not a lucky
    ordering, and its reverse mutation is expected to be deterministic (see
    `ord213/probe_y3_determinism_*.json`).

    What this door does not claim: it cannot observe which caller collided (the product exposes no such
    counter, and adding one for a test would be a product change). It also does not replace
    `test_g3_concurrent_open_generation_…`, which measures the honest-but-timing-dependent version.
    """

    import app.catalog_hydration_operations as operations

    question = _own_question("门E")
    fingerprint = _pending_fingerprint(question)
    _clear_operations_for(fingerprint)

    original_find = operations.find_open_operation
    blinds = threading.local()

    def blind_first_two_lookups(session, key):
        count = getattr(blinds, "lookups", 0) + 1
        blinds.lookups = count
        if count <= 2:  # the pre-resolution and post-resolution reads decide blindly
            return None
        return original_find(session, key)

    monkeypatch.setattr(operations, "find_open_operation", blind_first_two_lookups)

    gate = threading.Barrier(5)
    results: list[dict] = []
    collect = threading.Lock()

    def caller(index: int) -> None:
        gate.wait()
        with SessionLocal() as session:
            row, reused = operations.submit_operation(session, question, start_worker=False)
            outcome = {"index": index, "operation_id": str(row.id), "reused": bool(reused),
                       "status": str(row.status)}
        with collect:
            results.append(outcome)

    threads = [threading.Thread(target=caller, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 5, (
        f"期望：五发都返回（读被遮蔽不等于谁被卡住）；实际只有 {len(results)} 发，"
        f"仍活着={[t.name for t in threads if t.is_alive()]}"
    )
    ids = {item["operation_id"] for item in results}
    assert len(ids) == 1, (
        f"期望：每发都在看不见别人的情况下决定，最后仍只有一枚作业（只有 durable 层做得到）；实际 "
        f"{len(ids)} 枚：逐请求={sorted((i['index'], i['operation_id'][:8], i['reused']) for i in results)}"
    )
    winners = [item for item in results if not item["reused"]]
    assert len(winners) == 1, (
        f"期望：恰好一个 winner、四个 reuse（败者必须回读并上报赢家的 id）；实际 reused=False 有 "
        f"{len(winners)} 个，逐请求 reused={sorted(i['reused'] for i in results)}"
    )
    rows = _rows_for(fingerprint)
    assert len(rows) == 1, f"期望：库里只有 1 行；实际 {rows}"
    assert rows[0][1] == "QUEUED", f"期望：那一行仍是开放代次；实际 {rows[0][1]}"


def test_g3_xproc_loser_readback_five_processes_one_operation() -> None:
    """G3_XPROC_LOSER_READBACK, **supporting** tier per arbiter A-2 (bus ``5788111742``, 10:5x).

    A-2's ruling on this exact shape: ``ACCEPTED_STORE_LEVEL_SUPPORTING_EVIDENCE`` - the value is
    real (five OS processes, durable index + loser read-back, mutant Y3 kills it), but it calls
    ``submit_operation`` inside a bare interpreter, so it crosses no HTTP / app-process boundary, and
    it buys its interleaving by *answering* the first two open-operation lookups with ``None``.  The
    closure claim therefore lives in ``tests/test_qoder_g3_xproc_http.py`` (two spawned uvicorn PIDs,
    HTTP, one fresh database, and the blind spot scheduled on the storage layer instead of imposed on
    a read).  This door stays because it is the only arm that measures the *earliest* possible
    interleaving - both callers told "nothing is open" before either wrote - at no process-startup
    cost; it is not the door a closure signature gets written against.

    Why this door exists as its own process-level shape: every other g3 door runs its callers inside
    one interpreter, where a SQLAlchemy session, an identity map and the module-level dispatch set are
    all shared - so "two uvicorn workers do not share a lock" is asserted there but not measured. This
    door spawns five real children (`tests/_qoder_xproc_caller.py`), each pointed at the same
    app-resolved test database through `CITYNEXUS_DATABASE_URL`, and synchronises them on a listening
    socket: every child announces `ready`, the parent accepts all five, then releases them together.
    No sleep is used as a synchroniser; the only timers here are bounded waits that fail loudly.

    Each child has its first two open-operation lookups answered "nothing is open" - the earliest
    interleaving from a caller's point of view - so the only thing left that can deliver
    "one operation, five callers" is the partial unique index plus the loser's read-back of the
    winner's id. That is the claim, and the probe that validated the shape measured it red under
    mutant Y3 (`ord213/xproc_shape_probe_result.json`).

    It asserts the number of distinct pids first: if the children ever collapse into one process the
    door stops measuring anything, and that must be a red rather than a quiet green.
    """

    import json
    import os
    import socket
    import subprocess
    import sys

    from app.db.session import engine

    CALLERS = 5
    question = _own_question("跨进程")
    fingerprint = _pending_fingerprint(question)
    _clear_operations_for(fingerprint)

    backend = Path(__file__).resolve().parents[1]
    caller = Path(__file__).resolve().parent / "_qoder_xproc_caller.py"
    assert caller.exists(), f"期望：跨进程子体 {caller.name} 存在；实际不在树上"
    env = dict(os.environ)
    env["CITYNEXUS_DATABASE_URL"] = str(engine.url)
    env["PYTHONIOENCODING"] = "utf-8"
    # a script's sys.path[0] is its own directory, so `import app...` needs the backend on the path
    env["PYTHONPATH"] = os.pathsep.join(p for p in [str(backend), env.get("PYTHONPATH", "")] if p)

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(CALLERS)
    server.settimeout(60)
    kids = [
        subprocess.Popen([sys.executable, "-X", "utf8", str(caller), "blind", str(engine.url),
                          str(port), f"x{i}", question], cwd=str(backend), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        for i in range(CALLERS)
    ]
    conns = []
    try:
        for _ in range(CALLERS):
            conn, _addr = server.accept()
            conns.append(conn)
        for conn in conns:
            conn.sendall(b"go")
    finally:
        server.close()

    rows: list[dict] = []
    for index, kid in enumerate(kids):
        try:
            stdout, stderr = kid.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            kid.kill()
            stdout, stderr = kid.communicate()
            raise AssertionError(
                f"期望：五发子进程都在 120 s 内返回；实际 x{index} 未归（仪器故障，不是产品结论）；"
                f"stderr 尾={(stderr or '')[-200:]!r}"
            )
        payload = None
        for line in (stdout or "").splitlines():
            if line.startswith("{"):
                payload = json.loads(line)
        assert payload is not None, (
            f"期望：x{index} 交回一行 JSON 读数；实际没有，stderr 尾={(stderr or '')[-300:]!r}"
        )
        rows.append(payload)

    failed = [r for r in rows if r.get("error")]
    assert not failed, (
        f"期望：五发都被受理并拿到 operation_id（跨进程幂等不许把人推开）；实际报错 {len(failed)} 发："
        f"{[(r['label'], r['error']) for r in failed]}"
    )
    pids = {r["pid"] for r in rows}
    assert len(pids) == CALLERS, (
        f"期望：五发是 {CALLERS} 个不同 OS 进程（同一进程里的多线程不算这条门，共享会话/身份映射/"
        f"派发集合都会替产品撒谎）；实际 pid={sorted(pids)}"
    )
    ids = {str(r["operation_id"]) for r in rows}
    assert len(ids) == 1, (
        f"期望：跨进程、且每发都看不见别人时，五发仍是同一枚作业（只有部分唯一索引 + 败者回读做得到）；"
        f"实际 {len(ids)} 枚：逐发={[(r['label'], str(r['operation_id'])[:8], r['reused']) for r in rows]}"
    )
    winners = [r for r in rows if r["reused"] is False]
    joiners = [r for r in rows if r["reused"] is True]
    assert len(winners) == 1 and len(joiners) == CALLERS - 1, (
        f"期望：恰好 1 winner + 4 reuse，而且**跨进程**那四个 reuse 必须来自败者回读；实际 "
        f"reused=False {len(winners)} / reused=True {len(joiners)}，逐发 reused="
        f"{[r['reused'] for r in rows]}"
    )
    rows_in_store = _rows_for(fingerprint)
    assert len(rows_in_store) == 1, (
        f"期望：库里这一代只有 1 行（跨进程也不许多落一行——这是 durable 层，不是内存握手）；"
        f"实际 {rows_in_store}"
    )
    assert rows_in_store[0][1] == "QUEUED", (
        f"期望：那一行仍是开放代次；实际 {rows_in_store[0][1]}"
    )
    print(
        "\n[G3_XPROC_LOSER_READBACK] 5 个 OS 进程 → 1 枚作业 "
        f"id={sorted(ids)[0][:8]}，1 winner + 4 reuse，pids={sorted(pids)}，"
        f"等待闩 ms={[r['waited_for_release_ms'] for r in rows]}，提交 ms={[r['submit_ms'] for r in rows]}"
    )


def test_g3d_durable_layer_still_holds_the_partial_unique_index() -> None:
    """The persistence witness the retired node carried, kept as its own door.

    Convergence for two uvicorn workers cannot rest on a process-local lock, so the thing that
    must exist is a unique index over the fingerprint restricted to the open statuses.
    """

    from sqlalchemy import inspect

    from app.db.session import engine

    inspector = inspect(engine)
    tables = [t for t in inspector.get_table_names() if any(h in t for h in HYDRATION_TABLE_HINTS)]
    assert tables, (
        f"期望：有一张水合操作表可供查唯一索引（表名含 {list(HYDRATION_TABLE_HINTS)} 之一）；"
        f"实际：库里没有 ⇒ '至多一条活动操作' 没有强制物，只能靠自觉"
    )
    unique_on_fingerprint = []
    for table in tables:
        for index in inspector.get_indexes(table):
            cols = [c.lower() for c in (index.get("column_names") or [])]
            if index.get("unique") and any("fingerprint" in c or "canonical" in c for c in cols):
                unique_on_fingerprint.append((table, index.get("name"), cols))
    assert unique_on_fingerprint, (
        f"期望：水合表上有一个覆盖指纹/规范目的地的**唯一索引**（§7：durable 层强制）；实际：表 {tables} "
        f"的索引里没有。缺它，并发就会造出两条活动操作与两份 provider 风暴"
    )
    with engine.connect() as connection:
        sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"
            ),
            {"name": unique_on_fingerprint[0][1]},
        ).scalar()
    assert sql and "WHERE" in sql.upper(), (
        f"期望：那把唯一索引是**部分**索引（只覆盖开放状态），否则终态历史会永久挡住同一个目的地再开"
        f"一代——tail-A 就无从成立；实际索引定义：{sql!r}"
    )

# --------------------------------------------------------------------------------------
# G4 · category coverage
# --------------------------------------------------------------------------------------
def test_g4_partial_catalog_is_never_reported_as_fully_ready():
    """§2: two museums and no food/nightlife/lodging must not read READY for every intent."""
    response = client.get(CAPABILITY, params={"query": UNSEEN_COUNTY})
    _require_endpoint("GET /api/v1/destinations/capability", response)
    body = response.json()
    coverage = body.get("coverage") or {}
    assert set(coverage) == COVERAGE_KEYS, f"期望：六个分类齐全；实际：{sorted(coverage) or coverage!r}"

    verdict = body.get(CAPABILITY_FIELD)
    if verdict == "READY":
        not_ready = {key: value for key, value in coverage.items() if value != "READY"}
        assert not not_ready, (
            f"期望：{CAPABILITY_FIELD}=READY 时每个分类都是 READY；实际：{not_ready} —— "
            "整体 READY、局部 MISSING 就是「能规划一切」的谎"
        )
    if verdict == "READY":
        assert body.get("readiness_policy_version"), (
            "期望：READY 出自带版本的服务端策略，不是 places.length>0 这类无版本魔法数（v0.2 明令禁止）；"
            f"实际：readiness_policy_version={body.get('readiness_policy_version')!r}"
        )
    # 候选数只能用于诊断：v0.2 明写 candidate count alone cannot promote the destination to READY。
    counts = {
        key: value.get("candidate_count")
        for key, value in coverage.items()
        if isinstance(value, dict)
    }
    weak = {key: value for key, value in coverage.items() if isinstance(value, dict) and value.get("state") != "READY"}
    assert not (counts and weak and verdict == "READY"), (
        f"期望：有分类仍是 PARTIAL/MISSING 时，候选数量再多也不得把目的地举成 READY；"
        f"实际：{weak}，候选数 {counts}"
    )
    partial = {k: v for k, v in coverage.items() if v in {"PARTIAL", "MISSING"}}
    assert not (partial and verdict == "READY"), (
        f"期望：只有两间博物馆的城市不得报成全量 READY（§2 READY != places.length > 0）；"
        f"实际：{CAPABILITY_FIELD}=READY 而 {partial}"
    )
    places = body.get("place_count")
    if places is not None:
        assert not (places > 0 and verdict == "UNAVAILABLE"), (
            f"期望：有地点就不可能同时是 UNAVAILABLE；实际：place_count={places} "
            f"而 {CAPABILITY_FIELD}={verdict!r} —— 数地点来定状态正是 §2 禁的那种推断"
        )


# --------------------------------------------------------------------------------------
# G5 · AI no-op visibility
# --------------------------------------------------------------------------------------
def test_g5_an_ai_run_that_changed_must_say_which_it_was():
    """§11: 0 mutations / 0 proposals / 0 errors must not vanish silently.

    Measured shape today (`4c8c911`, own DB, AI off): the response is
    `{event_id, mission, run}`; `run.outcome` exists but is `null`; `run.status` and
    `mission.status` are the runtime label `SKIPPED_EXTERNAL_DEPENDENCY`. So the seam is
    already there - what is missing is the truth poured into it.
    """
    journey_id = _seeded_journey_id()
    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={"command": "帮我把这趟行程重新安排一下", "requested_autonomy": "PLAN_ONLY"},
    )
    assert response.status_code < 500, (
        f"期望：AI 无动作/被挡也要给出可读回答，而不是 5xx；实际：{_summary(response)}"
    )
    body = response.json() if isinstance(response.json(), dict) else {}
    run = body.get("run")
    assert isinstance(run, dict) and run, (
        f"期望：响应里有 `run`（§11 说 outcome 挂在这一层）；实际：顶层键 {sorted(body)[:10]}，"
        f"{_summary(response)[:160]}"
    )

    outcome = run.get("outcome")
    assert isinstance(outcome, dict) and outcome, (
        f"期望：`run.outcome` 带上 §11 的机器可读语义；实际：run.outcome={outcome!r}，"
        f"而 run.status={run.get('status')!r} / failure_reason={str(run.get('failure_reason'))[:60]!r} —— "
        "这次运行确实什么都没改（没 key、没动作），可它只留下一个运行时标签；"
        "一个既没变化也没原因的 200，正是用户早上看到的那句「转了半天啥也没发生」"
    )
    assert outcome.get("status") in OUTCOME_STATUSES, (
        f"期望：status ∈ {sorted(OUTCOME_STATUSES)}（区分「办了/什么都没做/被挡住了」）；"
        f"实际：status={outcome.get('status')!r}，运行时的 SKIPPED_EXTERNAL_DEPENDENCY 不是这个语义"
    )
    assert outcome.get("reason_code"), (
        f"期望：reason_code 具名（§11 的 CATALOG_EMPTY / CATALOG_INSUFFICIENT / "
        f"NO_EXISTING_PLAN_TO_REPLAN / PROVIDER_* 一族），界面才能不靠匹配英文句子分类；"
        f"实际：reason_code={outcome.get('reason_code')!r}"
    )
    assert outcome.get("user_summary") and outcome.get("recommended_action"), (
        f"期望：一句人话 + 一个具体恢复动作（准备数据/重试/改预算/改日期/看可用地点）；"
        f"实际：user_summary={outcome.get('user_summary')!r} "
        f"recommended_action={outcome.get('recommended_action')!r}"
    )


# --------------------------------------------------------------------------------------
# G6 · provenance stays distinguishable
# --------------------------------------------------------------------------------------
def test_g6a_annex_records_the_arbiters_provenance_vocabulary():
    """Contract-shape guard only: it proves the annex transcribes the ruling, not that
    provenance works anywhere. Behaviour is G6b; keeping them apart stops a green here
    from being read as the door passing."""
    with DRAFT_PATH.open("r", encoding="utf-8") as handle:
        draft = yaml.safe_load(handle)
    follows = " ".join(((draft.get("info") or {}).get("x-reconciliation") or {}).get("follows-arbiter") or [])
    for kind in ("GOLDEN_FIXTURE", "REMOTE_PROVIDER", "SIMULATED", "REPLAY", "OFFLINE_SNAPSHOT"):
        assert kind in follows, (
            f"期望：附件登记了仲裁 v1 的 source_kind 值 {kind}（来源必须分得开）；"
            "实际：x-reconciliation.follows-arbiter 里没有它"
        )
    assert "fact_quality" in follows, (
        "期望：附件记下『归一化不得把 SIMULATED/PARTIAL/未知升成更强类』这条 —— "
        "它是 provenance 不说谎的另一半"
    )


def test_g6b_capability_answer_carries_the_provenance_of_what_it_found():
    """§5/§10/§11: a sealed fixture, a live fetch and a simulated stage fact cannot share a label."""
    response = client.get(CAPABILITY, params={"query": "Beijing"})
    _require_endpoint("GET /api/v1/destinations/capability", response)
    body = response.json()
    provenance = body.get("provenance") or (body.get("coverage_provenance") or {})
    assert provenance, (
        f"期望：能力结论说得出自己依据哪一类来源（FIXTURE/FETCHED/SIMULATED/…）；"
        f"实际：provenance 缺失，响应键 {sorted(body)[:12]}"
    )
    text = str(provenance).upper()
    assert "LIVE" not in text or "SIMULATED" not in text, (
        f"期望：模拟台产出的数据不得被标成实况（§11/§14，也是本项目反复被记账的假真值）；"
        f"实际：{provenance!r}"
    )


# --------------------------------------------------------------------------------------
# G7 · no manual-city treadmill
# --------------------------------------------------------------------------------------
def test_g7_a_destination_with_no_hand_authored_file_still_gets_an_answer():
    """§19 items 2/3/16/17: "add another JSON city" must stop being the only way forward.

    Measured at `4c8c911`: the hand-authored catalog the product actually reads
    (`MOCK_ROOT/places.json`, i.e. `<repo>/data/mock/places.json`) carries 16 places across
    exactly two codes - CN-BEIJING and CNX-DEMO. Two cities is the treadmill.
    """
    from app.integrations.mock.providers import MOCK_ROOT  # 用产品自己认得的那个路径

    places_path = MOCK_ROOT / "places.json"
    assert places_path.exists(), (
        f"SETUP 失败：产品读的手工目录 {places_path} 不存在 —— 前提变了，这条门要连同任务书一起重写"
    )
    places = json.loads(places_path.read_text(encoding="utf-8"))
    hand_authored = {str(item.get("city_code")) for item in places if isinstance(item, dict)}
    hand_authored.discard("None")
    assert hand_authored, (
        f"SETUP 失败：{places_path} 里没解析出任何 city_code（条目 {len(places)} 个），"
        "这条门的「今天靠手工 JSON 撑城市」前提已不成立，请改门而不是改产品语义"
    )
    assert UNSEEN_COUNTY not in {str(item.get("city_code")) for item in places if isinstance(item, dict)}, (
        f"SETUP 失败：{UNSEEN_COUNTY} 已经出现在手工目录里，那它就不是「从未预录」的县，"
        "本门要换目标（车道 E 的 CASE3 同理）"
    )

    response = client.get(CAPABILITY, params={"query": UNSEEN_COUNTY})
    _require_endpoint("GET /api/v1/destinations/capability", response)
    body = response.json()
    assert body.get(CAPABILITY_FIELD) in STATES, (
        f"期望：未预录的 {UNSEEN_COUNTY} 也要得到一个语义态（能备 / 已可 / 确实不行）；"
        f"实际：{CAPABILITY_FIELD}={body.get(CAPABILITY_FIELD)!r}，{_summary(response)}。"
        f"手工目录现在只有 {sorted(hand_authored)} 这 {len(hand_authored)} 个城市码，"
        "答案不该由它决定"
    )
    assert body.get("recommended_action") or body.get(CAPABILITY_FIELD) == "READY", (
        f"期望：不能备就明说不能备，并给用户一条出路（§11/§14）；实际："
        f"recommended_action={body.get('recommended_action')!r} "
        f"{CAPABILITY_FIELD}={body.get(CAPABILITY_FIELD)!r}"
    )


if __name__ == "__main__":  # pragma: no cover - convenience for a human re-running one door
    raise SystemExit(pytest.main([__file__, "-v"]))
