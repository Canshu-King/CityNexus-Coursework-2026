from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from app.agent.autorun import enrolled_journey_ids, ensure_enrollment
from app.db.models import Base, Journey, Traveler
from app.db.session import make_engine


def test_v6_autopilot_enrollment_is_independent_per_journey() -> None:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    LocalSession = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(timezone.utc).isoformat()
    traveler_id = str(uuid4())
    journey_a_id = str(uuid4())
    journey_b_id = str(uuid4())

    with LocalSession() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="V6 enrollment traveler",
                email=None,
                locale="en-US",
                timezone="Asia/Shanghai",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
        session.add_all(
            [
                Journey(
                    id=journey_a_id,
                    traveler_id=traveler_id,
                    title="Journey A",
                    city_code="TSN",
                    timezone="Asia/Shanghai",
                    start_at="2026-10-10T09:00:00+08:00",
                    end_at="2026-10-10T21:00:00+08:00",
                    budget_currency="CNY",
                    budget_amount_minor=10000,
                    planning_mode="balanced",
                    status="ACTIVE",
                    active_plan_version_id=None,
                    current_activity_id=None,
                    created_at=now,
                    updated_at=now,
                ),
                Journey(
                    id=journey_b_id,
                    traveler_id=traveler_id,
                    title="Journey B",
                    city_code="TSN",
                    timezone="Asia/Shanghai",
                    start_at="2026-10-11T09:00:00+08:00",
                    end_at="2026-10-11T21:00:00+08:00",
                    budget_currency="CNY",
                    budget_amount_minor=10000,
                    planning_mode="balanced",
                    status="ACTIVE",
                    active_plan_version_id=None,
                    current_activity_id=None,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.commit()

        journey_a = session.get(Journey, journey_a_id)
        journey_b = session.get(Journey, journey_b_id)
        assert journey_a is not None and journey_b is not None
        ensure_enrollment(session, journey_a, interval_seconds=5)
        session.commit()
        assert enrolled_journey_ids(session) == [journey_a_id]

        ensure_enrollment(session, journey_b, interval_seconds=7)
        session.commit()
        assert set(enrolled_journey_ids(session)) == {journey_a_id, journey_b_id}

        ensure_enrollment(session, journey_a, enabled=False)
        session.commit()
        assert enrolled_journey_ids(session) == [journey_b_id]
