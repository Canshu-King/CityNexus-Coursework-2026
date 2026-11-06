# Feature / Evidence Matrix

**What was developed, what was observed, and what remains unverified**

A large repository does not demonstrate that every feature has been validated. This matrix is a reviewer-friendly index of concrete work and its **strongest evidence in the public record**.

## Evidence levels

- **Documented:** formal requirement, design or planned feature; not itself proof of implementation.
- **Implemented / captured:** source code and an actual rendered development screenshot exist; the screen may use test fixtures.
- **Controlled browser / local API:** a particular user flow or service result was exercised in a local, isolated or fixture-backed setup.
- **Live external provider:** an actual third-party HTTP/model request is evidenced, within its reported local setup.
- **Not verified:** a key end-to-end, field, device or deployment claim remains open.

| Product capability | Evidence level | Public evidence / qualification |
| --- | --- | --- |
| Requirement baseline, traceability and use cases | **Documented** | [138 candidate requirements](../requirements/README.md), [traceability](../requirements/traceability.yaml), [domain model](../domain-model/BASELINE.md); not formally teacher-approved |
| Traveller's natural-language V3 goal entry | **Implemented / captured** | [Real 390px Chrome screen](../screenshots/v3-first-trip-goal-mobile.png), [GoalDraftEditor source](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/GoalDraftEditor.tsx) |
| Agent conversation and tool progress visibility | **Implemented / captured** | [Real tool timeline](../screenshots/v3-agent-tool-timeline-mobile.png), [V3 source](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/WorkspaceActions.tsx). This is recorded activity, not private chain-of-thought |
| Source-aware place discovery | **Implemented / captured** | [City discovery screen](../screenshots/discovery-desktop.png), [backend discovery adapters](../backend/app/integrations/discovery/); displayed screen used SEALED REPLAY |
| V3 map with planned place markers | **Controlled browser / local API** | [Rendered mobile map](../screenshots/v3-real-map-mobile.png), [Oct 27 human-browser report](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261027_GO_FROM_BLANK_DELIVERY.md); not device-GPS arrival evidence |
| From first goal to an ACTIVE local Journey | **Controlled browser / local API** | [Oct 27 browser report](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261027_GO_FROM_BLANK_DELIVERY.md), 390/430 mobile-width cases; not full owner tablet verification |
| One walking route from a real routing provider | **Live external provider** | [R41 Valhalla result](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R41_ROUTE_SESSION_RESULT.md) returned a 22-minute grounded route |
| External model used to adjust a local itinerary | **Live external model in local experiment** | [R14 report](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R14_AUTONOMOUS_STRATEGY_RESULT.md); an opening-hours conflict initially rejected a candidate |
| User-authorized local recovery and plan switch | **Controlled local product run** | [R20 report](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R20_AUTONOMOUS_BRANCH_ARBITRATION_RESULT.md); terminal local Journey state does not mean physical trip |
| Simulated virtual time / location and presenter controls | **Controlled browser / local simulation** | [Classroom control screenshot](../screenshots/v3-classroom-simulation-control.png), [quick card](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261025_CLASSROOM_QUICK_CARD.md) |
| Single simulated two-day, four-stop run | **Controlled local simulation + external calls** | [Oct 26 Suzhou report](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261026_SUZHOU_FOUR_STOP_LIVE_CHAIN.md); external AgentRun PARTIAL, physical activities simulated |
| Member-bound group recovery and rendezvous | **Focused local / controlled browser tests** | [R62 report](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R62_GROUP_RECOVERY_FOLLOW_THROUGH_RESULT.md); arrival self-report is not physical co-location |
| Pocket Walk / two-stop private progression | **Controlled mobile-browser fixtures** | [R108 report](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R108_POCKET_WALK_GO_MODE_RESULT.md); progress status USER_REPORTED_PROGRESS |
| Actual complete multi-day physical trip | **Not verified** | Simulated time and GPS cannot prove real-world travel |
| Entirely merged, working mainline + Revolution Lab + V3 release | **Not verified** | [Separate branch map](DEVELOPMENT_STREAMS.md), [limitations](EVIDENCE_AND_LIMITATIONS.md) |
| Successfully completed professor live demo | **Not verified** | The full product was not successfully demonstrated in class |
| Public website that runs the complete AI backend | **Not verified** | The [static image-based reviewer site](index.html) is a presentation of engineering evidence, not an online connected app |
| Fresh post-redaction full-suite build / E2E result | **Not verified** | [Public release provenance](PUBLICATION_AND_PROVENANCE.md), [redaction summary](../PUBLIC_REDACTION_SUMMARY.md) |

## Suggested reviewer flow

For product experience, open the [two-minute walkthrough](DEMO_STORYBOARD.md) or the [visual review site](index.html). For research, open the [ten experiment case studies](EXPERIMENT_RESULTS.md) and their [103 original reports](RESEARCH_ARCHIVE_INDEX.md). For academic grading, open the [Visio/Project materials](../teacher-materials/README.md) and the [requirements baseline](../requirements/README.md).

**This matrix is a snapshot of reviewed evidence, not a certification of production readiness or an estimate of each team member's personal contribution.**
