"""ORD-0477 - the trip list answers by a named order, and refuses an order it does not have.

Measured at the authoritative tip `dcb90cccc68b81f2317b28db19051dedb9f06c80` (read from
`git ls-remote origin refs/heads/mirror/integration-frontend-apple-pass1`, the read-only mirror the
commander moves), `backend/app/main.py`'s `GET /api/v1/journeys` had one order and no way to ask for
another: `select(Journey).where(Journey.deleted_at.is_(None)).order_by(Journey.created_at, Journey.id)`.
The screen that lists trips therefore showed whatever order the store happened to keep, and a
traveller who wanted the next departure first had no door to ask at.

What this file pins, and why each of those is the shape rather than a preference:

* **the three tiers are one vocabulary in four artifacts** - `app.schemas.JOURNEY_SORT_TIERS`, the
  hand contract's `enum`, FastAPI's own `/openapi.json`, and the generated client's
  `operations["listJourneys"]`.  ORD-0387 was opened on exactly this drift and ORD-0395 on the
  half where only the hand contract knew about a refusal;
* **an unknown word is refused by name, and no list is answered**.  `sort` is read as a plain
  string here rather than declared as a `Literal` on purpose: with a `Literal`, pydantic answers
  `VALIDATION_ERROR` with nothing naming the tier that was wrong, which is the silent-default shape
  this order forbids.  `JOURNEY_SORT_NOT_RECOGNISED` names the word sent and the words supported,
  and it rides in `error.reason_code` under the door's existing `error.code`, so
  `test_qoder_ord0272_continuation.py`'s single-vocabulary boundary still holds;
* **a tier sorts instants, not characters**.  `journeys.start_at` is TEXT and this server stores
  whatever spelling it was handed: `+08:00`, `Z` and naive values coexist (arm
  `test_departure_date_...` shows the store's own strings, and the character order is the opposite
  of the instant order for that pair).  A naive stored value is read as UTC, which is a *stated*
  rule in `contracts/openapi.yaml`, judged here rather than trusted;
* **no tier changes which rows appear** - the `deleted_at IS NULL` rule is judged by set equality
  against the store under every tier, because the suite shares one database and a hand-typed total
  would be a number about whatever module ran before this one;
* **the tie-break is the same under the newest-first tier**: `journeys.id` ascending, which a
  descending SQL `ORDER BY ... DESC` would have quietly reversed.

The empty collection is split deliberately: an HTTP-level `[]` needs a store with no live row, and
this process shares its store with ninety other modules, so the door's empty answer is judged at
the helper (`test_an_empty_collection_orders_to_an_empty_collection`) and the *screen's* named empty
state is judged on a stack this order owns, in
`frontend/e2e/qoder-ord0477-journey-list-sort.spec.ts`.  No row is deleted from the shared store to
manufacture a zero.

Nothing here calls `POST /api/v1/demo/reset`, mocks a response, or skips an arm.  Every order claim
is read back from the door's own JSON and from the SQLite file the process opened (`conftest.RESOLVED_TEST_DATABASE`).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient

from app.db.models import Journey
from app.main import JOURNEY_SORT_REFUSAL, _journeys_in_tier_order, app
from app.schemas import JOURNEY_SORT_TIERS

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
GENERATED_CLIENT = ROOT / "frontend" / "src" / "generated" / "api.ts"
DOOR = "/api/v1/journeys"
RECENT, DEPARTURE, TITLE = JOURNEY_SORT_TIERS


def _store_path() -> Path:
    """The exact database file this pytest process opened - a resolved path, not a name."""

    from conftest import RESOLVED_TEST_DATABASE

    return Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()


def _traveler() -> str:
    created = client.post(
        "/api/v1/travelers",
        json={"display_name": f"ORD-0477 {uuid4()}", "locale": "zh-CN", "timezone": "Asia/Shanghai"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _journey(title: str, start: str, end: str) -> str:
    """One trip through the real creation door, so the row is a row the product writes."""

    created = client.post(
        DOOR,
        json={
            "traveler_id": _traveler(),
            "title": title,
            "city_code": "CN-TIANJIN",
            "timezone": "Asia/Shanghai",
            "start_at": start,
            "end_at": end,
            "budget": {"amount_minor": 90000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, f"{title} -> {created.status_code} {created.text}"
    return created.json()["id"]


def _door(sort: str | None = None) -> list[dict]:
    url = DOOR if sort is None else f"{DOOR}?sort={sort}"
    answer = client.get(url)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert isinstance(body, list), f"{url} answered {type(body).__name__}, not an array"
    return body


def _door_order(sort: str | None, ids: list[str]) -> list[str]:
    """The door's order restricted to `ids` - a total order restricted to a subset is still that
    subset's order, which is what makes a claim here independent of the shared store."""

    mine = set(ids)
    return [row["id"] for row in _door(sort) if row["id"] in mine]


