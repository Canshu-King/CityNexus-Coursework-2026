"""ORD-0526 · the offline catalogue sources this server registers, and what "registered" costs.

`GET/POST /api/v1/catalog-hydrations` used to answer `NO_REGISTERED_SOURCE` on every empty city
because the worker's PERSISTING stage only *counts*: `backend/app/catalog_hydration_operations.py`
surveys `places` and `real_place_candidates`, finds nothing, and says so.  The comment above that
branch claimed "in this build the registered sources are already applied" - which is true of the
provider legs (they apply themselves when a traveller asks through `/real-city/discover`) and empty
of any source that could answer *without* leaving the box.

So this file is that missing thing: a named, checked, offline source list.

Checked, not assumed.  A source is registered only while all of these hold, and the answer to each
question is reported so a reader can see why a source is or is not in effect:

  * the file is inside the repository (a path component may never escape the tree);
  * it parses as a JSON object carrying `candidates`, each with `external_id`, `name`, `category`
    and numeric coordinates;
  * the `city_code` written *inside* the file equals the `city_code` the entry is registered under -
    a file about one city can never be credited to another city's hydration;
  * the candidate rows keep the `provider_key` their own capture leg gives them
    (`openstreetmap-replay`), which is what makes them read back as an offline snapshot and never
    as a provider answer.

Nothing here invents a name: the rows come out of the product's own captured snapshot reader
(`_captured_replay_candidates` in `integrations/discovery/real_city.py`).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The kind the capability vocabulary already reserves for exactly this: material the build holds
#: offline.  `SOURCE_KINDS` in `anywhere_capability.py` is the list of what a reader may see.
OFFLINE_SNAPSHOT_KIND = "OFFLINE_SNAPSHOT"
DISABLE_ENV = "CITYNEXUS_CATALOG_OFFLINE_SOURCES"

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._/-]+$")


@dataclass(frozen=True)
class OfflineCatalogSource:
    """One built-in snapshot registered as a catalogue source for one city."""

    city_code: str
    relative_path: str
    provider_key: str
    source_kind: str = OFFLINE_SNAPSHOT_KIND

    @property
    def path(self) -> Path:
        return REPO_ROOT / self.relative_path


#: The registry.  Deliberately a literal table, so `registered_sources()` is a fact a reader can
#: compare with the file on disk rather than something computed from configuration nobody can see.
OFFLINE_CATALOG_SOURCES: tuple[OfflineCatalogSource, ...] = (
    OfflineCatalogSource(
        city_code="CN-TIANJIN",
        relative_path="data/replay/tianjin_real_snapshot.json",
        provider_key="openstreetmap-replay",
    ),
)


def disabled_by_environment() -> bool:
    """The kill switch, named: this is how the mutant proves absence is still absence."""

    return (os.getenv(DISABLE_ENV) or "").strip().lower() in {"off", "none", "disabled", "0"}


def registered_sources() -> tuple[OfflineCatalogSource, ...]:
    return () if disabled_by_environment() else OFFLINE_CATALOG_SOURCES


def source_for(city_code: str | None) -> OfflineCatalogSource | None:
    if not city_code:
        return None
    for source in registered_sources():
        if source.city_code == city_code:
            return source
    return None


def inspect_source(source: OfflineCatalogSource) -> dict[str, Any]:
    """Whether this entry is in effect *right now*, with the reason it is not when it is not.

    The check is what keeps a registered line in a table from becoming a claim about data: a file
    that moved, stopped parsing, or belongs to another city reports as not in effect, and the
    hydration outcome then stays the honest zero it was before this file existed.
    """

    facts: dict[str, Any] = {
        "city_code": source.city_code,
        "path": source.relative_path,
        "provider_key": source.provider_key,
        "source_kind": source.source_kind,
        "in_effect": False,
        "candidate_count": 0,
        "bytes": 0,
    }
    if not _SAFE_SEGMENT.match(source.relative_path) or ".." in Path(source.relative_path).parts:
        facts["reason"] = "SOURCE_PATH_OUTSIDE_TREE"
        return facts
    path = source.path
    if not path.is_file():
        facts["reason"] = "SOURCE_FILE_MISSING"
        return facts
    raw = path.read_bytes()
    facts["bytes"] = len(raw)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        facts["reason"] = "SOURCE_UNREADABLE"
        return facts
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        facts["reason"] = "SOURCE_HAS_NO_CANDIDATES"
        return facts
    stated = str(payload.get("city_code") or "").strip().upper()
    wanted = source.city_code.strip().upper()
    if stated != wanted:
        # Not equal as strings.  The only way this still counts is if the build itself already
        # treats the two as one city - asked of `real_city`, never of a table written here.
        from app.integrations.discovery.real_city import _is_tianjin_replay_city

        same_city = _is_tianjin_replay_city(stated) and _is_tianjin_replay_city(wanted)
        facts["city_code_inside_file"] = stated or None
        facts["city_equivalence_predicate"] = (
            "real_city._is_tianjin_replay_city" if same_city else None
        )
        if not same_city:
            # The mis-credit this order exists to prevent, stopped where it starts.
            facts["reason"] = "SOURCE_CITY_MISMATCH"
            return facts
    usable = [
        row
        for row in payload["candidates"]
        if isinstance(row, dict)
        and str(row.get("external_id") or "")
        and str(row.get("name") or "")
        and isinstance(row.get("latitude"), (int, float))
        and isinstance(row.get("longitude"), (int, float))
    ]
    facts["candidate_count"] = len(usable)
    if not usable:
        facts["reason"] = "SOURCE_CANDIDATES_UNUSABLE"
        return facts
    facts["in_effect"] = True
    facts["reason"] = None
    facts["snapshot_captured_at"] = payload.get("captured_at")
    facts["city_name"] = payload.get("city_name")
    return facts


def snapshot_payload(source: OfflineCatalogSource) -> dict[str, Any] | None:
    """The parsed snapshot, or `None` when `inspect_source` says the entry is not in effect."""

    if not inspect_source(source)["in_effect"]:
        return None
    return json.loads(source.path.read_bytes().decode("utf-8"))


def registry_facts() -> list[dict[str, Any]]:
    """Every source this process actually registers, in effect or not.

    The environment switch is honoured here too: a reader of the job record must see the same set
    the worker could act on, otherwise a disabled registry would still print its entries and a
    mutant run would look like it had sources available.
    """

    return [inspect_source(source) for source in registered_sources()]
