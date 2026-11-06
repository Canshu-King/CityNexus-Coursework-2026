#!/usr/bin/env python3
"""Release-gate validator for CityNexus requirements infrastructure and baselines."""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
REQ_DIR = ROOT / "requirements"
REQ_FILE = REQ_DIR / "requirements.yaml"
TRACE_FILE = REQ_DIR / "traceability.yaml"
REQ_SCHEMA_FILE = REQ_DIR / "requirements.schema.json"
TRACE_SCHEMA_FILE = REQ_DIR / "traceability.schema.json"
REPORT_FILE = REQ_DIR / "qa" / "requirements-report.json"
REPORT_VERSIONED_FILE = REQ_DIR / "qa" / "requirements-v0.3-report.json"

SECTION_META = {
    "business_goals": ("BG", "business_goal"),
    "functional_requirements": ("FR", "functional_requirement"),
    "business_rules": ("BR", "business_rule"),
    "non_functional_requirements": ("NFR", "non_functional_requirement"),
    "security_requirements": ("SR", "security_requirement"),
    "integration_requirements": ("IR", "integration_requirement"),
}
USE_CASES = {f"UC-{i:02d}" for i in range(1, 14)}
SOURCE_CODES = {"CW", "UJ", "DM", "OPT", "AI", "SEC", "ARCH", "GH-PR", "GH-TS", "GH-OPT"}
PRIORITIES = {"M", "S", "C", "W"}


class StrictLoader(yaml.SafeLoader):
    pass


def construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)


def load_yaml(path: Path):
    with path.open(encoding="utf-8") as f:
        return yaml.load(f, Loader=StrictLoader)


def item(name, status, details=None):
    row = {"name": name, "status": status}
    if details not in (None, [], {}, ""):
        row["details"] = details
    return row


def passed(name, ok, details=None):
    return item(name, "PASS" if ok else "FAIL", details)


def skipped(name, reason):
    return item(name, "SKIP", reason)


def schema_errors(data, schema):
    out = []
    for e in sorted(Draft202012Validator(schema).iter_errors(data), key=lambda x: list(x.absolute_path))[:50]:
        path = ".".join(str(x) for x in e.absolute_path) or "$"
        out.append(f"{path}: {e.message}")
    return out


def normalize_text(value):
    return " ".join(str(value or "").split()).casefold()


def dependency_cycles(graph):
    cycles = []
    state = {}  # 0 unseen, 1 visiting, 2 done
    stack = []

    def visit(node):
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt, 0) == 0:
                visit(nxt)
            elif state.get(nxt) == 1:
                try:
                    i = stack.index(nxt)
                    cyc = stack[i:] + [nxt]
                except ValueError:
                    cyc = [node, nxt, node]
                if cyc not in cycles:
                    cycles.append(cyc)
        stack.pop()
        state[node] = 2

    for n in graph:
        if state.get(n, 0) == 0:
            visit(n)
    return cycles


