"""Opt-in phase timings for expensive generation paths (ORD-0049).

Why this exists: a cold `POST /stay-options/generate` was measured at 114.98s returning an
empty `top_k`, and every explanation on the table was a guess about which layer owned the
wait - provider, geocoder, routing, solver, or the database. The arbiter's instruction was
explicit: get phase timings before naming a layer. This module is the only honest way to
get them without changing what a caller receives.

Design rules, each because of something that went wrong elsewhere in this project:

* **Off by default, and off means identical.** When the flag is unset, `phase()` is a
  no-op context manager and no trace object is created - so no allocation, no behaviour
  change, and nothing that could turn a measured response into an instrumented one. A
  regression asserts this, because "it only adds a header" is how contracts erode.
* **Outcome, not just duration.** Every span records how it ended (`ok`, `empty`,
  `timeout`, `error:Type`). A phase that took 40s and returned nothing is a different
  defect from one that took 40s and returned 15 rows, and the first is exactly the case
  that produced a silent empty result.
* **Nested spans keep their parent.** The report needs "provider 108s of 115s", which is
  only meaningful if children roll up.
* **Thread-local, not global.** The API runs requests in worker threads; a shared list
  would interleave two travellers' timings into one meaningless table.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class PhaseSpan:
    name: str
    ms: float
    outcome: str = "ok"
    detail: str = ""
    depth: int = 0


@dataclass
class PhaseTrace:
    spans: list[PhaseSpan] = field(default_factory=list)
    depth: int = 0

    def record(self, name: str, ms: float, outcome: str, detail: str = "", depth: int = 0) -> None:
        self.spans.append(
            PhaseSpan(
                name=name,
                ms=round(ms, 1),
                outcome=outcome,
                detail=detail,
                depth=depth,
            )
        )

    def total_ms(self) -> float:
        # Only top-level spans: a child is already inside its parent's duration, and
        # summing both would report the same wait twice.
        return round(sum(span.ms for span in self.spans if span.depth == 0), 1)

    def render(self) -> str:
        """One compact line, readable in a log and pasteable into a report."""

        parts = [
            f"{'  ' * span.depth}{span.name}={span.ms}ms:{span.outcome}" for span in self.spans
        ]
        return f"phase_timings total={self.total_ms()}ms | " + " | ".join(parts)

    def render_header(self) -> str:
        """The same table without the free text, so it can legally ride in a header.

        Details carry hotel names, which are Chinese; a header value must be ASCII, and
        truncating names to fit would be the kind of half-truth this file exists to avoid.
        """

        parts = [f"{span.name}={span.ms}:{span.outcome}" for span in self.spans]
        joined = ",".join(parts)
        limit = 1800
        return joined[:limit] + ("..." if len(joined) > limit else "")


_local = threading.local()


def timing_enabled() -> bool:
    return os.getenv("CITYNEXUS_PROVIDER_TIMING", "").strip().lower() in {"1", "true", "yes", "on"}


def begin_trace() -> PhaseTrace | None:
    """Start collecting for this thread. Returns None (and costs nothing) when disabled."""

    if not timing_enabled():
        return None
    trace = PhaseTrace()
    _local.trace = trace
    return trace


def current_trace() -> PhaseTrace | None:
    return getattr(_local, "trace", None)


def end_trace() -> PhaseTrace | None:
    trace = getattr(_local, "trace", None)
    _local.trace = None
    return trace


@dataclass
class SpanHandle:
    """What the instrumented body reports back: how it ended, and anything worth reading.

    `empty` versus `ok` is the distinction that matters most here - the incident was a
    provider that answered nothing after a long wait, and a span that only says "finished"
    cannot tell that apart from one that found twelve hotels.
    """

    outcome: str = "ok"
    detail: str = ""


@contextlib.contextmanager
def phase(name: str, *, detail: str = "") -> Iterator[SpanHandle | None]:
    """Time one segment, or cost nothing when timing is off.

    An exception closes the span as `error:Type` and keeps propagating: a measured failure
    must stay a failure, and the timing is the evidence, not the handling.
    """

    trace = getattr(_local, "trace", None)
    if trace is None:
        yield None
        return
    depth = trace.depth
    started = time.perf_counter()
    handle = SpanHandle(detail=detail)
    trace.depth += 1
    try:
        yield handle
    except Exception as exc:
        handle.outcome = f"error:{type(exc).__name__}"
        raise
    finally:
        trace.depth -= 1
        trace.record(
            name,
            (time.perf_counter() - started) * 1000.0,
            handle.outcome,
            handle.detail,
            depth=depth,
        )
