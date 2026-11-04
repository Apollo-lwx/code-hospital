"""能力市场的一份自描述文件（manifest）。

一份 manifest 说清三件事：**是什么**（kind）、**怎么跑**（transport + 命令）、
**会碰什么**（risk）。医院只认这三件事，其余都是备注。

契约字段只增不改——第三方 manifest 会长期存活在客户仓库里，
删字段等于让别人的文件突然失效。

两个区（文档 §7）：
- `kind: instrument`（A 区）是检查设备，跑完产发现
- `kind: substrate`（B 区）是基座，供能力不产发现（记忆、索引、知识库都在这区）

内置 instruments.yaml 不搬家，用它转成 `tier: core` 的 manifest；
第三方一律从 `tier: probation` 起步，跑得进覆盖账，进不了报告。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

API = "code-hospital/v1"
KINDS = ("instrument", "substrate")
TIERS = ("core", "probation", "active", "retired")
# 怎么跑：院内直接驱动 / 随包 semgrep / 进程外命令 / 外聘 MCP 服务
TRANSPORTS = ("builtin", "semgrep", "external", "mcp")
# 来源：内置 / 市场装的 / 被诊仓库自己写的
ORIGINS = ("core", "market", "repo")
ARTIFACTS = ("findings", "sarif", "text")

_ID = re.compile(r"^[a-z][a-z0-9._-]{0,48}$")
_RISK_KEYS = ("reads_repo", "writes_repo", "network", "executes_code")

# 引擎名 → 怎么跑。内置 13 台里有 8 台是进程内、4 台是外部可执行、2 台走 semgrep。
# 判定写死在这张表里，不从 `builtin` 布尔反推——布尔只说「随包」，不说「怎么跑」。
_PROCESS_ENGINES = {"ruff", "eslint", "gitleaks", "jscpd", "mypy", "spotbugs", "import-linter"}
_SEMGREP_ENGINES = {"semgrep"}


def transport_of(row: dict) -> str:
    """从 instruments.yaml 的一行推断怎么跑。"""
    engine = str(row.get("engine") or "").strip()
    if engine in _SEMGREP_ENGINES:
        return "semgrep"
    if engine in _PROCESS_ENGINES:
        return "external"
    return "builtin"


def blank() -> dict:
    """一份最小可校验的 manifest 骨架。给报错时的 next_action 用。"""
    return {
        "api": API,
        "kind": "instrument",
        "id": "",
        "name": "",
        "vendor": "",
        "version": "",
        "tier": "probation",
        "origin": "market",
        "concern": "",
        "extensions": [],
        "triage": ["outpatient", "checkup"],
        "specialty_id": "general",
        "transport": "",
        "engine": "",
        "binary": "",
        "command": "",
        "tool": "",
        "args": [],
        "rules": "",
        "risk": {key: False for key in _RISK_KEYS},
        "acceptance": {"artifact": "findings", "source_url": ""},
        "limitation": "",
    }


def from_instrument_row(row: dict) -> dict:
    """内置 instruments.yaml 的一行 → manifest 形态。tier 恒为 core，origin 恒为 core。

    这是「迁移」而不是「重写」：文件保持原样，装配时多一层视图。
    好处是迁移零风险、可逐台验证；坏处是两处字段名并存，
    所以这里只做单向转换，装配仍以 instruments.yaml 为准。
    """
    engine = str(row.get("engine") or "")
    transport = transport_of(row)
    doc = blank()
    doc.update({
        "api": API,
        "kind": "instrument",
        "id": str(row.get("id") or ""),
        "name": str(row.get("concern") or row.get("id") or "")[:40],
        "vendor": "code-hospital",
        "version": "builtin",
        "tier": "core",
        "origin": "core",
        "concern": str(row.get("concern") or ""),
        "extensions": [str(item).lstrip(".").lower() for item in row.get("extensions") or []],
        "triage": [str(item) for item in row.get("triage") or []],
        "specialty_id": str(row.get("specialty_id") or "general"),
        "transport": transport,
        "engine": engine,
        "binary": str(row.get("binary") or ""),
        "command": "",
        "tool": "",
        "args": [],
        "rules": "",
        # 内置仪器只读仓库：它们是医院自己带的尺子，不联网、不写盘、不执行仓库代码。
        "risk": {key: False for key in _RISK_KEYS},
        "acceptance": {"artifact": "findings", "source_url": ""},
        "limitation": str(row.get("limitation") or ""),
        "requires_contract": bool(row.get("requires_contract")),
        "can_autofix": bool(row.get("can_autofix")),
    })
    doc["risk"]["reads_repo"] = True
    return doc


def core_manifests() -> dict[str, dict]:
    """内置 13 台 + 外聘引擎行，全部转成 manifest 视图。按 id 索引。"""
    from .formulary import instruments

    return {ident: from_instrument_row(row) for ident, row in instruments().items()}


def load_file(path: str | Path) -> tuple[dict, list[str]]:
    """读一份 manifest 文件并校验。返回 (manifest, 问题)。问题非空即不可用。"""
    file = Path(path)
    if not file.is_file():
        return blank(), [f"manifest 不存在：{file}"]
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return blank(), [f"manifest 不是合法 YAML：{str(exc)[:120]}"]
    if not isinstance(data, dict):
        return blank(), ["manifest 必须是映射，不能是列表或标量"]
    return validate(data)


def validate(raw: dict, *, known: set[str] | None = None) -> tuple[dict, list[str]]:
    """校验一份 manifest。问题逐条列，不抛异常——调用方要能一次看全所有毛病。

    `known` 是已占用的 id（内置 + 已装）。第三方不许顶替内置：
    顶替意味着同一份报告里「内置尺子的结论」和「陌生仪器的结论」混成一个名字，
    溯源一旦混了，后面所有差分都不可信。
    """
    problems: list[str] = []
    doc = blank()

    if not isinstance(raw, dict):
        return doc, ["manifest 必须是映射"]

    api = str(raw.get("api") or "").strip()
    if api != API:
        problems.append(f"api 必须是 {API}，读到「{api or '空'}」")

    kind = str(raw.get("kind") or "instrument").strip()
    if kind not in KINDS:
        problems.append(f"kind 只能是 instrument 或 substrate，读到「{kind}」")
        kind = "instrument"
    doc["kind"] = kind

    ident = str(raw.get("id") or "").strip()
    if not _ID.match(ident):
        problems.append(f"id 必须是小写字母开头的短名（a-z0-9._-），读到「{ident or '空'}」")
    elif known and ident in known:
        problems.append(f"id「{ident}」已被占用；第三方仪器不能顶替已有仪器")
    doc["id"] = ident

    doc["name"] = str(raw.get("name") or ident)[:60]
    doc["vendor"] = str(raw.get("vendor") or "")[:60]
    doc["version"] = str(raw.get("version") or "")[:40]

    tier = str(raw.get("tier") or "probation")
    if tier not in TIERS:
        problems.append(f"tier 只能是 {'/'.join(TIERS)}，读到「{tier}」")
        tier = "probation"
    # 装进来的没有「天生 core」这回事：core 只属于随包仪器。
    if tier == "core" and str(raw.get("origin") or "") != "core":
        tier = "probation"
    doc["tier"] = tier

    origin = str(raw.get("origin") or "market")
    doc["origin"] = origin if origin in ORIGINS else "market"

    doc["concern"] = str(raw.get("concern") or "")[:200]
    doc["extensions"] = _exts(raw.get("extensions"))
    doc["triage"] = [
        str(item) for item in (raw.get("triage") or [])
        if str(item) in {"emergency", "outpatient", "checkup"}
    ]
    doc["specialty_id"] = str(raw.get("specialty_id") or "general")

    risk, risk_problems = _risk(raw.get("risk"))
    problems.extend(risk_problems)
    doc["risk"] = risk

    artifact, accept_problems = _acceptance(raw.get("acceptance"))
    problems.extend(accept_problems)
    doc["acceptance"] = artifact

    if kind == "substrate":
        # 基座不是仪器：它不产发现，也就不该声明怎么扫仓库。
        for key, hint in (("command", "命令"), ("tool", "工具"), ("rules", "规则文件")):
            if raw.get(key):
                problems.append(f"substrate 不声明{hint}——基座供能力，不产发现")
        doc["transport"] = "builtin"
        doc["limitation"] = str(raw.get("limitation") or "")[:200]
        return doc, problems

    transport = str(raw.get("transport") or "").strip()
    if transport not in TRANSPORTS:
        problems.append(f"transport 只能是 {'/'.join(TRANSPORTS)}，读到「{transport or '空'}」")
        transport = ""
    # 第三方不能用 builtin：那意味着医院自己驱动一个它不认识的引擎。
    if transport == "builtin" and doc["origin"] != "core":
        problems.append("第三方不能用 builtin 档——医院只驱动随包仪器，外来的一律走 external、mcp 或 semgrep")
        transport = ""
    # 内置档的 transport 由 instruments.yaml 的 engine 决定（transport_of），
    # 转出来时已经填好，这里不再覆盖。
    doc["transport"] = transport
    doc["engine"] = str(raw.get("engine") or "")[:40]

    command = str(raw.get("command") or "").strip()
    tool = str(raw.get("tool") or "").strip()
    rules = str(raw.get("rules") or "").strip()
    binary = str(raw.get("binary") or "").strip()
    # 内置档不查供应链：规则随包（`packs._PACKS`）、命令由医院登记、
    # 有几台是「声明了但还没拍」（分层尺）。外来档才需要证明自己怎么跑。
    strict = doc["origin"] != "core"

    if transport in ("external", "mcp"):
        name = command or binary
        if not name:
            if strict:
                problems.append(f"{transport} 要写一个命令名")
        elif any(char in name for char in "\\/\n"):
            problems.append(f"命令名不能带路径：{name}——只认 PATH 上的裸名")
    elif transport == "semgrep":
        if not rules:
            if strict:
                problems.append("semgrep 要写规则文件")
        elif Path(rules).is_absolute() or ".." in Path(rules).parts:
            problems.append(f"规则文件必须在仓库内：{rules}")
    elif transport == "builtin":
        engine = doc["engine"] or binary
        if not engine and strict:
            problems.append("builtin 要写引擎名")

    if transport == "mcp":
        from .mcp_client import tool_name

        if tool_name(tool) is None:
            problems.append(f"mcp 要写工具名（字母开头的短名），读到「{tool or '空'}」")

    args = raw.get("args")
    if args:
        from .external_run import safe_args

        kept = safe_args(args)
        if kept is None:
            problems.append("args 含禁止字符（shell 元字符、绝对路径或 ..）")
        else:
            doc["args"] = kept

    doc["command"] = command
    doc["tool"] = tool
    doc["rules"] = rules
    doc["binary"] = binary or command
    doc["limitation"] = str(raw.get("limitation") or "")[:200]
    return doc, problems


def _exts(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).lstrip(".").lower() for item in value if str(item).strip()]


def _risk(value) -> tuple[dict, list[str]]:
    """风险声明：布尔，缺省全 False。

    缺省 False 是刻意的——漏写时医院按「不碰」处理，但**执行前仍会复核**：
    probation 期只要声明了 writes_repo 就不运行（见 market.blocked_reason）。
    """
    doc = {key: False for key in _RISK_KEYS}
    problems: list[str] = []
    if value is None:
        return doc, problems
    if not isinstance(value, dict):
        return doc, ["risk 必须是映射，例如 writes_repo: false"]
    for key in _RISK_KEYS:
        if key in value and not isinstance(value[key], bool):
            problems.append(f"risk.{key} 必须是 true 或 false")
            continue
        doc[key] = bool(value.get(key, False))
    unknown = [key for key in value if key not in _RISK_KEYS]
    if unknown:
        # 不认识的键不拒收：契约只增不改，但要说出来，别以为是生效了。
        problems.append(f"risk 里不认识的键：{', '.join(sorted(unknown))}——这次不生效")
    return doc, problems


def _acceptance(value) -> tuple[dict, list[str]]:
    doc = {"artifact": "findings", "source_url": ""}
    if value is None:
        return doc, []
    if isinstance(value, str):
        # 简写：acceptance: sarif
        if value in ARTIFACTS:
            doc["artifact"] = value
            return doc, []
        return doc, [f"acceptance 只能是 {'/'.join(ARTIFACTS)}，读到「{value}」"]
    if not isinstance(value, dict):
        return doc, ["acceptance 必须是映射或短名"]
    artifact = str(value.get("artifact") or "findings")
    problems = []
    if artifact not in ARTIFACTS:
        problems.append(f"acceptance.artifact 只能是 {'/'.join(ARTIFACTS)}，读到「{artifact}」")
        artifact = "findings"
    doc["artifact"] = artifact
    doc["source_url"] = str(value.get("source_url") or "")[:200]
    return doc, problems


def describe(doc: dict) -> str:
    """一行摘要。给人看的，不参与判定。"""
    kind = doc.get("kind") or "instrument"
    tier = doc.get("tier") or ""
    head = f"{doc.get('id', '')}（{kind}，{tier}）"
    if kind == "substrate":
        return f"{head} {doc.get('concern', '')}".strip()
    transport = doc.get("transport") or ""
    return f"{head} {transport} · {doc.get('concern', '')}".strip()[:160]


def to_row(doc: dict) -> dict:
    """manifest 视图 → instruments.yaml 那种行。装配层只认这种行，所以要有这一层。

    与 from_instrument_row 互逆（内置档），字段名差异只在这里出现。
    """
    transport = doc.get("transport") or "builtin"
    engine = doc.get("engine") or ""
    if not engine:
        engine = {"external": "external", "mcp": "mcp", "semgrep": "semgrep"}.get(transport, "builtin")
    return {
        "id": doc.get("id", ""),
        "concern": doc.get("concern") or doc.get("name") or doc.get("id", ""),
        "extensions": list(doc.get("extensions") or []),
        "triage": list(doc.get("triage") or []),
        "activation": "package" if doc.get("triage") else "visit",
        "engine": engine,
        "binary": doc.get("binary") or doc.get("command") or "",
        "command": doc.get("command") or "",
        "tool": doc.get("tool") or "",
        "args": list(doc.get("args") or []),
        "builtin": transport == "builtin" and doc.get("origin") == "core",
        "can_autofix": bool(doc.get("can_autofix")),
        "specialty_id": doc.get("specialty_id") or "general",
        "requires_contract": bool(doc.get("requires_contract")),
        "limitation": doc.get("limitation") or f"{doc.get('id', '')} 未到岗",
        "rules": doc.get("rules") or "",
        "sandbox": False,
        "user": True,
        "transport": transport,
        "tier": doc.get("tier") or "probation",
        "origin": doc.get("origin") or "market",
        "risk": dict(doc.get("risk") or {}),
    }


__all__ = [
    "API",
    "ARTIFACTS",
    "KINDS",
    "ORIGINS",
    "TIERS",
    "TRANSPORTS",
    "blank",
    "core_manifests",
    "describe",
    "from_instrument_row",
    "load_file",
    "to_row",
    "transport_of",
    "validate",
]
