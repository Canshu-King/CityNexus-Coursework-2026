"""ORD-0219 (arbiter `5778314066`, first of the two layers) - the write side refuses what the
gate cannot execute, and it refuses the two reasons under two different names.

The defect: `ConstraintInput.rule_code` is a free string, and `POST /journeys/{id}/constraints`
stored whatever it was given.  A `HARD` row whose code the feasibility gate has no branch for
survives the write and surfaces much later as `UNSUPPORTED_HARD_CONSTRAINT`, which fails the
whole plan - so a typo could make a journey unplannable at the moment someone selected a plan,
with nothing on the write asking "is this a rule we have?".  A `SOFT` row with such a code was
worse in a quieter way: stored, counted, displayed, and never executed.

The four cells this door family pins (they are the arbiter's boundary, not my vocabulary):

    unknown code, either kind   -> 422 UNKNOWN_RULE_CODE,          nothing stored
    real code, not executable,
        kind=HARD               -> 422 HARD_RULE_NOT_EXECUTABLE,   nothing stored
        kind=SOFT               -> 201, stored (a preference the gate ignores is still one)
    executable hard code        -> 201, stored; whether it *blocks* is decided later by
                                   feasibility, from evidence

Two rules the doors hold the implementation to, because they are the parts most easily lost in a
later refactor: the two refusal names must stay **distinct** (one envelope code covering both is
the collapse the ruling rejected), and the sets must be **derived from the evaluator** rather
re-typed - including the sealed demo's own rows, which this layer must not break (they are read
from the fixture file here, not asserted from memory).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.capabilities import capability_constraints, extract_capabilities_from_text
from app.db.models import JourneyConstraint
from app.db.session import SessionLocal
from app.main import app
from app.replanning.feasibility import (
    CANONICAL_RULE_CODES,
    EXECUTED_HARD_RULE_CODES,
    SOFT_ONLY_RULE_CODES,
    _CAPABILITY_GATE_CODES,
    constraint_write_refusal,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
JOURNEY_ID = "22222222-2222-4222-8222-222222222222"
DEMO_FIXTURE = Path(__file__).resolve().parents[2] / "data" / "mock" / "demo_journey.json"


def _reset() -> None:
    response = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert response.status_code == 200, f"期望：演示夹具复位成功；实际 {response.status_code}"


def _stored() -> list[tuple[str, str]]:
    """What the store holds for this journey - HTTP can refuse and still be believed."""

    with SessionLocal() as session:
        rows = session.scalars(
            select(JourneyConstraint).where(JourneyConstraint.journey_id == JOURNEY_ID)
        ).all()
    return sorted((str(row.kind), str(row.rule_code)) for row in rows)


def _post(kind: str, rule_code: str, category: str = "test"):
    return client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/constraints",
        json={"kind": kind, "category": category, "rule_code": rule_code, "params": {}},
    )


def _error(response) -> dict:
    body = response.json()
    return body.get("error") if isinstance(body, dict) else {}


# --------------------------------------------------------------------------------------
# cell 1: a code the product has never heard of is refused for both kinds, and not stored
# --------------------------------------------------------------------------------------
def test_1a_an_unknown_rule_code_is_refused_before_it_can_be_stored() -> None:
    _reset()
    before = _stored()
    response = _post("SOFT", "NO_SUCH_RULE_AT_ALL")
    error = _error(response)
    assert response.status_code == 422, (
        f"期望：词表外的 rule_code 在写入端就被拒（422）；实际 {response.status_code} "
        f"body={json.dumps(error, ensure_ascii=False)[:200]}"
    )
    assert error.get("code") == "UNKNOWN_RULE_CODE", (
        f"期望：具名 UNKNOWN_RULE_CODE；实际 code={error.get('code')!r}"
    )
    assert error.get("reason_code") == "UNKNOWN_RULE_CODE", (
        f"期望：reason_code 同名，屏幕不必读英文句子；实际 {error.get('reason_code')!r}"
    )
    assert _stored() == before, (
        "期望：被拒的写入不留行（422 之后库里必须一模一样）；"
        f"实际改前 {before} → 改后 {_stored()}"
    )


def test_1b_the_unknown_refusal_gives_a_screen_a_key_and_the_offending_value() -> None:
    _reset()
    error = _error(_post("HARD", "NO_SUCH_RULE_AT_ALL"))
    details = error.get("details") or {}
    assert error.get("message_key") == "constraint.unknown_rule_code", (
        f"期望：稳定的 message_key（ORD-0055：屏幕不许翻译英文句子）；"
        f"实际 {error.get('message_key')!r}"
    )
    assert details.get("rule_code") == "NO_SUCH_RULE_AT_ALL", (
        f"期望：details 说清是哪一条被拒；实际 {details!r}"
    )
    assert isinstance(details.get("canonical_rule_codes"), list) and details["canonical_rule_codes"], (
        f"期望：details 带可写的词表，调用方才知道改成什么；实际 {sorted(details)!r}"
    )


# --------------------------------------------------------------------------------------
# cell 2: a real rule the gate cannot execute, asked as HARD, gets its own name
# --------------------------------------------------------------------------------------
def test_2_a_real_rule_the_gate_cannot_execute_is_refused_under_its_own_name() -> None:
    _reset()
    before = _stored()
    assert constraint_write_refusal("HARD", "SLOW_PACE") == "HARD_RULE_NOT_EXECUTABLE", (
        "前提：SLOW_PACE 在词表内但不属于可执行集（它正是会被求值层拒绝的那一族）"
    )
    response = _post("HARD", "SLOW_PACE", category="pace")
    error = _error(response)
    assert response.status_code == 422, (
        f"期望：HARD + 不可执行 ⇒ 写入端 422；实际 {response.status_code}"
    )
    assert error.get("code") == "HARD_RULE_NOT_EXECUTABLE", (
        f"期望：具名 HARD_RULE_NOT_EXECUTABLE（不能和 UNKNOWN_RULE_CODE 混成一句）；"
        f"实际 {error.get('code')!r}"
    )
    assert error.get("message_key") == "constraint.hard_rule_not_executable", (
        f"期望：这一格有自己的 message_key；实际 {error.get('message_key')!r}"
    )
    assert _stored() == before, (
        f"期望：不落库；实际改前 {before} → 改后 {_stored()}"
    )


def test_3_the_two_refusals_do_not_collapse_into_one_name() -> None:
    _reset()
    unknown = _error(_post("HARD", "NO_SUCH_RULE_AT_ALL"))
    not_executable = _error(_post("HARD", "SLOW_PACE", category="pace"))
    assert unknown.get("code") != not_executable.get("code"), (
        f"期望：两种拒绝的 code 不同（仲裁原话：UNKNOWN_RULE_CODE / HARD_RULE_NOT_EXECUTABLE "
        f"必须分开）；实际两边都是 {unknown.get('code')!r}"
    )
    assert unknown.get("message_key") != not_executable.get("message_key"), (
        "期望：屏幕拿到的是两个不同的键，不是一句话换两个值；"
        f"实际 {unknown.get('message_key')!r} vs {not_executable.get('message_key')!r}"
    )


# --------------------------------------------------------------------------------------
# cell 3: SOFT is not refused for being unexecutable - it is stored and left to ranking
# --------------------------------------------------------------------------------------
def test_4_a_soft_preference_the_gate_ignores_is_still_written() -> None:
    _reset()
    before = _stored()
    response = _post("SOFT", "SLOW_PACE", category="pace")
    assert response.status_code == 201, (
        f"期望：SOFT 不因『不属于 hard-executable』被拒（仲裁第二格）；"
        f"实际 {response.status_code} body={json.dumps(_error(response), ensure_ascii=False)[:200]}"
    )
    after = _stored()
    assert ("SOFT", "SLOW_PACE") in after, (
        f"期望：库里真有这一行（(kind, rule_code) 直接读表，不信 HTTP）；实际 {after}（改前 {before}）"
    )


# --------------------------------------------------------------------------------------
# cell 4: an executable hard rule is stored, and the *decision* stays with feasibility
# --------------------------------------------------------------------------------------
def test_5_an_executable_hard_rule_is_stored_and_not_prejudged_by_the_write_layer() -> None:
    _reset()
    assert "STROLLER" in EXECUTED_HARD_RULE_CODES, "前提：STROLLER 属于可执行集（求值层读证据）"
    response = _post("HARD", "STROLLER", category="accessibility")
    assert response.status_code == 201, (
        f"期望：可执行的硬约束在写入端被受理——挡不挡由 feasibility 按证据决定；"
        f"实际 {response.status_code} body={json.dumps(_error(response), ensure_ascii=False)[:200]}"
    )
    body = response.json()
    assert (body.get("kind"), body.get("rule_code")) == ("HARD", "STROLLER"), (
        f"期望：写回来的就是刚写的那一条；实际 {sorted(body)[:8]}"
    )
    assert ("HARD", "STROLLER") in _stored(), (
        f"期望：落库（真持久化，不是只回显）；实际 {_stored()}"
    )


# --------------------------------------------------------------------------------------
# the layer must not break what the product itself writes
# --------------------------------------------------------------------------------------
def test_6_every_rule_code_the_sealed_demo_seed_writes_stays_writable() -> None:
    """The demo seed is a writer too; a vocabulary layer that refuses its own fixture is a defect.

    The codes are read out of `data/mock/demo_journey.json` at test time, not typed here, so the
    fixture can grow and this door keeps meaning something.
    """

    fixture = json.loads(DEMO_FIXTURE.read_text(encoding="utf-8"))
    rows = [(str(item["kind"]), str(item["rule_code"])) for item in fixture.get("constraints", [])]
    assert rows, f"期望：{DEMO_FIXTURE.name} 里带约束行；实际读到 {rows!r}"
    refused = [(kind, code) for kind, code in rows if constraint_write_refusal(kind, code)]
    assert not refused, (
        "期望：封存演示自己写的每一条都能通过写入端两层校验；实际被拒 "
        f"{refused}（⇒ 这一层会把 demo/reset 变成半个演示）"
    )
    _reset()
    stored = _stored()
    missing = [row for row in sorted(rows) if row not in stored]
    assert not missing, (
        f"期望：复位后演示库里真有这些行；实际缺 {missing}，读到 {stored}"
    )


def test_7_the_written_sets_are_derived_from_the_evaluator_not_retyped() -> None:
    """Anti-drift: the guard may never be stricter or laxer than the gate it stands in for."""

    assert _CAPABILITY_GATE_CODES <= EXECUTED_HARD_RULE_CODES, (
        f"期望：求值层能读的 11 个能力码全在可执行集里；"
        f"实际缺 {sorted(_CAPABILITY_GATE_CODES - EXECUTED_HARD_RULE_CODES)}"
    )
    assert SOFT_ONLY_RULE_CODES == CANONICAL_RULE_CODES - EXECUTED_HARD_RULE_CODES, (
        "期望：词表 = 可执行集 ∪ 仅记录集，两者不重叠也不漏（第四格与第一格的分界就靠这个）；"
        f"实际 词表 {sorted(CANONICAL_RULE_CODES)} − 可执行 {sorted(EXECUTED_HARD_RULE_CODES)} "
        f"= {sorted(CANONICAL_RULE_CODES - EXECUTED_HARD_RULE_CODES)}"
    )
    agree = [
        code
        for code in sorted(CANONICAL_RULE_CODES)
        if (constraint_write_refusal("HARD", code) is None)
        != (code in EXECUTED_HARD_RULE_CODES)
    ]
    assert not agree, f"期望：写入端与求值层对每一个词表内 code 的判断一致；实际不一致 {agree}"


# --------------------------------------------------------------------------------------
# 五步① (the same order's first step): a hard-asserted need must reach the gate at all
# --------------------------------------------------------------------------------------
def test_8_an_asserted_stroller_need_becomes_a_gate_a_mentioned_one_does_not() -> None:
    def severities(text: str) -> dict[str, str]:
        return {
            str(item.get("code")): str(item.get("severity"))
            for item in extract_capabilities_from_text(text)
        }

    asserted = severities("我带着婴儿车，必须走无台阶的路线")
    mentioned = severities("我会带一个婴儿车")
    assert asserted.get("STROLLER") == "HARD", (
        f"期望：明说的婴儿车需求是 HARD（此前没有 severity 参数，一律落 SOFT，"
        f"于是 `capability_constraints` 把它丢掉，门永远看不到它）；实际 {asserted!r}"
    )
    assert mentioned.get("STROLLER") == "STRONG", (
        f"期望：只是提到 ⇒ STRONG，参与排序但不作为硬约束挡人；实际 {mentioned!r}"
    )
    gated = {
        str(item["rule_code"])
        for item in capability_constraints([{"code": "STROLLER", "severity": "HARD", "active": True}])
    }
    not_gated = {
        str(item["rule_code"])
        for item in capability_constraints([{"code": "STROLLER", "severity": "STRONG", "active": True}])
    }
    assert "STROLLER" in gated and "STROLLER" not in not_gated, (
        f"期望：只有 HARD 进求值层（正向不误挡、断言才 gate）；"
        f"实际 HARD→{sorted(gated)} STRONG→{sorted(not_gated)}"
    )
