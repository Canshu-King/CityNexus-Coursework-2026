from __future__ import annotations

from app.core.config import settings
from app.execution.bootstrap_discovery import bootstrap_discovery_readiness


def _set(name: str, value):
    object.__setattr__(settings, name, value)


def test_r105_provider_readiness_is_nonsecret_and_strict():
    names = ("real_city_enabled", "real_city_mode", "overpass_url", "city_search_test_provider")
    previous = {name: getattr(settings, name) for name in names}
    try:
        _set("real_city_enabled", False)
        _set("real_city_mode", "live")
        _set("overpass_url", "https://user:secret@example.invalid/api?token=secret")
        blocked = bootstrap_discovery_readiness()
        assert blocked["status"] == "BLOCKED"
        assert blocked["reason_code"] == "PROVIDER_DISABLED"
        assert "example.invalid" not in str(blocked)
        assert "secret" not in str(blocked)

        _set("real_city_enabled", True)
        _set("real_city_mode", "replay")
        replay = bootstrap_discovery_readiness()
        assert replay["reason_code"] == "LIVE_MODE_REQUIRED"

        _set("real_city_mode", "live")
        for endpoint in (
            "http://overpass-api.de/api",
            "https://unknown.example/api",
            "https://overpass-api.de/api?token=secret",
            "https://overpass-api.de/not-api",
        ):
            _set("overpass_url", endpoint)
            decision = bootstrap_discovery_readiness()
            assert decision["status"] == "BLOCKED"
            assert decision["reason_code"] == "ENDPOINT_NOT_APPROVED"
            assert decision["external_request_allowed"] is False

        _set("city_search_test_provider", True)
        _set("overpass_url", "http://127.0.0.1:8262/api")
        controlled = bootstrap_discovery_readiness()
        assert controlled["status"] == "READY"
        assert controlled["provider_grade"] == "TEST_PROVIDER"
        assert controlled["test_only"] is True
        assert controlled["external_request_allowed"] is False

        _set("overpass_url", "https://overpass-api.de/api")
        live = bootstrap_discovery_readiness()
        assert live["status"] == "READY"
        assert live["provider_grade"] == "LIVE_CAPABLE"
        assert live["capability_state"] == "LIVE_CAPABLE"
        assert live["external_request_allowed"] is True
        assert live["test_only"] is False
    finally:
        for name, value in previous.items():
            _set(name, value)


def test_r105_provider_readiness_rejects_loopback_without_test_boundary():
    names = ("real_city_enabled", "real_city_mode", "overpass_url", "city_search_test_provider")
    previous = {name: getattr(settings, name) for name in names}
    try:
        _set("real_city_enabled", True)
        _set("real_city_mode", "live")
        _set("city_search_test_provider", False)
        _set("overpass_url", "http://127.0.0.1:8262/api")
        decision = bootstrap_discovery_readiness()
        assert decision["status"] == "BLOCKED"
        assert decision["reason_code"] == "ENDPOINT_NOT_APPROVED"
    finally:
        for name, value in previous.items():
            _set(name, value)


def test_r105_signed_public_discovery_rejects_gps_before_provider(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import pytest
    from app.execution.bootstrap_discovery import BootstrapDiscoveryError, discover_bootstrap_candidates

    session = MagicMock()
    journey = SimpleNamespace(id="journey-r105", status="DRAFT", active_plan_version_id=None)
    session.scalar.return_value = None
    with pytest.raises(BootstrapDiscoveryError) as exc:
        discover_bootstrap_candidates(
            session,
            journey,
            member=SimpleNamespace(id="member-r105"),
            traveler_id="traveler-r105",
            query="art history",
            radius_m=2500,
            limit=3,
            source_kind="FOREGROUND_GPS",
            provider_consent=True,
            latitude=31.2304,
            longitude=121.4737,
            accuracy_m=15,
        )
    assert exc.value.code == "PUBLIC_CENTER_REQUIRED"
    session.add.assert_not_called()
    session.flush.assert_not_called()
