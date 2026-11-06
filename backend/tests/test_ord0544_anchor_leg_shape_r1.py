"""ORD-0544 · one unreadable leg answer must not demote the whole live capture.

Measured on this line before the fix (readings kept in `review/ord0544/diag_*.json`):

  * generation `8dd6905dec515b49b7713ea487806fe4f069d92a` (before ORD-0519): the cold pass asked
    `/search` then `/api/interpreter`, stored `provider_key = "overpass"`, and the warm pass made
    **0** outbound calls (`live-cache-hit`).
  * generation `c874b1d6bac6735d4ad164379a76db7b73398bd8` (after ORD-0519 put Photon first): a
    Photon body that is not a GeoJSON object raised `AttributeError`, which the orchestration filed
    as `PROVIDER_BAD_RESPONSE at anchor_geocode`.  The capture fell back to replay, the stored rows
    were keyed `openstreetmap-replay`, and because the TTL gate
    (`_unexpired_cached_candidates`, `backend/app/catalog_hydration_operations.py` reads it, the
    gate itself lives in `real_city.py`) credits only `provider_key == "overpass"`, **every warm ask
    paid for the network again** - which is exactly the g16b promise broken.

The fix is not to loosen the gate: replay must never be credited as supply (ORD-0519's own rule).
The fix is that a leg which replies with something unreadable is *that leg missing*, and the next
leg still gets asked.  These arms pin both halves, plus the named sub-word on `ALREADY_CURRENT`
that the commander ruled in `cmdr_1530.md` ③.
"""

from __future__ import annotations

import json
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import catalog_hydration_operations as hydration
from app.anywhere_capability import DestinationResolution
from app.core.config import settings
from app.db.models import Journey, Place, RealPlaceCandidate, Traveler
from app.db.session import ensure_schema
from app.integrations.discovery import real_city

CITY = "CN-TIANJIN"
QUERY = "咖啡馆附近"

CAFE_ELEMENT = {
    "type": "way",
    "id": 4242,
    "lat": 39.10,
    "lon": 117.20,
    "tags": {"amenity": "cafe", "name": "测试咖啡馆"},
}
NOMINATIM_ROW = [
    {
        "osm_type": "relation",
        "osm_id": 1,
        "lat": "39.12",
        "lon": "117.19",
        "display_name": "天津市, 中国",
        "type": "city",
        "class": "place",
    }
]


class _SettingsOverride:
    """`settings` is a frozen dataclass, so the module's own reference is swapped instead."""

    def __init__(self, **overrides: Any) -> None:
        object.__setattr__(self, "_overrides", dict(overrides))

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(settings, name)


class CallLog:
    """Counts every outbound call and answers from a route table this test controls."""

    def __init__(self, photon_body: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.photon_body = photon_body

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "CallLog":
        log = self
        real_client = httpx.Client

        def factory(**kwargs: Any) -> httpx.Client:
            kwargs.pop("timeout", None)
            kwargs.pop("trust_env", None)
            kwargs.pop("headers", None)
            return real_client(transport=httpx.MockTransport(log.handle))

        class _Shim:
            Client = staticmethod(factory)
            HTTPError = httpx.HTTPError
            TimeoutException = httpx.TimeoutException
            ConnectError = httpx.ConnectError

        monkeypatch.setattr(real_city, "httpx", _Shim)
        monkeypatch.setattr(
            real_city,
            "settings",
            _SettingsOverride(
                real_city_enabled=True,
                nominatim_url="http://anchor-shape.local",
                overpass_url="http://poi-shape.local",
            ),
        )
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"method": request.method, "path": request.url.path})
        path = request.url.path
        if path.endswith("/interpreter"):
            return httpx.Response(200, json={"elements": [CAFE_ELEMENT]})
        if path == "/api/":
            # the anchor leg's body; the mutant case hands it a Nominatim-shaped list
            return httpx.Response(200, json=self.photon_body)
        return httpx.Response(200, json=NOMINATIM_ROW)

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]


