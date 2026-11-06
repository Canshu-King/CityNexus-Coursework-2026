from __future__ import annotations

import hmac
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Journey


@dataclass(frozen=True)
class AgentPrincipal:
    """The server-resolved identity used for agent audit and idempotency."""

    actor_id: str
    auth_method: str


def _token_from_headers(request: Request) -> str | None:
    explicit = request.headers.get("X-CityNexus-Agent-Token")
    if explicit:
        return explicit
    authorization = request.headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    return None


def _is_local_request(request: Request) -> bool:
    host = request.client.host if request.client else None
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def resolve_agent_principal(
    request: Request,
    session: Session,
    *,
    journey_id: UUID | None,
) -> AgentPrincipal:
    """Resolve a scoped, server-owned principal for an Agent HTTP call.

    Production-like environments require a configured bearer/token credential.
    Local development keeps a loopback-only process principal so the offline
    course demo remains usable without copying a secret into the repository.
    The caller's ``agent_id`` is intentionally not an input to this function.
    """

    configured = settings.agent_token
    supplied = _token_from_headers(request)
    if configured is not None:
        if supplied is None or not hmac.compare_digest(supplied, configured):
            raise HTTPException(401, "valid CityNexus Agent credentials are required")
        auth_method = "configured_token"
    else:
        if settings.environment not in {"development", "test"} or not _is_local_request(request):
            raise HTTPException(503, "CityNexus Agent principal is not configured")
        auth_method = "local_process_principal"

    if journey_id is not None and settings.agent_allowed_traveler_ids:
        journey = session.get(Journey, str(journey_id))
        if journey is None:
            raise HTTPException(404, f"Journey {journey_id} was not found")
        if journey.traveler_id not in settings.agent_allowed_traveler_ids:
            raise HTTPException(403, "Agent principal is not scoped to this Journey")

    return AgentPrincipal(actor_id=settings.agent_principal_id, auth_method=auth_method)
