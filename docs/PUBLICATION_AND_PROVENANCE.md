# Publication and provenance: five repositories, two kinds of history

**Current status, 6 November 2026: All five repositories are PUBLIC.** This document distinguishes what is in this independently curated coursework showcase from the original, separately published development repositories.

## 1. Source origin of this curated showcase

- Coursework/mainline source snapshot: [CityNexus commit 9115b652](https://github.com/Canshu-King/CityNexus/commit/9115b6521678d587c2dea1cee0b20b08413854a0), branch [coursework/part2-20261104-kickoff](https://github.com/Canshu-King/CityNexus/tree/coursework/part2-20261104-kickoff), dated **4 November**.
- Standalone experimental Revolution Lab source snapshot: [CityNexus commit a5a2f109](https://github.com/Canshu-King/CityNexus/commit/a5a2f1096e0fbd806acd8be134fe8fb6095ad1a9), branch [gpt/revolution-r33-open-world-journey-adaptation](https://github.com/Canshu-King/CityNexus/tree/gpt/revolution-r33-open-world-journey-adaptation), dated **11 October**.
- Human-first V3 and classroom evidence: [original V3 branch](https://github.com/Canshu-King/CityNexus/tree/rescue/v3-final-departure-20261022), with observed work as late as **29 October**. Six original screenshots are copied into [this portfolio](CLASSROOM_AND_V3.md) with their real source preserved.
- Selected editable Visio/Project materials and PDFs came from the November coursework tree.

The current portfolio contains source snapshots, documentation, test files, UI captures, architectural materials and a clean-root public Git history. It has **not** claimed a newly built or tested merge between the October V3, Revolution Lab and the original coursework app.

## 2. Why this portfolio's Git commits start on November 6

The portfolio was created as a **separate GitHub repository with a new root commit**, not by changing the main original repository's commit tree. Its recent history records **publication work**. It does not reproduce or backdate months of original engineering commits.

**The original repository is now public**, so the historical branches and their true original commit records are available independently:

- [CityNexus original source and branches](https://github.com/Canshu-King/CityNexus)
- [Workspace and academic handoffs](https://github.com/Canshu-King/CityNexus-Workspace)
- [Early experimental labs](https://github.com/Canshu-King/CityNexus-Lab-Archive)
- [Historical release snapshots](https://github.com/Canshu-King/CityNexus-History)

The main source's **default** branch dates from September and should not be mistaken for the October V3 or November coursework branch. See [branch navigation and exact observed heads](DEVELOPMENT_STREAMS.md).

## 3. Publication sanitization is not product testing

The independent public portfolio's initial release used Gitleaks-guided scanning in a temporary copy, redacted exact possible credential matches, and excluded one test file. [Its redaction summary](../PUBLIC_REDACTION_SUMMARY.md) records the operations. That release did **not** rerun a comprehensive frontend/backend production build after sanitization.

**The original four repositories were subsequently made public in full at the user's explicit request.** Their original Git histories were **not** automatically sanitized by the portfolio release script. Public repository visibility does not mean their credentials or historical documents have been audited. Any active tokens accidentally committed there should be revoked/rotated; removing the file from HEAD cannot erase copies already made or historical commits.

We do not publish real credential strings in this documentation.

## 4. The academic submission boundary

The formal **Part 2 PDF** was uploaded separately to Moodle, and is not assumed to exist in these public repositories. The [Software Engineering assignment brief](https://github.com/Canshu-King/CityNexus-Workspace/blob/master/_coursework/Software%20Engineering%20-%20Assignment%20Coursework%202026.pdf) in the workspace is the course's original task document, **not** the submitted student report.

The public work includes a **candidate**, not approved, requirements baseline. Historical demos, browser fixtures, simulated GPS/time, live third-party providers and external Agent runs are identified separately. Full physical-world end-to-end completion and a successful classroom presentation of the entire stack are **not** claimed.

This guide is a map of real engineering achievements, not a substitute for the specific evidence in each experiment report.
