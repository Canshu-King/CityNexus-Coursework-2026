# Delegation Policy V1

The user policy API is:

- `GET /api/v1/travelers/{traveler_id}/agent-policy`
- `PUT /api/v1/travelers/{traveler_id}/agent-policy`

The Agent tool catalog does not contain a policy mutation tool. Policy updates
increment the server-owned `version` and are audited as traveler changes.

For `accept_replan`, the gateway derives these facts from persistence:

- proposal status, hard feasibility flag, base plan identity;
- persisted diff and verified feasibility envelope;
- locked/C4 preservation evidence;
- trigger Event status and severity.

The request body is typed and rejects unknown fields. Client fields such as
`hard_constraints_passed`, `c4_preserved`, `provider_ready`, `cost_delta`, and
feasibility results are not part of the Agent contract.

`ADVISORY` always requires confirmation. `ASSISTED` and `AUTONOMOUS` can
auto-accept only when the policy toggle, cost ceiling, provider state, event
severity, proposal status, base plan, and trigger state all pass. The final
deterministic gate remains authoritative.

