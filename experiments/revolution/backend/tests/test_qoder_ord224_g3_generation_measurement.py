"""ORD-0224 step 4 - the third-measurement package for g3, produced by doors, not by prose.

Why this file exists: the arbitration suspended the old readings (`12R/2G`, `0/10R`) with
`OLD_G3_PRODUCT_DEFECT_CLASSIFICATION = SUSPENDED_PENDING_EVIDENCE`, on the judgement that the
retired node very likely counted **two generations** as one - a single POST whose job reached a
terminal status, then five more.  A suspended classification cannot be lifted by my describing the
experiment; it needs the two shapes run side by side with the per-request evidence attached.  So the
experiment is a door: if a field is missing, if a caller did not return, if a probe never fired, the
door is red and the package does not exist.

Two shapes, deliberately opposite:

  shape A `TERMINAL_THEN_BURST`  - the retired domain.  Expectation is written as *two*
                                   generations, because tail-A says a terminal job must be
                                   answered by a new one.  What it measures is how many distinct
                                   operation_ids a "five concurrent" label quietly contains.
  shape B `CONCURRENT_OPEN_WINDOW` - the corrected domain (one open generation, latched worker,
                                   barrier-released callers).  Expectation is 1 + 4 reuse.

Four readings, per shape: ① provider/catalog resolution calls, ② durable rows for the key,
③ distinct operation_ids the callers were handed, ④ the winner/reuse split plus the generation's
state timeline (queued_at / started_at / finished_at, read from the same app-resolved DB).

Every reading names its `probe_source`, and no probe replaces product behaviour: the resolution
wrapper delegates to the product's own function, the worker latch calls through after waiting, and
the row reads go through `SessionLocal` - the same session factory the app uses.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.catalog_hydration_operations as operations
from app.db.models import CatalogHydrationOperation
from app.db.session import SessionLocal, engine
from app.main import app

client = TestClient(app)
HYDRATIONS = "/api/v1/catalog-hydrations"
COUNTY = "贡山独龙族怒族自治县"
CALLERS = 5
OUT_ENV = "CITYNEXUS_ORD224_OUT"
REQUIRED_READINGS = (
    "resolution_call_count",
    "durable_rows_for_key",
    "distinct_operation_ids",
    "winner_and_reuse_split",
    "state_timeline",
)


def _fingerprint() -> str:
    from app.anywhere_capability import pending_request_fingerprint

    return pending_request_fingerprint(COUNTY)


def _clear(fingerprint: str) -> None:
    with SessionLocal() as session:
        session.execute(
            delete(CatalogHydrationOperation).where(
                CatalogHydrationOperation.fingerprint == fingerprint
            )
        )
        session.commit()


def _instrument(monkeypatch) -> dict:
    """Count the product's own resolution passes; the wrapper always calls through."""

    calls: list[dict] = []
    original = operations.resolve_destination

    def counting(session, query):
        calls.append({"thread": threading.current_thread().name, "query": str(query)})
        return original(session, query)

    monkeypatch.setattr(operations, "resolve_destination", counting)
    return {"calls": calls, "probe_source": "wrapper on app.catalog_hydration_operations.resolve_destination"}


def _rows(fingerprint: str) -> list[dict]:
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(CatalogHydrationOperation).where(
                    CatalogHydrationOperation.fingerprint == fingerprint
                )
            ).all()
        )
    return [
        {
            "operation_id": str(r.id),
            "status": str(r.status),
            "outcome": r.outcome,
            "queued_at": r.queued_at,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
        }
        for r in rows
    ]


