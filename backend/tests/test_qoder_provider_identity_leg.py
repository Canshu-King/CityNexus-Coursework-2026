"""ORD-0191 ⑤ / ORD-0167 - the destination leg that lets a *real* provider name a place.

Why this file exists: `云和县` is a county that exists, and the only way this build could answer
about it was "structurally unusable" - because the resolver was allowed to read nothing but this
process.  The commander measured that at `8ee64ae` and asked for one function and one call site.
The other half of the truth is that a live leg inside a read endpoint would make the default suite
leave the process (§17C) and would let a developer's shell silently change product answers - so the
leg is **off by default** (R6), and every door here turns it on with a mocked transport and asserts
the transport is the thing that answered.

What each door refuses to allow:

* a fuzzy provider hit becoming an identity (a near match is not testimony);
* a hash or the traveller's text becoming an identity when the provider finds nothing;
* a timeout becoming `UNAVAILABLE` (R1's forbidden laundering, now with a real network in the loop);
* two disagreeing provider answers being collapsed into whichever came first;
* the seam being on in the default configuration, or any network attempt happening while it is off.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.anywhere_capability as capability_module
from app.db.models import CatalogHydrationOperation, Place
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
CAPABILITY = "/api/v1/destinations/capability"
HYDRATIONS = "/api/v1/catalog-hydrations"

RAW_COUNTY = "云和县"
COUNTY_ENTITY_ID = "R:3289208"
COUNTY_CANONICAL = f"OSM:{COUNTY_ENTITY_ID}"
COUNTY_EXTENT = [119.36, 27.95, 119.79, 28.36]
COUNTY_CENTROID = [119.5684002, 28.1185318]


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


def _feature(
    *,
    osm_id: int,
    osm_type: str = "R",  # the letter Photon really returns, measured 2026-09-22 01:4x
    name: str,
    centre=None,
    extent=None,
    country: str = "中国",
    admin_type: str = "district",
) -> dict:
    properties = {
        "osm_id": osm_id,
        "osm_type": osm_type,
        "name": name,
        "country": country,
        "type": admin_type,  # destination-grade, as the live capture reports it
    }
    if extent:
        properties["extent"] = extent
    geometry_centre = centre or [119.5684002, 28.1185318]
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": geometry_centre},
        "properties": properties,
    }


def _featurecollection(*features) -> str:
    return json.dumps({"type": "FeatureCollection", "features": list(features)}, ensure_ascii=False)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        return self.response(request)

    def response(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover - overridden
        raise AssertionError("not wired")


def _install(monkeypatch, recorder: _Recorder) -> None:
    """Hand the provider layer a transport that cannot reach a network.

    Only the transport is substituted: the classification these doors assert on is lane B's real
    `PhotonIdentityProvider`, so a door that passes here passed through the typed outcome too.
    """

    monkeypatch.setattr(
        capability_module,
        "_provider_transport",
        lambda: httpx.MockTransport(recorder.handler),
    )


def _answer(monkeypatch, features=(), *, raise_with: Exception | None = None) -> _Recorder:
    class Recorder(_Recorder):
        def response(self, request: httpx.Request) -> httpx.Response:
            if raise_with is not None:
                raise raise_with
            return httpx.Response(200, text=_featurecollection(*features))

    recorder = Recorder()
    _install(monkeypatch, recorder)
    return recorder


def _enable(monkeypatch, name: str = "photon") -> None:
    monkeypatch.setenv("CITYNEXUS_ANYWHERE_RESOLVER", name)


def _capability(query: str) -> dict:
    read = client.get(CAPABILITY, params={"query": query})
    assert read.status_code == 200, read.text
    return read.json()


# ---------------------------------------------------------------------------------------------
# The reading the commander asked for, before and after
# ---------------------------------------------------------------------------------------------
def test_g1_default_off_still_answers_structurally_and_attempts_no_network(seeded, monkeypatch) -> None:
    """ seam 关着时行为一字不变，且一次网络都不试（默认套件不许出网）。"""

    monkeypatch.delenv("CITYNEXUS_ANYWHERE_RESOLVER", raising=False)
    recorder = _answer(monkeypatch, features=(_feature(osm_id=3289208, osm_type="R", name=RAW_COUNTY),))
    assert capability_module.anywhere_resolver_name() == "off"
    body = _capability(RAW_COUNTY)
    assert body["capability"] == "UNAVAILABLE", body["capability"]
    assert body["reason_code"] == capability_module.REASON_NO_CANONICAL_IDENTITY
    assert body["recommended_action"] == capability_module.ACTION_CHOOSE_ANOTHER_DESTINATION
    assert body["canonical_destination_id"] is None
    assert body["pending_destination_label"] == RAW_COUNTY
    assert "provider_status" not in body, (
        f"期望：没查过就没有观测（R2 的缺席即诚实）；实际：{body.get('provider_status')}"
    )
    assert recorder.calls == [], f"期望 seam 关着时一次外呼都不发；实际：{recorder.calls}"


def test_g2_a_provider_named_county_becomes_hydratable_not_dead(seeded, monkeypatch) -> None:
    """ORD-0191 ⑤ 的那一格：provider 认得 ⇒ 真 canonical + HYDRATABLE + 可准备，不再判死。"""

    _enable(monkeypatch)
    recorder = _answer(
        monkeypatch,
        features=(
            _feature(
                osm_id=3289208,
                osm_type="R",
                name=RAW_COUNTY,
                extent=COUNTY_EXTENT,
                centre=COUNTY_CENTROID,
            ),
        ),
    )
    body = _capability(RAW_COUNTY)
    assert body["canonical_destination_id"] == COUNTY_CANONICAL, (
        f"期望：provider 给的实体成为服务端持有的 canonical（{COUNTY_CANONICAL}）；实际："
        f"{body['canonical_destination_id']!r}"
    )
    assert body["destination_state"] == "RESOLVED", body["destination_state"]
    assert body["capability"] == "HYDRATABLE", (
        f"期望：地名成立但本地目录不足 ⇒ HYDRATABLE；实际：{body['capability']}"
    )
    assert body["recommended_action"] == capability_module.ACTION_START_HYDRATION, body[
        "recommended_action"
    ]
    assert body["pending_destination_label"] == RAW_COUNTY, (
        "期望：即使解析成功，原话仍逐字保留（审计半边归 0167 ②）；实际："
        f"{body['pending_destination_label']!r}"
    )
    assert recorder.calls and "q=" in recorder.calls[0], recorder.calls
    provenance = body.get("destination_provenance") or {}
    assert provenance.get("source_provider") == "photon", provenance
    assert provenance.get("source_entity_id") == COUNTY_ENTITY_ID, provenance
    assert provenance.get("coordinate_system") == "WGS84", provenance
    assert provenance.get("centroid") == {"latitude": COUNTY_CENTROID[1], "longitude": COUNTY_CENTROID[0]}, (
        provenance
    )
    assert provenance.get("extent") == COUNTY_EXTENT, (
        f"期望：provider 给的 extent 原样带上（发现范围不许退回 5 km 圆）；实际：{provenance}"
    )
    status = (body.get("provider_status") or [{}])[0]
    assert status.get("status") == "OK", body.get("provider_status")
    # The attempt itself has to travel with the answer, not just a verdict: `elapsed_ms` is only
    # asserted as present (a mocked transport is instant), while the live receipt reports the real
    # number.  What must not come back is B's `resolve_from_endpoints` being bypassed, which
    # measured as every attempt recording 0.0 ms on a call that took 1.8 s.
    attempts = provenance.get("provider_attempts") or []
    assert len(attempts) == 1, f"期望：逐尝试留痕带上来；实际 {attempts!r}"
    assert attempts[0]["answered"] is True and attempts[0]["http_status"] == 200, attempts[0]
    assert isinstance(attempts[0]["elapsed_ms"], (int, float)), attempts[0]
    assert "photon" in str(attempts[0]["endpoint"]), attempts[0]["endpoint"]


# ---------------------------------------------------------------------------------------------
# The four classes, kept apart
# ---------------------------------------------------------------------------------------------
def test_g3_healthy_provider_with_no_answer_is_unavailable_and_invents_nothing(
    seeded, monkeypatch
) -> None:
    """provider 好好回答了但没这个东西 ⇒ UNAVAILABLE + canonical=null，且不落任何文本/摘要身份。"""

    _enable(monkeypatch)
    _answer(monkeypatch, features=())
    body = _capability("不存在的县xzq")
    assert body["capability"] == "UNAVAILABLE", body["capability"]
    assert body["canonical_destination_id"] is None, body["canonical_destination_id"]
    assert body["pending_destination_label"] == "不存在的县xzq"
    status = (body.get("provider_status") or [{}])[0]
    assert status.get("status") == "OK", (
        f"期望：200 + 空集合 = 一次成功的观测，不是失败；实际：{body.get('provider_status')}"
    )
    assert body["destination_state"] == "NO_MATCH", body["destination_state"]
    for key, value in body.items():
        if key in {"query", "pending_destination_label", "resolver_query_normalized"}:
            continue
        assert "xzq" not in str(value), f"字段 {key} 带着原文却不属于那三栏：{value!r}"


def test_g4_a_timeout_stays_a_named_runtime_failure_and_never_structural(seeded, monkeypatch) -> None:
    """硬负例（仲裁原列）：超时/传输失败不得变成 NO_MATCH/UNAVAILABLE，也不得写 hash。"""

    _enable(monkeypatch)
    _answer(monkeypatch, raise_with=httpx.ConnectTimeout("connect timed out"))
    body = _capability(RAW_COUNTY)
    assert body["destination_state"] == "RUNTIME_FAILURE", body["destination_state"]
    assert body["capability"] == capability_module.UNKNOWN_RETRYABLE, (
        f"期望：连不上是『问了没人答』（仲裁『选项一』新格），既不是结构结论 UNAVAILABLE，"
        f"也不是邀请准备的 HYDRATABLE；实际：{body['capability']}"
    )
    assert body["recommended_action"] == capability_module.ACTION_RETRY_LATER, (
        f"期望：动作指向稍后重试，而不是开始准备；实际：{body['recommended_action']}"
    )
    assert body["reason_code"] == capability_module.REASON_RESOLVER_UNAVAILABLE, body["reason_code"]
    assert body["reason_code"] in capability_module.TRANSIENT_REASONS
    assert body["retryable"] is True, body["retryable"]
    assert body["canonical_destination_id"] is None
    status = (body.get("provider_status") or [{}])[0]
    assert status.get("status") == "UNREACHABLE", body.get("provider_status")
    assert body["pending_destination_label"] == RAW_COUNTY, (
        "期望：失败也要原样留着用户那句话，重试不用重打；实际："
        f"{body['pending_destination_label']!r}"
    )


def test_g5_two_disagreeing_answers_must_not_pick_one(seeded, monkeypatch) -> None:
    """两个**都带边界**的同名实体 ⇒ 不许替用户选（面 vs 点的收敛另有一条门，别混过来）。"""

    _enable(monkeypatch)
    _answer(
        monkeypatch,
        features=(
            _feature(
                osm_id=3289208, osm_type="R", name=RAW_COUNTY, country="中国",
                extent=COUNTY_EXTENT,
            ),
            _feature(
                osm_id=999, osm_type="R", name=RAW_COUNTY, country="蒙古",
                extent=[100.1, 46.1, 100.9, 47.2],
            ),
        ),
    )
    body = _capability(RAW_COUNTY)
    assert body["destination_state"] == "AMBIGUOUS", body["destination_state"]
    assert body["canonical_destination_id"] is None
    ids = {item.get("source_entity_id") for item in body.get("candidates") or []}
    assert ids == {COUNTY_ENTITY_ID, "R:999"}, body.get("candidates")


def test_g6_a_fuzzy_near_match_is_not_an_identity(seeded, monkeypatch) -> None:
    """Photon 会容错：回来的名字不是用户问的那个 ⇒ 这不是解析成功，是"没这个东西"。"""

    _enable(monkeypatch)
    _answer(
        monkeypatch,
        features=(_feature(osm_id=1234567, osm_type="W", name="遂昌县", country="中国", admin_type="district"),),
    )
    body = _capability("云合")
    assert body["destination_state"] == "NO_MATCH", body["destination_state"]
    assert body["canonical_destination_id"] is None, (
        f"期望：近似命中不得冒充身份；实际：{body['canonical_destination_id']!r}"
    )
    assert body["capability"] == "UNAVAILABLE", body["capability"]


def test_g10_one_place_two_index_entries_collapses_under_a_named_version(seeded, monkeypatch) -> None:
    """实测形状（02:2x 打真 provider 拿到的读数）：同名同地有两条记录——面（带 extent 的 relation）
    与点（裸 node）。这是同一座城的两种表达，不是两座城市，所以不得反过来要求用户裁决；
    但收敛必须由**具名版本化策略**做，并把它放弃的那条**记下来**。"""

    _enable(monkeypatch)
    _answer(
        monkeypatch,
        features=(
            _feature(
                osm_id=3289208, osm_type="R", name=RAW_COUNTY, extent=COUNTY_EXTENT, centre=COUNTY_CENTROID
            ),
            _feature(
                osm_id=244082768, osm_type="N", name=RAW_COUNTY, admin_type="county",
                centre=[119.5779441, 28.1096051],
            ),
        ),
    )
    body = _capability(RAW_COUNTY)
    assert body["destination_state"] == "RESOLVED", body["destination_state"]
    assert body["canonical_destination_id"] == COUNTY_CANONICAL, (
        f"期望：面（relation + extent）是目的地身份；实际：{body['canonical_destination_id']!r}"
    )
    provenance = body["destination_provenance"]
    assert provenance["discriminated_by"] == (
        capability_module.DESTINATION_DISCRIMINATION_POLICY_VERSION
    ), provenance
    collapsed = {item["source_entity_id"] for item in provenance["collapsed_candidates"]}
    assert collapsed == {"N:244082768"}, (
        f"期望：被放弃的那条要**记名**，不是静默丢；实际：{provenance['collapsed_candidates']}"
    )
    assert provenance["extent"] == COUNTY_EXTENT and len(provenance["extent"]) == 4


def test_g11_a_broken_seam_is_a_runtime_failure_too_not_a_verdict_about_the_world(
    seeded, monkeypatch
) -> None:
    """provider 没答错，是**我们自己这条腿**起不来（配置/构造炸了）。

    This door exists because a cross-isolation run found nothing covered it: g4 exercises a provider
    that answered badly, and while that door is green the seam-broken branch could launder a local
    failure into "no such place" with no door reddening.  Two runtime sites, two doors.
    """

    def explode(*_args, **_kwargs):
        raise RuntimeError("resolver seam cannot be built: bad provider configuration")

    _enable(monkeypatch)
    monkeypatch.setattr(capability_module, "_provider_identity_resolver", explode)
    body = _capability(RAW_COUNTY)
    assert body["destination_state"] == "RUNTIME_FAILURE", body["destination_state"]
    assert body["capability"] != "UNAVAILABLE", (
        f"期望：我们自己的腿起不来，不得被写成'这个地方不存在'；实际：{body['capability']}"
    )
    assert body["canonical_destination_id"] is None
    assert body["retryable"] is True, body["retryable"]
    assert body["reason_code"] == capability_module.REASON_RESOLVER_UNAVAILABLE, body["reason_code"]
    assert body["pending_destination_label"] == RAW_COUNTY, (
        f"期望：原话照旧留着，重试不必重打；实际：{body['pending_destination_label']!r}"
    )
    status = (body.get("provider_status") or [{}])[0]
    assert status.get("status") == "UNREACHABLE", body.get("provider_status")


def test_g12_the_provider_cause_is_not_flattened_into_the_class_name(seeded, monkeypatch) -> None:
    """reason 词汇不另造：B 给了几个形状，我这层就传几个具名原因。

    `reason_code` 保持冻结的那个**类**（`DESTINATION_RESOLVER_UNAVAILABLE`），因为 capability 轴不许扩；
    但底下"哪一种失败"必须原样上来，否则 429（该退避）、超时（该重试）、结构非法（该报数据坏）
    在屏幕上就只有一句话可说。B 自己已有六个码，这里只做搬运，不做二次归类。
    """

    import app.integrations.discovery.provider_identity as provider_layer

    request = httpx.Request("GET", "https://photon.test/api/")

    def answer(response=None, error=None):
        class Recorder(_Recorder):
            def response(self, req):
                if error is not None:
                    raise error
                return response

        recorder = Recorder()
        _install(monkeypatch, recorder)
        _enable(monkeypatch)
        return _capability(RAW_COUNTY)

    timeout = answer(error=httpx.ReadTimeout("timed out", request=request))
    refused = answer(response=httpx.Response(429, request=request, text="rate limited"))
    server = answer(response=httpx.Response(502, request=request, text="bad gateway"))

    for label, body, expected in (
        ("timeout", timeout, provider_layer.REASON_TIMEOUT),
        ("429", refused, provider_layer.REASON_RATE_LIMITED),
        ("5xx", server, provider_layer.REASON_SERVER_ERROR),
    ):
        assert body["reason_code"] == capability_module.REASON_RESOLVER_UNAVAILABLE, (
            f"{label}：期望 capability 轴仍用冻结的类原因；实际 {body['reason_code']!r}"
        )
        assert body.get("attribution_reason") == expected, (
            f"{label}：期望具名原因原样传上来（{expected!r}）；"
            f"实际 {body.get('attribution_reason')!r} —— 把形状塌成一个类名就是 F 那条六形状门在罚的事"
        )
        assert "Error" not in str(body.get("attribution_reason")) , (
            f"{label}：具名原因里不得出现客户端类名（它会随 HTTP 库变）；实际 {body['attribution_reason']!r}"
        )

    distinct = {
        body.get("attribution_reason") for body in (timeout, refused, server)
    }
    assert len(distinct) == 3, f"期望三种形状三个不同原因；实际 {sorted(map(str, distinct))}"
    assert {body["destination_state"] for body in (timeout, refused, server)} == {"RUNTIME_FAILURE"}
    assert all(body["retryable"] is True for body in (timeout, refused, server))


# ---------------------------------------------------------------------------------------------
# The identity has to survive into the job, not just the answer
# ---------------------------------------------------------------------------------------------
def test_g7_a_resolved_identity_reaches_the_stored_operation_without_any_pending_token(
    seeded, monkeypatch
) -> None:
    """⑤ 的后半：服务端持有的身份要落进作业行，且 `pending:` 一个都不留。"""

    _enable(monkeypatch)
    _answer(
        monkeypatch,
        features=(_feature(osm_id=3289208, osm_type="R", name=RAW_COUNTY, extent=COUNTY_EXTENT),),
    )
    posted = client.post(HYDRATIONS, json={"query": RAW_COUNTY})
    assert posted.status_code in (200, 202), posted.text
    body = posted.json()
    assert body["canonical_destination_id"] == COUNTY_CANONICAL, body["canonical_destination_id"]
    with SessionLocal() as session:
        row = session.scalar(
            select(CatalogHydrationOperation).where(
                CatalogHydrationOperation.id == body["operation_id"]
            )
        )
    assert row is not None
    assert row.canonical_destination_id == COUNTY_CANONICAL
    assert row.pending_destination_label == RAW_COUNTY
    assert row.destination_pending_key is None, (
        f"期望：已经有真身份的作业不发 correlation token；实际：{row.destination_pending_key!r}"
    )
    assert not str(row.canonical_destination_id or "").startswith(
        capability_module.LEGACY_PENDING_PREFIX
    )
    assert not capability_module._normalise(RAW_COUNTY) in str(row.canonical_destination_id), (
        f"期望：身份里没有查询文本；实际：{row.canonical_destination_id!r}"
    )
    read_back = client.get(f"{HYDRATIONS}/{body['operation_id']}").json()
    assert read_back["canonical_destination_id"] == COUNTY_CANONICAL, read_back


def test_g8_the_seam_is_read_per_call_and_defaults_off(monkeypatch) -> None:
    """默认关，且按调用读：一次导入期的常量会把整条套件带到网上。"""

    monkeypatch.delenv("CITYNEXUS_ANYWHERE_RESOLVER", raising=False)
    assert capability_module.anywhere_resolver_name() == "off"
    monkeypatch.setenv("CITYNEXUS_ANYWHERE_RESOLVER", "PHOTON ")
    assert capability_module.anywhere_resolver_name() == "photon", "大小写/空白要折，但不许改语义"
    monkeypatch.setenv("CITYNEXUS_ANYWHERE_RESOLVER", "photonnn")
    assert capability_module.anywhere_resolver_name() == "photonnn"
    assert capability_module.anywhere_resolver_attempted() is False, (
        "拼错的 seam 名不得被当成'开了'"
    )
    assert "photon" in capability_module.RESOLVER_SEAMS, capability_module.RESOLVER_SEAMS
    assert "off" in capability_module.RESOLVER_SEAMS


def test_g9_provider_leg_never_writes_a_digest_or_a_placeholder(seeded, monkeypatch) -> None:
    """把原文哈希成身份是今晚第一次被拒的做法；provider 腿也不许换个马甲再来一次。"""

    import re

    _enable(monkeypatch)
    _answer(monkeypatch, raise_with=httpx.ReadTimeout("read timed out"))
    body = _capability(RAW_COUNTY)
    identity = str(body["canonical_destination_id"] or "")
    assert identity == "", f"期望身份为空；实际：{identity!r}"
    assert not re.search(r"[0-9a-f]{8,}", identity), identity
    for key in ("pending_destination_label", "resolver_query_normalized"):
        assert re.search(r"[0-9a-f]{16,}", str(body.get(key) or "")) is None, (
            f"{key} 里出现了摘要：{body.get(key)!r}"
        )
    identity_fields = {
        key: value
        for key, value in body.items()
        if key.endswith("destination_id") or key in {"canonical_destination_id", "city_code"}
    }
    assert set(identity_fields.values()) <= {None}, (
        f"期望：传输失败时任何身份字段都是空的；实际：{identity_fields}"
    )
    assert body["retryable"] is True, body["retryable"]


# ---------------------------------------------------------------------------------------------
# ORD-0191 尾巴 C / D + R14 §8：畸形证据、成功格的逐尝试、四字段同进同出
#
# Measured before these doors existed, on the same mocked-seam machinery (`1cce4bf`, one-off DB):
#   osm_type 缺失   -> state=RESOLVED  canonical="OSM:unknown:3289208"
#   osm_type="Z"    -> state=RESOLVED  canonical="OSM:Z:3289208"
#   质心越界         -> state=RESOLVED  canonical 照收
#   RESOLVED 的 provider_status = [{"status":"OK","stage":"resolving_destination"}]  # 没有逐尝试
#   §8 四字段里只有 attribution_reason 存在
# 三条都是"证据看着齐、其实没发生"，所以断言只认产品自己说出的话。
# ---------------------------------------------------------------------------------------------
def _raw_feature(properties: dict, *, coordinates=None) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": list(coordinates or [COUNTY_CENTROID[1], COUNTY_CENTROID[0]]),
        },
        "properties": dict(properties),
    }


def _county_props(**changes) -> dict:
    props = {
        "osm_id": 3289208,
        "osm_type": "R",
        "name": RAW_COUNTY,
        "country": "中国",
        "type": "district",
        "extent": COUNTY_EXTENT,
    }
    for key, value in changes.items():
        if value is None:
            props.pop(key, None)
        else:
            props[key] = value
    return props


def test_g13_a_successful_answer_carries_the_same_attempt_evidence_a_failure_does(
    seeded, monkeypatch
) -> None:
    """包工头从 `provider_status` 读出 n_attempts=0 是对的：attempts 当时只在 provenance 里。"""

    _enable(monkeypatch)
    _answer(
        monkeypatch,
        features=(_feature(osm_id=3289208, osm_type="R", name=RAW_COUNTY, extent=COUNTY_EXTENT),),
    )
    body = _capability(RAW_COUNTY)
    assert body["canonical_destination_id"] == COUNTY_CANONICAL, body["canonical_destination_id"]
    destination_cells = [
        item
        for item in (body.get("provider_status") or [])
        if isinstance(item, dict) and item.get("stage") == "resolving_destination"
    ]
    assert len(destination_cells) == 1, (
        f"期望：这一问的观察在 provider_status 里有且只有一格；实际：{body.get('provider_status')}"
    )
    cell = destination_cells[0]
    assert cell["status"] == "OK" and cell["answered"] is True, cell
    attempts = cell.get("attempts") or []
    assert len(attempts) == 1, f"期望：成功也要带逐尝试（和失败路径一样多）；实际：{cell}"
    attempt = attempts[0]
    assert attempt["answered"] is True and attempt["http_status"] == 200, attempt
    assert isinstance(attempt["elapsed_ms"], (int, float)), attempt
    assert "photon" in str(attempt["endpoint"]), attempt
    # 同一件事不能两处读数不一致：provenance 里那份必须是同一批尝试。
    provenance_attempts = (body.get("destination_provenance") or {}).get("provider_attempts") or []
    assert provenance_attempts == attempts, (
        f"期望：provider_status 与 provenance 是同一批尝试；实际：{provenance_attempts} vs {attempts}"
    )


def test_g14_a_reference_the_provider_cannot_be_held_to_never_becomes_identity(
    seeded, monkeypatch
) -> None:
    """答了、也报了 MATCH，但它给不出可引用的实体号 ⇒ 这是坏证据，不是"这里有这个地方"。"""

    _enable(monkeypatch)
    cases = (
        ("缺 osm_type", _county_props(osm_type=None)),
        ("osm_type 不是 OSM 的元素类", _county_props(osm_type="Z")),
        ("osm_type 写成小写", _county_props(osm_type="r")),
    )
    for label, props in cases:
        _answer(monkeypatch, features=(_raw_feature(props),))
        body = _capability(RAW_COUNTY)
        assert body["destination_state"] == "RUNTIME_FAILURE", (
            f"{label}：期望具名的运行时失败；实际 {body['destination_state']} / "
            f"{body['destination_resolution']}"
        )
        assert body["canonical_destination_id"] is None, f"{label}：身份必须为空；实际 {body}"
        assert body["retryable"] is True, (
            f"{label}：坏证据要可重试，不是『换个地方』；实际 {body['retryable']}"
        )
        # 指挥使点名的两条判据，按"不得等于"写：写成等于新值的话，哪天漂到另一个错词还是绿的。
        assert body["reason_code"] != capability_module.REASON_NO_CANONICAL_IDENTITY, (
            f"{label}：上游答了、只是证据不合格，却报成『没有这个身份/这个地方不存在』；"
            f"实际 reason_code={body['reason_code']!r}"
        )
        assert body["recommended_action"] != capability_module.ACTION_CHOOSE_ANOTHER_DESTINATION, (
            f"{label}：动作不许是『换个地方』（那是结构性无身份的动作）；"
            f"实际 action={body['recommended_action']!r}"
        )
        assert body["capability"] == capability_module.UNKNOWN_RETRYABLE, (
            f"{label}：证据不合格属于『没答上、可重试』那一格；实际 {body['capability']}"
        )
        assert body["destination_resolution"] == "provider_evidence_malformed", body
        assert body["attribution_reason"] == "PROVIDER_BAD_RESPONSE", (
            f"{label}：期望复用 provider 层已有的名字；实际 {body.get('attribution_reason')!r}"
        )
        refused = body.get("malformed_candidates") or []
        assert len(refused) == 1, f"{label}：被弃用的证据必须留痕；实际 {refused}"
        assert str(refused[0]["source_entity_id"]).startswith(("unknown:", "Z:", "r:")), refused[0]
        # 不许拿摘要/占位当身份：这一句是今晚第一次被拒的做法的延续。
        for key, value in body.items():
            if key.endswith("destination_id") or key == "city_code":
                assert value is None, f"{label}：{key} 被填了 {value!r}"


def test_g15_an_impossible_position_is_refused_not_rounded(seeded, monkeypatch) -> None:
    """经纬度超出 WGS84 值域的证据不能被收成身份；四舍五入就是造新事实。"""

    _enable(monkeypatch)
    _answer(monkeypatch, features=(_raw_feature(_county_props(), coordinates=[200.0, 120.0]),))
    body = _capability(RAW_COUNTY)
    assert body["destination_state"] == "RUNTIME_FAILURE", body["destination_state"]
    assert body["canonical_destination_id"] is None, body
    assert body["destination_resolution"] == "provider_evidence_malformed", body
    refused = (body.get("malformed_candidates") or [{}])[0]
    assert refused["coordinate_system"] == "WGS84", refused
    assert refused["centroid"] == {"latitude": 120.0, "longitude": 200.0}, (
        f"期望：被弃用的坐标原样留痕（不修、不圆）；实际：{refused['centroid']}"
    )


def test_g16_the_four_attribution_fields_arrive_together_or_not_at_all(
    seeded, monkeypatch
) -> None:
    """§8 要的是一个分类给四个消费者共读，不是四个各自可以缺的字段。"""

    fields = ("attribution_reason", "evidence_kind", "http_status", "source_layer")
    _enable(monkeypatch)

    # 答了但证据不合格：说过话的那一层是 HTTP
    _answer(monkeypatch, features=(_raw_feature(_county_props(osm_type="Z")),))
    answered = _capability(RAW_COUNTY)
    missing = [key for key in fields if key not in answered]
    assert not missing, f"期望：有具名原因时四字段齐到；缺 {missing}；实际键名 {sorted(answered)}"
    assert answered["evidence_kind"] == "HTTP_RESPONSE", answered["evidence_kind"]
    assert answered["http_status"] == 200, answered["http_status"]
    assert answered["source_layer"] == "RUNTIME", answered["source_layer"]

    # 没人答（传输层）：http_status 必须是 null，不许是 0
    _answer(monkeypatch, raise_with=httpx.ConnectError("connection refused"))
    transport = _capability(RAW_COUNTY)
    missing = [key for key in fields if key not in transport]
    assert not missing, f"期望：传输失败也要四字段齐；缺 {missing}"
    assert transport["evidence_kind"] == "TRANSPORT_EXCEPTION", transport["evidence_kind"]
    assert transport["http_status"] is None, (
        f"期望：没收到状态码就是没有，不许写 0 冒充；实际：{transport['http_status']!r}"
    )
    assert transport["source_layer"] == "TRANSPORT", transport["source_layer"]

    # 答了并且如实说"这里没有"：归因说的是世界，不是这次交换
    _answer(monkeypatch, features=())
    empty = _capability(RAW_COUNTY)
    assert empty["evidence_kind"] == "HTTP_RESPONSE", empty["evidence_kind"]
    assert empty["source_layer"] == "RESOURCE_STATE", empty["source_layer"]
    assert empty["attribution_reason"] == "PROVIDER_NO_RESULTS", empty["attribution_reason"]

    # 成功没有原因可归：四个字段必须一起缺席，不许留半套
    _answer(
        monkeypatch,
        features=(_feature(osm_id=3289208, osm_type="R", name=RAW_COUNTY, extent=COUNTY_EXTENT),),
    )
    ok = _capability(RAW_COUNTY)
    present = [key for key in fields if key in ok]
    assert present == [], f"期望：可归因的事没发生就不要摆出归因；实际出现了 {present}"


def test_g17_the_new_cell_keeps_the_pair_that_stops_it_being_a_vague_fourth_string(
    seeded, monkeypatch
) -> None:
    """仲裁"选项一"要的是"这一格与 retryable 联动"，不是多一个看起来像状态的词。

    所以这条门同时钉两面：① 凡是"问了、没人答得出"的三种成因（传输超时 / 我们自己那条腿起不来 /
    答了但证据不合格）都必须落 `UNKNOWN_RETRYABLE` + `retryable=true` + 动作 `RETRY_LATER`，并且
    `provider_status` 必须在场（不在场就等于声称"看过"而没看过）；② **压根没去问**的时候
    （seam=off）绝不能出现这一格 —— 那仍是结构答案 `UNAVAILABLE` + 缺席的观察，也就是本文件一直
    拒绝 UNKNOWN 的那半条理由。
    """

    assert capability_module.CAPABILITY_STATES == (
        "READY",
        "HYDRATABLE",
        "UNKNOWN_RETRYABLE",
        "UNAVAILABLE",
    ), capability_module.CAPABILITY_STATES

    sites = {
        "传输超时": lambda stage: _answer(stage, raise_with=httpx.ConnectTimeout("timed out")),
        "我们自己的腿起不来": _break_the_seam,
        "答了但证据不合格": lambda stage: _answer(
            stage, features=(_raw_feature(_county_props(osm_type="Z")),)
        ),
    }
    # Each cause is armed inside its own patch context: leaving the exploding seam in place would
    # make every later check in this door measure the wrong thing (it did, on the first run).
    for label, arm in sites.items():
        with monkeypatch.context() as stage:
            _enable(stage)
            arm(stage)
            body = _capability(RAW_COUNTY)
        assert body["destination_state"] == "RUNTIME_FAILURE", f"{label}: {body['destination_state']}"
        assert body["capability"] == capability_module.UNKNOWN_RETRYABLE, (
            f"{label}：期望落新格；实际 {body['capability']} / {body['destination_resolution']}"
        )
        assert body["retryable"] is True, f"{label}：新格必须与 retryable 联动；实际 {body['retryable']}"
        assert body["recommended_action"] == capability_module.ACTION_RETRY_LATER, (
            f"{label}：动作不许再邀请准备；实际 {body['recommended_action']}"
        )
        assert body.get("provider_status"), f"{label}：说了没人答，却没留下看过的痕迹"
        assert body["canonical_destination_id"] is None, f"{label}：新格不许带身份；实际 {body}"

    # 反过来：没去问就不许冒充"问了没人答"。这一半是老的"不许有 UNKNOWN"还在守的东西。
    monkeypatch.delenv("CITYNEXUS_ANYWHERE_RESOLVER", raising=False)
    never_asked = _capability("zzxqvw-不存在的地方")
    assert never_asked["capability"] == "UNAVAILABLE", never_asked["capability"]
    assert never_asked["destination_state"] == "NO_MATCH", never_asked["destination_state"]
    assert never_asked["retryable"] is False, never_asked["retryable"]
    assert not never_asked.get("provider_status"), (
        f"期望：没看过就没有观察；实际：{never_asked.get('provider_status')}"
    )

    # 答了并且说了话的两格，也不许滑进新格。（上面为了测“没去问”把 seam 关了，这里要重新开）
    _enable(monkeypatch)
    _answer(
        monkeypatch,
        features=(_feature(osm_id=3289208, osm_type="R", name=RAW_COUNTY, extent=COUNTY_EXTENT),),
    )
    resolved = _capability(RAW_COUNTY)
    assert resolved["capability"] == "HYDRATABLE", resolved["capability"]
    _answer(monkeypatch, features=())
    empty = _capability(RAW_COUNTY)
    assert empty["capability"] == "UNAVAILABLE", empty["capability"]


def _break_the_seam(monkeypatch) -> None:
    """Arm the third runtime cause: our own seam cannot even be constructed."""

    def explode():
        raise RuntimeError("receipt: the resolver leg cannot be built")

    monkeypatch.setattr(capability_module, "_provider_identity_resolver", explode)
