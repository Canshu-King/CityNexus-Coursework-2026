from scripts.hermes_r13_weather_reliability_probe import (
    _proposal_weather_truth,
    _read_only_weather_truth,
    _weather_fixture_truth,
)


def test_r13_weather_fixture_is_simulated_and_not_provider_verified():
    seed = {
        "event": {
            "event_type": "WEATHER",
            "severity": "CRITICAL",
            "provenance": "SIMULATED",
            "provider_key": "demo-simulator",
            "payload": {"kind": "SYNTHETIC_WEATHER_FIXTURE"},
        },
        "weather_provenance_contract": {"live_provider_evidence": False},
    }
    assert _weather_fixture_truth(seed) is True
    seed["event"]["provenance"] = "PROVIDER_VERIFIED"
    assert _weather_fixture_truth(seed) is False


def test_r13_proposal_arm_requires_full_persisted_contract_without_mutation():
    assert _proposal_weather_truth({
        "response_count": 1,
        "full_contract_observed": True,
        "all_server_proposal_ids_persisted": True,
    }, []) is True
    assert _proposal_weather_truth({
        "response_count": 1,
        "full_contract_observed": False,
        "all_server_proposal_ids_persisted": True,
    }, []) is False
    assert _proposal_weather_truth({
        "response_count": 1,
        "full_contract_observed": True,
        "all_server_proposal_ids_persisted": True,
    }, ["accept_replan"]) is False


def test_r13_read_only_arm_rejects_replan_or_entity_change():
    before = {"entities": {"proposals": [], "plans": ["same"], "events": ["weather"]}}
    after = {"entities": {"proposals": [], "plans": ["same"], "events": ["weather"]}}
    assert _read_only_weather_truth(before, after, {"response_count": 0}, []) is True
    assert _read_only_weather_truth(before, after, {"response_count": 1}, []) is False
    assert _read_only_weather_truth(
        before,
        {"entities": {"proposals": ["new"], "plans": ["same"], "events": ["weather"]}},
        {"response_count": 0},
        [],
    ) is False
