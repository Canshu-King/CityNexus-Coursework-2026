from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Journey,
    JourneyMember,
    MemberPlaceGroupShareConsent,
    MemberPlaceInvitation,
    MemberPlaceInvitationRecipient,
    Traveler,
)
from app.shared_stop_coplanning import member_is_in_force


GROUP_SHARE_TTL = timedelta(hours=24)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _current_facts(
    session: Session,
    journey: Journey,
    invitation: MemberPlaceInvitation,
    *,
    now: datetime,
) -> tuple[dict[str, Any], list[JourneyMember]]:
    if str(invitation.journey_id) != str(journey.id):
        raise HTTPException(404, "PLACE_INVITATION_NOT_FOUND")
    if str(invitation.state) == "WITHDRAWN":
        raise HTTPException(409, "INVITER_CONSENT_WITHDRAWN")
    if not journey.active_plan_version_id or str(invitation.active_plan_version_id) != str(journey.active_plan_version_id):
        raise HTTPException(409, "PLAN_CHANGED")
    if str(journey.status or "").upper() != "ACTIVE":
        raise HTTPException(409, "PLAN_CHANGED")

    owner = session.get(JourneyMember, str(invitation.owner_member_id))
    if (
        owner is None
        or str(owner.journey_id) != str(journey.id)
        or str(owner.traveler_id) != str(invitation.owner_traveler_id)
        or not member_is_in_force(owner, now=now)
    ):
        raise HTTPException(409, "INVITER_SPLIT_STALE")

    from app.execution.member_place_invitation import _invitation_context_is_current

    if not _invitation_context_is_current(session, journey, invitation, now=now):
        raise HTTPException(409, "INVITER_SPLIT_STALE")

    audience = session.scalars(
        select(JourneyMember)
        .where(JourneyMember.journey_id == str(journey.id))
        .order_by(JourneyMember.id)
    ).all()
    audience = [member for member in audience if member_is_in_force(member, now=now)]
    audience_facts = [
        {
            "member_id": str(member.id),
            "traveler_id": str(member.traveler_id),
            "role": str(member.role),
            "joined_at": str(member.joined_at or ""),
        }
        for member in sorted(audience, key=lambda item: str(item.id))
    ]
    if len(audience_facts) < 2:
        raise HTTPException(409, "NOT_SHARED_TO_GROUP")

    recipients = session.scalars(
        select(MemberPlaceInvitationRecipient).where(
            MemberPlaceInvitationRecipient.invitation_id == str(invitation.id)
        )
    ).all()
    accepted_recipients = {
        str(row.recipient_member_id)
        for row in recipients
        if str(row.state) == "ACCEPTED_OWN_STOP_READY"
        and any(str(member.id) == str(row.recipient_member_id) for member in audience)
    }
    if not accepted_recipients:
        raise HTTPException(409, "GROUP_SHARE_REQUIRES_ACCEPTED_INVITEE")

    try:
        disclosure = json.loads(str(invitation.disclosure_snapshot_json or "{}"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(409, "INVITER_SPLIT_STALE") from exc
    place = disclosure.get("place") if isinstance(disclosure, dict) else None
    rendezvous = disclosure.get("rendezvous") if isinstance(disclosure, dict) else None
    if not isinstance(place, dict) or not isinstance(rendezvous, dict):
        raise HTTPException(409, "INVITER_SPLIT_STALE")

    audience_snapshot = _digest(audience_facts)
    plan_material = {
        "active_plan_version_id": str(journey.active_plan_version_id),
        "current_activity_id": str(journey.current_activity_id or ""),
    }
    source_material = {
        "journey_id": str(journey.id),
        "invitation_id": str(invitation.id),
        "owner_member_id": str(invitation.owner_member_id),
        "owner_traveler_id": str(invitation.owner_traveler_id),
        "owner_split_intent_id": str(invitation.owner_split_intent_id),
        "owner_side_quest_id": str(invitation.owner_side_quest_id),
        "place_id": str(invitation.place_id),
        "place_provider_key": str(invitation.place_provider_key),
        "place_external_id": str(invitation.place_external_id),
        "place_snapshot": place,
        "rendezvous_activity_id": str(invitation.rendezvous_activity_id),
        "rendezvous_place_id": str(invitation.rendezvous_place_id),
        "rendezvous_snapshot": rendezvous,
    }
    facts = {
        "audience": audience_facts,
        "audience_snapshot_hash": audience_snapshot,
        "plan": plan_material,
        "plan_snapshot_hash": _digest(plan_material),
        "source": source_material,
        "source_snapshot_hash": _digest(source_material),
        "place_snapshot_json": _canonical(place),
        "accepted_recipient_member_ids": sorted(accepted_recipients),
    }
    return facts, audience


def group_share_preview(
    session: Session,
    *,
    journey: Journey,
    invitation: MemberPlaceInvitation,
    owner_member_id: str,
    owner_traveler_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (
        str(invitation.owner_member_id) != str(owner_member_id)
        or str(invitation.owner_traveler_id) != str(owner_traveler_id)
    ):
        raise HTTPException(403, "GROUP_SHARE_REQUIRES_SIGNED_SOURCE_OWNER")
    facts, audience = _current_facts(session, journey, invitation, now=current)
    try:
        disclosure = json.loads(str(invitation.disclosure_snapshot_json or "{}"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(409, "INVITER_SPLIT_STALE") from exc
    audience_views = []
    for member in audience:
        if str(member.id) == str(owner_member_id):
            continue
        traveler = session.get(Traveler, str(member.traveler_id))
        audience_views.append(
            {
                "member_id": str(member.id),
                "display_name": str(traveler.display_name if traveler else "Traveller"),
            }
        )
    return {
        "invitation_id": str(invitation.id),
        "place": disclosure["place"],
        "rendezvous": disclosure["rendezvous"],
        "audience": audience_views,
        "audience_snapshot": facts["audience_snapshot_hash"],
        "plan_snapshot": facts["plan_snapshot_hash"],
        "source_snapshot": facts["source_snapshot_hash"],
        "disclosure": (
            "This shares the selected public Place details with every member listed above. "
            "It does not share the inviter's device location, private origin, route, notes, or search history. "
            "A shared-plan change still requires separate, explicit decisions from the current Journey members."
        ),
    }


def legacy_full_audience_consent(
    session: Session,
    journey: Journey,
    invitation: MemberPlaceInvitation,
    *,
    facts: dict[str, Any],
) -> bool:
    addressed = _addressed_member_ids(session, invitation)
    audience_ids = {
        str(item["member_id"])
        for item in facts["audience"]
        if str(item["member_id"]) != str(invitation.owner_member_id)
    }
    return bool(audience_ids) and addressed == audience_ids


def _addressed_member_ids(
    session: Session, invitation: MemberPlaceInvitation
) -> set[str]:
    addressed = {
        str(row.recipient_member_id)
        for row in session.scalars(
            select(MemberPlaceInvitationRecipient).where(
                MemberPlaceInvitationRecipient.invitation_id == str(invitation.id)
            )
        ).all()
    }
    return addressed


def current_group_share(
    session: Session,
    journey: Journey,
    invitation: MemberPlaceInvitation,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        facts, _audience = _current_facts(session, journey, invitation, now=current)
    except HTTPException as exc:
        return {"shared": False, "status": str(exc.detail), "receipt": None}

    if legacy_full_audience_consent(session, journey, invitation, facts=facts):
        return {"shared": True, "status": "SHARED_BY_ORIGINAL_INVITATION", "receipt": None}

    receipts = session.scalars(
        select(MemberPlaceGroupShareConsent)
        .where(
            MemberPlaceGroupShareConsent.journey_id == str(journey.id),
            MemberPlaceGroupShareConsent.invitation_id == str(invitation.id),
        )
        .order_by(
            MemberPlaceGroupShareConsent.created_at.desc(),
            MemberPlaceGroupShareConsent.id.desc(),
        )
    ).all()
    active = next((row for row in receipts if str(row.status) == "ACTIVE"), None)
    if active is not None:
        expiry = _instant(active.expires_at)
        if expiry is None or expiry <= current:
            return {"shared": False, "status": "INVITER_CONSENT_EXPIRED", "receipt": active}
        if (
            str(active.base_plan_version_id) != str(journey.active_plan_version_id)
            or str(active.base_cursor_activity_id or "") != str(journey.current_activity_id or "")
            or str(active.plan_snapshot_hash) != str(facts["plan_snapshot_hash"])
        ):
            return {"shared": False, "status": "PLAN_CHANGED", "receipt": active}
        if str(active.audience_snapshot_hash) != str(facts["audience_snapshot_hash"]):
            return {"shared": False, "status": "AUDIENCE_CHANGED", "receipt": active}
        if (
            str(active.owner_member_id) != str(invitation.owner_member_id)
            or str(active.owner_traveler_id) != str(invitation.owner_traveler_id)
            or str(active.owner_split_intent_id) != str(invitation.owner_split_intent_id)
            or str(active.owner_side_quest_id) != str(invitation.owner_side_quest_id)
            or str(active.place_id) != str(invitation.place_id)
            or str(active.place_snapshot_json) != str(facts["place_snapshot_json"])
            or str(active.rendezvous_activity_id) != str(invitation.rendezvous_activity_id)
            or str(active.rendezvous_place_id) != str(invitation.rendezvous_place_id)
            or str(active.source_snapshot_hash) != str(facts["source_snapshot_hash"])
        ):
            return {"shared": False, "status": "INVITER_SPLIT_STALE", "receipt": active}
        return {"shared": True, "status": "SHARED_TO_GROUP", "receipt": active}

    terminal = next(
        (
            row
            for row in receipts
            if str(row.status) in {"WITHDRAWN", "EXPIRED", "STALE"}
        ),
        None,
    )
    if terminal is None:
        current_audience_ids = {
            str(item["member_id"])
            for item in facts["audience"]
            if str(item["member_id"]) != str(invitation.owner_member_id)
        }
        addressed = _addressed_member_ids(session, invitation)
        status = (
            "AUDIENCE_CHANGED"
            if addressed and addressed != current_audience_ids
            else "NOT_SHARED_TO_GROUP"
        )
        return {"shared": False, "status": status, "receipt": None}
    status = {
        "WITHDRAWN": "INVITER_CONSENT_WITHDRAWN",
        "EXPIRED": "INVITER_CONSENT_EXPIRED",
        "STALE": "INVITER_SPLIT_STALE",
    }.get(str(terminal.status), "NOT_SHARED_TO_GROUP")
    return {"shared": False, "status": status, "receipt": terminal}


def require_group_share(
    session: Session,
    journey: Journey,
    invitation: MemberPlaceInvitation,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    state = current_group_share(session, journey, invitation, now=now)
    if not state["shared"]:
        raise HTTPException(409, str(state["status"] or "NOT_SHARED_TO_GROUP"))
    return state


def require_group_share_sponsor(
    session: Session,
    journey: Journey,
    invitation: MemberPlaceInvitation,
    *,
    actor: JourneyMember,
    traveler_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    state = require_group_share(session, journey, invitation, now=now)
    if str(actor.traveler_id) != str(traveler_id):
        raise HTTPException(403, "SIGNED_TRAVELER_IDENTITY_REQUIRED_FOR_GROUP_SHARE_SPONSOR")
    if str(actor.id) == str(invitation.owner_member_id):
        if state["status"] == "SHARED_BY_ORIGINAL_INVITATION":
            return state
        # The source author may sponsor only if their real Journey role already
        # carries JOURNEY_UPDATE; the consent receipt never adds that capability.
    else:
        recipient = session.scalar(
            select(MemberPlaceInvitationRecipient.id).where(
                MemberPlaceInvitationRecipient.invitation_id == str(invitation.id),
                MemberPlaceInvitationRecipient.recipient_member_id == str(actor.id),
                MemberPlaceInvitationRecipient.recipient_traveler_id == str(traveler_id),
                MemberPlaceInvitationRecipient.state == "ACCEPTED_OWN_STOP_READY",
            )
        )
        if recipient is None:
            raise HTTPException(403, "GROUP_SHARE_SPONSOR_REQUIRES_ACCEPTED_INVITATION_RECIPIENT")
    return state


def consent_row_for_snapshot(
    *,
    session: Session,
    journey: Journey,
    invitation: MemberPlaceInvitation,
    owner: JourneyMember,
    traveler_id: str,
    facts: dict[str, Any],
    now: datetime,
) -> MemberPlaceGroupShareConsent | None:
    active = session.scalar(
        select(MemberPlaceGroupShareConsent).where(
            MemberPlaceGroupShareConsent.journey_id == str(journey.id),
            MemberPlaceGroupShareConsent.invitation_id == str(invitation.id),
            MemberPlaceGroupShareConsent.status == "ACTIVE",
        )
    )
    if active is None:
        return None
    expiry = _instant(active.expires_at)
    if expiry is None or expiry <= now:
        active.status = "EXPIRED"
        active.updated_at = now.isoformat()
        # Release the partial-unique ACTIVE consent slot *before* the caller
        # adds its replacement receipt. The flush stays in this transaction;
        # a later failed confirmation still rolls the retirement back.
        session.flush()
        return None
    if current_group_share(session, journey, invitation, now=now)["shared"]:
        if (
            str(active.owner_member_id) == str(owner.id)
            and str(active.owner_traveler_id) == str(traveler_id)
            and str(active.audience_snapshot_hash) == str(facts["audience_snapshot_hash"])
            and str(active.plan_snapshot_hash) == str(facts["plan_snapshot_hash"])
            and str(active.source_snapshot_hash) == str(facts["source_snapshot_hash"])
        ):
            return active
    active.status = "STALE"
    active.updated_at = now.isoformat()
    # A changed audience/cursor/source may be reconfirmed in the same
    # request. Flush the retiring row before inserting the new ACTIVE row:
    # an ORM commit need not order unrelated UPDATEs ahead of INSERTs.
    session.flush()
    return None


def build_consent_row(
    *,
    journey: Journey,
    invitation: MemberPlaceInvitation,
    owner: JourneyMember,
    traveler_id: str,
    facts: dict[str, Any],
    now: datetime,
) -> MemberPlaceGroupShareConsent:
    return MemberPlaceGroupShareConsent(
        id=uuid4().hex,
        journey_id=str(journey.id),
        invitation_id=str(invitation.id),
        owner_member_id=str(owner.id),
        owner_traveler_id=str(traveler_id),
        owner_split_intent_id=str(invitation.owner_split_intent_id),
        owner_side_quest_id=str(invitation.owner_side_quest_id),
        base_plan_version_id=str(journey.active_plan_version_id),
        base_cursor_activity_id=str(journey.current_activity_id) if journey.current_activity_id else None,
        place_id=str(invitation.place_id),
        place_snapshot_json=str(facts["place_snapshot_json"]),
        rendezvous_activity_id=str(invitation.rendezvous_activity_id),
        rendezvous_place_id=str(invitation.rendezvous_place_id),
        audience_snapshot_json=_canonical(facts["audience"]),
        audience_snapshot_hash=str(facts["audience_snapshot_hash"]),
        plan_snapshot_hash=str(facts["plan_snapshot_hash"]),
        source_snapshot_hash=str(facts["source_snapshot_hash"]),
        status="ACTIVE",
        consented_at=now.isoformat(),
        expires_at=(now + GROUP_SHARE_TTL).isoformat(),
        withdrawn_at=None,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
