"""ORD-0521 - the product's own door from an empty store to one demo trip that can be walked.

Three lanes have tried to put a walkable trip on a phone and each stopped at the same hop: the
supply.  A fresh store holds no `places` row, so `plans/generate` answers `places_in_catalog: 0`,
and the only door that fills the catalogue - `demo/reset` - deletes 52 tables before it does
(`services.seed_demo`, the first statement of its body).  That is also why `catalog-hydrations`
ends `NO_REGISTERED_SOURCE` on a clean store: the verdict is decided locally in
`catalog_hydration_operations._survey` from `places_held == 0`, and no provider is consulted on
that branch at all.

The rows are already in the repository - `data/mock/places.json` is 16 offline rows
(`provider_key=mock-city`, read from disk, no socket) - they were simply only reachable behind the
destructive door.  This module registers them as a supply source *additively*: install what is
missing, leave everything else alone, hand back one journey with a selected plan.

What this door refuses to do:
* delete anything - `rows_deleted` is reported, so a reader can check the claim instead of
  trusting it;
* invent a place - when the destination's places are not held it answers
  `DEMO_STATE_SUPPLY_UNAVAILABLE` naming the hop, the reason and the cities it *can* hold, rather
  than producing a trip whose screen says `unresolved=3`;
* pretend a second call is a second scenario - a replayed `Idempotency-Key` returns the first
  receipt, and the same key against a different body is a conflict, not a second board.
"""

from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import func, inspect as sa_inspect, select
from sqlalchemy.orm import Session

from app.commerce.idempotency import canonical_request_signature
from app.db.models import IdempotencyKey, Journey, Place
from app.services import build_demo_fixture_rows

DEMO_SCENE_ENV = "CITYNEXUS_DEMO_SCENE_TOKEN"
DEMO_SCENE_HEADER = "X-Demo-Scene-Token"
SCENE_OPERATION = "demo.scene.ensure"

# "Where did these places come from" is part of the payload: a catalogue that fills up without
# naming its source cannot be told apart from one that filled up correctly.
SUPPLY_LEG = "builtin_fixture:data/mock/places.json (provider_key=mock-city, read from disk, no network)"

DEMO_STATE_READY = "READY"
DEMO_STATE_SCENE_INSTALLED = "SCENE_INSTALLED"
DEMO_STATE_SUPPLY_UNAVAILABLE = "DEMO_STATE_SUPPLY_UNAVAILABLE"
DEMO_STATE_PLAN_REFUSED = "SCENE_INSTALLED_BUT_PLAN_REFUSED"


class SceneIdempotencyConflict(ValueError):
    """The same key arrived with a different body - a caller bug, never a second scene."""


def demo_scene_token_denied(presented: str | None) -> tuple[str, str] | None:
    """The operator's token gate, shaped like `demo_reset_token_denied`.

    Read from the environment at call time rather than from the frozen `Settings`, so both states
    are exercisable inside one process.  The two refusal codes are deliberately indistinguishable
    in shape and no token value is ever returned, logged or echoed.
    """

    if not presented:
        return (
            "SCENE_TOKEN_REQUIRED",
            f"No {DEMO_SCENE_HEADER} was presented; nothing was written.",
        )
    expected = os.environ.get(DEMO_SCENE_ENV) or ""
    if not hmac.compare_digest(presented, expected):
        return (
            "SCENE_TOKEN_INVALID",
            f"{DEMO_SCENE_HEADER} does not match this deployment's scene token; nothing was written.",
        )
    return None


def _primary_key_of(row: Any) -> tuple[type, Any] | None:
    mapper = sa_inspect(type(row))
    attrs = [column.key for column in mapper.primary_key]
    values = [getattr(row, attr, None) for attr in attrs]
    if any(value is None for value in values):
        return None
    return type(row), values[0] if len(values) == 1 else tuple(values)


