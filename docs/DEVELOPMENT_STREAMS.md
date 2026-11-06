# Engineering History: Repositories, Branches and Parallel Teams

**GitHub inventory verified 6 November 2026.**

CityNexus does **not** have one chronological "main" branch containing every experiment. The project progressed through parallel lanes. Reading only the default branch of the source repository is an inaccurate way to judge its development.

## Where to find the substantial work

| Purpose | Public source branch | Last observed head (UTC) | What is evidenced |
| --- | --- | --- | --- |
| **Original source default** | [eac/v19.2.2-realphone-codegate](https://github.com/Canshu-King/CityNexus/tree/eac/v19.2.2-realphone-codegate) | Sep 26, 2026 · 1da4d3d2 | Early mainline snapshot; **not** the latest user-facing product branch |
| **Codex Revolution Lab** | [gpt/revolution-r33-open-world-journey-adaptation](https://github.com/Canshu-King/CityNexus/tree/gpt/revolution-r33-open-world-journey-adaptation) | Oct 11 · a5a2f109 | Agent research, R14–R108 result records, R109 design/taskbooks, standalone tests, group and city-walk capabilities |
| **Qoder/Commander coordination** | [commander/control](https://github.com/Canshu-King/CityNexus/tree/commander/control) | Oct 24 · 24748119 | Coordination, taskbooks, detailed test/evidence ledger; not itself a runnable production frontend |
| **Human-first Revolution V3 product** | [rescue/v3-final-departure-20261022](https://github.com/Canshu-King/CityNexus/tree/rescue/v3-final-departure-20261022) | **Oct 29 · d7615273** | Newer human-first React interface, Agent timeline, city journeys, demo simulation, browser studies and many actual visual receipts |
| **Product fusion / Qoder import** | [fusion/qoder-import-20261014](https://github.com/Canshu-King/CityNexus/tree/fusion/qoder-import-20261014) | Oct 25 · 441c2197 | Work to reconcile independent product tracks; do not assume every branch was cleanly merged |
| **Codex simulation and privacy support** | [codex/sandbox-execution-security-20261025](https://github.com/Canshu-King/CityNexus/tree/codex/sandbox-execution-security-20261025) | Oct 26 · 22f1fe17 | Private Journey read authorization, simulation support and scoped accessibility limits; independently developed |
| **Codex developer companion** | [codex/developer-autopilot-r1-20261025](https://github.com/Canshu-King/CityNexus/tree/codex/developer-autopilot-r1-20261025) | Oct 25 · d7671c71 | Experimental DSH-style developer/agent tooling and execution boundaries, **separate from the travel app** |
| **Software Engineering coursework** | [coursework/part2-20261104-kickoff](https://github.com/Canshu-King/CityNexus/tree/coursework/part2-20261104-kickoff) | **Nov 4 · 9115b652** | Latest dated course plan and requirements deliverable work, source base for this curated public portfolio |

Dates above are **last observed commit timestamps** on selected branch heads, not claims that every line of a branch originated that day. They are not a "latest all-project release" declaration. A newer document branch may contain older product code, and an independently successful test does not merge two architectures.

## How the project developed

### Early September: formal foundations and first integrated prototypes

The early CityNexus work created structured requirements, domain rules, a data model, use cases, planning constraints, FastAPI APIs, initial React views, provider integrations and experiments with Visio and project planning. Historical evidence survives in the [original repository](https://github.com/Canshu-King/CityNexus), the [September workspace handoffs](https://github.com/Canshu-King/CityNexus-Workspace/tree/master/_handoff), and the [release archives](https://github.com/Canshu-King/CityNexus-History).

### September–October: full-stack traveller product and rigorous UI work

The early product expanded into place discovery, map displays, itinerary, budget and lodging, Agent activity, Traveller Twin, Guardian/recovery, bilingual UI and browser testing. The [October coursework source snapshot](https://github.com/Canshu-King/CityNexus/tree/coursework/part2-20261104-kickoff) includes real rendered UI screenshots and a large set of frontend and backend test sources. These validate particular test conditions rather than guarantee every screen works on a random user's device.

### October: Revolution Lab and model-driven Agent research (Codex)

Starting with autonomous planning and grounded model-tool interaction, the Revolution branch explored proactive checks, route provider facts, human-authorized plan replacement, group mediation, companion recovery, private side quests, verified place sharing and Pocket Walk GO MODE. There are [103 result-note filenames](RESEARCH_ARCHIVE_INDEX.md) on the original branch. The research page distinguishes outcomes such as **COMPLETED**, **NO_MATERIAL_CHANGE**, **PARTIAL** and **BLOCKED**, which are not interchangeable.

Start with [R14 real model strategy](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R14_AUTONOMOUS_STRATEGY_RESULT.md), [R20 governed standby activation](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R20_AUTONOMOUS_BRANCH_ARBITRATION_RESULT.md), [R41 Valhalla walking route](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R41_ROUTE_SESSION_RESULT.md), [R62 group recovery](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R62_GROUP_RECOVERY_FOLLOW_THROUGH_RESULT.md), and [R108 Pocket Walk](https://github.com/Canshu-King/CityNexus/blob/gpt/revolution-r33-open-world-journey-adaptation/REVOLUTION_R108_POCKET_WALK_GO_MODE_RESULT.md).

### October 15–29: turn research into a first-user journey (Qoder + Codex)

The user repeatedly found it difficult to find the Agent or proceed through the app, and a formal class presentation could not span real travel days. The team therefore worked on **one obvious entry**, a human-readable Agent conversation and tool timeline, real-city location selection, signed session boundaries, recovery paths and a **Presenter classroom** with controllable simulated time and position. These are documented under the V3 branch and its [Oct 27 from-blank browser report](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261027_GO_FROM_BLANK_DELIVERY.md).

The [Oct 26 Suzhou local experiment](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261026_SUZHOU_FOUR_STOP_LIVE_CHAIN.md) provides a specific controlled outcome: four distinct stops, two simulated dates, a genuine external Guardian/model request with a **PARTIAL** Agent run, accepted change and persisted terminal simulation. It does **not** prove an actual four-stop physical trip.

The [Oct 29 handoff](https://github.com/Canshu-King/CityNexus/blob/rescue/v3-final-departure-20261022/trial/results/20261029_HANDOFF_GO_FROM_BLANK_r185.md) also records remaining defects and **USER_VERIFIED=PENDING** rather than quietly rewriting those as success.

### November: formal submission and open project portfolio

The source coursework branch on **Nov 4** contains the 20-page Part 2 redesign work and original course artifacts. The formal submitted PDF went to **Moodle**. On **Nov 6**, a new, carefully curated GitHub portfolio was published with screenshots, selected code and engineering evidence; the four older source/workspace/archive repositories were subsequently made public, making their actual histories accessible separately.

## The historical auxiliary repositories

| Public repository | Representative history / source entry |
| --- | --- |
| [CityNexus-Workspace](https://github.com/Canshu-King/CityNexus-Workspace) | [Early academic handoff](https://github.com/Canshu-King/CityNexus-Workspace/tree/master/_handoff), [coursework reference](https://github.com/Canshu-King/CityNexus-Workspace/tree/master/_coursework), [project introduction and handoff notes](https://github.com/Canshu-King/CityNexus-Workspace/tree/master/_docs) |
| [CityNexus-Lab-Archive](https://github.com/Canshu-King/CityNexus-Lab-Archive) | [WorkBuddy / DSH experience lab](https://github.com/Canshu-King/CityNexus-Lab-Archive/tree/master/CityNexus-Tools), [Qoder recovery archive](https://github.com/Canshu-King/CityNexus-Lab-Archive/tree/master/qoder-recovery-20260919) |
| [CityNexus-History](https://github.com/Canshu-King/CityNexus-History) | [Historical V14/V15 release-staging snapshots](https://github.com/Canshu-King/CityNexus-History/tree/master/_v15_release_staging_20260914) |

**The significance of the public history:** It shows the *actual project branches, dated work and artifacts*. But the public portfolio repository itself began on Nov 6 as a one-off clean publication; its commits should not be used to estimate how many months the team worked.

## How to review fairly

1. **Product experience:** [V3 screenshots and classroom details](CLASSROOM_AND_V3.md).
2. **Engineering breadth:** [feature/source map](FEATURE_ATLAS.md).
3. **Research outcomes:** [ten experiment case studies](EXPERIMENT_RESULTS.md) and the original result notes above.
4. **Formal course criteria:** [requirements](../requirements/README.md), [Gantt](../teacher-materials/gantt.pdf) and [native diagrams](../teacher-materials/README.md).

No fresh multi-branch integration or on-device physical trial has been claimed. The responsible interpretation is **substantial, parallel, dated development, with variable levels of verification**.
