from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import JourneyMember, JourneyRoleCapability, Traveler


_TOKEN_VERSION = 1
_TOKEN_AUDIENCE = "citynexus-traveler"


@dataclass(frozen=True)
class TravelerPrincipal:
    traveler_id: str
    auth_method: str
    expires_at: int


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _secret() -> bytes:
    configured = settings.traveler_auth_secret
    if not configured:
        raise HTTPException(503, "CityNexus traveler principal verifier is not configured")
    if len(configured.encode("utf-8")) < 32:
        raise HTTPException(503, "CityNexus traveler principal secret is too short")
    return configured.encode("utf-8")


def issue_traveler_token(
    traveler_id: str,
    *,
    ttl_seconds: int | None = None,
    now: int | None = None,
) -> str:
    """Issue a signed short-lived traveler token.

    This is deliberately a server-side helper, not a public "give me any
    traveler token" endpoint.  A production login/identity provider can call
    the same signer after authenticating the person; local R28 validation may
    mint tokens inside a trusted test/operator process.
    """

    clock = int(time.time() if now is None else now)
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.traveler_auth_ttl_seconds)
    if ttl <= 0 or ttl > 86_400:
        raise ValueError("traveler token ttl must be between 1 and 86400 seconds")
    payload = {
        "v": _TOKEN_VERSION,
        "aud": _TOKEN_AUDIENCE,
        "sub": str(traveler_id),
        "iat": clock,
        "exp": clock + ttl,
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64encode(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    explicit = request.headers.get("X-CityNexus-Traveler-Token")
    return explicit.strip() if explicit and explicit.strip() else None


def resolve_traveler_principal(
    request: Request,
    session: Session,
    *,
    now: int | None = None,
) -> TravelerPrincipal:
    token = _bearer_token(request)
    if token is None:
        raise HTTPException(401, "authenticated traveler credentials are required")
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(401, "traveler credentials are invalid") from exc
    expected = _b64encode(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "traveler credentials are invalid")
    try:
        payload: dict[str, Any] = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(401, "traveler credentials are invalid") from exc
    clock = int(time.time() if now is None else now)
    if payload.get("v") != _TOKEN_VERSION or payload.get("aud") != _TOKEN_AUDIENCE:
        raise HTTPException(401, "traveler credentials are invalid")
    traveler_id = str(payload.get("sub") or "")
    try:
        expires_at = int(payload.get("exp"))
        issued_at = int(payload.get("iat"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(401, "traveler credentials are invalid") from exc
    if not traveler_id or issued_at > clock + 60 or expires_at <= clock:
        raise HTTPException(401, "traveler credentials are expired or invalid")
    if session.get(Traveler, traveler_id) is None:
        raise HTTPException(401, "traveler principal no longer exists")
    return TravelerPrincipal(
        traveler_id=traveler_id,
        auth_method="SIGNED_HMAC_BEARER",
        expires_at=expires_at,
    )


def _member_is_in_force(member: JourneyMember, *, now_epoch: int | None = None) -> bool:
    if member.removed_at:
        return False
    if not member.expires_at:
        return True
    from datetime import datetime, timezone

    try:
        expires = datetime.fromisoformat(member.expires_at)
    except ValueError:
        return False
    if expires.tzinfo is None:
        return False
    clock = datetime.fromtimestamp(
        int(time.time() if now_epoch is None else now_epoch), tz=timezone.utc
    )
    return expires.astimezone(timezone.utc) > clock


def resolve_journey_member_principal(
    request: Request,
    session: Session,
    *,
    journey_id: str,
) -> tuple[TravelerPrincipal, JourneyMember]:
    principal = resolve_traveler_principal(request, session)
    member = session.scalar(
        select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == principal.traveler_id,
        )
    )
    if member is None:
        raise HTTPException(403, "authenticated traveler is not a member of this Journey")
    if not _member_is_in_force(member):
        raise HTTPException(403, "authenticated Journey member is not in force")
    return principal, member


def member_has_capability(
    session: Session,
    member: JourneyMember,
    capability: str,
) -> bool:
    return (
        session.scalar(
            select(JourneyRoleCapability).where(
                JourneyRoleCapability.role == member.role,
                JourneyRoleCapability.capability == capability,
            )
        )
        is not None
    )
