from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.agent.runtime_events import normalize_hermes_event
from app.core.config import settings
from app.integrations.hermes import native_client


def test_unconfigured_native_status_has_stable_ready_false(monkeypatch):
    monkeypatch.setattr(
        native_client,
        "settings",
        replace(settings, hermes_native_api_url=None, hermes_api_server_key=None),
    )

    status = native_client.native_hermes_status()

    assert status["configured"] is False
    assert status["ready"] is False
    assert status["stop"] is False


def test_proposal_created_is_not_approval_required_without_explicit_approval():
    event = normalize_hermes_event(
        {
            "event_type": "proposal.created",
            "run_id": "run-proposal-only",
            "source": "HERMES_NATIVE",
            "status": "completed",
            "result": {"proposal_id": "proposal-1", "status": "PROPOSAL_ONLY"},
        }
    )

    assert event is not None
    assert event["event_type"] == "proposal.created"
    assert event["status"] == "completed"
    assert event["approval_status"] == "PROPOSAL_ONLY"


def test_unknown_runtime_event_is_safe_and_preserves_tool_identity():
    event = normalize_hermes_event(
        {
            "event_type": "future.lifecycle.signal",
            "run_id": "run-future",
            "tool_name": "mcp__citynexus__inspect_journey",
            "status": "started",
        }
    )

    assert event is not None
    assert event["event_type"] == "tool.started"
    assert event["tool_name"] == "inspect_journey"


def test_release_metadata_and_map_entrypoint_are_converged():
    root = Path(__file__).parents[2]
    manifest = (root / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8")
    current = (root / "CURRENT_STATUS.md").read_text(encoding="utf-8")
    assert "CityNexus-V18.3-Hermes-Experience-Convergence-20260915.zip" in manifest
    assert "V18_3_CONVERGENCE_NATIVE_HERMES_PROVEN" in manifest
    assert "native model run not configured" not in current.lower()
    map_panel = (root / "frontend/src/components/v8/MapPanel.tsx").read_text(encoding="utf-8")
    main = (root / "frontend/src/main.tsx").read_text(encoding="utf-8")
    assert 'import("maplibre-gl")' in map_panel
    assert 'from "maplibre-gl"' not in main
