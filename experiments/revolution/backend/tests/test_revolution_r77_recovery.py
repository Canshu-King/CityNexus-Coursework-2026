from __future__ import annotations

import ipaddress
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.agent.gateway import AgentToolGateway
from app.db.models import Place, RealPlaceCandidate
from app.db.session import SessionLocal, ensure_schema
from app.fact_acquisition import (
    _safe_get,
    _validate_public_http_url,
    _search_fact_candidates,
    _acquire_overpass_payload,
    _official_web_facts,
    _plain_text_cost,
    _plain_text_opening,
    acquire_place_facts,
)
from app.main import _plan_mobility_eligibility_receipts, _provider_mesh_candidate_payload
from app.schemas import AgentToolCall
from app.services import (
    COURSEWORK_ESTIMATE_AUTHORITY_ENVELOPE,
    SEALED_DEMO_AUTHORITY_ENVELOPE,
    UNRESOLVED_ACTIVITY_FACTS,
    _catalogue_templates,
    _mobility_catalogue_place_ids,
    plan_authority_blocker,
)
from app.stay_mobility import _plan_mobility_projection


def test_live_replan_candidate_with_unknown_place_facts_cannot_cross_authority_boundary():
    place = Place(
        id="real-place-unknown-facts",
        provider_key="overpass",
        external_id="overpass:node/123456",
        city_code="CN-TEST",
        name="Live unknown place",
        category="attraction",
        latitude=31.0,
        longitude=120.0,
        opening_hours_json=json.dumps({"status": "UNKNOWN"}),
        tags_json="[]",
        metadata_json=json.dumps(
            {
                "cost_evidence": {"status": "UNKNOWN", "amount_minor": None},
                "cost_minor": 0,
                "price_known": False,
            }
        ),
        updated_at="2026-09-30T00:00:00+00:00",
    )


def test_coursework_estimate_envelope_is_explicit_and_lab_scoped(monkeypatch):
    place = Place(
        id="coursework-estimate-place",
        provider_key="overpass",
        external_id="overpass:node/coursework-estimate",
        city_code="CN-TEST",
        name="Coursework estimated place",
        category="museum",
        latitude=31.0,
        longitude=120.0,
        opening_hours_json=json.dumps({"status": "UNKNOWN"}),
        tags_json="[]",
        metadata_json=json.dumps(
            {
                "cost_evidence": {"status": "UNKNOWN", "amount_minor": None},
                "strategy_estimate": {
                    "status": "ESTIMATED_AGENT",
                    "opening_hours": {"open_minutes": 600, "close_minutes": 1080},
                    "cost_evidence": {"status": "ESTIMATED_AGENT", "amount_minor": 0},
                },
            }
        ),
        updated_at="2026-09-30T00:00:00+00:00",
    )
    plan = SimpleNamespace(
        id="coursework-estimate-plan",
        explanation_json=json.dumps(
            {
                "authority_state": "COURSEWORK_ESTIMATED_FACTS",
                "authority_envelope": COURSEWORK_ESTIMATE_AUTHORITY_ENVELOPE,
            }
        ),
        mobility_evidence_json=json.dumps(
            {"snapshot_id": "live-snapshot", "source_provider": "valhalla-http", "facts": []}
        ),
    )

    monkeypatch.setattr("app.services.settings", SimpleNamespace(lab_estimate_authority=True))
    assert plan_authority_blocker(plan, {place.id: place}, used_place_ids=[place.id]) is None

    monkeypatch.setattr("app.services.settings", SimpleNamespace(lab_estimate_authority=False))
    assert (
        plan_authority_blocker(plan, {place.id: place}, used_place_ids=[place.id])
        == UNRESOLVED_ACTIVITY_FACTS
    )
    plan = SimpleNamespace(
        id="candidate-with-unknown-facts",
        explanation_json=json.dumps({"reason": "replan candidate"}),
        mobility_evidence_json=json.dumps({"snapshot_id": "live-snapshot", "facts": []}),
    )

    assert (
        plan_authority_blocker(
            plan,
            {place.id: place},
            used_place_ids=[place.id],
        )
        == UNRESOLVED_ACTIVITY_FACTS
    )


def test_explicit_sealed_demo_envelope_keeps_its_existing_authority_contract():
    place = Place(
        id="demo-place-sealed-envelope",
        provider_key="mock-city",
        external_id="demo:fixture/1",
        city_code="CNX-DEMO",
        name="Sealed demo place",
        category="attraction",
        latitude=31.0,
        longitude=120.0,
        opening_hours_json=json.dumps({"status": "UNKNOWN"}),
        tags_json="[]",
        metadata_json=json.dumps(
            {
                "cost_evidence": {"status": "UNKNOWN", "amount_minor": None},
                "cost_minor": 0,
                "price_known": False,
                "authority_envelope": SEALED_DEMO_AUTHORITY_ENVELOPE,
            }
        ),
        updated_at="2026-09-30T00:00:00+00:00",
    )
    plan = SimpleNamespace(
        id="sealed-demo-plan",
        explanation_json=json.dumps({"reason": "explicit sealed demo fixture"}),
        mobility_evidence_json=json.dumps({"snapshot_id": "demo-snapshot", "facts": []}),
    )

    assert plan_authority_blocker(plan, {place.id: place}, used_place_ids=[place.id]) is None


