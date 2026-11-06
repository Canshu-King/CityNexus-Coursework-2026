"""ORD-0307 · 字面量凭据的常驻启动门：十格，每格只问一条自己的问题，且只依赖自己的读数。

判据来源：包工头 494abfd7 的账本块 `### ORD-0307`（5 条要件）。射程来源：0289 的 AST 普查只管前端 fetch
腿，本单一条不碰；本单管"服务端开机那一刻，受版本控制的树与运行环境里有没有凭据"。

两档设计是量出来的，不是审美：只扫"受版本控制的树"今天得到 CLEAR（工作副本里那枚是脱敏桩，含 `redacted`），
而今天真正活着的凭据在**运行环境**里 —— 所以 `report` 档把两档都数出来，`strict` 档对两档都拒启动。
申报通道一律按 `token_fp`（内容摘要）而不是按路径：路径白名单挡不住"换个文件再放一把"。

每格都写明它**不该**被什么扰动；反突变臂（恒真/恒假/改指纹/摘掉 raise/默认改 off）各自咬哪一格，见收据。
绝不打印值：任何断言只用长度、位置与摘要；被测串在格内现场取，不硬编码进源码。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.security import credential_literals as CL

REPO = CL.REPO
BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROBE_REL = "backend/app/_ord0307_injected_probe.py"


def _scan_paths(paths: list[str]) -> list[dict]:
    return CL.find_literals(paths, REPO)


def _synthetic_token(seed: str) -> str:
    """造一枚形状为 provider key 的假凭据：值只活在测试进程内存里，断言只用它的摘要与长度。"""

    return "sk-" + hashlib.sha256(seed.encode()).hexdigest()[:28]


# ---------------------------------------------------------------- C01 扫描集合具名且可核对
def test_C01_the_scanned_set_is_named_and_actually_present() -> None:
    """C01：扫描集合必须是具名常量，且每一根都在树里、枚举出的文件数不是零。

    不该被什么扰动：任何"顺手加个 root"都会让 roots 常量与实树对不上 ⇒ 这格先红。
    """

    report = CL.scan(REPO)
    assert report["roots"] == list(CL.SCAN_ROOTS), report["roots"]
    assert report["roots_missing"] == [], report["roots_missing"]
    assert report["scanned_files"] > 100, report["scanned_files"]
    files = CL.candidate_files(REPO)
    assert len(files) == report["scanned_files"], (len(files), report["scanned_files"])
    assert all(Path(REPO / f).is_file() for f in files[:25]), files[:25]


# ---------------------------------------------------------------- C02 值绝不外泄
def test_C02_no_path_through_the_gate_leaks_a_value() -> None:
    """C02：门给出的每一段文字里都不许出现凭据本身，也不出现它的前 8 位。

    被测值现场取（合成 + 真树里那一枚），断言的是"缺席"，所以这格不会因为我引用过值而变脏。
    """

    probe_value = _synthetic_token("ord0307-c02")
    hits = [{"path": PROBE_REL, "line": 3, "pattern": "provider_key_shape", "length": len(probe_value),
             "entropy_bits_per_char": 4.0, "token_fp": CL.token_fp(probe_value),
             "position_fp": CL.position_fp(PROBE_REL, 3), "in_product_path": True,
             "declared": False, "declared_at": None}]
    err = CL.CredentialLiteralError(hits, "strict")
    surfaces = [str(err), json.dumps(err.details(), ensure_ascii=False),
                json.dumps(CL.scan(REPO), ensure_ascii=False, default=str)]
    for text in surfaces:
        assert probe_value not in text, text[:120]
        assert probe_value[:8] not in text, text[:120]
    assert CL.token_fp(probe_value) in surfaces[0]


# ---------------------------------------------------------------- C03 反件 (a)：注入必被点名
def test_C03_an_injected_literal_in_a_product_path_is_named_by_location(tmp_path) -> None:
    """C03：往产品路径塞一枚假凭据 ⇒ 门必须点名 文件:行 与摘要，并认它在产品路径内。

    这是包工头要件 3(a)。写前断言探针不存在、跑完必删并复验删除，免得给树留脏。
    """

    target = REPO / PROBE_REL
    assert not target.exists(), "探针残留：上一轮没还原，读数不可信"
    probe_value = _synthetic_token("ord0307-c03")
    body = f"SDK_KEY = \"{probe_value}\"\n"
    try:
        target.write_text(body, encoding="utf-8", newline="\n")
        hits = _scan_paths([PROBE_REL])
    finally:
        target.unlink(missing_ok=True)
    assert not target.exists()
    assert len(hits) == 1, hits  # noqa
    hit = hits[0]
    assert (hit["path"], hit["line"]) == (PROBE_REL, 1), hit
    assert hit["token_fp"] == CL.token_fp(probe_value), hit
    assert hit["in_product_path"] is True, hit
    assert hit["length"] == len(probe_value) and hit["declared"] is False, hit


# ---------------------------------------------------------------- C04 负控：占位与低熵不算（恒真臂的靶）
def test_C04_placeholders_and_low_entropy_never_fire(tmp_path) -> None:
    """C04：明显占位、低熵的串不算泄漏——而且必须**走产品入口**判，不许在测试里重算一遍过滤。

    每个样本只因一个维度被排除：占位词表 / 熵阈值。要件 3(b) 的"恒真臂只红这一格"靠的就是这个：
    臂改的是产品里的过滤器，所以只有这一格会动。上一版我把过滤逻辑抄进断言里，臂改产品码它看不见——
    那是"格自己实现了一遍产品"，已经改掉了。
    """

    hi = hashlib.sha256(b"ord0307-c04").hexdigest()
    samples = {"sk-" + hi[:12] + "redacted" + hi[12:24]: "高熵，只被占位词表挡住",
               "sk-" + "a" * 24: "形状对，只被熵阈值挡住"}
    probe_rel = "backend/app/_ord0307_c04_probe.py"
    probe = REPO / probe_rel
    assert not probe.exists(), "探针残留：上一轮没还原"
    body = "API_KEY = \"" + "\"\nAPI_KEY = \"".join(samples) + "\"\n"
    try:
        probe.write_text(body, encoding="utf-8", newline="\n")
        hits = CL.find_literals([probe_rel], REPO)
    finally:
        probe.unlink(missing_ok=True)
    assert not probe.exists()
    assert hits == [], {"不该命中的样本命中了": [(h["path"], h["line"], h["pattern"], h["length"])
                                        for h in hits], "样本数": len(samples)}
    assert len(samples) == 2, samples


# ---------------------------------------------------------------- C05 各形状各自可辨
def test_C05_each_shape_pattern_fires_on_its_own_shape() -> None:
    """C05：四类形状各造一枚，断言"被认出来"且认出的形状名符合预期（不是靠通配撞上的）。"""

    samples = {
        "provider_key_shape": f"KEY = \"{_synthetic_token('c05-provider')}\"",
        "aws_access_key": "AWS = \"AKIA" + "ABCDEFGHIJKLMNOP" + "\"",
        "github_token": "GH = \"ghp_" + hashlib.sha256(b"c05-gh").hexdigest()[:36] + "\"",
        "google_api_key": "GOOG = \"AIza" + hashlib.sha256(b"c05-ga").hexdigest()[:35] + "\"",
    }
    seen = {}
    for expected, line in samples.items():
        names = sorted({name for name, tok in CL._tokens_in(line)
                        if not any(k in tok.casefold() for k in CL.PLACEHOLDER_MARKERS)
                        and CL.entropy_bits_per_char(tok) >= CL.MIN_ENTROPY_BITS_PER_CHAR})
        seen[expected] = names
        assert expected in names, (expected, names)
    assert all(v for v in seen.values()), seen


# ---------------------------------------------------------------- C06 申报按摘要，改一个字符即作废
def test_C06_declaration_is_by_digest_not_by_path() -> None:
    """C06：D-2=甲 的申报通道 —— 吻合则记为已申报，摘要差一位就回到"未申报 ⇒ 红"。"""

    declared = CL.DECLARED_SAMPLES
    assert declared and all({"token_fp", "at", "declared_by"} <= set(d) for d in declared), declared
    fp = declared[0]["token_fp"]
    assert CL.find_literals([declared[0]["at"].rsplit(":", 1)[0]], REPO), declared[0]
    hit = _scan_paths([declared[0]["at"].rsplit(":", 1)[0]])[0]
    assert hit["token_fp"] == fp and hit["declared"] is True, hit
    mutated = fp[:-1] + ("0" if fp[-1] != "0" else "1")
    assert not any(d["token_fp"] == mutated for d in CL.DECLARED_SAMPLES), mutated
    assert CL.token_fp("anything-else") != fp


# ---------------------------------------------------------------- C07 真树正控（如实写）
def test_C07_what_the_working_tree_actually_holds_today() -> None:
    """C07（要件 4 的正控）：今天扫"受版本控制的树"得到什么，就照实断言什么。

    量到的不是"该红"：树里那两处命中是同一枚**已申报**的 e2e 假样（两条形状各命中一次），
    而 09-23 那把真 key 的位置现在是含 `redacted` 的脱敏桩 —— 被占位词表正当排除。
    所以"只扫树"的门今天必然是 CLEAR；这不叫门失效，叫射程到此为止（环境档见 C08）。
    """

    report = CL.scan(REPO)
    e2e = "frontend/e2e/qoder-e2e-config-guard.spec.ts"
    at_e2e = [h for h in report["hits"] if h["path"] == e2e]
    assert len(at_e2e) >= 1, report["hits"]
    assert {h["token_fp"] for h in at_e2e} == {CL.DECLARED_SAMPLES[0]["token_fp"]}, at_e2e
    assert all(h["declared"] for h in at_e2e), at_e2e
    assert report["hits_in_product_path"] == 0, report["hits_in_product_path"]
    # 只断言与占位词表无关的事实；"那枚真 key 现在是脱敏桩"作为读数进收据，不作为断言
    # （否则任何改动词表的反例都会顺带把这格拖红，要件 3(b) 就没法只红一格）
    stub = [h for h in report["hits"] if h["path"].startswith("qoder-handoff/")]
    assert all(not h["in_product_path"] for h in stub), stub
    # verdict 由两档合成：套件环境里 conftest 会摘掉 AI key，所以这里只核对它与自己读数一致
    expect = CL.CODE_LIVE_IN_ENVIRONMENT if report["env_undeclared"] else CL.CODE_OK
    assert report["verdict"] == expect, {"verdict": report["verdict"], "expect": expect,
                                         "env_present": report["env_present"]}
    assert report["env_present"] == len(CL.live_env_credentials()), report


# ---------------------------------------------------------------- C08 环境档：现役 key 不许申报
def test_C08_a_live_credential_in_the_environment_is_undeclared_by_construction() -> None:
    """C08：环境档只报名字、长度与摘要；`ENV_DECLARATIONS` 故意留空 ⇒ 现役凭据一律未申报。

    真环境里那一枚只作为读数记录（断言"值不在任何输出里"与"长度==实际长度"），
    判据本身用传入的合成 environ，换机器、换 key 都不会误红也不会假绿。
    """

    assert CL.ENV_DECLARATIONS == (), CL.ENV_DECLARATIONS
    probe_value = _synthetic_token("c08-synthetic")
    rows = CL.live_env_credentials({"DEEPSEEK_API_KEY": probe_value, "CITYNEXUS_AI_API_KEY": "",
                                    "CITYNEXUS_HERMES_API_KEY": "   "})
    assert len(rows) == 1, rows
    assert rows[0] == {"var": "DEEPSEEK_API_KEY", "length": len(probe_value),
                       "token_fp": CL.token_fp(probe_value), "position_fp": rows[0]["position_fp"],
                       "in_product_path": True, "declared": False, "declared_at": None}, rows
    assert probe_value not in json.dumps(rows, ensure_ascii=False), "值进了返回值"
    live = {r["var"]: r for r in CL.live_env_credentials()}
    for var, row in live.items():
        assert row["length"] == len(os.environ[var]) and not row["declared"], (var, row)


# ---------------------------------------------------------------- C09 strict 真拒 / report 真放行
def test_C09_strict_refuses_boot_and_report_does_not_in_a_real_import() -> None:
    """C09：启动语义走**子进程真 import**（0296 的 G11 形状）——双向各跑一次。

    合成环境里放一枚假凭据：strict ⇒ 拒绝启动并报具名码；report ⇒ 照常起来。
    断言的是"码在、值不在"，所以这格自己也不泄漏。
    """

    probe_value = _synthetic_token("c09-subprocess")
    base_env = {**os.environ, "CITYNEXUS_AI_ENABLED": "false", "PYTHONIOENCODING": "utf-8"}
    for requested, must_refuse in (("strict", True), ("report", False)):
        env = {**base_env, "CITYNEXUS_CREDENTIAL_GATE": requested, "DEEPSEEK_API_KEY": probe_value}
        done = subprocess.run([sys.executable, "-c", "import app.main"], cwd=str(BACKEND_ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=600)
        blob = (done.stderr or "") + (done.stdout or "")
        assert (done.returncode != 0) is must_refuse, (requested, done.returncode, blob[-400:])
        if must_refuse:
            assert "LIVE_CREDENTIAL_IN_ENVIRONMENT" in blob, blob[-400:]
            assert probe_value not in blob and probe_value[:8] not in blob, "启动拒绝里泄漏了值"
        else:
            assert "Traceback" not in blob, blob[-400:]


# ---------------------------------------------------------------- C10 默认不是 off
def test_C10_the_default_policy_is_report_not_off() -> None:
    """C10：没人配置时门必须在数东西；`off` 只能显式选，且选了就具名留痕。

    防的是"为了让大家别被拒，把默认悄悄改成 off"。
    """

    saved = os.environ.pop("CITYNEXUS_CREDENTIAL_GATE", None)
    try:
        assert CL.policy() == "report", CL.policy()
        assert CL.startup_report(REPO)["policy"] == "report"
    finally:
        if saved is not None:
            os.environ["CITYNEXUS_CREDENTIAL_GATE"] = saved
    os.environ["CITYNEXUS_CREDENTIAL_GATE"] = "off"
    try:
        report = CL.startup_report(REPO)
        assert report["verdict"] == CL.CODE_OFF and report["policy"] == "off", report
    finally:
        os.environ.pop("CITYNEXUS_CREDENTIAL_GATE", None)
    for bad in ("false", "0", "disabled"):
        os.environ["CITYNEXUS_CREDENTIAL_GATE"] = bad
        try:
            with pytest.raises(ValueError) as caught:
                CL.policy()
            assert CL.CODE_UNRECOGNISED in str(caught.value), str(caught.value)
        finally:
            os.environ.pop("CITYNEXUS_CREDENTIAL_GATE", None)
