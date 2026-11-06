#!/usr/bin/env python3
"""Mutation self-tests proving the release validator rejects representative bad models."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    import sys
    ROOT0 = Path(__file__).resolve().parents[1]
    for candidate in (ROOT0 / ".qa-deps", ROOT0.parent / ".qa-deps"):
        if candidate.exists():
            sys.path.insert(0, str(candidate))
    import yaml

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "citynexus-core-domain-model-v0.4.2.yaml"
SCHEMA = ROOT / "citynexus-domain-model.schema.json"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
VALIDATOR = ROOT / "scripts" / "validate-domain-model.py"
REPORT = ROOT / "qa" / "validator-self-test.json"


def make_case(base: Path, name: str) -> Path:
    case = base / name
    (case / "scripts").mkdir(parents=True)
    (case / "qa").mkdir(parents=True)
    for src in (MODEL, SCHEMA, README, CHANGELOG):
        shutil.copy2(src, case / src.name)
    shutil.copy2(VALIDATOR, case / "scripts" / VALIDATOR.name)
    return case


def run_validator(case: Path):
    proc = subprocess.run(
        ["python", str(case / "scripts" / "validate-domain-model.py")],
        cwd=case,
        text=True,
        capture_output=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def dump_yaml(path: Path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8")


def main():
    tests = []
    with tempfile.TemporaryDirectory(prefix="citynexus-validator-selftest-") as td:
        base = Path(td)

        case = make_case(base, "duplicate_key")
        with (case / MODEL.name).open("a", encoding="utf-8") as f:
            f.write("\nmodel:\n  id: DUPLICATE\n")
        code, output = run_validator(case)
        tests.append({"name": "duplicate YAML key", "expected": "reject", "status": "PASS" if code != 0 and "duplicate key" in output else "FAIL"})

        case = make_case(base, "invalid_state")
        p = case / MODEL.name
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        data["state_machines"]["Activity"]["transitions"][2]["to"] = "ghost_state"
        dump_yaml(p, data)
        code, output = run_validator(case)
        tests.append({"name": "state not declared in enum", "expected": "reject", "status": "PASS" if code != 0 and "ghost_state" in output else "FAIL"})

        case = make_case(base, "missing_array_items")
        p = case / MODEL.name
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        data["entities"]["ReplanProposal"]["fields"]["triggerEventIds"].pop("items", None)
        dump_yaml(p, data)
        code, output = run_validator(case)
        tests.append({"name": "array without items type", "expected": "reject", "status": "PASS" if code != 0 and "items" in output else "FAIL"})

        case = make_case(base, "reciprocal_association")
        p = case / MODEL.name
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        data["entities"]["Booking"].setdefault("relationships", []).append({
            "type": "association",
            "target": "PaymentTransaction",
            "multiplicity": {"source": "0..1", "target": "0..*"},
            "role": "settled_by",
        })
        dump_yaml(p, data)
        code, output = run_validator(case)
        tests.append({"name": "contradictory reciprocal association", "expected": "reject", "status": "PASS" if code != 0 and "reciprocal" in output else "FAIL"})

    summary = {"passed": sum(t["status"] == "PASS" for t in tests), "failed": sum(t["status"] == "FAIL" for t in tests)}
    payload = {"validator": "validate-domain-model.py", "tests": tests, "summary": summary}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
