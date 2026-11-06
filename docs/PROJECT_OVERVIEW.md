# Architecture and implementation overview

## Project purpose
CityNexus is a research-led smart city travel assistant designed to help travellers discover places, plan journeys, review agent suggestions, and adapt to changes without silently overriding user commitments.

## Interaction flow
```text
Traveller / mobile browser
   -> React UI (map, discovery, journey, agent controls)
   -> FastAPI backend (operations and persisted journey facts)
   -> planning, contextual data and AI/tool adapters
   -> proposal / authorization / bounded change
   -> updated journey and visible result
```

## Source map
- `frontend/src/`: React, TypeScript, responsive interface, map and agent interaction.
- `backend/app/`: Python/FastAPI services, persisted domain and integration boundaries.
- `agent-mcp/`: separate agent tooling integration.
- `contracts/`: API and tool interface descriptions.
- `requirements/`: the **candidate** requirements baseline and traceability.
- `domain-model/`, `use-cases/`, `visio-spec/`: formal modeling artifacts.
- `teacher-materials/`: editable Visio/Project evidence and PDF previews.
- `experiments/revolution/`: a distinct experimental code snapshot, **not merged** into mainline.

## Engineering idea
The AI agent proposes and reasons; the application applies source checks and authorization boundaries for consequential changes. A successful unit or browser test does not establish that all providers work or that a complete physical trip has been verified.

See [evidence and limitations](EVIDENCE_AND_LIMITATIONS.md) before evaluating completeness.
