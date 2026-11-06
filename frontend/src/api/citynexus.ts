import type { JourneyTwin } from "../contracts/journeyTwin";
import type { ReplanProposal } from "../contracts/replan";
import { fetchWithReadDeadline } from "../lib/readDeadline";
import { OriginResponseUnusableError } from "../lib/failureAttribution";
import { beginNonIdempotentRequest } from "../lib/inFlightWrites";

export interface Money {
  amount_minor: number;
  currency: string;
}

export interface Activity {
  id: string;
  sequence_no: number;
  title: string;
  activity_type: string;
  place_id: string | null;
  city_service_id: string | null;
  start_at: string;
  end_at: string;
  travel_from_prev_min: number;
  estimated_cost: Money;
  status: string;
  locked: boolean;
}

export interface PlanVersion {
  id: string;
  journey_id: string;
  version_no: number;
  planning_mode: string;
  status: string;
  source: string;
  parent_plan_version_id: string | null;
  total_cost: Money;
  utility_score: number | null;
  score_breakdown: Record<string, number> | null;
  activities: Activity[];
}

export interface DemoIds {
  traveler_id: string;
  journey_id: string;
  city_code: string;
}

export interface JourneyRecord {
  id: string;
  traveler_id: string;
  title: string;
  city_code: string;
  timezone: string;
  status: string;
}

export interface PlaceRecord {
  id: string;
  name: string;
  category: string;
  city_code: string;
  location: { latitude: number; longitude: number };
  tags: string[];
  provider_key: string;
  coordinate_system: string;
  /** Null means the capture never recorded what system the provider handed us --
      which is not the same fact as a stored system, so it stays null. */
  source_coordinate_system: string | null;
}

