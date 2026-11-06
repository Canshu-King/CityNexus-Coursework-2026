"""Shipped guard for the comment-anchor drift order (rel lane a68b0631).

Two claims, both machine-checkable and neither one a judgement call:

  G1  every citation this lane un-numbered stays un-numbered.  The registry names file and line; if a
      line number is written back into that comment, this test goes red -- which is the counterexample
      the order asked for, kept live rather than demonstrated once in a block.
  G2  no citation anywhere in the shipped tree points at a file that does not exist, or at a line
      beyond the end of a file that does.  Those are unambiguously dead whatever the prose meant.

G2 deliberately does not try to decide whether a still-in-range line number "means" the right thing:
only the range check and the registry are asserted, so the guard can never manufacture a false green
by guessing at intent.
"""
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "frontend" / "review" / "line_anchor_drift" / "CONVERTED_r1.txt"
CITE = re.compile(r"([A-Za-z0-9_./\\-]{0,80})\.(tsx|ts|py|cjs|mjs|js|json|yaml|yml|md|css|sql):(\d{1,6})(?:-\d{1,6})?")
COMMENT = re.compile(r"^\s*(//|\*|/\*|#|##|-|\s*\*/)")
SKIP = {"node_modules", ".git", "dist", "data", "test-results", ".venv", "__pycache__"}
EXTENSIONS = {".ts", ".tsx", ".py", ".js", ".cjs", ".mjs", ".md", ".css", ".sql", ".yaml", ".yml"}

# The scan range used to be an allow-list of roots.  An allow-list can only ever claim more than it
# walks -- nine roots left 156 files of live source outside the ruler while the docstring said
# "anywhere in the shipped tree".  It is now the opposite shape: everything the tree tracks is read
# unless a name below excludes it, so the only way to shrink coverage is to state a reason, and G5
# audits the reasons.
#
# Every entry is (prefix, why it is not policed).  A frozen entry is a delivered record: rewriting
# its prose would edit the evidence rather than the product, so its citations are reported by G5
# instead of being silently unread.
FROZEN = [
    ("review/", "lane evidence drops and dated checkpoint snapshots (see G5's reported count)"),
    ("frontend/review/", "this lane's own delivered readings, kept as they were written"),
    ("evidence/", "run evidence, immutable by convention"),
    ("reports/", "generated report dumps"),
    ("evals/", "evaluation fixtures"),
    ("visio", "drawings and their spec files, not shipped source"),
    ("agent-mcp/", "MCP packaging, external to the product tree"),
    ("qoder-handoff/", "handoff bundles: a snapshot of a past tree, so its anchors are historical"),
    ("requirements/", "requirement documents"),
    ("use-cases/", "use-case documents"),
    ("domain-model", "domain model notes"),
    # Root-level status/review documents, each a dated or point-in-time report of a past tree.
    *[(n, "point-in-time report of a past tree") for n in (
        "UI_OVERNIGHT_PLAN.md", "INTEGRATION_REPORT.md", "BUG_REPORT_20260913.md",
        "_codex_review_20260912.md", "CODE_QUALITY_REPORT_20260912.md",
        "PROJECT_STATUS_SNAPSHOT_20260912.md", "PROJECT_STATUS_SNAPSHOT_20260913.md",
        "DEVELOPER_LEVEL_ASSESSMENT_20260913.md", "_coursework_gap_analysis_20260912.md",
        "_mobile_app_reuse_and_timeline_20260912.md", "HERMES_INTEGRATION_STATUS.md",
        "TEST_RESULTS.md", "CURRENT_STATUS.md", "_autopilot_finding_20260912.md",
        "HANDOFF-ROUTE-CONTACT-GATE.md", "NEXT_RECOMMENDATION.md",
        "LOCAL-APPLY-INSTRUCTIONS.md", "KNOWN_NONBLOCKING_DEBT.md",
        "AGENT_RUNTIME_ARCHITECTURE.md", "AGENT_TOOL_CATALOG.md", "BLOCKERS.md",
        "DELEGATION_POLICY.md", "PHONE_TRIAL_RUNBOOK.md")],
]