@pytest.fixture()
def private(tmp_path: Path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'anchor.db').as_posix()}")
    ensure_schema(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    now_iso = datetime.now(timezone.utc).isoformat()
    traveler = Traveler(id=str(uuidlib.uuid4()), display_name="ORD-0544", email=None,
                        locale="zh-CN", timezone="Asia/Shanghai",
                        created_at=now_iso, updated_at=now_iso)
    session.add(traveler)
    session.commit()
    start = datetime.now(timezone.utc).replace(microsecond=0)
    journey = Journey(id=str(uuidlib.uuid4()), traveler_id=traveler.id, title="天津 · 锚腿形状",
                      city_code=CITY, timezone="Asia/Shanghai", start_at=start.isoformat(),
                      end_at=(start + timedelta(days=2)).isoformat(),
                      budget_currency="CNY", budget_amount_minor=600000, status="DRAFT",
                      created_at=now_iso, updated_at=now_iso)
    session.add(journey)
    session.commit()
    try:
        yield session, journey
    finally:
        session.close()
        engine.dispose()


def _candidate_keys(session, journey_id: str) -> list[str]:
    return sorted({
        row.provider_key
        for row in session.query(RealPlaceCandidate).filter_by(journey_id=journey_id).all()
    })


def test_unreadable_anchor_answer_costs_one_leg_not_the_capture(
    private, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Photon body that is not GeoJSON must fall through to Nominatim, not to replay."""

    session, journey = private
    log = CallLog(photon_body=NOMINATIM_ROW).install(monkeypatch)  # a list where an object is due
    cold = real_city.capture_real_city_candidates(
        session, journey, query=QUERY, radius_m=2500, limit=5, mode="live")
    session.commit()

    assert cold["provenance"] == "LIVE_PROVIDER", cold["provider_status"]
    assert cold["mode"] == "live", cold["mode"]
    assert (cold["provider_status"] or {}).get("failure_stage") != "anchor_geocode"
    # the row is credited to the leg that actually answered: Nominatim grounded it, Overpass filled it
    assert _candidate_keys(session, journey.id) == ["overpass"], _candidate_keys(session, journey.id)
    legs = (cold["provider_status"] or {}).get("anchor_legs") or []
    by_leg = {item.get("leg"): item for item in legs}
    assert by_leg["photon"]["attempted"] is True
    assert by_leg["photon"]["answered"] is True
    assert by_leg["photon"]["outcome"] == "ANSWERED_NOT_GEOJSON", by_leg["photon"]
    assert by_leg["photon"]["bytes"] > 0, by_leg["photon"]
    assert by_leg["nominatim"]["outcome"] == "ANSWERED", by_leg["nominatim"]
    assert (cold["provider_status"] or {}).get("anchor", {}).get("source") == "nominatim_city_anchor"

    # and the warm ask that g16b pins: an unexpired set answers with nobody else asked
    warm_log = CallLog(photon_body=NOMINATIM_ROW).install(monkeypatch)
    warm = real_city.capture_real_city_candidates(
        session, journey, query=QUERY, radius_m=2500, limit=5, mode="live")
    session.commit()
    assert warm_log.paths() == [], warm_log.paths()
    assert warm["mode"] == "live-cache-hit", warm["mode"]


def test_geojson_anchor_answer_is_the_leg_credited(
    private, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other shape of the same credit: when Photon really answers, it is named and nobody else is."""

    session, journey = private
    geojson = {"features": [{"geometry": {"coordinates": [117.19, 39.12]},
                             "properties": {"name": "天津市", "osm_id": 9}}]}
    log = CallLog(photon_body=geojson).install(monkeypatch)
    cold = real_city.capture_real_city_candidates(
        session, journey, query=QUERY, radius_m=2500, limit=5, mode="live")
    session.commit()
    legs = {item.get("leg"): item for item in (cold["provider_status"] or {}).get("anchor_legs") or []}
    assert legs["photon"]["outcome"] == "ANSWERED", legs
    assert legs["nominatim"]["outcome"] == "SKIPPED", legs
    assert (cold["provider_status"] or {}).get("anchor", {}).get("source") == "photon_city_anchor"
    assert "/api/" in log.paths()


def _resolution_factory(state: str = "RESOLVED"):
    def resolve(session, query: str) -> DestinationResolution:
        canonical = CITY if "天津" in query else "IS-REYKJAVIK"
        return DestinationResolution(
            query=query, canonical_destination_id=canonical, city_code=canonical,
            method="test-pinned", state=state, matched_label=canonical,
            resolver_attempted=True, resolver_status="ANSWERED",
        )

    return resolve


def _run_hydration(session_factory, query: str) -> dict[str, Any]:
    with session_factory() as session:
        job = hydration.accept_hydration(session, query)
    operation_id = job.payload["operation_id"]
    hydration.dispatch(operation_id)
    row = hydration.wait_for_operation(operation_id, deadline_seconds=90.0)
    assert row is not None and row.status in hydration.TERMINAL_STATUSES, row.status if row else None
    return json.loads(row.result_json or "{}") | {"status": row.status, "outcome": row.outcome}


def test_already_current_says_which_fact_made_it_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sub-word is decided by this run's own write, and it is a closed pair of names."""

    engine = create_engine(f"sqlite:///{(tmp_path / 'worker.db').as_posix()}")
    ensure_schema(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(hydration, "resolve_destination", _resolution_factory())
    monkeypatch.setattr(hydration, "SessionLocal", factory)
    try:
        first = _run_hydration(factory, "天津")
        assert first["outcome"] == hydration.OUTCOME_ALREADY_CURRENT, first
        # a registered offline source wrote this city's rows on this very run
        assert first["outcome_qualifier"] == hydration.OUTCOME_QUALIFIER_OFFLINE_SOURCE, first
        assert (first.get("offline_source_applied") or {}).get("places_written", 0) > 0, first

        second = _run_hydration(factory, "天津")
        assert second["outcome"] == hydration.OUTCOME_ALREADY_CURRENT, second
        # nothing was applied this time: the store already held the material, and the word says so
        assert second.get("offline_source_applied") is None, second
        assert second["outcome_qualifier"] == hydration.OUTCOME_QUALIFIER_ALREADY_HELD, second
    finally:
        engine.dispose()


def test_a_refusal_gets_no_invented_sub_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NO_REGISTERED_SOURCE` must stay a bare zero: a qualifier would dress the refusal up."""

    engine = create_engine(f"sqlite:///{(tmp_path / 'refusal.db').as_posix()}")
    ensure_schema(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(hydration, "resolve_destination", _resolution_factory())
    monkeypatch.setattr(hydration, "SessionLocal", factory)
    monkeypatch.setenv(hydration.catalog_sources.DISABLE_ENV, "off")
    try:
        result = _run_hydration(factory, "雷克雅未克")
        assert result["outcome"] == hydration.OUTCOME_NO_REGISTERED_SOURCE, result
        assert result["outcome_qualifier"] is None, result
    finally:
        engine.dispose()