export interface AgentPolicy {
  traveler_id: string;
  mode: "ADVISORY" | "ASSISTED" | "AUTONOMOUS";
  auto_accept_replan: boolean;
  max_auto_replan_cost_delta_minor: number;
  max_auto_loss_minor: number;
  max_auto_schedule_drift_min: number;
  allowed_activity_types: string[];
  allowed_booking_types: string[];
  protected_commitment_levels: string[];
  allow_auto_commitment_management: boolean;
  allow_auto_when_provider_degraded: boolean;
  allow_auto_on_critical_event: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface Booking {
  id: string;
  journey_id: string;
  activity_id: string | null;
  commitment_id: string | null;
  provider_key: string;
  external_ref: string | null;
  status: string;
  amount: Money;
}

export interface EventRecord {
  id: string;
  journey_id: string;
  occurred_at: string;
  event_type: string;
  affected_activity_id: string | null;
  status: string;
  payload?: Record<string, unknown>;
}

export interface AgentFeedEntry {
  id: string;
  timestamp: string;
  event_type: string;
  type: string;
  summary: string;
  actor_type: string;
  actor_id: string | null;
  tool: string | null;
  journey_id: string | null;
  proposal_id: string | null;
  entity_type: string | null;
  entity_id: string | null;
  decision: string | null;
  reason_codes: string[];
  trace_id: string;
  source: string;
  facts: Record<string, unknown>;
}

export interface AgentFeedResponse {
  journey_id: string;
  source: string;
  events: AgentFeedEntry[];
}

export interface AgentMission {
  id: string;
  journey_id: string;
  trigger_event_id: string | null;
  user_text: string;
  objective_codes: string[];
  protected_activity_ids: string[];
  mutable_activity_ids: string[];
  resolved_intent: Record<string, unknown>;
  allowed_action_classes: string[];
  unresolved_references: string[];
  compiler: { provider_key: string; model_key: string; confidence: number };
  reasoning_summary: string;
  is_standing: boolean;
  max_cost_delta_minor: number;
  max_loss_minor: number;
  max_schedule_drift_min: number;
  preferences: Record<string, unknown>;
  capabilities?: TravelerCapability[];
  requested_autonomy: "PLAN_ONLY" | "EXECUTE_WITHIN_BOUNDS";
  status: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  failure_reason: string | null;
  mission_plan?: AgentMissionPlan;
}

export interface GuardianRun {
  id: string;
  journey_id: string;
  decision: "NO_ACTION" | "WATCH" | "RECOVERY_REQUIRED" | "PREEMPTIVE_RECOVERY";
  status: string;
  action_status: string;
  deduplicated: boolean;
  standing_mission: AgentMission | null;
  trigger_event_id: string | null;
  trigger_kind?: "REACTIVE" | "PREDICTIVE";
  resilience_snapshot_id?: string | null;
  proposal_id: string | null;
  agent_run_id: string | null;
  agent_runtime: { provider_key: string; runtime_identity?: string; model_key: string; status: string } | null;
  tool_steps: AgentRunStep[];
  evidence: {
    observation?: Record<string, unknown>;
    inference?: Record<string, unknown>;
    solver?: Record<string, unknown> | null;
    policy?: Record<string, unknown> | null;
    mutation?: Record<string, unknown> | null;
    prediction?: ResilienceSnapshot | null;
  };
  reasoning_summary: string;
  active_plan_before_id: string | null;
  active_plan_after_id: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface GuardianStatus {
  journey_id: string;
  standing_mission: AgentMission | null;
  current_active_plan_version_id: string | null;
  latest: GuardianRun | null;
  runs: GuardianRun[];
  autopilot?: {
    enabled: boolean;
    interval_seconds: number;
    signal_provider: string;
    last_pulse_at: string | null;
    last_error: string | null;
    enrolled_journey_ids?: string[];
    enrollment?: {
      journey_id: string;
      enabled: boolean;
      interval_seconds: number;
      provider_mesh: Record<string, string>;
      last_pulse_at: string | null;
      last_error: string | null;
    } | null;
  };
  signal_fixture?: {
    journey_id: string;
    provider_key: string;
    state: string;
    revision: number;
    payload: Record<string, unknown>;
    updated_at: string;
  } | null;
}

export interface ResilienceSnapshot {
  id: string;
  journey_id: string;
  score: number;
  risk_score: number;
  status: string;
  captured_at: string;
  expires_at: string | null;
  components: Record<string, number>;
  activity_risks: Array<Record<string, unknown>>;
  commitment_buffers: Array<Record<string, unknown>>;
  dominant_failure_modes: string[];
  preventive_recommendation: Record<string, unknown>;
  evidence_provenance: Array<Record<string, unknown>>;
  fingerprint: string;
}

export interface CounterfactualSimulation {
  id: string;
  journey_id: string;
  snapshot_id: string | null;
  scenario_key: string;
  scenario_type: string;
  parameters: Record<string, unknown>;
  feasible: boolean;
  c4_preserved: boolean;
  cost_delta_minor: number;
  schedule_drift_min: number;
  plan_changes: Array<Record<string, unknown>>;
  evidence: Record<string, unknown>;
  created_at: string;
}

export interface ProviderMeshResponse {
  journey_id: string;
  providers: Array<{
    provider_key: string;
    observation_type: string;
    state: string;
    provenance: string;
    captured_at: string;
    expires_at: string | null;
  }>;
  real_place_candidates: Array<{
    id: string;
    provider_key: string;
    external_id: string;
    name: string;
    category: string;
    location: { latitude: number; longitude: number };
    tags: string[];
    captured_at: string;
    expires_at: string | null;
    materialized_place_id: string | null;
    status: string;
  }>;
}

export interface AgentRunStep {
  id?: string;
  sequence_no: number;
  tool_name: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
  decision: string | null;
  status: string;
  created_at?: string;
  completed_at?: string;
}

export interface AgentRun {
  run_id: string | null;
  mission_id: string;
  journey_id?: string;
  provider_key: string;
  runtime_identity?: string;
  model_key: string;
  status: string;
  outcome: string | null;
  failure_reason: string | null;
  mission_status?: string | null;
  created_at?: string;
  started_at?: string;
  completed_at?: string | null;
  mission?: AgentMission;
  tool_steps: AgentRunStep[];
  mission_plan?: AgentMissionPlan;
  native_events_url?: string;
  events_url?: string;
  timeline_url?: string;
  initial_events?: NativeRuntimeEvent[];
  protected_commitments?: Array<Record<string, unknown>>;
}

export interface AgentMissionPlanStep {
  id: string;
  operation: string;
  tool_name: string | null;
  status: string;
  requires_authority: boolean;
}

export interface AgentMissionPlan {
  version: string;
  autonomy: string;
  steps: AgentMissionPlanStep[];
  protected_activity_ids: string[];
  allowed_tools: string[];
  read_only_tools?: string[];
  approval_required_tools?: string[];
  authority: string;
}

export interface NativeRuntimeEvent {
  event_type: string;
  run_id: string | null;
  event_id?: string | null;
  sequence?: number | null;
  tool_name: string | null;
  friendly_label: string;
  status: string;
  timestamp?: string | number | null;
  duration_ms?: number | null;
  result_summary?: Record<string, unknown> | string | null;
  policy_decision?: string | null;
  raw_event_type?: string;
  source?: string;
  approval_id?: string;
  proposal_id?: string;
  mission_id?: string;
  approval_status?: string;
  approval_method?: string | null;
}

export interface AgentCommandResponse {
  mission: AgentMission;
  event_id: string;
  run: AgentRun;
}

/* ORD-0387: the four words `backend/app/agent/ai_availability.py` produces and nowhere else.
   `(string & {})` keeps them as the listed choices without claiming a fifth can never arrive --
   an unlisted word must fall through to "the server said something we do not name", not be
   silently typed as known. */
export type AiStateWord =
  | "CONFIGURED_UNVERIFIED"
  | "NOT_CONFIGURED"
  | "DISABLED_BY_OPS"
  | "PROVIDER_REFUSED";

export interface AgentRuntimeStatus {
  runtime_identity:
    | "HERMES_NATIVE_MCP"
    | "NATIVE_DEEPSEEK_TOOL_LOOP"
    | "HERMES_COMPAT_ENDPOINT"
    | "NOT_CONFIGURED"
    | string;
  native_binary_detected: boolean;
  native_mcp_proven: boolean;
  mcp_server: string;
  deepseek_configured: boolean;
  hermes_compat_configured: boolean;
  /* ORD-0359 keeps this out of the contract's `required`, so absence is a real answer here:
     an older server, or one that never learned the field, and this client must not invent one. */
  ai_state?: AiStateWord | (string & {});
  hermes_native_gateway?: {
    configured: boolean;
    reachable: boolean;
    ready?: boolean;
    runs: boolean;
    events: boolean;
    approval: boolean;
    profile?: string;
  };
}

export interface CopilotResponse {
  message: string;
  mode: "AI" | "FALLBACK";
  verified_context_used: boolean;
  intent: {
    name: string;
    confidence: number;
    reason: string;
    entities: Record<string, unknown>;
    provider: string;
    mode: "AI" | "FALLBACK";
    semantic_envelope?: {
      intent: string;
      assertion_status: string;
      effective_time: string;
      event_type: string;
      requested_action: string;
      confidence: number;
      reasoning_summary: string;
      entities: Record<string, unknown>;
      constraints: Array<Record<string, unknown>>;
      capabilities?: TravelerCapability[];
      route_intent?: string;
      server_action?: string;
      create_event?: boolean;
      verify_evidence?: boolean;
    };
  };
  action: { type: string } | null;
  what_if: {
    simulation_only: boolean;
    status: string;
    feasibility: string;
    before: Record<string, unknown>;
    after: Record<string, unknown>;
    deltas: Record<string, number | null>;
    affected_activities: Array<Record<string, unknown>>;
    protected_commitments: { count: number; preserved: boolean; ids: string[] };
    proposed_changes: Array<Record<string, unknown>>;
    provenance: Record<string, unknown>;
    explanation: string;
    next_action: string | null;
  } | null;
  event: {
    id: string;
    event_type: string;
    status: string;
    affected_activity_id?: string | null;
    occurred_at?: string;
    target_resolution?: string;
    requires_review?: boolean;
  } | null;
  verification?: {
    status: string;
    query?: string;
    event_created: boolean;
    observations: Array<Record<string, unknown>>;
  } | null;
  agent_run?: {
    run_id: string;
    status: string;
    runtime_identity: string;
    provider_key: string;
    events_url?: string;
    timeline_url?: string;
    mission_id?: string;
    model_key?: string;
    mission_plan?: AgentMissionPlan;
    initial_events?: NativeRuntimeEvent[];
    protected_commitments?: Array<Record<string, unknown>>;
  } | null;
  protected_commitments: Array<Record<string, unknown>>;
  evidence: Record<string, unknown>;
  recommendations: Array<Record<string, unknown>>;
}

export interface ReflectSummary {
  journey_id: string;
  status: string;
  completed: boolean;
  timeline: {
    actual_time_available: boolean;
    actual_time_note: string;
    activities: Array<{
      activity_id: string;
      title: string;
      activity_type: string;
      planned_start_at: string;
      planned_end_at: string;
      observed_status: string;
      actual_start_at: string | null;
      actual_end_at: string | null;
    }>;
  };
  replan_history: Array<Record<string, unknown>>;
  agent_action_history: AgentFeedEntry[];
  provider_disruptions: Array<Record<string, unknown>>;
  cost_summary: {
    currency: string;
    initial_estimated_minor: number;
    final_estimated_minor: number;
    estimated_delta_minor: number;
    confirmed_booking_minor: number;
    captured_payment_minor: number;
  };
  plan_version_chain: Array<Record<string, unknown>>;
  preference_feedback_hook: {
    available: boolean;
    traveler_id: string;
    endpoint: string;
    mode: string;
  };
  traveler_twin: TravelerTwinEvidence;
}

export interface TravelerTwinEvidence {
  traveler_id: string;
  explicit_preferences: Array<Record<string, unknown>>;
  explicit_capabilities?: TravelerCapability[];
  capability_summary?: {
    items: TravelerCapability[];
    hard: TravelerCapability[];
    strong: TravelerCapability[];
    soft: TravelerCapability[];
    unknown_accessibility_is_not_accessible?: boolean;
  };
  learned_soft_preferences: Array<{
    dimension?: string;
    direction?: string;
    strength?: number;
    confidence?: number;
    evidence_count?: number;
    explanation?: string;
    journey_ids?: string[];
    last_evidence_at?: string;
    expires_at?: string | null;
    conflict?: Record<string, unknown> | null;
  }>;
  confidence: number;
  evidence_count: number;
  conflicts: Array<Record<string, unknown>>;
  provenance: Record<string, unknown>;
  updated_at: string;
}

export interface TravelerCapability {
  id?: string;
  traveler_id?: string;
  journey_id?: string | null;
  code: string;
  class?: string;
  severity: "HARD" | "STRONG" | "SOFT";
  params: Record<string, unknown>;
  scope: "TRAVELER" | "JOURNEY";
  source?: string;
  provenance?: string;
  explicit?: boolean;
  confirmed?: boolean;
  active?: boolean;
  confirmed_at?: string | null;
}

export interface TravelerCapabilityResponse {
  traveler_id?: string;
  journey_id?: string;
  capabilities: TravelerCapability[];
  summary: TravelerTwinEvidence["capability_summary"];
  traveler_twin?: TravelerTwinEvidence;
}

export interface AgentToolResponse {
  tool: string;
  decision: "ALLOW_AUTO" | "REQUIRE_CONFIRMATION" | "REQUIRE_STEP_UP" | "DENY";
  reason_codes: string[];
  policy_version?: number;
  request_id?: string;
  executed?: boolean;
  user_confirmed?: boolean;
  approval_id?: string;
  approval_status?: "PENDING" | "APPROVED" | "CONSUMED" | "EXPIRED" | "REVOKED";
  approval_method?: "USER_CONFIRMATION" | "STEP_UP" | "WEBAUTHN" | null;
  expires_at?: string;
  facts?: Record<string, unknown>;
  result?: AcceptReplanResponse;
  proposal_id?: string;
  status?: string;
}

export interface AgentApprovalResponse {
  approval_id: string;
  journey_id: string;
  action: string;
  target_type: string;
  target_id: string;
  status: "PENDING" | "APPROVED" | "CONSUMED" | "EXPIRED" | "REVOKED";
  approval_method: "USER_CONFIRMATION" | "STEP_UP" | "WEBAUTHN" | null;
  created_at: string;
  expires_at: string;
  approved_at: string | null;
  consumed_at: string | null;
}

export interface RouteFeature {
  type: "Feature";
  properties: {
    provider: string;
    provider_backed: boolean;
    fallback: boolean;
    fallback_reason: string | null;
    plan_version_id: string;
  };
  geometry: { type: "LineString"; coordinates: [number, number][] };
}

export interface RouteEvidence {
  journey_id: string;
  proposal_id: string | null;
  snapshot_id: string | null;
  provider: string;
  frozen: boolean;
  // A plan with no route serves no line: the backend refuses to invent one, so the map takes its
  // "nothing honest to draw" branch instead of being handed a hard-coded city.
  old_route: RouteFeature | null;
  new_route: RouteFeature | null;
  disruption_scope?: {
    type: "Feature";
    properties: Record<string, unknown>;
    geometry: { type: "Polygon" | "MultiPolygon"; coordinates: unknown };
  } | null;
  impacted_activity_ids?: string[];
  markers: {
    type: "FeatureCollection";
    features: Array<{
      type: "Feature";
      properties: { role: string; title: string; activity_id: string | null };
      geometry: { type: "Point"; coordinates: [number, number] };
    }>;
  };
  fallback: boolean;
  fallback_reason: string | null;
  source: string;
}

export interface AcceptReplanResponse {
  journey: {
    id: string;
    status: string;
    active_plan_version_id: string | null;
    current_activity_id: string | null;
  };
  proposal: ReplanProposal;
  twin: JourneyTwin;
}

export class CityNexusApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: unknown,
    message: string,
  ) {
    super(message);
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const releaseInFlight = beginNonIdempotentRequest(init?.method);
  try {
    return await requestJsonInner<T>(path, init);
  } finally {
    releaseInFlight();
  }
}

async function requestJsonInner<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetchWithReadDeadline("/api/v1" + path, {
    ...init,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const text = await response.text();
  /* ORD-0263: this channel used to hand the undecodable answer straight back as data (`body = text`),
     so nothing threw, nothing was classified and the screen simply said nothing.  Only a *failed*
     parse or a 2xx with no bytes at all counts -- a legitimate bare JSON string or a 204 stays as it
     was, because neither of those is this defect. */
  const emptyAnswer = text.trim() === "";
  let body: unknown = null;
  let parseFailed = false;
  if (!emptyAnswer) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
      parseFailed = true;
    }
  }
  if (!response.ok) {
    throw new CityNexusApiError(
      response.status,
      body,
      "CityNexus API " + response.status + ": " + path,
    );
  }
  if (parseFailed || (emptyAnswer && response.status !== 204)) {
    const contentType = response.headers.get("content-type");
    throw new OriginResponseUnusableError(
      response.status,
      contentType,
      emptyAnswer ? "empty" : (contentType ?? "").includes("html") ? "html" : "not_json",
    );
  }
  return body as T;
}

