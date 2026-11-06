"""ORD-0219 (arbiter `5778314066`, ③ as ruled) - the four capability write routes share one
request-body model, so the two-layer refusal lives on `CapabilityInput` and covers them all.

Measured before writing doors (this is what the order is actually about):

* an **unknown** code was already refused, but by a generic unnamed body
  (`main.py` `_capability_input_rows`: `len(canonical) != len(raw)` ->
  "one or more capability codes are unsupported") - nothing to translate, nothing to branch on;
* a code that **exists but is only a preference** (`SLOW_PACE` and the other three
  `SOFT_CODES`) walked straight through with `severity="HARD"`, was stored, and only later the
  feasibility gate answered `UNSUPPORTED_HARD_CONSTRAINT`, which fails the *whole plan*
  (HARNESS 22:31: "只补 severity 会把『静默不执行』变成『整趟旅程不可规划』").

So the doors here are: every one of the four routes refuses both cells **by name**, **before** a row
exists; a legitimate SOFT preference is still writable; a legitimate HARD need is still writable;
and the executable set is *derived*, so it cannot drift from the gate's own list.

Store readback is the real check - `traveler_capabilities` is the single table both scopes write to
(journey-scoped rows carry `journey_id`), and every refusal case asserts the row count did not move.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.capabilities import (
    CAPABILITY_CODES,
    SOFT_CODES,
    hard_executable_capability_codes,
)
from app.db.models import TravelerCapability
from app.db.session import SessionLocal
from app.main import app
from app.replanning.feasibility import _CAPABILITY_GATE_CODES
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
JOURNEY_ID = "22222222-2222-4222-8222-222222222222"
TRAVELER_ID = "11111111-1111-4111-8111-111111111111"

ROUTES = (
    ("PUT", f"/api/v1/travelers/{TRAVELER_ID}/capabilities"),
    ("POST", f"/api/v1/travelers/{TRAVELER_ID}/capabilities/confirm"),
    ("PUT", f"/api/v1/journeys/{JOURNEY_ID}/capabilities"),
    ("POST", f"/api/v1/journeys/{JOURNEY_ID}/capabilities/confirm"),
)
SOFT_ONLY = sorted(SOFT_CODES)[0]


def _reset() -> None:
    response = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert response.status_code == 200, f"期望：夹具复位成功；实际 {response.status_code}"


def _rows() -> int:
    with SessionLocal() as session:
        return int(
            session.scalar(
                select(func.count()).select_from(TravelerCapability).where(
                    TravelerCapability.traveler_id == TRAVELER_ID
                )
            )
            or 0
        )


def _put(method: str, path: str, code: str, severity: str):
    return client.request(
        method,
        path,
        json=[{"code": code, "class": "PHYSICAL_LIMIT", "severity": severity, "params": {}}],
    )


def _validation_errors(response) -> list[dict]:
    """`main.py`'s handler nests the per-field errors under `error.details.errors`."""

    body = response.json()
    error = body.get("error") if isinstance(body, dict) else None
    details = (error or {}).get("details") or {}
    return list(details.get("errors") or [])


def _error_type(response) -> str:
    errors = _validation_errors(response)
    return str(errors[0].get("type")) if errors else f"<no details.errors[] in {sorted((response.json().get('error') or {}))!r}>"


def _message_key(response) -> str:
    errors = _validation_errors(response)
    return str(errors[0].get("message_key")) if errors else ""


# --------------------------------------------------------------------------------------
# cell 1: unknown code -> named 422 on all four doors, and no row anywhere
# --------------------------------------------------------------------------------------
def test_1_every_capability_route_refuses_an_unknown_code_by_name() -> None:
    _reset()
    before = _rows()
    for method, path in ROUTES:
        response = _put(method, path, "NO_SUCH_CAPABILITY_AT_ALL", "HARD")
        assert response.status_code == 422, (
            f"期望：{method} {path} 对词表外 code 一律写入端拒（422）；实际 {response.status_code}"
        )
        assert _error_type(response) == "unknown_rule_code", (
            f"期望：结构化名字 unknown_rule_code（屏幕按 key 分支，不读英文句子）；"
            f"实际 {method} {path} -> {_error_type(response)!r}"
        )
    assert _rows() == before, (
        f"期望：四次被拒之后库里行数不变（{before}）；实际 {_rows()}"
    )


# --------------------------------------------------------------------------------------
# cell 2: a real preference declared HARD -> its own name, still 0 rows
# --------------------------------------------------------------------------------------
def test_2_a_preference_declared_hard_is_refused_under_its_own_name_on_all_four_routes() -> None:
    _reset()
    assert SOFT_ONLY not in hard_executable_capability_codes(), (
        f"前提：{SOFT_ONLY} 是偏好、不在可执行集里"
    )
    before = _rows()
    for method, path in ROUTES:
        response = _put(method, path, SOFT_ONLY, "HARD")
        assert response.status_code == 422, (
            f"期望：{method} {path} 对『偏好 + HARD』拒（改前它 2xx 落库，之后由 feasibility 把整趟判红）；"
            f"实际 {response.status_code}"
        )
        assert _error_type(response) == "hard_rule_not_executable", (
            f"期望：这一格有自己的名字；实际 {_error_type(response)!r}"
        )
    assert _rows() == before, f"期望 0 行落库；实际 {before} -> {_rows()}"


