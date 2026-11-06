# Development Timeline | September–November 2026

**This work spans three calendar months of dated engineering artifacts.** This is an evidence-based overview of the project's evolution, not a reconstruction of every coding session, an estimate of individual working hours, or a claim that every planned feature is complete.

> **Why does GitHub show a recent, short commit history?** This public repository was intentionally republished as a clean, history-free snapshot on **6 November 2026**, after sensitive content was removed. Months of activity remain in private development branches and working records. We deliberately did **not** copy those private commit histories, credentials or logs into a public repository. The public commit chart therefore measures **publication**, not the project's full development period.

## Milestones with inspectable evidence

| Period | Engineering work | Evidence in this public repository |
| --- | --- | --- |
| **September 2026** | Structured the project domain and planning model. A candidate **18-entity domain model** was frozen on **5 September 2026**. The requirements and use-case work also produced a 138-item candidate requirements baseline, acceptance criteria, and traceability mappings. | [Domain-model baseline with freeze date](../domain-model/BASELINE.md) · [Requirements baseline](../requirements/BASELINE.md) · [Use-case baseline](../use-cases/BASELINE.md) |
| **September–October** | Built out two substantive implementation layers: a React/TypeScript traveller interface and a FastAPI/Python backend, with map, planning, integration, agent, and testing modules. These are *source snapshots*, not a claim that every UI path is functional. | [Frontend source](../frontend/src) · [Backend source](../backend/app) · [Frontend browser tests](../frontend/e2e) · [Backend tests](../backend/tests) |
| **October 2026** | Explored the separate **Revolution Lab** track: grounded city discovery, consent-gated sharing, short two-place Pocket Walks, member-private GO MODE, and agent-supported recovery. The **12 October** R108 report described a controlled browser test; the published source retains its explicit self-reported-progress semantics. | [Lab research overview](REVOLUTION_LAB.md) · [R108 backend test source](../experiments/revolution/backend/tests/test_r108_pocket_walk_go_mode.py) · [Experimental implementation](../experiments/revolution/) |
| **October 2026** | Prepared classroom-oriented systems engineering materials: editable Visio architecture and agent-flow diagrams, Microsoft Project planning, Gantt and WBS previews. These communicate the model and work plan, not that every milestone was achieved. | [System architecture](../teacher-materials/system-overview.pdf) · [Agent/user flow](../teacher-materials/agent-user-flow.pdf) · [Gantt](../teacher-materials/gantt.pdf) · [WBS](../teacher-materials/wbs.pdf) · [Editable Project plan](../teacher-materials/project-overview.mpp) |
| **4 November 2026** | Curated the coursework/mainline implementation snapshot used in this release while the separate experimental line retained its own code provenance. | [Source branch and commit provenance](PUBLICATION_AND_PROVENANCE.md) |
| **6 November 2026** | Submitted the coursework PDF through Moodle separately and packaged a public-facing, sanitized source-and-evidence showcase with an English reviewer guide. | [Repository overview](../README.md) · [Redaction summary](../PUBLIC_REDACTION_SUMMARY.md) · [Evidence limitations](EVIDENCE_AND_LIMITATIONS.md) |

## How to evaluate the work fairly

The evidence demonstrates **several distinct engineering stages across September, October and November**, including requirements analysis, domain modeling, UI/API implementation, experimental agent work, automated test development and project-management artifacts. It does not establish uninterrupted daily activity, individual contribution totals, or production readiness.

The 138 requirements are a **candidate**, not teacher-approved. Mainline and Revolution Lab are **not represented as a validated combined build**. Controlled/simulated provider results, automated tests and self-reported progress cannot be interpreted as proof of actual physical travel or a successful live classroom demo.

Because public-release sanitization changed some source files, this copy has **not** undergone fresh build or end-to-end validation after publication. For a short review, use the [five-minute project tour](../README.md) and the [editable teacher materials](../teacher-materials/README.md).

**Publication methodology:** Dates are drawn from dated baselines and original development records; only the deliberately selected safe artifacts are included here. No historical Git commits have been fabricated or backdated.