/** `GET /destinations/capability` -- the server's structural verdict about a destination. */
export interface DestinationCapability {
  query: string;
  /** READY / HYDRATABLE / UNAVAILABLE, decided server-side. */
  capability: string;
  reason_code: string;
  recommended_action: string;
  destination_state: string;
  destination_resolution: string;
  retryable: boolean;
  /** Present only for AMBIGUOUS: what the traveller has to choose between. */
  candidates?: Array<Record<string, unknown>>;
  matched_label?: string | null;
  canonical_destination_id?: string | null;
  /** The traveller's exact raw words, kept separately from the normalised resolver query. */
  pending_destination_label?: string | null;
  resolver_query_normalized?: string | null;
  coverage: Record<string, string>;
  missing_categories: string[];
  place_count: number;
  /** Absent unless something actually looked -- a default `OK` would be a fabricated measurement. */
  provider_status?: Array<{ provider?: string; status?: string; message?: string }>;
}

/** `POST/GET /catalog-hydrations[/{id}]` -- the job's own record.  No percentage by design. */
export interface CatalogHydrationOperation {
  operation_id: string;
  status: string;
  recorded_status: string;
  /** The stage the worker is genuinely inside; one of the server's PHASES. */
  phase: string;
  outcome: string | null;
  terminal: boolean;
  elapsed_ms: number;
  retry_after_ms: number;
  retryable: boolean;
  reused: boolean;
  canonical_destination_id: string | null;
  pending_destination_label: string | null;
  resolver_query_normalized: string | null;
  destination_state: string;
  result: Record<string, unknown> | null;
  /** The server's own error object; its `message_key` still has to be rendered as words. */
  error: Record<string, unknown> | null;
  journey_id: string | null;
  submitted_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  abandon_requested_at?: string | null;
}

