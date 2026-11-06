"""ORD-0355 - an undeclared stay budget must stop being persisted as a declared zero.

Measured at the authoritative tip `38277c4e9787b1ddf0bf69487060ba0e40c14146`: a trip POSTed
without the `stay_budget` key reached `journey_trip_contexts.stay_budget_amount_minor` as `0`,
that `0` went out inside the trip body a supplier reads, and an empty answer came back blamed
on the traveller's own money.  Absence had been fabricated into a quantity and then attributed
to the person.

These tests pin the four requirements and refuse the shortcuts that would pass them vacuously:

* R1 is judged by reading `journey_trip_contexts` back with raw SQL - naming table, column and
  the row it looked at - because a value that only survives inside the request object proves
  nothing about what the product stored;
* R2 asserts on the trip body the code actually builds, for both shapes, and additionally for
  a legacy row whose state is unknown;
* R3 asserts the reported code is not one whose sentence on the traveller's screen names
  money, and that the reasons the planner genuinely gave survive beside it - a disclosure of
  the missing budget is not licence to invent it as the cause;
* R4 runs the additive migration against a database built with the *old* DDL and checks that
  the pre-existing row and column are still there afterwards.

A declared `0` appears throughout as its own case: the fix must not make a real zero look like
a silence, or the bug simply changes direction.
"""

from __future__ import annotations

import sqlite3
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.models import Journey, JourneyTripContext, Traveler
from app.db.session import SessionLocal, ensure_schema, make_engine
from app.main import app
from app.stay_mobility import experience_payload, generate_stay_options


def stay_budget_body(context):  # noqa: N802 - mirrors the production symbol name
    """Resolved lazily so the R1/R3/R4 readings still run on an unmigrated tree.

    A module-level import of this name would turn the before-fix run into one collection
    error, which proves nothing about which of the four requirements was unmet.
    """

    from app import stay_mobility as module

    return module.stay_budget_body(context)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 11, 3, 9, 0, tzinfo=TZ)

#: The answers whose sentence on the Today screen names 预算 / budget as a constraint the trip
#: failed.  `V8Experience.tsx`'s `stayOutcomeLine` is the single place that wording lives, and
#: an undeclared budget must never be routed to one of these codes.
MONEY_NAMING_OUTCOMES = {
    "ALL_CANDIDATES_REJECTED",
    "BUDGET_MAX_EXCEEDED",
    "BUDGET_EXCEEDED",
}


def _traveler() -> str:
    traveler_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="ORD-0355 probe",
                email=None,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return traveler_id


def _journey(*, stay_budget: dict | None) -> str:
    """One trip through the real door, with or without the `stay_budget` key at all."""

    body = {
        "traveler_id": _traveler(),
        "title": "ORD-0355",
        "city_code": "CN-XIAMEN",
        "timezone": "Asia/Shanghai",
        "start_at": START.isoformat(),
        "end_at": (START + timedelta(days=2, hours=8)).isoformat(),
        "budget": {"amount_minor": 90000, "currency": "CNY"},
        "planning_mode": "balanced",
    }
    if stay_budget is not None:
        body["stay_budget"] = stay_budget
    created = client.post("/api/v1/journeys", json=body)
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _stored_trip_context(journey_id: str) -> dict:
    """Read the row back with plain SQLite: no ORM default can dress this up.

    Table `journey_trip_contexts`, columns `stay_budget_declared` and
    `stay_budget_amount_minor`, keyed by `journey_id` = the trip the request created.
    """

    from conftest import RESOLVED_TEST_DATABASE

    path = Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1))
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(journey_trip_contexts)").fetchall()
        ]
        assert "stay_budget_declared" in columns, (
            f"journey_trip_contexts stores {columns}: nothing in the row records whether a"
            " lodging budget was declared"
        )
        row = connection.execute(
            "SELECT stay_budget_declared, stay_budget_amount_minor, stay_budget_currency"
            " FROM journey_trip_contexts WHERE journey_id = ?",
            (journey_id,),
        ).fetchone()
    assert row is not None, f"no journey_trip_contexts row for journey_id={journey_id}"
    return dict(row)


# ------------------------------------------------------------------------ R1 persistence


def test_a_trip_posted_without_a_stay_budget_stores_not_declared() -> None:
    journey_id = _journey(stay_budget=None)

    stored = _stored_trip_context(journey_id)

    assert stored["stay_budget_declared"] == 0, stored
    # The amount column keeps its 0 - it is NOT NULL and rewriting it in place is out of
    # scope - so the *only* thing that distinguishes this row is the flag above.
    assert stored["stay_budget_amount_minor"] == 0, stored


