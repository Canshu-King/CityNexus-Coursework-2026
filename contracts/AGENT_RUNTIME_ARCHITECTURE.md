# CityNexus Agent Runtime V1

The Agent foundation is a constrained action layer over the existing CityNexus
domain services:

```text
Hermes or another Agent brain
    -> agent-mcp safe adapter
    -> /api/v1/agent/tools/{tool}
    -> AgentToolGateway
    -> persisted DelegationPolicy
    -> server-derived proposal/Event/Journey/C4 facts
    -> existing deterministic accept-time gate
    -> PlanVersion/Activity/Commitment domain truth
```

JourneyTwin remains a projection. The Agent cannot write SQLite, receive a
SQLAlchemy Session, supply provider evidence, supply feasibility facts, or
modify its own policy. It may select a server-known Activity as an `AI_ONLY`
semantic hypothesis, but cannot label it `VALIDATED_IMPACT`, select an
arbitrary Activity, or bypass the policy/gate boundary.

`request_replan` is proposal-only. `accept_replan` can return
`ALLOW_AUTO`, `REQUIRE_CONFIRMATION`, `REQUIRE_STEP_UP`, or `DENY`; only an
`ALLOW_AUTO` result calls the existing authoritative accept operation. An
ordinary reversible AI-only hypothesis may be auto-accepted when the
user-owned Autonomy Envelope and deterministic post-action gate pass. That
operation revalidates the proposal using the persisted MobilitySnapshot and
active C4 commitments. C4/other durable Commitment management is a separate
higher-risk action and remains explicitly pre-authorization/step-up gated.

The pinned V1 MCP surface remains unchanged. V2 adds typed
`replace_activity`, `reschedule_activity`, `create_booking`,
`cancel_refundable_booking`, and `request_commitment_change` commands behind
the same server-owned policy and deterministic gate. User confirmation is an
expiring, Journey-scoped, one-use approval receipt rather than a caller-
supplied boolean.

The server persists one `traveler_agent_policies` row per traveler. New
travelers receive `ADVISORY`, no automatic acceptance, zero cost allowance,
and no degraded-provider or CRITICAL-event exception. The envelope additionally
limits loss, schedule drift, Activity types, and protected Commitment levels.
