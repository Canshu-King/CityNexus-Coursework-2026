import type { TravelerCapability } from "../api/citynexus";
/* ORD-0520: the live-screen decision is typed from the contract, not re-declared here, so the
   shell cannot quietly invent a third value for a field the server answers with two. */
import type { components } from "../generated/api";

export type JourneyLiveView = components["schemas"]["JourneyLiveView"];

/** The trip length as the backend counts it. ORD-0063: the client no longer derives days. */
export type TripShape = {
  day_count: number | null;
  night_count: number | null;
  semantics: string;
  window_status: string;
  min_days: number;
  max_days: number;
};

/** The traveller's own budget divided by that authoritative shape (ORD-0064). */
export type BudgetReference = {
  available: boolean;
  amount_minor: number | null;
  currency: string | null;
  party_size: number | null;
  day_count: number | null;
  per_day_minor: number | null;
  per_person_per_day_minor: number | null;
  floor_minor: number | null;
  floor_currency: string;
  floor_comparable: boolean;
  reason: string | null;
};

/** The coordinate an activity happens at, and the system it is written in.
 *  `state`/`reason` are the frozen capability tokens (`CAPABILITY_STATES`, `REASON_*`), so an
 *  unjoinable activity is a named answer rather than a missing row. */
export interface ActivityCoordinate {
  state: "READY" | "HYDRATABLE" | "UNKNOWN_RETRYABLE" | "UNAVAILABLE";
  reason?: string | null;
  latitude: number | null;
  longitude: number | null;
  coordinate_system: string | null;
  source_coordinate_system?: string | null;
}


export type V8Data = {
  journey: { id: string; traveler_id: string; title: string; city_code: string; timezone: string; start_at: string; end_at: string; budget: { amount_minor: number; currency: string }; temporal_shape?: TripShape | null; status?: string; aborted_at?: string | null; abort_reason?: string | null; live_view?: JourneyLiveView | null };
  trip: { arrival_anchor: { at?: string; location?: string }; departure_anchor: { at?: string; location?: string }; party_size: number; occupancy: Record<string, unknown>; budget_reference?: BudgetReference | null; stay_budget: { amount_minor: number; currency: string } };
  offers: Array<{ id: string; external_offer_id: string; property_name: string; room_type: string; total_amount_minor: number; nightly_amount_minor: number; currency: string; refundable: boolean; check_in_at: string; check_out_at: string; cancellation_policy: { summary?: string }; evidence: { source?: string; confidence?: number }; location: { latitude: number; longitude: number; crs?: string } }>;
  options: Array<{ id: string; rank: number; status: string; score: number; stay: V8Data["offers"][number]; stay_total_minor: number; daily_travel_minor: number; mobility_total_minor: number; walking_min: number; protected_margin_min: number; arrival_fit: number; departure_fit: number; cancellation_score: number; days: Array<{ day: number; label: string; date?: string; anchor: string; activities: string[]; mode: string }>; leg_modes: string[]; leg_mode_map: Array<{ from_key: string; to_key: string; mode: string; mobility_fact_id?: string }>; journey_impact: Record<string, any>; hard_constraints_passed: boolean; optimizer_kind: string; weather_state: string; materialized_plan_version_id?: string | null }>;
  selected_option_id: string | null;
  mobility_facts: Array<{ id?: string; mode: string; from_key: string; to_key: string; duration_min: number; cost_amount_minor: number | null; currency: string | null; cost_status: string; walking_min: number; transfers: number; weather_exposure: string; reliability_score: number; provider_key?: string; provenance?: string; crs?: string; geometry?: { type: "Feature"; properties?: Record<string, unknown>; geometry: { type: "LineString"; coordinates: number[][] } } }>;
  active_plan: null | { id: string; version_no: number; status: string; source: string; mobility_snapshot_id?: string | null; mobility_evidence?: { snapshot_id?: string | null; expires_at?: string | null; aggregate_provenance?: string; replayable?: boolean }; current_activity_id: string | null; cursor_status?: "VALID" | "STALE" | "ABSENT_FALLBACK"; activities: Array<{ id: string; sequence_no: number; type: string; status: string; title: string; place_id?: string | null; city_service_id?: string | null; start_at: string; end_at: string; locked: boolean; protected: boolean; metadata: { latitude?: number; longitude?: number; crs?: string; role?: string };
    /** ORD-0394: the place's own coordinate, joined at read. `metadata` above stays the store's
     *  transition record; a coordinate was never written into it, and this is where the map looks. */
    location?: ActivityCoordinate | null; }>; legs: Array<{ id: string; from_key: string; to_key: string; mode: string; duration_min: number; provider_key: string; provenance: string; crs: string;
    /* ORD-0504: the price of a segment. The server has been sending these three since the leg table
       was cut (`journey_legs.cost_amount_minor / currency / cost_status`,
       `backend/db/schema.sql`), and `GET /journeys/{id}/experience` answers them -- measured on
       `c2bb95b8f4d78e6aacbb80773594020f1992f8a2` in `review/ord0504/spend_surface_r1.json`, which
       records `$.active_plan.legs[0].cost_amount_minor = 4500` beside `cost_status = KNOWN` and a
       second leg with `cost_amount_minor = null` and `cost_status = UNKNOWN`. What was missing was
       this declaration, not the data: a field the type does not name is a field no screen can read
       without a cast, which is how the 花费 tab came to say nothing about transport cost.
       `cost_amount_minor` is nullable and `cost_status` is the server's word for why -- the pair is
       the difference between "this leg is free" and "nobody has priced it". */
    cost_amount_minor?: number | null; currency?: string | null; cost_status?: string;
    depart_at?: string | null; arrive_at?: string | null;
    geometry: { type: "Feature"; properties?: Record<string, unknown>; geometry: { type: "LineString"; coordinates: number[][] } } }>; user_location: null | { latitude: number; longitude: number; accuracy_m: number; captured_at: string; source: string }; disruptions: Array<{ id: string; type: string; severity: string; status?: string; affected_activity_id: string | null; occurred_at: string; resolved_at?: string | null }> };
  traveler_twin?: { explicit_dimensions: string[]; learned_soft_dimensions: string[]; authority: string; explicit_capabilities?: TravelerCapability[]; capability_summary?: Record<string, unknown> };
  provider_mesh?: Record<string, any>;
  recovery?: RecoveryState | null;
  experience_flags?: {
    needs_review: boolean;
    recovery_required: boolean;
    staleness: boolean;
    feasibility_failures: string[];
  };
};

/**
 * Durable, governed recovery state returned by `/experience` and by the
 * capability confirm endpoints.  Present only when a confirmed HARD capability
 * (or an evidence regression) invalidated the active plan.  The active plan is
 * never silently mutated.
 */
export type RecoveryState = {
  state: "RECOVERY_REQUIRED" | "RECOVERY_REQUIRED_NO_ALTERNATIVE" | "RESOLVED";
  kind: string;
  event_id: string;
  event_status: "DETECTED" | "ACKNOWLEDGED" | "RESOLVED";
  acknowledged: boolean;
  no_alternative: boolean;
  capability_code: string | null;
  active_plan_version_id: string | null;
  failures: string[];
  candidate_plan_count: number;
  candidate_plan_ids: string[];
  requires_user_decision: boolean;
  silent_plan_mutation: false;
  actions: Array<"REPLAN" | "ACKNOWLEDGE_NO_ALTERNATIVE">;
  detected_at: string;
  journey_status?: string;
};

export const v8Modes = ["WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"] as const;
export type V8Mode = (typeof v8Modes)[number];
