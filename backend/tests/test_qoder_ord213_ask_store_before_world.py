"""ORD-0213 weak fix, released by the commander at 00:5x - *ask the store before asking the world*.

The defect this pins is a **sequence**, not a race: `submit_operation` computed its idempotency key
*out of* the destination resolution, so a request could not look the job up until it had already paid
for the answer.  Five taps therefore meant up to five provider passes, and reuse only ever happened
when somebody else's row had been committed long enough to be seen.  §7 ("provider 调用不得因并发而
翻倍") had no guard at all.

Scheduling, not gambling: every door below drives the product's own `start_worker=False` (a job
stays `QUEUED`, so "still open when the next tap arrives" is a set-up fact, not a timing wish) and
`_run_operation` (the worker body, in-process and synchronous, so "already terminal" is equally a
set-up fact).  No sleep, no retry, no lowered concurrency, no relaxed expectation anywhere here.

What these doors also refuse to pretend: the fix does **not** close g3.  A job that has already
reached a terminal status is still answered by a *new* job - the signed tail-A rule - so `w2`
asserts exactly that, and the closure of ORD-0213 remains the strong fix the commander escalated.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

import app.catalog_hydration_operations as operations
from app.db.models import CatalogHydrationOperation
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
UNRESOLVED = "贡山独龙族怒族自治县"
OTHER_UNRESOLVED = "一个从没听说过的地方XYZ"


@pytest.fixture(autouse=True)
def _empty_operation_store():
    response = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert response.status_code == 200, f"夹具复位失败：{response.status_code}"
    # Measured, not assumed: `demo/reset` runs `services.reset_all`, which deletes 52 tables, and
    # `catalog_hydration_operations` is **not** one of them (the name has 0 hits in
    # `app/services.py`) — so earlier doors' jobs survived and `_row_count()` read 3 instead of 1.
    # Every assertion below counts the jobs for *this* question, which only means something on a
    # table that starts empty, so it is emptied here by name for the single table these five doors
    # read.  This is scaffolding hygiene, not isolation for a race: no door here runs concurrent.
    with SessionLocal() as session:
        session.execute(delete(CatalogHydrationOperation))
        session.commit()
    yield


def _counting_resolve(monkeypatch):
    """Wrap the product's own resolver to count passes - it still runs, so nothing is mocked out."""

    original = operations.resolve_destination
    calls: list[str] = []

    def counting(session, query):
        calls.append(str(query))
        return original(session, query)

    monkeypatch.setattr(operations, "resolve_destination", counting)
    return calls


def _row_count() -> int:
    """How many jobs the store holds.

    Order-free on purpose: this model's primary key is a `String` id, so there is no ORM
    `rowid` to sort by, and nothing below needs an ordering - only the count and the states.
    """

    with SessionLocal() as session:
        return int(
            session.scalar(select(func.count()).select_from(CatalogHydrationOperation))
        )


def _statuses() -> list[str]:
    with SessionLocal() as session:
        return sorted(
            {str(value) for value in session.scalars(select(CatalogHydrationOperation.status)).all()}
        )


def _submit(query: str, *, start_worker: bool = False) -> tuple[dict, bool]:
    """Submit and read back what the door needs *while the session is still open*."""

    with SessionLocal() as session:
        row, reused = operations.submit_operation(session, query, start_worker=start_worker)
        seen = {
            "id": str(row.id),
            "fingerprint": str(row.fingerprint),
            "status": str(row.status),
        }
    return seen, reused


