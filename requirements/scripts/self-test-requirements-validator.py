#!/usr/bin/env python3
"""Mutation tests proving the CityNexus requirements validator rejects bad data."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REQ_DIR = ROOT / "requirements"
REPORT = REQ_DIR / "qa" / "requirements-validator-self-test.json"

VALIDATOR_PATH = REQ_DIR / "scripts" / "validate-requirements.py"
_spec = importlib.util.spec_from_file_location("citynexus_requirements_validator", VALIDATOR_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"Cannot load validator from {VALIDATOR_PATH}")
validator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validator)


def dump(path: Path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")


def metadata(status="infrastructure-only"):
    return {
        "name": "CityNexus Requirements Baseline",
        "version": "0.3.0",
        "status": status,
        "officialProject": "Smart City Traveler System",
        "product": "CityNexus",
        "domainModel": "../domain-model/citynexus-core-domain-model-v0.4.2.yaml",
        "useCases": [f"UC-{i:02d}" for i in range(1, 14)],
        "expectedCounts": {
            "business_goals": 8,
            "functional_requirements": 61,
            "business_rules": 24,
            "non_functional_requirements": 16,
            "security_requirements": 14,
            "integration_requirements": 15,
            "total": 138,
        },
    }


def base_requirement(rid="FR-001"):
    return {
        "id": rid,
        "type": "functional_requirement",
        "title": "Self-test requirement",
        "statement": "The system shall support the self-test fixture.",
        "sources": ["UJ"],
        "priority": "M",
        "rationale": "Validator mutation fixture.",
        "acceptanceCriteria": [{
            "id": f"{rid}-AC-01",
            "condition": "Given the fixture is active",
            "expectedResult": "The fixture behaves deterministically",
            "verificationMethod": "functional_test",
        }],
        "useCases": ["UC-01"],
        "domainElements": [{"name": "Traveler", "kind": "entity"}],
        "dependsOn": [],
        "conflictsWith": [],
        "verificationMethod": "functional_test",
        "testCases": [],
        "status": "draft",
    }


def base_mapping(r):
    return {
        "requirementId": r["id"],
        "useCases": list(r["useCases"]),
        "domainElements": [dict(x) for x in r["domainElements"]],
        "diagrams": [],
        "testCases": [],
    }


def fresh_data():
    r = base_requirement()
    req = {
        "metadata": metadata(),
        "business_goals": [],
        "functional_requirements": [r],
        "business_rules": [],
        "non_functional_requirements": [],
        "security_requirements": [],
        "integration_requirements": [],
    }
    trace = {
        "metadata": {
            "name": "CityNexus Requirements Traceability Baseline",
            "version": "0.3.0",
            "status": "infrastructure-only",
            "requirementsFile": "requirements.yaml",
            "domainModel": "../domain-model/citynexus-core-domain-model-v0.4.2.yaml",
        },
        "mappings": [base_mapping(r)],
    }
    return req, trace


def prepare(case: Path):
    shutil.copytree(ROOT / "domain-model", case / "domain-model")
    shutil.copytree(REQ_DIR, case / "requirements")
    req_path = case / "requirements" / "requirements.yaml"
    trace_path = case / "requirements" / "traceability.yaml"
    req, trace = fresh_data()
    dump(req_path, req)
    dump(trace_path, trace)
    return req_path, trace_path


def run(case: Path):
    req_dir = case / "requirements"
    validator.ROOT = case
    validator.REQ_DIR = req_dir
    validator.REQ_FILE = req_dir / "requirements.yaml"
    validator.TRACE_FILE = req_dir / "traceability.yaml"
    validator.REQ_SCHEMA_FILE = req_dir / "requirements.schema.json"
    validator.TRACE_SCHEMA_FILE = req_dir / "traceability.schema.json"
    validator.REPORT_FILE = req_dir / "qa" / "requirements-report.json"
    validator.REPORT_VERSIONED_FILE = req_dir / "qa" / "requirements-v0.3-report.json"
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = validator.main()
    report_path = validator.REPORT_FILE
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    return code, report, stream.getvalue()


def failed_check(report, name):
    return any(x.get("name") == name and x.get("status") == "FAIL" for x in report.get("checks", []))


def record(tests, name, ok, expected_check):
    tests.append({"name": name, "expected": f"reject via {expected_check}", "status": "PASS" if ok else "FAIL"})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}", flush=True)


def main():
    tests = []
    with tempfile.TemporaryDirectory(prefix="cnx-req-validator-selftest-") as td:
        base = Path(td)

        case = base / "duplicate_key"; req_path, _ = prepare(case)
        with req_path.open("a", encoding="utf-8") as f: f.write("\nmetadata:\n  name: duplicate\n")
        code, report, _ = run(case)
        record(tests, "duplicate YAML key", code != 0 and failed_check(report, "strict YAML duplicate-key detection"), "strict YAML duplicate-key detection")

        case = base / "wrong_section"; req_path, _ = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); r = req["functional_requirements"].pop(); req["business_goals"] = [r]; dump(req_path, req)
        code, report, _ = run(case)
        record(tests, "FR placed in business_goals", code != 0 and failed_check(report, "section / ID prefix / type consistency"), "section / ID prefix / type consistency")

        case = base / "wrong_domain_kind"; req_path, trace_path = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); req["functional_requirements"][0]["domainElements"][0]["kind"] = "enum"; dump(req_path, req)
        tr = yaml.safe_load(trace_path.read_text(encoding="utf-8")); tr["mappings"][0]["domainElements"][0]["kind"] = "enum"; dump(trace_path, tr)
        code, report, _ = run(case)
        record(tests, "domain name with wrong kind", code != 0 and failed_check(report, "domain references resolve by name and kind"), "domain references resolve by name and kind")

        case = base / "duplicate_trace"; _, trace_path = prepare(case)
        tr = yaml.safe_load(trace_path.read_text(encoding="utf-8")); tr["mappings"].append(dict(tr["mappings"][0])); dump(trace_path, tr)
        code, report, _ = run(case)
        record(tests, "duplicate trace mapping", code != 0 and failed_check(report, "traceability mapping IDs are unique"), "traceability mapping IDs are unique")

        case = base / "dependency_cycle"; req_path, trace_path = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); r1 = req["functional_requirements"][0]; r2 = base_requirement("FR-002"); r1["dependsOn"] = ["FR-002"]; r2["dependsOn"] = ["FR-001"]; req["functional_requirements"].append(r2); dump(req_path, req)
        tr = yaml.safe_load(trace_path.read_text(encoding="utf-8")); tr["mappings"].append(base_mapping(r2)); dump(trace_path, tr)
        code, report, _ = run(case)
        record(tests, "dependsOn cycle", code != 0 and failed_check(report, "dependsOn graph is acyclic"), "dependsOn graph is acyclic")

        case = base / "self_dependency"; req_path, _ = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); req["functional_requirements"][0]["dependsOn"] = ["FR-001"]; dump(req_path, req)
        code, report, _ = run(case)
        record(tests, "self dependency", code != 0 and failed_check(report, "no self dependency/conflict"), "no self dependency/conflict")

        case = base / "duplicate_ac_semantics"; req_path, _ = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); ac = dict(req["functional_requirements"][0]["acceptanceCriteria"][0]); ac["id"] = "FR-001-AC-02"; req["functional_requirements"][0]["acceptanceCriteria"].append(ac); dump(req_path, req)
        code, report, _ = run(case)
        record(tests, "duplicate acceptance semantics", code != 0 and failed_check(report, "no duplicate acceptance-criterion semantics"), "no duplicate acceptance-criterion semantics")

        case = base / "bad_source"; req_path, _ = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); req["functional_requirements"][0]["sources"] = ["MAGIC"]; dump(req_path, req)
        code, report, _ = run(case)
        record(tests, "invalid source code", code != 0 and failed_check(report, "JSON Schema validation"), "JSON Schema validation")

        case = base / "bad_priority"; req_path, _ = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); req["functional_requirements"][0]["priority"] = "urgent"; dump(req_path, req)
        code, report, _ = run(case)
        record(tests, "invalid priority", code != 0 and failed_check(report, "JSON Schema validation"), "JSON Schema validation")

        case = base / "wrong_ac_owner"; req_path, _ = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); req["functional_requirements"][0]["acceptanceCriteria"][0]["id"] = "BG-001-AC-01"; dump(req_path, req)
        code, report, _ = run(case)
        record(tests, "acceptance criterion owned by another requirement", code != 0 and failed_check(report, "acceptance-criterion IDs belong to their requirement"), "acceptance-criterion IDs belong to their requirement")

        case = base / "missing_trace"; _, trace_path = prepare(case)
        tr = yaml.safe_load(trace_path.read_text(encoding="utf-8")); tr["mappings"] = []; dump(trace_path, tr)
        code, report, _ = run(case)
        record(tests, "missing trace mapping", code != 0 and failed_check(report, "traceability completeness and mirror consistency"), "traceability completeness and mirror consistency")

        case = base / "trace_mirror_mismatch"; _, trace_path = prepare(case)
        tr = yaml.safe_load(trace_path.read_text(encoding="utf-8")); tr["mappings"][0]["useCases"] = ["UC-02"]; dump(trace_path, tr)
        code, report, _ = run(case)
        record(tests, "traceability mirror mismatch", code != 0 and failed_check(report, "traceability completeness and mirror consistency"), "traceability completeness and mirror consistency")

        case = base / "id_gap"; req_path, trace_path = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); r = req["functional_requirements"][0]; r["id"] = "FR-002"; r["acceptanceCriteria"][0]["id"] = "FR-002-AC-01"; dump(req_path, req)
        tr = yaml.safe_load(trace_path.read_text(encoding="utf-8")); tr["mappings"][0]["requirementId"] = "FR-002"; dump(trace_path, tr)
        code, report, _ = run(case)
        record(tests, "non-continuous requirement IDs", code != 0 and failed_check(report, "requirement IDs are continuous and ordered"), "requirement IDs are continuous and ordered")

        case = base / "method_mismatch"; req_path, _ = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); req["functional_requirements"][0]["acceptanceCriteria"][0]["verificationMethod"] = "inspection"; dump(req_path, req)
        code, report, _ = run(case)
        record(tests, "acceptance method mismatch", code != 0 and failed_check(report, "acceptance-criterion verification methods match their requirement"), "acceptance-criterion verification methods match their requirement")

        case = base / "missing_core_uc"; req_path, trace_path = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); req["functional_requirements"][0]["useCases"] = []; dump(req_path, req)
        tr = yaml.safe_load(trace_path.read_text(encoding="utf-8")); tr["mappings"][0]["useCases"] = []; dump(trace_path, tr)
        code, report, _ = run(case)
        record(tests, "functional requirement without use case", code != 0 and failed_check(report, "business goals, functional requirements and business rules have use-case mappings"), "business goals, functional requirements and business rules have use-case mappings")

        case = base / "dangling_trace_tc"; _, trace_path = prepare(case)
        tr = yaml.safe_load(trace_path.read_text(encoding="utf-8")); tr["mappings"][0]["testCases"] = ["TC-999"]; dump(trace_path, tr)
        code, report, _ = run(case)
        record(tests, "dangling trace test case", code != 0 and failed_check(report, "dangling trace test-case references"), "dangling trace test-case references")

        case = base / "exact_count_gate"; req_path, trace_path = prepare(case)
        req = yaml.safe_load(req_path.read_text(encoding="utf-8")); req["metadata"]["status"] = "baseline-candidate"; dump(req_path, req)
        tr = yaml.safe_load(trace_path.read_text(encoding="utf-8")); tr["metadata"]["status"] = "baseline-candidate"; dump(trace_path, tr)
        code, report, _ = run(case)
        record(tests, "baseline candidate exact-count gate", code != 0 and (failed_check(report, "JSON Schema validation") or failed_check(report, "exact 138-item baseline counts")), "exact 138-item baseline counts")

    payload = {
        "validator": "validate-requirements.py",
        "version": "0.3.0",
        "tests": tests,
        "summary": {
            "passed": sum(x["status"] == "PASS" for x in tests),
            "failed": sum(x["status"] == "FAIL" for x in tests),
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
