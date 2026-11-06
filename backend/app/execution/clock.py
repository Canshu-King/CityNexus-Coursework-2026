"""Authoritative, freezeable execution clock shared by Journey surfaces."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

_frozen_now: ContextVar[datetime | None] = ContextVar("citynexus_execution_now", default=None)


class ExecutionClock:
    def now(self, timezone_name: str | None = None) -> datetime:
        return self.now_honest(timezone_name)[0]

    def now_honest(self, timezone_name: str | None = None) -> tuple[datetime, str | None, bool]:
        """The clock AND which zone it actually landed in, and whether that zone is the one asked for.

        `now()` used to fall back to UTC in silence when the zone could not be used, so a payload
        could carry `now` in UTC beside a `timezone` naming somewhere else entirely -- the two fields
        disagreeing inside one response, with the moment placed nowhere the reader can check.  The
        screen half of this domain already says "this device's time" in that case; this is the
        service half saying which zone it used, so a caller can name the moment instead of guessing.
        """
        value = _frozen_now.get() or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if not timezone_name:
            return value, None, False
        try:
            zone = ZoneInfo(timezone_name)
        except (KeyError, ValueError):
            return value, "UTC", False
        return value.astimezone(zone), timezone_name, True

    @contextmanager
    def freeze(self, value: datetime) -> Iterator[datetime]:
        token = _frozen_now.set(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
        try:
            yield self.now()
        finally:
            _frozen_now.reset(token)


execution_clock = ExecutionClock()
