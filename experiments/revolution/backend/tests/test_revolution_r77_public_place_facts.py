from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.models import Place
from app.db.session import SessionLocal, ensure_schema
from app.main import app


def test_public_places_projects_identity_and_place_fact_evidence_without_free_coercion():
    ensure_schema()
    city = f"R77-PUBLIC-FACTS-{uuid4().hex}"
    with SessionLocal() as session:
        session.add(
            Place(
                id=f"{city}-unknown",
                provider_key="overpass",
                external_id="overpass:node/4246892594",
                city_code=city,
                name="狮子林",
                category="attraction",
                latitude=31.3234041,
                longitude=120.6247017,
                opening_hours_json=json.dumps(
                    {"status": "NORMALIZED_VALID", "open": "07:30", "close": "17:30"}
                ),
                tags_json="[]",
                metadata_json=json.dumps(
                    {
                        "cost_minor": 0,
                        "price_known": False,
                        "cost_evidence": {
                            "status": "UNKNOWN",
                            "amount_minor": None,
                            "currency": "CNY",
                            "source": "overpass",
                        },
                    }
                ),
                updated_at="2026-09-30T00:00:00+00:00",
            )
        )
        session.commit()

    response = TestClient(app).get("/api/v1/places", params={"city_code": city})

    assert response.status_code == 200, response.text
    row = response.json()[0]
    assert row["external_id"] == "overpass:node/4246892594"
    assert row["opening_hours"]["status"] == "NORMALIZED_VALID"
    assert row["cost_evidence"]["status"] == "UNKNOWN"
    assert row["cost_evidence"]["amount_minor"] is None
    assert row["cost_evidence"].get("amount_minor") != 0