def _stored(ids: list[str]) -> dict[str, sqlite3.Row]:
    if not ids:
        return {}
    with sqlite3.connect(_store_path()) as connection:
        connection.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(ids))
        rows = connection.execute(
            f"SELECT rowid, id, title, start_at, created_at, updated_at, deleted_at"
            f" FROM journeys WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    return {row["id"]: row for row in rows}


def _utc(value: str) -> datetime:
    """The test's own reading of the rule the contract states: instants, naive means UTC."""

    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


# ------------------------------------------------------------------ the door without a tier

def test_no_tier_keeps_the_order_this_door_shipped_before_tiers() -> None:
    """Omitting `sort` is not a fourth tier and not a refusal: the SELECT ORD-0383 slice 1 shipped."""

    ids = [
        _journey("zulu", "2026-11-09T09:00:00+08:00", "2026-11-10T09:00:00+08:00"),
        _journey("alpha", "2026-11-01T09:00:00+00:00", "2026-11-02T09:00:00+00:00"),
        _journey("mike", "2026-11-05T09:00:00", "2026-11-06T09:00:00"),
    ]
    stored = _stored(ids)
    expected = [
        row_id
        for row_id, row in sorted(
            stored.items(), key=lambda item: (item[1]["created_at"], item[1]["id"])
        )
    ]
    assert _door_order(None, ids) == expected, {
        "door": _door_order(None, ids),
        "expected_by_stored_created_at_then_id": expected,
        "stored_created_at": {i: stored[i]["created_at"] for i in ids},
    }
    # these three fixtures are the case where a tier is *not* the default: a tier that was wired to
    # the old order would pass the assertion above and fail here
    assert _door_order(TITLE, ids) != expected, "the TITLE tier returned the default order"
    assert _door_order(DEPARTURE, ids) != expected, "the DEPARTURE_DATE tier returned the default order"


# ------------------------------------------------------------------ RECENT_UPDATE

def test_recent_update_tier_puts_the_trip_that_was_just_edited_first() -> None:
    """`最近更新` reads `journeys.updated_at`, newest first, and a PATCH moves the row.

    The three trips leave the create door with `updated_at == created_at`, so creation order and
    update order coincide until something is edited.  Editing the *first* one is what separates the
    two: a tier that quietly re-read `created_at` would keep it last.
    """

    first = _journey("ORD-0477 first", "2026-11-09T09:00:00+08:00", "2026-11-10T09:00:00+08:00")
    second = _journey("ORD-0477 second", "2026-11-08T09:00:00+08:00", "2026-11-09T09:00:00+08:00")
    third = _journey("ORD-0477 third", "2026-11-07T09:00:00+08:00", "2026-11-08T09:00:00+08:00")
    ids = [first, second, third]

    assert _door_order(RECENT, ids)[:1] == [third], "a fresh store should read newest-created first"

    patched = client.patch(f"{DOOR}/{first}", json={"title": "ORD-0477 first, edited"})
    assert patched.status_code == 200, patched.text
    assert _door_order(RECENT, ids) == [first, third, second], {
        "door": _door_order(RECENT, ids),
        "stored_updated_at": {i: _stored([i])[i]["updated_at"] for i in ids},
        "why": "RECENT_UPDATE orders journeys.updated_at descending; the edited row is newest",
    }


# ------------------------------------------------------------------ DEPARTURE_DATE

def test_departure_date_tier_orders_by_instant_not_by_characters() -> None:
    """The crown arm: two rows whose characters order one way and whose instants the other.

    `+08:00` and `Z` rows coexist because `create_journey` stores `payload.start_at.isoformat()`
    unchanged.  The `+08:00` trip departs at 01:00 UTC and the `Z` trip at 08:00 UTC, so the
    departure order is the `+08:00` row first - while a byte comparison of the stored strings puts
    `...T08:00:00Z` before `...T09:00:00+08:00`, which is the opposite answer.
    """

    offset_row = _journey("ORD-0477 +08", "2026-11-03T09:00:00+08:00", "2026-11-04T09:00:00+08:00")
    utc_row = _journey("ORD-0477 +00", "2026-11-03T08:00:00+00:00", "2026-11-04T08:00:00+00:00")
    naive_row = _journey("ORD-0477 naive", "2026-11-05T09:00:00", "2026-11-06T09:00:00")
    ids = [offset_row, utc_row, naive_row]
    stored = {i: _stored([i])[i]["start_at"] for i in ids}

    assert _door_order(DEPARTURE, ids) == [offset_row, utc_row, naive_row], {
        "door": _door_order(DEPARTURE, ids),
        "stored_start_at": stored,
        "character_order_would_be": sorted(ids, key=lambda i: stored[i]),
        "instant_order_utc": sorted(ids, key=lambda i: _utc(stored[i])),
    }
    # the claim is only worth anything if the two readings genuinely disagree here
    assert sorted(ids, key=lambda i: stored[i]) != sorted(ids, key=lambda i: _utc(stored[i])), (
        "this arm lost its discriminating power: the stored strings now order the same way the "
        "instants do, so pin a different pair before trusting the assertion above"
    )


def test_a_naive_stored_departure_is_read_as_utc_the_rule_the_contract_states() -> None:
    """Naive means UTC - stated in `contracts/openapi.yaml`, and judged against the alternative.

    If a naive `start_at` were instead read as the trip's own `Asia/Shanghai` zone, 05:00 naive
    would become 2026-11-02T21:00Z and the naive row would come first.  It does not.
    """

    naive = _journey("ORD-0477 naive as UTC", "2026-11-03T05:00:00", "2026-11-04T05:00:00")
    utc = _journey("ORD-0477 explicit UTC", "2026-11-03T04:00:00+00:00", "2026-11-04T04:00:00+00:00")
    ids = [naive, utc]
    assert _stored([naive])[naive]["start_at"] == "2026-11-03T05:00:00", _stored([naive])[naive]
    assert _door_order(DEPARTURE, ids) == [utc, naive], {
        "door": _door_order(DEPARTURE, ids),
        "stored": {i: _stored([i])[i]["start_at"] for i in ids},
        "reading_if_naive_were_local": "the naive row would be 2026-11-02T21:00Z and would come first",
    }


# ------------------------------------------------------------------ TITLE

def test_title_tier_is_case_folded_code_point_order_and_names_what_it_is_not() -> None:
    """`名称` is case-folded Unicode code-point order - not pinyin, not stroke count, not locale.

    Two discriminations are pinned in one arm:
      * case-fold, because raw code points would put `Zoo camp` (U+005A) before `apple market`
        (U+0061) while a traveller reading a phone list expects `apple` first;
      * not pinyin, because 阿里 (U+963F ...) sorts *after* 北京 (U+5317 ...) on code points, which
        is the reverse of the pinyin order the same two words have in a Chinese address book.
    """

    ids = [
        _journey("Zoo camp", "2026-11-09T09:00:00+08:00", "2026-11-10T09:00:00+08:00"),
        _journey("apple market", "2026-11-09T09:00:00+08:00", "2026-11-10T09:00:00+08:00"),
        _journey("北京", "2026-11-09T09:00:00+08:00", "2026-11-10T09:00:00+08:00"),
        _journey("阿里", "2026-11-09T09:00:00+08:00", "2026-11-10T09:00:00+08:00"),
    ]
    by_code_point = sorted(ids, key=lambda i: _stored([i])[i]["title"])
    assert _door_order(TITLE, ids) == [ids[1], ids[0], ids[2], ids[3]], {
        "door": _door_order(TITLE, ids),
        "titles": {i: _stored([i])[i]["title"] for i in ids},
        "raw_code_point_order_would_be": by_code_point,
        "pinyin_order_would_put": "阿里 before 北京 - this tier does not",
    }
    assert by_code_point != _door_order(TITLE, ids), (
        "the case-fold arm lost its discrimination: raw code points and case-folded order now "
        "agree on this fixture, so change the fixture before trusting this file"
    )


# ------------------------------------------------------------------ the named refusal

def test_an_unknown_word_is_refused_by_name_and_no_list_is_answered() -> None:
    """422, one `error.code` the suite already has, one `reason_code` that names the word."""

    refused = client.get(f"{DOOR}?sort=by_mood")
    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert not isinstance(body, list), "the door answered a list for a word it does not know"
    error = body["error"]
    assert error["code"] == "VALIDATION_ERROR", error
    assert error["reason_code"] == JOURNEY_SORT_REFUSAL, error
    assert error["details"]["sort"] == "by_mood", error
    assert error["details"]["supported"] == list(JOURNEY_SORT_TIERS), error


def test_an_empty_sort_word_is_a_sent_word_and_is_refused_too() -> None:
    """`?sort=` is not `?sort` absent: an empty word names no tier, and is refused by name."""

    refused = client.get(f"{DOOR}?sort=")
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["reason_code"] == JOURNEY_SORT_REFUSAL, error
    assert error["details"]["sort"] == "", error


def test_a_tier_word_is_not_case_insensitive() -> None:
    """The tiers are words, not spellings: `recent_update` names no tier this server knows."""

    refused = client.get(f"{DOOR}?sort=recent_update")
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["reason_code"] == JOURNEY_SORT_REFUSAL, refused.text


# ------------------------------------------------------------------ the filter is untouched

def test_no_tier_adds_or_removes_a_row_and_the_bin_stays_out() -> None:
    """The `deleted_at IS NULL` rule is judged by set equality against the store, under every tier.

    This is the regression guard for moving the order out of SQL: a rewritten SELECT that dropped
    the WHERE clause would show up here as a tier answer containing the binned trip.
    """

    kept = _journey("ORD-0477 kept", "2026-11-09T09:00:00+08:00", "2026-11-10T09:00:00+08:00")
    binned = _journey("ORD-0477 binned", "2026-11-09T09:00:00+08:00", "2026-11-10T09:00:00+08:00")
    assert client.delete(f"{DOOR}/{binned}").status_code == 204

    with sqlite3.connect(_store_path()) as connection:
        store_live = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM journeys WHERE deleted_at IS NULL"
            ).fetchall()
        }
    assert binned not in store_live, "the store still calls a binned trip live"

    for sort in (None, *JOURNEY_SORT_TIERS):
        answered = {row["id"] for row in _door(sort)}
        assert answered == store_live, f"tier {sort} disagrees with the store on which rows are live"
        assert binned not in answered, f"tier {sort} listed a trip the recycle bin holds"
    # the binned row is still readable at its own door - the tier did not delete anything
    assert client.get(f"{DOOR}/{binned}").status_code == 200


