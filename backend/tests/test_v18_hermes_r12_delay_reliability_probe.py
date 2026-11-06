from scripts.hermes_r12_delay_reliability_probe import (
    _delay_fixture_truth,
    _proposal_delay_truth,
    _read_only_delay_truth,
)


def test_r12_fixture_is_explicitly_synthetic_traffic_delay():
    assert _delay_fixture_truth({
        "event": {"event_type": "TRAFFIC_DELAY"},
        "seed_provenance": {"traffic_delay": "SYNTHETIC_HARNESS_FIXTURE"},
    }) is True
    assert _delay_fixture_truth({
        "event": {"event_type": "TRAFFIC_DELAY"},
        "seed_provenance": {"traffic_delay": "LIVE"},
    }) is False


def test_r12_proposal_arm_requires_persisted_full_contract():
    assert _proposal_delay_truth({
        "response_count": 1,
        "full_contract_observed": True,
        "all_server_proposal_ids_persisted": True,
    }, []) is True
    assert _proposal_delay_truth({
        "response_count": 1,
        "full_contract_observed": False,
        "all_server_proposal_ids_persisted": True,
    }, []) is False


def test_r12_read_only_arm_rejects_replan_or_entity_change():
    before = {"entities": {"proposals": [], "plans": ["same"], "events": ["delay"]}}
    after = {"entities": {"proposals": [], "plans": ["same"], "events": ["delay"]}}
    assert _read_only_delay_truth(before, after, {"response_count": 0}, []) is True
    assert _read_only_delay_truth(before, after, {"response_count": 1}, []) is False
    assert _read_only_delay_truth(before, {"entities": {"proposals": ["new"], "plans": ["same"], "events": ["delay"]}}, {"response_count": 0}, []) is False
