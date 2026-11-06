"""ORD-0495 · 要读上一代源码的臂，必须分清"环境答不出"与"产品真的坏了"。

号令者给的现场：同一族格在 `git archive` 导出树里跑，H07 因为 `git show 127ce7b0…:…` 拿不到对象库
直接 rc 128 红——落地门里它绿、导出树里它红，差别只在环境。要的是两种都行：要么自己把仓库根找出来，
要么具名写 NOT_MEASURED；不许静默绿。

最危险的一种"修法"是把 128 一律当环境不可测——那会把"我引的那一代根本没有这个文件"（真缺陷）也
洗成不可测。所以这里的分界是判据，不是便利：提交能解析而路径不在 ⇒ DEFECT（仍红）；没有 git、没有
仓库根、对象库答不出这枚提交 ⇒ NOT_MEASURED（具名 skip）。G04 就是钉这条线的一格。

r3 补的是号令者在导出树里量出来的一件事：导出目录如果被放在**别人仓库的工作树里面**，从测试文件往上
走会先撞上那一枚不相干的 .git，然后拿它的对象库去答"127ce7b0 在不在"——答不出，于是"找到仓库"这件
事本身就是错的。find_repo_root 因此不只看 .git，还要看这枚仓库是不是**本项目**（PROJECT_MARKERS）；
G10 专门量这一条：同一棵树，带标记判据找不到根，去掉判据就找到邻居家。

每格自带临时目录，不依赖跑它的是仓库还是导出；G01 反过来钉"在真仓库里它照旧断言"，防止这单把臂
改成永远沉默。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qoder_git_source_r1 import (  # noqa: E402
    DEFECT, NOT_MEASURED, OK, PROJECT_MARKERS, find_repo_root, git_ancestor, git_source,
    require_git_ancestor, require_git_source)

HERE = Path(__file__).resolve()
GATE_REL = "backend/app/security/credential_literals.py"
ORDER_BASE = "127ce7b05329eaa837701cdb93bdbc2e5d64e10f"
REPO_ROOT = find_repo_root(HERE)


@pytest.fixture()
def repo() -> Path:
    """A checkout whose object store can answer, or a NAMED skip.

    This file is about telling the two worlds apart, so it has to live by the same rule: a cell that
    can only pass inside a repository must say NOT_MEASURED outside one, not go red. The commander's
    whole complaint was an export tree reading like a product defect.
    """

    if REPO_ROOT is None:
        pytest.skip("%s: no repository root above this file, so no object store can be asked"
                    % NOT_MEASURED)
    return REPO_ROOT


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- G01 正控：真仓库里它照旧取到源码
def test_G01_a_real_repository_still_yields_the_source(repo: Path) -> None:
    assert (repo / ".git").exists(), "this test is not running inside a repo"
    status, text, reason = git_source(ORDER_BASE, GATE_REL, repo)
    assert status == OK, (status, reason)
    assert "PLACEHOLDER_MARKERS" in text, len(text)
    assert require_git_source(ORDER_BASE, GATE_REL, repo) == text


# ---------------------------------------------------------------- G02 反控：没有仓库根 ⇒ 具名不可测
def test_G02_no_repository_root_is_not_measured(tmp_path: Path) -> None:
    assert find_repo_root(tmp_path / "backend" / "tests") is None
    status, text, reason = git_source(ORDER_BASE, GATE_REL, None)
    assert status == NOT_MEASURED, (status, reason)
    assert "repository root" in reason, reason
    assert text == "", "a not-measured arm must hand back no text at all"
    with pytest.raises(pytest.skip.Exception) as caught:
        require_git_source(ORDER_BASE, GATE_REL, tmp_path / "backend")
    assert str(caught.value).startswith(NOT_MEASURED), str(caught.value)


# ---------------------------------------------------------------- G03 反控：有个空 .git 也不算仓库
def test_G03_a_stray_empty_dot_git_is_not_measured(tmp_path: Path) -> None:
    """导出包里夹一枚 .git 占位（zip 里常见的残留），加上本项目该有的标记，仍算环境不可测。

    The marker test cannot save this one -- the directory does look like the project -- so what has to
    save it is the second question: can this store actually answer for the commit cited. It cannot, and
    the answer is NOT_MEASURED with git's own words, not a red and not a pass.
    """
    (tmp_path / ".git").write_text("", encoding="utf-8", newline="\n")
    for marker in PROJECT_MARKERS:
        target = tmp_path / marker
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("placeholder for the marker test\n", encoding="utf-8", newline="\n")
    root = find_repo_root(tmp_path)
    assert root == tmp_path, root
    status, text, reason = git_source(ORDER_BASE, GATE_REL, root)
    assert status == NOT_MEASURED, (status, reason)
    assert text == ""


# ---------------------------------------------------------------- G04 关键线：提交解析得到而路径没有 ⇒ 仍红
def test_G04_a_resolvable_commit_missing_the_path_is_a_defect_not_a_skip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    (repo / "said.txt").write_text("one line\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "said.txt")
    _git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "one")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head, "the fixture commit was never made -- this cell would prove nothing"

    #: the commit resolves, the path does not: this is the citation being wrong, not the environment
    status, text, reason = git_source(head, "backend/app/never_was.py", repo)
    assert status == DEFECT, (status, reason)
    assert "never_was.py" in reason, reason
    with pytest.raises(AssertionError) as caught:
        require_git_source(head, "backend/app/never_was.py", repo)
    assert "does not hold" in str(caught.value), str(caught.value)

    #: and the same arm on a path that commit really does hold, in the same repository
    assert git_source(head, "said.txt", repo)[0] == OK


# ---------------------------------------------------------------- G05 不许静默绿：三种状态互斥
def test_G05_no_state_is_silently_green(tmp_path: Path, repo: Path) -> None:
    """OK 必带字节；NOT_MEASURED 必无字节且只能以 skip 出去；DEFECT 必以断言失败出去。

    这格钉的是接口本身：任何一格把"没量到"伪装成"量到了且相等"，这里就红。
    """
    statuses = [git_source(ORDER_BASE, GATE_REL, repo)[0],
                git_source(ORDER_BASE, GATE_REL, None)[0],
                git_source("0" * 40, GATE_REL, repo)[0]]
    assert statuses[0] == OK and statuses[1] == NOT_MEASURED and statuses[2] == NOT_MEASURED, statuses
    for bad in (None, tmp_path):
        status, text, reason = git_source(ORDER_BASE, GATE_REL, bad)
        assert status == NOT_MEASURED and text == "" and reason, (status, text, reason)
    ok_status, ok_text, _r = git_source(ORDER_BASE, GATE_REL, repo)
    assert ok_status == OK and ok_text, "the readable case handed back nothing to compare"


# ---------------------------------------------------------------- G06 祖先那一臂同样两种都行
def test_G06_the_ancestry_arm_answers_or_names_itself(tmp_path: Path, repo: Path) -> None:
    root = repo
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    parent = _git(root, "rev-parse", "HEAD~1").stdout.strip()
    assert head and parent, "this checkout cannot answer for its own history"
    assert require_git_ancestor(parent, "HEAD", root) is True
    assert require_git_ancestor(head, "HEAD~1", root) is False
    status, answer, reason = git_ancestor(head, "HEAD~1", None)
    assert status == NOT_MEASURED and answer is None, (status, answer, reason)
    with pytest.raises(pytest.skip.Exception) as caught:
        require_git_ancestor(head, "HEAD~1", tmp_path)
    assert str(caught.value).startswith(NOT_MEASURED), str(caught.value)


# ---------------------------------------------------------------- G07 被改的那一格在两种世界里都不红
def test_G07_the_converted_cell_skips_in_an_export_and_asserts_in_a_repo(tmp_path: Path, repo: Path) -> None:
    """把 ORD-0479 的 H07 单独跑在两处：真树里它过，导出树里它是具名 skip——不是失败。

    "导出"在这里是**工作树里 backend/ 的一份副本，不带 .git**（号令者那侧是 git archive 导出；两者
    共同点就是对象库取不到，而这正是这单要分开的东西）。副本用工作树而不是 HEAD，是为了量到我正要
    交的那批字节，不是上一次提交的。
    """
    nodeid = "tests/test_qoder_ord0479_dictionary_key_shape.py::test_H07_the_two_forbidden_shortcuts_were_not_taken"
    repo_root = repo
    in_repo = _git(repo_root, "rev-parse", "HEAD").stdout.strip()

    export = tmp_path / "export"
    export.mkdir()
    shutil.copytree(repo_root / "backend", export / "backend",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", "node_modules"))
    assert not (export / ".git").exists(), "the copy must not carry a repository"
    copied = [p.relative_to(export) for p in (export / "backend").rglob("*") if p.is_file()]
    assert len(copied) > 100, len(copied)
    for rel in ("backend/tests/test_qoder_ord0479_dictionary_key_shape.py",
                "backend/tests/qoder_git_source_r1.py"):
        assert (export / rel).exists(), rel

    out = subprocess.run([sys.executable, "-m", "pytest", nodeid, "-q", "-rs",
                          "--no-header", "-p", "no:cacheprovider"],
                         cwd=str(export / "backend"), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    skipped = [l for l in out.stdout.splitlines() if l.startswith("SKIPPED")]
    assert out.returncode == 0, out.stdout[-900:]
    assert "1 skipped" in out.stdout, out.stdout[-500:]
    assert skipped, out.stdout[-500:]
    assert all(NOT_MEASURED in s for s in skipped), skipped

    #: and the same nodeid still asserts in the real checkout -- the portability did not mute it
    again = subprocess.run([sys.executable, "-m", "pytest", nodeid, "-q", "-rs", "--no-header",
                            "-p", "no:cacheprovider"],
                           cwd=str(repo_root / "backend"), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    assert again.returncode == 0, again.stdout[-600:]
    assert "1 passed" in again.stdout, again.stdout[-500:]
    assert "SKIPPED" not in again.stdout, again.stdout[-300:]
    assert in_repo, "the checkout could not name its own head"


# ---------------------------------------------------------------- G10 邻居的仓库不是本项目
def test_G10_a_neighbour_repository_is_not_this_project(tmp_path: Path) -> None:
    """导出目录被放在别人工作树里时，往上走会先撞上那一枚不相干的 .git（号令者量到的就是这个）。

    "找到 .git"不等于"找到本项目"：带着邻居的对象库去问 127ce7b0 在不在，答案是答不出——那既不是
    OK 也不是 DEFECT。这一格两头都钉：标记判据把邻居拒掉；而万一调用方还是把那枚根传进来，状态必须
    是 NOT_MEASURED 并带上 git 原话，绝不能被读成产品缺陷。
    """
    neighbour = tmp_path / "neighbour"
    neighbour.mkdir()
    assert _git(neighbour, "init", "-q").returncode == 0, "the fixture repository was never made"
    deep = neighbour / "unpacked" / "backend" / "tests"
    deep.mkdir(parents=True)
    probe = deep / "test_probe.py"
    probe.write_text("", encoding="utf-8", newline="\n")

    blind = find_repo_root(probe, markers=())
    assert blind == neighbour, "without the marker test the walk lands on the neighbour: {}".format(blind)
    assert find_repo_root(probe) is None, "the marker test accepted a repository that is not this project"

    status, text, reason = git_source(ORDER_BASE, GATE_REL, blind)
    assert status == NOT_MEASURED, (status, reason)
    assert text == "", "an unanswerable store must hand back nothing to compare"
    assert "cannot answer" in reason, reason