# --------------------------------------------------------------------------------------
# w1 - the released property: a repeat of a question that is still open costs one resolution
# --------------------------------------------------------------------------------------
def test_w1_a_repeat_of_an_open_question_asks_the_provider_once_not_twice(monkeypatch) -> None:
    calls = _counting_resolve(monkeypatch)
    first, first_reused = _submit(UNRESOLVED)
    after_first_submit = len(calls)
    second, second_reused = _submit(UNRESOLVED)

    assert first_reused is False and second_reused is True, (
        f"期望：第一发开始工作、第二发加入它；实际 reused={first_reused}/{second_reused}"
    )
    assert first["id"] == second["id"], (
        f"期望：两次拿到同一个 operation_id（一个开放问题=一个作业）；实际 {first['id']} / {second['id']}"
    )
    # ORD-0224 步 1 的口径原话：第二次 submit 的 **additional provider resolution calls = 0**。
    assert len(calls) - after_first_submit == 0, (
        f"期望：已有 OPEN 同键作业时，重复 submit 带来的额外解析次数 = 0；"
        f"实际额外 {len(calls) - after_first_submit} 次（全部={calls}）"
    )
    assert calls == [UNRESOLVED], (
        f"期望：提供方/目录只被问了一次——改前是两次，因为键要等工作做完才算得出来；"
        f"实际 {calls}"
    )
    assert _row_count() == 1, f"期望：库里只有一行；实际 {_row_count()}"
    assert first["fingerprint"] == second["fingerprint"], (
        "期望：加入者看到的键与开始者一致（同一问题的两次到达，不是两个问题）"
    )


# --------------------------------------------------------------------------------------
# w2 - the limit of this fix, asserted so nobody oversells it
# --------------------------------------------------------------------------------------
def test_w2_a_terminal_job_is_still_answered_by_a_new_attempt(monkeypatch) -> None:
    """tail-A stays intact, and this is why the weak fix does NOT close g3."""

    calls = _counting_resolve(monkeypatch)
    first, _ = _submit(UNRESOLVED)
    after_first_submit = len(calls)
    operations._run_operation(first["id"])  # the worker body, synchronously: no sleep, no gamble
    after_worker = len(calls)
    with SessionLocal() as session:
        closed = session.get(CatalogHydrationOperation, first["id"])
        assert str(closed.status) in operations.TERMINAL_STATUSES, (
            f"前提：作业已被 worker 跑到终态；实际 {closed.status}（outcome={closed.outcome}）"
        )
    second, reused = _submit(UNRESOLVED)

    assert reused is False and second["id"] != first["id"], (
        f"期望：终态之后的同指纹仍然另开一枚（已签收的 tail-A）——正因为这样，本修复不结 g3；"
        f"实际 reused={reused} id {first['id']} -> {second['id']}"
    )
    # Per-phase, not a total: submit#1 resolves once to *create* the job, the worker resolves again
    # for itself (`resolve_destination` is also called at catalog_hydration_operations.py), and
    # submit#2 resolves once more because a terminal row is not something to join.  Naming each
    # phase is what keeps a future drop from hiding inside an averaged count.
    assert (after_first_submit, after_worker - after_first_submit, len(calls) - after_worker) == (
        1,
        1,
        1,
    ), f"期望：受理1/worker/受理2 各解一次；实际 {(after_first_submit, after_worker - after_first_submit, len(calls) - after_worker)} 全部={calls}"


# --------------------------------------------------------------------------------------
# w3 - the pre-check cannot merge two different questions
# --------------------------------------------------------------------------------------
def test_w3_the_pre_check_only_joins_the_same_normalised_question() -> None:
    from app.anywhere_capability import capability_fingerprint, pending_request_fingerprint

    assert pending_request_fingerprint(UNRESOLVED) == capability_fingerprint(
        None, resolver_query_normalized="贡山独龙族怒族自治县"
    ), "期望：预查用的键就是未具名作业实际登记的那个键（同一表达式，不能各算各的）"
    assert pending_request_fingerprint("  贡山 独龙族 怒族自治县 ") == pending_request_fingerprint(
        UNRESOLVED
    ), "期望：空白/标点折叠后仍是同一个问题（与存储侧同一规范化）"
    assert pending_request_fingerprint(UNRESOLVED) != pending_request_fingerprint(
        OTHER_UNRESOLVED
    ), "期望：不同问题不会被这一步并成一件"

    first, _ = _submit(UNRESOLVED)
    other, other_reused = _submit(OTHER_UNRESOLVED)
    assert other_reused is False and other["id"] != first["id"], (
        f"期望：另一条未具名问题另开一枚；实际 reused={other_reused}"
    )
    assert _row_count() == 2, f"期望：库里两行、两个问题；实际 {_row_count()}"