class _Response:
    def __init__(self, payload, *, status_code: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_official_html_parser_promotes_date_applicable_opening_and_cost(monkeypatch):
    from app import fact_acquisition

    body = """
    <html><script>ignore opening hours 00:00-00:01</script>
      <div>开放时间：7:30-17:30 (3月1日-10月31日)，7:30-17:00 (11月1日-次年2月底)</div>
      <div>价格：淡季30元（1月-3月、6月、11月-12月）；旺季40元（4月-5月、7月-10月）</div>
    </html>
    """
    body = (
        "<div>&#x5f00;&#x653e;&#x65f6;&#x95f4;&#xff1a;7:30-17:30 (3&#x6708;1&#x65e5;-10&#x6708;31&#x65e5;)，"
        "7:30-17:00 (11&#x6708;1&#x65e5;-&#x6b21;&#x5e74;2&#x6708;&#x5e95;)</div>"
        "<div>&#x4ef7;&#x683c;&#xff1a;&#x6de1;&#x5b63;30&#x5143;&#xff08;1&#x6708;-3&#x6708;&#x3001;6&#x6708;&#x3001;11&#x6708;-12&#x6708;&#xff09;;&#x65fa;&#x5b63;40&#x5143;&#xff08;4&#x6708;-5&#x6708;&#x3001;7&#x6708;-10&#x6708;&#xff09;</div>"
    )

    class Response:
        text = body
        status_code = 200

    monkeypatch.setattr(
        fact_acquisition,
        "_safe_get",
        lambda *_args, **_kwargs: (Response(), "https://visitsz.example/detail"),
    )
    result = _official_web_facts(
        object(),
        "https://visitsz.example/detail",
        retrieved_at="2026-10-04T00:00:00+00:00",
        reference_at="2026-10-04T09:00:00+08:00",
    )

    assert result["status"] == "ACQUIRED"
    assert result["opening_hours"]["status"] == "OFFICIAL_WEB_VERIFIED"
    assert (result["opening_hours"]["open"], result["opening_hours"]["close"]) == ("07:30", "17:30")
    assert result["cost_evidence"]["status"] == "VERIFIED_PROVIDER"
    assert result["cost_evidence"]["amount_minor"] == 4000
    assert len(result["cost_evidence"]["seasonal_amounts"]) == 2


def test_lotsmall_exact_product_api_promotes_bound_provider_facts(monkeypatch):
    from app import fact_acquisition

    final_url = (
        "https://wap.lotsmall.cn/vue/detail/ticket?"
        "id=8187&productCode=park2021051111555676069&m_id=1939"
    )
    detail = {
        "data": {
            "id": 8187,
            "productCode": "park2021051111555676069",
            "merchantInfoId": 1939,
            "openTime": "07:30",
            "endTime": "17:30",
            "orderNotice": "每年3月1日-10月31日：7:30开始检票，17:00停止检票入园，17:30闭园；每年11月1日-次年2月底：7:30开始检票，16:30停止检票入园，17:00闭园。",
        }
    }
    grid = {
        "data": [
            {
                "labelValue": "成人票",
                "ticketList": [
                    {
                        "id": "49827",
                        "parkTicketId": "52526",
                        "modelName": "网师园旺季成人票",
                        "modelCode": "MP2021060917054740913",
                        "merchantParkInfoId": "8187",
                        "merchantInfoId": "1939",
                        "price": 40,
                        "saleStatus": "T",
                        "buyDateAllow": True,
                    }
                ],
            }
        ]
    }

    def fake_safe_get(_client, url, **_kwargs):
        if url.endswith("jump.szjlxx.cn/?park=wsy"):
            return _Response({}, headers={"location": final_url}), final_url
        if "/merchantParkInfo/merchantParkInfo?" in url:
            return _Response(detail), url
        if "/merchantParkTicketGridNew?" in url:
            return _Response(grid), url
        raise AssertionError(url)

    monkeypatch.setattr(fact_acquisition, "_safe_get", fake_safe_get)
    binding = {
        "provider_entity": "overpass:node/5042033121",
        "source_url": "http://jump.szjlxx.cn/?park=wsy",
        "source_class": "OFFICIAL_TICKETING",
        "identity_basis": {
            "matched_provider_entity": True,
            "provider_entity": "overpass:node/5042033121",
        },
    }
    result = _official_web_facts(
        object(),
        "http://jump.szjlxx.cn/?park=wsy",
        retrieved_at="2026-09-30T12:00:00+08:00",
        reference_at="2026-10-04T09:00:00+08:00",
        source_binding=binding,
    )

    assert result["status"] == "ACQUIRED"
    assert result["opening_hours"]["status"] == "OFFICIAL_WEB_VERIFIED"
    assert result["opening_hours"]["open"] == "07:30"
    assert result["cost_evidence"]["status"] == "VERIFIED_PROVIDER"
    assert result["cost_evidence"]["amount_minor"] == 4000
    assert result["provider_identity"]["merchant_park_info_id"] == "8187"
    assert result["opening_hours"]["close"] == "17:30"
    assert len(result["opening_hours"]["seasonal_periods"]) == 2

    winter = _official_web_facts(
        object(),
        "http://jump.szjlxx.cn/?park=wsy",
        retrieved_at="2026-09-30T12:00:00+08:00",
        reference_at="2026-11-04T09:00:00+08:00",
        source_binding=binding,
    )
    assert winter["opening_hours"]["status"] == "OFFICIAL_WEB_VERIFIED"
    assert winter["opening_hours"]["close"] == "17:00"


def test_provider_mesh_projects_current_canonical_place_facts_over_capture():
    candidate = SimpleNamespace(
        id="rpc-stale-node",
        provider_key="overpass",
        external_id="overpass:node/5042033121",
        name="网师园",
        category="attraction",
        latitude=31.3003833,
        longitude=120.6298435,
        tags_json="[]",
        opening_hours_json=json.dumps({"status": "UNKNOWN"}),
        metadata_json=json.dumps(
            {"cost_evidence": {"status": "UNKNOWN", "amount_minor": None}}
        ),
        captured_at="2026-09-29T19:55:29+00:00",
        expires_at=None,
        materialized_place_id="real-place-bcd9c9f324751168d422a06b",
        status="DISCOVERED",
    )
    canonical = SimpleNamespace(
        id="real-place-bcd9c9f324751168d422a06b",
        opening_hours_json=json.dumps(
            {"status": "OFFICIAL_WEB_VERIFIED", "open": "07:30", "close": "17:30"}
        ),
        metadata_json=json.dumps(
            {
                "cost_evidence": {
                    "status": "VERIFIED_PROVIDER",
                    "amount_minor": 4000,
                    "currency": "CNY",
                }
            }
        ),
        updated_at="2026-09-30T04:46:14+00:00",
    )

    result = _provider_mesh_candidate_payload(candidate, canonical)

    assert result["facts_source"] == "CANONICAL_PLACE"
    assert result["facts_updated_at"] == "2026-09-30T04:46:14+00:00"
    assert result["opening_hours"]["status"] == "OFFICIAL_WEB_VERIFIED"
    assert result["cost_evidence"]["status"] == "VERIFIED_PROVIDER"
    assert result["cost_evidence"]["amount_minor"] == 4000
    assert json.loads(candidate.opening_hours_json)["status"] == "UNKNOWN"


def test_lotsmall_product_identity_mismatch_stays_unknown(monkeypatch):
    from app import fact_acquisition

    final_url = (
        "https://wap.lotsmall.cn/vue/detail/ticket?"
        "id=8187&productCode=park2021051111555676069&m_id=1939"
    )

    def fake_safe_get(_client, url, **_kwargs):
        if url == "http://jump.szjlxx.cn/?park=wsy":
            return _Response({}, headers={"location": final_url}), final_url
        return _Response(
            {
                "data": {
                    "id": 8187,
                    "productCode": "different-product",
                    "merchantInfoId": 1939,
                    "openTime": "07:30",
                    "endTime": "17:30",
                }
            }
        ), url

    monkeypatch.setattr(fact_acquisition, "_safe_get", fake_safe_get)
    result = _official_web_facts(
        object(),
        "http://jump.szjlxx.cn/?park=wsy",
        retrieved_at="2026-09-30T12:00:00+08:00",
        reference_at="2026-10-04T09:00:00+08:00",
        source_binding={
            "provider_entity": "overpass:node/5042033121",
            "source_url": "http://jump.szjlxx.cn/?park=wsy",
            "source_class": "OFFICIAL_TICKETING",
            "identity_basis": {"matched_provider_entity": True},
        },
    )

    assert result["status"] == "NO_STRUCTURED_FACTS"
    assert result["opening_hours"]["status"] == "UNKNOWN"
    assert result["cost_evidence"]["status"] == "UNKNOWN"


def test_official_html_parser_keeps_verified_free_and_all_day_distinct_from_unknown(monkeypatch):
    from app import fact_acquisition

    body = "<div>开放时间：全天开放</div><div>价格：免费</div>"
    body = "<div>&#x5f00;&#x653e;&#x65f6;&#x95f4;&#xff1a;&#x5168;&#x5929;&#x5f00;&#x653e;</div><div>&#x4ef7;&#x683c;&#xff1a;&#x514d;&#x8d39;</div>"

    class Response:
        text = body
        status_code = 200

    monkeypatch.setattr(
        fact_acquisition,
        "_safe_get",
        lambda *_args, **_kwargs: (Response(), "https://visitsz.example/detail"),
    )
    result = _official_web_facts(object(), "https://visitsz.example/detail", retrieved_at="now")

    assert result["opening_hours"]["status"] == "OFFICIAL_WEB_VERIFIED"
    assert result["opening_hours"]["always_available"] is True
    assert result["cost_evidence"]["status"] == "VERIFIED_PROVIDER"
    assert result["cost_evidence"]["amount_minor"] == 0


def test_official_municipal_cultural_service_free_opening_is_structured_cost(monkeypatch):
    from app import fact_acquisition

    body = "玉龙纳西族自治县文化馆持续提供公共文化服务免费开放。"

    class Response:
        text = body
        status_code = 200

    monkeypatch.setattr(
        fact_acquisition,
        "_safe_get",
        lambda *_args, **_kwargs: (Response(), "https://www.lijiang.cn/article/166182.html"),
    )
    result = _official_web_facts(
        object(),
        "https://www.lijiang.cn/article/166182.html",
        retrieved_at="now",
        source_binding={"source_class": "MUNICIPAL_AUTHORITY"},
    )

    assert result["cost_evidence"]["status"] == "VERIFIED_PROVIDER"
    assert result["cost_evidence"]["amount_minor"] == 0


def test_official_html_parser_keeps_login_shell_as_no_structured_facts(monkeypatch):
    from app import fact_acquisition

    body = """
    <html><title>用户登录</title>
      <input placeholder="请输入手机号/邮箱" />
      <input placeholder="请输入密码" />
      <div>登录即代表同意《用户协议》</div>
    </html>
    """

    class Response:
        text = body
        status_code = 200

    monkeypatch.setattr(
        fact_acquisition,
        "_safe_get",
        lambda *_args, **_kwargs: (Response(), "https://wap.example/login"),
    )
    result = _official_web_facts(object(), "https://wap.example/login", retrieved_at="now")

    assert result["status"] == "NO_STRUCTURED_FACTS"
    assert result["opening_hours"]["status"] == "UNKNOWN"
    assert result["cost_evidence"]["status"] == "UNKNOWN"


def test_official_price_label_with_full_width_spacing_is_parsed_as_cost(monkeypatch):
    from app import fact_acquisition

    body = (
        "<div>&#x4ef7;&#x3000;&#x3000;&#x683c;&#xff1a;30&#x5143;"
        "&#xff08;1&#x6708;-3&#x6708;&#xff09;</div>"
    )

    class Response:
        text = body
        status_code = 200

    monkeypatch.setattr(
        fact_acquisition,
        "_safe_get",
        lambda *_args, **_kwargs: (Response(), "https://visitsz.example/detail"),
    )
    result = _official_web_facts(
        object(),
        "https://visitsz.example/detail",
        retrieved_at="2026-01-10T00:00:00+00:00",
        reference_at="2026-01-10T09:00:00+08:00",
    )

    assert result["cost_evidence"]["status"] == "VERIFIED_PROVIDER"
    assert result["cost_evidence"]["amount_minor"] == 3000


def test_official_ticket_context_without_price_label_selects_ticket_amount(monkeypatch):
    from app import fact_acquisition

    body = "<div>团队签单 100元 七星景区+七星岩套票 55元 景区门票预定</div>"

    class Response:
        text = body
        status_code = 200

    monkeypatch.setattr(
        fact_acquisition,
        "_safe_get",
        lambda *_args, **_kwargs: (Response(), "https://www.glqxjq.cn/"),
    )
    result = _official_web_facts(
        object(),
        "https://www.glqxjq.cn/",
        retrieved_at="2026-10-01T00:00:00+00:00",
        reference_at="2026-10-14T09:00:00+08:00",
    )

    assert result["cost_evidence"]["status"] == "VERIFIED_PROVIDER"
    assert result["cost_evidence"]["amount_minor"] == 5500
    assert result["cost_evidence"]["pricing_basis"] == "TICKET_CONTEXT_LABEL"


def test_fact_source_binding_requires_exact_provider_identity():
    from app.fact_acquisition import _fact_source_binding

    place = SimpleNamespace(external_id="overpass:way/46")
    assert _fact_source_binding(
        place,
        {
            "fact_source_binding": {
                "provider_entity": "overpass:way/other",
                "source_url": "https://official.example/46",
                "source_class": "MUNICIPAL_AUTHORITY",
                "identity_basis": {"matched_provider_entity": True, "provider_entity": "overpass:way/other"},
            }
        },
    ) is None
    binding = _fact_source_binding(
        place,
        {
            "fact_source_binding": {
                "provider_entity": "overpass:way/46",
                "source_url": "https://official.example/46",
                "source_class": "MUNICIPAL_AUTHORITY",
                "identity_basis": {"matched_provider_entity": True, "provider_entity": "overpass:way/46"},
            }
        },
    )
    assert binding["source_url"] == "https://official.example/46"


def test_requested_fact_source_binding_derives_exact_provider_identity():
    from app.fact_acquisition import _requested_fact_source_binding

    place = SimpleNamespace(external_id="overpass:node/3818354965")
    binding = _requested_fact_source_binding(
        place,
        {
            "source_url": "https://www.heijo-park.jp/information/facility/",
            "source_class": "MUNICIPAL_AUTHORITY",
        },
    )

    assert binding == {
        "provider_entity": "overpass:node/3818354965",
        "source_url": "https://www.heijo-park.jp/information/facility/",
        "source_class": "MUNICIPAL_AUTHORITY",
        "identity_basis": {
            "matched_provider_entity": True,
            "provider_entity": "overpass:node/3818354965",
            "basis": "OPERATOR_CONFIRMED_EXACT_PROVIDER_ENTITY",
        },
        "approval": "OPERATOR_VERIFIED",
    }


def test_official_japanese_facility_labels_are_parsed_as_authoritative_facts():
    source_url = "https://www.heijo-park.jp/information/facility/"
    opening = _plain_text_opening(
        "開館時間 9：00～17：00（入場は16：30まで）",
        source_url,
        reference_at="2026-10-02T09:00:00+09:00",
    )
    cost = _plain_text_cost(
        "入館料 無料", source_url, reference_at="2026-10-02T09:00:00+09:00"
    )

    assert opening["status"] == "OFFICIAL_WEB_VERIFIED"
    assert opening["open"] == "09:00"
    assert opening["close"] == "17:00"
    assert cost["status"] == "VERIFIED_PROVIDER"
    assert cost["amount_minor"] == 0


def test_fact_acquisition_keeps_one_live_client_for_secondary_and_official_phases(monkeypatch):
    ensure_schema()
    city = f"R77-CLIENT-{uuid4().hex}"
    place_id = f"r77-place-{uuid4().hex}"
    journey = SimpleNamespace(id=f"journey-{uuid4().hex}", city_code=city)
    with SessionLocal() as session:
        place = Place(
            id=place_id,
            provider_key="overpass",
            external_id="overpass:node/123",
            city_code=city,
            name="Real viewpoint",
            category="viewpoint",
            latitude=27.7,
            longitude=98.6,
            opening_hours_json=json.dumps({"status": "UNKNOWN"}),
            tags_json="[]",
            metadata_json=json.dumps(
                {
                    "cost_evidence": {
                        "status": "KNOWN",
                        "amount_minor": 500,
                        "currency": "CNY",
                    },
                    "cost_minor": 500,
                    "price_known": True,
                }
            ),
            updated_at="2026-09-30T00:00:00+00:00",
        )
        session.add(place)
        session.commit()

        class Client:
            instances = []

            def __init__(self, **kwargs):
                self.closed = False
                self.__class__.instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True
                return False

            def post(self, *_args, **_kwargs):
                assert not self.closed
                return _Response(
                    {"elements": [{"type": "node", "id": 123, "tags": {"tourism": "viewpoint"}}]}
                )

            def get(self, url, *_args, **_kwargs):
                assert not self.closed
                assert "lookup" in url
                return _Response([{"osm_type": "node", "osm_id": 123, "extratags": {}}])

        monkeypatch.setattr("app.fact_acquisition.httpx.Client", Client)
        receipt = acquire_place_facts(session, journey)
        session.commit()

        refreshed = session.get(Place, place_id)
        metadata = json.loads(refreshed.metadata_json)
        assert receipt["status"] == "ACQUIRED"
        assert refreshed.updated_at == receipt["retrieved_at"]
        assert receipt["secondary_source"]["status"] == "ACQUIRED"
        assert receipt["places"][0]["secondary_source"]["matched_provider_entity"] is True
        assert metadata["cost_evidence"]["status"] == "UNKNOWN"
        assert metadata["cost_evidence"]["amount_minor"] is None
        assert metadata["price_known"] is False
        assert Client.instances[0].closed is True


def test_bound_official_facts_supersede_known_map_tags(monkeypatch):
    from app import fact_acquisition

    ensure_schema()
    city = f"R77-OFFICIAL-PRIORITY-{uuid4().hex}"
    place_id = f"r77-place-{uuid4().hex}"
    provider_entity = "overpass:way/44679452"
    source_url = "https://official.example/master-of-nets"
    with SessionLocal() as session:
        place = Place(
            id=place_id,
            provider_key="overpass",
            external_id=provider_entity,
            city_code=city,
            name="网师园",
            category="attraction",
            latitude=31.3004105,
            longitude=120.6297906,
            opening_hours_json=json.dumps(
                {"status": "NORMALIZED_VALID", "raw": "Mo-Su 07:30-17:00", "close": "17:00"}
            ),
            tags_json="[]",
            metadata_json=json.dumps(
                {
                    "cost_evidence": {
                        "status": "KNOWN",
                        "amount_minor": 3000,
                        "currency": "CNY",
                        "source": "overpass",
                    },
                    "cost_minor": 3000,
                    "price_known": True,
                    "fact_source_binding": {
                        "provider_entity": provider_entity,
                        "source_url": source_url,
                        "source_class": "MUNICIPAL_AUTHORITY",
                        "identity_basis": {
                            "matched_provider_entity": True,
                            "provider_entity": provider_entity,
                        },
                        "approval": "OPERATOR_VERIFIED",
                    },
                }
            ),
            updated_at="2026-09-30T00:00:00+00:00",
        )
        session.add(place)
        session.commit()

        monkeypatch.setattr(
            fact_acquisition,
            "_acquire_overpass_payload",
            lambda *_args, **_kwargs: (
                {
                    "elements": [
                        {
                            "type": "way",
                            "id": "44679452",
                            "tags": {
                                "fee": "30 CNY",
                                "opening_hours": "Mo-Su 07:30-17:00",
                            },
                        }
                    ]
                },
                "https://overpass.example/api",
                [],
            ),
        )
        monkeypatch.setattr(
            fact_acquisition,
            "_nominatim_lookup",
            lambda *_args, **_kwargs: (
                {("way", "44679452"): {"osm_type": "way", "osm_id": 44679452, "extratags": {}}},
                "https://nominatim.example/lookup",
            ),
        )
        monkeypatch.setattr(
            fact_acquisition,
            "_official_web_facts",
            lambda *_args, **kwargs: {
                "provider": "official_web",
                "source": source_url,
                "status": "ACQUIRED",
                "source_binding": kwargs.get("source_binding"),
                "opening_hours": {
                    "status": "OFFICIAL_WEB_VERIFIED",
                    "source": "official_web",
                    "source_url": source_url,
                    "raw": "07:30-17:30",
                    "open": "07:30",
                    "close": "17:30",
                    "intervals": [{"open": "07:30", "close": "17:30", "days": ["*"]}],
                },
                "cost_evidence": {
                    "status": "VERIFIED_PROVIDER",
                    "amount_minor": 4000,
                    "currency": "CNY",
                    "source": "official_web",
                    "source_url": source_url,
                },
            },
        )
        monkeypatch.setattr(
            fact_acquisition,
            "_search_fact_candidates",
            lambda *_args, **_kwargs: {
                "provider": "searxng",
                "status": "UNCONFIGURED",
                "attempted": False,
                "observations": [],
                "opening_hours": {"status": "UNKNOWN", "intervals": []},
                "cost_evidence": {"status": "UNKNOWN", "amount_minor": None},
            },
        )

        receipt = acquire_place_facts(
            session,
            SimpleNamespace(id=f"journey-{uuid4().hex}", city_code=city, start_at="2026-10-04T09:00:00+08:00"),
            place_ids=[place_id],
        )
        session.commit()

        refreshed = session.get(Place, place_id)
        metadata = json.loads(refreshed.metadata_json)
        row = receipt["places"][0]
        assert receipt["official_web"]["acquired"] == 1
        assert row["opening_hours"]["status"] == "OFFICIAL_WEB_VERIFIED"
        assert row["opening_hours"]["close"] == "17:30"
        assert row["cost_evidence"]["status"] == "VERIFIED_PROVIDER"
        assert row["cost_evidence"]["amount_minor"] == 4000
        assert json.loads(refreshed.opening_hours_json)["close"] == "17:30"
        assert metadata["cost_evidence"]["amount_minor"] == 4000
        assert metadata["cost_minor"] == 4000


def test_agent_place_search_accepts_exact_name_with_fact_query_terms():
    ensure_schema()
    city = f"R77-AF-{uuid4().hex[:20]}"
    place_id = f"r77-place-{uuid4().hex}"
    with SessionLocal() as session:
        session.add(
            Place(
                id=place_id,
                provider_key="overpass",
                external_id=f"overpass:way/{uuid4().int % 1000000000}",
                city_code=city,
                name="网师园",
                category="attraction",
                latitude=31.3004,
                longitude=120.6298,
                opening_hours_json=json.dumps(
                    {"status": "OFFICIAL_WEB_VERIFIED", "open": "07:30", "close": "17:30"}
                ),
                tags_json="[]",
                metadata_json=json.dumps(
                    {
                        "cost_evidence": {
                            "status": "VERIFIED_PROVIDER",
                            "amount_minor": 4000,
                            "currency": "CNY",
                            "source": "official_web",
                        },
                        "fact_acquisition": {
                            "status": "ACQUIRED",
                            "official_web": {
                                "status": "ACQUIRED",
                                "source_url": "https://example.test/wangshi",
                                "raw": "captured article body " * 2000,
                            },
                        },
                    }
                ),
                updated_at="2026-09-30T00:00:00+00:00",
            )
        )
        session.commit()
        gateway = AgentToolGateway(session)
        result = gateway.search_city_services(
            AgentToolCall(
                request_id="r77-agent-fact-search",
                city_code=city,
                q="网师园 official opening hours ticket cost",
            )
        )
        assert [item["id"] for item in result["places"]] == [place_id]
        assert result["places"][0]["opening_hours"]["status"] == "OFFICIAL_WEB_VERIFIED"
        assert result["places"][0]["metadata"]["cost_evidence"]["amount_minor"] == 4000
        assert result["places"][0]["metadata"]["fact_acquisition"]["official_web"]["status"] == "ACQUIRED"
        assert "raw" not in result["places"][0]["metadata"]["fact_acquisition"]["official_web"]
        assert result["places"][0]["provider_key"] == "overpass"
        assert result["places"][0]["external_id"].startswith("overpass:way/")


def test_selected_plan_projection_exposes_edge_scoped_hard_receipts():
    plan = SimpleNamespace(
        id="r77-edge-receipt-plan",
        mobility_evidence_json=json.dumps(
            {
                "source_provider": "valhalla-http",
                "facts": [
                    {
                        "origin_key": "real-place-a",
                        "destination_key": "real-place-b",
                        "minutes": 13,
                        "provider": "valhalla-http",
                        "fact_quality": "LIVE_PROVIDER_CAPTURE",
                        "route_provenance": "LIVE_PROVIDER_CAPTURE",
                        "degraded": False,
                    }
                ],
            }
        ),
    )
    activities = [
        SimpleNamespace(place_id="real-place-a", city_service_id=None, metadata_json="{}"),
        SimpleNamespace(place_id="real-place-b", city_service_id=None, metadata_json="{}"),
    ]

    result = _plan_mobility_eligibility_receipts(plan, activities)

    assert result["used_edge_ids"] == [["real-place-a", "real-place-b"]]
    assert result["all_actual_edges_hard_eligible"] is True
    assert result["receipts"] == [
        {
            "origin_key": "real-place-a",
            "destination_key": "real-place-b",
            "provider": "valhalla-http",
            "fact_quality": "LIVE_PROVIDER_CAPTURE",
            "route_provenance": "LIVE_PROVIDER_CAPTURE",
            "degraded": False,
            "persisted_minutes": 13,
            "hard_minutes": 13,
            "hard_eligible": True,
            "decision": "HARD_ELIGIBLE",
        }
    ]


def test_selected_plan_projection_names_provider_collocated_zero_minute_edge():
    plan = SimpleNamespace(
        id="r77-collocated-plan",
        mobility_snapshot_id="r77-collocated-snapshot",
        mobility_evidence_json=json.dumps(
            {
                "source_provider": "valhalla-http",
                "facts": [
                    {
                        "origin_key": "real-place-way",
                        "destination_key": "real-place-node",
                        "minutes": 0,
                        "route_geometry": [[120.630126, 31.300803], [120.630126, 31.300803]],
                        "provider": "valhalla-http",
                        "fact_quality": "LIVE_PROVIDER_CAPTURE",
                        "route_provenance": "LIVE_PROVIDER_CAPTURE",
                        "degraded": False,
                    }
                ],
            }
        ),
    )
    activities = [
        SimpleNamespace(id="activity-way", place_id="real-place-way", city_service_id=None),
        SimpleNamespace(id="activity-node", place_id="real-place-node", city_service_id=None),
    ]

    summary, legs = _plan_mobility_projection(plan, activities)

    assert summary["used_edges"][0]["co_located"] is True
    assert legs[0]["duration_min"] == 0
    assert legs[0]["co_located"] is True


def test_fact_acquisition_rotates_to_mirror_after_transport_failure(monkeypatch):
    from app import fact_acquisition

    monkeypatch.setattr(
        fact_acquisition,
        "settings",
        SimpleNamespace(overpass_url="https://primary.example/api"),
    )

    class Client:
        def __init__(self):
            self.calls = []

        def post(self, url, **_kwargs):
            self.calls.append(url)
            if len(self.calls) == 1:
                raise httpx.ReadTimeout("primary timed out")
            return _Response({"elements": [{"type": "node", "id": 456}]})

    client = Client()
    payload, endpoint, attempts = _acquire_overpass_payload(client, [("node", "456")])

    assert payload["elements"][0]["id"] == 456
    assert client.calls[:2] == [
        "https://primary.example/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    ]
    assert endpoint == "https://overpass-api.de/api/interpreter"
    assert attempts[0]["answered"] is False
    assert attempts[0]["error"].startswith("ReadTimeout:")
    assert attempts[1]["answered"] is True
    assert attempts[1]["elements"] == 1


def test_strict_catalogue_mobility_excludes_unresolved_opening_rows():
    places = {
        "known-visit": SimpleNamespace(
            id="known-visit",
            category="attraction",
            opening_hours_json=json.dumps({"status": "NORMALIZED_VALID", "open": "09:00", "close": "17:00"}),
            metadata_json=json.dumps({}),
        ),
        "unknown-visit": SimpleNamespace(
            id="unknown-visit",
            category="attraction",
            opening_hours_json=json.dumps({"status": "UNKNOWN"}),
            metadata_json=json.dumps({}),
        ),
        "lodging": SimpleNamespace(
            id="lodging",
            category="hotel",
            opening_hours_json=json.dumps({"status": "NORMALIZED_VALID", "open": "00:00", "close": "24:00"}),
            metadata_json=json.dumps({}),
        ),
    }

    assert _mobility_catalogue_place_ids(places, [], []) == ("known-visit",)


def test_strict_catalogue_templates_exclude_unverified_cost_rows():
    def place(place_id: str, cost: int | None):
        metadata = {"duration_min": 60}
        if cost is None:
            metadata["cost_evidence"] = {"status": "UNKNOWN", "amount_minor": None}
        else:
            metadata["cost_evidence"] = {
                "status": "KNOWN",
                "amount_minor": cost,
                "currency": "CNY",
            }
        return SimpleNamespace(
            id=place_id,
            name=place_id,
            category="park",
            provider_key="overpass",
            opening_hours_json=json.dumps({"open": "09:00", "close": "17:00"}),
            metadata_json=json.dumps(metadata),
        )

    places = {
        item.id: item
        for item in (
            place("unknown-cost", None),
            place("known-cost-a", 3000),
            place("known-cost-b", 9000),
        )
    }

    templates = _catalogue_templates(places, lambda _origin, _destination: 5, variants=1)

    assert templates
    assert [item[0] for item in templates[0]] == ["known-cost-a", "known-cost-b"]
    assert all(item[0] != "unknown-cost" for template in templates for item in template)


def test_real_catalogue_treats_verified_gardens_as_visit_stops():
    from app.services import _catalogue_visit_eligible

    garden = SimpleNamespace(
        category="garden",
        metadata_json="{}",
    )
    assert _catalogue_visit_eligible(garden) is True


def test_real_catalogue_treats_theatres_as_visit_stops():
    from app.services import _catalogue_visit_eligible

    theatre = SimpleNamespace(
        category="theatre",
        metadata_json="{}",
    )
    assert _catalogue_visit_eligible(theatre) is True


def test_strategy_voi_prefers_missing_facts_matching_intent():
    from app.services import _strategy_voi_decisions

    def unknown(place_id: str, category: str, tags: list[str]):
        return SimpleNamespace(
            id=place_id,
            name=place_id,
            category=category,
            tags_json=json.dumps(tags),
            opening_hours_json=json.dumps({"status": "UNKNOWN"}),
            metadata_json=json.dumps({"cost_evidence": {"status": "UNKNOWN"}}),
        )

    museum = unknown("unknown-museum", "museum", ["indoor"])
    park = unknown("unknown-park", "park", ["outdoor"])
    decisions = _strategy_voi_decisions(
        None,
        None,
        {museum.id: museum, park.id: park},
        {"user_text": "indoor museum and craft", "preferences": {"indoor": True}},
    )
    assert decisions[0]["place_id"] == "unknown-museum"


def test_strategy_pivots_after_source_path_has_no_structured_facts():
    from app.services import _strategy_pivot_decisions

    abandoned = SimpleNamespace(
        id="unknown-source",
        name="unknown-source",
        category="museum",
        tags_json="[]",
        opening_hours_json=json.dumps({"status": "UNKNOWN"}),
        metadata_json=json.dumps({
            "cost_evidence": {"status": "UNKNOWN"},
            "fact_acquisition": {"official_web": {"status": "NO_STRUCTURED_FACTS"}},
        }),
    )
    replacement = SimpleNamespace(
        id="verified-museum",
        name="verified-museum",
        category="museum",
        tags_json="[\"indoor\"]",
        opening_hours_json=json.dumps({"status": "OFFICIAL_WEB_VERIFIED", "open": "10:00", "close": "17:00"}),
        metadata_json=json.dumps({"cost_evidence": {"status": "KNOWN", "amount_minor": 0}}),
    )
    pivots = _strategy_pivot_decisions(
        {abandoned.id: abandoned, replacement.id: replacement},
        {"user_text": "indoor museum", "preferences": {"indoor": True}},
    )
    assert pivots[0]["decision"] == "ABANDON_SOURCE_PATH"
    assert pivots[0]["abandoned_place_ids"] == ["unknown-source"]
    assert pivots[0]["replacement_place_ids"] == ["verified-museum"]


def test_official_source_boundary_rejects_private_targets_and_redirects(monkeypatch):
    with pytest.raises(ValueError, match="OFFICIAL_SOURCE_PRIVATE_TARGET"):
        _validate_public_http_url("http://127.0.0.1:8080/facts")

    from app import fact_acquisition

    original_resolve = fact_acquisition._resolved_public_addresses

    def resolve(hostname):
        if hostname == "example.com":
            return {ipaddress.ip_address("93.184.216.34")}
        return original_resolve(hostname)

    monkeypatch.setattr("app.fact_acquisition._resolved_public_addresses", resolve)

    class RedirectClient:
        def get(self, *_args, **_kwargs):
            return _Response({}, status_code=302, headers={"location": "http://127.0.0.1/secret"})

    with pytest.raises(ValueError, match="OFFICIAL_SOURCE_PRIVATE_TARGET"):
        _safe_get(RedirectClient(), "https://example.com/facts", retrieved_at="now")


def test_search_source_returns_structured_candidates_without_authority(monkeypatch):
    from app import fact_acquisition
    from app.integrations.web.searxng import SearXNGObservation

    monkeypatch.setattr(
        fact_acquisition,
        "settings",
        SimpleNamespace(searxng_url="https://search.example.test"),
    )

    class Provider:
        def __init__(self, base_url, *, client):
            assert base_url == "https://search.example.test"

        def search(self, query, *, limit):
            assert '"way/1417996052"' in query
            assert limit == 5
            return (
                SearXNGObservation(
                    query=query,
                    url="https://example.gov.cn/park",
                    title="贡山县总体国家安全观主题公园",
                    domain="example.gov.cn",
                    content="开放时间：09:00-17:00；门票：30元",
                    captured_at="2026-09-25T00:00:00+00:00",
                ),
            )

        def close(self):
            return None

    monkeypatch.setattr(fact_acquisition, "SearXNGSearchProvider", Provider)
    result = _search_fact_candidates(
        object(),
        SimpleNamespace(name="贡山县总体国家安全观主题公园", external_id="overpass:way/1417996052"),
        SimpleNamespace(city_code="OSM:R:2729765"),
        retrieved_at="2026-09-25T00:00:01+00:00",
    )
    assert result["status"] == "CANDIDATES_FOUND"
    assert result["opening_hours"]["status"] == "SEARCH_CANDIDATE"
    assert result["cost_evidence"]["status"] == "SEARCH_CANDIDATE"
    assert result["cost_evidence"]["amount_minor"] == 3000
    assert result["observations"][0]["authority_status"] == "SEARCH_CANDIDATE"


def test_search_candidate_never_overwrites_unknown_place_facts(monkeypatch):
    from app import fact_acquisition
    from app.integrations.web.searxng import SearXNGObservation

    monkeypatch.setattr(
        fact_acquisition,
        "settings",
        SimpleNamespace(
            overpass_url="https://overpass.example/api",
            nominatim_url="https://nominatim.example",
            searxng_url="https://search.example",
        ),
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, **kwargs):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True
            return False

        def post(self, *args, **kwargs):
            assert not self.closed
            return Response({"elements": [{"type": "node", "id": 177, "tags": {"tourism": "viewpoint"}}]})

        def get(self, *args, **kwargs):
            assert not self.closed
            return Response([{"osm_type": "node", "osm_id": 177, "extratags": {}}])

    class Provider:
        def __init__(self, base_url, *, client):
            assert base_url == "https://search.example"

        def search(self, query, *, limit):
            return (
                SearXNGObservation(
                    query=query,
                    url="https://example.gov.cn/park",
                    title="贡山县总体国家安全观主题公园",
                    domain="example.gov.cn",
                    content="开放时间：09:00-17:00；门票：30元",
                    captured_at="2026-09-25T00:00:00+00:00",
                ),
            )

        def close(self):
            return None

    monkeypatch.setattr("app.fact_acquisition.httpx.Client", Client)
    monkeypatch.setattr(fact_acquisition, "SearXNGSearchProvider", Provider)
    ensure_schema()
    place_id = f"r77-search-{uuid4().hex}"
    with SessionLocal() as session:
        place = Place(
            id=place_id,
            provider_key="overpass",
            external_id="overpass:node/177",
            city_code="OSM:R:2729765",
            name="贡山县总体国家安全观主题公园",
            category="viewpoint",
            latitude=27.7,
            longitude=98.6,
            opening_hours_json=json.dumps({"status": "UNKNOWN"}),
            tags_json="[]",
            metadata_json=json.dumps({}),
            updated_at="2026-09-25T00:00:00+00:00",
        )
        session.add(place)
        session.commit()
        receipt = acquire_place_facts(
            session,
            SimpleNamespace(id=f"journey-{uuid4().hex}", city_code="OSM:R:2729765"),
        )
        session.commit()
        refreshed = session.get(Place, place_id)
        metadata = json.loads(refreshed.metadata_json)
        assert receipt["search_source"]["status"] == "CANDIDATES_FOUND"
        assert metadata["fact_acquisition"]["search_source"]["cost_evidence"]["status"] == "SEARCH_CANDIDATE"
        assert metadata["cost_evidence"]["status"] == "UNKNOWN"
        assert metadata["cost_evidence"]["amount_minor"] is None
        assert metadata["price_known"] is False
        assert json.loads(refreshed.opening_hours_json)["status"] == "UNKNOWN"
        session.delete(refreshed)
        session.commit()


def test_live_discovery_cache_is_keyed_by_the_provider_question():
    from app.integrations.discovery.real_city import _unexpired_cached_candidates

    ensure_schema()
    city = f"R77-CACHE-{uuid4().hex}"
    journey_id = None
    place_id = f"r77-cache-place-{uuid4().hex}"
    captured_at = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    with SessionLocal() as session:
        session.add(
            Place(
                id=place_id,
                provider_key="overpass",
                external_id=f"overpass:node/{uuid4().int % 100000000}",
                city_code=city,
                name="Question-keyed viewpoint",
                category="viewpoint",
                latitude=27.7,
                longitude=98.6,
                opening_hours_json=json.dumps({"status": "UNKNOWN"}),
                tags_json="[]",
                metadata_json=json.dumps({"cost_evidence": {"status": "UNKNOWN"}}),
                updated_at=captured_at,
            )
        )
        session.add(
            RealPlaceCandidate(
                id=f"rpc-{uuid4().hex}",
                journey_id=journey_id,
                provider_key="overpass",
                external_id="overpass:node/987654321",
                city_code=city,
                name="Question-keyed viewpoint",
                category="viewpoint",
                latitude=27.7,
                longitude=98.6,
                tags_json="[]",
                opening_hours_json=json.dumps({"status": "UNKNOWN"}),
                metadata_json=json.dumps({"cost_evidence": {"status": "UNKNOWN"}}),
                captured_at=captured_at,
                expires_at=expires_at,
                distance_m=12.0,
                evidence_json=json.dumps({"request": {"query": "park"}}),
                materialized_place_id=place_id,
                status="DISCOVERED",
            )
        )
        session.commit()
        journey = SimpleNamespace(id=journey_id, city_code=city)
        assert _unexpired_cached_candidates(session, journey, "park", None)
        assert _unexpired_cached_candidates(session, journey, "viewpoint", None) is None


def test_japanese_admission_does_not_budget_rental_hours_or_headcounts():
    from app.fact_acquisition import _plain_text_cost, _plain_text_opening

    url = "https://official.example/facility"
    opening = _plain_text_opening(
        "開園時間 9時00分～17時00分 入園料 無料 茶室利用料金 12,560円",
        url, reference_at="2026-10-02",
    )
    assert (opening["open"], opening["close"]) == ("09:00", "17:00")
    cost = _plain_text_cost(
        "拝観料 団体30人以上 大人500円 中学生200円", url, reference_at=None,
    )
    assert cost["currency"] == "JPY"
    assert cost["amount_minor"] == 50000
    rental = _plain_text_cost("茶室利用料金 9:00～12:00 12,560円", url, reference_at=None)
    assert rental["status"] == "UNKNOWN"

    from app.fact_acquisition import _plain_text_opening
    primary = _plain_text_opening(
        "開園時間 9時00分～17時00分 入園料 無料 茶室利用時間 9時00分～12時00分",
        url, reference_at="2026-10-02",
    )
    assert (primary["open"], primary["close"]) == ("09:00", "17:00")
    whole_hour = _plain_text_opening("開館時間 10時～16時", url, reference_at=None)
    assert (whole_hour["open"], whole_hour["close"]) == ("10:00", "16:00")
    address_range = _plain_text_opening("開館時間 10時～16時 所在地 奈良市14-3", url, reference_at=None)
    assert len(address_range["seasonal_periods"]) == 1
    assert _plain_text_cost("入館無料", url, reference_at=None)["amount_minor"] == 0
    comma_price = _plain_text_cost("入園料 一般 1,200円 大学生 500円", url, reference_at=None)
    assert comma_price["currency"] == "JPY"
    assert comma_price["amount_minor"] == 120000
    assert _plain_text_cost(
        "入園料 一般 1,200円 介添え者１名無料", url, reference_at=None,
    )["amount_minor"] == 120000
    assert _plain_text_cost("観覧料 無料 / Free of Charge", url, reference_at=None)["amount_minor"] == 0
    assert _plain_text_cost(
        "観覧料 アクセス 駐車場 観覧料 | Admission Fee 無料 / Free of Charge",
        url, reference_at=None,
    )["amount_minor"] == 0
    navigation_page = (
        "観覧料 アクセス 駐車場 バリアフリー 施設の概要 開館時間 | Hours 10:00-18:00 "
        + "休館日 Mondays " * 30
        + "観覧料 | Admission Fee 無料 / Free of Charge アクセス"
    )
    assert _plain_text_cost(navigation_page, url, reference_at=None)["amount_minor"] == 0


def test_real_catalogue_fallback_is_visible_but_not_selectable():
    from fastapi.testclient import TestClient

    from app.main import app

    ensure_schema()
    city = f"R77-CATALOGUE-{uuid4().hex}"
    with SessionLocal() as session:
        session.add_all(
            [
                Place(
                    id=f"{city}-park",
                    provider_key="overpass",
                    external_id=f"overpass:node/{uuid4().int % 100000000}",
                    city_code=city,
                    name="Provider park",
                    category="park",
                    latitude=27.73,
                    longitude=98.67,
                    opening_hours_json=json.dumps({"status": "UNKNOWN"}),
                    tags_json="[]",
                    metadata_json=json.dumps({"cost_evidence": {"status": "UNKNOWN"}, "price_known": False}),
                    updated_at="2026-09-30T00:00:00+00:00",
                ),
                Place(
                    id=f"{city}-viewpoint",
                    provider_key="overpass",
                    external_id=f"overpass:node/{uuid4().int % 100000000}",
                    city_code=city,
                    name="Provider viewpoint",
                    category="viewpoint",
                    latitude=27.74,
                    longitude=98.68,
                    opening_hours_json=json.dumps({"status": "UNKNOWN"}),
                    tags_json="[]",
                    metadata_json=json.dumps({"cost_evidence": {"status": "UNKNOWN"}, "price_known": False}),
                    updated_at="2026-09-30T00:00:00+00:00",
                ),
            ]
        )
        session.commit()
    client = TestClient(app)
    traveler = client.post(
        "/api/v1/travelers",
        json={"display_name": "R77 test", "locale": "zh-CN", "timezone": "Asia/Shanghai"},
    ).json()
    journey = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler["id"],
            "title": "R77 catalogue",
            "city_code": city,
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-03T09:00:00+08:00",
            "end_at": "2026-10-03T20:00:00+08:00",
            "budget": {"amount_minor": 100000, "currency": "CNY"},
        },
    ).json()
    generated = client.post(
        f"/api/v1/journeys/{journey['id']}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert plan["selectable"] is False
    assert plan["explanation"]["authority_state"] == "PROVISIONAL_UNRESOLVED_FACTS"
    assert all(item["cost_status"] == "UNKNOWN" for item in plan["activities"])
    selected = client.post(
        f"/api/v1/journeys/{journey['id']}/plans/{plan['id']}/select"
    )
    assert selected.status_code == 409, selected.text
    assert selected.json()["error"]["reason_code"] == "UNRESOLVED_ACTIVITY_FACTS"


def test_explicit_provider_catalogue_unknown_cost_becomes_provisional_not_authoritative():
    from fastapi.testclient import TestClient
    from app.main import app

    ensure_schema()
    city = f"CN-R77-EXPLICIT-{uuid4().hex}"
    place_ids = [f"{city}-a", f"{city}-b"]
    with SessionLocal() as session:
        session.add_all(
            [
                Place(
                    id=place_ids[0],
                    provider_key="overpass",
                    external_id="overpass:node/901001",
                    city_code=city,
                    name="Provider attraction A",
                    category="attraction",
                    latitude=27.73,
                    longitude=98.67,
                    opening_hours_json=json.dumps({"status": "UNKNOWN"}),
                    tags_json="[]",
                    metadata_json=json.dumps({"cost_evidence": {"status": "UNKNOWN"}}),
                    updated_at="2026-09-30T00:00:00+00:00",
                ),
                Place(
                    id=place_ids[1],
                    provider_key="overpass",
                    external_id="overpass:node/901002",
                    city_code=city,
                    name="Provider attraction B",
                    category="attraction",
                    latitude=27.74,
                    longitude=98.68,
                    opening_hours_json=json.dumps({"status": "UNKNOWN"}),
                    tags_json="[]",
                    metadata_json=json.dumps({"cost_evidence": {"status": "UNKNOWN"}}),
                    updated_at="2026-09-30T00:00:00+00:00",
                ),
            ]
        )
        session.commit()
    client = TestClient(app)
    traveler = client.post(
        "/api/v1/travelers",
        json={"display_name": "R77 explicit provider", "locale": "zh-CN", "timezone": "Asia/Shanghai"},
    ).json()
    journey = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler["id"],
            "title": "R77 explicit provider catalogue",
            "city_code": city,
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-03T09:00:00+08:00",
            "end_at": "2026-10-03T20:00:00+08:00",
            "budget": {"amount_minor": 100000, "currency": "CNY"},
        },
    ).json()
    generated = client.post(
        f"/api/v1/journeys/{journey['id']}/plans/generate",
        json={"candidate_count": 1, "place_ids": place_ids},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert plan["selectable"] is False
    assert plan["explanation"]["authority_state"] == "PROVISIONAL_UNRESOLVED_FACTS"
    assert [item["place_id"] for item in plan["activities"]] == place_ids
    assert all(item["cost_status"] == "UNKNOWN" for item in plan["activities"])
