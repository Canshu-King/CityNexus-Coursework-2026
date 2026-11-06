"""Pin the two projection contracts the pass-16 arbitration asked for.

`V14PreventivePanel` used to take `runs[0]` as "the newest preventive run" and to
stop polling the moment any run appeared.  The arbitration ruled that neither may
rest on a guess:

  * if the endpoint guarantees newest-first, that guarantee needs a test;
  * if it does not, the panel must select by `created_at`.

It does guarantee it - `guardian_status` sorts `created_at desc, id desc` - so the
ordering is pinned here, and the panel still selects by `created_at` rather than by
position, so a future change to that sort cannot make the surface describe a stale
run.

The second case is the lifecycle one: a `GuardianRun` is written RUNNING/PLANNING
before the recomposition lands, so "a run exists" must never be treated as "the
story is finished".  The panel's stop condition is asserted against the payload it
actually receives.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient

import app.main as main_module
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def _guardian_runs() -> tuple[str, list[dict[str, Any]]]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select")
    client.post(f"/api/v1/journeys/{journey_id}/start")
    client.put(
        f"/api/v1/journeys/{journey_id}/guardian/mission",
        json={
            "command": "Protect the cruise, prefer indoor alternatives.",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    # A scan is the reactive controller: with no open event it answers NO_ACTION,
    # and re-scanning the same state is deduplicated.  Prevention is the autopilot's
    # cycle, so enrol the journey and let it run.
    assert client.post(f"/api/v1/journeys/{journey_id}/guardian/scan").status_code == 200
    assert (
        client.post(
            f"/api/v1/journeys/{journey_id}/guardian/provider-fixture",
            json={"state": "RISK_FORECAST", "payload": {"risk_score": 88}},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/journeys/{journey_id}/guardian/autopilot/start", json={"interval_seconds": 0.5}
        ).status_code
        == 200
    )
    runs = []
    for _ in range(120):
        runs = client.get(f"/api/v1/journeys/{journey_id}/guardian/status").json()["runs"]
        # Wait for the run to finish, not merely to appear: it is written
        # RUNNING/PLANNING first, which is the very fact this file is about.
        if any(
            item["decision"] == "PREEMPTIVE_RECOVERY"
            and item["trigger_kind"] == "PREDICTIVE"
            and item["status"] == "COMPLETED"
            for item in runs
        ):
            break
        time.sleep(0.5)
    client.post(f"/api/v1/journeys/{journey_id}/guardian/autopilot/stop", json={})
    return journey_id, runs


def test_guardian_status_lists_runs_newest_first() -> None:
    """The panel may not guess which end of the list is current.

    Either this ordering is a contract, or the consumer has to sort.  It is a
    contract, so it is pinned; the panel sorts anyway, which costs nothing and
    stops a future re-sort from silently describing an old run.
    """
    _, runs = _guardian_runs()
    assert len(runs) >= 2, (
        "two guardian cycles produced fewer than two runs, so this proves nothing "
        "about ordering"
    )
    stamps = [datetime.fromisoformat(str(item["created_at"])) for item in runs]
    assert stamps == sorted(stamps, reverse=True), (
        "/guardian/status stopped guaranteeing newest-first; a consumer reading "
        f"runs[0] would now be reading an arbitrary run: {stamps}"
    )
    ids = [item["id"] for item in runs]
    assert len(set(ids)) == len(ids), "the same run appears twice in the status list"


def test_a_preventive_run_carries_the_fields_the_panel_selects_on() -> None:
    """`created_at` and `status` are what the panel branches on.

    It stops polling only on COMPLETED, so if the endpoint ever stopped reporting
    a terminal status the panel would poll forever with no signal that anything is
    wrong.
    """
    _, runs = _guardian_runs()
    for item in runs:
        assert item.get("created_at"), "a run without created_at cannot be ordered safely"
        assert item.get("status") in {"COMPLETED", "RUNNING", "FAILED_RETRYABLE"}, item.get("status")
    preventive = [
        item
        for item in runs
        if item["decision"] == "PREEMPTIVE_RECOVERY" and item["trigger_kind"] == "PREDICTIVE"
    ]
    assert preventive, "the forecast cycle produced no preventive run to inspect"
    newest = max(preventive, key=lambda item: str(item["created_at"]))
    assert newest["status"] == "COMPLETED", (
        "a preventive run that never reaches a terminal status would keep the panel "
        "polling forever"
    )
    # The honest contract: once the run is terminal, its action_status is no
    # longer the in-flight PLANNING.  Which settled value it holds depends on the
    # traveller's policy (this journey is not AUTONOMOUS, so it is gated to
    # REQUIRE_CONFIRMATION), and inventing an allowlist here would only ever
    # encode what the test author guessed.
    assert newest["action_status"] != "PLANNING", (
        "a COMPLETED run still reports PLANNING, so the panel would render a "
        "finished action as half-done"
    )
    assert newest["action_status"], newest
