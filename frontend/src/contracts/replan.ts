export type ReplanCandidateFamily =
  | "F0_SAME_SLOT_REPLACEMENT"
  | "F1_OPTIONAL_REMOVAL"
  | "F2_SUFFIX_DELAY"
  | "F3_OPPORTUNITY_WINDOW"
  | "ORTOOLS";

export interface FeasibilityEnvelope {
  next_commitment_buffer_min: number | null;
  journey_end_slack_min: number;
  budget_remaining_minor: number;
  preserved_commitment_count: number;
  degraded_providers: string[];
  risk_reason_codes: string[];
  robust: boolean;
}

export interface ReplanDiff {
  candidate_family: ReplanCandidateFamily | string;
  kept: string[];
  moved: Array<{
    semantic_key: string;
    old_start_at: string;
    new_start_at: string;
    delta_minutes: number;
  }>;
  removed: string[];
  added: string[];
  cost_delta_minor: number;
  travel_delta_min: number;
  end_time_delta_min: number;
  preserved_protected: string[];
  before_feasibility_envelope: FeasibilityEnvelope;
  after_feasibility_envelope: FeasibilityEnvelope;
  reason_codes: string[];
  opportunity_window_evidence?: Record<string, unknown> | null;
  verified_evidence?: Record<string, unknown>;
}

export interface ReplanProposal {
  id: string;
  journey_id: string;
  trigger_event_id: string | null;
  base_plan_version_id: string;
  proposed_plan_version_id: string;
  status: "GENERATED" | "PRESENTED" | "ACCEPTED" | "REJECTED" | "EXPIRED" | "INVALIDATED";
  utility_score: number;
  score_breakdown: Record<string, number>;
  locked_commitments_preserved: boolean;
  hard_constraints_passed: boolean;
  diff: ReplanDiff;
  explanation: Record<string, unknown>;
}
