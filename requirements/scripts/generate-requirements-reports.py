#!/usr/bin/env python3
"""Generate deterministic coverage and domain-mapping reports for CityNexus requirements."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REQ_DIR = ROOT / "requirements"
REQ_FILE = REQ_DIR / "requirements.yaml"
TRACE_FILE = REQ_DIR / "traceability.yaml"
DOMAIN_FILE = ROOT / "domain-model" / "citynexus-core-domain-model-v0.4.2.yaml"
QA_DIR = REQ_DIR / "qa"

SOURCE_CODES = ["CW", "UJ", "DM", "OPT", "AI", "SEC", "ARCH", "GH-PR", "GH-TS", "GH-OPT"]
PRIORITIES = ["M", "S", "C", "W"]

SECTIONS = [
    "business_goals",
    "functional_requirements",
    "business_rules",
    "non_functional_requirements",
    "security_requirements",
    "integration_requirements",
]


def load(path: Path):
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_json(path: Path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    req = load(REQ_FILE)
    trace = load(TRACE_FILE)
    domain = load(DOMAIN_FILE)
    entries = [r for section in SECTIONS for r in (req.get(section) or [])]

    priority_counts = Counter(r["priority"] for r in entries)
    source_counts = Counter(source for r in entries for source in r.get("sources", []))
    verification_counts = Counter(r["verificationMethod"] for r in entries)
    status_counts = Counter(r["status"] for r in entries)
    namespace_counts = {section: len(req.get(section) or []) for section in SECTIONS}
    namespace_counts["total"] = len(entries)

    uc_to_requirements = defaultdict(list)
    domain_to_requirements = defaultdict(list)
    source_priority = defaultdict(Counter)
    for r in entries:
        for uc in r.get("useCases", []):
            uc_to_requirements[uc].append(r["id"])
        for dref in r.get("domainElements", []):
            key = f"{dref['kind']}:{dref['name']}"
            domain_to_requirements[key].append(r["id"])
        for source in r.get("sources", []):
            source_priority[source][r["priority"]] += 1

    no_uc = [r["id"] for r in entries if not r.get("useCases")]
    no_domain = [r["id"] for r in entries if not r.get("domainElements")]
    no_ac = [r["id"] for r in entries if not r.get("acceptanceCriteria")]
    with_uc = len(entries) - len(no_uc)
    with_domain = len(entries) - len(no_domain)
    with_ac = len(entries) - len(no_ac)
    acceptance_count = sum(len(r.get("acceptanceCriteria", [])) for r in entries)
    test_case_count = sum(len(r.get("testCases", [])) for r in entries)
    diagram_count = sum(len(m.get("diagrams", [])) for m in trace.get("mappings", []))

    coverage = {
        "report": "CityNexus Requirements Coverage Report",
        "version": req["metadata"]["version"],
        "baselineStatus": req["metadata"]["status"],
        "domainModel": req["metadata"]["domainModel"],
        "countsByNamespace": namespace_counts,
        "countsByPriority": {p: priority_counts.get(p, 0) for p in PRIORITIES},
        "countsBySource": {s: source_counts.get(s, 0) for s in SOURCE_CODES},
        "countsByVerificationMethod": dict(sorted(verification_counts.items())),
        "countsByRequirementStatus": dict(sorted(status_counts.items())),
        "acceptanceCriteriaCount": acceptance_count,
        "testCaseCount": test_case_count,
        "diagramReferenceCount": diagram_count,
        "useCaseCoverage": {
            "withMapping": with_uc,
            "withoutMapping": len(no_uc),
            "requirementsWithoutUseCase": no_uc,
            "byUseCase": {uc: sorted(ids) for uc, ids in sorted(uc_to_requirements.items())},
        },
        "domainCoverage": {
            "withMapping": with_domain,
            "withoutMapping": len(no_domain),
            "requirementsWithoutDomainElement": no_domain,
        },
        "acceptanceCoverage": {
            "withAcceptanceCriteria": with_ac,
            "withoutAcceptanceCriteria": len(no_ac),
            "requirementsWithoutAcceptanceCriteria": no_ac,
        },
        "sourcePriorityMatrix": {
            source: {priority: source_priority[source].get(priority, 0) for priority in PRIORITIES}
            for source in SOURCE_CODES
        },
    }

    # Build a complete domain catalogue, including unreferenced elements.
    all_domain = []
    for name in domain.get("entities", {}): all_domain.append(("entity", name))
    for name in domain.get("value_objects", {}): all_domain.append(("value_object", name))
    for name in domain.get("enums", {}): all_domain.append(("enum", name))
    for name in domain.get("state_machines", {}): all_domain.append(("state_machine", name))
    for rule in domain.get("domain_rules", []): all_domain.append(("rule", rule["id"]))

    domain_mapping = {
        "report": "CityNexus Requirements Domain Mapping Report",
        "version": req["metadata"]["version"],
        "domainModelVersion": domain["model"]["version"],
        "domainElementCount": len(all_domain),
        "mappedDomainElementCount": sum(1 for kind, name in all_domain if f"{kind}:{name}" in domain_to_requirements),
        "unmappedDomainElements": [
            {"name": name, "kind": kind}
            for kind, name in all_domain
            if f"{kind}:{name}" not in domain_to_requirements
        ],
        "mappings": [
            {
                "name": name,
                "kind": kind,
                "requirementCount": len(domain_to_requirements.get(f"{kind}:{name}", [])),
                "requirements": sorted(domain_to_requirements.get(f"{kind}:{name}", [])),
            }
            for kind, name in sorted(all_domain, key=lambda x: (x[0], x[1]))
        ],
        "requirementsWithoutDomainElement": no_domain,
    }

    QA_DIR.mkdir(parents=True, exist_ok=True)
    write_json(QA_DIR / "requirements-coverage-report.json", coverage)
    write_json(QA_DIR / "requirements-domain-mapping-report.json", domain_mapping)

    md = [
        "# CityNexus Requirements Coverage Summary",
        "",
        f"- Baseline: **{req['metadata']['version']}** ({req['metadata']['status']})",
        f"- Total requirements: **{len(entries)}**",
        f"- Acceptance criteria: **{acceptance_count}**",
        f"- Requirements with use-case mapping: **{with_uc}/{len(entries)}**",
        f"- Requirements with domain mapping: **{with_domain}/{len(entries)}**",
        f"- Test cases currently defined: **{test_case_count}**",
        f"- Diagram references currently defined: **{diagram_count}**",
        "",
        "## Namespace counts",
        "",
    ]
    for section in SECTIONS:
        md.append(f"- `{section}`: {namespace_counts[section]}")
    md += ["", "## Priority counts", ""]
    for p in PRIORITIES:
        md.append(f"- `{p}`: {priority_counts.get(p, 0)}")
    md += ["", "## Requirements without a core-domain mapping", ""]
    md += [f"- `{rid}`" for rid in no_domain] or ["- None"]
    md += ["", "## Requirements without a use-case mapping", ""]
    md += [f"- `{rid}`" for rid in no_uc] or ["- None"]
    (QA_DIR / "requirements-coverage-summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(json.dumps({
        "requirements": len(entries),
        "acceptanceCriteria": acceptance_count,
        "withUseCase": with_uc,
        "withoutUseCase": no_uc,
        "withDomain": with_domain,
        "withoutDomain": no_domain,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
