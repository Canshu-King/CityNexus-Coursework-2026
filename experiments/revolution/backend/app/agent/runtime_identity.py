"""Non-secret identity for the agent runtime exposed by CityNexus."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.agent.ai_availability import ai_availability_state
from app.integrations.hermes.native_client import native_hermes_status


HERMES_NATIVE_MCP = "HERMES_NATIVE_MCP"
NATIVE_DEEPSEEK_TOOL_LOOP = "NATIVE_DEEPSEEK_TOOL_LOOP"
HERMES_COMPAT_ENDPOINT = "HERMES_COMPAT_ENDPOINT"
NOT_CONFIGURED = "NOT_CONFIGURED"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _hermes_binary() -> str | None:
    configured = getattr(settings, "hermes_native_binary", None)
    if configured:
        return configured
    discovered = shutil.which("hermes")
    if discovered:
        return discovered
    for root in (os.getenv("LOCALAPPDATA"), os.getenv("USERPROFILE")):
        if not root:
            continue
        candidate = Path(root) / "hermes" / "hermes-agent" / ".venv" / "Scripts" / "hermes.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def runtime_identity() -> str:
    """Return the verified runtime identity without inspecting or exposing keys."""

    binary = _hermes_binary()
    native = native_hermes_status()
    # Runtime identity answers what is reachable NOW. Proof history remains a
    # separate audit field; a stale CITYNEXUS_HERMES_NATIVE_PROVEN flag must not
    # make a dead gateway look active, and a live verified gateway should not be
    # hidden merely because an old proof flag was omitted from the process.
    if binary and native.get("ready"):
        return HERMES_NATIVE_MCP
    if settings.hermes_api_url and settings.hermes_api_key:
        return HERMES_COMPAT_ENDPOINT
    if settings.ai_api_key:
        return NATIVE_DEEPSEEK_TOOL_LOOP
    return NOT_CONFIGURED


def runtime_status() -> dict[str, Any]:
    """Describe runtime provenance for UI and audits, never credential material."""

    binary = _hermes_binary()
    native_proven = bool(binary and _truthy(getattr(settings, "hermes_native_proven", None)))
    native = native_hermes_status()
    return {
        "runtime_identity": runtime_identity(),
        "native_binary_detected": bool(binary),
        "native_mcp_proven": native_proven,
        "mcp_server": "citynexus-v2",
        "deepseek_configured": bool(settings.ai_api_key),
        "hermes_compat_configured": bool(settings.hermes_api_url and settings.hermes_api_key),
        # ORD-0359: additive. `deepseek_configured` stays a boolean for the clients that already
        # read it; `ai_state` is the named reason, so a screen no longer has to infer "disabled"
        # from a flag that cannot tell an operator's switch from a missing credential.
        "ai_state": ai_availability_state(),
        "hermes_native_gateway": native,
    }
