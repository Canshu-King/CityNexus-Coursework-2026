"""V19.1 convergence red-team harness (tests-only, no production changes).

Self-locating: the same file works for Snapshot A and Snapshot B without edits,
because the snapshot root is derived from this file's own path
(``<snapshot>/backend/tests/v191_harness.py`` -> ``<snapshot>``).

Drives the FastAPI app in-process against a throwaway SQLite database, with
bytecode writing disabled so nothing is ever written back into the main
worktree.

Import:
    import v191_harness as H
    H.reset_demo()
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]              # <snapshot>/backend
SNAPSHOT = HERE.parents[2]             # <snapshot>
LAB = SNAPSHOT.parent                  # the lab root
LABEL = SNAPSHOT.name                  # snapshot_a / snapshot_b

DB_PATH = LAB / "evidence" / "_db" / f"{LABEL}.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["CITYNEXUS_DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ.setdefault("CITYNEXUS_ENV", "test")
os.environ.setdefault("CITYNEXUS_AI_ENABLED", "false")
os.environ.setdefault("CITYNEXUS_GUARDIAN_AUTORUN", "false")

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_module  # noqa: E402
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)

DEMO_JOURNEY_ID = "22222222-2222-4222-8222-222222222222"
DEMO_TRAVELER_ID = "11111111-1111-4111-8111-111111111111"
DEMO_LOCKED_PLACE = "place-river-cruise"

# Capability codes the gate claims to interpret.
GATE_CAPABILITY_CODES = (
    "STEP_FREE_REQUIRED",
    "AVOID_STAIRS",
    "MAX_WALKING_PER_LEG_M",
    "MAX_CONTINUOUS_WALKING_MIN",
    "MAX_DAILY_WALKING_M",
    "MIDDAY_REST_WINDOW",
    "REST_EVERY_MIN",
    "INFANT_NAP_WINDOW",
    "TOILET_BREAK_EVERY_MIN",
    "STROLLER",
    "LARGE_LUGGAGE",
)


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def reset_demo() -> dict[str, Any]:
    r = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert r.status_code == 200, r.text
    return r.json()


def journey(journey_id: str = DEMO_JOURNEY_ID) -> dict[str, Any]:
    r = client.get(f"/api/v1/journeys/{journey_id}")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------
def get_traveler_capabilities(traveler_id: str = DEMO_TRAVELER_ID) -> dict[str, Any]:
    r = client.get(f"/api/v1/travelers/{traveler_id}/capabilities")
    assert r.status_code == 200, r.text
    return r.json()


def put_traveler_capabilities(payload: list[dict[str, Any]], traveler_id: str = DEMO_TRAVELER_ID):
    return client.put(f"/api/v1/travelers/{traveler_id}/capabilities", json=payload)


def confirm_traveler_capabilities(payload: list[dict[str, Any]], traveler_id: str = DEMO_TRAVELER_ID):
    return client.post(f"/api/v1/travelers/{traveler_id}/capabilities/confirm", json=payload)


def get_journey_capabilities(journey_id: str = DEMO_JOURNEY_ID) -> dict[str, Any]:
    r = client.get(f"/api/v1/journeys/{journey_id}/capabilities")
    assert r.status_code == 200, r.text
    return r.json()


def put_journey_capabilities(payload: list[dict[str, Any]], journey_id: str = DEMO_JOURNEY_ID):
    return client.put(f"/api/v1/journeys/{journey_id}/capabilities", json=payload)


def confirm_journey_capabilities(payload: list[dict[str, Any]], journey_id: str = DEMO_JOURNEY_ID):
    return client.post(f"/api/v1/journeys/{journey_id}/capabilities/confirm", json=payload)


def capability(code: str, severity: str = "HARD", **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "severity": severity, "params": {}}
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
def generate_plans(journey_id: str = DEMO_JOURNEY_ID, **payload: Any) -> tuple[int, Any]:
    r = client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json=payload or {})
    try:
        body = r.json()
    except ValueError:
        body = r.text
    return r.status_code, body


def list_plans(journey_id: str = DEMO_JOURNEY_ID) -> list[dict[str, Any]]:
    r = client.get(f"/api/v1/journeys/{journey_id}/plans")
    assert r.status_code == 200, r.text
    return r.json()


def select_plan(journey_id: str, plan_id: str):
    return client.post(f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select")


def plan_signatures(journey_id: str = DEMO_JOURNEY_ID) -> list[str]:
    """Stable fingerprint of every plan: (place_id, start_at) per activity."""
    return sorted(
        jdump(
            [
                [a.get("place_id") or a.get("title"), a.get("start_at")]
                for a in p.get("activities", [])
            ]
        )
        for p in list_plans(journey_id)
    )


def experience(journey_id: str = DEMO_JOURNEY_ID) -> dict[str, Any]:
    r = client.get(f"/api/v1/journeys/{journey_id}/experience")
    assert r.status_code == 200, r.text
    return r.json()


def journey_twin(journey_id: str = DEMO_JOURNEY_ID) -> dict[str, Any]:
    r = client.get(f"/api/v1/journeys/{journey_id}/twin")
    assert r.status_code == 200, r.text
    return r.json()


def traveler_twin(traveler_id: str = DEMO_TRAVELER_ID) -> dict[str, Any]:
    r = client.get(f"/api/v1/travelers/{traveler_id}/traveler-twin")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# constraints / events / replan
# ---------------------------------------------------------------------------
def list_constraints(journey_id: str = DEMO_JOURNEY_ID) -> list[dict[str, Any]]:
    r = client.get(f"/api/v1/journeys/{journey_id}/constraints")
    assert r.status_code == 200, r.text
    return r.json()


def add_constraint(journey_id: str, payload: dict[str, Any]):
    return client.post(f"/api/v1/journeys/{journey_id}/constraints", json=payload)


def post_event(journey_id: str, **payload: Any):
    body = {"severity": "MAJOR", "event_type": "CLOSURE"}
    body.update(payload)
    return client.post(f"/api/v1/journeys/{journey_id}/events", json=body)


def request_replan(journey_id: str, **payload: Any):
    return client.post(f"/api/v1/journeys/{journey_id}/replans", json=payload or {})


def list_replans(journey_id: str = DEMO_JOURNEY_ID) -> list[dict[str, Any]]:
    r = client.get(f"/api/v1/journeys/{journey_id}/replans")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# C4 protected commitment via the real production path
#   POST /journeys/{id}/inbox/import   -> ExternalReservationDraft
#   POST /journeys/{id}/inbox/{d}/confirm -> TransportReservation + Commitment(C4_Anchored)
# ---------------------------------------------------------------------------
def inbox_import(journey_id: str, reservation: dict[str, Any], artifact_type: str = "STRUCTURED_JSON"):
    return client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={"artifact_type": artifact_type, "payload": {"reservation": reservation}},
    )


def inbox_confirm(journey_id: str, draft_id: str):
    return client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft_id}/confirm")


def c4_reservation(
    *,
    confirmation_ref: str,
    departure_at: str,
    arrival_at: str,
    anchor_role: str = "ARRIVAL",
    reservation_type: str = "RAIL",
) -> dict[str, Any]:
    """A STRUCTURED_JSON reservation carrying every CRITICAL_RESERVATION_FIELD."""
    return {
        "reservation_type": reservation_type,
        "confirmation_ref": confirmation_ref,
        "origin": {"name": "Shanghai Hongqiao", "code": "SHH"},
        "destination": {"name": "Tianjin West", "code": "TJP"},
        "scheduled_departure_at": departure_at,
        "scheduled_arrival_at": arrival_at,
        "anchor_role": anchor_role,
        "provider_key": "mock-rail",
        "price_amount_minor": 128000,
        "currency": "CNY",
    }


def seed_c4_commitment(
    journey_id: str = DEMO_JOURNEY_ID,
    *,
    confirmation_ref: str = "C4-RAIL-0001",
    departure_at: str = "2026-10-10T08:00:00+08:00",
    arrival_at: str = "2026-10-10T12:30:00+08:00",
    anchor_role: str = "ARRIVAL",
) -> dict[str, Any]:
    """Create a genuine C4_Anchored commitment and return the full trace."""
    imp = inbox_import(
        journey_id,
        c4_reservation(
            confirmation_ref=confirmation_ref,
            departure_at=departure_at,
            arrival_at=arrival_at,
            anchor_role=anchor_role,
        ),
    )
    out: dict[str, Any] = {"import_status": imp.status_code}
    if imp.status_code not in (200, 201):
        out["import_body"] = imp.text[:400]
        return out
    draft = imp.json()
    out["draft_id"] = draft.get("id")
    out["draft_status"] = draft.get("status")
    out["unresolved_fields"] = draft.get("unresolved_fields")
    conf = inbox_confirm(journey_id, draft["id"])
    out["confirm_status"] = conf.status_code
    if conf.status_code == 200:
        body = conf.json()
        out["commitment"] = body.get("commitment")
        out["reservation_id"] = (body.get("reservation") or {}).get("id")
    else:
        out["confirm_body"] = conf.text[:400]
    out["commitments"] = commitment_rows(journey_id)
    return out


# ---------------------------------------------------------------------------
# direct DB readers (no audit HTTP surface exists in this build)
# ---------------------------------------------------------------------------
def _session():
    from app.db.session import SessionLocal

    return SessionLocal()


def audit_rows(action_like: str | None = None) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db.models import AuditLog

    with _session() as session:
        rows = list(session.scalars(select(AuditLog).order_by(AuditLog.created_at)).all())
        out = [
            {
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "journey_id": r.journey_id,
                "actor_type": r.actor_type,
                "actor_id": r.actor_id,
                "before": json.loads(r.before_json) if r.before_json else None,
                "after": json.loads(r.after_json) if r.after_json else None,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    if action_like:
        out = [r for r in out if action_like in (r["action"] or "")]
    return out


def capability_rows(scope: str | None = None) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db.models import TravelerCapability

    with _session() as session:
        rows = list(session.scalars(select(TravelerCapability)).all())
        out = [
            {
                "code": r.code,
                "severity": r.severity,
                "class": r.capability_class,
                "scope": r.scope,
                "journey_id": r.journey_id,
                "active": bool(r.active),
                "confirmed": bool(r.confirmed),
                "explicit": bool(r.explicit),
                "source": r.source,
                "provenance": r.provenance,
                "params": json.loads(r.params_json or "{}"),
            }
            for r in rows
        ]
    if scope:
        out = [r for r in out if r["scope"] == scope]
    return out


def constraint_rows(journey_id: str = DEMO_JOURNEY_ID) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db.models import JourneyConstraint

    with _session() as session:
        rows = list(
            session.scalars(
                select(JourneyConstraint).where(JourneyConstraint.journey_id == journey_id)
            ).all()
        )
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "category": r.category,
                "rule_code": r.rule_code,
                "params": json.loads(r.params_json or "{}") if getattr(r, "params_json", None) else {},
                "active": bool(r.active),
                "source": getattr(r, "source", None),
            }
            for r in rows
        ]


def commitment_rows(journey_id: str = DEMO_JOURNEY_ID) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db.models import Commitment

    with _session() as session:
        rows = list(
            session.scalars(select(Commitment).where(Commitment.journey_id == journey_id)).all()
        )
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "level": r.level,
                "status": r.status,
                "activity_id": r.activity_id,
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# agent / hermes
# ---------------------------------------------------------------------------
def agent_tools(version: str = "") -> list[dict[str, Any]]:
    suffix = f"/{version}" if version else ""
    r = client.get(f"/api/v1/agent/tools{suffix}")
    assert r.status_code == 200, r.text
    body = r.json()
    # The endpoint returns a bare list; older builds wrapped it in {"tools": [...]}.
    if isinstance(body, dict):
        tools = body.get("tools")
        return tools if isinstance(tools, list) else []
    return body if isinstance(body, list) else []


def agent_tool(tool: str, arguments: dict[str, Any]):
    return client.post(f"/api/v1/agent/tools/{tool}", json=arguments)


def agent_tool_v2(tool: str, arguments: dict[str, Any]):
    return client.post(f"/api/v1/agent/tools/v2/{tool}", json=arguments)


def agent_command(journey_id: str, command: str):
    return client.post(f"/api/v1/journeys/{journey_id}/agent/commands", json={"command": command})


def copilot(journey_id: str, message: str):
    return client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages", json={"message": message}
    )
