import type { FeasibilityEnvelope } from "./replan";

export interface Money {
  amount_minor: number;
  currency: string;
}

export interface CommitmentPolicy {
  semantic_key?: string;
  scheduled_start_at?: string;
  [key: string]: unknown;
}

export interface Commitment {
  id: string;
  journey_id: string;
  activity_id: string | null;
  kind: string;
  level: string;
  status: string;
  monetary_value: Money;
  external_ref: string | null;
  policy: CommitmentPolicy;
}

export interface JourneyTwinContext {
  journey_status: string;
  provider_level: number;
  degraded_providers: string[];
  feasibility_envelope: FeasibilityEnvelope;
  opportunity_window: {
    opens_at: string;
    must_depart_by: string;
    available_minutes: number;
    next_commitment_start: string;
  } | null;
}

export interface JourneyTwin {
  journey_id: string;
  active_plan_version_id: string | null;
  current_activity_id: string | null;
  latest_event_id: string | null;
  projection_status: "READY" | "STALE" | "DEGRADED";
  snapshot_at: string;
  upcoming_commitments: Commitment[];
  context: JourneyTwinContext;
}