def test_the_list_door_writes_nothing_under_any_tier() -> None:
    """A read route stays read-neutral: no audit row, no timestamp moved."""

    journey = _journey("ORD-0477 read neutral", "2026-11-09T09:00:00+08:00", "2026-11-10T09:00:00+08:00")
    with sqlite3.connect(_store_path()) as connection:
        before_audit = connection.execute("SELECT count(*) FROM audit_logs").fetchone()[0]
    before = _stored([journey])

    for sort in (None, *JOURNEY_SORT_TIERS, "by_mood"):
        client.get(DOOR if sort is None else f"{DOOR}?sort={sort}")

    after = _stored([journey])
    with sqlite3.connect(_store_path()) as connection:
        after_audit = connection.execute("SELECT count(*) FROM audit_logs").fetchone()[0]
    assert after_audit == before_audit, (before_audit, after_audit)
    assert dict(after.items()) == dict(before.items()), {
        "before": dict(before[journey]),
        "after": dict(after[journey]),
        "why": "GET /api/v1/journeys moved a stored value",
    }


# ------------------------------------------------------------------ the helper's stated rules

def test_an_empty_collection_orders_to_an_empty_collection() -> None:
    """Zero rows in, zero rows out, for every tier - no invented row, no error."""

    for tier in JOURNEY_SORT_TIERS:
        assert _journeys_in_tier_order([], tier) == [], tier


