# CityNexus | The Complete Project Portal

**Research · Software Engineering · Full-stack Application · AI Agents · Human-first V3 · Classroom Simulation**

**Development evidence: September–November 2026**  
**Five public GitHub repositories. Multiple parallel engineering tracks. One CityNexus project.**

> **For the professor:** This portal makes the work behind our Software Engineering coursework visible and traceable. The official Part 2 submission was uploaded separately to Moodle. The repositories demonstrate planning, architecture, user-interface implementation, backend integration, agent experiments and testing. They are not a single hosted, production-ready application.

## The easiest review path

**[Two-minute screenshot walkthrough](DEMO_STORYBOARD.md)** · **[Feature evidence and verification matrix](FEATURE_VERIFICATION_MATRIX.md)** · **[Interactive visual site: prepared for GitHub Pages](SITE_DEPLOYMENT.md)**

The interactive site presents historic development captures, not a live travel system. These alternatives let a professor inspect the actual user-facing work without installing Node.js, Python or any private API credentials.

## What were we trying to build?

A conventional itinerary app may suggest where to go. **CityNexus aims to help a traveller manage an entire journey:** understand natural-language goals, find real places, propose a schedule, present a map and route, communicate with an AI agent, adapt to new information, coordinate companions and continue after human approval.

Our long-term research question was: **Can a tool-using Agent remain useful throughout a changing trip rather than stop at its first recommendation?**

### One traveller's story

