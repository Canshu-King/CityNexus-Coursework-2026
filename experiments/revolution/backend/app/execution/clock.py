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
        value = _frozen_now.get() or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if timezone_name:
            try:
                return value.astimezone(ZoneInfo(timezone_name))
            except (KeyError, ValueError):
                return value
        return value

    @contextmanager
    def freeze(self, value: datetime) -> Iterator[datetime]:
        token = _frozen_now.set(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
        try:
            yield self.now()
        finally:
            _frozen_now.reset(token)


execution_clock = ExecutionClock()
