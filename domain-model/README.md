# CityNexus Core Domain Model

Current baseline: **v0.4.2 Release Candidate 3**
Source of truth: `citynexus-core-domain-model-v0.4.2.yaml`

This folder contains the working core-domain baseline for the official coursework project **Smart City Traveler System**, product name **CityNexus**.

The model is intentionally designed as the upstream source for:

- `requirements.yaml` and SRS traceability;
- Microsoft Visio UML Class, Use Case, Sequence and State Machine diagrams;
- demo-domain interfaces and planner data contracts;
- acceptance-test and traceability generation.

## Modelling boundaries

The model separates:

- traveler profile and scoped preferences;
- immutable journey plan versions and activities;
- real-world commitments and commercial records;
- deterministic constraints and feasibility decisions;
- live context snapshots, events and the `JourneyTwin` projection;
- verified places/services and contextual recommendations;
- progressive replanning with candidate plans and auditable score breakdowns.

`Journey` is the core aggregate root. `JourneyTwin` is a derived `<<projection>>`, not an independent source of truth. AI may assist intent extraction, semantic scoring and explanation, but deterministic feasibility rules remain authoritative.

## v0.4.1 release gates

The release validator lives at:

`./scripts/validate-domain-model.py`

Run it from any working directory:

```powershell
python .\scripts\validate-domain-model.py
```

It validates strict YAML parsing, JSON Schema compliance, typed references, UML multiplicity rules, lifecycle-state reachability, use-case traceability, relationship consistency, score weights, domain-rule IDs and agreed CityNexus scope coverage.

The generated report is written to:

`./qa/domain-model-v0.4.1-report.json`

Validator mutation self-tests can also be run with:

```powershell
python .\scripts\self-test-validator.py
```

They intentionally inject duplicate YAML keys, invalid lifecycle states, untyped arrays and reciprocal associations to prove the release gate rejects them. The resulting report is `./qa/validator-self-test.json`.

A machine-readable v0.4 → v0.4.1 change report is stored at `./qa/model-diff-v0.4-to-v0.4.1.json`.

A release candidate is acceptable only when the main report and validator self-tests contain zero failed checks.

## Versioning policy

Older model YAML files should be retained locally for history. Do not modify an activated baseline in place. New semantic changes should produce a new version and a changelog entry.

After v0.4.1 passes local validation, it is intended to be frozen as the **Domain Model v1.0 Candidate**. The next engineering artifact should be `requirements/requirements.yaml`; Visio page specifications should be derived after the requirement baseline is machine-readable and traceable.
