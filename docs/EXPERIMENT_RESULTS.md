# Revolution Lab: Specific Results, Not Just Feature Names

The independent Revolution Lab was a **research and product-development track** that explored continuous, agent-driven journeys. The original engineering branch, now publicly accessible, has an extensive collection of dated result notes across experiments R14–R108 and related work. This public page extracts **specific, representative outcomes** and links to source code or tests that remain in the sanitized public snapshot.

**Evidence grades:** a result may be **live-provider/local experiment**, **controlled-browser or fixture**, **code/test delivery**, or **blocked/no material change**. The grade matters more than how many experiments had an “R” number.

## 1. A real model tried to redesign a day in Chongqing (R14)

**Problem:** The traveller asked for cultural stops, little walking and a compact schedule. An Agent using the configured external reasoning runtime explored real OSM-sourced venues and evaluated a candidate itinerary.

**Observed result:** The first proposal was **rejected** because one museum's opening interval conflicted with the visit. The report documents a strategy change and a revised local product Journey, with estimated-fact boundaries still declared. This is evidence of model-and-tool orchestration under a calendar gate, **not proof of a physically completed city visit**.

**Related code:** [Agent runtime](../experiments/revolution/backend/app/agent/runtime.py) · [planning](../experiments/revolution/backend/app/planning/) · [source authority test](../experiments/revolution/backend/tests/test_r15_source_authority.py).

## 2. When weather evidence did *not* justify a replanning event (R17 / R34)

**Problem:** An always-on Guardian should use relevant live context, but it must not invent a disruption just to demonstrate “AI autonomy.”

**Observed result:** The R17 Nara runs yielded stable weather observations and did **not** reach predictive-prevention closure. A proposed timing change was rejected under route/travel-gap constraints. R34 refreshed exact place evidence for the active plan and returned **NO_MATERIAL_CHANGE**, with zero Agent runs and zero plan mutations.

**Related code:** [Guardian](../experiments/revolution/backend/app/agent/guardian.py) · [Open-Meteo adapter](../experiments/revolution/backend/app/integrations/context/open_meteo.py) · [live context test](../experiments/revolution/backend/tests/test_live_context_watch.py).

## 3. Choosing an alternate route after a disruption (R20)

**Problem:** A user-reported mobility delay required an Agent to inspect the current journey and choose from compatible future plans.

**Observed result:** In an **isolated laboratory product run**, the configured Agent selected a standby plan; the deterministic gateway required user approval; after approval the new plan was activated and executed through the local HTTP activity state machine until the Journey reached **COMPLETED**. The report also records a mismatched trigger that the gateway refused.

**Related code:** [Agent approval](../experiments/revolution/backend/app/agent/approval.py) · [planning and replanning](../experiments/revolution/backend/app/replanning/).

## 4. Real walking-route acquisition (R41)

**Problem:** A trip needs actual mobility evidence, not a model-generated ETA.

**Observed result:** A fresh local Journey executed a single on-demand **Valhalla** walking-route operation between two Nara locations. A real provider returned a **22-minute** route and a persisted source-bound mobility fact. The route session finished **ROUTE_SESSION_READY**. It did not mutate the active plan or constitute physical location proof.

**Related code:** [Valhalla integration](../experiments/revolution/backend/app/integrations/mobility/valhalla_http.py) · [mobility runtime](../experiments/revolution/backend/app/integrations/mobility/runtime.py).

## 5. One Operator entrypoint for recovery (R43)

**Problem:** A user should not have to manually connect separate Guardian, Agent, proposal and approval consoles.

**What was delivered:** An Operator API and user-facing timeline were implemented with start/stop and an approval view. A local fixture generated a contextual event. However, the external model was unavailable in that run, so the product correctly reported **OPERATOR_EXTERNAL_AGENT_BLOCKED** rather than fabricating an Agent proposal. Focused backend tests were reported passing; the targeted browser execution was blocked by a local Python launcher mismatch.

**Related code:** [operator test](../experiments/revolution/backend/tests/test_r43_operator_loop.py) · [Agent runtime](../experiments/revolution/backend/app/agent/runtime.py).

## 6. Coordinated recovery for a group (R62 / R77)

