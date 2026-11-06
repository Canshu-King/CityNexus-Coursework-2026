"""Optional Web Push delivery with an explicit record-only fallback."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.config import settings


@dataclass(frozen=True)
class PushDeliveryResult:
    status: str
    detail: str


def push_capability() -> str:
    """Return a non-secret capability label for diagnostics and UI."""

    if not settings.vapid_private_key or not settings.vapid_public_key:
        return "RECORD_ONLY"
    try:
        import pywebpush  # noqa: F401
    except ImportError:
        return "DEPENDENCY_MISSING"
    return "WEB_PUSH_CONFIGURED"


def deliver_web_push(subscription: dict[str, Any], payload: dict[str, Any]) -> PushDeliveryResult:
    if push_capability() != "WEB_PUSH_CONFIGURED":
        return PushDeliveryResult("RECORD_ONLY", "web push is not configured")
    try:
        from pywebpush import webpush

        webpush(
            subscription_info=subscription,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},
            ttl=60,
        )
    except Exception:  # noqa: BLE001 - external push clients expose provider-specific failures
        return PushDeliveryResult("FAILED_EXTERNAL", "web push provider rejected delivery")
    return PushDeliveryResult("DELIVERED", "web push accepted")
