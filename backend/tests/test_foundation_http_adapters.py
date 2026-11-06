import httpx
import pytest

from app.integrations.context.open_meteo import OpenMeteoContextProvider, OpenMeteoProviderError
from app.integrations.discovery.photon import PhotonGeocoder


@pytest.mark.asyncio
async def test_open_meteo_parser_and_risk_without_real_network():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": ["2026-10-10T14:00", "2026-10-10T15:00"],
                    "precipitation_probability": [20, 85],
                    "weather_code": [1, 95],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenMeteoContextProvider(client=client, base_url="https://weather.test")
        observations = await provider.forecast(latitude=39.1, longitude=117.2)
        risks = provider.risks(observations)
        assert len(observations) == 2
        assert [risk.code for risk in risks] == ["HEAVY_RAIN_RISK"]
        assert risks[0].evidence["precipitation_probability"] == 85


@pytest.mark.asyncio
async def test_open_meteo_rejects_timeout_and_malformed_hourly_payloads():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
        provider = OpenMeteoContextProvider(client=client, base_url="https://weather.test")
        try:
            await provider.forecast(latitude=39.1, longitude=117.2)
        except OpenMeteoProviderError as exc:
            assert "request failed" in str(exc)
        else:
            raise AssertionError("timeout must be a controlled provider error")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "hourly": {
                        "time": ["not-a-time"],
                        "precipitation_probability": [101],
                        "weather_code": [65],
                    }
                },
            )
        )
    ) as client:
        provider = OpenMeteoContextProvider(client=client, base_url="https://weather.test")
        try:
            await provider.forecast(latitude=39.1, longitude=117.2)
        except OpenMeteoProviderError as exc:
            assert "malformed" in str(exc)
        else:
            raise AssertionError("malformed hourly payload must be rejected")


@pytest.mark.asyncio
async def test_photon_parser_without_real_network():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "features": [
                    {
                        "geometry": {"coordinates": [117.2, 39.1]},
                        "properties": {
                            "osm_id": 123,
                            "osm_type": "N",
                            "name": "Nexus Museum",
                            "country": "China",
                            "city": "Tianjin",
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = PhotonGeocoder(client=client, base_url="https://photon.test")
        result = await provider.search("museum")
        assert result[0].external_id == "N:123"
        assert result[0].longitude == 117.2
