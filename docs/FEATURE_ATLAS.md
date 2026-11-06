# Feature Atlas: What Is Actually in the Code?

Rather than an abstract list of proposed features, this catalogue points to **specific files already present in the published repository**. Their existence is evidence of implemented modules, but is not by itself proof that every integration works in a live deployment.

## Important: this atlas is the curated source snapshot, not the newest V3 branch

The internal links below describe the **November coursework source snapshot** included in this portfolio. The original source repository also contains a **later Qoder-led human-first V3 product line** at [rescue/v3-final-departure-20261022](https://github.com/Canshu-King/CityNexus/tree/rescue/v3-final-departure-20261022), with actual browser captures and product work up to **29 October**.

| V3 product capability | Live source from the original public repository |
| --- | --- |
| Natural-language entry and goal editing | [GoalDraftEditor.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/GoalDraftEditor.tsx) |
| Place discovery and city selection | [CityPicker.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/CityPicker.tsx) · [NamedPlaceSearch.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/NamedPlaceSearch.tsx) |
| Conversational Agent, tool timeline and clear availability | [MobileHermesRun.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/MobileHermesRun.tsx) · [WorkspaceActions.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/WorkspaceActions.tsx) |
| Plan workbench, candidate selection and departure | [MobilePlanWorkbench.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/MobilePlanWorkbench.tsx) · [DraftDepartureCard.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/DraftDepartureCard.tsx) |
| Mobile map and persisted place identities | [WorkspaceMap.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/WorkspaceMap.tsx) |
| Guardian decision and simulation | [GuardianRealityPanel.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/GuardianRealityPanel.tsx) · [ClassroomPresenterPanel.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/ClassroomPresenterPanel.tsx) |
| Simulated time and place | [ClassroomClockStrip.tsx](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/ClassroomClockStrip.tsx) · [simulationClient.ts](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/frontend/src/trial/mobile/simulationClient.ts) |

**[See authentic V3 screenshots and the classroom walk-through](CLASSROOM_AND_V3.md)**. A source module and a documented controlled test are valuable evidence, but do not guarantee complete live provider configuration, a physically verified trip, or a successful final classroom demo.

## 1. Traveller experience

| Feature / engineering scope | Inspect the implementation |
| --- | --- |
| App shell, route-driven navigation and responsive journey views | [V8Experience](../frontend/src/app/V8Experience.tsx), [routes](../frontend/src/routes/) |
| Journey setup and input collection | [JourneyTrialSetup](../frontend/src/features/setup/JourneyTrialSetup.tsx) |
| Personal preferences and traveller model | [TwinPanel](../frontend/src/features/traveler-twin/TwinPanel.tsx), [Traveler Twin](../backend/app/execution/traveler_twin.py) |
| Map view and map controls | [JourneyMap](../frontend/src/components/JourneyMap.tsx), [MapPanel](../frontend/src/components/v8/MapPanel.tsx) |
| Itinerary and activity details | [TripItinerary](../frontend/src/features/trip/TripItinerary.tsx), [ActivityDetail](../frontend/src/features/trip/ActivityDetail.tsx) |
| Cost and spending presentation | [SpendPanel](../frontend/src/features/trip/SpendPanel.tsx) |
| Hotel/stay discovery | [HotelsPanel](../frontend/src/features/stay/HotelsPanel.tsx), [StayExplorer](../frontend/src/features/stay/StayExplorer.tsx) |
| User's pre-trip checklist | [PretripChecklistPanel](../frontend/src/features/checklist/PretripChecklistPanel.tsx) |
| Offline journey records | [offlineJourney](../frontend/src/lib/offlineJourney.ts) |
| Bilingual UI infrastructure | [LocaleContext](../frontend/src/i18n/LocaleContext.tsx) |

## 2. Discovery, planning and mobility

| Feature / engineering scope | Inspect the implementation |
| --- | --- |
| Real-city venue discovery and candidate evaluation | [RealCityDiscoveryPanel](../frontend/src/features/discovery/RealCityDiscoveryPanel.tsx) |
| Provenance of place information | [PlaceProvenance](../frontend/src/components/PlaceProvenance.tsx) |
| Planning with AI assistance | [PlanWithAiSheet](../frontend/src/features/planning/PlanWithAiSheet.tsx) |
| Ranking and comparison of plans | [PlanRankingPanel](../frontend/src/features/planning/PlanRankingPanel.tsx), [PlanComparison](../frontend/src/features/planning/PlanComparison.tsx) |
| Planning solvers and constraints | [planning package](../backend/app/planning/), [OR-Tools adapter](../backend/app/planning/ortools_adapter.py) |
| Route estimation and mobility providers | [mobility integration](../backend/app/integrations/mobility/), [Valhalla adapter](../backend/app/integrations/mobility/valhalla_http.py) |
| AMap integration for China-oriented scenarios | [AMap adapter](../backend/app/integrations/china/amap.py) |
| Weather and contextual signals | [Open-Meteo adapter](../backend/app/integrations/context/open_meteo.py) |
| Geographic place discovery | [discovery package](../backend/app/integrations/discovery/) |
| Provenance-aware plan editing | [V12TripStudioPanel](../frontend/src/features/studio/V12TripStudioPanel.tsx) |

## 3. Agent reasoning and user control

| Feature / engineering scope | Inspect the implementation |
| --- | --- |
| User/Agent conversation and intention routing | [AgentComposer](../frontend/src/features/agent/AgentComposer.tsx) |
| Visible run and tool progress | [AgentRunRail](../frontend/src/features/agent/AgentRunRail.tsx), [CopilotDock](../frontend/src/features/agent/CopilotDock.tsx) |
| Runtime identity and autonomy policy view | [ControlTowerPanel](../frontend/src/features/intelligence/ControlTowerPanel.tsx) |
| Agent mission and runtime | [mission](../backend/app/agent/mission.py), [runtime](../backend/app/agent/runtime.py) |
| Authorization and delegation | [approval](../backend/app/agent/approval.py), [delegation policy](../backend/app/agent/delegation_policy.py) |
| Guardian signals and autonomous monitoring design | [guardian](../backend/app/agent/guardian.py), [signal pipeline](../backend/app/agent/signal_pipeline.py) |
| Agent tool registration | [tool catalog](../backend/app/agent/tool_catalog.py), [Agent MCP](../agent-mcp/) |
| Model/provider abstraction | [AI integration](../backend/app/integrations/ai/), [Hermes client](../backend/app/integrations/hermes/native_client.py) |

The interface recognizes intent categories including explain plan, what-if, discover, preference change, report disruption, request replan, verify fact and personal assistance. The exact behavior depends on runtime configuration and available providers.

## 4. Journey execution and recovery

| Feature / engineering scope | Inspect the implementation |
| --- | --- |
| Live journey progression | [V10LiveExecutionPanel](../frontend/src/features/execution/V10LiveExecutionPanel.tsx) |
| Server-authoritative journey status | [V12AuthoritativeJourneyPanel](../frontend/src/features/execution/V12AuthoritativeJourneyPanel.tsx) |
| Explicit recovery screens | [RecoveryRequiredPanel](../frontend/src/features/recovery/RecoveryRequiredPanel.tsx) |
| Live recovery interaction | [LiveRecovery](../frontend/src/features/execution/LiveRecovery.tsx) |
| Preventive and provenance-sensitive recovery views | [V14PreventivePanel](../frontend/src/features/recovery/V14PreventivePanel.tsx), [V14RecoveryTruthPanel](../frontend/src/features/recovery/V14RecoveryTruthPanel.tsx) |
| Replanning and protected commitments | [replanning package](../backend/app/replanning/), [protected commitments](../backend/app/replanning/protected_commitments.py) |
| Plan history and editing | [PlanVersionHistoryPanel](../frontend/src/features/studio/PlanVersionHistoryPanel.tsx), [SortableActivityList](../frontend/src/features/studio/SortableActivityList.tsx) |
| Explicit simulation stages | [SimulationPanel](../frontend/src/features/simulation/SimulationPanel.tsx) |
| Recovery backend automated tests | [adaptive recovery tests](../backend/tests/test_v14_grounded_adaptive_recovery.py), [journey state-machine test](../backend/tests/stateful/test_journey_truth_state_machine.py) |

## 5. Shared travel and personal utility

| Feature / engineering scope | Inspect the implementation |
| --- | --- |
| Shared journey screens | [SharedJourneyPanel](../frontend/src/features/shared/SharedJourneyPanel.tsx) |
| Shared trip poster / export | [SharedPosterScreen](../frontend/src/features/poster/SharedPosterScreen.tsx), [TripPosterPanel](../frontend/src/features/poster/TripPosterPanel.tsx) |
| Booking confirmation and imported material | [BookingArtifactScanner](../frontend/src/features/inbox/BookingArtifactScanner.tsx), [JourneyInboxWorkbench](../frontend/src/features/inbox/JourneyInboxWorkbench.tsx) |
| Travel collection / saved places | [CollectionPanel](../frontend/src/features/collections/CollectionPanel.tsx) |
| Journey check-ins | [FootprintPanel](../frontend/src/features/checkin/FootprintPanel.tsx) |
| Push capability and notices | [PushCapabilityCard](../frontend/src/features/notifications/PushCapabilityCard.tsx) |
| User-controlled deletion/archiving | [JourneyTrashPanel](../frontend/src/features/trash/JourneyTrashPanel.tsx), [JourneyAbortPanel](../frontend/src/features/abort/JourneyAbortPanel.tsx) |
| Experimental shared group recovery | [R62 test](../experiments/revolution/backend/tests/test_r62_group_recovery_followthrough.py) |
| Experimental companion navigation and member boundaries | [R76/R77 tests](../experiments/revolution/backend/tests/test_r76_journey_membership_invitation.py) |
| Experimental private two-stop walk | [R108 test](../experiments/revolution/backend/tests/test_r108_pocket_walk_go_mode.py) |

## 6. The engineering underneath

| Area | Read the artifact |
| --- | --- |
| Formal requirements: **138 candidate items** | [requirements.yaml](../requirements/requirements.yaml) |
| Requirements → use cases / domain traceability | [traceability.yaml](../requirements/traceability.yaml) |
| Domain model, **18 frozen core entities** | [domain model](../domain-model/), [baseline metadata](../domain-model/BASELINE.md) |
| Use-case model | [Use cases](../use-cases/), [use-case PDF](../teacher-materials/use-cases.pdf) |
| API contracts | [OpenAPI spec](../contracts/openapi.yaml) |
| Backend tests | [backend/tests](../backend/tests/) |
| Frontend browser scenarios | [frontend/e2e](../frontend/e2e/) |
| Experimental tests | [experiments/revolution/backend/tests](../experiments/revolution/backend/tests/) |
| Visio diagrams | [teacher materials](../teacher-materials/README.md) |
| Microsoft Project schedule / Gantt / WBS | [project files](../teacher-materials/README.md) |

## What this catalogue does *not* mean

It is **not a declaration that each row is production-ready**. The published repository is a sanitized source snapshot, and some components require configured services or are experimental. The [research cases](EXPERIMENT_RESULTS.md) contain concrete examples of what was tested and what was not. 

The actual contribution here is breadth of engineering, persistence and domain modeling, multi-provider coordination, agent boundary design, mobile UI work and explicit testing, not a claim that the professor can launch the entire project by clicking a single button.