def test_the_tie_break_is_id_ascending_even_under_the_newest_first_tier() -> None:
    """Two trips sharing one instant are separated by `journeys.id`, ascending, in every tier.

    A SQL `ORDER BY updated_at DESC, id DESC` would have reversed this and made a tie read as an
    arbitrary - and unreproducible - order across two calls.
    """

    stamp = "2026-11-03T09:00:00+00:00"
    rows = [
        Journey(id="z-row", updated_at=stamp, start_at=stamp, title="same"),
        Journey(id="a-row", updated_at=stamp, start_at=stamp, title="same"),
    ]
    for tier in JOURNEY_SORT_TIERS:
        assert [row.id for row in _journeys_in_tier_order(rows, tier)] == ["a-row", "z-row"], tier


# ------------------------------------------------------------------ one vocabulary, four artifacts

def _contract_journeys_get() -> dict:
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return spec["paths"]["/journeys"]["get"]


def _runtime_journeys_get() -> dict:
    return client.get("/openapi.json").json()["paths"]["/api/v1/journeys"]["get"]


def test_the_tier_words_are_one_vocabulary_in_code_contract_and_generated_spec() -> None:
    contract = _contract_journeys_get()
    runtime = _runtime_journeys_get()
    contract_sort = next(p for p in contract["parameters"] if p["name"] == "sort")
    runtime_sort = next(p for p in runtime["parameters"] if p["name"] == "sort")
    assert set(contract_sort["schema"]["enum"]) == set(JOURNEY_SORT_TIERS), contract_sort
    assert set(runtime_sort["schema"]["enum"]) == set(JOURNEY_SORT_TIERS), runtime_sort
    assert contract_sort["required"] is False, contract_sort
    assert runtime_sort["required"] is False, runtime_sort