def test_a_declared_zero_is_stored_differently_from_a_trip_that_said_nothing() -> None:
    """The required negative case: a real 0 must not become an absence.

    Both trips end up with `stay_budget_amount_minor == 0`.  Before ORD-0355 they were the
    same row; now the one where the traveller actually typed zero declares itself as such.
    """

    declared_zero = _journey(stay_budget={"amount_minor": 0, "currency": "CNY"})
    never_asked = _journey(stay_budget=None)

    first = _stored_trip_context(declared_zero)
    second = _stored_trip_context(never_asked)

    assert first["stay_budget_amount_minor"] == 0, first
    assert second["stay_budget_amount_minor"] == 0, second
    assert first["stay_budget_declared"] == 1, first
    assert second["stay_budget_declared"] == 0, second
    assert first["stay_budget_declared"] != second["stay_budget_declared"], (first, second)


def test_a_declared_amount_above_zero_is_stored_with_its_amount_and_its_flag() -> None:
    journey_id = _journey(stay_budget={"amount_minor": 30000, "currency": "CNY"})

    stored = _stored_trip_context(journey_id)

    assert stored["stay_budget_declared"] == 1, stored
    assert stored["stay_budget_amount_minor"] == 30000, stored


# ------------------------------------------------------------------- R2 the outbound body


def _built_trip_body(journey_id: str) -> dict:
    """The stay-budget block exactly as the code assembles it for the outbound trip body."""

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None, journey_id
        built = experience_payload(session, journey)["trip"]["stay_budget"]
    assert isinstance(built, dict), built
    return built


def test_an_undeclared_budget_sends_no_invented_amount_minor() -> None:
    journey_id = _journey(stay_budget=None)

    built = _built_trip_body(journey_id)

    # Asserted first, so an unmigrated tree fails on the fabrication itself rather than on a
    # missing key: this is the number that went out as if the traveller had named it.
    assert built["amount_minor"] is None, built
    assert built["amount_minor"] != 0, built
    assert built["declared"] is False, built
    # An explicit null, not a dropped key: `v8Types.ts` types this object and
    # `V8Experience.tsx` reads `stay_budget.currency` off it.
    assert "amount_minor" in built, built


def test_a_declared_zero_still_sends_zero_and_a_declared_amount_still_sends_it() -> None:
    declared_zero = _journey(stay_budget={"amount_minor": 0, "currency": "CNY"})
    declared_amount = _journey(stay_budget={"amount_minor": 30000, "currency": "CNY"})

    zero_body = _built_trip_body(declared_zero)
    amount_body = _built_trip_body(declared_amount)

    assert zero_body == {"declared": True, "amount_minor": 0, "currency": "CNY"}, zero_body
    assert amount_body == {
        "declared": True,
        "amount_minor": 30000,
        "currency": "CNY",
    }, amount_body


def test_a_row_written_before_the_column_existed_keeps_the_amount_it_already_held() -> None:
    """NULL means "never recorded", and must not be silently read as "the traveller said no".

    A legacy row may hold an amount somebody really typed (the V12 fixtures store 6000), so
    folding unknown into not-declared would throw that money away and change a working trip.
    """

    journey_id = _journey(stay_budget={"amount_minor": 6000, "currency": "CNY"})
    with SessionLocal() as session:
        row = session.get(JourneyTripContext, journey_id)
        assert row is not None, journey_id
        row.stay_budget_declared = None  # exactly what a pre-ORD-0355 row holds
        session.commit()

    built = _built_trip_body(journey_id)

    assert built["declared"] is None, built
    assert built["amount_minor"] == 6000, built


def test_stay_budget_body_covers_all_three_states_from_the_row_alone() -> None:
    def context(declared, amount: int = 0) -> JourneyTripContext:
        row = JourneyTripContext(journey_id="probe", stay_budget_amount_minor=amount)
        row.stay_budget_declared = declared
        row.stay_budget_currency = "CNY"
        return row

    assert stay_budget_body(context(False))["amount_minor"] is None
    assert stay_budget_body(context(True, 0))["amount_minor"] == 0
    assert stay_budget_body(context(None, 4500))["amount_minor"] == 4500


# ------------------------------------------------------------------- R3 attribution


def _no_hotel_survives(monkeypatch) -> None:
    """Offers exist; every one of them is refused while its nodes are built."""

    import app.stay_mobility as module

    class _Refused(module.StayItineraryConflict):
        pass

    def _offers(_session, _journey, *, attempts_out=None):
        if attempts_out is not None:
            attempts_out.append({"step": "provider", "outcome": "answered", "detail": "2"})
        return [
            types.SimpleNamespace(property_name="Fake Hotel One"),
            types.SimpleNamespace(property_name="Fake Hotel Two"),
        ]

    def _refuse(*_args, **_kwargs):
        raise _Refused("pinned window")

    monkeypatch.setattr(module, "capture_accommodation_offers", _offers)
    monkeypatch.setattr(module, "build_candidate_nodes", _refuse)


def _generation(journey_id: str) -> dict:
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None, journey_id
        out: dict = {}
        ranked = generate_stay_options(session, journey, generation_out=out)
    assert ranked == [], ranked
    return out


def test_an_undeclared_budget_that_returns_nothing_is_not_blamed_on_money(monkeypatch) -> None:
    _no_hotel_survives(monkeypatch)
    journey_id = _journey(stay_budget=None)

    generation = _generation(journey_id)

    assert generation["outcome"] == "STAY_BUDGET_NOT_DECLARED", generation
    assert generation["outcome"] not in MONEY_NAMING_OUTCOMES, MONEY_NAMING_OUTCOMES