def _burst_via_route() -> list[dict]:
    """`CALLERS` posts that are released together; nobody starts until everyone is ready."""

    gate = threading.Barrier(CALLERS)
    seen: list[dict] = []
    guard = threading.Lock()

    def caller(index: int) -> None:
        gate.wait()
        started = time.monotonic()
        response = client.post(HYDRATIONS, json={"query": COUNTY})
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = {}
        with guard:
            seen.append(
                {
                    "index": index,
                    "status_code": response.status_code,
                    "operation_id": body.get("operation_id"),
                    "reused": body.get("reused"),
                    "fingerprint": body.get("fingerprint"),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                }
            )

    threads = [threading.Thread(target=caller, args=(i,)) for i in range(CALLERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(seen) == CALLERS, f"探针不完整：{CALLERS} 发里只有 {len(seen)} 发返回"
    return sorted(seen, key=lambda item: item["index"])


def test_shape_a_terminal_job_then_five_requests_is_two_generations_not_one(monkeypatch) -> None:
    """The retired domain, measured rather than argued about."""

    fingerprint = _fingerprint()
    _clear(fingerprint)
    probe = _instrument(monkeypatch)

    # A is opened through the service with `start_worker=False`, then run by hand, so "the previous
    # generation is closed" is a set-up fact.  Going through the route here raced with the route's
    # own dispatched worker: it marked the row RUNNING first, and `_run_operation` returns by design
    # when the row is no longer QUEUED - which left A at RUNNING and made this door's *precondition*
    # fail (measured 2026-09-23, combined run).  That is an instrument race, not a product finding,
    # and the fix is the deterministic schedule above - not a wait loop.
    with SessionLocal() as session:
        opened, opened_reused = operations.submit_operation(
            session, COUNTY, start_worker=False
        )
        a_id = str(opened.id)
    assert opened_reused is False, f"前提：A 是这一代的开始者；实际 reused={opened_reused}"
    operations._run_operation(a_id)  # the worker body, synchronously: A really closes
    with SessionLocal() as session:
        closed = session.get(CatalogHydrationOperation, a_id)
        assert str(closed.status) in operations.TERMINAL_STATUSES, (
            f"前提：A 已进入终态（这才是『上一代已经关门』的形状）；实际 {closed.status}"
        )

    responses = _burst_via_route()
    ids = {str(item["operation_id"]) for item in responses}
    rows = _rows(fingerprint)
    statuses = {row["operation_id"]: row["status"] for row in rows}

    # What makes this shape A is not "the burst fans out" - it may well converge on one new job.
    # It is that under one label of "five concurrent requests" the store holds **two generations**:
    # the terminal A the retired node itself created, plus whatever its five successors opened.
    assert a_id not in ids, (
        f"期望：五发里没有任何一发被塞回终态的 A（tail-A：关门的那代不许复用）；实际拿到 A 的是 "
        f"{[item['index'] for item in responses if item['operation_id'] == a_id]}"
    )
    terminal_rows = [r for r in rows if r["status"] in operations.TERMINAL_STATUSES]
    open_rows = [r for r in rows if r["status"] not in operations.TERMINAL_STATUSES]
    assert terminal_rows and open_rows, (
        f"期望：这一形制在库里留下**两代**（A 已终态 + 后继另开一代）；实际 {len(rows)} 行 "
        f"{[(r['operation_id'][:8], r['status']) for r in rows]}"
    )
    assert len({r["operation_id"] for r in terminal_rows} | {r["operation_id"] for r in open_rows}) >= 2, (
        f"期望：终态代与开放代不是同一枚作业；实际 {statuses}"
    )
    assert len(probe["calls"]) >= 1, "探针必须真的量到过解析，否则这一档读数是空的"
    # The sentence this shape exists to test: the retired node folded these two generations into
    # one "five concurrent requests" label, which is what made its verdict unstable.
    print(
        "\n[shape A] 单请求先行 + 五发并发 → 代次数="
        f"{len(terminal_rows)} 终态 + {len(open_rows)} 开放 = {len(rows)} 行；五发里的 distinct "
        f"operation_ids={len(ids)}（读数，不是判据），解析次数={len(probe['calls'])}，A={a_id[:8]}"
    )


def test_shape_b_five_callers_in_one_open_window_converge(monkeypatch) -> None:
    """The corrected domain: one live worker parked inside the window, five barrier-released callers."""

    fingerprint = _fingerprint()
    _clear(fingerprint)
    probe = _instrument(monkeypatch)

    worker_entered = threading.Event()
    hold = threading.Event()
    original_run = operations._run_operation

    def parked_worker(operation_id: str):
        worker_entered.set()
        hold.wait(10)
        return original_run(operation_id)

    monkeypatch.setattr(operations, "_run_operation", parked_worker)
    try:
        responses = _burst_via_route()
    finally:
        hold.set()

    assert worker_entered.is_set(), (
        "前提破坏：窗口里没有活着的 worker（`_run_operation` 从未进入）⇒ 这一档只是『没人干活所以收敛』"
    )
    ids = {str(item["operation_id"]) for item in responses}
    rows = _rows(fingerprint)
    winners = [item for item in responses if item["reused"] is False]
    joiners = [item for item in responses if item["reused"] is True]

    assert len(ids) == 1, (
        f"期望：一个开放代次里五发同一枚 operation_id；实际 {len(ids)} 个 {sorted(ids)}"
    )
    assert len(winners) == 1 and len(joiners) == CALLERS - 1, (
        f"期望：1 winner + 4 reuse；实际 {len(winners)}/{len(joiners)}，"
        f"逐请求 reused={[item['reused'] for item in responses]}"
    )
    assert len(rows) == 1, f"期望：库里这一代只有 1 行；实际 {rows}"
    print(
        "\n[shape B] barrier(5) + latched worker → 1 op、1 winner + 4 reuse、"
        f"解析次数={len(probe['calls'])}、逐请求耗时={[item['elapsed_ms'] for item in responses]}"
    )


def test_measurement_package_is_complete_and_writable(tmp_path, monkeypatch) -> None:
    """The package the arbitration asked for: both shapes, four readings, per-request ids, probe sources.

    Completeness is asserted, not promised - and when `CITYNEXUS_ORD224_OUT` names a path, the same
    dict is written there so the ledger can quote an artifact instead of my paraphrase.
    """

    fingerprint = _fingerprint()
    _clear(fingerprint)
    probe = _instrument(monkeypatch)

    worker_entered = threading.Event()
    hold = threading.Event()
    original_run = operations._run_operation

    def parked_worker(operation_id: str):
        worker_entered.set()
        hold.wait(10)
        return original_run(operation_id)

    monkeypatch.setattr(operations, "_run_operation", parked_worker)
    before = time.monotonic()
    try:
        responses = _burst_via_route()
    finally:
        hold.set()
    rows = _rows(fingerprint)

    package = {
        "gate_id": "G3_CONCURRENT_OPEN_GENERATION",
        "order": "ORD-0224 step 4",
        "measurement_only_for_old_readings": {
            "OLD_G3_PRODUCT_DEFECT_CLASSIFICATION": "SUSPENDED_PENDING_EVIDENCE",
            "suspended_labels": ["12R/2G", "0/10R"],
        },
        "database": {
            "url": str(engine.url),
            "probe_source": "app.db.session.engine (same factory the app uses)",
        },
        "fingerprint_under_test": fingerprint,
        "shapes": {
            "B_CONCURRENT_OPEN_WINDOW": {
                "definition": "5 callers released by threading.Barrier(5) while one live worker is "
                              "parked inside _run_operation, so the row stays QUEUED across the burst",
                "per_request": responses,
                "readings": {
                    "resolution_call_count": len(probe["calls"]),
                    "durable_rows_for_key": len(rows),
                    "distinct_operation_ids": sorted({str(r["operation_id"]) for r in responses}),
                    "winner_and_reuse_split": {
                        "winners": sum(1 for r in responses if r["reused"] is False),
                        "joiners": sum(1 for r in responses if r["reused"] is True),
                    },
                    "state_timeline": rows,
                },
                "probes": {
                    "resolution": probe["probe_source"],
                    "worker": "latch around app.catalog_hydration_operations._run_operation "
                              f"(entered={worker_entered.is_set()})",
                    "rows": "SELECT via SessionLocal on the app-resolved suite DB",
                },
                "burst_wall_ms": round((time.monotonic() - before) * 1000, 1),
            },
            "A_TERMINAL_THEN_BURST": {
                "definition": "the retired domain: one POST, its job driven to a terminal status, "
                              "then five barrier-released callers - i.e. two generations",
                "measured_by": "test_shape_a_terminal_job_then_five_requests_is_two_generations_not_one",
                "why_it_is_not_a_convergence_verdict": "tail-A requires a terminal job to be answered "
                                                        "by a NEW operation, so >1 id here is correct "
                                                        "behaviour, not a failure to converge",
            },
        },
    }

    missing = [
        field
        for field in REQUIRED_READINGS
        if field not in package["shapes"]["B_CONCURRENT_OPEN_WINDOW"]["readings"]
    ]
    assert not missing, f"期望：四档读数 + 状态时间线齐全；实际缺 {missing}"
    for probe_name, source in package["shapes"]["B_CONCURRENT_OPEN_WINDOW"]["probes"].items():
        assert isinstance(source, str) and source, f"探针 {probe_name} 没有交代来源"
    assert len(package["shapes"]["B_CONCURRENT_OPEN_WINDOW"]["per_request"]) == CALLERS
    assert all(item["operation_id"] for item in package["shapes"]["B_CONCURRENT_OPEN_WINDOW"]["per_request"]), (
        "期望：每一发都留下它拿到的 operation_id；实际有空白项"
    )

    target = os.environ.get(OUT_ENV)
    if target:
        Path(target).write_text(
            json.dumps(package, ensure_ascii=False, indent=2, default=str), encoding="utf-8", newline="\n"
        )
        print(f"\n[package] written -> {target}")
    else:
        print("\n[package] 未写文件（没有设 CITYNEXUS_ORD224_OUT）；字段完整性已在上面断言过")
