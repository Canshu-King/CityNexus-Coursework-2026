"""Untrusted public two-stop share parsing for Revolution R101.

The parser deliberately returns public OSM identities only.  It never preserves
the pasted text and never treats sender supplied names, provenance, timing or
route claims as evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


MAX_SHARED_WALK_TEXT = 12_000
_URL_TOKEN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_OBJECT_PATH = re.compile(r"^/(node|way|relation)/([1-9][0-9]*)$")
_SENSITIVE_MARKER = re.compile(
    r"(?:bearer|access[\s_-]*token|refresh[\s_-]*token|authorization|journey[\s_-]*id|"
    r"member[\s_-]*id|receipt|foreground[\s_-]*gps|\bgps\b|secret|password|cookie|"
    r"private[\s_-]*(?:token|key|credential))",
    re.IGNORECASE,
)
_CONTROL_OR_HTML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f<>]")


@dataclass(frozen=True)
class SharedWalkObject:
    osm_type: str
    osm_id: str
    source_url: str


class SharedWalkParseError(ValueError):
    """Stable, screen-safe parser error with no reflection of pasted text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_osm_object_url(osm_type: str, osm_id: str) -> str:
    kind = str(osm_type or "").strip().lower()
    identifier = str(osm_id or "").strip()
    if kind not in {"node", "way", "relation"} or not re.fullmatch(r"[1-9][0-9]*", identifier):
        raise SharedWalkParseError("INVALID_OSM_IDENTITY", "the shared walk contains an invalid public OSM identity")
    return f"https://www.openstreetmap.org/{kind}/{identifier}"


def normalize_osm_object_url(raw: str) -> SharedWalkObject:
    """Validate and canonicalize one HTTPS OSM object URL."""

    value = str(raw or "").strip()
    try:
        parsed = urlsplit(value)
        # Accessing .port intentionally rejects malformed numeric ports too.
        port = parsed.port
    except ValueError as exc:
        raise SharedWalkParseError("INVALID_SHARED_URL", "the shared walk contains an invalid OSM source link") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or host not in {"www.openstreetmap.org", "openstreetmap.org"}
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SharedWalkParseError("INVALID_SHARED_URL", "shared OSM source links must be canonical HTTPS object links")
    match = _OBJECT_PATH.fullmatch(parsed.path)
    if not match:
        raise SharedWalkParseError("INVALID_OSM_IDENTITY", "shared OSM source links must name one node, way or relation")
    osm_type, osm_id = match.groups()
    return SharedWalkObject(osm_type=osm_type, osm_id=osm_id, source_url=canonical_osm_object_url(osm_type, osm_id))


def validate_shared_walk_objects(objects: list[dict[str, object]]) -> list[SharedWalkObject]:
    """Validate the already parsed identities sent by the browser."""

    if len(objects) != 2:
        raise SharedWalkParseError("EXACTLY_TWO_OBJECTS_REQUIRED", "a shared walk must contain exactly two public OSM objects")
    result: list[SharedWalkObject] = []
    seen: set[tuple[str, str]] = set()
    for raw in objects:
        if not isinstance(raw, dict):
            raise SharedWalkParseError("INVALID_SHARED_OBJECT", "shared walk identities are invalid")
        source = normalize_osm_object_url(str(raw.get("source_url") or ""))
        supplied_type = str(raw.get("osm_type") or "").strip().lower()
        supplied_id = str(raw.get("osm_id") or "").strip()
        if (supplied_type, supplied_id) != (source.osm_type, source.osm_id):
            raise SharedWalkParseError("OSM_IDENTITY_MISMATCH", "the OSM identity and source link do not match")
        key = (source.osm_type, source.osm_id)
        if key in seen:
            raise SharedWalkParseError("DUPLICATE_OSM_OBJECT", "a shared walk requires two distinct public OSM objects")
        seen.add(key)
        result.append(source)
    return result


def parse_shared_walk_text(value: str) -> list[SharedWalkObject]:
    """Extract exactly two canonical public OSM object links from untrusted text."""

    text = str(value or "")
    if not text or len(text) > MAX_SHARED_WALK_TEXT:
        raise SharedWalkParseError("SHARED_TEXT_TOO_LARGE", "the shared walk text is empty or too large")
    if _CONTROL_OR_HTML.search(text):
        raise SharedWalkParseError("UNSAFE_SHARED_TEXT", "the shared walk contains unsafe control or HTML characters")
    if _SENSITIVE_MARKER.search(text) or re.search(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}", text):
        raise SharedWalkParseError("PRIVATE_DATA_IN_SHARED_TEXT", "private credentials or member data cannot be imported")
    tokens = _URL_TOKEN.findall(text)
    if len(tokens) != 2:
        raise SharedWalkParseError("EXACTLY_TWO_OSM_LINKS_REQUIRED", "paste a public walk containing exactly two OSM object links")
    parsed: list[SharedWalkObject] = []
    for token in tokens:
        # Sender text commonly places a sentence-ending full stop after a URL;
        # delimiters are not part of the URL, while query/fragment characters are.
        candidate = token.rstrip(".,;:!?)]}")
        parsed.append(normalize_osm_object_url(candidate))
    return validate_shared_walk_objects([item.__dict__ for item in parsed])
