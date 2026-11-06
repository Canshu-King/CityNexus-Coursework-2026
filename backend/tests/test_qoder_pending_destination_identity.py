"""ORD-0167 - lane C's doors for `docs/pending_destination_identity_policy.md` @ `e4b7faa3`.

Freeze sentence: *Unknown destination text may identify a request to resolve a destination; it does
not identify a destination.*  The closure freeze sentence is *Resolve a real place, preserve the
user's raw request, and carry only real canonical identity into planning.*

What was broken, in one line: `anywhere_capability.py` put the traveller's own words into the
`canonical_destination_id` column behind a `pending:` prefix (and, before `23bdf9f`, a sha1 digest
of them).  That column is what fingerprints, idempotency and catalog joins believe in, so a query
string was being used as a place identity - and `_normalise()` had already eaten its punctuation,
which is the database tampering with a witness statement.

Policy gate -> node id (the arbiter's twelve rows; 11 and 12 are reverse mutants and live in
`review/mutate_ord167.py`, so they are named here as the doors they must redden):

    1  unresolved raw input never appears in canonical_destination_id ... g1
    2  NO_MATCH -> canonical null ...................................... g2
    3  RUNTIME_FAILURE -> canonical null + retryable named failure ..... g3
    4  AMBIGUOUS -> canonical null + candidates kept separately ........ g4
    5  RESOLVED -> real canonical identity + provenance ................ g5
    6  exact raw stored apart from the normalised resolver query ....... g6
    7  punctuation-loss regression (`zzxqvw-不存在县`) .................. g7
    8  pending key cannot be consumed as planner/place identity ........ g8
    9  repeated unresolved requests obey idempotency, invent nothing ... g9
    10 legacy `pending:` rows migrate in place and are not deleted ..... g10
    11 mutant: `pending:<raw>` written back into canonical ............. M1 -> g1
    12 mutant: `destination_pending_key` used as identity/join key ..... M2 -> g8
    +  extra doors this build needs: blank input (g11), no writer left
       minting text-derived identities anywhere in `backend/app` (g12).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

import app.anywhere_capability as capability_module
from app.db.models import CatalogHydrationOperation, Place
from app.db.session import SessionLocal, ensure_schema, make_engine
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
CAPABILITY = "/api/v1/destinations/capability"
HYDRATIONS = "/api/v1/catalog-hydrations"

# The traveller's own words, exactly as typed.  `zzxqvw-不存在县` is the standing nonsense
# regression; the hyphen is the whole point: `_normalise` deletes it, so any column that shows
# the de-punctuated form while claiming to be "what the user wrote" is lying.
RAW_NO_COUNTY = "zzxqvw-不存在县"
NORMALISED_NO_COUNTY = "zzxqvw不存在县"
UNSEEN_COUNTY = "贡山独龙族怒族自治县"
STAGE_CITY = "CN-QINGPU"

# The shape the pre-ORD-0167 build actually wrote: identity NOT NULL, no label column, no pending
# key, and an outcome CHECK that cannot hold the two new terminal names.
LEGACY_DDL = """
CREATE TABLE catalog_hydration_operations (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    canonical_destination_id TEXT NOT NULL,
    catalog_schema_version TEXT NOT NULL,
    readiness_policy_version TEXT NOT NULL,
    provider_policy_version TEXT NOT NULL,
    journey_id TEXT,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN
        ('QUEUED','RUNNING','ABANDON_REQUESTED','COMPLETED','FAILED','UNAVAILABLE','ABANDONED')),
    phase TEXT,
    request_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    outcome TEXT,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    abandon_requested_at TEXT,
    updated_at TEXT NOT NULL
);
"""


@pytest.fixture()
def seeded(monkeypatch) -> str:
    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, reset.text
    # Jobs are per-stage state, not demo seed: `/demo/reset` does not clear this table, and an
    # unfinished row is *reused by fingerprint* by whoever asks for the same words next - so an
    # orphan left here becomes somebody else's 15-second timeout.  Several doors in this file
    # stage a job with no worker on purpose, which makes the teardown load-bearing.
    _clear_operations()
    yield str(reset.json()["traveler_id"])
    _clear_operations()


def _clear_operations() -> None:
    with SessionLocal() as session:
        session.execute(CatalogHydrationOperation.__table__.delete())
        session.commit()


def _capability(query: str) -> dict:
    read = client.get(CAPABILITY, params={"query": query})
    assert read.status_code == 200, read.text
    return read.json()


def _submit(query: str) -> dict:
    posted = client.post(HYDRATIONS, json={"query": query})
    assert posted.status_code in (200, 202), posted.text
    return posted.json()


def _row(operation_id: str) -> CatalogHydrationOperation:
    with SessionLocal() as session:
        row = session.scalar(
            select(CatalogHydrationOperation).where(
                CatalogHydrationOperation.id == operation_id
            )
        )
    assert row is not None, f"SETUP 失败：库里没有操作 {operation_id}"
    return row


# ---------------------------------------------------------------------------------------------
# Policy gate 1 - user text must stop being an identifier
# ---------------------------------------------------------------------------------------------
def test_g1_unresolved_text_never_reaches_the_identity_column(seeded) -> None:
    """问不中 ⇒ `canonical_destination_id` 是 null，不是 `pending:<原文>`，也不是原文本身。"""

    for query in (UNSEEN_COUNTY, RAW_NO_COUNTY, "zzxqvw不存在县", "不存在的名字"):
        body = _capability(query)
        assert body.get("canonical_destination_id") is None, (
            f"期望：问「{query}」解析不出身份时 canonical_destination_id=null；实际："
            f"{body.get('canonical_destination_id')!r}。这一列是幂等指纹与目录 join 相信的东西，"
            "把查询文本放进去就等于给一句话发了身份证（仲裁 e4b7faa3 ①）"
        )
        identity_fields = {
            key: value
            for key, value in body.items()
            if isinstance(value, str) and query in value.replace("-", "")
            and key != "query" and key != "pending_destination_label"
            and key != "resolver_query_normalized"
        }
        assert not identity_fields, (
            f"期望：除 `query` / `pending_destination_label` / `resolver_query_normalized` 三列外，"
            f"回答里任何字段都不得含用户原文（「{query}」）；实际：{sorted(identity_fields)}"
        )
    # and the job must store the same verdict the read gave
    job = _submit(UNSEEN_COUNTY)
    row = _row(str(job["operation_id"]))
    assert row.canonical_destination_id is None, (
        f"期望：落库行的 canonical_destination_id 也是 null；实际：{row.canonical_destination_id!r}"
        "（`:222` 那行把归一化原文写进身份列就是本门的成因）"
    )


# ---------------------------------------------------------------------------------------------
# Policy gate 2 - NO_MATCH is a structural answer with a null identity
# ---------------------------------------------------------------------------------------------
def test_g2_no_match_names_itself_and_keeps_identity_null(seeded) -> None:
    """结构上没人认得这句话：具名 NO_MATCH，身份为空，且不得伪报成一个 pending 身份。"""

    body = _capability(RAW_NO_COUNTY)
    assert body.get("destination_state") == "NO_MATCH", (
        f"期望：destination_state=NO_MATCH（仲裁四态之一）；实际：{body.get('destination_state')!r}，"
        f"现有键 {sorted(body)[:16]}"
    )
    assert body.get("canonical_destination_id") is None
    assert str(body.get("pending_destination_label") or "") == RAW_NO_COUNTY, (
        f"期望：pending_destination_label 逐字符等于用户原文 {RAW_NO_COUNTY!r}；"
        f"实际：{body.get('pending_destination_label')!r}"
    )


# ---------------------------------------------------------------------------------------------
# Policy gate 3 - a source that failed at runtime is not a structural verdict (R1)
# ---------------------------------------------------------------------------------------------
def test_g3_a_broken_source_is_a_runtime_failure_not_a_structural_no_match(seeded, monkeypatch) -> None:
    """取来源这件事本身炸了 ⇒ RUNTIME_FAILURE + retryable，不得洗成「这座城做不了」。"""

    def explode(*_args, **_kwargs):
        raise OperationalError("SELECT", None, Exception("vocabulary source is gone"))

    monkeypatch.setattr(capability_module, "_display_labels", explode)
    body = _capability(RAW_NO_COUNTY)
    assert body.get("destination_state") == "RUNTIME_FAILURE", (
        f"期望：来源读取抛异常时 destination_state=RUNTIME_FAILURE；实际："
        f"{body.get('destination_state')!r}。今天 `_display_labels` 把异常吞成空字典，"
        "于是「我的来源炸了」被报成「这句话没有目的地」——R1 明令禁止的那类 laundering"
    )
    assert body.get("canonical_destination_id") is None
    assert body.get("retryable") is True, (
        f"期望：retryable=true，让调用方知道该重试而不是放弃；实际：{body.get('retryable')!r}"
    )
    assert body.get("capability") != "UNAVAILABLE", (
        f"期望：运行时失败绝不得把结构能力判成 UNAVAILABLE；实际：{body.get('capability')!r}"
    )
    assert str(body.get("reason_code") or "") == capability_module.REASON_RESOLVER_UNAVAILABLE, (
        f"期望：具名到契约已有的运行时原因 {capability_module.REASON_RESOLVER_UNAVAILABLE!r}，"
        f"不新造词；实际：{body.get('reason_code')!r}"
    )
    assert body.get("reason_code") in capability_module.TRANSIENT_REASONS, (
        f"期望：这个原因必须在 transient（可重试）清单里；实际："
        f"{body.get('reason_code')!r} 不在 {capability_module.TRANSIENT_REASONS}"
    )


def test_g3b_a_runtime_failure_ends_the_job_retryable_and_never_structural(
    seeded, monkeypatch
) -> None:
    """作业侧的同一件事：来源炸了 ⇒ FAILED + 具名运行时结局，不是 UNAVAILABLE 那句「这地方没救」。"""

    from app.catalog_hydration_operations import (
        OUTCOME_RESOLVER_RUNTIME_FAILURE,
        submit_operation,
        wait_for_operation,
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("vocabulary source is gone")

    monkeypatch.setattr(capability_module, "_display_labels", explode)
    with SessionLocal() as session:
        row, _reused = submit_operation(session, RAW_NO_COUNTY)
        operation_id = row.id
    landed = wait_for_operation(operation_id, deadline_seconds=20)
    assert landed is not None
    assert landed.status == "FAILED", (
        f"期望：运行时失败停在可重试的 FAILED；实际：{landed.status} / {landed.outcome}"
    )
    assert landed.outcome == OUTCOME_RESOLVER_RUNTIME_FAILURE, landed.outcome
    assert landed.canonical_destination_id is None
    assert landed.pending_destination_label == RAW_NO_COUNTY, (
        f"期望：失败也要把用户原话留在作业行里（重试时不必重打）；实际："
        f"{landed.pending_destination_label!r}"
    )
    poll = client.get(f"{HYDRATIONS}/{operation_id}")
    assert poll.status_code == 200, poll.text
    body = poll.json()
    assert body["destination_state"] == "RUNTIME_FAILURE", body["destination_state"]
    assert body["retryable"] is True, body
    assert body["status"] != "UNAVAILABLE", (
        "期望：运行时失败永远不会被写成结构性 UNAVAILABLE（R1）；实际："
        f"{body['status']} / {body.get('outcome')}"
    )


def test_g3c_the_job_projection_carries_the_same_identity_shape_and_prejudges_nothing(
    seeded,
) -> None:
    """同一件事在作业投影上必须同形；而在途作业不得提前判定目的地。"""

    from app.catalog_hydration_operations import submit_operation

    with SessionLocal() as session:
        staged, _reused = submit_operation(session, UNSEEN_COUNTY, start_worker=False)
        operation_id = staged.id
    inflight = client.get(f"{HYDRATIONS}/{operation_id}").json()
    assert inflight["destination_state"] is None, (
        f"期望：还在排队的作业对目的地不下结论；实际："
        f"{inflight['destination_state']!r} —— 把未定当结论，和凭空给个进度是一类错"
    )
    assert inflight["canonical_destination_id"] is None
    assert inflight["pending_destination_label"] == UNSEEN_COUNTY
    assert inflight["resolver_query_normalized"] == capability_module._normalise(UNSEEN_COUNTY)
    assert str(inflight["destination_pending_key"] or "")
    assert inflight["destination_pending_key"] == f"pend-{operation_id}", (
        f"期望：token 由这一行作业自己的 id 派生（稳定、指向作业、不由查询文本派生）；实际："
        f"{inflight['destination_pending_key']!r} vs 期望 pend-{operation_id}"
    )
    assert (
        client.get(f"{HYDRATIONS}/{operation_id}").json()["destination_pending_key"]
        == inflight["destination_pending_key"]
    ), "期望：同一个作业重复读回同一个 token"
    assert _capability(UNSEEN_COUNTY).get("destination_pending_key") is None, (
        "期望：一次能力查询没有开出任何作业，就不该发一个 correlation token；实际："
        f"{_capability(UNSEEN_COUNTY).get('destination_pending_key')!r}"
    )
    assert inflight["pending_label_fidelity"] == capability_module.PENDING_LABEL_EXACT_RAW
    capability_body = _capability(UNSEEN_COUNTY)
    for key in (
        "canonical_destination_id",
        "pending_destination_label",
        "resolver_query_normalized",
        "pending_label_fidelity",
    ):
        assert capability_body.get(key) == inflight.get(key), (
            f"期望：{key} 在能力回答与作业投影里同值；实际："
            f"{capability_body.get(key)!r} vs {inflight.get(key)!r}"
        )


# ---------------------------------------------------------------------------------------------
# Policy gate 4 - AMBIGUOUS: null identity, candidates in their own field
# ---------------------------------------------------------------------------------------------
def test_g4_an_ambiguous_query_keeps_identity_null_and_lists_candidates_separately(
    seeded, monkeypatch
) -> None:
    """两个编码都能对上这个名字 ⇒ 谁都不许当赢家；候选另有一栏。

    Disclosure: this build's 16-label vocabulary has no collision (measured: 0 duplicate labels),
    so the branch is exercised by widening the vocabulary the resolver reads.  The gate is about
    the branch, not about inventing a city.
    """

    monkeypatch.setitem(
        app_module_names(), "TWINA", "双城"
    )  # two codes, one traveller-facing name
    monkeypatch.setitem(app_module_names(), "TWINB", "双城")
    try:
        body = _capability("双城")
    finally:
        monkeypatch.undo()
    assert body.get("destination_state") == "AMBIGUOUS", (
        f"期望：两种写法撞同一个名字时 destination_state=AMBIGUOUS；实际："
        f"{body.get('destination_state')!r}（canonical={body.get('canonical_destination_id')!r}）"
    )
    assert body.get("canonical_destination_id") is None, (
        "期望：AMBIGUOUS 不猜赢家，canonical_destination_id=null；实际："
        f"{body.get('canonical_destination_id')!r}"
    )
    codes = {item.get("city_code") for item in body.get("candidates") or []}
    assert codes == {"CN-TWINA", "CN-TWINB"}, (
        f"期望：candidates 栏单独带出两个候选编码；实际：{sorted(str(c) for c in codes)}"
    )


def app_module_names() -> dict:
    """The vocabulary the resolver reads (single source: `main._CITY_DISPLAY_NAMES`)."""

    from app import main as product_main

    return product_main._CITY_DISPLAY_NAMES


# ---------------------------------------------------------------------------------------------
# Policy gate 5 - a resolved destination gets a real identity and a named source
# ---------------------------------------------------------------------------------------------
def test_g5_a_resolved_destination_carries_a_real_canonical_identity(seeded) -> None:
    """认得的地方必须给真身份：canonical 非空、来源具名，且不掺任何 pending 材料。"""

    body = _capability("CN-BEIJING")
    assert body.get("destination_state") == "RESOLVED"
    assert body.get("canonical_destination_id") == "CN-BEIJING", (
        f"期望：库里真有行的城市拿到真 canonical；实际：{body.get('canonical_destination_id')!r}"
    )
    assert body.get("destination_resolution") in {"catalog", "vocabulary"}, (
        f"期望：身份来源具名为 catalog/vocabulary；实际：{body.get('destination_resolution')!r}"
    )
    kinds = body.get("provenance", {}).get("source_kinds")
    assert kinds and set(kinds) <= set(capability_module.SOURCE_KINDS), (
        f"期望：provenance.source_kinds 非空且只取冻结枚举；实际：{kinds!r}"
    )
    assert body.get("destination_pending_key") is None, (
        f"期望：已经有真身份时不再 mint pending key；实际：{body.get('destination_pending_key')!r}"
    )


# ---------------------------------------------------------------------------------------------
# Policy gates 6 + 7 - raw label and normalised query are two different columns
# ---------------------------------------------------------------------------------------------
def test_g6_the_stored_label_is_separate_from_the_normalised_resolver_query(seeded) -> None:
    """两栏必须各自成立：屏幕读原文，匹配用归一化，谁也不许冒充谁。"""

    job = _submit(RAW_NO_COUNTY)
    row = _row(str(job["operation_id"]))
    assert str(row.pending_destination_label or "") == RAW_NO_COUNTY, (
        f"期望：pending_destination_label == {RAW_NO_COUNTY!r}（逐字符）；"
        f"实际：{row.pending_destination_label!r}"
    )
    assert str(row.resolver_query_normalized or "") == NORMALISED_NO_COUNTY, (
        f"期望：resolver_query_normalized == {NORMALISED_NO_COUNTY!r}；"
        f"实际：{row.resolver_query_normalized!r}"
    )
    assert row.pending_label_fidelity == capability_module.PENDING_LABEL_EXACT_RAW, (
        f"期望：这条是当场收到的原话 ⇒ fidelity={capability_module.PENDING_LABEL_EXACT_RAW!r}；"
        f"实际：{row.pending_label_fidelity!r}"
    )
    assert row.resolver_query_normalized != row.pending_destination_label, (
        "期望：归一化列与原文列不同值（这条查询正是会掉标点的那种）；实际两列同为 "
        f"{row.pending_destination_label!r} —— 那就是把改写后的文本当证词存"
    )


def test_g7_punctuation_is_not_lost_when_the_input_is_stored(seeded) -> None:
    """`-` 被 `_normalise` 吃掉是设计，被当成「用户就这么写的」是伪证。"""

    body = _capability(RAW_NO_COUNTY)
    assert "-" in str(body.get("pending_destination_label") or ""), (
        f"期望：回答里的 pending_destination_label 保留连字符；实际："
        f"{body.get('pending_destination_label')!r}"
    )
    assert body.get("resolver_query_normalized") == NORMALISED_NO_COUNTY
    assert body.get("pending_label_fidelity") == capability_module.PENDING_LABEL_EXACT_RAW


# ---------------------------------------------------------------------------------------------
# Policy gate 8 - the pending key is correlation material, never a join key
# ---------------------------------------------------------------------------------------------
def test_g8_the_pending_key_cannot_buy_a_place_join(seeded) -> None:
    """把 pending key 种成一行地点的 city_code，目录调查必须仍然是零。

    If a survey ever matched on the correlation token, a made-up string would start feeding the
    planner - the exact hole this order exists to close.  The job is staged without a worker so
    the decoy row is definitely in place before anything measures the catalog.
    """

    from app.catalog_hydration_operations import _run_operation, submit_operation

    with SessionLocal() as session:
        operation, _reused = submit_operation(session, UNSEEN_COUNTY, start_worker=False)
        key = str(operation.destination_pending_key or "")
    assert key, "期望：未解析的请求要有 correlation 材料；实际：destination_pending_key 为空"
    assert key != _row(operation.id).canonical_destination_id
    planted = "place-" + key[:8]
    with SessionLocal() as session:
        session.add(
            Place(
                id=planted,
                provider_key="mock-city",
                external_id=key,
                city_code=key,  # the join column, if any code ever uses the token here
                name="诱饵",
                category="attractions",
                latitude=31.1,
                longitude=121.0,
                opening_hours_json="{}",
                tags_json="[]",
                metadata_json="{}",
                updated_at="2026-01-01T00:00:00+08:00",
            )
        )
        session.commit()
    try:
        # The worker's own stage, run in this thread so the decoy is certainly on the table while
        # the survey happens.  `start_worker=False` above is what makes that order observable
        # rather than a race.
        _run_operation(operation.id)
        body = client.get(f"{HYDRATIONS}/{operation.id}").json()
        recorded = ((body.get("error") or {}).get("details") or {}).get("survey") or {}
        assert int(recorded.get("places_held") or 0) == 0, (
            f"期望：以 pending key 当城市编码种一行地点，作业自己记下的目录调查仍为 0；实际："
            f"{recorded} —— 有代码拿 correlation token 去 join 目录了"
        )
        assert int((body.get("result") or {}).get("survey", {}).get("places_held") or 0) == 0, (
            "期望：完成态的结果里也不得把诱饵算进来；实际："
            f"{(body.get('result') or {}).get('survey')}"
        )
        assert body["canonical_destination_id"] is None, body["canonical_destination_id"]
        users: list[str] = []
        root = Path(capability_module.__file__).resolve().parent
        for source in sorted(root.rglob("*.py")):
            for number, line in enumerate(
                source.read_text(encoding="utf-8").splitlines(), start=1
            ):
                code = line.split("#", 1)[0]
                if "destination_pending_key" in code and re.search(
                    r"\b(Place|RealPlaceCandidate|CityService)\b.*\b(city_code|external_id|"
                    r"provider_key)\b",
                    code,
                ):
                    users.append(f"{source.name}:{number}: {code.strip()}")
        assert not users, (
            "期望：没有任何产品码把 destination_pending_key 放进目录/place 的 join 条件；实际：\n  "
            + "\n  ".join(users)
        )
    finally:
        with SessionLocal() as session:
            session.execute(Place.__table__.delete().where(Place.id == planted))
            session.commit()


# ---------------------------------------------------------------------------------------------
# Policy gate 9 - idempotency without inventing an identity
# ---------------------------------------------------------------------------------------------
def test_g9_repeated_unresolved_requests_share_one_job_and_invent_no_identity(seeded) -> None:
    """同一句问话的两种拼法：同一个作业、同一个指纹，而指纹里没有 canonical、没有原文。

    Reuse is a rule about *open* work: a job that already finished is not resurrected, and the
    second request that opens a fresh one must still prove the same two things - the fingerprint
    converged, and no identity was invented to make it converge.
    """

    from app.catalog_hydration_operations import submit_operation

    with SessionLocal() as session:
        staged, _reused = submit_operation(session, RAW_NO_COUNTY, start_worker=False)
        staged_id, staged_fingerprint = staged.id, staged.fingerprint
    again = _submit(NORMALISED_NO_COUNTY)  # same words, the punctuation dropped by the traveller
    assert again["operation_id"] == staged_id, (
        f"期望：同指纹的作业直接复用，不重开一单；实际 {staged_id} vs {again['operation_id']}"
    )
    assert again.get("reused") is True, again
    assert again["fingerprint"] == staged_fingerprint, (
        f"期望：`zzxqvw-不存在县` 与 `zzxqvw不存在县` 同一个幂等指纹；实际："
        f"{str(staged_fingerprint)[:16]!r} vs {str(again['fingerprint'])[:16]!r}"
    )
    row = _row(staged_id)
    assert row.canonical_destination_id is None
    assert str(row.destination_pending_key or "") not in row.fingerprint, (
        "期望：pending key 不进指纹（它是 correlation 材料，不是身份）；实际指纹里含它"
    )
    assert RAW_NO_COUNTY not in row.fingerprint and NORMALISED_NO_COUNTY not in row.fingerprint, (
        "期望：指纹是 32 位摘要，正文一个字都不该出现在里面；实际："
        f"{row.fingerprint!r}"
    )
    parts = capability_module.fingerprint_parts(
        None, resolver_query_normalized=NORMALISED_NO_COUNTY
    )
    assert parts.get("namespace") == capability_module.PENDING_REQUEST_NAMESPACE
    assert parts.get("canonical_destination_id") is None
    assert RAW_NO_COUNTY not in json.dumps(parts, ensure_ascii=False), (
        f"期望：未解析请求的指纹材料只有归一化文本，没有带标点的原文；实际：{parts}"
    )

    # The honest complement: closing the job must not be hidden behind the same key.
    with SessionLocal() as session:
        session.execute(
            CatalogHydrationOperation.__table__.update()
            .where(CatalogHydrationOperation.id == staged_id)
            .values(status="UNAVAILABLE", outcome="NO_REGISTERED_SOURCE")
        )
        session.commit()
    fresh = _submit(NORMALISED_NO_COUNTY)
    assert fresh["operation_id"] != staged_id, (
        "期望：已结束的作业不会被复用（幂等是「同一件未完成的工作」，不是永久屏蔽）；实际："
        f"复用了 {staged_id}"
    )
    assert fresh["fingerprint"] == staged_fingerprint, (
        "期望：重开的作业仍是同一个请求指纹；实际："
        f"{str(fresh['fingerprint'])[:16]!r} vs {str(staged_fingerprint)[:16]!r}"
    )
    assert _row(str(fresh["operation_id"])).canonical_destination_id is None


# ---------------------------------------------------------------------------------------------
# Policy gate 10 - legacy rows are backfilled in place, never deleted
# ---------------------------------------------------------------------------------------------
def test_g10_legacy_pending_rows_migrate_in_place_without_losing_their_ids(tmp_path: Path) -> None:
    """库里的旧 `pending:` 行不许为了新 schema 好看而被删（R10 / 仲裁 ④）。"""

    database = tmp_path / "legacy_pending.db"
    connection = sqlite3.connect(database)
    connection.execute(LEGACY_DDL)
    rows = [
        # id, canonical (legacy text-derived identity), request_json
        ("op-a", f"pending:{NORMALISED_NO_COUNTY}", json.dumps({"query": RAW_NO_COUNTY})),
        ("op-b", "pending:贡山独龙族怒族自治县", "{}"),  # only the normalised payload survived
        ("op-c", "CN-BEIJING", json.dumps({"query": "北京"})),  # a real identity, must not move
    ]
    fingerprints: dict[str, str] = {}
    for operation_id, canonical, request_json in rows:
        fingerprints[operation_id] = capability_module.capability_fingerprint(canonical)
        connection.execute(
            "INSERT INTO catalog_hydration_operations (id, fingerprint,"
            " canonical_destination_id, catalog_schema_version, readiness_policy_version,"
            " provider_policy_version, status, request_json, result_json, error_json, queued_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                fingerprints[operation_id],
                canonical,
                "catalog-v1",
                "readiness-v1",
                "provider-v1",
                "QUEUED",
                request_json,
                "{}",
                "{}",
                "2026-09-21T10:00:00+08:00",
                "2026-09-21T10:00:00+08:00",
            ),
        )
    connection.commit()
    connection.close()

    target = make_engine(f"sqlite:///{database.as_posix()}")
    ensure_schema(target)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        after = {row["id"]: dict(row) for row in connection.execute(
            "SELECT * FROM catalog_hydration_operations"
        )}
        held = connection.execute(
            "SELECT COUNT(*) FROM catalog_hydration_operations"
        ).fetchone()[0]
        index_names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND tbl_name='catalog_hydration_operations'"
            )
        }
    finally:
        connection.close()

    assert held == len(rows), (
        f"期望：迁移前后各 {len(rows)} 行，一行都不许少；实际迁移后 {held} 行"
    )
    assert set(after) == {row[0] for row in rows}, (
        f"期望：operation id 原样保留（不删不重建）；实际：{sorted(after)}"
    )
    assert after["op-a"]["canonical_destination_id"] is None, (
        f"期望：legacy 合成身份被置 null；实际：{after['op-a']['canonical_destination_id']!r}"
    )
    assert after["op-a"]["pending_destination_label"] == RAW_NO_COUNTY, (
        f"期望：从最佳可用 raw source（request_json.query）回填出**带标点**的原文；实际："
        f"{after['op-a']['pending_destination_label']!r}"
    )
    assert after["op-a"]["pending_label_fidelity"] == capability_module.PENDING_LABEL_EXACT_RAW
    assert after["op-b"]["pending_label_fidelity"] == (
        capability_module.PENDING_LABEL_LEGACY_NORMALIZED
    ), (
        f"期望：只剩归一化 payload 的行必须标 {capability_module.PENDING_LABEL_LEGACY_NORMALIZED!r}"
        f"，不许伪称 exact raw；实际：{after['op-b']['pending_label_fidelity']!r}"
    )
    assert after["op-b"]["canonical_destination_id"] is None
    assert str(after["op-b"]["destination_pending_key"] or ""), (
        "期望：回填 correlation/幂等材料；实际：destination_pending_key 为空"
    )
    assert after["op-a"]["legacy_fingerprint"] == fingerprints["op-a"], (
        "期望：旧指纹作为审计材料留在 legacy_fingerprint；实际："
        f"{after['op-a']['legacy_fingerprint']!r}"
    )
    assert after["op-a"]["fingerprint"] == capability_module.capability_fingerprint(
        None, resolver_query_normalized=NORMALISED_NO_COUNTY
    ), "期望：新指纹按 pending-request-v1 重算，使同一请求仍然复用到这一行"
    assert after["op-c"]["canonical_destination_id"] == "CN-BEIJING", (
        f"期望：真身份一行不动；实际：{after['op-c']['canonical_destination_id']!r}"
    )
    assert after["op-c"]["fingerprint"] == fingerprints["op-c"]
    assert "ux_catalog_hydration_one_open_job" in index_names, (
        f"期望：重建表之后持久唯一索引仍在（否则幂等退回进程本地）；实际索引：{sorted(index_names)}"
    )


# ---------------------------------------------------------------------------------------------
# Policy gate 10b - the migration is replay-safe, because `executescript` commits
# ---------------------------------------------------------------------------------------------
def test_g13_rerunning_the_schema_migration_never_duplicates_or_eats_a_row(tmp_path: Path) -> None:
    """A boot that died halfway must be finishable, not replayed into duplicates.

    The rename and the row copy are two statements separated by `executescript`, which commits -
    so "parked but not yet dropped" is a real state a database can be found in.  A second boot has
    to converge on the same rows, and the rebuilt table must not become a second, slightly
    different definition of the same thing.
    """

    database = tmp_path / "replay.db"
    connection = sqlite3.connect(database)
    connection.execute(LEGACY_DDL)
    connection.execute(
        "INSERT INTO catalog_hydration_operations (id, fingerprint, canonical_destination_id,"
        " catalog_schema_version, readiness_policy_version, provider_policy_version, status,"
        " request_json, result_json, error_json, queued_at, updated_at)"
        " VALUES ('op-replay','fp','pending:云和县','catalog-v1','readiness-v1','provider-v1',"
        "'QUEUED','{\"query\": \"云和县\"}','{}','{}','2026-09-21T10:00:00+08:00',"
        "'2026-09-21T10:00:00+08:00')"
    )
    connection.commit()
    connection.close()

    target = make_engine(f"sqlite:///{database.as_posix()}")
    ensure_schema(target)
    first = sqlite3.connect(database)
    first.row_factory = sqlite3.Row
    try:
        rows_once = [dict(row) for row in first.execute(
            "SELECT * FROM catalog_hydration_operations ORDER BY id"
        )]
        shape_once = list(first.execute("PRAGMA table_info(catalog_hydration_operations)"))
    finally:
        first.close()

    ensure_schema(target)  # a second boot over an already-migrated database
    second = sqlite3.connect(database)
    second.row_factory = sqlite3.Row
    try:
        rows_twice = [dict(row) for row in second.execute(
            "SELECT * FROM catalog_hydration_operations ORDER BY id"
        )]
        shape_twice = list(second.execute("PRAGMA table_info(catalog_hydration_operations)"))
        parked = second.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%pre_identity_fix'"
        ).fetchall()
    finally:
        second.close()

    assert len(rows_twice) == len(rows_once) == 1, (
        f"期望：两次启动后还是那一行，不复制也不丢；实际 {len(rows_once)} -> {len(rows_twice)}"
    )
    assert [row["id"] for row in rows_twice] == [row["id"] for row in rows_once]
    assert rows_twice[0]["canonical_destination_id"] is None
    assert rows_twice[0]["pending_destination_label"] == "云和县", (
        f"期望：重放不会把已回填的原文冲掉；实际：{rows_twice[0]['pending_destination_label']!r}"
    )
    assert [tuple(row) for row in shape_twice] == [tuple(row) for row in shape_once], (
        "期望：第二次启动不再改动表形状（形状只有一份权威定义）"
    )
    assert not parked, (
        f"期望：迁移收尾后不留下 parked 的旧表；实际：{[row[0] for row in parked]}"
    )
    fresh = make_engine(f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}")
    ensure_schema(fresh)
    fresh_connection = sqlite3.connect(tmp_path / "fresh.db")
    try:
        shape_fresh = list(fresh_connection.execute(
            "PRAGMA table_info(catalog_hydration_operations)"
        ))
    finally:
        fresh_connection.close()
    # dflt_value/pk flags can differ only if the two paths built the table from different DDL.
    assert [(row[1], row[2], row[3], row[5]) for row in shape_twice] == [
        (row[1], row[2], row[3], row[5]) for row in shape_fresh
    ], (
        "期望：迁移出来的表和全新库的表列形状逐列一致（名称/类型/NOT NULL/主键）；"
        "实际两者不一致，说明 DDL 有了第二份定义在漂移"
    )


# ---------------------------------------------------------------------------------------------
# Extra door 1 - the blank request must not keep the `pending:(blank)` placeholder either
# ---------------------------------------------------------------------------------------------
def test_g11_a_blank_request_answers_a_null_identity_and_echoes_the_input(seeded) -> None:
    body = _capability("   ")
    assert body.get("canonical_destination_id") is None, (
        f"期望：空问句的 canonical 是 null；实际：{body.get('canonical_destination_id')!r}"
        "（`pending:(blank)` 与 `pending:<原文>` 是同一味药）"
    )
    assert body.get("pending_destination_label") == "   ", (
        f"期望：空白原文照样逐字符存着，屏幕才显示得出现场；实际："
        f"{body.get('pending_destination_label')!r}"
    )
    assert body.get("destination_state") in {"NO_MATCH", "RUNTIME_FAILURE"}


# ---------------------------------------------------------------------------------------------
# Extra door 2 - no writer in the product may mint a text-derived identity any more
# ---------------------------------------------------------------------------------------------
def test_g12_no_product_module_writes_user_text_into_an_identity_column() -> None:
    """扫产品码：`canonical_destination_id=` 的赋值点只许出现 null 或真 city_code。"""

    root = Path(capability_module.__file__).resolve().parent
    offenders: dict[str, list[str]] = {}
    for source in sorted(root.rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            code = line.split("#", 1)[0]
            if "canonical_destination_id" not in code:
                continue
            looks_like_fabrication = (
                "PENDING_PREFIX" in code
                or "LEGACY_PENDING_PREFIX" in code
                or 'f"pending:' in code
                or "f'pending:" in code
                or "f\"{PENDING" in code
            )
            if looks_like_fabrication:
                offenders.setdefault(str(source.relative_to(root)), []).append(
                    f":{number}: {code.strip()}"
                )
    assert not offenders, (
        "期望：backend/app 里没有任何一处把用户原文/前缀写进身份列；实际：\n"
        + "\n".join(f"{name}{hits}" for name, hits in sorted(offenders.items()))
        + "\n（`pending:` 只能作为**读取旧数据**的 legacy 标记存在，绝不能是产品写出去的值）"
    )