# --------------------------------------------------------------------------------------
# w4 - the one open status this step must NOT join
# --------------------------------------------------------------------------------------
def test_w4_a_job_somebody_walked_away_from_is_not_a_job_to_join(monkeypatch) -> None:
    """What the new step declines, and what the old step still (openly) does.

    `ABANDON_REQUESTED` belongs to `OPEN_STATUSES`, so the lookup that runs *after* resolution
    has always reused it, and the released weak fix does not touch that semantic — the only
    thing that may change it is the escalated strong fix.  So this door does not pretend to
    close that gap; it pins the half that is actually new (the pre-check must not be what
    joins an abandoning job) and then *records* the rest as the reason g3 stays open.
    """

    calls = _counting_resolve(monkeypatch)
    first, _ = _submit(UNRESOLVED)
    after_first_submit = len(calls)
    with SessionLocal() as session:
        session.execute(
            update(CatalogHydrationOperation)
            .where(CatalogHydrationOperation.id == first["id"])
            .values(status=operations.ABANDON_REQUESTED)
        )
        session.commit()

    second, reused = _submit(UNRESOLVED)

    # The discriminating assertion: had the pre-check counted an abandoning job as "open", the
    # store would have answered the repeat before the resolver and the second phase would be 0.
    assert (len(calls) - after_first_submit) == 1, (
        "期望：『先查存储』这一步不接管 `ABANDON_REQUESTED`（它正奔向 ABANDONED），所以重复提问仍然被解一次"
        f"（若预查把它也算成开放作业，这里就是 0 次）；实际第一次受理解了 {after_first_submit} 次、"
        f"重复提问后又多了 {len(calls) - after_first_submit} 次，全部={calls}"
    )
    # Measured remainder, asserted as-is so nobody later reads this door as a fix for it.
    assert reused is True and second["id"] == first["id"], (
        "实测（非期望的产品理想）：改前就有的解析后查找回会把新 caller 挂到一件没人等的作业上——"
        f"这正是本修复不结 g3 的所在，要改它得等强修裁决；实际 reused={reused} id={first['id']}"
    )
    assert _row_count() == 1 and _statuses() == [operations.ABANDON_REQUESTED], (
        f"期望：没有新作业被开出来，唯一那行仍在被弃的路上；实际 {_row_count()} 行 {_statuses()}"
    )


# --------------------------------------------------------------------------------------
# w5 - the published key and the looked-up key are the same object, seen through the route
# --------------------------------------------------------------------------------------
def test_w5_the_route_publishes_the_key_this_step_looks_up_by() -> None:
    from app.anywhere_capability import pending_request_fingerprint

    response = client.post("/api/v1/catalog-hydrations", json={"query": UNRESOLVED})
    assert response.status_code in (200, 202), (
        f"期望：受理；实际 {response.status_code} {response.text[:120]}"
    )
    body = response.json()
    with SessionLocal() as session:
        row = session.get(CatalogHydrationOperation, body["operation_id"])
        assert row is not None, f"期望：回执里的 operation_id 在库里真有一行；实际 {body['operation_id']}"
    # Premise, named: this query stays unnamed under the default resolver, which is exactly why the
    # stored key is the pending-request key.  If a future tree resolves 贡山 to a canonical place,
    # this equality is expected to break - the key would then be the destination's, not the question's.
    assert body.get("fingerprint") == row.fingerprint, (
        f"期望：回执说的键就是登记着的键；实际回执 {body.get('fingerprint')} vs 库 {row.fingerprint}"
    )
    assert row.fingerprint == pending_request_fingerprint(UNRESOLVED), (
        "期望：一条未具名问题登记的键，正是『先查存储』这一步用来查的那个键——否则先查后解会永远"
        f"查不到东西（键各算各的）；实际登记 {row.fingerprint}"
    )
