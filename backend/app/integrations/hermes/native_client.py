"""Small server-to-server client for the official Hermes Runs API."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterator

import httpx

from app.core.config import settings


@dataclass(frozen=True)
class HermesCapabilities:
    reachable: bool
    supports_runs: bool
    supports_events: bool
    supports_approval: bool
    payload: dict[str, Any]
    supports_stop: bool = False

    @property
    def ready(self) -> bool:
        return self.reachable and self.supports_runs and self.supports_events


@dataclass(frozen=True)
class HermesRunRef:
    run_id: str
    status: str


class HermesNativeClient:
    """Use only the configured loopback Hermes API; never expose its bearer key."""

    def __init__(self, *, client: httpx.Client | None = None):
        base_url = getattr(settings, "hermes_native_api_url", None)
        if not base_url or not getattr(settings, "hermes_api_server_key", None):
            raise ValueError("native Hermes API is not configured")
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=settings.impact_timeout_seconds)
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {getattr(settings, 'hermes_api_server_key', '')}"}

    def capabilities(self) -> HermesCapabilities:
        try:
            response = self._client.get(f"{self.base_url}/v1/capabilities", headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return HermesCapabilities(False, False, False, False, {})
        endpoints = payload.get("endpoints") if isinstance(payload, dict) else {}
        features = payload.get("features") if isinstance(payload, dict) else {}
        supports_runs = bool(features.get("run_submission")) or "runs" in features
        supports_events = bool(features.get("run_events_sse")) or bool(
            isinstance(endpoints, dict)
            and isinstance(endpoints.get("run_events"), dict)
        )
        supports_approval = bool(features.get("run_approval")) or bool(features.get("run_approval_response")) or bool(
            isinstance(endpoints, dict)
            and isinstance(endpoints.get("run_approval"), dict)
        )
        supports_stop = bool(features.get("run_stop")) or bool(
            isinstance(endpoints, dict) and isinstance(endpoints.get("run_stop"), dict)
        )
        return HermesCapabilities(True, supports_runs, supports_events, supports_approval, payload, supports_stop)

    def create_run(
        self,
        *,
        mission_id: str,
        journey_id: str,
        input_text: str,
        instructions: str,
        autonomy: str,
        mission_plan: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> HermesRunRef:
        payload: dict[str, Any] = {
            "input": input_text,
            "instructions": instructions,
            # Product runs pass a mission-scoped session so Hermes cannot
            # replay a prior proposal. The legacy client contract keeps its
            # journey-scoped default for compatibility tests/callers.
            "session_id": session_id or f"citynexus-{journey_id}",
            "metadata": {
                "citynexus_mission_id": mission_id,
                "citynexus_journey_id": journey_id,
                "autonomy": autonomy,
                "tool_surface": "citynexus-mcp-only",
            },
        }
        native_provider = getattr(settings, "hermes_native_provider", None)
        native_model = getattr(settings, "hermes_native_model", None)
        if native_provider:
            payload["provider"] = native_provider
        if native_model:
            payload["model"] = native_model
        if mission_plan:
            # The plan is an execution envelope, not model reasoning.  Keep it
            # in metadata so the gateway and the audit trail can reconcile the
            # requested safe sequence after reconnect.
            payload["metadata"]["mission_plan"] = mission_plan
        response = self._client.post(
            f"{self.base_url}/v1/runs",
            headers={
                **self._headers(),
                "Idempotency-Key": f"citynexus-{mission_id}",
                "X-Hermes-Session-Key": (
                    f"citynexus:journey:{journey_id}:mission:{mission_id}"
                    if session_id
                    else f"citynexus:journey:{journey_id}"
                ),
            },
            json=payload,
        )
        response.raise_for_status()
        payload = response.json()
        run_id = str(payload.get("run_id") or payload.get("id") or "")
        if not run_id:
            raise ValueError("Hermes run response did not contain a run id")
        return HermesRunRef(run_id, str(payload.get("status") or "queued"))

    def get_run(self, run_id: str) -> dict[str, Any]:
        response = self._client.get(f"{self.base_url}/v1/runs/{run_id}", headers=self._headers())
        response.raise_for_status()
        return response.json()

    def iter_events(self, run_id: str) -> Iterator[dict[str, Any]]:
        with self._client.stream(
            # A run event stream may legitimately remain quiet while Hermes
            # waits on a provider or deterministic CityNexus check.  Do not
            # apply the short impact-request timeout to this long-lived SSE
            # connection; lifecycle polling still has its normal timeout.
            "GET", f"{self.base_url}/v1/runs/{run_id}/events", headers=self._headers(), timeout=None
        ) as response:
            response.raise_for_status()
            data_lines: list[str] = []
            event_name: str | None = None
            for line in response.iter_lines():
                if not line:
                    if data_lines:
                        payload = json.loads("\n".join(data_lines))
                        if isinstance(payload, dict) and event_name:
                            payload.setdefault("event_type", event_name)
                        yield payload
                        data_lines.clear()
                        event_name = None
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip() or None
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())

    def approve(self, run_id: str, choice: str) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/runs/{run_id}/approval",
            headers=self._headers(),
            json={"choice": choice},
        )
        response.raise_for_status()
        return response.json()

    def stop(self, run_id: str) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/runs/{run_id}/stop", headers=self._headers(), json={}
        )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def native_hermes_status() -> dict[str, Any]:
    configured = bool(
        getattr(settings, "hermes_native_api_url", None)
        and getattr(settings, "hermes_api_server_key", None)
    )
    if not configured:
        return {
            "configured": False,
            "reachable": False,
            "runs": False,
            "events": False,
            "approval": False,
            "stop": False,
            "ready": False,
            "profile": getattr(settings, "hermes_native_profile", "citynexus"),
            "provider": getattr(settings, "hermes_native_provider", None),
            "model": getattr(settings, "hermes_native_model", None),
        }
    client = HermesNativeClient()
    try:
        capabilities = client.capabilities()
        return {
            "configured": True,
            "reachable": capabilities.reachable,
            "runs": capabilities.supports_runs,
            "events": capabilities.supports_events,
            "approval": capabilities.supports_approval,
            "stop": capabilities.supports_stop,
            "ready": capabilities.ready,
            "profile": getattr(settings, "hermes_native_profile", "citynexus"),
            "provider": getattr(settings, "hermes_native_provider", None),
            "model": getattr(settings, "hermes_native_model", None),
        }
    finally:
        client.close()
