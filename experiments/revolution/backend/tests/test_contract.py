from __future__ import annotations

from pathlib import Path

import yaml

from app.integrations.mock.providers import MockMobilityProvider
from app.main import app

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml"


def test_cnx_c_001_openapi_parses_and_wp01_wp03_routes_exist():
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert spec["openapi"] == "3.1.0"
    routes = {
        (route.path, method) for route in app.routes for method in getattr(route, "methods", set())
    }
    expected = {
        ("/api/v1/health", "GET"),
        ("/api/v1/demo/reset", "POST"),
        ("/api/v1/travelers", "POST"),
        ("/api/v1/travelers/{traveler_id}", "GET"),
        ("/api/v1/travelers/{traveler_id}/preferences", "GET"),
        ("/api/v1/travelers/{traveler_id}/preferences", "PUT"),
        ("/api/v1/journeys", "POST"),
        ("/api/v1/journeys/{journey_id}", "GET"),
        ("/api/v1/journeys/{journey_id}", "PATCH"),
        ("/api/v1/journeys/{journey_id}/constraints", "GET"),
        ("/api/v1/journeys/{journey_id}/constraints", "POST"),
        ("/api/v1/journeys/{journey_id}/constraints/{constraint_id}", "DELETE"),
        ("/api/v1/places", "GET"),
        ("/api/v1/city-services", "GET"),
        ("/api/v1/journeys/{journey_id}/plans/generate", "POST"),
        ("/api/v1/journeys/{journey_id}/plans", "GET"),
        ("/api/v1/plans/{plan_version_id}", "GET"),
        ("/api/v1/journeys/{journey_id}/plans/{plan_version_id}/select", "POST"),
    }
    assert expected <= routes
    assert all(
        item[1].lower() in spec["paths"][item[0].removeprefix("/api/v1") or "/"]
        for item in expected
    )


def test_cnx_c_005_mock_mobility_is_deterministic():
    provider = MockMobilityProvider()
    assert provider.estimate_minutes("place-food-hall", "place-tech-art") == 20
    assert provider.estimate_minutes("place-food-hall", "place-tech-art") == 20


def test_weather_refresh_contract_is_server_owned_and_bodyless():
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    path = spec["paths"]["/journeys/{journey_id}/weather/refresh"]
    operation = path["post"]
    runtime_paths = {
        (route.path, method) for route in app.routes for method in getattr(route, "methods", set())
    }

    assert ("/api/v1/journeys/{journey_id}/weather/refresh", "POST") in runtime_paths
    assert operation["operationId"] == "refreshJourneyWeather"
    assert "requestBody" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/WeatherRuntimeRefresh"
    )


def test_openapi_runtime_method_parity_includes_v7_surface():
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    runtime = {
        (route.path, method.lower())
        for route in app.routes
        if route.path.startswith("/api/v1")
        for method in getattr(route, "methods", set())
    }
    contract = {
        ("/api/v1" + path, method)
        for path, item in spec["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete", "options", "head"}
    }
    assert runtime == contract
