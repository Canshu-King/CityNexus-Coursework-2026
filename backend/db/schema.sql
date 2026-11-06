PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS travelers (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    email TEXT,
    locale TEXT NOT NULL DEFAULT 'en-GB',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    id TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    preference_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0.5 CHECK (weight >= 0.0 AND weight <= 1.0),
    source TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(traveler_id, category, preference_key)
);

CREATE TABLE IF NOT EXISTS journey_outcome_events (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    occurred_at_actual TEXT NOT NULL,
    planned_start_at TEXT,
    planned_end_at TEXT,
    source TEXT NOT NULL DEFAULT 'DOMAIN',
    related_proposal_id TEXT,
    related_agent_run_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS traveler_preference_evidence (
    id TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
    journey_id TEXT REFERENCES journeys(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    preference_dimension TEXT NOT NULL,
    direction TEXT NOT NULL,
    strength REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    explanation TEXT NOT NULL DEFAULT '',
    related_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
);

CREATE TABLE IF NOT EXISTS traveler_capabilities (
    id TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
    journey_id TEXT REFERENCES journeys(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    capability_class TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('HARD','STRONG','SOFT')),
    params_json TEXT NOT NULL DEFAULT '{}',
    scope TEXT NOT NULL CHECK (scope IN ('TRAVELER','JOURNEY')),
    source TEXT NOT NULL DEFAULT 'USER_CONFIRMED',
    provenance TEXT NOT NULL DEFAULT 'USER_STATED',
    explicit INTEGER NOT NULL DEFAULT 1 CHECK (explicit IN (0,1)),
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(traveler_id, journey_id, scope, code)
);

CREATE TABLE IF NOT EXISTS traveler_twin_snapshots (
    traveler_id TEXT PRIMARY KEY REFERENCES travelers(id) ON DELETE CASCADE,
    explicit_preferences_json TEXT NOT NULL DEFAULT '[]',
    learned_soft_preferences_json TEXT NOT NULL DEFAULT '[]',
    conflicts_json TEXT NOT NULL DEFAULT '[]',
    explicit_capabilities_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcome_events_journey ON journey_outcome_events(journey_id, occurred_at_actual);
CREATE INDEX IF NOT EXISTS idx_preference_evidence_traveler ON traveler_preference_evidence(traveler_id, status, created_at);

CREATE TABLE IF NOT EXISTS traveler_agent_policies (
    traveler_id TEXT PRIMARY KEY REFERENCES travelers(id) ON DELETE CASCADE,
    mode TEXT NOT NULL DEFAULT 'ADVISORY' CHECK (mode IN ('ADVISORY','ASSISTED','AUTONOMOUS')),
    auto_accept_replan INTEGER NOT NULL DEFAULT 0 CHECK (auto_accept_replan IN (0,1)),
    max_auto_replan_cost_delta_minor INTEGER NOT NULL DEFAULT 0 CHECK (max_auto_replan_cost_delta_minor >= 0),
    max_auto_loss_minor INTEGER NOT NULL DEFAULT 0 CHECK (max_auto_loss_minor >= 0),
    max_auto_schedule_drift_min INTEGER NOT NULL DEFAULT 0 CHECK (max_auto_schedule_drift_min >= 0),
    allowed_activity_types_json TEXT NOT NULL DEFAULT '[]',
    allowed_booking_types_json TEXT NOT NULL DEFAULT '[]',
    protected_commitment_levels_json TEXT NOT NULL DEFAULT '["C3_Guaranteed","C4_Anchored"]',
    allow_auto_commitment_management INTEGER NOT NULL DEFAULT 0 CHECK (allow_auto_commitment_management IN (0,1)),
    allow_auto_when_provider_degraded INTEGER NOT NULL DEFAULT 0 CHECK (allow_auto_when_provider_degraded IN (0,1)),
    allow_auto_on_critical_event INTEGER NOT NULL DEFAULT 0 CHECK (allow_auto_on_critical_event IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journeys (
    id TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    city_code TEXT NOT NULL,
    timezone TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    budget_currency TEXT NOT NULL,
    budget_amount_minor INTEGER NOT NULL CHECK (budget_amount_minor >= 0),
    planning_mode TEXT NOT NULL DEFAULT 'balanced' CHECK (planning_mode IN ('balanced','budget','explore','strict_schedule')),
    status TEXT NOT NULL CHECK (status IN ('DRAFT','PLANNED','ACTIVE','COMPLETED','CANCELLED')),
    active_plan_version_id TEXT REFERENCES plan_versions(id) ON DELETE SET NULL,
    current_activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- ORD-0383 slice 1: additive and deliberately without a DEFAULT.  A row that was never
    -- deleted is NULL, which is what every pre-existing row already is; a default would have
    -- to invent a deletion moment.  See `deleted_at` in `app/db/models.py`.
    deleted_at TEXT,
    -- ORD-0520: the same additive shape for 中止.  `status` already had the word (`CANCELLED`
    -- in the CHECK above, written by no door until this order), but a status cannot carry why,
    -- and the reason is the traveller's own sentence -- it belongs to the row, not to a screen.
    -- Both stay NULL for a trip that was never aborted, so no pre-existing row acquires a
    -- moment or a reason it never gave.
    aborted_at TEXT,
    abort_reason TEXT,
    CHECK (end_at > start_at)
);

CREATE TABLE IF NOT EXISTS constraints (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('HARD','SOFT')),
    category TEXT NOT NULL,
    rule_code TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    weight REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0.0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_versions (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL CHECK (version_no >= 1),
    planning_mode TEXT NOT NULL CHECK (planning_mode IN ('balanced','budget','explore','strict_schedule')),
    status TEXT NOT NULL CHECK (status IN ('CANDIDATE','SELECTED','ACTIVE','SUPERSEDED','REJECTED')),
    source TEXT NOT NULL CHECK (source IN ('INITIAL','REPLAN','MANUAL')),
    parent_plan_version_id TEXT REFERENCES plan_versions(id) ON DELETE SET NULL,
    total_cost_minor INTEGER NOT NULL DEFAULT 0 CHECK (total_cost_minor >= 0),
    currency TEXT NOT NULL,
    estimated_duration_min INTEGER NOT NULL DEFAULT 0 CHECK (estimated_duration_min >= 0),
    utility_score REAL,
    score_breakdown_json TEXT,
    explanation_json TEXT,
    mobility_snapshot_id TEXT,
    mobility_evidence_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(journey_id, version_no)
);

CREATE TABLE IF NOT EXISTS places (
    id TEXT PRIMARY KEY,
    provider_key TEXT NOT NULL,
    external_id TEXT NOT NULL,
    city_code TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    opening_hours_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    -- ORD-0095-1 / arbitration R4: every geometry stored here is WGS-84, and the row says so
    -- instead of leaving that to memory.  `source_coordinate_system` records the CRS a provider
    -- handed us (a future GCJ-02 source), so an inbound conversion can never be mistaken for
    -- the system the *stored* geometry is in.  Existing rows are backfilled by the default.
    coordinate_system TEXT NOT NULL DEFAULT 'WGS84',
    source_coordinate_system TEXT,
    UNIQUE(provider_key, external_id)
);

CREATE TABLE IF NOT EXISTS city_services (
    id TEXT PRIMARY KEY,
    provider_key TEXT NOT NULL,
    external_id TEXT NOT NULL,
    place_id TEXT REFERENCES places(id) ON DELETE SET NULL,
    city_code TEXT NOT NULL,
    service_type TEXT NOT NULL,
    name TEXT NOT NULL,
    availability_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    UNIQUE(provider_key, external_id)
);

CREATE TABLE IF NOT EXISTS activities (
    id TEXT PRIMARY KEY,
    plan_version_id TEXT NOT NULL REFERENCES plan_versions(id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 1),
    activity_type TEXT NOT NULL,
    title TEXT NOT NULL,
    place_id TEXT REFERENCES places(id) ON DELETE SET NULL,
    city_service_id TEXT REFERENCES city_services(id) ON DELETE SET NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    travel_from_prev_min INTEGER NOT NULL DEFAULT 0 CHECK (travel_from_prev_min >= 0),
    estimated_cost_minor INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_minor >= 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PLANNED','READY','NOT_STARTED','EN_ROUTE','ARRIVED','IN_PROGRESS','ACTIVE','COMPLETED','SKIPPED','CANCELLED')),
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_version_id, sequence_no),
    CHECK (end_at > start_at)
);

CREATE TABLE IF NOT EXISTS commitments (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN ('BOOKING','ORDER','PAYMENT','APPOINTMENT','OTHER')),
    level TEXT NOT NULL CHECK (level IN ('C0_Flexible','C1_Preferred','C2_Reserved','C3_Guaranteed','C4_Anchored')),
    status TEXT NOT NULL CHECK (status IN ('PROPOSED','ACTIVE','FULFILLED','CANCELLED','BREACHED')),
    monetary_value_minor INTEGER NOT NULL DEFAULT 0 CHECK (monetary_value_minor >= 0),
    monetary_value_status TEXT NOT NULL DEFAULT 'KNOWN',
    currency TEXT NOT NULL,
    external_ref TEXT,
    policy_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    commitment_id TEXT REFERENCES commitments(id) ON DELETE SET NULL,
    provider_key TEXT NOT NULL,
    external_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('DRAFT','PENDING','CONFIRMED','COMPLETED','CANCELLED','FAILED')),
    amount_minor INTEGER NOT NULL DEFAULT 0 CHECK (amount_minor >= 0),
    amount_status TEXT NOT NULL DEFAULT 'KNOWN',
    currency TEXT NOT NULL,
    cancellation_policy_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    commitment_id TEXT REFERENCES commitments(id) ON DELETE SET NULL,
    provider_key TEXT NOT NULL,
    external_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('DRAFT','PENDING','CONFIRMED','FULFILLED','CANCELLED','FAILED')),
    amount_minor INTEGER NOT NULL DEFAULT 0 CHECK (amount_minor >= 0),
    currency TEXT NOT NULL,
    items_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    booking_id TEXT REFERENCES bookings(id) ON DELETE SET NULL,
    order_id TEXT REFERENCES orders(id) ON DELETE SET NULL,
    provider_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('AUTHORIZE','CAPTURE','REFUND','VOID')),
    status TEXT NOT NULL CHECK (status IN ('INITIATED','AUTHORIZED','SUCCEEDED','FAILED','REFUNDED','VOIDED')),
    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
    currency TEXT NOT NULL,
    external_ref TEXT,
    provider_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_key, idempotency_key)
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    provider_key TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'SIMULATED' CHECK (provenance IN ('SIMULATED','PROVIDER_VERIFIED','USER_REPORTED','SYSTEM_DERIVED')),
    event_type TEXT NOT NULL CHECK (event_type IN ('TRAFFIC_DELAY','CLOSURE','WEATHER','SCHEDULE_DELAY','SERVICE_UNAVAILABLE','USER_CHANGE','OTHER')),
    severity TEXT NOT NULL CHECK (severity IN ('INFO','MINOR','MAJOR','CRITICAL')),
    status TEXT NOT NULL CHECK (status IN ('DETECTED','ACKNOWLEDGED','RESOLVED','IGNORED')),
    affected_activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    occurred_at TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS impact_assessments (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    provider_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    status TEXT NOT NULL,
    maturity_stage TEXT NOT NULL DEFAULT 'AI_ONLY',
    selected_corroboration_refs_json TEXT NOT NULL DEFAULT '[]',
    independent_evidence_count INTEGER NOT NULL DEFAULT 0,
    uncertain INTEGER NOT NULL DEFAULT 0 CHECK (uncertain IN (0,1)),
    selected_activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    primary_recovery_target_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    secondary_risk_activity_ids_json TEXT NOT NULL DEFAULT '[]',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    validated_activity_ids_json TEXT NOT NULL DEFAULT '[]',
    rejected_activity_ids_json TEXT NOT NULL DEFAULT '[]',
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    semantic_reason TEXT NOT NULL DEFAULT '',
    failure_code TEXT,
    response_hash TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replan_proposals (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    trigger_event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
    base_plan_version_id TEXT NOT NULL REFERENCES plan_versions(id) ON DELETE RESTRICT,
    proposed_plan_version_id TEXT NOT NULL REFERENCES plan_versions(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('GENERATED','PRESENTED','ACCEPTED','REJECTED','EXPIRED','INVALIDATED')),
    hard_constraints_passed INTEGER NOT NULL CHECK (hard_constraints_passed IN (0,1)),
    locked_commitments_preserved INTEGER NOT NULL CHECK (locked_commitments_preserved IN (0,1)),
    utility_score REAL NOT NULL,
    score_breakdown_json TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    CHECK (base_plan_version_id <> proposed_plan_version_id)
);

CREATE TABLE IF NOT EXISTS recommendations (
    id TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
    journey_id TEXT REFERENCES journeys(id) ON DELETE CASCADE,
    plan_version_id TEXT REFERENCES plan_versions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('PLACE','ACTIVITY','PLAN','REPLAN_EXPLANATION')),
    source TEXT NOT NULL CHECK (source IN ('RULES','AI','HYBRID')),
    score REAL,
    explanation TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journey_twins (
    journey_id TEXT PRIMARY KEY REFERENCES journeys(id) ON DELETE CASCADE,
    active_plan_version_id TEXT REFERENCES plan_versions(id) ON DELETE SET NULL,
    current_activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    latest_event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
    projection_status TEXT NOT NULL CHECK (projection_status IN ('READY','STALE','DEGRADED')),
    snapshot_at TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    upcoming_commitments_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_calls (
    id TEXT PRIMARY KEY,
    provider_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('SUCCESS','FAILURE','TIMEOUT','DEGRADED')),
    latency_ms INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    journey_id TEXT,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    before_json TEXT,
    after_json TEXT,
    trace_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_status INTEGER,
    response_json TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    claim_token TEXT,
    claim_deadline TEXT
);

CREATE TABLE IF NOT EXISTS action_approvals (
    id TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','CONSUMED','EXPIRED','REVOKED')),
    required_decision TEXT NOT NULL DEFAULT 'REQUIRE_CONFIRMATION',
    approval_method TEXT,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    approved_at TEXT,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_journeys_traveler ON journeys(traveler_id);
CREATE INDEX IF NOT EXISTS idx_plan_versions_journey ON plan_versions(journey_id, version_no);
-- One ACTIVE PlanVersion per Journey is a domain invariant, not a convention.
-- No Python-side lock can guarantee it across processes, and SQLite has no
-- SELECT ... FOR UPDATE to serialise the read/validate/write window that
-- produces it, so the guarantee is stated where it can actually be enforced.
-- A rejected writer sees IntegrityError and must treat it as a conflict, not
-- as a crash.
CREATE UNIQUE INDEX IF NOT EXISTS ux_plan_versions_single_active
    ON plan_versions(journey_id) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS idx_activities_plan ON activities(plan_version_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_commitments_journey ON commitments(journey_id, status, level);
-- ORD-0360: a Journey claims a node with at most one ACTIVE C4 anchor.  Two ACTIVE rows for
-- one node are not two constraints - resolve_plan_anchors() emits one anchor per row, the pair
-- then overlaps with itself, and generation answered the traveller's own duplicate reservation
-- as "no feasible plan candidates".  Python cannot guarantee this across processes (see the
-- note on ux_plan_versions_single_active), so it is stated where it can be enforced.
-- The role split below is the same one resolve_plan_anchors()/anchor_claim_identity() use:
-- ARRIVAL and DEPARTURE collapse onto the Journey's structural node, MID_TRIP carries its own
-- place/service key.  Rows with no $.anchor_role - the commerce booking/order commitments -
-- match none of these on purpose: they share the semantic_key namespace but are protected
-- through activity_id, so two of them for one place stay legal.
CREATE UNIQUE INDEX IF NOT EXISTS uq_commitments_one_active_arrival_anchor
    ON commitments(journey_id)
    WHERE status = 'ACTIVE' AND level = 'C4_Anchored' AND json_valid(policy_json)
      AND upper(json_extract(policy_json, '$.anchor_role')) = 'ARRIVAL';
CREATE UNIQUE INDEX IF NOT EXISTS uq_commitments_one_active_departure_anchor
    ON commitments(journey_id)
    WHERE status = 'ACTIVE' AND level = 'C4_Anchored' AND json_valid(policy_json)
      AND upper(json_extract(policy_json, '$.anchor_role')) = 'DEPARTURE';
CREATE UNIQUE INDEX IF NOT EXISTS uq_commitments_one_active_mid_trip_anchor
    ON commitments(journey_id, json_extract(policy_json, '$.semantic_key'))
    WHERE status = 'ACTIVE' AND level = 'C4_Anchored' AND json_valid(policy_json)
      AND upper(json_extract(policy_json, '$.anchor_role')) = 'MID_TRIP';
CREATE INDEX IF NOT EXISTS idx_events_journey ON events(journey_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_impact_assessments_event ON impact_assessments(event_id, created_at);
CREATE INDEX IF NOT EXISTS idx_replans_journey ON replan_proposals(journey_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_journey ON audit_logs(journey_id, created_at);
CREATE INDEX IF NOT EXISTS idx_action_approvals_scope
    ON action_approvals(journey_id, action, target_id, status, expires_at);

CREATE TABLE IF NOT EXISTS journey_missions (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
    trigger_event_id TEXT,
    user_text TEXT NOT NULL,
    objective_codes_json TEXT NOT NULL DEFAULT '[]',
    protected_activity_ids_json TEXT NOT NULL DEFAULT '[]',
    mutable_activity_ids_json TEXT NOT NULL DEFAULT '[]',
    intent_json TEXT NOT NULL DEFAULT '{}',
    resolved_intent_json TEXT NOT NULL DEFAULT '{}',
    allowed_action_classes_json TEXT NOT NULL DEFAULT '[]',
    unresolved_references_json TEXT NOT NULL DEFAULT '[]',
    compiler_provider_key TEXT NOT NULL DEFAULT 'deterministic',
    compiler_model_key TEXT NOT NULL DEFAULT 'mission-compiler-v2-fallback',
    compiler_confidence REAL NOT NULL DEFAULT 1.0,
    reasoning_summary TEXT NOT NULL DEFAULT '',
    is_standing INTEGER NOT NULL DEFAULT 0 CHECK (is_standing IN (0,1)),
    parent_mission_id TEXT,
    max_cost_delta_minor INTEGER NOT NULL DEFAULT 0 CHECK (max_cost_delta_minor >= 0),
    max_loss_minor INTEGER NOT NULL DEFAULT 0 CHECK (max_loss_minor >= 0),
    max_schedule_drift_min INTEGER NOT NULL DEFAULT 0 CHECK (max_schedule_drift_min >= 0),
    preferences_json TEXT NOT NULL DEFAULT '{}',
    requested_autonomy TEXT NOT NULL DEFAULT 'PLAN_ONLY',
    status TEXT NOT NULL DEFAULT 'CREATED',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES journey_missions(id) ON DELETE CASCADE,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    provider_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    outcome TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_run_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    decision TEXT,
    status TEXT NOT NULL DEFAULT 'COMPLETED',
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS guardian_runs (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    standing_mission_id TEXT REFERENCES journey_missions(id) ON DELETE SET NULL,
    trigger_event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
    agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
    proposal_id TEXT REFERENCES replan_proposals(id) ON DELETE SET NULL,
    decision TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'COMPLETED',
    action_status TEXT NOT NULL DEFAULT 'NO_ACTION',
    evidence_fingerprint TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    reasoning_summary TEXT NOT NULL DEFAULT '',
    active_plan_before_id TEXT,
    active_plan_after_id TEXT,
    trigger_kind TEXT NOT NULL DEFAULT 'REACTIVE',
    resilience_snapshot_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    cooldown_until TEXT,
    UNIQUE(journey_id, evidence_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_journey_missions_journey ON journey_missions(journey_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_mission ON agent_runs(mission_id, started_at);
CREATE INDEX IF NOT EXISTS idx_agent_run_steps_run ON agent_run_steps(run_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_guardian_runs_journey ON guardian_runs(journey_id, created_at);

CREATE TABLE IF NOT EXISTS provider_observations (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    provider_key TEXT NOT NULL,
    observation_type TEXT NOT NULL,
    state TEXT NOT NULL,
    incident_fingerprint TEXT,
    provenance TEXT NOT NULL DEFAULT 'REPLAY',
    captured_at TEXT NOT NULL,
    expires_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS city_signal_fixtures (
    journey_id TEXT PRIMARY KEY REFERENCES journeys(id) ON DELETE CASCADE,
    provider_key TEXT NOT NULL DEFAULT 'city-signal-fixture',
    state TEXT NOT NULL DEFAULT 'NORMAL',
    revision INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guardian_pulses (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    provider_key TEXT NOT NULL,
    observation_ids_json TEXT NOT NULL DEFAULT '[]',
    guardian_run_id TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_provider_observations_journey
    ON provider_observations(journey_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_guardian_pulses_journey
    ON guardian_pulses(journey_id, started_at);

CREATE TABLE IF NOT EXISTS autopilot_enrollments (
    journey_id TEXT PRIMARY KEY REFERENCES journeys(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_seconds REAL NOT NULL DEFAULT 60,
    provider_mesh_json TEXT NOT NULL DEFAULT '{}',
    last_pulse_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journey_resilience_snapshots (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    score INTEGER NOT NULL,
    status TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    expires_at TEXT,
    components_json TEXT NOT NULL DEFAULT '{}',
    activity_risks_json TEXT NOT NULL DEFAULT '[]',
    commitment_buffers_json TEXT NOT NULL DEFAULT '[]',
    failure_modes_json TEXT NOT NULL DEFAULT '[]',
    recommendation_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL,
    UNIQUE(journey_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS counterfactual_simulations (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    snapshot_id TEXT REFERENCES journey_resilience_snapshots(id) ON DELETE SET NULL,
    scenario_key TEXT NOT NULL,
    scenario_type TEXT NOT NULL,
    parameters_json TEXT NOT NULL DEFAULT '{}',
    feasible INTEGER NOT NULL,
    c4_preserved INTEGER NOT NULL,
    cost_delta_minor INTEGER NOT NULL DEFAULT 0,
    schedule_drift_min INTEGER NOT NULL DEFAULT 0,
    plan_changes_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(journey_id, scenario_key)
);

CREATE TABLE IF NOT EXISTS real_place_candidates (
    id TEXT PRIMARY KEY,
    journey_id TEXT REFERENCES journeys(id) ON DELETE CASCADE,
    provider_key TEXT NOT NULL,
    external_id TEXT NOT NULL,
    city_code TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    opening_hours_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    captured_at TEXT NOT NULL,
    expires_at TEXT,
    distance_m REAL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    materialized_place_id TEXT,
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    UNIQUE(journey_id, provider_key, external_id)
);

CREATE INDEX IF NOT EXISTS idx_autopilot_enrollments_enabled
    ON autopilot_enrollments(enabled, updated_at);
CREATE INDEX IF NOT EXISTS idx_resilience_snapshots_journey
    ON journey_resilience_snapshots(journey_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_counterfactuals_journey
    ON counterfactual_simulations(journey_id, created_at);
CREATE INDEX IF NOT EXISTS idx_real_place_candidates_journey
    ON real_place_candidates(journey_id, captured_at);

CREATE TABLE IF NOT EXISTS journey_trip_contexts (
    journey_id TEXT PRIMARY KEY REFERENCES journeys(id) ON DELETE CASCADE,
    arrival_anchor_json TEXT NOT NULL DEFAULT '{}',
    departure_anchor_json TEXT NOT NULL DEFAULT '{}',
    party_size INTEGER NOT NULL DEFAULT 1 CHECK (party_size >= 1),
    occupancy_json TEXT NOT NULL DEFAULT '{}',
    stay_budget_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK (stay_budget_amount_minor >= 0),
    stay_budget_currency TEXT NOT NULL DEFAULT 'CNY',
    -- ORD-0355: additive, and three-valued. NULL is a row written before this build could ask
    -- the question, not an answer of "not declared"; see `stay_budget_declared` in
    -- `app/db/models.py`.  The amount column above keeps its type and nullability unchanged.
    stay_budget_declared INTEGER CHECK (stay_budget_declared IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accommodation_offer_snapshots (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    provider_key TEXT NOT NULL,
    external_offer_id TEXT NOT NULL,
    property_name TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    coordinate_crs TEXT NOT NULL DEFAULT 'WGS84',
    room_type TEXT NOT NULL,
    occupancy_json TEXT NOT NULL DEFAULT '{}',
    check_in_at TEXT NOT NULL,
    check_out_at TEXT NOT NULL,
    nightly_amount_minor INTEGER NOT NULL CHECK (nightly_amount_minor >= 0),
    taxes_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK (taxes_amount_minor >= 0),
    total_amount_minor INTEGER NOT NULL CHECK (total_amount_minor >= 0),
    currency TEXT NOT NULL,
    cancellation_policy_json TEXT NOT NULL DEFAULT '{}',
    luggage_drop_json TEXT NOT NULL DEFAULT '{}',
    refundable INTEGER NOT NULL DEFAULT 0 CHECK (refundable IN (0,1)),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    captured_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'CAPTURED',
    UNIQUE(journey_id, provider_key, external_offer_id)
);

CREATE TABLE IF NOT EXISTS stay_journey_options (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    accommodation_offer_id TEXT NOT NULL REFERENCES accommodation_offer_snapshots(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'CANDIDATE',
    stay_total_minor INTEGER NOT NULL,
    daily_travel_minor INTEGER NOT NULL,
    mobility_total_minor INTEGER NOT NULL,
    walking_min INTEGER NOT NULL,
    protected_margin_min INTEGER NOT NULL,
    arrival_fit REAL NOT NULL,
    departure_fit REAL NOT NULL,
    cancellation_score REAL NOT NULL,
    days_json TEXT NOT NULL DEFAULT '[]',
    leg_modes_json TEXT NOT NULL DEFAULT '[]',
    journey_impact_json TEXT NOT NULL DEFAULT '{}',
    hard_constraints_passed INTEGER NOT NULL DEFAULT 1 CHECK (hard_constraints_passed IN (0,1)),
    optimizer_kind TEXT NOT NULL DEFAULT 'BOUNDED_DETERMINISTIC',
    weather_state TEXT NOT NULL DEFAULT 'NORMAL',
    materialized_plan_version_id TEXT REFERENCES plan_versions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(journey_id, rank)
);

CREATE TABLE IF NOT EXISTS mobility_facts (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    option_id TEXT REFERENCES stay_journey_options(id) ON DELETE CASCADE,
    from_key TEXT NOT NULL,
    to_key TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('WALK','BICYCLE','TRANSIT','AUTO','RIDE_HAIL')),
    duration_min INTEGER NOT NULL CHECK (duration_min >= 0),
    distance_m INTEGER NOT NULL CHECK (distance_m >= 0),
    cost_amount_minor INTEGER,
    currency TEXT,
    cost_status TEXT NOT NULL,
    walking_min INTEGER NOT NULL CHECK (walking_min >= 0),
    transfers INTEGER NOT NULL DEFAULT 0 CHECK (transfers >= 0),
    weather_exposure TEXT NOT NULL,
    reliability_score REAL NOT NULL,
    provider_key TEXT NOT NULL,
    provenance TEXT NOT NULL,
    geometry_json TEXT NOT NULL DEFAULT '{}',
    crs TEXT NOT NULL DEFAULT 'WGS84',
    paid_mode INTEGER NOT NULL DEFAULT 0 CHECK (paid_mode IN (0,1)),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    captured_at TEXT NOT NULL,
    expires_at TEXT,
    UNIQUE(journey_id, from_key, to_key, mode)
);

CREATE TABLE IF NOT EXISTS journey_legs (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    plan_version_id TEXT NOT NULL REFERENCES plan_versions(id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    from_activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    to_activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    from_key TEXT NOT NULL,
    to_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    depart_at TEXT NOT NULL,
    arrive_at TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    distance_m INTEGER NOT NULL,
    cost_amount_minor INTEGER,
    currency TEXT,
    cost_status TEXT NOT NULL,
    walking_min INTEGER NOT NULL,
    transfers INTEGER NOT NULL DEFAULT 0,
    reliability_score REAL NOT NULL,
    weather_exposure TEXT NOT NULL,
    geometry_json TEXT NOT NULL DEFAULT '{}',
    crs TEXT NOT NULL DEFAULT 'WGS84',
    provider_key TEXT NOT NULL,
    provenance TEXT NOT NULL,
    provider_snapshot_id TEXT REFERENCES mobility_facts(id) ON DELETE SET NULL,
    captured_at TEXT NOT NULL,
    expires_at TEXT,
    UNIQUE(plan_version_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS journey_operations_contexts (
    journey_id TEXT PRIMARY KEY REFERENCES journeys(id) ON DELETE CASCADE,
    baggage_present INTEGER NOT NULL DEFAULT 0 CHECK (baggage_present IN (0,1)),
    baggage_size_class TEXT NOT NULL DEFAULT 'NONE',
    luggage_drop_required INTEGER NOT NULL DEFAULT 0 CHECK (luggage_drop_required IN (0,1)),
    constraints_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_reservation_drafts (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    extracted_json TEXT NOT NULL DEFAULT '{}',
    unresolved_fields_json TEXT NOT NULL DEFAULT '[]',
    extraction_mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS transport_reservations (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    draft_id TEXT REFERENCES external_reservation_drafts(id) ON DELETE SET NULL,
    reservation_type TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    confirmation_ref TEXT,
    origin_json TEXT NOT NULL,
    destination_json TEXT NOT NULL,
    scheduled_departure_at TEXT NOT NULL,
    scheduled_arrival_at TEXT NOT NULL,
    estimated_arrival_at TEXT,
    actual_arrival_at TEXT,
    status TEXT NOT NULL,
    realtime_status TEXT NOT NULL DEFAULT 'UNKNOWN_REALTIME',
    passenger_scope_json TEXT NOT NULL DEFAULT '{}',
    change_policy_json TEXT NOT NULL DEFAULT '{}',
    price_amount_minor INTEGER,
    currency TEXT,
    price_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    source_provenance TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    booking_id TEXT REFERENCES bookings(id) ON DELETE SET NULL,
    commitment_id TEXT REFERENCES commitments(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS notification_events (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    payload_json TEXT NOT NULL DEFAULT '{}',
    delivery_channels_json TEXT NOT NULL DEFAULT '[]',
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offline_journey_snapshots (
    journey_id TEXT PRIMARY KEY REFERENCES journeys(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL DEFAULT 1,
    valid_until TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    provider_freshness_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS location_observations (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    accuracy_m REAL NOT NULL,
    captured_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'FOREGROUND_WEB'
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
    endpoint_hash TEXT NOT NULL UNIQUE,
    subscription_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edge_devices (
    id TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
    journey_id TEXT REFERENCES journeys(id) ON DELETE SET NULL,
    display_name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    last_sequence INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edge_actions (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES edge_devices(id) ON DELETE CASCADE,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edge_observations (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES edge_devices(id) ON DELETE CASCADE,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    observation_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_observations (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    query TEXT NOT NULL,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    title TEXT NOT NULL,
    source_class TEXT NOT NULL DEFAULT 'OPEN_WEB',
    provider_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    content_excerpt TEXT NOT NULL DEFAULT '',
    authority_status TEXT NOT NULL DEFAULT 'NON_AUTHORITATIVE',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_web_observations_journey
    ON web_observations(journey_id, captured_at);

CREATE INDEX IF NOT EXISTS idx_journey_legs_plan ON journey_legs(plan_version_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_external_reservation_drafts_journey ON external_reservation_drafts(journey_id, created_at);
CREATE INDEX IF NOT EXISTS idx_transport_reservations_journey ON transport_reservations(journey_id, scheduled_arrival_at);
-- Authoritative reservation identity is created by the startup migration
-- (app/db/session.py::_migrate_reservation_identity) rather than here.
--
-- Reason: a plain unique index over (journey_id, confirmation_ref) cannot be
-- created while historical duplicate rows exist, and silently swallowing that
-- failure is exactly the defect this replaces.  The migration first quarantines
-- duplicate losers (preserving their evidence) and then creates a partial
-- unique index, so uniqueness is genuinely enforced on every database.
CREATE INDEX IF NOT EXISTS idx_notification_events_journey ON notification_events(journey_id, created_at);
CREATE INDEX IF NOT EXISTS idx_location_observations_journey ON location_observations(journey_id, captured_at);

CREATE INDEX IF NOT EXISTS idx_stay_offers_journey ON accommodation_offer_snapshots(journey_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_stay_options_journey ON stay_journey_options(journey_id, rank);
CREATE INDEX IF NOT EXISTS idx_mobility_facts_journey ON mobility_facts(journey_id, from_key, to_key);

-- ORD-0057: an asynchronous generation job and its state machine.
CREATE TABLE IF NOT EXISTS stay_generation_operations (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN
        ('QUEUED','RUNNING','ABANDON_REQUESTED','COMPLETED','FAILED','ABANDONED')),
    phase TEXT CHECK (phase IS NULL OR phase IN
        ('DISCOVERY','NORMALIZATION','MOBILITY_CHECK','RANKING')),
    request_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    outcome TEXT CHECK (outcome IS NULL OR outcome IN
        ('OPTIONS_FOUND','NO_OFFERS_FROM_PROVIDER','ALL_CANDIDATES_REJECTED','NOT_APPLICABLE_ZERO_NIGHTS')),
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    abandon_requested_at TEXT,
    updated_at TEXT NOT NULL
);

-- At most one *unfinished* job per journey and per identical request.  This is a database
-- rule, not a polite convention: two taps racing through the same code path cannot both
-- start a second ~115s provider chain, and a rejected insert is read back as the winner's
-- id.  Terminal rows are deliberately excluded so a traveller can regenerate later.
CREATE UNIQUE INDEX IF NOT EXISTS ux_stay_generation_one_open_job
    ON stay_generation_operations(journey_id, fingerprint)
    WHERE status IN ('QUEUED','RUNNING','ABANDON_REQUESTED');

CREATE INDEX IF NOT EXISTS idx_stay_generation_journey_recent
    ON stay_generation_operations(journey_id, queued_at);

-- ORD-0095-1 (CITYNEXUS-ANYWHERE §6/§7): one catalog-hydration job per destination.
-- The lifecycle is *not* invented here: `status` and `phase` are the two axes the stay-generation
-- job already proved out (`stay_generation_operations`), with the arbitration's structural
-- terminal state `UNAVAILABLE` added, and `phase` names are the arbiter's, one per stage this
-- code can actually be inside.  A stage that never happens is never reported.
CREATE TABLE IF NOT EXISTS catalog_hydration_operations (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    -- Nullable on purpose (ORD-0167): a request whose destination nobody can name has *no*
    -- canonical identity.  The NOT NULL below used to force this build to invent one out of the
    -- traveller's own words, and a fingerprint was then computed from that invention.
    canonical_destination_id TEXT,
    -- The three non-identity columns: verbatim input, folded input, correlation token.  None of
    -- them is a destination identifier and none may be joined against `places`.
    pending_destination_label TEXT,
    resolver_query_normalized TEXT,
    destination_pending_key TEXT,
    pending_label_fidelity TEXT CHECK (pending_label_fidelity IS NULL OR pending_label_fidelity IN
        ('EXACT_RAW','LEGACY_NORMALIZED')),
    legacy_fingerprint TEXT,
    catalog_schema_version TEXT NOT NULL,
    readiness_policy_version TEXT NOT NULL,
    provider_policy_version TEXT NOT NULL,
    journey_id TEXT REFERENCES journeys(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN
        ('QUEUED','RUNNING','ABANDON_REQUESTED','COMPLETED','FAILED','UNAVAILABLE','ABANDONED')),
    phase TEXT CHECK (phase IS NULL OR phase IN
        ('QUEUED','RESOLVING_DESTINATION','DISCOVERING_POIS','NORMALIZING','BUILDING_MOBILITY','PERSISTING','FINALIZING')),
    request_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    outcome TEXT CHECK (outcome IS NULL OR outcome IN
        ('CATALOG_PREPARED','ALREADY_CURRENT','NO_REGISTERED_SOURCE','PENDING_CANDIDATES_UNAPPLIED','ABANDONED_BY_TRAVELLER','WORKER_LOST','DESTINATION_AMBIGUOUS','RESOLVER_RUNTIME_FAILURE')),
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    abandon_requested_at TEXT,
    updated_at TEXT NOT NULL
);

-- Durable idempotency, not a polite convention: at most one *unfinished* job per identical
-- fingerprint.  A process-local lock cannot satisfy this, because two uvicorn workers do not
-- share one; a rejected insert is read back as the winner's id, so five concurrent taps of one
-- county get one job and one provider chain, and every caller is told `reused`.
CREATE UNIQUE INDEX IF NOT EXISTS ux_catalog_hydration_one_open_job
    ON catalog_hydration_operations(fingerprint)
    WHERE status IN ('QUEUED','RUNNING','ABANDON_REQUESTED');

CREATE INDEX IF NOT EXISTS idx_catalog_hydration_destination_recent
    ON catalog_hydration_operations(canonical_destination_id, queued_at);

CREATE INDEX IF NOT EXISTS idx_catalog_hydration_journey_recent
    ON catalog_hydration_operations(journey_id, queued_at);

-- ORD-0434 step 1: a share grant is a server fact, not a URL the client assembles.
-- One journey can hold many grants.  `expires_at` and `revoked_at` carry no DEFAULT and no
-- sentinel: a NULL means that moment was never set, which is the only honest way to say
-- "this grant has no deadline" and "this grant was never revoked" without inventing either.
-- `created_by` is what the creating device asserted; this step does not authenticate it,
-- because the anonymous read path is still the product's default (tightening it is step 2).
CREATE TABLE IF NOT EXISTS journey_share_grants (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('PRIVATE','LINK_READ','INVITEES_ONLY','PUBLIC')),
    created_by TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_journey_share_grants_token
    ON journey_share_grants(token);

CREATE INDEX IF NOT EXISTS idx_journey_share_grants_journey
    ON journey_share_grants(journey_id, created_at);

-- ORD-0434 step 2: one member set per journey, and a role-to-capability table the server holds.
-- Borrowed shape: the relation-and-copy findings note written for this order -- each role carries
-- one sentence of scope, and an invitation has a lifetime while a withdrawal has a moment.  That
-- note is not a file in this tree, so the sentences it held are restated here instead of pointed at.
--
-- `journey_members` is the single set the product speaks of: the unique index below is what makes
-- "one row per traveller per journey" a store fact rather than a convention a caller must respect.
-- `expires_at` is NULL for a membership that was given no end - not a far-future date.
CREATE TABLE IF NOT EXISTS journey_members (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('OWNER','ADMIN','COLLABORATOR')),
    invited_by TEXT NOT NULL,
    expires_at TEXT,
    joined_at TEXT,
    removed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_journey_members_journey_traveler
    ON journey_members(journey_id, traveler_id);

CREATE INDEX IF NOT EXISTS idx_journey_members_journey
    ON journey_members(journey_id, created_at);

-- The ACL table.  A row says which capability a role has and which message key states its scope in
-- one sentence - the words themselves stay in the client's dictionary, because a server sentence
-- reaching a screen is the defect this project keeps having to fix (RELATION:94's shape is a table,
-- not a string).  Seeded once by `db/session.py`, so it is readable without being editable per trip.
CREATE TABLE IF NOT EXISTS journey_role_capabilities (
    role TEXT NOT NULL CHECK (role IN ('OWNER','ADMIN','COLLABORATOR')),
    capability TEXT NOT NULL CHECK (capability IN ('JOURNEY_READ','JOURNEY_UPDATE','PLACE_EDIT','MEMBER_MANAGE','SHARE_GRANT_MANAGE')),
    scope_message_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (role, capability)
);

-- ORD-0476 slice 1: the trip footprint -- a stamp saying the traveller was at one target of one
-- journey.  Built from zero: the only `check_in` reading on the model layer before this order was
-- `Stay.check_in_at` in `backend/app/db/models.py` (an accommodation arrival time), and no route,
-- table or contract path named a check-in.
--
-- Why `activity_id` and `place_id` are `ON DELETE SET NULL` rather than RESTRICT or CASCADE.  Both
-- are deleted by doors that already exist: `generate_replan` in `backend/app/main.py` and
-- `materialize_stay_option` in `backend/app/operations/v9.py` remove `activities` rows when a plan
-- version is superseded, and
-- the catalog prunes `places`.  A footpoint is the traveller's own history, so it must outlive the
-- row it pointed at -- a RESTRICT would turn those two replan doors into failures, which is the same
-- class of defect this project already had once with `demo/reset` and a new table's foreign key.
--
-- `target_key` is the half that carries the 重复打卡 promise durably, and it is deliberately NOT
-- derived from the two nullable columns: `UNIQUE (journey_id, activity_id)` with a NULL in that
-- column is not a constraint SQLite enforces (NULLs are distinct), and a SET NULL cascade could
-- mint or erase a duplicate after the fact.  The door writes one immutable key -- `activity:<id>` or
-- `place:<id>` -- and this index makes two workers tapping the same target twice impossible without
-- any read-then-write check inside a process.
CREATE TABLE IF NOT EXISTS journey_checkins (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    place_id TEXT REFERENCES places(id) ON DELETE SET NULL,
    -- Which column the stamp was taken against, as the door decided it -- not re-derived at read
    -- time, because after a cascade there is nothing left to derive it from.
    target_kind TEXT NOT NULL CHECK (target_kind IN ('ACTIVITY','PLACE')),
    target_key TEXT NOT NULL,
    -- The moment the server recorded the stamp.  This slice takes no caller-supplied instant: an
    -- offline queue's device clock is a different fact and would arrive as its own named field.
    stamped_at TEXT NOT NULL,
    -- NULL means no note was written, which a screen must tell apart from an empty one.
    note TEXT,
    -- One channel exists today: the traveller's own front-end tap.  A CHECK rather than free text,
    -- so a second channel has to arrive as a contract change instead of smuggling a value in.
    source TEXT NOT NULL DEFAULT 'FOREGROUND_WEB' CHECK (source IN ('FOREGROUND_WEB')),
    UNIQUE (journey_id, target_key)
);

-- The read door answers one journey's stamps oldest-stamped first; this is that scan.
CREATE INDEX IF NOT EXISTS idx_journey_checkins_journey_stamped
    ON journey_checkins(journey_id, stamped_at);
-- ORD-0444: the collection container -- the 「收藏」 exit of the POI capture chain.
-- Two tables, both new; no existing table or column is touched.  A folder is the named shelf,
-- an item is one place laid on it.  Nothing here belongs to a plan: adding a place to a folder
-- writes no activity and no plan version, which is exactly the difference between this door and
-- the 「加进计划」 one.
CREATE TABLE IF NOT EXISTS collection_folders (
    id TEXT PRIMARY KEY,
    -- The name a traveller reads on the shelf.  Not unique on purpose: two shelves may both be
    -- called 「周末」, and it is the id that addresses one of them.
    name TEXT NOT NULL,
    -- 成员归属, and NOT NULL rather than nullable: a nullable owner would look like ownership while
    -- nothing wrote it, which is how 「我的收藏」 becomes a claim the row cannot support.  It is a
    -- reference to the traveller who asserted the shelf at creation -- asserted, not authenticated;
    -- the anonymous read is still this product's default (same stance as `journey_share_grants`
    -- `created_by`).
    owner_traveler_id TEXT NOT NULL REFERENCES travelers(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_items (
    id TEXT PRIMARY KEY,
    folder_id TEXT NOT NULL REFERENCES collection_folders(id) ON DELETE CASCADE,
    -- A reference to `places`, not a copy of its fields: the shelf holds the place, and its name,
    -- category and coordinates are read from that row when the shelf is answered.  No cascade --
    -- a place that disappears must not silently take a traveller's note with it.
    place_id TEXT NOT NULL REFERENCES places(id),
    -- NULL means no note was ever written, which is a different fact from an empty one.
    note TEXT,
    added_at TEXT NOT NULL,
    -- One shelf holds a place once.  The second add is refused by name at the door
    -- (`PLACE_ALREADY_IN_COLLECTION`), and this index is what makes that refusal hold across two
    -- uvicorn workers, where a read-then-write check in one process would not.
    UNIQUE (folder_id, place_id)
);

-- The shelf reads its own items in the order they were put on it; this is that scan.
CREATE INDEX IF NOT EXISTS idx_collection_items_folder_added
    ON collection_items(folder_id, added_at);

-- The only read of the shelf list is scoped by its owner, oldest shelf first -- that is this scan.
-- It exists because `GET /api/v1/collections` requires `traveler_id`; a table with no owner column
-- would have had no use for it, which is the difference this round is about.
CREATE INDEX IF NOT EXISTS idx_collection_folders_owner
    ON collection_folders(owner_traveler_id, created_at);
