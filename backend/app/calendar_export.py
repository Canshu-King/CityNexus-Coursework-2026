"""ORD-0496 slice 1 - a trip's own schedule as an RFC 5545 calendar.

This module holds the whole iCalendar grammar and none of the HTTP: it takes the values a row
already carries and returns the bytes of a `VCALENDAR`, plus the counts of what it did with them.
The door in `app/main.py` reads the rows and hands them over as `CalendarActivity` values, so the
two families of detail that this order is actually about -- escaping, folding, which instants count
as a clock -- have exactly one home and can be tested without a server.

Four decisions that a reader would otherwise have to guess:

1. **One activity is one `VEVENT`, and nothing here splits an interval that crosses midnight.**
   "一天一活动一个 VEVENT" is a day-per-activity reading, and splitting is what it would demand --
   but a split needs a start for the second half and an end for the first, and those two instants
   exist in no column.  They would be invented.  So an activity whose stored `end_at` lands on a
   later UTC day than its `start_at` stays the one event the row is, and the count of those rides
   in `X-CityNexus-Ics-Cross-Day` rather than being smoothed away.  The day structure a calendar
   app draws comes from the real interval; what this exporter promises is that it does not redraw
   the row.
2. **A usable clock means an instant with its own offset.**  `datetime.fromisoformat` is happy to
   read `2026-11-04` as midnight, and happy to read a naive `2026-11-04T09:00:00` as local wall
   time -- in this process that is whatever the machine's zone says.  Both readings would be this
   server deciding what a traveller meant, which is the thing the order forbids ("不许本地造时间").
   A row whose `start_at` or `end_at` has no offset, or does not parse, or is empty, is therefore
   skipped and *counted* (`X-CityNexus-Ics-Skipped-No-Clock`), never dropped.  Same rule for the
   `DTSTAMP` a `VEVENT` requires: it is the row's own `updated_at`, falling back to its
   `created_at`, and if neither is a usable clock the row is skipped and counted separately
   (`X-CityNexus-Ics-Skipped-No-Stamps`).  The request's wall clock is never used for either, which
   is also what makes two exports of an unchanged trip the same bytes.
3. **`DTSTART`/`DTEND` are emitted in the UTC form, so no `VTIMEZONE` is needed.**  The rows carry
   an explicit offset; converting to UTC preserves the instant, while re-labelling it with
   `journeys.timezone` would not -- that column says where the trip is, not what the row's offset
   meant.  A `TZID` would additionally oblige this build to emit a `VTIMEZONE` component it has no
   durable source for (RFC 5545 §3.6.5), so it is not emitted.
4. **The one place characters are removed is a leading or trailing plain space in a value.**
   RFC 5545 §3.1 forbids a `contentline` value from beginning or ending with a space or horizontal
   tab, and a fold cannot carry one either: §3.1's unfolding deletes the CRLF *and one following
   space*, so a line that ends at a space loses it.  Those characters are trimmed, and the number of
   trimmed values is reported (`X-CityNexus-Ics-Whitespace-Trimmed`) instead of being silently
   spent.  Folding itself therefore never breaks a line at a space, never splits a multi-octet
   UTF-8 character, and never splits an escaped pair like `\\,` -- see `_fold`.

Escaping is RFC 5545 §3.3.11: backslash first, then semicolon, comma, and newline as the literal
two-character `\\n` (not a raw CRLF).  Colon is only special in a parameter value, and this exporter
writes no parameters, so a colon stays verbatim -- an over-eager `\\:` here is a real interoperability
bug, so `unescape_text` and the round-trip arms in
`backend/tests/test_qoder_ord0496_calendar_export.py` pin which four characters move.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

#: RFC 5545 §3.1: a `contentline` MUST NOT be longer than 75 octets excluding the CRLF.
LINE_LIMIT_OCTETS = 75

#: RFC 5545 §3.1: every line ends with CRLF.  A file of bare LF is not an iCalendar object.
CRLF = "\r\n"

#: RFC 5545 §3.2: the media type, and the only one this door ever answers with.
ICS_MEDIA_TYPE = "text/calendar; charset=utf-8"

PRODID = "-//CityNexus//Journey Calendar Export 1.0//EN"
UID_DOMAIN = "journeys.citynexus"


@dataclass(frozen=True)
class CalendarActivity:
    """One activity row as the exporter sees it, every field verbatim from a durable column.

    The `..._at` fields stay strings on purpose: parsing them is this module's job and the one
    rule about what counts as a clock has to be readable in one place.  `stamp_at` is the row's
    own `updated_at` or `created_at` -- not the moment the export was asked for.
    """

    uid: str
    summary: str
    location: str | None
    description: str
    start_at: str | None
    end_at: str | None
    stamp_at: str | None


@dataclass
class CalendarDocument:
    """The bytes, and the arithmetic that says where every input row went."""

    body: bytes
    events: int = 0
    skipped_no_clock: int = 0
    skipped_no_stamp: int = 0
    cross_day_events: int = 0
    activities: int = 0
    trimmed_values: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "events": self.events,
            "skipped_no_clock": self.skipped_no_clock,
            "skipped_no_stamp": self.skipped_no_stamp,
            "cross_day_events": self.cross_day_events,
            "activities": self.activities,
            "trimmed_values": self.trimmed_values,
        }


@dataclass
class _Sink:
    """Line sink that keeps the trim count, so a caller cannot forget to report it."""

    lines: list[str] = field(default_factory=list)
    trimmed: int = 0

    def add(self, name: str, value: str) -> None:
        escaped = escape_text(value)
        trimmed = _trim_value(escaped)
        if trimmed != escaped:
            self.trimmed += 1
        self.lines.extend(_fold(f"{name}:{trimmed}"))

    def add_raw(self, line: str) -> None:
        self.lines.extend(_fold(line))


def escape_text(value: str) -> str:
    """RFC 5545 §3.3.11 TEXT escaping.  Backslash first, or it would escape its own output."""

    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def unescape_text(value: str) -> str:
    """The exact inverse, kept here so the round-trip arms measure against the same rule."""

    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in (",", ";", "\\"):
                out.append(nxt)
            else:
                # Not an escape this grammar knows: keep both characters rather than guess.
                out.append(char)
                out.append(nxt)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _trim_value(escaped: str) -> str:
    """Drop a leading/trailing plain space or tab, which §3.1 does not allow in a value.

    Operates on the escaped form: a `\\,` is never whitespace, so this cannot eat an escaped pair.
    """

    return escaped.strip(" \t")


def _tokens(escaped: str) -> list[str]:
    """Split into atomic pieces: an escape pair stays together, a character stays together.

    Folding is allowed between octets of a multi-octet character by no reading of §3.1, and a
    `\\` that ends a line leaves the escaped character orphaned on the next one.  Packing tokens
    instead of characters makes both shapes unreachable by construction.
    """

    pieces: list[str] = []
    index = 0
    while index < len(escaped):
        if escaped[index] == "\\" and index + 1 < len(escaped):
            pieces.append(escaped[index : index + 2])
            index += 2
        else:
            pieces.append(escaped[index])
            index += 1
    return pieces


def _octets(text: str) -> int:
    return len(text.encode("utf-8"))


def _fold(logical: str) -> list[str]:
    """One logical line to physical lines of at most 75 octets, continuations prefixed by a space.

    Three properties the receiver depends on, none of which a writer notices by testing an ASCII
    title: a physical line never exceeds 75 octets (measured in bytes, not characters); a
    continuation spends one of those octets on its leading space, so it carries 74 of content; and
    a line never *ends* with a space, because unfolding deletes CRLF plus one following space and
    would otherwise delete a character the value had.  Such a space therefore moves to the next
    physical line, where it sits *after* the mandatory continuation space and survives unfolding.
    No logical line this module writes begins with a space -- values are trimmed and a property
    name does not -- so a piece can never empty out under that migration.
    """

    tokens = _tokens(logical)
    pieces: list[str] = []
    current = ""
    current_octets = 0
    budget = LINE_LIMIT_OCTETS
    for token in tokens:
        size = _octets(token)
        if current and current_octets + size > budget:
            ended = current
            moved = ""
            while ended and (ended.endswith(" ") or ended.endswith("\t")):
                moved = ended[-1] + moved
                ended = ended[:-1]
            pieces.append(ended)
            current = moved
            current_octets = _octets(moved)
            budget = LINE_LIMIT_OCTETS - 1
        current += token
        current_octets += size
    pieces.append(current)
    return [pieces[0]] + [" " + piece for piece in pieces[1:]]


def _usable_clock(raw: str | None) -> datetime | None:
    """An instant this exporter may print: parseable, and carrying its own UTC offset."""

    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _stamp(date_time: datetime) -> str:
    return date_time.strftime("%Y%m%dT%H%M%SZ")


def _interval(start: datetime, end: datetime) -> tuple[str, str]:
    """The two UTC forms for one interval, ordered as the row has them.

    No clamping and no "end = start if the row says less": §3.6.1 permits `DTEND` equal to
    `DTSTART`, but the rows here are constrained `end_at > start_at`
    (`backend/db/schema.sql` `activities`), and silently repairing a stored pair would be this
    server editing the trip.  Whatever the pair says is what the file says.
    """

    return _stamp(start), _stamp(end)


def build_calendar(journey_title: str, activities: list[CalendarActivity]) -> CalendarDocument:
    """Render the object, and count every row into one of the named buckets.

    The arithmetic is the promise: `activities == events + skipped_no_clock + skipped_no_stamp`,
    which is what makes "nothing was dropped" checkable from the response headers alone.
    """

    sink = _Sink()
    sink.add_raw("BEGIN:VCALENDAR")
    sink.add_raw("VERSION:2.0")
    sink.add_raw(f"PRODID:{escape_text(PRODID)}")
    sink.add_raw("CALSCALE:GREGORIAN")
    sink.add_raw("METHOD:PUBLISH")
    sink.add("X-WR-CALNAME", journey_title)

    document = CalendarDocument(body=b"", activities=len(activities))
    for activity in activities:
        start = _usable_clock(activity.start_at)
        end = _usable_clock(activity.end_at)
        if start is None or end is None:
            document.skipped_no_clock += 1
            continue
        stamp = _usable_clock(activity.stamp_at)
        if stamp is None:
            document.skipped_no_stamp += 1
            continue
        document.events += 1
        if start.date() != end.date():
            document.cross_day_events += 1
        sink.add_raw("BEGIN:VEVENT")
        sink.add("UID", f"{activity.uid}@{UID_DOMAIN}")
        sink.add_raw(f"DTSTAMP:{_stamp(stamp)}")
        dt_start, dt_end = _interval(start, end)
        sink.add_raw(f"DTSTART:{dt_start}")
        sink.add_raw(f"DTEND:{dt_end}")
        sink.add("SUMMARY", activity.summary)
        if activity.location is not None:
            sink.add("LOCATION", activity.location)
        if activity.description:
            sink.add("DESCRIPTION", activity.description)
        sink.add_raw("END:VEVENT")

    sink.add_raw("END:VCALENDAR")
    document.trimmed_values = sink.trimmed
    document.body = (CRLF.join(sink.lines) + CRLF).encode("utf-8")
    return document


def content_disposition(title: str, journey_id: str) -> str:
    """The RFC 6266 / RFC 5987 pair a download needs, with no invented name.

    `filename=` stays pure ASCII and is deliberately generic: a title like 沿海一周 has no honest
    ASCII form, and transliterating one would be this server writing a name the traveller never
    had.  `filename*=UTF-8''...` carries the real title, percent-encoded to §4.1 of RFC 5987 so a
    quote or a slash in it cannot restructure the header.  A receiver that understands `filename*`
    shows the trip's name; one that does not gets `calendar.ics` rather than a mojibake guess.
    """

    safe = _ext_value(title, journey_id)
    return f'attachment; filename="calendar.ics"; filename*=UTF-8\'\'{safe}'


_EXT_VALUE_SAFE = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "!#$&+-.^_`|~"
)


def _ext_value(title: str, journey_id: str) -> str:
    """`<sanitised title>.ics` as a `setext`-free ext-value, with a durable id in the fallback.

    Control characters are dropped (they would be a header-injection surface, not a name) and the
    percent-encoding is applied to the UTF-8 octets of what survives.  The journey id is appended
    so two trips with the same title do not arrive as one indistinguishable file.
    """

    kept = "".join(
        char for char in title if char in _EXT_VALUE_SAFE or _octets(char) > 1 or char in " ."
    ).strip(" .\t")
    base = "".join(char if char in _EXT_VALUE_SAFE else _percent(char) for char in kept)
    suffix = "".join(char for char in journey_id if char.isalnum() or char == "-")
    stem = f"{base}--{suffix}" if base and suffix else (base or suffix or "calendar")
    return f"{stem}.ics"


def _percent(char: str) -> str:
    return "".join(f"%{octet:02X}" for octet in char.encode("utf-8"))
