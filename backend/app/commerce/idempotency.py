from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class IdempotencyConflict(ValueError):
    pass


def canonical_request_signature(payload: dict[str, Any]) -> str:
    """Return stable canonical JSON for semantic request comparison.

    No cryptographic/file hash is needed. The existing DB column name
    `request_hash` can store this canonical signature during the MVP.
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class StoredResult:
    signature: str
    response: dict[str, Any]


class MemoryIdempotencyRegistry:
    def __init__(self) -> None:
        self._results: dict[str, StoredResult] = {}

    def resolve(self, scoped_key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        stored = self._results.get(scoped_key)
        if stored is None:
            return None
        signature = canonical_request_signature(payload)
        if stored.signature != signature:
            raise IdempotencyConflict("same idempotency key used for a different request")
        return dict(stored.response)

    def store(self, scoped_key: str, payload: dict[str, Any], response: dict[str, Any]) -> None:
        signature = canonical_request_signature(payload)
        existing = self._results.get(scoped_key)
        if existing is not None and existing.signature != signature:
            raise IdempotencyConflict("same idempotency key used for a different request")
        self._results[scoped_key] = StoredResult(signature, dict(response))
