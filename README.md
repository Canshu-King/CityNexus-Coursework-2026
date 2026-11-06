# CityNexus | Smart City Traveler System

**Software Engineering coursework companion and implementation showcase (2026)**

CityNexus is a research and implementation project exploring an AI-assisted urban travel system. It combines journey planning, maps, location discovery, contextual updates, agent tooling, and user-controlled changes to an itinerary.

> **For the professor:** The official Software Engineering Part 2 PDF was submitted separately on Moodle. This repository is supplementary development evidence, not the formal coursework submission and not a claim that the entire application was successfully demonstrated in class. The live classroom demonstration remained incomplete.

## Five-minute project tour

| Review order | Start here |
| --- | --- |
| 1. System idea | [System overview diagram (PDF)](teacher-materials/system-overview.pdf) |
| 2. Human and Agent responsibilities | [Agent-user interaction (PDF)](teacher-materials/agent-user-flow.pdf) |
| 3. Requirements | [138 formal candidate requirements](requirements/README.md) and [use cases](use-cases/) |
| 4. Planning | [Gantt](teacher-materials/gantt.pdf), [WBS](teacher-materials/wbs.pdf), [network diagram](teacher-materials/network-diagram.pdf), [editable Microsoft Project file](teacher-materials/project-overview.mpp) |
| 5. Implementation | [React frontend](frontend/src), [FastAPI backend](backend/app), [Agent tools](agent-mcp/), [API contracts](contracts/) |
| 6. Experimental research | [Revolution Lab overview](docs/REVOLUTION_LAB.md) and [separate experimental code](experiments/revolution/) |

## What is in the repository?

**Frontend:** React 19, TypeScript, Vite, MapLibre, user-facing journey and map components, agent activity views and automated browser test sources.

**Backend:** Python 3.12+, FastAPI, SQLAlchemy, API services, journey operations, planning, integrations, agent orchestration and test sources.

**Formal software engineering:** 138 requirements (8 business goals, 61 functional requirements, 24 business rules, 16 non-functional requirements, 14 security requirements, 15 integration requirements), traceability records, domain model, use cases, diagrams, and project planning artifacts.

**Revolution Lab:** separate experimental work on proactive monitoring, recovery proposals, multi-person journeys and short city walks. A laboratory test is not automatically proof of real-world deployment.

## Read before assessing the implementation

- [Architecture and directory map](docs/PROJECT_OVERVIEW.md)
- [Evidence and honest limitations](docs/EVIDENCE_AND_LIMITATIONS.md)
- [Revolution Lab scope and status](docs/REVOLUTION_LAB.md)
- [Local developer setup](docs/RUNNING_LOCALLY.md)
- [Editable artifacts and PDFs](teacher-materials/README.md)
- [Publication / privacy / provenance](docs/PUBLICATION_AND_PROVENANCE.md)

## Evidence boundaries

- The 138-requirement v0.3 baseline is **candidate**, not confirmed by the teacher/customer.
- The snapshot contains code and test sources; **a fresh end-to-end test of this export was not performed**.
- Controlled test-provider fixtures and traveler self-reports do **not** prove physical arrival or reliable operation with every live provider.
- The mainline code and Revolution Lab code are **two separate snapshots**, not one proven combined build.
- The original private source history, secrets, local data, duplicate experiments and review logs are intentionally **not part of this candidate public release**.

**Academic disclosure:** AI-assisted coding and writing tools were used during development. No explicit open-source software license is granted by this publication.
