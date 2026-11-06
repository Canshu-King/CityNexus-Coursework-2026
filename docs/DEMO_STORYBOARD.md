# Two-Minute Professor Walkthrough

**CityNexus · Smart City Traveler System · Software Engineering coursework, 2026**

This is a short, English-language story that guides a reviewer through **original development screenshots and verifiable source evidence**. It is not presented as a continuous recording of the complete application. For a click-through gallery, use the [interactive HTML page source](index.html); the owner can publish it on GitHub Pages from the \`main\` branch's \`/docs\` folder.

> The screenshots below come from controlled October browser captures, the full-stack implementation and actual classroom design artifacts. A screenshot is not proof of a physical trip or production deployment.

## A 120-second viewing path

| Time | What to show | English narration for the professor |
| --- | --- | --- |
| 0–15 s | [V3 traveller entry](../screenshots/v3-first-trip-goal-mobile.png) | "Our project asks what happens after an AI assistant initially plans a trip. We redesigned the mobile entry so a normal traveller could describe their destination and needs without navigating technical controls." |
| 15–30 s | [Agent tool timeline](../screenshots/v3-agent-tool-timeline-mobile.png) | "The assistant can show its recorded progress and tool calls. We wanted the user to know what the Agent was doing and when a tool was unavailable." |
| 30–45 s | [City discovery](../screenshots/discovery-desktop.png) | "The planning system connects recommendations to real place identities, evidence and geographical context. This specific capture was made in a controlled replay." |
| 45–60 s | [Mapped places](../screenshots/v3-real-map-mobile.png) | "The mobile UI displays planned places on a rendered map. We distinguish provider-backed venue and route facts from guessed or simulated positions." |
| 60–75 s | [ACTIVE journey](../screenshots/v3-active-journey-mobile.png) | "Unlike a one-shot travel list, the project models execution and a continuing journey. A controlled Chrome test reached an ACTIVE state after completing the local planning workflow." |
| 75–95 s | [Classroom control and proposal](../screenshots/v3-classroom-simulation-control.png) | "Because we cannot actually travel for two days during a short class, we created virtual time and position controls, event injection and a user approval step. The simulation is clearly labelled." |
| 95–110 s | [Changed itinerary after approval](../screenshots/v3-classroom-approved-change.png) | "A proposed change is not applied silently. The traveller reviews the change, and the UI then reflects the approved plan in a controlled classroom scenario." |
| 110–120 s | [Architecture](../screenshots/system-overview.png) and [Gantt](../screenshots/gantt-english.png) | "Behind the screens are a FastAPI backend, formal requirements, domain models, provider adapters, agent experiments, tests, editable Visio diagrams and a Microsoft Project schedule." |

## What is independently inspectable?

- [Original V3 browser testing: goal → plan → ACTIVE](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261027_GO_FROM_BLANK_DELIVERY.md). Tested in controlled Chrome contexts. The user's personal tablet verification remained pending in the handoff.
- [Original classroom demonstration quick card](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261025_CLASSROOM_QUICK_CARD.md). Includes simulated-time commands and limitations.
- [Original Suzhou four-stop simulation report](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261026_SUZHOU_FOUR_STOP_LIVE_CHAIN.md). Four stops were *virtually* completed across two simulated dates, with a real external AgentRun marked PARTIAL.
- [Real Valhalla Nara walking-route result](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R41_ROUTE_SESSION_RESULT.md). A real provider returned one 22-minute route.
- [138 candidate requirements](../requirements/README.md), [system diagrams](../teacher-materials/README.md), [Agent source](../backend/app/agent/) and [map / execution code](../frontend/src/).

## Suggested caption if this walkthrough is shown as a video

> **CityNexus Development Evidence Walkthrough (2026).** This short video presents original screenshots from different local development, browser-test and classroom-simulation sessions. The screenshots do not come from one uninterrupted live production journey. Real third-party route acquisitions and external model runs are distinguished from test fixtures, virtual time, virtual location and self-reported activity completion.

**Reviewers:** Start with the [full project portal](PROJECT_PORTAL.md), [interactive visual site](index.html) and [feature verification matrix](FEATURE_VERIFICATION_MATRIX.md). The required Part 2 PDF was submitted through Moodle separately.