def test_a_declared_budget_that_returns_nothing_keeps_the_rejection_answer(monkeypatch) -> None:
    """The other direction, so the new code cannot become the catch-all it replaces."""

    _no_hotel_survives(monkeypatch)
    journey_id = _journey(stay_budget={"amount_minor": 30000, "currency": "CNY"})

    generation = _generation(journey_id)

    assert generation["outcome"] == "ALL_CANDIDATES_REJECTED", generation


def test_the_new_answer_still_reports_the_reasons_the_planner_actually_gave(monkeypatch) -> None:
    """Naming the missing budget is a disclosure, not a claimed cause.

    No planner leg prices a stay against `stay_budget_amount_minor`, so the sentence may not
    crowd out what genuinely happened - the candidate refusals stay in the same block.
    """

    _no_hotel_survives(monkeypatch)
    journey_id = _journey(stay_budget=None)

    generation = _generation(journey_id)

    assert generation["offers_from_provider"] == 2, generation
    assert generation["options_ranked"] == 0, generation
    assert generation["rejections_by_reason"].get("STAY_ITINERARY_CONFLICT") == 2, generation


# ------------------------------------------------------------------------ R4 migration


LEGACY_DDL = """
CREATE TABLE journeys (
    id TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL,
    title TEXT NOT NULL,
    city_code TEXT NOT NULL,
    timezone TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    budget_currency TEXT NOT NULL,
    budget_amount_minor INTEGER NOT NULL CHECK (budget_amount_minor >= 0),
    planning_mode TEXT NOT NULL DEFAULT 'balanced',
    status TEXT NOT NULL,
    active_plan_version_id TEXT,
    current_activity_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE journey_trip_contexts (
    journey_id TEXT PRIMARY KEY REFERENCES journeys(id) ON DELETE CASCADE,
    arrival_anchor_json TEXT NOT NULL DEFAULT '{}',
    departure_anchor_json TEXT NOT NULL DEFAULT '{}',
    party_size INTEGER NOT NULL DEFAULT 1 CHECK (party_size >= 1),
    occupancy_json TEXT NOT NULL DEFAULT '{}',
    stay_budget_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK (stay_budget_amount_minor >= 0),
    stay_budget_currency TEXT NOT NULL DEFAULT 'CNY',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO journeys (id, traveler_id, title, city_code, timezone, start_at, end_at,
                      budget_currency, budget_amount_minor, status, created_at, updated_at)
VALUES ('legacy-journey', 'legacy-traveler', 'before ORD-0355', 'CN-XIAMEN', 'Asia/Shanghai',
        '2026-11-03T09:00:00+08:00', '2026-11-05T17:00:00+08:00', 'CNY', 90000, 'DRAFT',
        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
INSERT INTO journey_trip_contexts (journey_id, stay_budget_amount_minor, stay_budget_currency,
                                   created_at, updated_at)
VALUES ('legacy-journey', 6000, 'CNY', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
"""


def test_the_additive_migration_arrives_without_touching_the_existing_row_or_column(
    tmp_path,
) -> None:
    """A database built with the pre-ORD-0355 DDL, migrated, and read back.

    This store is a `tmp_path` file created by this test and deleted with it.  No shared
    database was opened, and none of `_live.db` / `_preview.db` /
    `D:/moodle/_qoder_preview_20260920` was named by any code path below.
    """

    database = tmp_path / "_ord0355_legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(LEGACY_DDL)

    before = sqlite3.connect(database).execute(
        "SELECT name FROM pragma_table_info('journey_trip_contexts')"
    ).fetchall()
    assert ("stay_budget_declared",) not in before, before

    engine = make_engine(f"sqlite:///{database.as_posix()}")
    ensure_schema(engine)

    after = sqlite3.connect(database).execute(
        "SELECT name, \"notnull\", dflt_value FROM pragma_table_info('journey_trip_contexts')"
        " WHERE name IN ('stay_budget_declared','stay_budget_amount_minor')"
    ).fetchall()
    columns = {str(row[0]): row for row in after}
    assert "stay_budget_declared" in columns, sorted(columns)
    # Additive: nullable, no default. A DEFAULT 0 would have converted every historical row
    # into "asked, and no answer", which is the fabrication this order removes.
    assert int(columns["stay_budget_declared"][1]) == 0, columns
    assert columns["stay_budget_declared"][2] is None, columns
    # The column it shares the table with came out of the migration unchanged.
    assert int(columns["stay_budget_amount_minor"][1]) == 1, columns
    assert columns["stay_budget_amount_minor"][2] == "0", columns

    with sqlite3.connect(database) as connection:
        legacy = connection.execute(
            "SELECT stay_budget_declared, stay_budget_amount_minor"
            " FROM journey_trip_contexts WHERE journey_id = 'legacy-journey'"
        ).fetchone()
    assert legacy == (None, 6000), legacy
    engine.dispose()