**Problem:** If a group separates, the system needs member-bound progress and a sensible reunification boundary.

**What was delivered:** The R62 code/tests persist authenticated arrival reports independently for each in-force member and close the recovery once the required set reports completion. The evidence does **not** prove physical co-location by GPS. R77 introduced a member-consented foreground location sample and bounded routing to the planned group stop, with stale-result rejection and a controlled multi-account browser test.

**Related code:** [R62 follow-through](../experiments/revolution/backend/tests/test_r62_group_recovery_followthrough.py) · [member join/catch-up tests](../experiments/revolution/backend/tests/test_r76_journey_membership_invitation.py).

## 7. A private side quest from a shared journey (R83)

**Problem:** A member may want a brief, personal diversion without rewriting the group's itinerary.

**Observed result:** A **controlled test-provider** browser scenario covered venue discovery after consent, personal selection, destination-only navigation, return navigation and a *self-reported* start of return. The group's PlanVersion remained unchanged and other members saw only coarse progress. The report explicitly labelled the overall outcome **PARTIAL** because no public Overpass call or physical excursion was proven.

**Related code:** [member side-quest tests](../experiments/revolution/backend/tests/test_r64_member_side_quest.py) · [shared-journey UI](../frontend/src/features/shared/SharedJourneyPanel.tsx).

## 8. Searching a city with honest provider attribution (R95)

**Problem:** A city search that uses a test endpoint should not be presented as evidence from the public internet.

**Observed result:** A mobile Chromium acceptance flow covered an explicit city search action, user consent, caching and precise provider-grade propagation into discovery and saved places. The run used local fixtures and **TEST_PROVIDER** labels, while real public geocoder/Overpass calls remained **zero**.

**Related code:** [provider provenance test](../experiments/revolution/backend/tests/test_r95_city_jump_and_provenance.py) · [discovery adapters](../experiments/revolution/backend/app/integrations/discovery/).

## 9. Sharing two public places without leaking private journey data (R103)

**Problem:** A traveller wants to share two candidate stops with a friend, but not private itinerary details, bearer tokens or location history.

**Observed result:** Two signed browser contexts used controlled city-search/Overpass fixtures. The sender shared **two public OSM identities**; the recipient consented to verification and saved a separate private shortlist and two-stop walk. The **2 passing** controlled Chromium checks did not constitute a live provider or real-world route test.

**Related code:** [R102 shared outing tests](../experiments/revolution/backend/tests/test_r102_shared_city_outing.py) · [R100 two-stop walk tests](../experiments/revolution/backend/tests/test_r100_pocket_walk.py).

## 10. Two-stop Pocket Walk GO MODE (R108)

**Problem:** An ordinary traveller needs a simple, resumable “first stop → second stop” experience, without the system pretending to observe physical arrival.

**Observed result:** The experimental frontend/backend expose a private, revision-bound progression with start, pause, resume, undo and two self-reported stops. The **2 passing** mobile Chromium checks used **390×844**, two signed accounts and controlled provider fixtures, with AI disabled. Status is explicitly **USER_REPORTED_PROGRESS**, not GPS-proven arrival.

**Related code:** [R108 state-machine test](../experiments/revolution/backend/tests/test_r108_pocket_walk_go_mode.py) · [Pocket Walk service code](../experiments/revolution/backend/app/).

---

## What these results establish

They show **different engineering problems addressed over successive iterations**: real-model tool use, real source acquisition, guarded changes, consent, state persistence, group isolation and mobile acceptance. They do **not** mean that every result was successfully integrated, that every original lab result is replicated in this curated sub-repository, or that the entire sanitized public export has passed a new comprehensive test suite.

A source-derived [index of 103 archived result-note filenames](RESEARCH_ARCHIVE_INDEX.md) now shows the broader history and directly links available public tests. The original source branch is now public, so reviewers can inspect the [original reports through the full linked index](RESEARCH_ARCHIVE_INDEX.md). They may contain detailed runtime receipts; they are not copied wholesale into this curated document. This page is a reviewed explanatory summary; the implementation and test sources linked above are the public inspection points.

For the main coursework engineering files, return to the [teacher walkthrough](START_HERE_FOR_REVIEWER.md) or [feature atlas](FEATURE_ATLAS.md).
