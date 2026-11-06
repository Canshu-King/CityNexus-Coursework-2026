"""ORD-0478: the route-ownership gate for the ORD-0471 family.

`test_contract.py` judges whether a path is registered.  ORD-0471 sat red for a full night with
every path registered: a stranded `@app.post` line rebound `/trip-studio/preview` to
`poi_import_parse`, so the door existed, answered, and answered with the wrong body schema.
Existence cannot see that shape, so this gate judges the other half of the pair -- which handler
a registered path actually resolves to.

The judge is `app.main:app`'s own route table, read cell by cell.  Nothing here opens a socket,
writes a row, or imports a fixture: it is the same object the server would dispatch on.

Two mutations are what make the assertions worth anything, and each kills a different half:
removing the stacked alias decorator makes the preview cell vanish from the table, and
re-pointing that decorator at the POI door keeps the cell present with the wrong owner.
"""

from __future__ import annotations

from app.main import app

PREFIX = "/api/v1/journeys/{journey_id}/"

# Every cell this family owns, and the single handler that must answer it.  The two
# `plans/*-reorder` paths are aliases of their `trip-studio/*` siblings by declaration
# (`contracts/openapi.yaml` `$ref`s to `contracts/openapi.yaml`), which is why
# both halves name one handler rather than two.
OWNERSHIP: dict[tuple[str, str], str] = {
    ("POST", f"{PREFIX}trip-studio/preview"): "trip_studio_preview",
    ("POST", f"{PREFIX}plans/preview-reorder"): "trip_studio_preview",
    ("POST", f"{PREFIX}trip-studio/accept"): "trip_studio_accept",
    ("POST", f"{PREFIX}plans/accept-reorder"): "trip_studio_accept",
    ("POST", f"{PREFIX}poi-import/parse"): "poi_import_parse",
    ("POST", f"{PREFIX}poi-import/add"): "poi_import_add",
}

ALIAS_PAIRS: tuple[tuple[str, str], ...] = (
    ("trip-studio/preview", "plans/preview-reorder"),
    ("trip-studio/accept", "plans/accept-reorder"),
)

# Substrings that mean "this path belongs to the family under judgement".  A new door wearing
# one of them has to be listed in OWNERSHIP, so it cannot join unwatched.
FAMILY_MARKERS: tuple[str, ...] = ("trip-studio", "poi-import", "preview-reorder", "accept-reorder")

# The two handler families must never answer for each other; this is the ORD-0471 shape stated
# as a positive rather than as a diff against last night.
_STUDIO_HANDLERS = frozenset({"trip_studio_preview", "trip_studio_accept"})
_IMPORT_HANDLERS = frozenset({"poi_import_parse", "poi_import_add"})


def _route_table() -> dict[tuple[str, str], list[str]]:
    """`(method, path) -> [handler names]`, in registration order.

    A key with two entries is a shadowed registration: Starlette dispatches the first match, so
    the second handler is dead code the reading would otherwise silently average over.
    """
    table: dict[tuple[str, str], list[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str) or not path.startswith("/api/v1"):
            continue
        methods = getattr(route, "methods", None) or set()
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            table.setdefault((method, path), []).append(getattr(route, "endpoint", None).__name__)
    return table


def test_ord0478_every_owned_cell_is_registered_exactly_once() -> None:
    table = _route_table()
    doubled = {key: names for key, names in table.items() if len(names) > 1}
    assert not doubled, f"shadowed registrations (first match wins, the rest are dead): {doubled}"
    missing = [key for key in OWNERSHIP if key not in table]
    assert not missing, (
        f"these cells left the route table entirely; a stacked decorator was most likely "
        f"stranded from its def: {missing}"
    )


def test_ord0478_each_cell_resolves_to_the_named_handler() -> None:
    table = _route_table()
    wrong = [
        (key, table.get(key), OWNERSHIP[key])
        for key in OWNERSHIP
        if table.get(key) != [OWNERSHIP[key]]
    ]
    assert not wrong, "path resolved to the wrong owner, which is what a schema never explains: " + "; ".join(
        f"{method} {path} -> {got!r}, wanted {wanted!r}" for (method, path), got, wanted in wrong
    )


def test_ord0478_each_alias_pair_shares_one_handler() -> None:
    """The invariant ORD-0471 broke: an alias must reach the same function, not merely exist."""
    table = _route_table()
    split = []
    for primary, alias in ALIAS_PAIRS:
        first = table.get(("POST", f"{PREFIX}{primary}"), [])
        second = table.get(("POST", f"{PREFIX}{alias}"), [])
        if first != second or len(first) != 1:
            split.append((primary, first, alias, second))
    assert not split, "alias pair split across handlers: " + "; ".join(
        f"{p} -> {a!r} but {q} -> {b!r}" for p, a, q, b in split
    )


def test_ord0478_the_two_door_families_never_answer_for_each_other() -> None:
    table = _route_table()
    crossed = []
    for (method, path), names in table.items():
        if not any(marker in path for marker in FAMILY_MARKERS):
            continue
        owners = set(names)
        if any(marker in path for marker in ("trip-studio", "preview-reorder", "accept-reorder")):
            if owners & _IMPORT_HANDLERS:
                crossed.append((method, path, names))
        elif "poi-import" in path and owners & _STUDIO_HANDLERS:
            crossed.append((method, path, names))
    assert not crossed, (
        "a POI door is answering a trip-studio path or the reverse; the body schema of the "
        f"wrong door then rejects every real caller: {crossed}"
    )


def test_ord0478_no_unlisted_route_joins_the_family() -> None:
    table = _route_table()
    family_cells = {(method, path) for (method, path) in table
                    if any(marker in path for marker in FAMILY_MARKERS)}
    unwatched = sorted(family_cells - set(OWNERSHIP))
    assert not unwatched, (
        "these paths wear a family name but carry no declared owner, so this gate would not "
        f"notice them changing hands: {unwatched}"
    )