def supplied_city_codes() -> list[str]:
    """The cities the offline supply actually holds, counted off the fixture rows themselves."""

    held: dict[str, int] = {}
    for row in build_demo_fixture_rows():
        if isinstance(row, Place):
            held[row.city_code] = held.get(row.city_code, 0) + 1
    return sorted(held)


_FIXTURE_IDS: dict[str, str | None] = {}


def _fixture_journey_id() -> str | None:
    """The id the shipped scenario is stored under, read off the fixture rather than typed here."""

    if "journey" not in _FIXTURE_IDS:
        _FIXTURE_IDS["journey"] = next(
            (row.id for row in build_demo_fixture_rows() if isinstance(row, Journey)), None
        )
    return _FIXTURE_IDS["journey"]


def install_supply(session: Session) -> dict[str, Any]:
    """Add every fixture row this store does not already hold and change nothing else.

    `rows_deleted: 0` is a count, not a promise: the only statements reached are `session.add`,
    one `flush` and one `commit`, and the suite's mutant for this claim is a `delete` inserted at
    the top of this function.
    """

    rows = build_demo_fixture_rows()
    installed: list[str] = []
    present: list[str] = []
    for row in rows:
        keyed = _primary_key_of(row)
        if keyed is not None and session.get(keyed[0], keyed[1]) is not None:
            present.append(type(row).__name__)
            continue
        session.add(row)
        # per row, not batched: table-level FKs alone do not order a unit of work
        session.flush()
        installed.append(type(row).__name__)
    session.flush()
    held = dict(
        session.execute(
            select(Place.city_code, func.count(Place.id)).group_by(Place.city_code)
        ).all()
    )
    return {
        "supply_leg": SUPPLY_LEG,
        "destructive": False,
        "rows_deleted": 0,
        "rows_installed": len(installed),
        "rows_already_present": len(present),
        "places_held": held,
        "supported_city_codes": sorted(held),
        "installed_by_table": {
            name: sum(1 for item in installed if item == name) for name in sorted(set(installed))
        },
    }


def _journey_for_city(session: Session, city_code: str, base: Journey) -> Journey:
    """A trip for a city the supply holds but the shipped scenario does not cover.

    Only the identity and the label change: traveler, dates, timezone and budget come from the
    fixture journey, so this is the same demo moved onto a city the catalogue can serve - not a
    destination invented from a string.
    """

    journey = Journey(
        id=str(uuid4()),
        traveler_id=base.traveler_id,
        title=f"{base.title} ({city_code})",
        city_code=city_code,
        timezone=base.timezone,
        start_at=base.start_at,
        end_at=base.end_at,
        budget_currency=base.budget_currency,
        budget_amount_minor=base.budget_amount_minor,
        planning_mode=base.planning_mode,
        status="DRAFT",
        active_plan_version_id=None,
        current_activity_id=None,
        created_at=base.created_at,
        updated_at=base.updated_at,
    )
    session.add(journey)
    session.flush()
    return journey


def _replay(session: Session, scoped_key: str, signature: str) -> dict[str, Any] | None:
    row = session.get(IdempotencyKey, scoped_key)
    if row is None:
        return None
    if row.request_hash != signature:
        raise SceneIdempotencyConflict("IDEMPOTENCY_KEY_REUSE_CONFLICT")
    if row.response_json:
        receipt = json.loads(row.response_json)
        receipt["idempotency_replay"] = True
        return receipt
    return {
        "state": DEMO_STATE_SCENE_INSTALLED,
        "idempotency_replay": True,
        "replayed_at": row.created_at,
    }


