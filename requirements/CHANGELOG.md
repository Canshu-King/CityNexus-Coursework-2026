# CityNexus Requirements Changelog

## 0.3.0 — requirements baseline candidate

- Imported and structured the approved 138-item proposed baseline:
  - 8 Business Goals
  - 61 Functional Requirements
  - 24 Business Rules
  - 16 Non-functional Requirements
  - 14 Security Requirements
  - 15 Integration Requirements
- Added one requirement-owned, verifiable acceptance criterion to every requirement.
- Added concise rationale and an explicit verification method to every requirement.
- Linked requirements to the fixed Use Case catalogue (`UC-01` through `UC-13`).
- Linked requirements to CityNexus domain entities, value objects, enums and domain rules by exact name and kind.
- Created 138 mirrored traceability mappings.
- Kept diagram and Test Case links empty pending their dedicated baselines.
- Upgraded the requirement and traceability schemas to v0.3.0.
- Added controlled verification-method values and baseline-candidate exact-count constraints.
- Added continuous-ID, acceptance-method, core Use Case mapping and candidate-status release checks.
- Expanded mutation testing from 11 to 17 negative cases.
- Added deterministic coverage and domain-mapping report generation.

## 0.2.0 — hardened pre-baseline infrastructure

- Kept all six concrete requirement sections empty.
- Replaced free-text `source` with typed `sources` codes.
- Standardised MoSCoW priority values to `M`, `S`, `C`, `W`.
- Changed acceptance-criterion IDs to requirement-owned identifiers such as `FR-010-AC-01`.
- Added section / prefix / requirement-type consistency rules.
- Added exact domain-reference validation by both name and kind.
- Added dependency-cycle, self-reference and depends/conflicts overlap checks.
- Added semantic duplicate detection for acceptance criteria.
- Added traceability uniqueness, completeness and mirror-consistency checks.
- Added real dangling trace test-case validation.
- Added exact 138-item counts as a release gate for candidate and approved status.
- Added PASS/FAIL/SKIP reporting.
- Added 11 mutation self-tests.
