"""ORD-0094 §17C - the ordinary backend suite may not depend on live providers.

Commander's 13:4x addendum, the easiest of the three to lose: 900+ backend tests must stay on
deterministic fixtures, with live verification split into an **opt-in smoke** that records
environment, egress and duration. The reason is not tidiness - a suite that reaches the network
turns into a storm of false reds the first time a provider hiccups, and false reds get
re-earned by loosening assertions.

Two of these doors are today-green *guards* (they protect a property that holds now) and one
is today-red-by-design in the sense that it proves the opt-in actually refuses. None of them
uses `skip`: an opted-out live test that reports "passed" would be the exact lie this file is
written to prevent, so the live runner must **fail loudly** when the flag is absent.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = TESTS_ROOT.parent
REPO_ROOT = BACKEND_ROOT.parent

# 宿主清单抄自 app/integrations/**（实测），不是凭印象列的。
LIVE_HOSTS = (
    "photon.komoot.io",
    "overpass-api.de",
    "overpass.private.coffee",
    "overpass.kumi.systems",
    "nominatim.openstreetmap.org",
    "restapi.amap.com",
    "api.open-meteo.com",
    "api.deepseek.com",
)

LIVE_SMOKE = BACKEND_ROOT / "live_checks" / "test_anywhere_live_smoke.py"
LIVE_FLAG = "CITYNEXUS_LIVE_PROVIDER_SMOKE"

# 一个 `https://host` 字面量和一个调用出现在同一行，才算"这条测试会真出网"。
CALL_NEAR_HOST = re.compile(
    r"(httpx|requests|urlopen|aiohttp|\.get\(|\.post\(|AsyncHTTPTransport|HTTPTransport)"
)


def _python() -> str:
    return sys.executable


def _live_call_offenders() -> list[str]:
    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if "live_checks" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if not any(host in line for host in LIVE_HOSTS):
                continue
            if CALL_NEAR_HOST.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:110]}")
    return offenders


def test_g8a_default_suite_contains_no_live_provider_calls():
    """§17C: 常规套件里不得有真出网的调用。今天绿 = 它是护栏，不是成就证明。"""
    offenders = _live_call_offenders()
    assert not offenders, (
        "期望：backend/tests/** 里没有对 provider 域名的真实调用（§17C，live 只能走 "
        "backend/live_checks/ 的 opt-in smoke）；实际：" + " ; ".join(offenders)
    )


def test_g8b_live_smoke_is_not_under_the_default_collection_root():
    """`pytest tests` must not collect the live runner; it also must exist to be run."""
    assert LIVE_SMOKE.exists(), (
        f"期望：opt-in live smoke 存在于 {LIVE_SMOKE.relative_to(REPO_ROOT)}；实际：文件不存在。"
        "§17C 要求 live 验证存在且能单独跑，不是不存在"
    )
    assert TESTS_ROOT not in LIVE_SMOKE.parents, (
        "期望：live smoke 放在收集根之外（backend/live_checks/）；实际：它被放在 "
        f"{TESTS_ROOT} 的收集范围内，默认套件会被迫依赖网络"
    )
    assert not (TESTS_ROOT / "live").exists(), (
        "期望：不要在 tests/ 下再开 live/ 子目录（pytest 会递归收集，默认套件就会出网）；"
        "实际：backend/tests/live 存在"
    )


def test_g8c_live_smoke_refuses_loudly_without_the_opt_in_flag():
    """Opt-in means refusing, not passing quietly, when nobody asked for it."""
    env = {key: value for key, value in os.environ.items() if key != LIVE_FLAG}
    proc = subprocess.run(
        [_python(), "-m", "pytest", str(LIVE_SMOKE), "-q", "--tb=line", "-p", "no:cacheprovider"],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0, (
        f"期望：未设 {LIVE_FLAG}=1 时 live smoke 必须非零退出并指名开关；"
        f"实际：RC={proc.returncode}，输出尾部 {output[-200:]!r}。"
        "用 skip 实现 opt-in 就是这条门要防的：它会把「没人跑过 live」报成「没有失败」"
    )
    # RC != 0 单独用不算证据：文件写坏、fixture 报错、收集失败都会非零。
    # 这条门要的是"设计好的拒绝"，所以三件事同时成立：RC != 0、点名开关、且不是收集期报错。
    assert LIVE_FLAG in output, (
        f"期望：拒绝信息里点名开关 {LIVE_FLAG}，让人知道怎么才算真跑过；"
        f"实际输出尾部：{output[-240:]!r}"
    )
    assert " error during collection" not in output and "SyntaxError" not in output, (
        f"期望：这是 opt-in 的主动拒绝，不是文件自己跑不起来；实际：{output[-240:]!r}"
    )


def test_g8d_conftest_strips_provider_credentials_before_app_import():
    """The mechanism that keeps a configured terminal from flipping fallback tests onto the wire."""
    conftest = (TESTS_ROOT / "conftest.py").read_text(encoding="utf-8")
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "CITYNEXUS_AI_API_KEY", "CITYNEXUS_AI_MODEL"):
        assert name in conftest, (
            f"期望：tests/conftest.py 仍然显式清掉 {name}（ORD-AR1 的前科：终端里恰好有真 key "
            f"会把「没有 provider ⇒ 走确定性回退」那一族翻成走网络）；实际：这行没了"
        )
    assert "os.environ.pop" in conftest, "期望：conftest 用 pop 真正清除而不是只注释掉；实际：没找到"

    import os

    leaked = [name for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "CITYNEXUS_AI_API_KEY") if name in os.environ]
    assert not leaked, (
        f"期望：跑到这里时进程环境里不该还有 provider 凭据（conftest 应已清掉）；实际：{leaked}。"
        "不在这里报 key 内容，只报键名——规则：报告不写任何 key 前缀"
    )

