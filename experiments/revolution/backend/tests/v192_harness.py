"""V19.2 Real-Phone Shadow-Trial Gate harness (tests-only, no production changes).

Self-locating: every path is derived from this file's own location, so the same
bytes work in any checkout.  Bytecode writing is disabled so nothing is written
back into the worktree, and the throwaway SQLite database lives in the system
temp directory rather than inside the repository.

Import:
    import v192_harness as H

    H.caps()                              -> the ``app.capabilities`` module
    H.extract("我可能需要轮椅")             -> capability rows parsed from raw language
    H.requirement_state("STEP_FREE_REQUIRED", {...}) -> evidence verdict
    H.client().post("/api/v1/...")        -> in-process HTTP against a throwaway DB
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# Imported before the sys.path work below: `demo_stage` reads only `os.environ`, so it cannot
# drag `app.main` in at a moment when this harness has not finished declaring the environment.
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]          # <repo>/backend
REPO = HERE.parents[2]             # <repo>

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEMO_JOURNEY_ID = "22222222-2222-4222-8222-222222222222"
DEMO_TRAVELER_ID = "11111111-1111-4111-8111-111111111111"

_client = None
_app_module = None


# ---------------------------------------------------------------------------
# bootstrap (lazy: never re-configures an app someone else already imported)
# ---------------------------------------------------------------------------
def _configure_env() -> Path:
    d = Path(tempfile.gettempdir()) / "v192_shadow_trial"
    d.mkdir(parents=True, exist_ok=True)
    db = d / "v192_gate.db"
    os.environ.setdefault("CITYNEXUS_DATABASE_URL", f"sqlite:///{db}")
    os.environ.setdefault("CITYNEXUS_ENV", "test")
    os.environ.setdefault("CITYNEXUS_AI_ENABLED", "false")
    os.environ.setdefault("CITYNEXUS_GUARDIAN_AUTORUN", "false")
    return db


def app_main():
    """The imported ``app.main`` module (imported once, on first use)."""
    global _app_module
    if _app_module is None:
        if "app.main" not in sys.modules:
            _configure_env()
        import app.main as main_module

        _app_module = main_module
    return _app_module


def client():
    """A ``TestClient`` over the app; created once."""
    global _client
    if _client is None:
        from fastapi.testclient import TestClient

        _client = TestClient(app_main().app)
    return _client


def safe_client():
    """Same client, but a 500 is returned as a response instead of raised."""
    from fastapi.testclient import TestClient

    return TestClient(app_main().app, raise_server_exceptions=False)


def caps():
    """The ``app.capabilities`` module (raw-language -> capability rows)."""
    import app.capabilities as module

    return module


# ---------------------------------------------------------------------------
# pure capability-layer helpers
# ---------------------------------------------------------------------------
def extract(text: str, scope: str = "JOURNEY") -> list[dict[str, Any]]:
    return list(caps().extract_capabilities_from_text(text, scope=scope))


def codes(items: list[dict[str, Any]]) -> list[str]:
    return sorted({i["code"] for i in items})


def params_of(items: list[dict[str, Any]], code: str) -> dict[str, Any]:
    item = next((i for i in items if i["code"] == code), None)
    return dict(item.get("params") or {}) if item else {}


def severity_of(items: list[dict[str, Any]], code: str) -> str | None:
    item = next((i for i in items if i["code"] == code), None)
    return item.get("severity") if item else None


def canon(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(caps().canonicalize_capabilities(items))


def requirement_state(code: str, facts: dict[str, Any]) -> str:
    return caps().evaluate_accessibility_requirement(code, facts)


def normalize_tags(value: Any) -> Any:
    return caps().normalize_osm_tags(value)


def capability(code: str, severity: str = "HARD", **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "severity": severity, "params": {}}
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def reset_demo() -> dict[str, Any]:
    # ORD-0389: the whole-library reset needs the operator's token; this harness declares one
    # through conftest and hands it here, so the gate is exercised rather than worked around.
    r = client().post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert r.status_code == 200, r.text
    return r.json()


def journey(journey_id: str = DEMO_JOURNEY_ID) -> dict[str, Any]:
    r = client().get(f"/api/v1/journeys/{journey_id}")
    assert r.status_code == 200, r.text
    return r.json()


def get_journey_capabilities(journey_id: str = DEMO_JOURNEY_ID) -> dict[str, Any]:
    r = client().get(f"/api/v1/journeys/{journey_id}/capabilities")
    assert r.status_code == 200, r.text
    return r.json()


def put_journey_capabilities(payload: list[dict[str, Any]], journey_id: str = DEMO_JOURNEY_ID):
    return client().put(f"/api/v1/journeys/{journey_id}/capabilities", json=payload)


def confirm_journey_capabilities(payload: list[dict[str, Any]], journey_id: str = DEMO_JOURNEY_ID):
    return client().post(f"/api/v1/journeys/{journey_id}/capabilities/confirm", json=payload)


def put_traveler_capabilities(payload: list[dict[str, Any]], traveler_id: str = DEMO_TRAVELER_ID):
    return client().put(f"/api/v1/travelers/{traveler_id}/capabilities", json=payload)


def confirm_traveler_capabilities(payload: list[dict[str, Any]], traveler_id: str = DEMO_TRAVELER_ID):
    return client().post(f"/api/v1/travelers/{traveler_id}/capabilities/confirm", json=payload)


def generate_plans(journey_id: str = DEMO_JOURNEY_ID, **payload: Any):
    r = client().post(f"/api/v1/journeys/{journey_id}/plans/generate", json=payload or {})
    try:
        body = r.json()
    except ValueError:
        body = r.text
    return r.status_code, body


def list_plans(journey_id: str = DEMO_JOURNEY_ID) -> list[dict[str, Any]]:
    r = client().get(f"/api/v1/journeys/{journey_id}/plans")
    assert r.status_code == 200, r.text
    body = r.json()
    return body if isinstance(body, list) else []


def select_plan(journey_id: str, plan_id: str):
    return client().post(f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select")


def start_journey(journey_id: str = DEMO_JOURNEY_ID):
    return client().post(f"/api/v1/journeys/{journey_id}/start")


def experience(journey_id: str = DEMO_JOURNEY_ID) -> dict[str, Any]:
    r = client().get(f"/api/v1/journeys/{journey_id}/experience")
    assert r.status_code == 200, r.text
    return r.json()


def list_replans(journey_id: str = DEMO_JOURNEY_ID) -> list[dict[str, Any]]:
    r = client().get(f"/api/v1/journeys/{journey_id}/replans")
    assert r.status_code == 200, r.text
    body = r.json()
    return body if isinstance(body, list) else []


def list_constraints(journey_id: str = DEMO_JOURNEY_ID) -> list[dict[str, Any]]:
    r = client().get(f"/api/v1/journeys/{journey_id}/constraints")
    assert r.status_code == 200, r.text
    body = r.json()
    return body if isinstance(body, list) else []


# ---------------------------------------------------------------------------
# C4 protected commitment through the real production path
# ---------------------------------------------------------------------------
def c4_reservation(
    *,
    confirmation_ref: str,
    departure_at: str,
    arrival_at: str,
    anchor_role: str = "ARRIVAL",
    reservation_type: str = "RAIL",
    semantic_node_key: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "reservation_type": reservation_type,
        "confirmation_ref": confirmation_ref,
        "origin": {"name": "Shanghai Hongqiao", "code": "SHH"},
        "destination": {"name": "Nexus City", "code": "CNX"},
        "scheduled_departure_at": departure_at,
        "scheduled_arrival_at": arrival_at,
        "anchor_role": anchor_role,
        "provider_key": "mock-rail",
        "price_amount_minor": 128000,
        "currency": "CNY",
    }
    if semantic_node_key:
        body["semantic_node_key"] = semantic_node_key
    return body


def inbox_import(journey_id: str, reservation: dict[str, Any], artifact_type: str = "STRUCTURED_JSON"):
    return client().post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={"artifact_type": artifact_type, "payload": {"reservation": reservation}},
    )


def inbox_confirm(journey_id: str, draft_id: str):
    return client().post(f"/api/v1/journeys/{journey_id}/inbox/{draft_id}/confirm")


# ---------------------------------------------------------------------------
# direct DB readers/writers (throwaway database only)
# ---------------------------------------------------------------------------
def _session():
    from app.db.session import SessionLocal

    return SessionLocal()


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


def clear_capabilities() -> None:
    """Delete every capability row (throwaway database only)."""
    from sqlalchemy import delete

    from app.db.models import TravelerCapability

    with _session() as session:
        session.execute(delete(TravelerCapability))
        session.commit()


def set_place_evidence(accessibility: dict[str, Any] | None) -> int:
    """Write (or clear) accessibility evidence on every place; returns the count."""
    from sqlalchemy import select

    from app.db.models import Place

    with _session() as session:
        rows = list(session.scalars(select(Place)).all())
        for row in rows:
            meta = json.loads(row.metadata_json or "{}")
            if accessibility is None:
                meta.pop("accessibility", None)
            else:
                meta["accessibility"] = accessibility
            row.metadata_json = json.dumps(meta, ensure_ascii=False)
        session.commit()
        return len(rows)


def plan_signatures(journey_id: str = DEMO_JOURNEY_ID) -> list[str]:
    """Stable fingerprint of every plan: (place_id, start_at) per activity."""
    return sorted(
        json.dumps(
            [[a.get("place_id") or a.get("title"), a.get("start_at")] for a in p.get("activities", [])],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        for p in list_plans(journey_id)
    )


def plan_evidence_json(plan_id: str) -> str:
    from app.db.models import PlanVersion

    with _session() as session:
        row = session.get(PlanVersion, plan_id)
        return (row.mobility_evidence_json or "") if row is not None else ""


def set_plan_evidence_json(plan_id: str, raw: str) -> None:
    from app.db.models import PlanVersion

    with _session() as session:
        row = session.get(PlanVersion, plan_id)
        if row is None:
            raise AssertionError(f"plan not found: {plan_id}")
        row.mobility_evidence_json = raw
        session.commit()


def expire_plan_evidence(plan_id: str) -> str:
    """Mark a plan's frozen evidence as expired; returns the previous JSON."""
    from datetime import datetime, timedelta, timezone

    original = plan_evidence_json(plan_id)
    raw = json.loads(original or "{}")
    raw["expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    set_plan_evidence_json(plan_id, json.dumps(raw))
    return original


def validate_plan(session, journey_row, plan_row, **kwargs: Any):
    """Feasibility verdict for one plan (single source of truth)."""
    from app.services import validate_plan_feasibility

    return validate_plan_feasibility(session, journey_row, plan_row, **kwargs)
