from app.integrations.provider_status import ProviderState
from app.integrations.registry import ProviderRegistry


class Provider:
    def __init__(self, key):
        self.provider_key = key


def test_registry_prefers_ready_real_but_keeps_mock_fallback():
    registry = ProviderRegistry()
    mock = Provider("mock-mobility")
    real = Provider("valhalla")
    registry.register("mobility", mock, priority=100)
    registry.register("mobility", real, priority=10)
    assert registry.resolve("mobility") is real


def test_registry_skips_unavailable_provider():
    registry = ProviderRegistry()
    mock = Provider("mock-mobility")
    real = Provider("valhalla")
    registry.register("mobility", mock, priority=100)
    registry.register("mobility", real, priority=10, state=ProviderState.UNAVAILABLE)
    assert registry.resolve("mobility") is mock