def main():
    checks = []
    try:
        req = load_yaml(REQ_FILE)
        trace = load_yaml(TRACE_FILE)
        checks.append(passed("strict YAML duplicate-key detection", True))
    except Exception as exc:
        checks.append(passed("strict YAML duplicate-key detection", False, str(exc)))
        return finish(checks, {})

    try:
        req_schema = json.loads(REQ_SCHEMA_FILE.read_text(encoding="utf-8"))
        trace_schema = json.loads(TRACE_SCHEMA_FILE.read_text(encoding="utf-8"))
        errs = schema_errors(req, req_schema) + schema_errors(trace, trace_schema)
        checks.append(passed("JSON Schema validation", not errs, errs))
    except Exception as exc:
        checks.append(passed("JSON Schema validation", False, str(exc)))

    # Flatten requirements while preserving section.
    entries = []
    for section in SECTION_META:
        for r in req.get(section, []) or []:
            entries.append((section, r))
    ids = [r.get("id") for _, r in entries]
    by_id = {r.get("id"): r for _, r in entries if r.get("id")}
    section_by_id = {r.get("id"): section for section, r in entries if r.get("id")}

    dup_ids = sorted({rid for rid in ids if rid and ids.count(rid) > 1})
    checks.append(passed("unique requirement IDs", not dup_ids, dup_ids))

    section_errors = []
    for section, r in entries:
        prefix, typ = SECTION_META[section]
        rid = r.get("id", "")
        if not rid.startswith(prefix + "-") or r.get("type") != typ:
            section_errors.append({"section": section, "id": rid, "type": r.get("type"), "expectedPrefix": prefix, "expectedType": typ})
    checks.append(passed("section / ID prefix / type consistency", not section_errors, section_errors))

    # Baseline identifiers must be continuous and ordered within each namespace.
    sequence_errors = []
    for section, (prefix, _) in SECTION_META.items():
        section_ids = [r.get("id") for sec, r in entries if sec == section]
        expected_ids = [f"{prefix}-{i:03d}" for i in range(1, len(section_ids) + 1)]
        if section_ids != expected_ids:
            sequence_errors.append({"section": section, "actual": section_ids, "expected": expected_ids})
    checks.append(passed("requirement IDs are continuous and ordered", not sequence_errors, sequence_errors))

    # Fixed use-case catalog.
    meta_ucs = req.get("metadata", {}).get("useCases", []) or []
    checks.append(passed("metadata contains exactly UC-01 through UC-13", set(meta_ucs) == USE_CASES and len(meta_ucs) == 13, meta_ucs))

    # Load domain and build exact name+kind catalog.
    domain_catalog = set()
    domain_load_error = None
    try:
        domain_path = (REQ_DIR / req["metadata"]["domainModel"]).resolve()
        model = load_yaml(domain_path)
        domain_catalog |= {(name, "entity") for name in model.get("entities", {})}
        domain_catalog |= {(name, "value_object") for name in model.get("value_objects", {})}
        domain_catalog |= {(name, "enum") for name in model.get("enums", {})}
        domain_catalog |= {(name, "state_machine") for name in model.get("state_machines", {})}
        domain_catalog |= {(r.get("id"), "rule") for r in model.get("domain_rules", []) if isinstance(r, dict) and r.get("id")}
    except Exception as exc:
        domain_load_error = str(exc)
    checks.append(passed("domain model loads dynamically", domain_load_error is None, domain_load_error))

    uc_bad = []
    domain_bad = []
    source_bad = []
    priority_bad = []
    ac_owner_bad = []
    ac_id_dupes = []
    ac_semantic_dupes = []
    ac_method_mismatches = []
    test_id_dupes = []
    missing_core_uc_mapping = []
    non_candidate_status = []
    self_deps = []
    overlap_dep_conflict = []
    dangling_req_refs = []
    graph = defaultdict(list)
    all_test_ids = []

    for _, r in entries:
        rid = r.get("id")
        for uc in r.get("useCases", []) or []:
            if uc not in USE_CASES:
                uc_bad.append(f"{rid}:{uc}")
        for ref in r.get("domainElements", []) or []:
            key = (ref.get("name"), ref.get("kind"))
            if key not in domain_catalog:
                domain_bad.append({"requirement": rid, "name": ref.get("name"), "kind": ref.get("kind")})
        bad_sources = [x for x in r.get("sources", []) or [] if x not in SOURCE_CODES]
        if bad_sources:
            source_bad.append({"requirement": rid, "sources": bad_sources})
        if r.get("priority") not in PRIORITIES:
            priority_bad.append({"requirement": rid, "priority": r.get("priority")})

        acs = r.get("acceptanceCriteria", []) or []
        local_ids = [a.get("id") for a in acs]
        if len(local_ids) != len(set(local_ids)):
            ac_id_dupes.append(rid)
        for a in acs:
            aid = a.get("id", "")
            if rid and not aid.startswith(rid + "-AC-"):
                ac_owner_bad.append({"requirement": rid, "acceptanceCriterion": aid})
        seen_sem = set()
        for a in acs:
            sig = (normalize_text(a.get("condition")), normalize_text(a.get("expectedResult")))
            if sig in seen_sem:
                ac_semantic_dupes.append({"requirement": rid, "signature": sig})
            seen_sem.add(sig)
            if a.get("verificationMethod") != r.get("verificationMethod"):
                ac_method_mismatches.append({"requirement": rid, "criterion": a.get("id"), "criterionMethod": a.get("verificationMethod"), "requirementMethod": r.get("verificationMethod")})

        if section_by_id.get(rid) in {"business_goals", "functional_requirements", "business_rules"} and not (r.get("useCases") or []):
            missing_core_uc_mapping.append(rid)
        if req.get("metadata", {}).get("status") == "baseline-candidate" and r.get("status") != "proposed":
            non_candidate_status.append({"requirement": rid, "status": r.get("status")})

        tcs = r.get("testCases", []) or []
        local_tc = [t.get("id") for t in tcs]
        all_test_ids.extend(local_tc)
        if len(local_tc) != len(set(local_tc)):
            test_id_dupes.append(rid)

        deps = r.get("dependsOn", []) or []
        conflicts = r.get("conflictsWith", []) or []
        graph[rid] = list(deps)
        if rid in deps or rid in conflicts:
            self_deps.append(rid)
        overlap = sorted(set(deps) & set(conflicts))
        if overlap:
            overlap_dep_conflict.append({"requirement": rid, "refs": overlap})
        for dep in deps + conflicts:
            if dep not in by_id:
                dangling_req_refs.append({"requirement": rid, "reference": dep})

    global_test_dupes = sorted({x for x in all_test_ids if x and all_test_ids.count(x) > 1})
    if global_test_dupes:
        test_id_dupes.append({"global": global_test_dupes})

    checks += [
        passed("use-case references resolve", not uc_bad, uc_bad),
        passed("domain references resolve by name and kind", not domain_bad, domain_bad),
        passed("source codes are valid", not source_bad, source_bad),
        passed("priorities are valid MoSCoW codes", not priority_bad, priority_bad),
        passed("acceptance-criterion IDs belong to their requirement", not ac_owner_bad, ac_owner_bad),
        passed("acceptance-criterion IDs are unique", not ac_id_dupes, ac_id_dupes),
        passed("no duplicate acceptance-criterion semantics", not ac_semantic_dupes, ac_semantic_dupes),
        passed("acceptance-criterion verification methods match their requirement", not ac_method_mismatches, ac_method_mismatches),
        passed("business goals, functional requirements and business rules have use-case mappings", not missing_core_uc_mapping, missing_core_uc_mapping),
        passed("baseline-candidate requirement statuses are proposed", not non_candidate_status, non_candidate_status),
        passed("test-case IDs are unique", not test_id_dupes, test_id_dupes),
        passed("requirement references resolve", not dangling_req_refs, dangling_req_refs),
        passed("no self dependency/conflict", not self_deps, self_deps),
        passed("dependsOn and conflictsWith do not overlap", not overlap_dep_conflict, overlap_dep_conflict),
    ]

    cycles = dependency_cycles({k: [x for x in v if x in by_id] for k, v in graph.items()}) if ids else []
    checks.append(passed("dependsOn graph is acyclic", not cycles, cycles))

    # Traceability must be exactly one mapping per requirement and mirror the requirement view.
    maps = trace.get("mappings", []) or []
    map_ids = [m.get("requirementId") for m in maps]
    map_dupes = sorted({x for x in map_ids if x and map_ids.count(x) > 1})
    checks.append(passed("traceability mapping IDs are unique", not map_dupes, map_dupes))

    if not ids and not maps:
        checks.append(skipped("traceability completeness and mirror consistency", "infrastructure-only dataset contains no concrete requirements"))
        checks.append(skipped("dangling trace test-case references", "no traceability mappings yet"))
    else:
        missing_maps = sorted(set(ids) - set(map_ids))
        extra_maps = sorted(set(map_ids) - set(ids))
        mismatch = []
        dangling_trace_tc = []
        for m in maps:
            rid = m.get("requirementId")
            if rid not in by_id:
                continue
            r = by_id[rid]
            req_domain = {(x.get("name"), x.get("kind")) for x in r.get("domainElements", []) or []}
            map_domain = {(x.get("name"), x.get("kind")) for x in m.get("domainElements", []) or []}
            req_tc = {x.get("id") for x in r.get("testCases", []) or []}
            map_tc = set(m.get("testCases", []) or [])
            if set(r.get("useCases", []) or []) != set(m.get("useCases", []) or []) or req_domain != map_domain or req_tc != map_tc:
                mismatch.append(rid)
            dangling_trace_tc.extend([f"{rid}:{tc}" for tc in map_tc if tc not in set(all_test_ids)])
        checks.append(passed("traceability completeness and mirror consistency", not missing_maps and not extra_maps and not mismatch,
                             {"missing": missing_maps, "extra": extra_maps, "mismatch": mismatch}))
        checks.append(passed("dangling trace test-case references", not dangling_trace_tc, dangling_trace_tc))

    # Exact counts only become a release gate after infrastructure-only status.
    counts = {sec: len(req.get(sec, []) or []) for sec in SECTION_META}
    counts["total"] = sum(counts.values())
    expected = req.get("metadata", {}).get("expectedCounts", {}) or {}
    status = req.get("metadata", {}).get("status")
    if status == "infrastructure-only":
        checks.append(skipped("exact 138-item baseline counts", {"current": counts, "expected": expected}))
    else:
        checks.append(passed("exact 138-item baseline counts", counts == expected, {"current": counts, "expected": expected}))

    # For any concrete requirement, acceptance criteria must exist. Schema also enforces this.
    if ids:
        empty = [rid for rid, r in by_id.items() if not (r.get("acceptanceCriteria") or [])]
        checks.append(passed("concrete requirements have acceptance criteria", not empty, empty))
    else:
        checks.append(skipped("concrete requirements have acceptance criteria", "no concrete requirements yet"))

    # A concrete baseline candidate must not rely on infrastructure-only SKIPs.
    if status == "baseline-candidate":
        current_skips = [c["name"] for c in checks if c["status"] == "SKIP"]
        checks.append(passed("baseline-candidate has no infrastructure-only skipped checks", not current_skips, current_skips))

    details = {
        "requirementCount": len(ids),
        "traceabilityMappingCount": len(maps),
        "counts": counts,
        "status": status,
        "domainCatalogSize": len(domain_catalog),
    }
    return finish(checks, details)


def finish(checks, details):
    summary = {
        "passed": sum(c["status"] == "PASS" for c in checks),
        "failed": sum(c["status"] == "FAIL" for c in checks),
        "skipped": sum(c["status"] == "SKIP" for c in checks),
    }
    payload = {"validator": "validate-requirements.py", "version": "0.3.0", "checks": checks, "details": details, "summary": summary}
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    REPORT_FILE.write_text(rendered, encoding="utf-8")
    REPORT_VERSIONED_FILE.write_text(rendered, encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