| User moment | CityNexus engineering behind it | Where to look |
| --- | --- | --- |
| "I want two easy cultural days in a city." | Human-first V3 goal entry, traveller/session models and plan-preparation workflow | [Actual V3 first screen](../screenshots/v3-first-trip-goal-mobile.png) · [V3 branch](https://github.com/Canshu-King/CityNexus/tree/rescue/v3-final-departure-20261022) |
| "Find venues that match what I said." | Venue discovery and provenance through map/location/provider adapters | [Discovery UI capture](../screenshots/discovery-desktop.png) · [Provider code](../backend/app/integrations/discovery/) |
| "Show the alternatives, timing and transport." | PlanVersion, constraints, ranking, route evidence, trip studio and mobility comparison | [Mobility comparison](../screenshots/mobility-mobile.png) · [Planning code](../backend/app/planning/) |
| "Why did the Agent suggest that?" | Conversational Agent, mission/tool runtime, tool-status timeline and authorization | [V3 Agent timeline](../screenshots/v3-agent-tool-timeline-mobile.png) · [Agent runtime](../backend/app/agent/) |
| "The venue closes. Change my plans." | Guardian/context evidence, recovery proposal, approval and committed plan changes | [Recovery capture](../screenshots/recovery-proposal-mobile-zh.png) · [Replanning code](../backend/app/replanning/) |
| "My companion and I got separated." | Signed group membership, rendezvous/coordinated recovery and member-specific navigation research | [R62 experiment](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R62_GROUP_RECOVERY_FOLLOW_THROUGH_RESULT.md) · [Group tests](../experiments/revolution/backend/tests/test_r62_group_recovery_followthrough.py) |
| "We need to demonstrate two days in five minutes." | Classroom Presenter, virtual clock and mock-location execution, event injection and acceptance controls | [Classroom gallery](CLASSROOM_AND_V3.md) · [Original runbook](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261025_CLASSROOM_QUICK_CARD.md) |

That sequence represents the **intended product**, assembled across different workstreams. It must not be mistaken for one fully validated public deployment.

## Four major streams of work

### 01 · Main application and engineering foundation

React/TypeScript mobile and desktop views; Python/FastAPI backend; persistent Journey and PlanVersion state; routing and location adapters; planning constraints; bilingual UI, map, budget, lodging, trip history and automated tests.

- [Main full-stack implementation](../frontend/src/) · [Backend](../backend/app/) · [Feature-by-feature index](FEATURE_ATLAS.md)
- [Original CityNexus repository](https://github.com/Canshu-King/CityNexus) and the [November coursework source snapshot](https://github.com/Canshu-King/CityNexus/tree/coursework/part2-20261104-kickoff)
- [Representative real UI captures](VISUAL_GALLERY.md)

### 02 · Revolution Lab, the experimental Agent track

Research into model-guided itinerary choice, real-world source checks, adaptive recovery, continuing journey operation, human approval, group mediation and private two-stop walks.

- [Experiment cases with measured outcomes](EXPERIMENT_RESULTS.md)
- [Index of 103 historical result-note files](RESEARCH_ARCHIVE_INDEX.md)
- [Original Revolution Lab branch](https://github.com/Canshu-King/CityNexus/tree/gpt/revolution-r33-open-world-journey-adaptation)
- [Revolution Lab product charter](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_LAB_PRODUCT_DIRECTION_CHARTER.md)

The index records experiments, including unsuccessful ones; it is **not** a count of successful releases. The Oct 11 branch head includes an R109 taskbook, not an R109 verified result.

### 03 · Human-first Revolution V3 and real classroom interaction

A later Qoder-led V3 line focused on an interface a normal traveller could actually operate: one clear entry, natural-language goals, visible Agent progress, real-city discovery, an ACTIVE journey, map continuity, and a purpose-built simulated-time/position classroom demonstration. Codex separately worked on bounded backend/security and simulation support.

- **[V3 visual evidence and classroom demonstration](CLASSROOM_AND_V3.md)**
- [Original V3 product branch](https://github.com/Canshu-King/CityNexus/tree/rescue/v3-final-departure-20261022) (last checked Oct 29)
- [V3 actual human-first browser evidence](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261027_GO_FROM_BLANK_DELIVERY.md)
- [Oct 26 same-run Suzhou four-stop simulation](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261026_SUZHOU_FOUR_STOP_LIVE_CHAIN.md)
- [Separate security/simulation branch](https://github.com/Canshu-King/CityNexus/tree/codex/sandbox-execution-security-20261025)

**Important distinction:** The Suzhou research used real external provider/model requests, but travel time, GPS, activity completion and a two-day trip were **simulated**. The reported AgentRun was PARTIAL and contained tool errors. The demonstration is not a physical field trip. The classroom user/tablet acceptance remained separately unverified in the Oct 29 handoff.

### 04 · Formal Software Engineering coursework and project management

The project was not limited to coding. It produced a 138-item **candidate** requirements baseline with acceptance criteria and traceability, a domain model with 18 core entities, 13 main use cases, diagrams in editable Visio, and schedule/network/WBS artifacts in Microsoft Project.

- [Requirements baseline](../requirements/README.md) · [Domain model](../domain-model/BASELINE.md) · [Use cases](../use-cases/BASELINE.md)
- [Architecture and Agent/user flow PDFs](../teacher-materials/README.md)
- [Gantt and milestones](../screenshots/gantt-english.png) · [Editable MPP](../teacher-materials/project-overview.mpp)
- [Original Coursework Assignment Brief](https://github.com/Canshu-King/CityNexus-Workspace/blob/master/_coursework/Software%20Engineering%20-%20Assignment%20Coursework%202026.pdf) (this is the **assignment brief**, not the submitted final Part 2 PDF)
- The submitted Part 2 PDF is stored in **Moodle**, not automatically mirrored into this GitHub portal.

## The five public repositories

| Repository | What it contributes |
| --- | --- |
| **[CityNexus](https://github.com/Canshu-King/CityNexus)** | The primary historical source repository, multiple competing branches, full code, lab results, V3 human-first and classroom development. **Its default branch is an older September snapshot.** Use the relevant branch links above. |
| **[CityNexus-Coursework-2026](https://github.com/Canshu-King/CityNexus-Coursework-2026)** | **You are here.** Curated reviewer-friendly overview, snapshots, illustrations and selected engineering deliverables. |
| **[CityNexus-Workspace](https://github.com/Canshu-King/CityNexus-Workspace)** | Early academic references, September handoff notes, source assignment brief, Visio preparation and research workspace. |
| **[CityNexus-Lab-Archive](https://github.com/Canshu-King/CityNexus-Lab-Archive)** | Earlier WorkBuddy/DSH experience, testing and independent lab archives. These are historical development materials, not a second production app. |
| **[CityNexus-History](https://github.com/Canshu-King/CityNexus-History)** | Earlier V14/V15 release staging, snapshots and development history, useful when tracing how the product was built. |

The three secondary original repositories were imported largely as historical snapshots in September. The later October work is concentrated in **branches of the main CityNexus repository**, not in their default-branch commit graphs.

**[Full repository and branch navigation](DEVELOPMENT_STREAMS.md)** · **[Development timeline](DEVELOPMENT_TIMELINE.md)**

## Three particularly informative results

**1. A real routing measurement:** One R41 Valhalla on-demand walking route between two Nara places produced a **22-minute** source-bound mobility record. That demonstrates one grounded route acquisition, **not real physical movement**. [Original report](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R41_ROUTE_SESSION_RESULT.md).

**2. Agent chose a backup after a disruption:** In an isolated R20 local run, a configured external Agent inspected a disrupted Journey, selected a compatible standby, requested human authority and continued through the local execution state machine to a terminal Journey. This is **local system completion**, not an actual day outdoors. [Original report](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R20_AUTONOMOUS_BRANCH_ARBITRATION_RESULT.md).

**3. Four stops over two virtual days:** In the V3 Suzhou report, a single signed simulated run completed four city places, with real external provider calls and an external Guardian/Agent execution that was explicitly **PARTIAL**. The user-authorized proposal and simulation states were persisted. [Original report](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261026_SUZHOU_FOUR_STOP_LIVE_CHAIN.md).

The third case is particularly relevant to our classroom problem: a two-day storyline can be replayed in minutes **without claiming the traveller physically moved**.

## Reading path for a professor

1. **See the product:** [visual gallery](VISUAL_GALLERY.md) and [newer V3 captures](CLASSROOM_AND_V3.md).
2. **Understand what the Agent can do:** [guided tour](START_HERE_FOR_REVIEWER.md), [feature atlas](FEATURE_ATLAS.md).
3. **Evaluate experiments:** [ten reviewed cases](EXPERIMENT_RESULTS.md), then raw linked reports in the original public repo.
4. **Evaluate the course deliverables:** [requirements](../requirements/README.md) and [teacher materials](../teacher-materials/README.md).
5. **Trace engineering provenance:** [branch map](DEVELOPMENT_STREAMS.md) and [timeline](DEVELOPMENT_TIMELINE.md).

**Honest status:** We built and tested substantial components in different branches and controlled environments; we did **not** successfully demonstrate the complete live system to the professor in class, and this curated snapshot has not been retested as one merged production release. Where a test was blocked, only partially successful or fixture-backed, we preserve that qualification. The work is real even when integration or presentation was incomplete.