def test_3_the_two_refusals_are_structurally_different() -> None:
    _reset()
    method, path = ROUTES[0]
    unknown = _put(method, path, "NO_SUCH_CAPABILITY_AT_ALL", "HARD")
    unexecutable = _put(method, path, SOFT_ONLY, "HARD")
    assert _error_type(unknown) != _error_type(unexecutable), (
        f"期望：两种拒绝在 `type` 上就不同（仲裁：UNKNOWN_RULE_CODE / HARD_RULE_NOT_EXECUTABLE 必须分开）；"
        f"实际两边都是 {_error_type(unknown)!r}"
    )
    assert _message_key(unknown) != _message_key(unexecutable), (
        f"期望：屏幕拿到两个不同的 message_key；实际 "
        f"{_message_key(unknown)!r} vs {_message_key(unexecutable)!r}"
    )
    for response in (unknown, unexecutable):
        assert "unsupported" not in str(response.json()), (
            "期望：不再是那句无名的『one or more capability codes are unsupported』"
            "（它把两种情况并成一句，而且只能靠读英文区分）"
        )


# --------------------------------------------------------------------------------------
# the other two cells: SOFT stays writable, a real hard need stays writable
# --------------------------------------------------------------------------------------
def test_4_a_soft_preference_is_still_written() -> None:
    _reset()
    before = _rows()
    response = _put("PUT", f"/api/v1/travelers/{TRAVELER_ID}/capabilities", SOFT_ONLY, "SOFT")
    assert response.status_code == 200, (
        f"期望：SOFT 不因『不属于可执行集』被拒（仲裁第二格）；实际 {response.status_code} "
        f"{str(response.text)[:180]}"
    )
    codes = {str(item.get("code")) for item in response.json()["capabilities"]}
    assert SOFT_ONLY in codes, f"期望：回执里真有这条偏好；实际 {sorted(codes)}"
    assert _rows() > before, f"期望：确实落库（{before} -> ?）；实际 {_rows()}"


def test_5_real_hard_needs_are_not_killed_by_the_vocabulary_guard() -> None:
    """The over-refusal risk in a write guard: a need we *can* execute must still get through.

    Measured route rules, which this door encodes rather than assumes: a `HARD` capability is only
    writable through `/confirm` (the plain `PUT` answers 409 "hard capability needs require
    /confirm"), so on the `PUT` routes the correct outcome is **409, not 422** - the vocabulary
    guard has to let the request live long enough to meet the next rule.
    """

    _reset()
    for code in ("STEP_FREE_REQUIRED", "AVOID_STAIRS", "STROLLER", "MAX_DAILY_WALKING_M"):
        assert code in hard_executable_capability_codes(), f"前提：{code} 属于可执行集"
        for method, path in ROUTES:
            response = _put(method, path, code, "HARD")
            refusal = _error_type(response) if response.status_code == 422 else ""
            assert refusal not in {"unknown_rule_code", "hard_rule_not_executable"}, (
                f"期望：可执行的硬需求不会被词表守卫拒掉；实际 {method} {path} {code} -> "
                f"{response.status_code} type={refusal!r}"
            )
            expected = 409 if method == "PUT" else 200
            assert response.status_code == expected, (
                f"期望：{method} {path} 对 HARD 需求给 {expected}"
                f"（PUT 走的是『必须 /confirm』那条既有规则）；实际 {response.status_code} "
                f"{str(response.text)[:150]}"
            )
    stored = {
        str(item.get("code"))
        for item in client.get(f"/api/v1/travelers/{TRAVELER_ID}/capabilities").json()["capabilities"]
    }
    assert {"STEP_FREE_REQUIRED", "AVOID_STAIRS", "STROLLER", "MAX_DAILY_WALKING_M"} <= stored, (
        f"期望：四条 /confirm 写入都真的落了库；实际库里 {sorted(stored)}"
    )


# --------------------------------------------------------------------------------------
# the anti-drift door: the set is derived from the gate's own list, not typed a second time
# --------------------------------------------------------------------------------------
def test_6_the_executable_set_equals_the_gate_and_partitions_the_vocabulary() -> None:
    derived = set(hard_executable_capability_codes())
    assert derived == set(_CAPABILITY_GATE_CODES), (
        f"期望：写入端用的可执行集 == feasibility 自己那张（谁也不能单方面扩/缩）；"
        f"只在写入端 {sorted(derived - set(_CAPABILITY_GATE_CODES))} / "
        f"只在求值层 {sorted(set(_CAPABILITY_GATE_CODES) - derived)}"
    )
    third_bucket = sorted(
        code for code in CAPABILITY_CODES if (code in derived) == (code in SOFT_CODES)
    )
    assert not third_bucket, (
        f"期望：词表被干净地分成『可执行』与『仅偏好』两格，没有第三格；实际 {third_bucket}"
    )
    assert len(CAPABILITY_CODES) == len(derived) + len(SOFT_CODES), (
        f"期望：{len(CAPABILITY_CODES)} = 可执行 {len(derived)} + 偏好 {len(SOFT_CODES)}；"
        f"两边都不等于 15 就说明有一份手抄名单在别处"
    )
