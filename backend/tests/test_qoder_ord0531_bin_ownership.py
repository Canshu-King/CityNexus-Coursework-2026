"""ORD-0531 -- the recycle bin answers from a seat: whose trip, and may this finger bring it back.

Three doors are in play and only one of them changes.  `GET /api/v1/recycle-bin/journeys` answers the
whole bin to anybody and is left exactly as it is, tested here for that; the scoped door
`GET /api/v1/travelers/{id}/recycle-bin/journeys` is what gains the two named answers; and the 找回
write door is untouched, because this order pinned its own landing point to the read route.  What the
write door will do with a row this door calls read-only is reported, not quietly fixed here.

* B1 the owner's own binned trip comes back owned-by-them and restorable;
* B2 a stranger's binned trip is not in this traveller's bin at all -- the row, not a refusal of it;
* B3 a traveller seated on somebody else's binned trip sees it, named as another's, with the role
  that answer came from;
* B4 the capability table decides `recovery`, so a seat whose role does not hold JOURNEY_UPDATE reads
  refused while the same caller's own trip reads allowed -- in one response, which is what stops
  `recovery` from being a constant with better branding;
* B5 counter-example: with the ownership filter lifted the row does appear, which is the reading that
  proves the filter is the thing keeping it out (a helper-level mutation, so the shipped door is never
  weakened by this file);
* B6 the anonymous whole-bin door still answers every binned trip, unscoped, and its rows carry no
  ownership answer -- the promise ORD-0402 slice 1 tested is intact;
* B7 isolation narrows which rows a traveller is handed, not the order they arrive in: the rows both
  doors answer come out `deleted_at` newest-first from each of them;
* B8 the bin screen carries no numeric `file:line` pointer, and the names it does point at are still
  in the product -- the CMDR 07:57 ruling applied to this screen's own comments.

Names measured, not assumed: `journeys.traveler_id` is the ownership column, `journey_members.role`
the seat, `journey_role_capabilities (role, capability)` the table `recovery` is asked of.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.models import Traveler
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
TZ = timezone(timedelta(hours=8))
START = datetime(2027, 3, 4, 9, 0, tzinfo=TZ)

BIN_PATH = "/api/v1/recycle-bin/journeys"
SCOPED_PATH = "/api/v1/travelers/{traveler_id}/recycle-bin/journeys"


def _traveler(label: str) -> str:
    traveler_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name=f"ORD-0531 {label}",
                email=None,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return traveler_id


def _journey(traveler_id: str, title: str) -> str:
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler_id,
            "title": title,
            "city_code": "CN-TIANJIN",
            "timezone": "Asia/Shanghai",
            "start_at": START.isoformat(),
            "end_at": (START + timedelta(days=1, hours=8)).isoformat(),
            "budget": {"amount_minor": 90000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _bin(journey_id: str) -> None:
    dropped = client.delete(f"/api/v1/journeys/{journey_id}")
    assert dropped.status_code == 204, dropped.text


def _seat(journey_id: str, traveler_id: str, role: str, inviter: str) -> None:
    added = client.post(
        f"/api/v1/journeys/{journey_id}/members",
        json={"traveler_id": traveler_id, "role": role, "invited_by": inviter},
    )
    assert added.status_code == 201, added.text


def _scoped(traveler_id: str) -> list[dict]:
    read = client.get(SCOPED_PATH.format(traveler_id=traveler_id))
    assert read.status_code == 200, read.text
    return read.json()


def _row(rows: list[dict], journey_id: str) -> dict | None:
    return next((row for row in rows if row["id"] == journey_id), None)


def test_b1_the_owner_reads_their_own_row_as_owned_and_restorable() -> None:
    owner = _traveler("owner-b1")
    journey_id = _journey(owner, "ORD-0531 b1")
    _bin(journey_id)

    row = _row(_scoped(owner), journey_id)
    assert row is not None, "the owner's own binned trip is missing from their bin"
    assert row["ownership"] == "OWNED_BY_THIS_TRAVELER", row
    assert row["recovery"] == "RECOVERY_ALLOWED", row
    assert row["bin_state"] == "IN_RECYCLE_BIN", row
    assert row["deleted_at"], "the stored discard moment must ride with the row"


def test_b2_a_stranger_does_not_see_the_row_at_all() -> None:
    owner = _traveler("owner-b2")
    other = _traveler("stranger-b2")
    journey_id = _journey(owner, "ORD-0531 b2")
    _bin(journey_id)

    assert _row(_scoped(other), journey_id) is None, (
        "another traveller's discarded trip reached this bin; a refusal would still be a leak"
    )
    # and it is really in the bin, so the absence is the filter rather than a missing row
    whole = client.get(BIN_PATH).json()
    assert _row(whole, journey_id) is not None, whole


def test_b3_a_seat_sees_it_and_is_named_as_somebody_elses() -> None:
    owner = _traveler("owner-b3")
    member = _traveler("member-b3")
    journey_id = _journey(owner, "ORD-0531 b3")
    _bin(journey_id)
    _seat(journey_id, member, "ADMIN", owner)

    row = _row(_scoped(member), journey_id)
    assert row is not None, "an in-force seat should still reach the binned trip"
    assert row["ownership"] == "OWNED_BY_ANOTHER_TRAVELER", row
    assert row["member_role"] == "ADMIN", row


def test_b4_recovery_follows_the_capability_table_in_one_response() -> None:
    # Measured on this tree's own seed: only OWNER holds JOURNEY_UPDATE, so an ADMIN seat is the real
    # read-only case rather than a hypothetical one.
    member = _traveler("member-b4")
    theirs = _journey(member, "ORD-0531 b4 mine")
    _bin(theirs)
    owner = _traveler("owner-b4")
    shared = _journey(owner, "ORD-0531 b4 shared")
    _bin(shared)
    _seat(shared, member, "ADMIN", owner)

    rows = _scoped(member)
    mine = _row(rows, theirs)
    theirs_row = _row(rows, shared)
    assert mine is not None and theirs_row is not None, rows
    assert mine["recovery"] == "RECOVERY_ALLOWED", mine
    assert theirs_row["recovery"] == "RECOVERY_REFUSED_ROLE_CANNOT_RESTORE", theirs_row
    assert mine["recovery"] != theirs_row["recovery"], (
        "one caller, one response, two different answers -- equal values would mean a constant"
    )


def test_b5_counterexample_the_filter_is_what_keeps_the_row_out() -> None:
    """B2 proves the door hides it; this proves *why* -- lift the seat test and the same row arrives.

    Done against the helper the door calls, so the shipped door is never loosened by this file.  A
    helper that hid nothing would make B2 pass for the wrong reason, and that is the reading nobody
    would notice later.
    """
    from datetime import timezone as _tz

    from app.db.models import Journey
    from app.db.session import SessionLocal as _Session
    from app.main import _traveler_bin_row

    owner = _traveler("owner-b5")
    stranger = _traveler("stranger-b5")
    journey_id = _journey(owner, "ORD-0531 b5")
    _bin(journey_id)

    with _Session() as session:
        journey = session.get(Journey, journey_id)
        now = datetime.now(_tz.utc)
        # as the owner: a row exists and says so
        assert _traveler_bin_row(session, journey, owner, now) is not None
        # as a stranger with no seat: no row at all
        assert _traveler_bin_row(session, journey, stranger, now) is None
        # and the row is genuinely binned, so the None above is the isolation doing work
        assert journey.deleted_at and journey.traveler_id == owner


def test_b6_the_anonymous_door_still_answers_the_whole_bin_unchanged() -> None:
    """Item 4 of the order: the existing anonymous read path is not touched by this change."""
    owner = _traveler("owner-b6")
    other = _traveler("other-b6")
    first = _journey(owner, "ORD-0531 b6 a")
    second = _journey(other, "ORD-0531 b6 b")
    _bin(first)
    _bin(second)

    read = client.get(BIN_PATH)
    assert read.status_code == 200, read.text
    rows = read.json()
    ids = {row["id"] for row in rows}
    assert {first, second} <= ids, "the whole-bin door lost a row -- scope leaked into it"
    assert all(row["deleted_at"] for row in rows), rows
    assert all("ownership" not in row and "recovery" not in row for row in rows), (
        "the anonymous door started speaking about a caller it was never given"
    )
    # and the scoped doors for each owner still see only their own
    assert {row["id"] for row in _scoped(owner)} & {first, second} == {first}
    assert {row["id"] for row in _scoped(other)} & {first, second} == {second}


def test_b7_the_two_doors_put_the_same_rows_in_the_same_order() -> None:
    """`deleted_at` newest-first is one rule for both doors, not one per door.

    Isolation narrows which rows this traveller is handed; it must not silently narrow the order too.
    Reading the same two trips in two different orders from two doors of one bin is the kind of
    disagreement a screen cannot detect, so it is tested here rather than left to the docstring.
    """
    owner = _traveler("owner-b7")
    earlier = _journey(owner, "ORD-0531 b7 earlier")
    later = _journey(owner, "ORD-0531 b7 later")
    _bin(earlier)
    _bin(later)

    whole = [row["id"] for row in client.get(BIN_PATH).json()]
    mine = [row["id"] for row in _scoped(owner)]
    shared = [row_id for row_id in whole if row_id in set(mine)]
    assert {earlier, later} <= set(shared), (earlier, later, shared)
    assert [row_id for row_id in mine if row_id in set(whole)] == shared, (
        "the scoped door reordered rows the whole-bin door already orders"
    )
    # and the newest discard really is first in both
    assert shared.index(later) < shared.index(earlier), shared


def test_b8_the_screen_owns_no_stale_pointer() -> None:
    """CMDR 07:57 的裁定用在回收站这一屏自己身上：注释只准指名字，不准指行号。

    这个模块的头部原来钉了七枚 `file:line`，代次一换就有五枚落到无关的行上、一枚指向仓里已经不存在的
    收件件。判据两头都要有：不许出现数字指针，且那些名字必须还在产品里——不然删掉名字就能骗过这把尺。
    """

    repo = Path(__file__).resolve().parents[2]
    watched = [
        "frontend/src/features/trash/journeyTrash.ts",
        "frontend/src/features/trash/JourneyTrashPanel.tsx",
        "frontend/src/features/trash/journeyTrashCopy.ts",
    ]
    numeric = re.compile(r"`[^`]*\.(?:py|ts|tsx|json|ya?ml):\d+`")
    offenders: dict[str, list[str]] = {}
    for relative in watched:
        text = (repo / relative).read_text(encoding="utf-8")
        hits = [line.strip()[:78] for line in text.splitlines() if numeric.search(line)]
        if hits:
            offenders[relative] = hits
    assert offenders == {}, f"numeric file:line citations came back into the bin screen: {offenders}"

    backend_source = (repo / "backend/app/main.py").read_text(encoding="utf-8")
    schemas_source = (repo / "backend/app/schemas.py").read_text(encoding="utf-8")
    for needle in (
        'def undelete_journey(',
        'def list_recycle_bin_journeys(',
        'def list_journeys(',
        'reason_code = "JOURNEY_NOT_DELETED"',
    ):
        assert needle in backend_source, f"the header names {needle!r}, which is no longer on the line"
    assert "class RecycleBinJourney(" in schemas_source
