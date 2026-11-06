from __future__ import annotations

import os
from typing import Any

from sqlalchemy.orm import Session

from app.core.audit import record_audit


def phoenix_status() -> str:
    return "CONFIGURED" if os.getenv("CITYNEXUS_PHOENIX_ENDPOINT") else "NOT_CONFIGURED"


def persist_span(
    session: Session,
    *,
    journey_id: str,
    trace_id: str,
    name: str,
    attributes: dict[str, Any],
    status: str = "OK",
) -> dict[str, Any]:
    """Persist an OpenInference-shaped span without making Phoenix mandatory."""

    span = {
        "trace_id": trace_id,
        "name": name,
        "status": status,
        "attributes": attributes,
        "exporter": "PHOENIX_OPTIONAL",
    }
    record_audit(
        session,
        action="trace.span",
        entity_type="Journey",
        entity_id=journey_id,
        journey_id=journey_id,
        actor_type="SYSTEM",
        actor_id="citynexus-observability",
        after=span,
        trace_id=trace_id,
    )
    return span
