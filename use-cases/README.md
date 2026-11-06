# CityNexus Use Case Specification

Current baseline candidate: **v0.1.0**.

This package derives the 13 authoritative top-level use cases from the approved CityNexus Requirements Baseline v0.3.0 and Core Domain Model v0.4.2 RC3. It is the machine-readable source for detailed SRS use-case narratives and Visio use-case diagrams.

Important modelling rules:

- UC-01..UC-13 are the only top-level business use cases.
- UC-04.x, UC-05.x and UC-10.x are focused Part 2 documentation sub-use-cases only.
- UC-07 extends UC-06 only when payment is required.
- UC-09 extends UC-08 only when an event is detected.
- UC-10 extends UC-09 only when replanning is required.
- The planning solver is internal and is not an actor.
- AI output is advisory/semantic and never directly owns feasibility or persistence mutation.
