"""ORD-0332: a rejection the traveller can read back to us.

`error.trace_id` (ORD-0055) answers "which envelope did the server just build" -- it is minted inside
`error()` and is different for every response, so it cannot survive being spoken over the phone, and a
retry of the very same failing call produces a different one.  A *receipt* has the opposite
requirement: it identifies **that call's that failure**, so resending the same request has to give the
same string.  Both are emitted, and they are never the same field.

The id is derived, not stored: `CNX-` + 8 characters of a SHA-256 over the request's own identity
(method, path, sorted query, and the caller's `Idempotency-Key` when it sent one) plus the rejection
`code`.  Nothing about the clock, the process, or the worker enters the seed, which is what makes
"同一请求重发要相等" a property of the recipe rather than a hope.

Absence is an answer: with no request context -- a job, a script, a unit call straight into `error()`
-- the field is *not written at all*.  An empty string would be a number-shaped lie, and the screen
would have nothing to refuse.  `contracts/ERROR_RECEIPT_ID_V1.md` is the document that fixes the name.
"""
from __future__ import annotations

import hashlib
import re
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlencode

#: The single fixed field name, and the single public spelling of it.  Nothing else may carry
#: a receipt (ORD-0332 ①); `contracts/ERROR_RECEIPT_ID_V1.md` is what pins the wire name.
RECEIPT_FIELD = "error_receipt_id"
PREFIX = "CNX-"
DIGEST_CHARS = 8
#: Crockford-style: no I/L/O/U/0/1, so a traveller reading it aloud cannot produce two different
#: spellings.  The alphabet, not the length, is what makes it recitable.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
RECEIPT_SHAPE = re.compile(rf"^{re.escape(PREFIX)}[{ALPHABET}]{{{DIGEST_CHARS}}}$")


@dataclass(frozen=True)
class ReceiptSeed:
    """The parts of a request that a retry reproduces exactly."""

    method: str
    path: str
    query: str
    idempotency_key: str | None


_CURRENT: ContextVar[ReceiptSeed | None] = ContextVar("citynexus_error_receipt_seed", default=None)


def canonical_query(items) -> str:
    """Sorted, so a client that reorders its query string is still the same call.

    `multi_items` keeps repeated parameters (a filter listed twice is not the same request as one),
    and nothing here hashes the body: a POST that carries an `Idempotency-Key` is scoped by that key,
    and one that does not is scoped by its route -- documented, not guessed at.
    """

    return urlencode(sorted((str(k), str(v)) for k, v in items))


def seed_for_request(request) -> ReceiptSeed:
    return ReceiptSeed(
        method=str(request.method).upper(),
        path=request.url.path,
        query=canonical_query(request.query_params.multi_items()),
        idempotency_key=request.headers.get("idempotency-key"),
    )


def arm_receipt_seed(request) -> ReceiptSeed:
    """Called by the HTTP middleware -- never by a route."""

    seed = seed_for_request(request)
    _CURRENT.set(seed)
    return seed


def clear_receipt_seed() -> None:
    _CURRENT.set(None)


def current_receipt_seed() -> ReceiptSeed | None:
    return _CURRENT.get()


def receipt_id_for(seed: ReceiptSeed | None, code: str) -> str | None:
    """The receipt for this call's this rejection, or None when there is no call to be a receipt for.

    `code` is inside the digest on purpose: the same request failing twice with two different reasons
    is two different failures, and one number for both would blur them.
    """

    if seed is None:
        return None
    key = "\0".join((seed.method, seed.path, seed.query, seed.idempotency_key or "", code))
    raw = hashlib.sha256(key.encode("utf-8")).digest()
    # 256 is a multiple of len(ALPHABET), so one byte per character is uniform -- reading hex digits
    # instead would have silently used only half the alphabet.
    body = "".join(ALPHABET[b % len(ALPHABET)] for b in raw[:DIGEST_CHARS])
    return f"{PREFIX}{body}"


def receipt_for_code(code: str) -> str | None:
    """`error()`'s one call: fold in whatever request this is, or write nothing."""

    return receipt_id_for(_CURRENT.get(), code)


def is_receipt_shape(value: object) -> bool:
    return isinstance(value, str) and bool(RECEIPT_SHAPE.fullmatch(value))
