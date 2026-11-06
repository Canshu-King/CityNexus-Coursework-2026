from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Place
from app.db.session import SessionLocal
from app.main import app
from app.replanning.mock_replan_engine import choose_closure_replacement
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


def test_canonical_tech_art_closure_prefers_design_library():
    client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    with SessionLocal() as session:
        places = session.scalars(select(Place).order_by(Place.id)).all()
        affected = session.get(Place, "place-tech-art")
        choice = choose_closure_replacement(
            affected_place=affected,
            candidate_places=places,
            occupied_place_ids={
                "place-old-town",
                "place-riverside-museum",
                "place-food-hall",
                "place-tech-art",
                "place-river-cruise",
                "place-skyline-dinner",
            },
            max_duration_min=80,
        )
        assert choice is not None
        assert choice.place_id == "place-design-library"
        assert choice.duration_min == 60
        assert choice.cost_minor == 2000