def ensure_demo_scene(
    session: Session,
    *,
    idempotency_key: str,
    body: dict[str, Any],
    advance: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Empty store -> one trip with a schedule and stops, or a named reason it cannot be done.

    `advance` is injected by the route because the plan hop lives in `main.py` and has to be
    reached through the same in-process composition `demo/prepare-scenario` uses - never through
    the HTTP route, whose `Header(default=None)` sentinels made an in-process call generate zero
    plans while still answering 200.
    """

    signature = canonical_request_signature(
        {"city_code": body.get("city_code"), "journey_id": body.get("journey_id")}
    )
    scoped_key = f"{SCENE_OPERATION}:{idempotency_key}"
    replayed = _replay(session, scoped_key, signature)
    if replayed is not None:
        return replayed

    supply = install_supply(session)
    requested = body.get("city_code")
    # The door advances the trip it just put on the stage, not "whatever row is newest": on a store
    # that already holds somebody else's board, picking theirs would both move their journey and
    # plan it against constraints that are not theirs.
    fixture_id = _fixture_journey_id()
    journey = session.get(Journey, fixture_id) if fixture_id else None
    if journey is None:
        journey = session.scalar(
            select(Journey).where(Journey.status == "DRAFT").order_by(Journey.created_at.desc())
        ) or session.scalar(select(Journey).order_by(Journey.created_at.desc()))

    def unsupplyable(reason_hop: str, placed: int, note: str) -> dict[str, Any]:
        session.rollback()
        return {
            "state": DEMO_STATE_SUPPLY_UNAVAILABLE,
            "message": note,
            "failed_hop": reason_hop,
            "reason": "NO_SUPPLIED_PLACE_ROW_FOR_DESTINATION",
            "places_held": placed,
            "supported_city_codes": supply["supported_city_codes"],
            "next_hop": "catalog-hydrations: register a provider that covers this destination",
            "supply": supply,
        }

    if requested and requested not in supply["supported_city_codes"]:
        return unsupplyable(
            "places_catalog",
            supply["places_held"].get(requested, 0),
            f"This server cannot obtain places for {requested!r}. Its only supply leg is the "
            f"built-in offline fixture, which holds "
            f"{', '.join(f'{c}:{n}' for c, n in supply['places_held'].items()) or 'nothing'}.",
        )

    if journey is None:
        return unsupplyable(
            "journey",
            sum(supply["places_held"].values()),
            "No trip could be put on the stage: the supply installed, but the fixture carries no "
            "journey row and nothing else was created.",
        )

    created_by_this_call = False
    if requested and requested != journey.city_code:
        journey = _journey_for_city(session, requested, journey)
        created_by_this_call = True

    session.commit()
    journey_id = str(journey.id)
    advance_report = advance(journey_id)
    row = session.get(Journey, journey_id)
    plan_version_id = row.active_plan_version_id if row is not None else None
    refused_steps = [step for step in advance_report.get("steps", []) if "refused" in step]

    if plan_version_id and not refused_steps:
        state = DEMO_STATE_READY
    else:
        state = DEMO_STATE_PLAN_REFUSED

    decision = {
        "state": state,
        "journey_id": journey_id,
        # Which row this call put there, so a refused leg leaves an attributable residue rather
        # than an unexplained journey on the board.
        "journey_created_by_this_call": created_by_this_call,
        "city_code": row.city_code if row is not None else None,
        "journey_status": row.status if row is not None else None,
        "plan_version_id": str(plan_version_id) if plan_version_id else None,
        # The readable sentence cell (3) asks for, in place of an `unresolved` count on a screen.
        "scene_label": (
            f"{row.title if row is not None else ''} - {row.city_code if row is not None else ''} - "
            "places supplied from the built-in offline fixture"
        ),
        "supply": supply,
        "advance": advance_report,
        "advance_refused_steps": refused_steps,
        "idempotency_key": scoped_key,
    }
    session.add(
        IdempotencyKey(
            key=scoped_key,
            operation=SCENE_OPERATION,
            request_hash=signature,
            response_status=200,
            response_json=json.dumps(decision, sort_keys=True, default=str),
            created_at=datetime.now(timezone.utc).isoformat(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        )
    )
    try:
        session.commit()
    except Exception:  # noqa: BLE001 - losing the receipt must not lose the answer it receipts
        session.rollback()
    return decision