def is_frozen(rel):
    return any(rel.startswith(p) for p, _ in FROZEN)


def tracked_paths():
    import subprocess
    out = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=str(ROOT),
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        return None
    return [l for l in out.stdout.split("\n") if l.strip()]


_POLICED_CACHE = None


def policed_files():
    """Every file of a policed extension that is not frozen -- walked from the tree root, so the
    scan range cannot be narrower than the tree unless FROZEN says so by name."""
    global _POLICED_CACHE
    if _POLICED_CACHE is not None:
        return _POLICED_CACHE
    found = []
    for sub, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP and not is_frozen(
            str((Path(sub) / d).relative_to(ROOT)).replace("\\", "/") + "/")]
        rel_dir = str(Path(sub).relative_to(ROOT)).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        if is_frozen(rel_dir):
            dirs[:] = []
            continue
        for f in files:
            if os.path.splitext(f)[1] in EXTENSIONS and not is_frozen(rel_dir + f):
                found.append((Path(sub) / f, rel_dir + f))
    _POLICED_CACHE = found
    return found


def read_lines(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return None


def resolve(part, ext, caller_dir):
    joined = (part or "") + "." + ext
    for cand in (os.path.normpath(os.path.join(caller_dir, joined)),
                 os.path.normpath(os.path.join(str(ROOT / "frontend"), joined)),
                 os.path.normpath(os.path.join(str(ROOT), joined))):
        if os.path.isfile(cand):
            return Path(cand)
    tail = (part.split("/")[-1] if part else "") + "." + ext
    for _, rel in policed_files():
        if rel == tail or rel.endswith("/" + tail):
            return ROOT / rel
    return None


def test_G1_unnumbered_citations_stay_unnumbered():
    assert REGISTRY.is_file(), "the converted-citation registry is missing, so G1 has nothing to police"
    entries = [l for l in REGISTRY.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    assert len(entries) > 0, "the registry is empty"
    bad = []
    for entry in entries:
        rel, _, frag = entry.partition("	")
        lines = read_lines(ROOT / rel)
        if lines is None:
            bad.append("%s disappeared" % rel)
            continue
        hits = [i for i, l in enumerate(lines) if frag[:60] in l]
        if not hits:
            bad.append("%s no longer carries the converted comment %r, so the registry needs updating" % (rel, frag[:40]))
            continue
        for i in hits:
            m = CITE.search(lines[i])
            if m:
                bad.append("%s regained a line number: %s" % (rel, m.group(0)))
    assert not bad, "converted citations were re-numbered; a line anchor silently starts describing "                     "the wrong entry once the file moves: %s" % bad[:6]


def test_G2_no_dead_line_citation_survives_in_the_shipped_tree():
    dead = []
    for path, rel in policed_files():
        lines = read_lines(path) or []
        for i, line in enumerate(lines):
            if not COMMENT.match(line):
                continue
            for m in CITE.finditer(line):
                target = resolve(m.group(1), m.group(2), str(path.parent))
                n = int(m.group(3))
                if target is None:
                    dead.append("%s:%d cites %s which is not in the tree" % (rel, i + 1, m.group(0)))
                    continue
                tl = read_lines(target) or []
                if n > len(tl):
                    dead.append("%s:%d cites %s beyond its %d lines" % (rel, i + 1, m.group(0), len(tl)))
    assert not dead, "dead line citations are back (%d found): %s" % (len(dead), dead[:8])


# --- G3: the whole tree, in one form -------------------------------------------------------------
#
# A line-start rule is not a comment detector.  It reads a one-line block comment as code, and it never
# sees the second line of a block comment or a docstring, which is where most of the remaining
# citations were living.  These helpers find the regions themselves, so G3 can promise what G1 could
# not: no shipped comment or prose line anywhere in the scanned roots carries a line number.

GENERATED_EXEMPTION = {
    # The text here is copied out of the server's own endpoint descriptions by openapi-typescript;
    # policing it would only re-state the docstring rule, and regenerating it inside a comment-only
    # hand would sweep other lanes' pending contract bytes along with it.
    "frontend/src/generated/api.ts": "generated from contracts/openapi.yaml",
}


def _py_regions(text):
    import io as _io
    import tokenize as _tk

    lines = set()
    try:
        for tok in _tk.generate_tokens(_io.StringIO(text.replace("\r\n", "\n")).readline):
            if tok.type == _tk.COMMENT:
                lines.add(tok.start[0])
            elif tok.type == _tk.STRING and "\n" in tok.string:
                lines.update(range(tok.start[0], tok.end[0] + 1))
    except Exception:
        lines.update(i for i, line in enumerate(text.split("\n"), start=1) if line.lstrip().startswith("#"))
    return lines


def _block_regions(text):
    lines = set()
    i, n, state, quote, line_no = 0, len(text), "code", "", 1
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line_no += 1
            if state == "line":
                state = "code"
            i += 1
            continue
        if state == "code":
            if ch in "\"'`":
                quote, state = ch, "string"
            elif ch == "/" and nxt == "/":
                state, _ = "line", lines.add(line_no)
                i += 2
                continue
            elif ch == "/" and nxt == "*":
                state, _ = "block", lines.add(line_no)
                i += 2
                continue
        elif state == "string":
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                state = "code"
        elif state == "block":
            lines.add(line_no)
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
        i += 1
    return lines


def comment_regions(rel, text):
    if rel.endswith(".py"):
        return _py_regions(text)
    if rel.endswith(".sql"):
        # `--` is the only comment form here, and a file of this extension is otherwise DDL: every
        # prose line in it starts with the marker.  Without this branch the ruler reads a .sql file
        # and reports nothing from it, which is a silent blind spot rather than a clean file.
        return {i for i, line in enumerate(text.split("\n"), start=1)
                if line.lstrip().startswith("--")}
    if rel.endswith(".md"):
        return set(range(1, text.count("\n") + 2))
    if rel.endswith((".yaml", ".yml")):
        # A `description: |` block scalar is prose the traveller's screen reads; a key line is not.
        out = set()
        block_indent = None
        for i, line in enumerate(text.split("\n"), start=1):
            if line.lstrip().startswith("#"):
                out.add(i)
                continue
            stripped = line.strip()
            if block_indent is not None:
                indent = len(line) - len(line.lstrip())
                if stripped and indent >= block_indent:
                    out.add(i)
                    continue
                if stripped:
                    block_indent = None
            m = re.search(r":\s*[|>][-+]?\s*(#.*)?$", stripped)
            if m:
                block_indent = len(line) - len(line.lstrip()) + 2
        return out
    return _block_regions(text)


def test_G3_no_shipped_comment_carries_a_line_number_anywhere():
    offenders = []
    scanned = 0
    for path, rel in policed_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        scanned += 1
        if rel in GENERATED_EXEMPTION:
            continue
        marks = comment_regions(rel, text)
        for i, line in enumerate(text.split("\n"), start=1):
            if i not in marks:
                continue
            m = CITE.search(line)
            if m:
                offenders.append("%s:%d %s" % (rel, i, m.group(0)))
    assert scanned > 900, ("the scan walked only %d files; the policed set of this tree is about a "
                           "thousand, so a pass here would be a narrowed range, not a clean tree"
                           % scanned)
    assert not offenders, ("these shipped comments cite a line number, and the tree is one form -- a name "
                           "anchor: %s" % offenders[:8])


# The exemption above is only honest while the generated copy cannot carry a citation that its own
# source dropped; otherwise the one file users' types come from rots invisibly under the exemption.
DERIVED_FROM = {
    "frontend/src/generated/api.ts": "contracts/openapi.yaml",
}


def test_G4_generated_copy_is_not_staler_than_the_source_it_came_from():
    offenders = []
    scanned = 0
    for rel, src_rel in DERIVED_FROM.items():
        dst, src = ROOT / rel, ROOT / src_rel
        if not dst.is_file() or not src.is_file():
            continue
        dst_text = dst.read_text(encoding="utf-8", errors="replace")
        src_text = src.read_text(encoding="utf-8", errors="replace")
        marks = comment_regions(rel, dst_text)
        for i, line in enumerate(dst_text.split("\n"), start=1):
            if i not in marks:
                continue
            scanned += 1
            m = CITE.search(line)
            if m and m.group(0) not in src_text:
                offenders.append("%s:%d cites %s, which the file it is generated from (%s) no longer "
                                 "carries -- regenerate it, do not hand-edit it"
                                 % (rel, i, m.group(0), src_rel))
    assert scanned > 0, ("no comment line was scanned in %s, so this arm measured nothing"
                         % ", ".join(sorted(DERIVED_FROM)))
    assert not offenders, ("the generated copy is staler than its source; a reader following it lands on "
                           "a line that describes something else: %s" % offenders[:6])


# --- G5: the boundary itself ---------------------------------------------------------------------
#
# G3 can only ever be as honest as the range it walks, and a range is exactly the thing a list of
# names lets silently rot: the first generation of this guard carried a comment asserting a root had
# been added when it had not, so the tree-wide arm was blind to the OpenAPI file while claiming to
# read the tree.  Widening the range once is not a fix -- the next narrowing would be invisible for
# the same reason.  G5 therefore polices the shape of the boundary instead of its contents:
#
#   a. every file the tree tracks, of a policed extension, is either read by G3 or sits under a
#      prefix that FROZEN names and justifies -- so nothing can fall between the two sets;
#   b. every FROZEN prefix bites at least one real file -- so an exclusion cannot be a rumour, and a
#      stale one (the directory is gone) is reported rather than trusted;
#   c. every file this lane registered as converted is in the policed set, because G1's registry and
#      G3's range must describe the same tree.
#
# The frozen zone is not asserted to be citation-free: it holds delivered readings and dated reports
# whose text is the evidence itself.  What is asserted is that its extent is stated, name by name.

def _all_extensions_on_disk():
    out = []
    for sub, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP]
        rel_dir = str(Path(sub).relative_to(ROOT)).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        for f in files:
            if os.path.splitext(f)[1] in EXTENSIONS:
                out.append(rel_dir + f)
    return out


def test_G5_the_scan_boundary_is_a_statement_not_a_habit():
    policed = set(rel for _, rel in policed_files())
    every = _all_extensions_on_disk()
    frozen = [p for p in every if is_frozen(p)]

    # (a) nothing escapes both sets
    tracked = tracked_paths()
    escaped = []
    if tracked is not None:
        for p in tracked:
            if os.path.splitext(p)[1] not in EXTENSIONS:
                continue
            if p not in policed and not is_frozen(p):
                escaped.append(p)
    assert not escaped, ("these tracked files are neither policed by G3 nor excluded by a named "
                         "FROZEN prefix, so the ruler is blind to them without saying so: %s"
                         % escaped[:8])

    # (b) every exclusion bites something real
    unused = [prefix for prefix, _ in FROZEN
              if not any(q == prefix or q.startswith(prefix) for q in every)]
    assert not unused, ("these FROZEN entries exclude no file in this tree: %s -- a boundary term "
                        "that matches nothing is the same defect as the comment that claimed a scan "
                        "root had been added"
                        % unused[:6])

    # (c) the registry lives inside the policed set
    registry_files = []
    if REGISTRY.is_file():
        for l in REGISTRY.read_text(encoding="utf-8").splitlines():
            if l.strip() and not l.startswith("#"):
                registry_files.append(l.partition("\t")[0])
    outside = sorted(set(r for r in registry_files if r not in policed))
    assert not outside, ("G1 polices these registry files but G3 does not read them, so the two arms "
                         "describe different trees: %s" % outside[:8])

    # (d) the denominators: a boundary that shrinks must be argued with numbers, not silently
    assert len(policed) > 900, ("only %d files are policed; this tree's live source set is about a "
                                "thousand, so the walk has narrowed" % len(policed))
    assert len(frozen) > 1000, ("only %d files are excluded by name; the frozen evidence zones of this "
                                "tree hold thousands, so an entry above was probably deleted rather "
                                "than renamed" % len(frozen))
    assert registry_files, "the registry is empty, so check (c) would have measured nothing"


