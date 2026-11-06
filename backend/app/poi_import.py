"""ORD-0437: the local text/link import parser for the POI entry chain.

The red line on this order is that extraction goes through our own channel: everything below is a
pure function over the text the traveller pasted plus rows we already hold.  Nothing here opens a
socket, and a link is therefore *not* followed -- the screen says so by name rather than going quiet,
which is the failure mode the order calls out ("不支持的形态要具名说不能而不静默失败").

Segmentation, form classification and resolution status are decided here so the panel and the gate can
read the same words; the route that calls this owns the database look-up, because only it knows which
city's places are fair game to match against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The one input form this channel can actually extract from.
FORM_PASTED_TEXT = "PASTED_TEXT"

# Forms the reference chain has, named here so the screen can say which one it hit and why it stops.
FORM_LINK_ONLY = "LINK_ONLY"                    # 只有链接：要抓取才有内容，我们不抓
FORM_SCREENSHOT = "SCREENSHOT_OR_IMAGE"          # 截图识别：本单不含 OCR
FORM_PASSWORD_CODE = "PASSWORD_CODE"             # 口令码：需要对方 App 内解析
FORM_EMPTY_INPUT = "EMPTY_INPUT"

STATUS_RESOLVED = "RESOLVED"        # exactly one of our own places matched
STATUS_AMBIGUOUS = "AMBIGUOUS"      # several matched; picking one silently would invent a place
STATUS_UNRESOLVED = "UNRESOLVED"    # nothing in our catalogue matched this name

_SEGMENT_SPLIT = re.compile(r"[\n；;、·•\r]+|\s{2,}")
_BULLET = re.compile(r"^\s*(?:[-*]|\d{1,2}[.)、]|第\s*\d+\s*[天日])[\s:：、]+")
_URL = re.compile(r"(?:https?://|www\.)[^\s，,；;、]+", re.IGNORECASE)
_HOST = re.compile(r"^(?:https?://)?(?:www\.)?([^/\s]+)", re.IGNORECASE)
_TRAILING = re.compile(r"[，,。.；;：:\s]+$")
_NOTE_SPLIT = re.compile(r"[（(]|[,，]\s*(?:地址|备注|note|address)")

# Domains from the reference chain's own hint text. Naming them is what lets the screen say which
# kind of link arrived, instead of one generic "cannot read this".
KNOWN_LINK_HOSTS: dict[str, str] = {
    "xhslink.com": FORM_LINK_ONLY,
    "xiaohongshu.com": FORM_LINK_ONLY,
    "mp.weixin.qq.com": FORM_LINK_ONLY,
    "feishu.cn": FORM_LINK_ONLY,
    "larkoffice.com": FORM_LINK_ONLY,
}


@dataclass(frozen=True)
class ExtractedItem:
    """One candidate the traveller might add: the text they wrote, and what we could do with it."""

    raw: str
    title: str
    note: str | None
    char_count: int
    forms: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        return f"{self.title}|{self.note or ''}"


@dataclass(frozen=True)
class ResolvedItem(ExtractedItem):
    """An `ExtractedItem` after the look-up: matched place rows, and whether that is addable."""

    status: str = STATUS_UNRESOLVED
    place_id: str | None = None
    place_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    coordinate_system: str | None = None
    matches: tuple[str, ...] = ()


def detect_forms(text: str) -> tuple[str, ...]:
    """Which input forms are present in what the traveller pasted.

    A link is reported even when the same blob also carries text: the screen has to say "the link
    part was not read" while still offering the text it did read.
    """
    if not text or not text.strip():
        return (FORM_EMPTY_INPUT,)
    forms: list[str] = []
    if _URL.search(text):
        forms.append(FORM_LINK_ONLY)
    if re.search(r"\.(?:png|jpe?g|webp|heic)\b|截图|screenshot", text, re.IGNORECASE):
        forms.append(FORM_SCREENSHOT)
    if re.search(r"[A-Za-z0-9]{6,}\s*(?:口令|code)\b|(?:口令|复制.*打开)\s*[A-Za-z0-9]{6,}", text):
        forms.append(FORM_PASSWORD_CODE)
    residual = _URL.sub("", text)
    if _segments(residual) or _segments(text):
        forms.append(FORM_PASTED_TEXT)
    return tuple(forms) or (FORM_PASTED_TEXT,)


def link_hosts(text: str) -> tuple[str, ...]:
    """The hosts present in the pasted text, in the order they appear."""
    found: list[str] = []
    for match in _URL.finditer(text or ""):
        host = _HOST.match(match.group(0))
        if host and host.group(1).lower() not in found:
            found.append(host.group(1).lower())
    return tuple(found)


def known_link_kinds(text: str) -> tuple[str, ...]:
    """Which of the reference chain's named link kinds this text carries."""
    kinds: list[str] = []
    for host in link_hosts(text):
        for known, kind in KNOWN_LINK_HOSTS.items():
            if host.endswith(known) and kind not in kinds:
                kinds.append(kind)
    return tuple(kinds)


