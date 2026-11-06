# CityNexus in 7 Minutes: Professor's Guided Tour

**Software Engineering | Smart City Traveler System | September-November 2026**

CityNexus explores what a travel assistant could do **after** it first suggests an itinerary: discover places using source-bound evidence, explain options, track a journey, detect material changes, ask for authorization when a plan needs to change, and continue the trip.

This is a **source-backed explanation**, not a hosted full-product demo. Each link leads to real code or an engineering artifact in this public repository.

## Step 1 — See the traveller's experience

The [main React interface](../frontend/src/app/V8Experience.tsx) coordinates several real screen modules:

- [Trip setup](../frontend/src/features/setup/JourneyTrialSetup.tsx) and [traveller preferences](../frontend/src/features/traveler-twin/TwinPanel.tsx)
- [Place discovery](../frontend/src/features/discovery/RealCityDiscoveryPanel.tsx) and [map](../frontend/src/components/JourneyMap.tsx)
- [AI-assisted plans](../frontend/src/features/planning/PlanWithAiSheet.tsx) and [plan comparison](../frontend/src/features/planning/PlanComparison.tsx)
- [Activity itinerary](../frontend/src/features/trip/TripItinerary.tsx), [mobility](../frontend/src/features/mobility/MobilitySheet.tsx) and [spending](../frontend/src/features/trip/SpendPanel.tsx)

## Step 2 — Find the Agent

The travel assistant is not just a decorative chatbot. The [Agent conversation UI](../frontend/src/features/agent/AgentComposer.tsx) supports requests to explain a plan, evaluate what-if questions, discover places or replan. [AgentRunRail](../frontend/src/features/agent/AgentRunRail.tsx) and the [control tower](../frontend/src/features/intelligence/ControlTowerPanel.tsx) expose the run/tool status and autonomy policy.

In the backend, inspect the [Agent runtime](../backend/app/agent/runtime.py), [tool catalogue](../backend/app/agent/tool_catalog.py), [Guardian](../backend/app/agent/guardian.py) and [MCP tools](../agent-mcp/).

## Step 3 — What if reality changes?

The project models active plans, protected commitments and approval before consequential modifications. Read the [recovery interface](../frontend/src/features/recovery/RecoveryRequiredPanel.tsx), [live execution](../frontend/src/features/execution/V10LiveExecutionPanel.tsx), [trip editor](../frontend/src/features/studio/V12TripStudioPanel.tsx), and [backend replanning logic](../backend/app/replanning/).

A [simulation panel](../frontend/src/features/simulation/SimulationPanel.tsx) implements explicit travel-stage actions so scenarios can be exercised in a classroom without physically travelling.

## Step 4 — Read the actual experiments

[Experiment results](EXPERIMENT_RESULTS.md) describes ten tangible research scenarios, including an Agent's locally executed plan adjustment in Chongqing, a live Valhalla walking route in Nara, member-scoped group recovery and a two-stop Pocket Walk tested in mobile Chromium.

Each result distinguishes live providers from controlled fixtures, self-reporting from physical presence, and implemented code from blocked or partial demonstrations.

## Step 5 — Inspect the Software Engineering coursework

- [System architecture PDF](../teacher-materials/system-overview.pdf)
- [Agent / user flow PDF](../teacher-materials/agent-user-flow.pdf)
- [Use-case diagram](../teacher-materials/use-cases.pdf)
- [Gantt chart](../teacher-materials/gantt.pdf), [WBS](../teacher-materials/wbs.pdf) and [dependency diagram](../teacher-materials/network-diagram.pdf)
- [Editable Visio and Project files](../teacher-materials/README.md)
- [138 candidate requirements](../requirements/README.md), [domain model](../domain-model/) and [traceability](../requirements/traceability.yaml)

## A larger implementation index

For a systematic breakdown of the frontend, backend, external providers, Agent runtime, shared journeys and test infrastructure, open the [Feature Atlas](FEATURE_ATLAS.md).

**Evidence boundary:** This public copy has not been retested as one complete deployment after credential redaction. The stages above describe the intended system and available modules; the research results state what was actually tested. The Part 2 PDF was submitted separately through Moodle.
