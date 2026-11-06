from datetime import datetime, timedelta, timezone

from app.intelligence.replan_diff import PlanActivitySnapshot, diff_plans


def test_replan_diff_handles_duplicate_semantic_places_without_dict_collapse():
    base = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
    old = [
        PlanActivitySnapshot("cafe", base, base + timedelta(minutes=30), 1000),
        PlanActivitySnapshot(
            "cafe", base + timedelta(hours=2), base + timedelta(hours=2, minutes=30), 1000
        ),
        PlanActivitySnapshot(
            "cruise",
            base + timedelta(hours=6, minutes=30),
            base + timedelta(hours=7, minutes=30),
            12000,
            True,
        ),
    ]
    new = [
        PlanActivitySnapshot(
            "cafe", base + timedelta(minutes=10), base + timedelta(minutes=40), 1000
        ),
        PlanActivitySnapshot(
            "cafe", base + timedelta(hours=2), base + timedelta(hours=2, minutes=30), 1000
        ),
        PlanActivitySnapshot(
            "cruise",
            base + timedelta(hours=6, minutes=30),
            base + timedelta(hours=7, minutes=30),
            12000,
            True,
        ),
    ]
    diff = diff_plans(old, new)
    assert len(diff.moved) == 1
    assert diff.moved[0].semantic_key == "cafe"
    assert diff.preserved_protected == ("cruise",)
