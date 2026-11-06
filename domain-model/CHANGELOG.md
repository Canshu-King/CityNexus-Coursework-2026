# CityNexus Domain Model Changelog

## 0.4.1 — Release Candidate 2 semantic closure

- Kept the core entity set frozen at 18 entities; no new core entity was introduced.
- Added `OpportunityWindow` as a value object and expanded `JourneyTwin` with current/next activity, next hard commitment, delay, walking distance, risk and opportunity-window projection fields.
- Added explicit Journey origin/final-destination Place references and associations for the Tianjin golden-path anchors.
- Added Activity estimated/actual cost and PlanVersion plan-level cost/travel/walking/end-time metrics so plan diffs are deterministic rather than generated from prose.
- Removed duplicate Booking/Order-to-PaymentTransaction relationships; retained a single payment-subject direction and the `booking_xor_order` invariant.
- Reversed the Activity/Commitment relationship so one Activity can carry multiple commitments while each Commitment binds to at most one Activity.
- Removed the ambiguous PlanVersion-to-ReplanProposal back-reference; tightened base-plan and candidate-plan multiplicities.
- Renamed the top-level `business_rules` collection to `domain_rules` and retained `DR-*` identifiers to avoid collision with formal Requirements `BR-*` IDs.
- Removed duplicated value-object `used_by` metadata; actual typed references are now the source of truth.
- Added machine-readable Place availability windows and typed array item declarations for event, commitment, activity, constraint, evidence and order-item arrays.
- Upgraded Constraint `kind` and `relaxationPolicy` to enums and added soft-constraint weight/violation-penalty fields.
- Added `DR-008` progressive replanning, `DR-009` PlanVersion immutability and `DR-010` AI authority-boundary rules.
- Corrected explainability semantics so Recommendation evidence and selected CandidatePlan/ScoreBreakdown audit data are distinct.
- Reworked Booking, Order and PaymentTransaction state enums/machines to match the agreed domain lifecycles.
- Removed unused `superseded` ReplanProposal state and changed Journey planning transition from `plan_created` to `plan_selected`.
- Converted traceability entries to a uniform object structure and marked UC-13 as an application/infrastructure concern with no forced core-domain entity mapping.
- Added relationship-role metadata where a typed entity reference maps directly to one UML association, enabling deterministic validation.
- Replaced the previous permissive validator with a strict duplicate-key YAML loader, typed-array/reference checks, state-enum/reachability checks, reciprocal-association checks, exact UC-01..UC-13 checks and requirements-coverage sanity gates.
- Preserved the package directory structure under `scripts/` and `qa/`.

## 0.4 — semantic hardening release candidate

- Fixed flow-mapping descriptions by emitting explicit quoted/block scalar strings.
- Changed Journey→PlanVersion to `1 -> 0..*` and replaced the unconditional active-plan invariant with lifecycle-specific conditions.
- Removed Journey→JourneyTwin projection and ContextSnapshot→Journey duplication; Journey now composes ContextSnapshots.
- Enforced UML multiplicity rules: association/aggregation/composition require multiplicity; dependency/projection/generalization do not.
- Reworked ReplanProposal around scope, trigger events, base plan and selected candidate; CandidatePlan now owns ranking, delta, rationale and typed ScoreBreakdown reference.
- Added Journey commercial traceability, payment-subject XOR invariant, health/booking/order/payment enums and state machines.
- Added typed entity fields with explicit enum, entity and value-object references.
- Corrected UC-03 traceability and linked Recommendation to JourneyTwin, Place and optional CityService.
- Added ContextSnapshot freshness, event impact levels, expanded event taxonomy and Exception Mode rule semantics.
- Added a release validator and QA report.

## 0.3 — semantic correction baseline

- Removed the duplicate `Journey -> Activity` composition; activities are owned by `PlanVersion`.
- Separated commitment strength (`C0_Flexible`–`C4_Anchored`) from lifecycle status.
- Added commitment `sourceType`, candidate plans, score breakdowns and context snapshots.
- Renamed `Transaction` to `PaymentTransaction` and normalised payment cardinalities.
- Replaced constraint hardness with `system_hard`, `commitment_hard` and `soft`, plus temporal/scope/relaxation fields.
- Added provider freshness metadata to places, services and events; added scoped preferences and explicit plan-version status.
- Renamed domain rule IDs from `BR-*` to `DR-*` and expanded use-case traceability.
- Corrected replanning policy for feasible versus infeasible active plans and Exception Mode.
- Normalised relationship cardinalities into `multiplicity.source` and `multiplicity.target` objects.
## 0.4.2 RC3 — domain promotion

- Promoted the reviewed v0.4.2 RC3 domain model while preserving v0.4.1 RC2 as an immutable historical baseline.
- Added the `planning_mode` enum and PlanVersion planning/scoring references required by the v0.3.0 requirements baseline.
