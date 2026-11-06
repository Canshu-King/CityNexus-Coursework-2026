"""ORD-0197 acceptance v2 - four behaviour receipts, one per provider answer, on the normal path.

The arbiter's wording for this order is blunt: *importing lane B's class is not a delivery*.  What is
asked for instead is a row of evidence per outcome class, taken through the product's own entrances on
one exact SHA, because every one of these four classes has already been mislabelled once in this
project's history (`unresolved:<hash>`, a timeout read as "no such place", a candidate list where an
identity belongs).

So each door below answers a question a reader can only accept from a reading:

* MATCH - is there a server-owned canonical, with the provider's own entity reference, a legal WGS84
  point and extent, the traveller's words still in their own column, and the attempt history stored
  with the *job* rather than only in the response that happened to render it?  And when the answer
  carries no usable id or no legal position, does the product refuse (named) instead of minting a
  temporary identity?
* AMBIGUOUS - is the identity null, are the contenders in a separate field, is nobody installed by
  taking element zero, and does a replay change nothing about that?
* NO_MATCH - can the row itself show that this emptiness came from an answer that satisfied the
  transport contract, and not from an HTTP failure or a timeout?
* RUNTIME_FAILURE - is it named, retryable, attributable to a mirror, never collapsed into NO_MATCH,
  and does replaying it produce no new pseudo-canonical?

One extra knife, because it is the easy one to miss: the audit must hold **every** attempt.
`mirror A timed out, mirror B answered "nothing"` may legitimately conclude NO_MATCH - but both
attempts have to be visible afterwards, since final outcome and audit history are different things,
and a job outlives the request that created it.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.anywhere_capability as capability_module
import app.catalog_hydration_operations as hydration_module
from app.db.models import CatalogHydrationOperation
from app.db.session import SessionLocal
from app.main import app
from test_qoder_provider_identity_leg import (
    COUNTY_CANONICAL,
    COUNTY_CENTROID,
    COUNTY_ENTITY_ID,
    COUNTY_EXTENT,
    RAW_COUNTY,
    _answer,
    _capability,
    _enable,
    _feature,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
CAPABILITY = "/api/v1/destinations/capability"
HYDRATIONS = "/api/v1/catalog-hydrations"
OTHER_COUNTY = "云和县"  # same words, different shape: two areas claiming one name


@pytest.fixture()
def seeded(monkeypatch) -> str:
    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, reset.text
    with SessionLocal() as session:
        session.execute(CatalogHydrationOperation.__table__.delete())
        session.commit()
    yield str(reset.json()["traveler_id"])
    with SessionLocal() as session:
        session.execute(CatalogHydrationOperation.__table__.delete())
        session.commit()
    os.environ.pop("CITYNEXUS_ANYWHERE_RESOLVER", None)


def _job(operation_id: str) -> dict:
    with SessionLocal() as session:
        row = session.scalar(
            select(CatalogHydrationOperation).where(
                CatalogHydrationOperation.id == operation_id
            )
        )
    assert row is not None, f"作业 {operation_id} 不在库里"
    payload = {}
    for raw in (row.result_json, row.error_json):
        if raw:
            payload.update(json.loads(raw))
    return {
        "status": row.status,
        "outcome": row.outcome,
        "canonical_destination_id": row.canonical_destination_id,
        "pending_destination_label": row.pending_destination_label,
        "resolver_query_normalized": row.resolver_query_normalized,
        "destination_pending_key": row.destination_pending_key,
        "fingerprint": row.fingerprint,
        "payload": payload,
    }


def _audit(job: dict) -> dict:
    """The stored audit, from either envelope shape.

    A resolved ending writes a flat result payload; an unresolved one writes the standard error
    envelope, whose extras live under `details`.  Reading only the top level would report "the job
    kept no attempts" for a job that kept them one level down - which is a wrong reading, not a
    missing feature, so both shapes are looked at and the door says which one answered.
    """

    payload = job["payload"]
    return {**payload, **(payload.get("details") or {})}


def _replay_semantics(query: str) -> tuple[dict, dict, dict]:
    """Ask the same question twice and separate the two things a replay is allowed to change.

    Reuse of an *open* job is a real promise (two callers tapping the same words must not start two
    workers).  Equality of operation ids across an arbitrary delay is not: by the time the second
    request arrives the first job may have closed honestly, and then a new job is the right answer.
    So the invariant asserted here is the one the product actually owns - one fingerprint, never two
    open jobs for it, no canonical appearing on either trip, and the conclusion unchanged.
    """

    first = client.post(HYDRATIONS, json={"query": query})
    second = client.post(HYDRATIONS, json={"query": query})
    assert first.status_code in (200, 202) and second.status_code in (200, 202)
    a, b = first.json(), second.json()
    assert a["fingerprint"] == b["fingerprint"], (
        f"同一句话两次请求指纹不同：{a['fingerprint']} vs {b['fingerprint']}"
    )
    # `destination_state` is deliberately null on an in-flight job (the projection refuses to
    # prejudge a worker that has not concluded), so the class is asserted by the callers below on the
    # capability read and on the stored outcome - not by reading a field the product keeps empty on
    # purpose.
    assert a["canonical_destination_id"] == b["canonical_destination_id"], (a, b)
    for posted in (a, b):
        assert posted["canonical_destination_id"] is None, (
            f"期望：重放不生成新伪 canonical；实际：{posted['canonical_destination_id']!r}"
        )
    with SessionLocal() as session:
        open_rows = session.scalars(
            select(CatalogHydrationOperation).where(
                CatalogHydrationOperation.fingerprint == a["fingerprint"],
                CatalogHydrationOperation.status.in_(
                    hydration_module.OPEN_STATUSES
                ),
            )
        ).all()
    assert len(open_rows) <= 1, (
        f"期望：同一指纹至多一单开放作业；实际 {len(open_rows)} 单："
        f"{[(row.id[:8], row.status) for row in open_rows]}"
    )
    return a, b, _job(a["operation_id"])


def _wait_closed(operation_id: str) -> dict:
    import time

    for _ in range(200):
        job = _job(operation_id)
        if job["status"] in {"COMPLETED", "FAILED", "UNAVAILABLE", "ABANDONED"}:
            return job
        time.sleep(0.05)
    pytest.fail(f"产品：作业 {operation_id} 没进终态")


def _submit_and_close(query: str) -> tuple[dict, dict]:
    """Accept the request through the product, then read the job it became - not the response."""

    posted = client.post(HYDRATIONS, json={"query": query})
    assert posted.status_code in (200, 202), posted.text
    body = posted.json()
    operation_id = body["operation_id"]
    for _ in range(200):
        job = _job(operation_id)
        if job["status"] in {"COMPLETED", "FAILED", "UNAVAILABLE", "ABANDONED"}:
            return body, job
        import time

        time.sleep(0.05)
    pytest.fail(f"产品：作业 {operation_id} 没进终态，最后 {job['status']} / phase {job}")


def _two_mirrors(monkeypatch, first: httpx.Response | Exception, second: httpx.Response):
    """A seam backed by two geocoder mirrors, so B's rotation and its attempt history are real.

    Only the transports are substituted.  The classification these doors read is lane B's own
    `resolve_from_endpoints` + `PhotonIdentityProvider`, mapped by lane C's answer, on the normal
    product path - not a copy of either made for the test.
    """

    from app.core.config import settings
    from app.integrations.discovery.provider_identity import (
        HttpxTransport,
        PhotonIdentityProvider,
        build_httpx_client,
        resolve_from_endpoints,
    )
    from app.integrations.provider_config import ExternalProviderSettings

    def _respond(payload):
        if isinstance(payload, Exception):
            raise payload
        return payload

    settings_ = ExternalProviderSettings()
    provider_settings = ExternalProviderSettings()

    @contextmanager
    def resolver():
        client_a = build_httpx_client(
            transport=httpx.MockTransport(lambda request: _respond(first))
        )
        client_b = build_httpx_client(
            transport=httpx.MockTransport(lambda request: _respond(second))
        )
        try:
            def ask(query: str):
                return resolve_from_endpoints(
                    query,
                    providers=[
                        (
                            f"{PhotonIdentityProvider.provider_key}-a",
                            HttpxTransport(
                                client=client_a,
                                timeout_s=float(settings.real_city_timeout_seconds),
                            ),
                        ),
                        (
                            f"{PhotonIdentityProvider.provider_key}-b",
                            HttpxTransport(
                                client=client_b,
                                timeout_s=float(settings.real_city_timeout_seconds),
                            ),
                        ),
                    ],
                    base_url=provider_settings.photon_base_url,
                )

            yield ask
        finally:
            client_a.close()
            client_b.close()

    monkeypatch.setattr(capability_module, "_provider_identity_resolver", resolver)
    monkeypatch.setenv("CITYNEXUS_ANYWHERE_RESOLVER", "photon")
    return settings_


def _collection(*features) -> httpx.Response:
    return httpx.Response(
        200,
        text=json.dumps(
            {"type": "FeatureCollection", "features": list(features)}, ensure_ascii=False
        ),
    )


# ---------------------------------------------------------------------------------------------
# r1 · MATCH
# ---------------------------------------------------------------------------------------------
def test_r1_a_matched_answer_becomes_a_chaseable_identity_and_stays_split_in_columns(
    seeded, monkeypatch
) -> None:
    """MATCH 的六件事一次量完，其中"入审计"是读库、不是读回答。"""

    _enable(monkeypatch)
    _answer(
        monkeypatch,
        features=(_feature(osm_id=3289208, osm_type="R", name=RAW_COUNTY, extent=COUNTY_EXTENT),),
    )
    read = _capability(RAW_COUNTY)
    assert read["canonical_destination_id"] == COUNTY_CANONICAL, read["canonical_destination_id"]
    provenance = read["destination_provenance"] or {}
    assert provenance.get("source_provider") == "photon", provenance
    assert provenance.get("source_entity_id") == COUNTY_ENTITY_ID, provenance
    assert provenance.get("coordinate_system") == "WGS84", provenance
    centroid = provenance.get("centroid") or {}
    west, south, east, north = provenance.get("extent") or (None,) * 4
    assert -90 <= centroid.get("latitude") <= 90, centroid
    assert -180 <= centroid.get("longitude") <= 180, centroid
    assert south <= centroid["latitude"] <= north, (centroid, provenance.get("extent"))
    assert west <= centroid["longitude"] <= east, (centroid, provenance.get("extent"))
    assert len(provenance.get("extent") or ()) == 4, provenance
    assert provenance.get("observed_at") and provenance.get("fetched_at"), provenance
    # exact raw 与 normalized 仍是两栏，且都不许冒充身份
    assert read["pending_destination_label"] == RAW_COUNTY, read["pending_destination_label"]
    assert read["destination_pending_key"] is None, read["destination_pending_key"]
    for key, value in read.items():
        if key.endswith("destination_id") or key == "city_code":
            assert value in (None, COUNTY_CANONICAL), f"{key}={value!r} 不是服务端创建的那个身份"
        assert not str(key).startswith("pending") or key in {
            "pending_destination_label",
            "pending_label_fidelity",
        }, f"多了一个冒充身份栏目：{key}"

    _, job = _submit_and_close(RAW_COUNTY)
    assert job["canonical_destination_id"] == COUNTY_CANONICAL, job
    assert job["pending_destination_label"] == RAW_COUNTY, job
    stored = _audit(job)
    attempts = stored.get("provider_attempts") or []
    assert len(attempts) == 1 and attempts[0]["answered"] is True, (
        f"期望：ProviderAttempt[] 进作业自己的审计（不是只在回答里）；实际：{sorted(stored)}"
    )
    assert (stored.get("destination_provenance") or {}).get("source_entity_id") == (
        COUNTY_ENTITY_ID
    ), sorted(stored)


def test_r1b_a_matched_answer_without_a_usable_id_or_position_refuses_instead_of_minting(
    seeded, monkeypatch
) -> None:
    """验收口径点名的反例：缺 source id 或合法 WGS84 ⇒ 具名失败，禁止临时 hash 一个身份。"""

    _enable(monkeypatch)
    feature = _feature(osm_id=3289208, osm_type="R", name=RAW_COUNTY, extent=COUNTY_EXTENT)
    feature_without_position = json.loads(json.dumps(feature))
    feature_without_position["geometry"]["coordinates"] = [300.0, 200.0]
    for label, payload, state in (
        # 坐标越界是 provider 报了 MATCH、由 C 拒收 ⇒ 具名 malformed 失败。
        ("坐标不合法", feature_without_position, "RUNTIME_FAILURE"),
    ):
        _answer(monkeypatch, features=(payload,))
        body = _capability(RAW_COUNTY)
        assert body["canonical_destination_id"] is None, f"{label}：身份必须为空；实际 {body}"
        assert body["destination_state"] == state, f"{label}: {body['destination_state']}"
        assert body["reason_code"] == capability_module.REASON_RESOLVER_UNAVAILABLE, body
        for key, value in body.items():
            if key.endswith("destination_id") or key == "city_code":
                assert value is None, f"{label}：{key} 被临时值占了：{value!r}"
        _, job = _submit_and_close(RAW_COUNTY)
        assert job["canonical_destination_id"] is None, f"{label}：作业里出现了伪身份 {job}"
        assert _audit(job).get("provider_attempts"), (
            f"{label}：具名失败也要留下是谁答的、答成什么样；实际 {sorted(job['payload'])}"
        )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "验收口径 v2 的 MATCH 反例还没全部成立，且成因今晚量清楚了（探针 "
        "`_qoder_ord30_evidence_20260920/ord191/repro_cmdr_malformed.py`：六种形状走同一正常入口，逐条打印 "
        "provider 层交出来的东西）。实测两点：(1) 只缺 `osm_id` 的 feature 在 lane B 的分级层 "
        "`integrations/discovery/provider_identity.py:318-321` 就被丢掉，C 收到的是 "
        "`outcome=NO_MATCH / candidates=0 / reason=PROVIDER_NO_RESULTS`；同一过滤器还按 "
        "`props['type'] not in DESTINATION_GRADE_TYPES` 丢，所以塌成『这个地方不存在』的形状不止一种。"
        "(2) 探针把那两条 dataclass 的字段名整排打出来：ProviderAttempt 只有 "
        "answered/elapsed_ms/endpoint/error/http_status/reason/retry_after_s，ProviderResolution 只有 "
        "answered/attempts/candidates/normalized_query/outcome/provider_replied/query/reason/retryable "
        "——**没有任何一条报『看到几条 feature』**，所以 C 侧无论怎么写都无法区分『答了但证据不合格』与"
        "『答了且确实没有』。差的那一步按名点给 B，口径只要一个字段："
        "`ProviderAttempt.features_viewed`（或等价的 `candidates_before_filter`＋丢弃原因）。"
        "C 侧能守的都已守住（见 g14/r1b：身份 null、原文照旧、不造 hash）。"
        "这条 strict xfail 就是那台探针：B 交出计数后它会以 XPASS 报错，提醒摘标记而不是让这一格被遗忘。"
    ),
)
def test_r1d_a_feature_without_any_usable_id_is_named_malformed_not_absent(
    seeded, monkeypatch
) -> None:
    """缺 source id 的 feature 应当被说成"证据畸形"，而不是"这里没有这个地方"。"""

    _enable(monkeypatch)
    feature = _feature(osm_id=3289208, osm_type="R", name=RAW_COUNTY, extent=COUNTY_EXTENT)
    without_id = json.loads(json.dumps(feature))
    without_id["properties"].pop("osm_id", None)
    bad_type = json.loads(json.dumps(feature))
    bad_type["properties"]["type"] = "not_a_destination_grade_type"
    for label, payload in (("缺 osm_id", without_id), ("type 非目的地级", bad_type)):
        _answer(monkeypatch, features=(payload,))
        body = _capability(RAW_COUNTY)
        assert body["canonical_destination_id"] is None, body
        assert body["destination_state"] == "RUNTIME_FAILURE", (
            f"{label} 今天实测：{body['destination_state']} / {body.get('attribution_reason')}"
        )
        assert body["attribution_reason"] == "PROVIDER_BAD_RESPONSE", (
            f"{label} 今天实测 attribution_reason={body.get('attribution_reason')!r}"
        )
        assert body["reason_code"] != capability_module.REASON_NO_CANONICAL_IDENTITY, (
            f"{label}：不许报成『这个地方不存在』；实际 reason_code={body['reason_code']!r}"
        )
        assert body["recommended_action"] != capability_module.ACTION_CHOOSE_ANOTHER_DESTINATION, (
            f"{label}：动作不许是『换个地方』；实际 action={body['recommended_action']!r}"
        )


# ---------------------------------------------------------------------------------------------
# r2 · AMBIGUOUS
# ---------------------------------------------------------------------------------------------
def test_r2_an_ambiguous_answer_holds_no_identity_and_replay_does_not_pick_one(
    seeded, monkeypatch
) -> None:
    """两条面积同名 ⇒ null + 候选另栏 + 不取第一项 + 重放语义不变。"""

    _enable(monkeypatch)
    _answer(
        monkeypatch,
        features=(
            _feature(
                osm_id=111, osm_type="R", name=RAW_COUNTY, extent=[118.0, 27.0, 119.0, 28.0]
            ),
            _feature(
                osm_id=222, osm_type="R", name=RAW_COUNTY, extent=[120.0, 28.0, 121.0, 29.0]
            ),
        ),
    )
    body = _capability(RAW_COUNTY)
    assert body["destination_state"] == "AMBIGUOUS", body["destination_state"]
    assert body["canonical_destination_id"] is None, body
    candidates = body["candidates"] or []
    assert len(candidates) == 2, candidates
    assert {item["source_entity_id"] for item in candidates} == {"R:111", "R:222"}, candidates
    # "不自动取第一项"要能被证伪：答案里的身份栏必须是空，而不是恰好等于 candidates[0]
    assert body["canonical_destination_id"] != COUNTY_CANONICAL, body
    again = _capability(RAW_COUNTY)
    assert again["canonical_destination_id"] is None, again
    assert [item["source_entity_id"] for item in again["candidates"]] == [
        item["source_entity_id"] for item in candidates
    ], "重放之后候选集合变了：那等于第二次回答换了一个问题"
    # Reuse is only a promise while the first job is still open, and whether it is open is a race
    # with its own worker - which is exactly the timing luck this file set out to stop. So the door
    # schedules it: nothing is dispatched, the job stays open, and reuse becomes a fact to assert
    # rather than a coin to flip.
    real_dispatch = hydration_module.dispatch
    with monkeypatch.context() as stage:
        stage.setattr(hydration_module, "dispatch", lambda operation_id: None)
        a, b, _ = _replay_semantics(RAW_COUNTY)
        assert a["operation_id"] == b["operation_id"], (
            f"期望：作业还开着的时候第二次请求复用同一单；实际 {a['operation_id']} vs "
            f"{b['operation_id']}"
        )
        # A job left open here would keep owning its fingerprint, and the next request for the same
        # words would be handed a worker that never arrives - the exact hazard this file's other
        # doors name. So it is started and closed inside this door, not abandoned to the fixture.
        real_dispatch(a["operation_id"])
    _wait_closed(a["operation_id"])
    job = _job(a["operation_id"])
    stored = _audit(job)
    assert stored.get("candidates"), sorted(stored)
    assert {item["source_entity_id"] for item in stored["candidates"]} == {
        "R:111",
        "R:222",
    }, stored["candidates"]
    assert stored.get("provider_attempts"), sorted(stored)
    assert job["canonical_destination_id"] is None, job


# ---------------------------------------------------------------------------------------------
# r3 · NO_MATCH
# ---------------------------------------------------------------------------------------------
def test_r3_an_empty_answer_proves_it_came_from_a_reply_and_not_from_an_outage(
    seeded, monkeypatch
) -> None:
    """NO_MATCH 必须能自证"有人答了、答的是没有"，而不是被写成的。"""

    _enable(monkeypatch)
    _answer(monkeypatch, features=())
    body = _capability(RAW_COUNTY)
    assert body["destination_state"] == "NO_MATCH", body["destination_state"]
    assert body["canonical_destination_id"] is None, body
    assert body["pending_destination_label"] == RAW_COUNTY, body
    cells = [
        item
        for item in (body.get("provider_status") or [])
        if isinstance(item, dict) and item.get("stage") == "resolving_destination"
    ]
    assert len(cells) == 1 and cells[0]["status"] == "OK", cells
    assert cells[0]["answered"] is True and cells[0]["attempts"][0]["http_status"] == 200, cells
    a, b, job = _replay_semantics(RAW_COUNTY)
    assert job["canonical_destination_id"] is None, job
    assert job["pending_destination_label"] == RAW_COUNTY, job
    assert _audit(job).get("provider_attempts"), sorted(job["payload"])
    assert _audit(job).get("survey") is not None, sorted(job["payload"])


# ---------------------------------------------------------------------------------------------
# r4 · RUNTIME_FAILURE
# ---------------------------------------------------------------------------------------------
def test_r4_a_failed_source_is_named_retryable_and_never_becomes_no_match(
    seeded, monkeypatch
) -> None:
    """超时 ⇒ 具名 + 可重试 + 说得出哪一镜 + 不塌成 NO_MATCH + 重放不生伪身份。"""

    _enable(monkeypatch)
    _answer(monkeypatch, raise_with=httpx.ConnectTimeout("connect timed out"))
    body = _capability(RAW_COUNTY)
    assert body["destination_state"] == "RUNTIME_FAILURE", body["destination_state"]
    assert body["canonical_destination_id"] is None, body
    assert body["retryable"] is True, body
    assert body["capability"] == capability_module.UNKNOWN_RETRYABLE, body["capability"]
    cells = [
        item
        for item in (body.get("provider_status") or [])
        if isinstance(item, dict) and item.get("stage") == "resolving_destination"
    ]
    assert cells and cells[0]["status"] == "UNREACHABLE", cells
    attempt = cells[0]["attempts"][0]
    assert attempt["answered"] is False and attempt["endpoint"], attempt
    assert attempt["reason"], f"期望：说得出哪一类失败；实际：{attempt}"
    a, b, job = _replay_semantics(RAW_COUNTY)
    assert job["outcome"] == hydration_module.OUTCOME_RESOLVER_RUNTIME_FAILURE, job
    assert _audit(job).get("provider_attempts"), sorted(job["payload"])
    assert _audit(job)["provider_attempts"][0]["answered"] is False, _audit(job)


# ---------------------------------------------------------------------------------------------
# r5 · the extra knife: every attempt survives the conclusion
# ---------------------------------------------------------------------------------------------
def test_r5_a_timeout_then_an_empty_answer_concludes_no_match_but_keeps_both_attempts(
    seeded, monkeypatch
) -> None:
    """镜A 超时 + 镜B 语义空 ⇒ 结论可以是 NO_MATCH，但两条审计都必须在。

    This is the shape that makes an outage invisible if only the last attempt is kept: the
    conclusion is produced by the mirror that answered, and the one that failed is exactly the
    information an operator needs afterwards.  Read from the stored job, not from a response.
    """

    _two_mirrors(monkeypatch, httpx.ConnectTimeout("mirror A timed out"), _collection())
    body = _capability(RAW_COUNTY)
    assert body["destination_state"] == "NO_MATCH", body["destination_state"]
    assert body["canonical_destination_id"] is None, body
    cells = [
        item
        for item in (body.get("provider_status") or [])
        if isinstance(item, dict) and item.get("stage") == "resolving_destination"
    ]
    attempts = cells[0]["attempts"]
    assert len(attempts) == 2, f"期望：两条尝试都留痕；实际：{attempts}"
    assert attempts[0]["answered"] is False and attempts[0]["reason"], attempts[0]
    assert attempts[1]["answered"] is True and attempts[1]["http_status"] == 200, attempts[1]
    _, job = _submit_and_close(RAW_COUNTY)
    stored = _audit(job).get("provider_attempts") or []
    assert len(stored) == 2, (
        f"期望：作业自己的审计里也是两条（final outcome ≠ audit history）；实际：{stored}"
    )
    assert [item["answered"] for item in stored] == [False, True], stored
    assert job["outcome"] in {
        hydration_module.OUTCOME_NO_REGISTERED_SOURCE,
        hydration_module.OUTCOME_RESOLVER_RUNTIME_FAILURE,
    }, job
