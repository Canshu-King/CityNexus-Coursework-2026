"""ORD-0467 · 契约是那族具名拒绝码的唯一真相，凭据尺只按它逐字节排除，并且双向自证。

背景（号令者 15:07 那块第三节）：现尖真执行套件里 `test_qoder_ord04307_credential_gate.py`
（C07 那格）读到产品路径内一枚命中，位置 `frontend/src/features/pending/pendingPlaceWrites.ts`。
那是一枚具名拒绝码，被放在一个叫 `token` 的字段上，所以 labeled_assignment 的形状判据抓它没有错。
本单不许的路也被同一批格子钉住：不给 `in_product_path` 加豁免词表、不把 ORD-0307 的断言改小、
不只改屏上文案而留着字段名说谎。

每一格都自己造输入（临时 root + 现场取的合成串），断言只用位置摘要与长度，绝不打印命中值。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.security import credential_literals as CL  # noqa: E402

REPO = CL.REPO
CODE_RE = re.compile(r"\bV13(?:_[A-Z0-9]+)+\b")
#: the labels the shape probe treats as credential-bearing; a family code must not sit on one of these
CREDENTIAL_LABEL = re.compile(r"(?i)\b(api_?key|secret|token|password|passwd|access_?key)\w*\s*[:=]")


def _tree_codes() -> set[str]:
    """The vocabulary as the sources actually write it, byte for byte."""

    out: set[str] = set()
    for root in ("backend/app", "frontend/src"):
        for p in sorted((REPO / root).rglob("*")):
            if p.is_file() and p.suffix in (".py", ".ts", ".tsx"):
                out |= set(CODE_RE.findall(p.read_text(encoding="utf-8", errors="replace")))
    return out


def _contract_enum() -> list[str]:
    spec = yaml.safe_load((REPO / CL.CONTRACT_SPEC_REL).read_text(encoding="utf-8"))
    return spec["components"]["schemas"]["V13NamedRejection"]["enum"]


def _synthetic_named(code: str) -> str:
    return 'export const refusal = { token: "%s" };\n' % code


def _synthetic_key() -> str:
    return "sk-" + hashlib.sha256(b"ord0467-positive-control").hexdigest()[:28]


@pytest.fixture()
def tmp_root(tmp_path: Path) -> Path:
    (tmp_path / CL.CONTRACT_SPEC_REL).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / CL.CONTRACT_SPEC_REL, tmp_path / CL.CONTRACT_SPEC_REL)
    return tmp_path


# ------------------------------------------------------------------ F01 契约里有这族码，且与源码逐字节相同
def test_F01_the_contract_names_the_family_and_matches_the_emitters() -> None:
    """F01：枚举必须存在、非空，且与 backend/app 与 frontend/src 实写的码逐字节相同。

    契约少了成员、或写了个源码里不存在的名字（例如把带数字的名字截断），这一格就红。
    """

    enum = _contract_enum()
    tree = _tree_codes()
    assert enum, "components.schemas.V13NamedRejection.enum is empty"
    assert len(enum) == len(set(enum)), "duplicate members in the contract enum"
    assert set(enum) == tree, {"only_in_contract": sorted(set(enum) - tree),
                               "only_in_tree": sorted(tree - set(enum))}
    for value in enum:
        assert CL.NAMED_CODE_SHAPE.match(value), value


# ------------------------------------------------------------------ F02 真件：契约赦免的是"形状+成员"两件事都成立
def test_F02_a_code_of_the_family_on_a_credential_label_is_exempted_and_stays_visible(tmp_root: Path) -> None:
    """F02：把 ORD-0467 修之前那种写法（`token:` 上放一枚家族码）放进产品路径，它必须被契约赦免、
    仍然出现在 hits 里（看得见是哪一处赦免的），并且不进 live。

    合成而不是读真实那一行，是因为本单第二笔就把那个字段改叫 `rejection_code` 了；F08 钉那一边。
    """

    rel = "frontend/src/features/pending/ord0467_lying_shape.ts"
    (tmp_root / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_root / rel).write_text(_synthetic_named("V13_DISCOVERY_COMPOSE_EMPTY"), encoding="utf-8")
    hits = CL.find_literals([rel], tmp_root)
    assert len(hits) == 1, hits
    hit = hits[0]
    assert hit["in_product_path"] is True, hit
    assert hit["pattern"] == "labeled_assignment", hit
    assert hit["contract_named"] == "V13NamedRejection", hit
    report = CL.scan(tmp_root)
    assert report["hits_contract_named"] == 1, report
    assert report["live_total"] == 0, report["live_total"]


# ------------------------------------------------------------------ F03 反件（摘掉契约）：同一行立刻回红
def test_F03_removing_the_enum_from_the_contract_makes_the_same_line_live_again(tmp_root: Path) -> None:
    """F03：只在契约里删掉那枚 enum，其余一字不动 ⇒ 同一处必须回到 live。

    这证明赦免的来源是契约文件本身，不是代码里的某张表。
    """

    spec = yaml.safe_load((tmp_root / CL.CONTRACT_SPEC_REL).read_text(encoding="utf-8"))
    spec["components"]["schemas"].pop("V13NamedRejection")
    (tmp_root / CL.CONTRACT_SPEC_REL).write_text(yaml.safe_dump(spec, sort_keys=False, width=200),
                                                 encoding="utf-8")
    rel = "frontend/src/features/pending/ord0467_lying_shape.ts"
    (tmp_root / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_root / rel).write_text(_synthetic_named("V13_DISCOVERY_COMPOSE_EMPTY"), encoding="utf-8")
    hits = CL.find_literals([rel], tmp_root)
    assert hits and hits[0]["contract_named"] is None, hits
    report = CL.scan(tmp_root)
    assert report["hits_contract_named"] == 0, report
    assert report["hits_in_product_path"] == 1, report["hits_in_product_path"]
    assert report["verdict"] == CL.CODE_IN_PRODUCT_PATH, report["verdict"]


# ------------------------------------------------------------------ F04 反例（形状像、契约里没有）
def test_F04_a_v13_looking_code_that_the_contract_does_not_name_stays_red(tmp_root: Path) -> None:
    """F04：一枚长得像这族、但契约里没有的串，必须仍然红——排除不是前缀豁免。"""

    rel = "frontend/src/features/pending/ord0467_negative.ts"
    (tmp_root / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_root / rel).write_text(_synthetic_named("V13_NOT_IN_ANY_CONTRACT_ZULU"), encoding="utf-8")
    report = CL.scan(tmp_root)
    assert report["hits_contract_named"] == 0, report
    assert report["hits_in_product_path"] == 1, report["live"]
    assert report["verdict"] == CL.CODE_IN_PRODUCT_PATH, report["verdict"]


# ------------------------------------------------------------------ F05 反例（真凭据形状）
def test_F05_a_real_credential_shape_on_the_same_field_stays_red(tmp_root: Path) -> None:
    """F05：同一枚字段名换成真凭据形状，必须仍然红——这是"门还有牙"的正控。"""

    rel = "frontend/src/features/pending/ord0467_positive.ts"
    (tmp_root / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_root / rel).write_text('const cfg = { token: "%s" };\n' % _synthetic_key(), encoding="utf-8")
    report = CL.scan(tmp_root)
    assert report["hits_contract_named"] == 0, report
    assert report["hits_in_product_path"] >= 1, report["live"]
    assert report["verdict"] == CL.CODE_IN_PRODUCT_PATH, report["verdict"]


# ------------------------------------------------------------------ F06 反洗钱：把真 key 塞进契约也不赦免
def test_F06_a_provider_shaped_value_cannot_be_launders_through_the_contract(tmp_root: Path) -> None:
    """F06：就算有人把一把真 key 的形状写进契约枚举，provider 形状那五判据也永不豁免。

    这是"豁免词表洗绿"那条禁路的正面对撞：来源合法（契约）也不够，形状说了算。
    """

    key = _synthetic_key()
    spec = yaml.safe_load((tmp_root / CL.CONTRACT_SPEC_REL).read_text(encoding="utf-8"))
    spec["components"]["schemas"]["V13NamedRejection"]["enum"].append(key)
    (tmp_root / CL.CONTRACT_SPEC_REL).write_text(yaml.safe_dump(spec, sort_keys=False, width=200),
                                                 encoding="utf-8")
    rel = "frontend/src/features/pending/ord0467_launder.ts"
    (tmp_root / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_root / rel).write_text('const cfg = { token: "%s" };\n' % key, encoding="utf-8")
    hits = CL.find_literals([rel], tmp_root)
    assert hits, "the synthetic key produced no hit at all -- the shape probe is broken"
    for hit in hits:
        if hit["pattern"] in CL.NEVER_EXEMPT_PATTERNS:
            assert hit["contract_named"] is None, hit
    report = CL.scan(tmp_root)
    assert report["live_total"] >= 1, report["live_total"]


# ------------------------------------------------------------------ F07 判据没被改小：产品路径分类不看契约
def test_F07_the_product_path_rule_still_only_looks_at_the_path(tmp_root: Path) -> None:
    """F07：`in_product_path` 仍只由路径前缀决定，契约成员不改它。

    把同一行放在产品路径外（证人侧）与产品路径内各一次：两边 contract_named 相同，
    in_product_path 不同 ⇒ 排除发生在"是不是活的"这一步，不在"算不算产品路径"那一步。
    """

    text = _synthetic_named("V13_DISCOVERY_COMPOSE_EMPTY")
    inside = "frontend/src/features/pending/ord0467_inside.ts"
    outside = "review/ord0467/inside_copy.ts"
    for rel in (inside, outside):
        (tmp_root / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_root / rel).write_text(text, encoding="utf-8")
    hits = CL.find_literals([inside, outside], tmp_root)
    assert len(hits) == 2, hits
    by_path = {h["path"]: h for h in hits}
    assert by_path[inside]["in_product_path"] is True, by_path[inside]
    assert by_path[outside]["in_product_path"] is False, by_path[outside]
    assert by_path[inside]["contract_named"] == by_path[outside]["contract_named"] == "V13NamedRejection"


# ------------------------------------------------------------------ F08 真树：不再需要赦免，因为字段名说了实话
def test_F08_the_real_tree_clears_without_needing_any_exemption() -> None:
    """F08：真实树现在必须 0 处需要契约赦免，且家族码不再落在凭据类标签上。

    这条断的是状态，不是词：`contract_named` 为空说明没有一枚码还顶着 `token:` 这类标签；
    若有人把字段名改回去，这里立刻红（同时 F02/F03 那两格仍在证明赦免机制本身有牙）。
    """

    report = CL.scan(REPO)
    assert report["hits_contract_named"] == 0, report["contract_named_positions"]
    #: the scope of ORD-0467 is this family. A product-path hit that is **not** one of these codes is
    #: another order's line, and the cell that judges the whole tree is ORD-0307's C07 -- this file
    #: must not duplicate that global assertion, because then a landing by another lane turns a cell
    #: about my vocabulary red. What stays hard here is the bound I do own: no family code may be live.
    members = set(_contract_enum())
    enum_m = re.compile(r"[\"']([A-Z0-9_]+)[\"']")
    foreign = []
    for hit in report["live"]:
        if not hit["in_product_path"]:
            continue
        line = (REPO / hit["path"]).read_text(encoding="utf-8", errors="replace").splitlines()[hit["line"] - 1]
        named = [v for v in enum_m.findall(line) if v in members]
        assert not named, {"family code still live on a credential label": hit, "line": line.strip()[:120]}
        foreign.append((hit["path"], hit["line"], hit["pattern"]))
    print("product-path hits outside this family (belong to other orders, judged by C07): %s" % foreign)
    lying = []
    label = re.compile(r"(?i)(api_?key|secret|token|password|passwd|access_?key)\w*\s*[:=]")
    for root in ("backend/app", "frontend/src"):
        for p in sorted((REPO / root).rglob("*")):
            if not p.is_file() or p.suffix not in (".py", ".ts", ".tsx"):
                continue
            for no, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if CODE_RE.search(line) and label.search(line):
                    lying.append((str(p.relative_to(REPO)), no))
    assert lying == [], lying