def _segments(text: str) -> list[str]:
    out: list[str] = []
    for part in _SEGMENT_SPLIT.split(text or ""):
        cleaned = _BULLET.sub("", part or "").strip()
        cleaned = _TRAILING.sub("", cleaned)
        cleaned = _URL.sub("", cleaned).strip()
        cleaned = _TRAILING.sub("", cleaned)
        if len(cleaned) >= 2:
            out.append(cleaned)
    return out


def extract(text: str) -> list[ExtractedItem]:
    """Turn pasted text into the items a traveller could tick.

    The character count rides along because the reference shape puts `字数` next to the input box:
    what was recognised has to be attributable to how much was typed.
    """
    items: list[ExtractedItem] = []
    for segment in _segments(text):
        note_split = _NOTE_SPLIT.split(segment, maxsplit=1)
        title = (note_split[0] or segment).strip()
        note = None
        if len(note_split) > 1 and note_split[-1]:
            note = _TRAILING.sub("", note_split[-1].replace("）", "").replace(")", "")).strip() or None
        if not title:
            continue
        items.append(
            ExtractedItem(
                raw=segment,
                title=title,
                note=note,
                char_count=len(segment),
            )
        )
    return items


def resolve(items: list[ExtractedItem], catalogue: list[dict[str, object]]) -> list[ResolvedItem]:
    """Match each extracted title against rows we already hold.

    `catalogue` is a list of `{id, name, latitude, longitude, coordinate_system, aliases}`. Matching
    is exact-then-contained on the name and the aliases, case- and width-insensitive; more than one
    row surviving that is `AMBIGUOUS`, because choosing between them is the traveller's call and not
    something this function may guess.
    """
    normalised = [
        (row, str(row.get("name") or "").casefold().strip(), _aliases(row))
        for row in catalogue
    ]
    resolved: list[ResolvedItem] = []
    for item in items:
        needle = item.title.casefold().strip()
        hits = [
            row
            for row, name, aliases in normalised
            if name
            and (name == needle or needle in name or name in needle
                 or any(needle == a or needle in a or a in needle for a in aliases))
        ]
        if len(hits) == 1:
            row = hits[0]
            resolved.append(
                ResolvedItem(
                    raw=item.raw,
                    title=item.title,
                    note=item.note,
                    char_count=item.char_count,
                    forms=item.forms,
                    status=STATUS_RESOLVED,
                    place_id=str(row.get("id") or "") or None,
                    place_name=str(row.get("name") or "") or None,
                    latitude=row.get("latitude"),          # type: ignore[arg-type]
                    longitude=row.get("longitude"),        # type: ignore[arg-type]
                    coordinate_system=row.get("coordinate_system"),  # type: ignore[arg-type]
                    matches=(str(row.get("name") or ""),),
                )
            )
        elif len(hits) > 1:
            resolved.append(
                ResolvedItem(
                    raw=item.raw,
                    title=item.title,
                    note=item.note,
                    char_count=item.char_count,
                    forms=item.forms,
                    status=STATUS_AMBIGUOUS,
                    matches=tuple(sorted({str(h.get("name") or "") for h in hits})),
                )
            )
        else:
            resolved.append(
                ResolvedItem(
                    raw=item.raw,
                    title=item.title,
                    note=item.note,
                    char_count=item.char_count,
                    forms=item.forms,
                    status=STATUS_UNRESOLVED,
                )
            )
    return resolved


def _aliases(row: dict[str, object]) -> tuple[str, ...]:
    raw = row.get("aliases") or row.get("tags_json") or ()
    if isinstance(raw, str):
        try:
            import json as _json

            raw = _json.loads(raw)
        except ValueError:
            raw = ()
    if isinstance(raw, dict):
        raw = tuple(raw.keys())
    return tuple(str(a).casefold().strip() for a in (raw or ()) if str(a).strip())


def counts(resolved: list[ResolvedItem]) -> dict[str, int]:
    """The tallies the count line and the empty state both read from."""
    return {
        "extracted": len(resolved),
        "resolved": sum(1 for r in resolved if r.status == STATUS_RESOLVED),
        "ambiguous": sum(1 for r in resolved if r.status == STATUS_AMBIGUOUS),
        "unresolved": sum(1 for r in resolved if r.status == STATUS_UNRESOLVED),
        "addable": sum(1 for r in resolved if r.status == STATUS_RESOLVED),
    }