def test_the_generated_client_carries_the_tier_the_contract_declares() -> None:
    """`frontend/src/generated/api.ts` is generated, and a client that cannot send the tier is drift."""

    generated = GENERATED_CLIENT.read_text(encoding="utf-8")
    start = generated.index("listJourneys: {")
    block = generated[start : start + 4000]
    assert 'sort?: "RECENT_UPDATE" | "DEPARTURE_DATE" | "TITLE"' in block, block[:400]
    assert "query?: never" not in block.split("responses")[0], (
        "the generated client still advertises no query parameter for the door that takes one"
    )


def test_both_openapi_artifacts_advertise_the_refusal_the_door_actually_gives() -> None:
    """ORD-0395's shape: a hand contract that names a refusal the generated spec hides is a lie."""

    contract = _contract_journeys_get()
    runtime = _runtime_journeys_get()
    assert "422" in contract["responses"], sorted(contract["responses"])
    assert JOURNEY_SORT_REFUSAL in contract["responses"]["422"]["description"], contract["responses"]["422"]
    assert "422" in runtime["responses"], sorted(runtime["responses"])
    assert JOURNEY_SORT_REFUSAL in runtime["responses"]["422"]["description"], runtime["responses"]["422"]


def test_the_tier_descriptions_name_stored_columns_and_not_invented_ones() -> None:
    """Every tier's sentence points at a column the `journeys` table actually has."""

    with sqlite3.connect(_store_path()) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(journeys)").fetchall()}
    for column in ("updated_at", "start_at", "title", "id", "created_at", "deleted_at"):
        assert column in columns, sorted(columns)
    description = _contract_journeys_get()["parameters"][0]["description"]
    for column in ("journeys.updated_at", "journeys.start_at", "journeys.title", "journeys.id"):
        assert column in description, description
