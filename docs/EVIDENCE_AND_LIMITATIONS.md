# Evidence and limitations

This repository distinguishes intended behavior from code, tests and proven real-world operation.

## Evidence available
- Source code for React/TypeScript frontend, Python/FastAPI backend and agent-tool integration.
- 138 machine-readable formal requirements with traceability, all under a **candidate** baseline.
- Test source code in `frontend/e2e/` and `backend/tests/`.
- Editable and previewable engineering diagrams and project planning artifacts.
- Experimental R103 and R108 results documented in Revolution Lab; those results include controlled browser scenarios and test-provider fixtures.

## Not established by this publication
- A complete successful classroom demonstration.
- Production deployment, robust live-service availability or a fresh end-to-end verification of the exported snapshot.
- Formal teacher/customer sign-off on the original requirements baseline.
- GPS-proven physical arrival, booking confirmation or real payments simply from test fixtures or traveller self-reports.
- Successful integration of every experimental Revolution capability into the coursework/mainline app.

## Review method
Read the diagram first, the baseline next, then inspect source files and tests. Treat controlled tests as evidence only for their declared fixtures, identities and assertions. In particular, Revolution R108 labels journey progression **USER_REPORTED_PROGRESS**, not physical-arrival evidence.

The official assessed Part 2 PDF was submitted on Moodle separately.
