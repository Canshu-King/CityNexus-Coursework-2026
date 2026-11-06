"""ORD-0495: arms that need a PREVIOUS generation's source, in a way that works in both worlds.

A test that runs `git show <rev>:<path>` sits in one of three situations, and telling them apart is
the whole point:

  OK           the object store answers and the commit holds the path -- the arm has a real text to
               compare against, so it asserts like it always did;
  NOT_MEASURED there is no git, or no repository above this file (a `git archive` export, a release
               zip, an unpacked artifact), or the commit is not in this object store (a shallow or
               partial clone). Nothing the arm claims is testable here, so the arm says so BY NAME and
               the run skips it. It never returns "pass" for a reading it did not take;
  DEFECT       the commit resolves and that commit does NOT hold the path. That is not an environment
               problem -- it means the coordinate the test cites has genuinely gone, and it has to
               stay red.

The old shape of this arm called `git show` and asserted returncode == 0, so an export tree produced a
product-looking failure for a fact about the checkout. Same evidence, different door: a gate that must
not be told "green" when nobody measured, and a reader who must not be told "broken" when there is
nothing to break.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

OK = "OK"
NOT_MEASURED = "NOT_MEASURED"
DEFECT = "DEFECT"

#: What makes an ancestor repository THIS project rather than whatever happens to sit above the
#: directory the tests were unpacked into. A `.git` alone is not identity: an export dropped inside a
#: neighbour's work tree walks up, finds that .git, and then reads a store that has never heard of the
#: commit the arm cites -- which is a wrong answer, not a missing one.
PROJECT_MARKERS = ("backend/app/security/credential_literals.py", "contracts/openapi.yaml")


def find_repo_root(start, markers=PROJECT_MARKERS) -> Path | None:
    """The nearest ancestor holding a .git AND every marker path -- directory, file, or worktree pointer.

    Read from the test file rather than from the module under test, because an export keeps the same
    relative layout: the answer has to be "is the project's repository around HERE", not "where does
    the product think the root is" and not "is any repository around here".
    """

    p = Path(start).resolve()
    for candidate in (p, *p.parents):
        if not (candidate / ".git").exists():
            continue
        if all((candidate / m).is_file() for m in markers):
            return candidate
    return None


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY")}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def _blame(result: subprocess.CompletedProcess) -> str:
    """Git's own first line, so a skip names what the environment actually said."""

    for stream in (result.stderr, result.stdout):
        for line in (stream or "").splitlines():
            if line.strip():
                return line.strip()
    return "git exited %d and said nothing" % result.returncode


def git_source(rev: str, rel: str, root):
    """(status, text, reason) for `rev:rel` -- see the module docstring for the three statuses."""

    if shutil.which("git") is None:
        return NOT_MEASURED, "", "no git binary on PATH"
    if root is None:
        return NOT_MEASURED, "", "no repository root above this file (an export tree holds no .git)"
    probe = _run(root, "cat-file", "-e", "%s^{commit}" % rev)
    if probe.returncode != 0:
        return NOT_MEASURED, "", "this object store cannot answer for %s (%s)" % (rev[:12], _blame(probe))
    shown = _run(root, "show", "%s:%s" % (rev, rel))
    if shown.returncode != 0:
        return DEFECT, "", "%s resolves as a commit but does not hold %s (%s)" % (
            rev[:12], rel, _blame(shown))
    return OK, shown.stdout, ""


def require_git_source(rev: str, rel: str, root) -> str:
    """The text the arm needs, or a NAMED skip. A defect is raised, not skipped.

    "Silently green" is the failure mode this exists to close: an unreadable previous generation must
    not be able to satisfy a byte-for-byte comparison by being empty, so the skip is the only way out
    and it carries NOT_MEASURED plus the reason in its own name.
    """

    status, text, reason = git_source(rev, rel, root)
    if status == NOT_MEASURED:
        pytest.skip("%s: cannot read %s:%s -- %s" % (NOT_MEASURED, rev[:12], rel, reason))
    if status == DEFECT:
        raise AssertionError(reason)
    assert text, "git reported %s:%s as readable but handed back no bytes" % (rev[:12], rel)
    return text


def git_ancestor(rev: str, ref: str, root):
    """(status, is_ancestor, reason) for "is rev an ancestor of ref" -- same three worlds."""

    if shutil.which("git") is None:
        return NOT_MEASURED, None, "no git binary on PATH"
    if root is None:
        return NOT_MEASURED, None, "no repository root above this file (an export tree holds no .git)"
    for what, args in ((rev, ("cat-file", "-e", "%s^{commit}" % rev)),
                       (ref, ("rev-parse", "--verify", ref))):
        probe = _run(root, *args)
        if probe.returncode != 0:
            return NOT_MEASURED, None, "this object store cannot answer for %s (%s)" % (
                what[:12], _blame(probe))
    check = _run(root, "merge-base", "--is-ancestor", rev, ref)
    if check.returncode == 0:
        return OK, True, ""
    if check.returncode == 1:
        return OK, False, ""
    return DEFECT, None, "git could not decide the ancestry (%s)" % _blame(check)


def require_git_ancestor(rev: str, ref: str, root) -> bool:
    """True/False where an object store can answer; a NAMED skip where none can."""

    status, answer, reason = git_ancestor(rev, ref, root)
    if status == NOT_MEASURED:
        pytest.skip("%s: cannot order %s against %s -- %s" % (NOT_MEASURED, rev[:12], ref, reason))
    if status == DEFECT:
        raise AssertionError(reason)
    return bool(answer)
