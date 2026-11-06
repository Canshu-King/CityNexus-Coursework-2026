#!/usr/bin/env python3
"""Release-gate validator for the CityNexus core domain model."""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_FILE = ROOT / "citynexus-core-domain-model-v0.4.2.yaml"
SCHEMA_FILE = ROOT / "citynexus-domain-model.schema.json"
README_FILE = ROOT / "README.md"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"
REPORT_FILE = ROOT / "qa" / "domain-model-v0.4.2-report.json"

# Prefer normal dependencies; support the project's optional local QA dependency folder.
try:
    import yaml  # type: ignore
    from yaml.constructor import ConstructorError  # type: ignore
    from jsonschema import Draft202012Validator  # type: ignore
except ImportError:
    for candidate in (ROOT / ".qa-deps", ROOT.parent / ".qa-deps"):
        if candidate.exists():
            sys.path.insert(0, str(candidate))
    import yaml  # type: ignore
    from yaml.constructor import ConstructorError  # type: ignore
    from jsonschema import Draft202012Validator  # type: ignore


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys at every nesting level."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise ConstructorError("while constructing a mapping", node.start_mark,
                                   "found unhashable key", key_node.start_mark) from exc
        if key in mapping:
            raise ConstructorError("while constructing a mapping", node.start_mark,
                                   f"found duplicate key ({key})", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def result(name: str, passed: bool, details=None):
    item = {"name": name, "status": "PASS" if passed else "FAIL"}
    if details not in (None, [], {}, ""):
        item["details"] = details
    return item


def schema_error_list(errors):
    rows = []
    for e in sorted(errors, key=lambda x: list(x.absolute_path))[:30]:
        path = ".".join(str(p) for p in e.absolute_path) or "$"
        rows.append(f"{path}: {e.message}")
    return rows


def walk_fields(spec, entities, value_objects, enums, prefix):
    errors = []
    t = spec.get("type") if isinstance(spec, dict) else None
    if t == "entityRef" and spec.get("ref") not in entities:
        errors.append(f"{prefix}: unknown entityRef {spec.get('ref')}")
    elif t == "valueObjectRef" and spec.get("ref") not in value_objects:
        errors.append(f"{prefix}: unknown valueObjectRef {spec.get('ref')}")
    elif t == "enumRef" and spec.get("enumRef") not in enums:
        errors.append(f"{prefix}: unknown enumRef {spec.get('enumRef')}")
    elif t == "array":
        items = spec.get("items")
        if not isinstance(items, dict):
            errors.append(f"{prefix}: array missing typed items")
        else:
            errors.extend(walk_fields(items, entities, value_objects, enums, prefix + "[]"))
    return errors


def all_reachable(initial, transitions):
    edges = defaultdict(set)
    for t in transitions:
        fr = t.get("from")
        sources = fr if isinstance(fr, list) else [fr]
        for s in sources:
            edges[s].add(t.get("to"))
    seen = {initial}
    q = deque([initial])
    while q:
        s = q.popleft()
        for nxt in edges.get(s, set()):
            if nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return seen


def nested_has_null(obj, path="$", found=None):
    if found is None:
        found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if v is None:
                found.append(p)
            nested_has_null(v, p, found)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            nested_has_null(v, f"{path}[{i}]", found)
    return found


def main():
    checks = []

    required_paths = [MODEL_FILE, SCHEMA_FILE, README_FILE, CHANGELOG_FILE, Path(__file__), REPORT_FILE.parent]
    missing_paths = [str(p.relative_to(ROOT)) for p in required_paths if not p.exists()]
    checks.append(result("package structure", not missing_paths, missing_paths))

    # Strict YAML parse is itself the duplicate-key gate.
    try:
        data = yaml.load(MODEL_FILE.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        checks.append(result("strict YAML parse and duplicate-key rejection", isinstance(data, dict)))
    except Exception as exc:
        checks.append(result("strict YAML parse and duplicate-key rejection", False, str(exc)))
        report = {
            "model": "CityNexus Core Domain Model",
            "version": "0.4.1",
            "status": "release-candidate",
            "checks": checks,
            "summary": {"passed": sum(c["status"] == "PASS" for c in checks), "failed": sum(c["status"] == "FAIL" for c in checks)},
        }
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    schema_errors = list(Draft202012Validator(schema).iter_errors(data))
    checks.append(result("JSON Schema", not schema_errors, schema_error_list(schema_errors)))

    entities = data.get("entities", {})
    value_objects = data.get("value_objects", {})
    enums = data.get("enums", {})
    rules = data.get("domain_rules", [])

    checks.append(result("core entity count frozen at 18", len(entities) == 18, len(entities)))
    identities = [e.get("identity") for e in entities.values()]
    dup_identity = sorted({x for x in identities if x and identities.count(x) > 1})
    checks.append(result("unique entity identity fields", not dup_identity, dup_identity))

    rule_ids = [r.get("id") for r in rules]
    dup_rule_ids = sorted({x for x in rule_ids if x and rule_ids.count(x) > 1})
    expected_rule_ids = [f"DR-{i:03d}" for i in range(1, 11)]
    checks.append(result("domain rule IDs unique and contiguous", not dup_rule_ids and rule_ids == expected_rule_ids,
                         {"duplicates": dup_rule_ids, "actual": rule_ids, "expected": expected_rule_ids} if dup_rule_ids or rule_ids != expected_rule_ids else None))
    checks.append(result("legacy business_rules key removed", "business_rules" not in data))

    enum_dupes = {name: vals for name, vals in enums.items() if len(vals) != len(set(vals))}
    checks.append(result("enum values unique", not enum_dupes, enum_dupes))

    valid_mult = {"0", "1", "0..1", "0..*", "1..*"}
    structural_types = {"association", "aggregation", "composition"}
    dependency_types = {"dependency", "projection", "generalization"}
    dangling, invalid_mult, dependency_mult, duplicate_rels = [], [], [], []
    reciprocal = []
    signatures = set()
    directed_pairs = defaultdict(list)

    for source, entity in entities.items():
        for rel in entity.get("relationships", []):
            target, typ, role = rel.get("target"), rel.get("type"), rel.get("role")
            if target not in entities:
                dangling.append(f"{source}->{target} ({role})")
            if typ in structural_types:
                mult = rel.get("multiplicity")
                if not isinstance(mult, dict) or mult.get("source") not in valid_mult or mult.get("target") not in valid_mult:
                    invalid_mult.append(f"{source}->{target} ({role})")
            elif typ in dependency_types and "multiplicity" in rel:
                dependency_mult.append(f"{source}->{target} ({role})")
            sig = (source, target, typ, role)
            if sig in signatures:
                duplicate_rels.append("|".join(map(str, sig)))
            signatures.add(sig)
            if typ in structural_types and target in entities:
                directed_pairs[frozenset((source, target))].append((source, target, role, rel.get("multiplicity")))

    for pair, rels in directed_pairs.items():
        directions = {(a, b) for a, b, _, _ in rels}
        if len(pair) == 2 and len(directions) > 1:
            reciprocal.append({"pair": sorted(pair), "relations": rels})

    checks += [
        result("no dangling relationship targets", not dangling, dangling),
        result("valid relationship multiplicities", not invalid_mult, invalid_mult),
        result("dependencies carry no multiplicity", not dependency_mult, dependency_mult),
        result("no duplicate relationship signatures", not duplicate_rels, duplicate_rels),
        result("no contradictory reciprocal associations", not reciprocal, reciprocal),
    ]

    # Typed refs, typed arrays, and value-object usage.
    bad_refs = []
    vo_refs = defaultdict(list)
    relationship_role_errors = []
    for source, entity in entities.items():
        for field, spec in entity.get("fields", {}).items():
            bad_refs.extend(walk_fields(spec, entities, value_objects, enums, f"{source}.{field}"))
            if isinstance(spec, dict) and spec.get("type") == "valueObjectRef" and spec.get("ref") in value_objects:
                vo_refs[spec["ref"]].append(f"{source}.{field}")
            if isinstance(spec, dict) and spec.get("type") == "array":
                items = spec.get("items", {})
                if items.get("type") == "valueObjectRef" and items.get("ref") in value_objects:
                    vo_refs[items["ref"]].append(f"{source}.{field}[]")
            if isinstance(spec, dict) and spec.get("type") == "entityRef" and spec.get("relationshipRole"):
                role = spec["relationshipRole"]
                target = spec.get("ref")
                matches = [r for r in entity.get("relationships", []) if r.get("role") == role and r.get("target") == target]
                if len(matches) != 1:
                    relationship_role_errors.append(f"{source}.{field}: expected exactly one {role}->{target} relationship")
                else:
                    mult = matches[0].get("multiplicity", {})
                    expected_target = "1" if spec.get("required") else "0..1"
                    if mult.get("target") != expected_target:
                        relationship_role_errors.append(
                            f"{source}.{field}: required={spec.get('required')} but relationship target multiplicity={mult.get('target')}"
                        )

    unused_vos = sorted(set(value_objects) - set(vo_refs))
    used_by_present = [name for name, vo in value_objects.items() if "used_by" in vo]
    checks += [
        result("typed field and array references resolve", not bad_refs, bad_refs),
        result("entityRef relationshipRole consistency", not relationship_role_errors, relationship_role_errors),
        result("all value objects are used by typed fields", not unused_vos, unused_vos),
        result("value objects contain no duplicated used_by metadata", not used_by_present, used_by_present),
    ]

    # State machine semantics against actual enum referenced by status field.
    state_errors, unreachable_errors = [], []
    for entity_name, machine in data.get("state_machines", {}).items():
        entity = entities.get(entity_name)
        if not entity:
            state_errors.append(f"{entity_name}: no matching entity")
            continue
        status_spec = entity.get("fields", {}).get("status")
        if not isinstance(status_spec, dict) or status_spec.get("type") != "enumRef":
            state_errors.append(f"{entity_name}: no enumRef status field")
            continue
        enum_name = status_spec.get("enumRef")
        allowed = set(enums.get(enum_name, []))
        initial = machine.get("initial")
        if initial not in allowed:
            state_errors.append(f"{entity_name}: initial {initial} not in {enum_name}")
        referenced = set()
        for t in machine.get("transitions", []):
            fr = t.get("from")
            sources = fr if isinstance(fr, list) else [fr]
            referenced.update(sources)
            referenced.add(t.get("to"))
        bad = sorted(x for x in referenced if x not in allowed)
        if bad:
            state_errors.append(f"{entity_name}: states not in {enum_name}: {bad}")
        reachable = all_reachable(initial, machine.get("transitions", [])) if initial in allowed else set()
        unreachable = sorted(allowed - reachable)
        if unreachable:
            unreachable_errors.append(f"{entity_name}: unreachable enum states {unreachable}")

    checks += [
        result("state machines use declared status enums", not state_errors, state_errors),
        result("all declared lifecycle states are reachable", not unreachable_errors, unreachable_errors),
    ]

    # Score consistency.
    formula = data.get("replanning_score", {}).get("formula", {})
    dimensions = data.get("replanning_score", {}).get("dimensions", [])
    weights = [v.get("weight", 0) for v in formula.values() if isinstance(v, dict)]
    checks += [
        result("score dimensions exactly match formula keys", set(dimensions) == set(formula) and len(dimensions) == len(formula),
               {"dimensions": dimensions, "formulaKeys": list(formula)} if set(dimensions) != set(formula) or len(dimensions) != len(formula) else None),
        result("score weights sum == 1.0", abs(sum(weights) - 1.0) < 1e-9, sum(weights)),
        result("no unexpected null values", not nested_has_null(data), nested_has_null(data)),
    ]

    # Graph connectivity.
    degrees = {name: 0 for name in entities}
    for source, entity in entities.items():
        for rel in entity.get("relationships", []):
            target = rel.get("target")
            if target in degrees:
                degrees[source] += 1
                degrees[target] += 1
    isolated = [name for name, deg in degrees.items() if deg == 0]
    checks.append(result("no isolated core entities", not isolated, isolated))

    # Traceability exact UC set and valid refs.
    expected_ucs = [f"UC-{i:02d}" for i in range(1, 14)]
    use_cases = data.get("traceability", {}).get("use_cases", {})
    uc_set_ok = list(use_cases.keys()) == expected_ucs
    trace_bad = []
    for uc, spec in use_cases.items():
        for ref in spec.get("domain_elements", []):
            if ref not in entities and ref not in value_objects:
                trace_bad.append(f"{uc}:{ref}")
    uc13 = use_cases.get("UC-13", {})
    uc13_ok = uc13.get("domain_elements") == [] and uc13.get("layer") == "application_infrastructure"
    checks += [
        result("UC-01 through UC-13 exist exactly once and in order", uc_set_ok, list(use_cases.keys())),
        result("use-case domain references resolve", not trace_bad, trace_bad),
        result("UC-13 correctly marked application/infrastructure", uc13_ok, uc13 if not uc13_ok else None),
    ]

    # Domain-specific release gates.
    journey_enum = set(enums.get("journey_status", []))
    lifecycle_map = None
    for inv in entities.get("Journey", {}).get("invariants", []):
        if isinstance(inv, dict) and "active_plan_lifecycle" in inv:
            lifecycle_map = inv["active_plan_lifecycle"]
            break
    lifecycle_ok = isinstance(lifecycle_map, dict) and set(lifecycle_map) == journey_enum
    checks.append(result("Journey active-plan lifecycle covers every journey status", lifecycle_ok,
                         {"expected": sorted(journey_enum), "actual": sorted(lifecycle_map or {})} if not lifecycle_ok else None))

    pref_invariants = " ".join(map(str, entities.get("Preference", {}).get("invariants", [])))
    pref_ok = all(token in pref_invariants for token in ["scope=traveler", "scope=journey", "scope=temporary", "validUntil"])
    checks.append(result("Preference scope invariants present", pref_ok, pref_invariants if not pref_ok else None))

    payment_invariants = " ".join(map(str, entities.get("PaymentTransaction", {}).get("invariants", [])))
    checks.append(result("PaymentTransaction exactly-one subject invariant present", "booking_xor_order" in payment_invariants))
    checks.append(result("Progressive replanning rule DR-008 present", any(r.get("id") == "DR-008" for r in rules)))
    checks.append(result("AI authority rule DR-010 present", any(r.get("id") == "DR-010" for r in rules)))

    # Sanity coverage for the already-agreed CityNexus scope.
    expected_features = {
        "journey anchors": all(f in entities["Journey"]["fields"] for f in ["originPlaceId", "finalDestinationPlaceId", "startAt", "endAt", "budgetLimit"]),
        "immutable plan versioning": "PlanVersion" in entities and any(r.get("id") == "DR-009" for r in rules),
        "journey twin context": all(f in entities["JourneyTwin"]["fields"] for f in ["currentActivityId", "nextActivityId", "nextHardCommitmentId", "riskScore", "opportunityWindow"]),
        "real place grounding": all(f in entities["Place"]["fields"] for f in ["provider", "providerPlaceId", "observedAt"]),
        "commitment strength/status": all(f in entities["Commitment"]["fields"] for f in ["strength", "status", "sourceType"]),
        "three-level constraints": set(enums.get("constraint_severity", [])) == {"system_hard", "commitment_hard", "soft"},
        "progressive replan candidates": "CandidatePlan" in entities and "ReplanProposal" in entities and set(enums.get("replan_scope", [])) == {"local", "window", "global", "exception"},
        "score breakdown": "ScoreBreakdown" in value_objects,
        "events and freshness": all(f in entities["Event"]["fields"] for f in ["observedAt", "expiresAt", "confidence", "impactLevel"]),
        "booking/order/payment": all(e in entities for e in ["Booking", "Order", "PaymentTransaction"]),
        "scoped preferences": "scope" in entities["Preference"]["fields"],
        "AI cannot own feasibility": any(r.get("id") == "DR-010" for r in rules),
    }
    missing_features = [name for name, ok in expected_features.items() if not ok]
    checks.append(result("requirements coverage sanity", not missing_features, missing_features))

    readme = README_FILE.read_text(encoding="utf-8")
    changelog = CHANGELOG_FILE.read_text(encoding="utf-8")
    readme_ok = "v0.4.2" in readme and "citynexus-core-domain-model-v0.4.2.yaml" in readme
    changelog_ok = re.search(r"^## 0\.4\.1\b", changelog, flags=re.MULTILINE) is not None
    checks += [
        result("README current baseline version consistency", readme_ok),
        result("CHANGELOG contains v0.4.2 entry", changelog_ok),
    ]

    report = {
        "model": data.get("model", {}).get("name"),
        "modelId": data.get("model", {}).get("id"),
        "version": data.get("model", {}).get("version"),
        "status": data.get("model", {}).get("status"),
        "checks": checks,
        "coverage": expected_features,
        "summary": {
            "passed": sum(c["status"] == "PASS" for c in checks),
            "failed": sum(c["status"] == "FAIL" for c in checks),
            "entityCount": len(entities),
            "relationshipCount": sum(len(e.get("relationships", [])) for e in entities.values()),
            "valueObjectCount": len(value_objects),
            "enumCount": len(enums),
            "domainRuleCount": len(rules),
            "useCaseCount": len(use_cases),
        },
    }
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
