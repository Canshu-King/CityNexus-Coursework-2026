from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True)
class StepUpGrant:
    traveler_id: str
    method: str
    verified_at: datetime

    def valid_for(self, *, max_age_seconds: int) -> bool:
        age = (
            datetime.now(timezone.utc) - self.verified_at.astimezone(timezone.utc)
        ).total_seconds()
        return 0 <= age <= max_age_seconds


class StepUpAuthProvider(Protocol):
    provider_key: str

    def require_recent_grant(self, traveler_id: str, *, max_age_seconds: int) -> StepUpGrant: ...


# The concrete py_webauthn adapter should own challenge generation/verification
# and challenge persistence. It must not be a boolean "biometric succeeded" flag.
