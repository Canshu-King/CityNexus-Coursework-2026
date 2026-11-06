# CityNexus Visio Model Specification

Current specification candidate: **v1.0.0**.

This specification is derived from Domain Model v0.4.2 RC3, Requirements Baseline v0.3.0, and Use Case Specification v0.1.0. Coordinates use centimetres with a logical top-left origin. Codex/Visio automation must translate Y coordinates when the Visio API uses a bottom-left origin.

Important current-baseline corrections already incorporated:

- Journey -> PlanVersion and PlanVersion -> Activity use `0..*`, matching the frozen Domain Model.
- Preference is a single scoped entity; obsolete `TravelerPreference` / `JourneyPreference` classes must not be drawn.
- `PaymentTransaction` replaces the obsolete generic `Transaction` name.
- `AIInsight` is not a Core Domain entity and is intentionally absent from the detailed discovery/commerce class diagram.
- ReplanProposal may have `0..*` CandidatePlan while in Draft.
