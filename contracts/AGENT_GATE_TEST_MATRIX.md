# Agent Runtime Gate Matrix

The DB/API gate is `backend/tests/test_agent_runtime_gate.py` and covers the
following required semantics:

| IDs | Covered behavior |
| --- | --- |
| AGT-01..03 | read tools, proposal-only request, advisory confirmation |
| AGT-04..09 | assisted policy, cost/C4/hard/degraded/CRITICAL guards |
| AGT-10..13 | protected tool exclusion, policy isolation, forged facts, ownership |
| AGT-14..16 | stale/resolved proposal and accept-time gate |
| AGT-17..18 | replay idempotency and AI_AGENT audit |
| AGT-19..23 | exact catalog, no Hermes dependency, injection rejection, proposal status |
| AGT-24 | auto-accept activates the new Plan, refreshes Twin, and preserves C4 |

