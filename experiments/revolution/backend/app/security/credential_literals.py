"""ORD-0307：字面量凭据的常驻启动门——只判存在与位置，绝不打印值。

形状判据沿用 09-23 那枚未落地的门（`review/credential-literals-gate` @ 9cdaf6c8），这里补上它没有的
三件：具名扫描集合、`strict|report|off` 三档策略、以及按**内容摘要**而非路径的申报通道（D-2=甲）。

两枚摘要分工不同，混用会读歪：`position_fp = sha256("路径:行号")[:12]` 答"是哪一处"；
`token_fp = sha256(命中的串)[:12]` 答"两处是不是同一把凭据"。原文与其任何前缀都不进返回值、日志或异常串。
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parents[3]

CODE_OK = "CREDENTIAL_GATE_CLEAR"
CODE_IN_PRODUCT_PATH = "CREDENTIAL_LITERAL_IN_TREE"
CODE_OUTSIDE_PRODUCT_PATH = "CREDENTIAL_LITERAL_OUTSIDE_PRODUCT_PATH"
CODE_OFF = "CREDENTIAL_GATE_CONFIGURED_OFF"
CODE_UNRECOGNISED = "CREDENTIAL_GATE_POLICY_UNRECOGNISED"
CODE_LIVE_IN_ENVIRONMENT = "LIVE_CREDENTIAL_IN_ENVIRONMENT"

#: 扫描集合是判据的一部分，所以写死并由门自己核对存在性；收据里的清单必须来自同一次枚举。
SCAN_ROOTS: tuple[str, ...] = ("backend/app", "backend/pyproject.toml", "contracts", "data",
                               "tools", "agent-mcp", "frontend/src", "frontend/e2e", "qoder-handoff")
#: 产品启动路径**之内**的命中才算"开机该拒"；其余是"版本控制里带着字面量"的证人侧。
PRODUCT_ROOTS: tuple[str, ...] = ("backend/app", "backend/pyproject.toml", "contracts", "data",
                                  "tools", "agent-mcp", "frontend/src")
EXCLUDED_DIR_NAMES = frozenset({"__pycache__", "node_modules", ".git", "dist", "build"})
SCANNED_SUFFIXES = frozenset({".py", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".env",
                              ".txt", ".md", ".ts", ".tsx", ".patch", ".diff"})

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("provider_key_shape", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("aws_access_key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("bearer_or_basic_literal", re.compile(r"(?:Basic|Bearer)\s+[A-Za-z0-9+/=_\-]{24,}")),
    ("labeled_assignment", re.compile(r"(?i)(api_?key|secret|token|password|passwd|access_?key)"
                                      r"\w*\s*[:=]\s*[\"']([^\"'\s]{20,})[\"']")),
)

#: 明显占位/自证样本不算泄漏——但也不算"门有效"的证据（负控那一格反过来钉它）。
PLACEHOLDER_MARKERS: tuple[str, ...] = ("redacted", "example", "placeholder", "dummy", "fake",
                                        "sample", "your-key", "yourkey", "xxx", "test-key",
                                        "notreal", "changeme", "instrument-selftest", "aaaaaaaa")
MIN_ENTROPY_BITS_PER_CHAR = 3.2

#: D-2=甲：**申报看内容摘要，不看路径**。桩一改指纹就不吻合 ⇒ 声明作废并回到"未申报 ⇒ 红"。
DECLARED_SAMPLES: tuple[dict, ...] = (
    {"at": "frontend/e2e/qoder-e2e-config-guard.spec.ts:140", "token_fp": "faca4b0049a4",
     "what": "e2e 配置守卫塞进子进程的**假** DEEPSEEK_API_KEY（形状真、值假）",
     "declared_by": "commander 494abfd7 裁定 D-2=甲；lane F 8ccd71d8 录入 @2026-09-23",
     "proof": "同文件 :144 断言 run.status != 0；:145 断言 stderr 含 'external AI must be off'"},
)


#: 产品自己会当凭据读的三把环境变量：现役侧**只从这里取**，且只取长度与指纹。
LIVE_CREDENTIAL_ENV_VARS: tuple[str, ...] = ("CITYNEXUS_AI_API_KEY", "DEEPSEEK_API_KEY",
                                             "CITYNEXUS_HERMES_API_KEY")

#: 现役环境凭据的申报表**故意留空**：把"今天真在用的 key"申报成正常，正是本单要防的事。
#: 它存在的意义只有一个——轮换之后，旧指纹可以在这里具名退役，而**不**是新 key 的容身处。
ENV_DECLARATIONS: tuple[dict, ...] = ()


def live_env_credentials(environ: dict | None = None) -> list[dict]:
    """运行环境里现役的凭据：只报变量名、长度与摘要；值 0 字节进返回值。"""

    env = environ if environ is not None else os.environ
    out: list[dict] = []
    for name in LIVE_CREDENTIAL_ENV_VARS:
        value = env.get(name) or ""
        if not value.strip():
            continue
        fp = token_fp(value)
        declared = next((d for d in ENV_DECLARATIONS if d["token_fp"] == fp), None)
        out.append({"var": name, "length": len(value), "token_fp": fp,
                    "position_fp": position_fp("environment", LIVE_CREDENTIAL_ENV_VARS.index(name) + 1),
                    "in_product_path": True, "declared": declared is not None,
                    "declared_at": declared["at"] if declared else None})
    return out


class CredentialLiteralError(RuntimeError):
    """开机就拒：受版本控制的集合里有**未申报**的字面量凭据，而策略要求拒绝启动。"""

    def __init__(self, hits: list[dict], requested_policy: str,
                 code: str = CODE_IN_PRODUCT_PATH) -> None:
        self.code = code
        self.hits = hits
        self.policy = requested_policy
        where = "; ".join(f"{h.get('path') or h.get('var')}:{h.get('line', 0)} "
                          f"token_fp={h['token_fp']} len={h['length']} "
                          f"in_product_path={h['in_product_path']}" for h in hits)
        super().__init__(f"{self.code}: {len(hits)} 处未申报的字面量凭据（只报位置与摘要）：{where}")

    def details(self) -> dict[str, object]:
        keys = ("length", "token_fp", "position_fp", "in_product_path", "declared")
        keys = keys + ("path", "line", "pattern", "var")
        return {"policy": self.policy, "count": len(self.hits),
                "hits": [{k: h[k] for k in keys if k in h} for h in self.hits]}


def entropy_bits_per_char(token: str) -> float:
    counts = Counter(token)
    n = len(token)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def token_fp(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def position_fp(path: str, line: int) -> str:
    return hashlib.sha256(f"{path}:{line}".encode("utf-8")).hexdigest()[:12]


def policy(raw: str | None = None) -> str:
    """strict＝命中即拒启动；report＝照常启动但交出计数（默认）；off＝显式关掉，代价由门点名。"""

    value = (raw if raw is not None else os.getenv("CITYNEXUS_CREDENTIAL_GATE", "report")).strip().lower()
    if value not in {"strict", "report", "off"}:
        raise ValueError(f"{CODE_UNRECOGNISED}: {value!r}（只认 strict|report|off）")
    return value


def candidate_files(root: Path = REPO) -> list[str]:
    out: list[str] = []
    for rel in SCAN_ROOTS:
        p = root / rel
        if p.is_file():
            out.append(rel)
            continue
        if not p.is_dir():
            continue
        for q in sorted(p.rglob("*")):
            if not q.is_file() or q.suffix.lower() not in SCANNED_SUFFIXES:
                continue
            if EXCLUDED_DIR_NAMES & set(q.parts):
                continue
            try:
                out.append(q.relative_to(root).as_posix())
            except ValueError:
                continue
    return sorted(set(out))


def _tokens_in(line: str) -> Iterable[tuple[str, str]]:
    for name, pat in PATTERNS:
        for m in pat.finditer(line):
            yield name, (m.group(2) if name == "labeled_assignment" and m.lastindex else m.group(0))


def find_literals(paths: list[str], root: Path = REPO) -> list[dict]:
    hits: list[dict] = []
    for rp in paths:
        try:
            text = (root / rp).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for name, tok in _tokens_in(line):
                if any(k in tok.casefold() for k in PLACEHOLDER_MARKERS):
                    continue
                ent = entropy_bits_per_char(tok)
                if ent < MIN_ENTROPY_BITS_PER_CHAR:
                    continue
                fp = token_fp(tok)
                declared = next((d for d in DECLARED_SAMPLES if d["token_fp"] == fp), None)
                hits.append({"path": rp, "line": no, "pattern": name, "length": len(tok),
                             "entropy_bits_per_char": round(ent, 2), "token_fp": fp,
                             "position_fp": position_fp(rp, no),
                             "in_product_path": rp.startswith(PRODUCT_ROOTS),
                             "declared": declared is not None,
                             "declared_at": declared["at"] if declared else None})
    return hits


def scan(root: Path = REPO) -> dict:
    files = candidate_files(root)
    hits = find_literals(files, root)
    undeclared = [h for h in hits if not h["declared"]]
    product = [h for h in undeclared if h["in_product_path"]]
    verdict = (CODE_OFF if policy() == "off" else CODE_IN_PRODUCT_PATH if product
               else CODE_OUTSIDE_PRODUCT_PATH if undeclared else CODE_OK)
    env = live_env_credentials()
    env_undeclared = [h for h in env if not h["declared"]]
    if env_undeclared:
        verdict = CODE_LIVE_IN_ENVIRONMENT
    return {"verdict": verdict, "policy": policy(), "roots": list(SCAN_ROOTS),
            "roots_missing": [r for r in SCAN_ROOTS if not (root / r).exists()],
            "scanned_files": len(files), "hits_total": len(hits),
            "hits_undeclared": len(undeclared), "hits_in_product_path": len(product),
            "hits_declared": len(hits) - len(undeclared), "hits": hits,
            "env_present": len(env), "env_undeclared": len(env_undeclared),
            "env_fingerprints": [{k: h[k] for k in ("var", "length", "token_fp", "declared")}
                                 for h in env],
            "live_total": len(undeclared) + len(env_undeclared), "live": undeclared + env_undeclared}


def startup_report(root: Path = REPO) -> dict:
    """main.py 启动时只叫这一枚。report/off 返回计数；strict 遇未申报命中即抛具名错。"""

    report = scan(root)
    if report["policy"] == "strict" and report["live_total"]:
        raise CredentialLiteralError(report["live"], "strict", report["verdict"])
    return {k: v for k, v in report.items() if k not in ("hits", "live")}
