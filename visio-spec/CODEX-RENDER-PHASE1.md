# Codex Render Phase 1: Style Guide + Part 1 UML

Execute only after `use-cases/` and `visio-spec/` have passed local validation and been committed.

Project root: `D:\moodle\CityNexus`

Inputs:

- `visio-spec\visio-model-spec.yaml`
- `visio-spec\style-tokens.yaml`
- `visio-spec\qa-rules.yaml`
- `visio-spec\pages\00_style_guide.yaml`
- `visio-spec\pages\p1_uc_overview.yaml`
- `visio-spec\pages\p1_domain_overview.yaml`

Use the installed Visio MCP and Visio Automation Skill. Use native Microsoft Visio UML masters and native UML connectors. Do not substitute ordinary ellipses or rectangles when a native UML master is available.

## Output

Create:

`D:\moodle\CityNexus\visio\CityNexus_Visio_Model_Baseline_v1.0.vsdx`

Create pages in this exact order:

1. `00_Style_Guide`
2. `P1_UC_Overview`
3. `P1_Domain_Overview`

Do not create Part 2 pages yet.

Export:

- `visio\exports\part1\CNX_P1_UC_Overview_v1.0.svg`
- `visio\exports\part1\CNX_P1_Domain_Overview_v1.0.svg`

PNG fallback may also be exported, but SVG is authoritative for PowerPoint embedding.

## Rendering rules

1. Read and render `00_Style_Guide` first.
2. Use centimetres from the YAML specification.
3. YAML coordinates use a logical top-left origin. If Visio COM/API uses a bottom-left origin, convert with `VisioY = PageHeight - LogicalY`.
4. Do not change logical grouping, actor side, use-case ordering, or class ordering.
5. Minor alignment adjustments are allowed only when needed to avoid collision and must remain within 2 mm unless explicitly reported.
6. All connectors must be glued to actual source and target shapes.
7. Use right-angle routing by default.
8. Association connectors have no arrowhead.
9. `<<extend>>` arrows point toward the base use case.
10. Composition diamonds appear on the owner/source side.
11. Dependency connectors have no multiplicity.
12. Class operations compartments remain hidden.
13. Multiplicity labels must sit near association endpoints, not in connector midpoints.
14. `Journey` displays `<<aggregate root>>` where specified.
15. `JourneyTwin` displays `<<projection>>`.
16. Do not draw obsolete `TravelerPreference`, `JourneyPreference`, `AIInsight`, or generic `Transaction` classes.
17. Do not silently alter Domain v0.4.2 multiplicities. In particular, Journey -> PlanVersion and PlanVersion -> Activity are `0..*` on the target side.

## Build gates

Treat any of the following as a failed render:

- semantic shape overlap
- connector passes through unrelated semantic shape
- connector is not glued at either endpoint
- external actor is inside system boundary
- use case is outside system boundary
- include/extend direction is wrong
- composition diamond is on the wrong side
- dependency has multiplicity
- more than 2 connector crossings after one rerouting attempt
- a requested native UML master was replaced with a generic basic shape without explicit failure report

## MCP verification

After rendering, run available MCP inspection tools equivalent to:

- list pages
- list shapes for each rendered page
- inspect connections/glue
- page summary
- export page as image/SVG

Verify expected semantic shape IDs/names against the YAML source.

## QA report

Create:

`D:\moodle\CityNexus\visio\qa\phase1-render-report.json`

Include at least:

- Visio template/stencil used
- native masters resolved for Actor, Use Case, System Boundary, Class, Association, Dependency, Extend
- rendered page count and page names
- semantic shape count per page
- connector count per page
- unglued connector count
- overlap count
- connector-through-shape count
- connector crossing count per page
- exported SVG paths and existence
- warnings/manual review items

Do not render Part 2 after Phase 1. Stop for visual and semantic review.
