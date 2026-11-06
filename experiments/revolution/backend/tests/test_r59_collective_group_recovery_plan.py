from app.agent.mission_plan import build_mission_plan


def test_r59_collective_group_recovery_plan_only_can_open_member_decision_without_mutation_tools():
    plan = build_mission_plan(
        autonomy="PLAN_ONLY",
        objective_codes=["COLLECTIVE_JOURNEY_RECOVERY"],
    )

    assert "request_replan" in plan.allowed_tools
    assert "request_collective_decision" in plan.allowed_tools
    assert "accept_replan" not in plan.allowed_tools
    assert "activate_standby" not in plan.allowed_tools
    assert "replace_activity" not in plan.allowed_tools
    assert "reschedule_activity" not in plan.allowed_tools
    assert "create_booking" not in plan.allowed_tools
    assert "cancel_refundable_booking" not in plan.allowed_tools
    assert "request_commitment_change" not in plan.allowed_tools

    collective_steps = [
        step
        for step in plan.steps
        if step.tool_name == "request_collective_decision"
    ]
    assert len(collective_steps) == 1
    assert collective_steps[0].requires_authority is True


def test_r59_unrelated_plan_only_mission_does_not_gain_collective_decision_capability():
    plan = build_mission_plan(
        autonomy="PLAN_ONLY",
        objective_codes=["RECOVER_FROM_CITY_DISRUPTION"],
    )

    assert "request_replan" in plan.allowed_tools
    assert "request_collective_decision" not in plan.allowed_tools
    assert all(
        step.tool_name != "request_collective_decision"
        for step in plan.steps
    )


def test_r59_collective_plus_live_intent_keeps_strategy_tools_but_still_cannot_execute():
    plan = build_mission_plan(
        autonomy="PLAN_ONLY",
        objective_codes=[
            "COLLECTIVE_JOURNEY_RECOVERY",
            "LIVE_JOURNEY_INTENT_STEERING",
        ],
    )

    assert "acquire_place_facts" in plan.allowed_tools
    assert "generate_plans" in plan.allowed_tools
    assert "request_replan" in plan.allowed_tools
    assert "request_collective_decision" in plan.allowed_tools
    assert "accept_replan" not in plan.allowed_tools


def test_r59_material_world_change_changes_collective_decision_epoch():
    from app.collective_mediation import collective_decision_epoch

    shared = {
        "journey_id": "journey-r59",
        "mission_id": "mission-r59",
        "trigger_event_id": "event-r59",
        "base_plan_version_id": "plan-base",
        "required_member_ids": ["member-a", "member-b"],
        "members": [
            {"member_id": "member-a", "role": "OWNER", "capabilities": ["JOURNEY_UPDATE"]},
            {"member_id": "member-b", "role": "ADMIN", "capabilities": ["JOURNEY_UPDATE"]},
        ],
    }
    option = {
        "proposal_id": "proposal-r59",
        "plan_id": "plan-candidate",
        "status": "GROUNDED_FEASIBLE",
        "fit_subject_place_ids": ["place-option"],
        "fit_subject_tags": ["garden"],
        "member_fits": {},
        "fairness": {"minimum_member_fit": 0.4, "maximum_regret": 0.2},
        "material_world_epoch": "world-before",
    }
    first = collective_decision_epoch(**shared, options=[option])
    option["material_world_epoch"] = "world-after"
    second = collective_decision_epoch(**shared, options=[option])
    assert first != second