export const cityNexusApi = {
  resetDemo(): Promise<DemoIds> {
    return requestJson<DemoIds>("/demo/reset", { method: "POST" });
  },

  /* ORD-0193: the destination-preparation surface.  These mirror the server's own projections --
     `GET /destinations/capability` (anywhere_capability.evaluate_capability) and the three
     catalog-hydration calls -- so the screen shows what the server decided rather than guessing a
     verdict from a place count.  There is deliberately no progress percentage: the payload carries
     `phase` and `elapsed_ms` and the backend refuses to invent a denominator. */
  getDestinationCapability(query: string): Promise<DestinationCapability> {
    return requestJson<DestinationCapability>(
      "/destinations/capability?query=" + encodeURIComponent(query),
    );
  },

  startCatalogHydration(
    query: string,
    options: { journeyId?: string | null; requestedCategories?: string[] } = {},
  ): Promise<CatalogHydrationOperation> {
    return requestJson<CatalogHydrationOperation>("/catalog-hydrations", {
      method: "POST",
      body: JSON.stringify({
        query,
        journey_id: options.journeyId ?? null,
        requested_categories: options.requestedCategories,
      }),
    });
  },

  getCatalogHydration(operationId: string): Promise<CatalogHydrationOperation> {
    return requestJson<CatalogHydrationOperation>(
      "/catalog-hydrations/" + encodeURIComponent(operationId),
    );
  },

  abandonCatalogHydration(operationId: string): Promise<CatalogHydrationOperation> {
    return requestJson<CatalogHydrationOperation>(
      "/catalog-hydrations/" + encodeURIComponent(operationId) + "/abandon",
      { method: "POST" },
    );
  },

  getJourney(journeyId: string): Promise<JourneyRecord> {
    return requestJson<JourneyRecord>("/journeys/" + journeyId);
  },

  listPlaces(cityCode: string): Promise<PlaceRecord[]> {
    return requestJson<PlaceRecord[]>("/places?city_code=" + encodeURIComponent(cityCode));
  },

  listEvents(journeyId: string): Promise<EventRecord[]> {
    return requestJson<EventRecord[]>("/journeys/" + journeyId + "/events");
  },

  getAgentPolicy(travelerId: string): Promise<AgentPolicy> {
    return requestJson<AgentPolicy>("/travelers/" + travelerId + "/agent-policy");
  },

  updateAgentPolicy(
    travelerId: string,
    policy: Pick<
      AgentPolicy,
      | "mode"
      | "auto_accept_replan"
      | "max_auto_replan_cost_delta_minor"
      | "max_auto_loss_minor"
      | "max_auto_schedule_drift_min"
      | "allowed_activity_types"
      | "allowed_booking_types"
      | "protected_commitment_levels"
      | "allow_auto_commitment_management"
      | "allow_auto_when_provider_degraded"
      | "allow_auto_on_critical_event"
    >,
  ): Promise<AgentPolicy> {
    return requestJson<AgentPolicy>("/travelers/" + travelerId + "/agent-policy", {
      method: "PUT",
      body: JSON.stringify(policy),
    });
  },

  generatePlans(journeyId: string, candidateCount = 1): Promise<PlanVersion[]> {
    return requestJson<PlanVersion[]>("/journeys/" + journeyId + "/plans/generate", {
      method: "POST",
      body: JSON.stringify({ candidate_count: candidateCount }),
    });
  },

  selectPlan(journeyId: string, planId: string): Promise<unknown> {
    return requestJson("/journeys/" + journeyId + "/plans/" + planId + "/select", {
      method: "POST",
    });
  },

  getPlan(planId: string): Promise<PlanVersion> {
    return requestJson<PlanVersion>("/plans/" + planId);
  },

  createBooking(journeyId: string, activityId: string): Promise<Booking> {
    return requestJson<Booking>("/journeys/" + journeyId + "/bookings", {
      method: "POST",
      body: JSON.stringify({ activity_id: activityId }),
    });
  },

  captureBooking(journeyId: string, booking: Booking, idempotencyKey: string): Promise<unknown> {
    return requestJson("/journeys/" + journeyId + "/transactions", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        booking_id: booking.id,
        transaction_type: "CAPTURE",
        amount: booking.amount,
      }),
    });
  },

  startJourney(journeyId: string): Promise<JourneyTwin> {
    return requestJson<JourneyTwin>("/journeys/" + journeyId + "/start", { method: "POST" });
  },

  completeActivity(journeyId: string, activityId: string): Promise<JourneyTwin> {
    return requestJson<JourneyTwin>(
      "/journeys/" + journeyId + "/activities/" + activityId + "/complete",
      { method: "POST" },
    );
  },

  getTwin(journeyId: string, now?: string): Promise<JourneyTwin> {
    const suffix = now ? "?now=" + encodeURIComponent(now) : "";
    return requestJson<JourneyTwin>("/journeys/" + journeyId + "/twin" + suffix);
  },

  getAgentFeed(journeyId: string, limit = 100): Promise<AgentFeedResponse> {
    return requestJson<AgentFeedResponse>(
      "/journeys/" + journeyId + "/agent-feed?limit=" + encodeURIComponent(String(limit)),
    );
  },

  getAgentRuntimeStatus(): Promise<AgentRuntimeStatus> {
    return requestJson<AgentRuntimeStatus>("/agent/runtime-status");
  },

  runAgentCommand(
    journeyId: string,
    command: string,
    requestedAutonomy: "PLAN_ONLY" | "EXECUTE_WITHIN_BOUNDS" = "EXECUTE_WITHIN_BOUNDS",
  ): Promise<AgentCommandResponse> {
    return requestJson<AgentCommandResponse>("/journeys/" + journeyId + "/agent/commands", {
      method: "POST",
      body: JSON.stringify({ command, requested_autonomy: requestedAutonomy }),
    });
  },

  sendCopilotMessage(
    journeyId: string,
    message: string,
    locale: "zh" | "en",
  ): Promise<CopilotResponse> {
    return requestJson<CopilotResponse>("/journeys/" + journeyId + "/copilot/messages", {
      method: "POST",
      body: JSON.stringify({ message, locale }),
    });
  },

  getNativeHermesRun(journeyId: string, runId: string): Promise<Record<string, unknown>> {
    return requestJson<Record<string, unknown>>(
      "/journeys/" + journeyId + "/agent/hermes/runs/" + encodeURIComponent(runId),
    );
  },

  getNativeHermesTimeline(journeyId: string, runId: string): Promise<{
    run_id: string;
    journey_id: string;
    runtime_identity: string;
    events: NativeRuntimeEvent[];
    observability?: {
      run_id: string;
      runtime_identity: string;
      current_mission: string | null;
      tool_count: number;
      current_tool: string | null;
      completion_status: string;
      gateway_decision: string | null;
      guardian_decision: string | null;
      total_duration_ms: number;
    };
  }> {
    return requestJson(
      "/journeys/" + journeyId + "/agent/hermes/runs/" + encodeURIComponent(runId) + "/timeline",
    );
  },

  stopNativeHermesRun(journeyId: string, runId: string): Promise<Record<string, unknown>> {
    return requestJson<Record<string, unknown>>(
      "/journeys/" + journeyId + "/agent/hermes/runs/" + encodeURIComponent(runId) + "/stop",
      { method: "POST" },
    );
  },

  getAgentRun(journeyId: string, runId: string): Promise<AgentRun> {
    return requestJson<AgentRun>(
      "/journeys/" + journeyId + "/agent/runs/" + runId,
    );
  },

  putGuardianMission(
    journeyId: string,
    command: string,
    requestedAutonomy: "PLAN_ONLY" | "EXECUTE_WITHIN_BOUNDS" = "EXECUTE_WITHIN_BOUNDS",
  ): Promise<AgentMission> {
    return requestJson<AgentMission>("/journeys/" + journeyId + "/guardian/mission", {
      method: "PUT",
      body: JSON.stringify({ command, requested_autonomy: requestedAutonomy }),
    });
  },

  scanGuardian(journeyId: string): Promise<GuardianRun> {
    return requestJson<GuardianRun>("/journeys/" + journeyId + "/guardian/scan", {
      method: "POST",
    });
  },

  getGuardianStatus(journeyId: string): Promise<GuardianStatus> {
    return requestJson<GuardianStatus>("/journeys/" + journeyId + "/guardian/status");
  },

  getResilience(journeyId: string): Promise<ResilienceSnapshot> {
    return requestJson<ResilienceSnapshot>("/journeys/" + journeyId + "/resilience");
  },

  refreshResilience(
    journeyId: string,
    mode: "replay" | "live" = "replay",
  ): Promise<{
    snapshot: ResilienceSnapshot;
    counterfactuals: CounterfactualSimulation[];
    discovery: Record<string, unknown> | null;
  }> {
    return requestJson("/journeys/" + journeyId + "/resilience/refresh", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
  },

  getProviderMesh(journeyId: string): Promise<ProviderMeshResponse> {
    return requestJson<ProviderMeshResponse>("/journeys/" + journeyId + "/provider-mesh");
  },

  discoverRealCity(
    journeyId: string,
    query = "indoor",
    mode: "replay" | "captured_replay" | "live" = "captured_replay",
  ): Promise<Record<string, unknown>> {
    return requestJson<Record<string, unknown>>("/journeys/" + journeyId + "/real-city/discover", {
      method: "POST",
      body: JSON.stringify({ query, mode, limit: 8, radius_m: 2500 }),
    });
  },

  startGuardianAutopilot(journeyId: string, intervalSeconds = 0.5): Promise<Record<string, unknown>> {
    return requestJson<Record<string, unknown>>(
      "/journeys/" + journeyId + "/guardian/autopilot/start",
      {
        method: "POST",
        body: JSON.stringify({ interval_seconds: intervalSeconds }),
      },
    );
  },

  stopGuardianAutopilot(journeyId: string): Promise<Record<string, unknown>> {
    return requestJson<Record<string, unknown>>(
      "/journeys/" + journeyId + "/guardian/autopilot/stop",
      { method: "POST" },
    );
  },

  setGuardianProviderFixture(
    journeyId: string,
    state: "NORMAL" | "DISRUPTED" | "OUTAGE" | "RISK_FORECAST",
    payload: Record<string, unknown> = {},
  ): Promise<GuardianStatus["signal_fixture"]> {
    return requestJson<GuardianStatus["signal_fixture"]>(
      "/journeys/" + journeyId + "/guardian/provider-fixture",
      {
        method: "POST",
        body: JSON.stringify({ state, payload }),
      },
    );
  },

  getReflect(journeyId: string): Promise<ReflectSummary> {
    return requestJson<ReflectSummary>("/journeys/" + journeyId + "/reflect");
  },

  getTravelerTwin(travelerId: string): Promise<TravelerTwinEvidence> {
    return requestJson<TravelerTwinEvidence>("/travelers/" + travelerId + "/traveler-twin");
  },

  getTravelerCapabilities(travelerId: string): Promise<TravelerCapabilityResponse> {
    return requestJson<TravelerCapabilityResponse>(
      "/travelers/" + travelerId + "/capabilities",
    );
  },

  confirmTravelerCapabilities(
    travelerId: string,
    capabilities: Array<Pick<TravelerCapability, "code" | "severity" | "params"> & Partial<TravelerCapability>>,
  ): Promise<TravelerCapabilityResponse> {
    return requestJson<TravelerCapabilityResponse>(
      "/travelers/" + travelerId + "/capabilities/confirm",
      { method: "POST", body: JSON.stringify(capabilities) },
    );
  },

  getJourneyCapabilities(journeyId: string): Promise<TravelerCapabilityResponse> {
    return requestJson<TravelerCapabilityResponse>(
      "/journeys/" + journeyId + "/capabilities",
    );
  },

  confirmJourneyCapabilities(
    journeyId: string,
    capabilities: Array<Pick<TravelerCapability, "code" | "severity" | "params"> & Partial<TravelerCapability>>,
  ): Promise<TravelerCapabilityResponse> {
    return requestJson<TravelerCapabilityResponse>(
      "/journeys/" + journeyId + "/capabilities/confirm",
      { method: "POST", body: JSON.stringify(capabilities) },
    );
  },

  clearTravelerTwinLearned(travelerId: string): Promise<TravelerTwinEvidence> {
    return requestJson<TravelerTwinEvidence>("/travelers/" + travelerId + "/traveler-twin/learned", {
      method: "DELETE",
    });
  },

  callAgentTool(
    tool: string,
    payload: {
      request_id: string;
      agent_id?: string;
      journey_id?: string;
      proposal_id?: string;
      trigger_event_id?: string;
      candidate_count?: number;
      approval_id?: string;
      user_confirmed?: boolean;
    },
  ): Promise<AgentToolResponse> {
    return requestJson<AgentToolResponse>("/agent/tools/" + tool, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  approveAgentAction(
    journeyId: string,
    approvalId: string,
    approvalMethod: "USER_CONFIRMATION" | "STEP_UP" | "WEBAUTHN" = "USER_CONFIRMATION",
  ): Promise<AgentApprovalResponse> {
    return requestJson<AgentApprovalResponse>(
      "/journeys/" + journeyId + "/agent/approvals/" + approvalId + "/approve",
      {
        method: "POST",
        body: JSON.stringify({ approval_method: approvalMethod }),
      },
    );
  },

  getRouteEvidence(journeyId: string, proposalId?: string): Promise<RouteEvidence> {
    const suffix = proposalId ? "?proposal_id=" + encodeURIComponent(proposalId) : "";
    return requestJson<RouteEvidence>("/journeys/" + journeyId + "/route-evidence" + suffix);
  },

  createClosureEvent(
    journeyId: string,
    activityId: string,
    occurredAt: string,
  ): Promise<EventRecord> {
    return requestJson<EventRecord>("/journeys/" + journeyId + "/events", {
      method: "POST",
      body: JSON.stringify({
        event_type: "CLOSURE",
        severity: "MAJOR",
        affected_activity_id: activityId,
        occurred_at: occurredAt,
        payload: { reason: "Demo Event Simulator" },
      }),
    });
  },

  createJourneyEvent(
    journeyId: string,
    eventType: "WEATHER" | "TRAFFIC_DELAY" | "CLOSURE" | "SCHEDULE_DELAY",
    severity: "INFO" | "MINOR" | "MAJOR" | "CRITICAL",
    activityId: string,
    occurredAt: string,
    payload: Record<string, unknown>,
  ): Promise<EventRecord> {
    return requestJson<EventRecord>("/journeys/" + journeyId + "/events", {
      method: "POST",
      body: JSON.stringify({
        event_type: eventType,
        severity,
        affected_activity_id: activityId,
        occurred_at: occurredAt,
        payload,
      }),
    });
  },

  generateReplans(
    journeyId: string,
    eventId: string,
    candidateCount = 3,
    idempotencyKey?: string,
  ): Promise<ReplanProposal> {
    return requestJson<ReplanProposal>("/journeys/" + journeyId + "/replans", {
      method: "POST",
      headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
      body: JSON.stringify({ trigger_event_id: eventId, candidate_count: candidateCount }),
    });
  },

  listReplans(journeyId: string): Promise<ReplanProposal[]> {
    return requestJson<ReplanProposal[]>("/journeys/" + journeyId + "/replans");
  },

  acceptReplan(proposalId: string, idempotencyKey: string): Promise<AcceptReplanResponse> {
    return requestJson<AcceptReplanResponse>("/replans/" + proposalId + "/accept", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    });
  },

  rejectReplan(proposalId: string, idempotencyKey?: string): Promise<ReplanProposal> {
    return requestJson<ReplanProposal>("/replans/" + proposalId + "/reject", {
      method: "POST",
      headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
    });
  },
};
