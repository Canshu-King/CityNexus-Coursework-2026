"""ORD-0479 · 凭据尺不许把 i18n 查表的值读成凭据——判据是结构，不是词表、也不是申报。

号令者那块点名的红在 backend/tests/test_qoder_ord0307_credential_gate.py（C07，断
hits_in_product_path == 0 得 1），命中的是 frontend/src/features/shared/SharedJourneyPanel.tsx
那张"拒绝码 ↔ 字典键"对照表里的值。要求写得很死：要结构判据，不要往占位词表塞词，也不要走申报通道
（那等于把尺的假阳性登记成真凭据）。

重取到 aa2a08ce 那一代时，同一族又多出一处（features/poster/TripPosterPanel.tsx 的失败步骤 ↔ 字典键
表，标签 MissingTokens 里含着 token）——结构判据不需要为它改一个字，两处都靠同三条赦免。所以本文件
不写"赦免了几处"这种会随上游移动的数字；真树那一格改成把尺报出来的每一处**脱离尺再证一遍**。

每格自己造输入（临时 root，自己的词典、自己的表），断言只用位置摘要与长度，绝不打印命中值；
正控与反控成对：真凭据形状仍红、形状像但结构不符仍红、词典读不到仍红。
H07 那一格反过来钉"两条禁路没被走"：把词表与申报表跟基座尖上的版本逐字节比。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.security import credential_literals as CL  # noqa: E402
from qoder_git_source_r1 import (  # noqa: E402
    find_repo_root, require_git_ancestor, require_git_source)

REPO = CL.REPO
#: ORD-0495: the repository this arm needs is found from THIS file upward, not from the product's own
#: idea of the root. In a `git archive` export the layout is identical and the .git is simply absent,
#: so the honest answer there is "the object store cannot be asked", not "the product is broken".
GIT_ROOT = find_repo_root(Path(__file__))
#: The generation the order was written against -- a fixed commit, not whichever tip this branch
#: happens to sit on, because the tip moves while an order is in flight and a pin that chases it is
#: a pin that gets re-edited instead of a fact that gets checked. H07 compares the two forbidden
#: channels against this generation and requires it to be an ancestor of HEAD, so the comparison is
#: against the vocabulary as it stood before this order and cannot silently rot into self-reference.
ORDER_BASE = "127ce7b05329eaa837701cdb93bdbc2e5d64e10f"
GATE_REL = "backend/app/security/credential_literals.py"
SITE = "frontend/src/features/shared/SharedJourneyPanel.tsx"
VALUE = "shared.refusedTokenUnknown"


def _write(root: Path, rel: str, text: str) -> str:
    dst = root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8", newline="\n")
    return rel


def _dict(tmp: Path, en: list[str], zh: list[str]) -> str:
    body = ("const en: Dict = {\n"
            + "".join('  "%s": "english %s",\n' % (k, k) for k in en)
            + "};\n\nconst zhCN: Dict = {\n"
            + "".join('  "%s": "中文 %s",\n' % (k, k) for k in zh)
            + "};\n")
    return _write(tmp, CL.I18N_SOURCE_REL, body)


def _table(tmp: Path, values: list[tuple[str, str]]) -> str:
    body = "const REFUSAL_KEY: Record<string, string> = {\n" + "".join(
        '  %s: "%s",\n' % (k, v) for k, v in values) + "};\n"
    return _write(tmp, "frontend/src/features/shared/panel.ts", body)


def _synthetic_key(seed: str) -> str:
    return "sk-" + hashlib.sha256(seed.encode()).hexdigest()[:28]


@pytest.fixture()
def tmp_root(tmp_path: Path) -> Path:
    """A dictionary with four translated keys; the tables below use three of them.

    The counts are deliberately unequal so a receipt that reports the wrong one cannot pass:
    `translation_lookup_table:3 entries, dictionary:4 keys` names the table and the vocabulary.
    """

    _dict(tmp_path, [VALUE, "shared.refusedRevoked", "shared.refusedExpired",
                     "shared.refusedUnused"],
          [VALUE, "shared.refusedRevoked", "shared.refusedExpired", "shared.refusedUnused"])
    return tmp_path


# ---------------------------------------------------------------- H01 真那一处：被结构赦免，且 C07 的断言成立
def test_H01_the_real_site_is_exempted_by_structure_not_by_a_word(tmp_root: Path) -> None:
    """整表都是"凭据形状的标签 ↔ 两本词典都认得的键"，三处命中全靠结构赦免。

    标签逐枚都带 token，是因为尺只看凭据形状的标签；把其中一枚换成 revoked 就只剩一命中，
    那一格就不再证明"整表都是键"这条判据了。
    """

    rel = _table(tmp_root, [("token_unknown", VALUE), ("revoked_token", "shared.refusedRevoked"),
                            ("expired_token", "shared.refusedExpired")])
    hits = CL.find_literals([rel], tmp_root)
    assert len(hits) == 3, hits
    for h in hits:
        assert h["pattern"] == "labeled_assignment", h
        assert h["in_product_path"] is True, h
        assert h["vocabulary_exemption"] == "translation_lookup_table:3 entries, dictionary:4 keys", h
    report = CL.scan(tmp_root)
    assert report["hits_in_product_path"] == 0, report["live"]
    assert report["verdict"] == CL.CODE_OK, report["verdict"]
    assert report["hits_vocabulary_exempted"] == 3, report["vocabulary_exemptions"]
    assert {e["position_fp"] for e in report["vocabulary_exemptions"]} == \
        {h["position_fp"] for h in hits}, report["vocabulary_exemptions"]


def _independent_locale_keys() -> tuple[set[str], set[str]]:
    """The two dictionaries read a second time, without touching any helper in the gate.

    The point is not a faster parse: it is that a claim about what the product translates is checked
    against the file rather than against the code that also grants the exemption.
    """

    lines = (REPO / "frontend/src/i18n/LocaleContext.tsx").read_text(
        encoding="utf-8", errors="replace").splitlines()
    per: dict[str, set[str]] = {}
    for name in ("en", "zhCN"):
        head = [i for i, l in enumerate(lines) if l.startswith("const %s: Dict = {" % name)]
        assert len(head) == 1, (name, head)
        keys = set()
        for line in lines[head[0] + 1:]:
            if line.startswith("};"):
                break
            m = re.match(r'^\s*"([^"]+)"\s*:\s*"', line)
            if m:
                keys.add(m.group(1))
        assert keys, name
        per[name] = keys
    return per["en"], per["zhCN"]


def _table_values_around(path: str, line_no: int) -> list[str]:
    """Every quoted value in the object literal the given line sits in, read straight from the file."""

    lines = (REPO / path).read_text(encoding="utf-8", errors="replace").splitlines()
    start = None
    for i in range(line_no - 1, -1, -1):
        if lines[i].lstrip().startswith(("const ", "export const ")) and lines[i].rstrip().endswith("{"):
            start = i
            break
    assert start is not None, (path, line_no)
    values = []
    for line in lines[start + 1:]:
        if line.startswith("};"):
            break
        m = re.match(r'^\s*[A-Za-z_][A-Za-z0-9_]*\s*:\s*"([^"]+)"\s*,?\s*$', line)
        if m:
            values.append(m.group(1))
    assert len(values) >= 2, (path, start + 1, values)
    return values


def _table_line_inside_named_const(path: str, symbol: str, value: str) -> int:
    """The line where `value` sits inside the object literal opened by the named const `symbol`.

    ORD-0479's own ruling is that a site is identified by what it *is*, not by where it happens to sit:
    this order was opened against line 45, and the line is 47 today because ORD-0528 exported the table.
    Anchoring on (const name, dictionary key) keeps both directions working -- the reading survives any
    amount of upstream movement, and renaming either symbol still fails the arm.
    """

    lines = (REPO / path).read_text(encoding="utf-8", errors="replace").splitlines()
    opener = re.compile(r"\b(?:export\s+)?const\s+%s\b" % re.escape(symbol))
    start = None
    for i, line in enumerate(lines, 1):
        if opener.search(line) and line.rstrip().endswith("{"):
            start = i
            break
    assert start is not None, (path, symbol, "the named const is not in the file any more")
    entry = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\s*:\s*"%s"\s*,?$' % re.escape(value))
    for i in range(start, len(lines)):
        if lines[i].startswith("};"):
            break
        if entry.match(lines[i].strip()):
            return i + 1
    raise AssertionError((path, symbol, value, "the named key is not in that table any more"))


def test_H01b_the_shipped_tree_clears_and_every_exemption_is_re_derivable() -> None:
    """真树上：C07 那句断言成立；而且每一处被赦免的行，都能脱离尺自己再证一遍。

    这一格不数"赦免了几处"——那是会随上游移动的量——它把尺报出来的每一处拿去独立核对：值必须在
    两本词典里都作为键出现，且它所在的那张表每一条值都是两本词典里的键。对不上就红。
    """

    report = CL.scan(REPO)
    assert report["hits_in_product_path"] == 0, report["live"]
    assert report["verdict"] == CL.CODE_OK, report["verdict"]
    assert report["hits_undeclared"] == 0, report["live"]
    en, zh = _independent_locale_keys()
    both = en & zh
    exemptions = report["vocabulary_exemptions"]
    assert exemptions, "the tree is clear with nothing forgiven -- then C07 was already green and this order moved"
    for e in exemptions:
        assert "translation_lookup_table" in e["reason"], e
        line_text = (REPO / e["path"]).read_text(encoding="utf-8", errors="replace").splitlines()[e["line"] - 1]
        m = re.search(r':\s*"([^"]+)"', line_text)
        assert m, e
        value = m.group(1)
        assert value in both, (e["path"], e["line"], "not a key in both locales")
        for v in _table_values_around(e["path"], e["line"]):
            assert v in both, (e["path"], e["line"], "a table neighbour is not a translated key")
    #: the site this order was opened for is among them, and is forgiven on the same grounds.
    #: It is found by the two symbols that make it what it is - the exported table and the dictionary key
    #: - never by a row number, because the row moves under the tree while the site stays the same site.
    site_line = _table_line_inside_named_const(SITE, "REFUSAL_KEY", VALUE)
    assert (SITE, site_line) in [(e["path"], e["line"]) for e in exemptions], exemptions


# ---------------------------------------------------------------- H02 正控：真凭据形状仍红
def test_H02_a_real_credential_shape_on_the_same_label_stays_live(tmp_root: Path) -> None:
    rel = _write(tmp_root, "frontend/src/features/shared/positive.ts",
                 'const cfg = { token_unknown: "%s" };\n' % _synthetic_key("ord0479-positive-control"))
    hits = CL.find_literals([rel], tmp_root)
    assert len(hits) == 2, hits  # 同一行被 provider_key_shape 与 labeled_assignment 各抓一次
    for h in hits:
        assert h["vocabulary_exemption"] is None, h
    report = CL.scan(tmp_root)
    assert report["hits_in_product_path"] == len(hits) == report["live_total"], report["live"]
    assert report["verdict"] == CL.CODE_IN_PRODUCT_PATH, report["verdict"]


# ---------------------------------------------------------------- H03 反例：形状像、词典里没有
def test_H03_a_dotted_camel_value_that_no_locale_translates_stays_live(tmp_root: Path) -> None:
    rel = _table(tmp_root, [("token_unknown", "shared.refusedMadeUpKey"),
                            ("revoked_token", "shared.refusedRevoked"),
                            ("expired_token", "shared.refusedExpired")])
    report = CL.scan(tmp_root)
    assert report["hits_in_product_path"] >= 1, report["live"]
    assert report["verdict"] == CL.CODE_IN_PRODUCT_PATH, report["verdict"]


# ---------------------------------------------------------------- H04 反例：只有一本词典认得它
def test_H04_a_key_present_in_one_locale_only_does_not_exempt(tmp_root: Path) -> None:
    _write(tmp_root, CL.I18N_SOURCE_REL,
           'const en: Dict = {\n  "shared.refusedOneSided": "only english",\n};\n\n'
           'const zhCN: Dict = {\n  "shared.refusedRevoked": "中文",\n'
           '  "shared.refusedExpired": "中文",\n};\n')
    rel = _write(tmp_root, "frontend/src/features/shared/one_sided.ts",
                 'const T: Record<string, string> = {\n  revoked_token: "shared.refusedOneSided",\n'
                 '  expired_token: "shared.refusedOneSided",\n};\n')
    hits = CL.find_literals([rel], tmp_root)
    assert len(hits) == 2, hits
    for h in hits:
        assert h["vocabulary_exemption"] is None, h
    assert CL.scan(tmp_root)["verdict"] == CL.CODE_IN_PRODUCT_PATH


# ---------------------------------------------------------------- H05 反例：表里混进非键值
def test_H05_a_table_mixed_with_a_non_key_value_does_not_exempt(tmp_root: Path) -> None:
    rel = _table(tmp_root, [("token_unknown", VALUE),
                            ("revoked_token", _synthetic_key("ord0479-mixed-table"))])
    hits = CL.find_literals([rel], tmp_root)
    assert hits, "nothing was probed -- the fixture does not exercise the rule"
    for h in hits:
        assert h["vocabulary_exemption"] is None, h
    assert CL.scan(tmp_root)["verdict"] == CL.CODE_IN_PRODUCT_PATH


# ---------------------------------------------------------------- H06 反例：provider 形状借词典也进不来
def test_H06_a_provider_shaped_value_cannot_hide_inside_a_translated_table(tmp_root: Path) -> None:
    """把真凭据形状写进两本词典、再放进查表里——赦免仍然一次都不给。

    结构判据要求整表的值都是字典键形状，而 DICT_KEY_SHAPE 不认 provider 形状（见 H09），
    所以这一格同时钉两条：混进一枚就整表不赦免，且 provider 图案永远不被赦免。
    """

    key = _synthetic_key("ord0479-smuggled")
    _dict(tmp_root, [VALUE, "shared.refusedRevoked", key], [VALUE, "shared.refusedRevoked", key])
    rel = _write(tmp_root, "frontend/src/features/shared/smuggled.ts",
                 'const T: Record<string, string> = {\n  token_unknown: "%s",\n'
                 '  revoked_token: "%s",\n};\n' % (VALUE, key))
    hits = CL.find_literals([rel], tmp_root)
    assert len(hits) == 3, hits  # 两行 labeled_assignment + provider 形状那一行再被抓一次
    assert [h for h in hits if h["pattern"] in CL.NEVER_EXEMPT_PATTERNS], [h["pattern"] for h in hits]
    for h in hits:
        assert h["vocabulary_exemption"] is None, h
    assert CL.scan(tmp_root)["verdict"] == CL.CODE_IN_PRODUCT_PATH


# ---------------------------------------------------------------- H07 两条禁路没被走
def test_H07_the_two_forbidden_shortcuts_were_not_taken() -> None:
    """词表与申报表都必须与基座那一代逐字节相同；in_product_path 的算法也一样。

    这格是反"洗绿"的：如果谁把那一处假阳性塞进 PLACEHOLDER_MARKERS，或者用 DECLARED_SAMPLES 给它
    登记一个指纹，这里立刻红。被比的"现在"这一侧读的是**工作树里那份尺**（也就是真在跑的那份），
    不是 HEAD，所以改动一落盘就红，不必等提交。基座只钉令上点名那一枚（固定的），并用 git 现查它
    在本枝的祖先里——钉"我这次量到哪尖"会随上游一路改常量，那不是判据。
    """

    def tuple_from(text: str, name: str) -> str:
        m = re.search(r"^%s: tuple\[[^\]]*\] = \((.*?)\)\n" % name, text, re.M | re.S)
        assert m, "the %s tuple is no longer shaped like the one this cell pins" % name
        return m.group(1)

    cur = (Path(REPO) / GATE_REL).read_text(encoding="utf-8", errors="replace")
    assert cur, "the working-tree gate file read back empty -- this cell would pass on nothing"
    #: ORD-0495: a previous generation is read through the portable arm. Where no object store can
    #: answer, this names NOT_MEASURED and the run skips; it never returns an empty text that would
    #: satisfy the comparison below by being nothing.
    base = require_git_source(ORDER_BASE, GATE_REL, GIT_ROOT)
    for name in ("PLACEHOLDER_MARKERS", "DECLARED_SAMPLES"):
        assert tuple_from(cur, name) == tuple_from(base, name), \
            ("%s moved against the order's base: neither the placeholder list nor the declaration "
             "channel may carry this order's exemption" % name)
    assert 'rp.startswith(PRODUCT_ROOTS)' in cur, "the product-path rule was rewritten"
    #: the pin has to sit behind this branch, or the comparison is with a generation this order never
    #: stood on; read it from git where git can answer, and name the case where it cannot
    assert require_git_ancestor(ORDER_BASE, "HEAD", GIT_ROOT), \
        "the order's base %s is not an ancestor of HEAD" % ORDER_BASE[:12]


# ---------------------------------------------------------------- H08 词典读不到时朝闭
def test_H08_an_unreadable_dictionary_exempts_nothing(tmp_path: Path) -> None:
    """没有词典（或它解析不出两块）⇒ 赦免必须为 0，那一处回到 live。"""

    rel = _table(tmp_path, [("token_unknown", VALUE), ("revoked_token", "shared.refusedRevoked"),
                            ("expired_token", "shared.refusedExpired")])
    assert CL.i18n_dictionary_keys(tmp_path) == frozenset()
    hits = CL.find_literals([rel], tmp_path)
    assert hits and all(h["vocabulary_exemption"] is None for h in hits), hits
    report = CL.scan(tmp_path)
    assert report["hits_in_product_path"] == len(hits), report["live"]
    assert report["hits_vocabulary_exempted"] == 0, report["vocabulary_exemptions"]


# ---------------------------------------------------------------- H09 结构判据够不到 provider 形状
def test_H09_no_provider_shape_is_shaped_like_a_dictionary_key() -> None:
    """五把 provider 图案各自的样本，全都不满足 DICT_KEY_SHAPE —— 赦免通道对它们结构性关闭。"""

    samples = {"provider_key_shape": "sk-" + "a" * 24,
               "aws_access_key": "AKIA" + "A" * 16,
               "github_token": "ghp_" + "a" * 40,
               "google_api_key": "AIza" + "a" * 35,
               "bearer_or_basic_literal": "Bearer " + "a" * 30}
    for name, sample in samples.items():
        assert name in CL.NEVER_EXEMPT_PATTERNS, name
        assert not CL.DICT_KEY_SHAPE.match(sample), name
