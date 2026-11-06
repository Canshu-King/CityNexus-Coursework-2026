# CityNexus Requirements Baseline

Current baseline: **v0.3.0 Candidate**  
Official coursework project: **Smart City Traveler System**  
Product: **CityNexus**  
Domain reference: `../domain-model/citynexus-core-domain-model-v0.4.2.yaml`

This directory is the machine-readable source of truth for the CityNexus requirements baseline. It contains **138 formal requirements**:

- 8 Business Goals (`BG`)
- 61 Functional Requirements (`FR`)
- 24 Business Rules (`BR`)
- 16 Non-functional Requirements (`NFR`)
- 14 Security Requirements (`SR`)
- 15 Integration Requirements (`IR`)

Domain rules use `DR-xxx` and remain separate from formal requirements business rules (`BR-xxx`).

## Baseline structure

Each requirement records:

- a stable ID and requirement type;
- a concise title and normative statement;
- typed source codes;
- MoSCoW priority (`M`, `S`, `C`, `W`);
- rationale;
- at least one requirement-owned acceptance criterion;
- Use Case and domain-element links;
- dependency and conflict links;
- verification method;
- future test-case links;
- lifecycle status.

`BASELINE.md` records freeze metadata and integrity hashes. `traceability.yaml` is a derived second view. Its Use Case, domain-element and test-case references must mirror the corresponding fields in `requirements.yaml`. Diagram and test-case references are intentionally empty until those later baselines are created.

## Source codes

- `CW` — formal coursework boundary
- `UJ` — user journey and product design
- `DM` — domain model
- `OPT` — planning and optimisation design
- `AI` — AI architecture
- `SEC` — security and trust design
- `ARCH` — software architecture
- `GH-PR` — PlacesRecommendation research
- `GH-TS` — Travel Story research
- `GH-OPT` — solver and optimisation research

The teacher/customer has not yet approved the baseline, so no requirement currently uses a customer-confirmed source code.

## Validation

Run from the project root or this directory:

```powershell
python .\requirements\scripts\validate-requirements.py
python .\requirements\scripts\self-test-requirements-validator.py
python .\requirements\scripts\generate-requirements-reports.py
```

The release gate validates strict YAML loading, JSON Schema conformance, exact counts, continuous IDs, domain references by name and kind, Use Case references, acceptance-criterion ownership, verification-method consistency, dependency cycles, traceability mirror consistency and candidate-status rules. Coverage, source/priority, Use Case and domain-mapping reports are generated under `qa/`.

## Current limitations by design

- Formal Test Cases are not yet defined; `testCases` therefore remains empty.
- Visio diagram IDs are not yet assigned; `diagrams` therefore remains empty.
- Requirements are `proposed` until teacher/customer review promotes an agreed baseline.
