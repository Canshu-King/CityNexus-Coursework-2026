from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.models import AuditLog


def record_audit(
    session: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    journey_id: str | None = None,
    before: object | None = None,
    after: object | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Persist a traceable, JSON-safe audit event for a state-changing operation."""
    session.add(
        AuditLog(
            id=str(uuid4()),
            actor_type=actor_type,
            actor_id=actor_id,
            journey_id=journey_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_json=json.dumps(before, ensure_ascii=False) if before is not None else None,
            after_json=json.dumps(after, ensure_ascii=False) if after is not None else None,
            trace_id=trace_id or str(uuid4()),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )
