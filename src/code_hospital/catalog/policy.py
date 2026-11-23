"""被诊疗仓库根目录 `code-hospital.yaml` 里的病症口径。

没有这份文件时用内置默认。结构尺写成 0 表示这把尺不算病。
`catalog.rules.ignore` 按规则号或前缀丢掉发现：`F401` 只丢这一条，`FURB` 丢掉以此开头的全部。
`min_severity` 留空表示各严重度都保留。`ruff_select` 非空时传给 `ruff --select`。
`catalog.rename.exempt` 声明仓库的框架约定名（符号 glob，如 `visit_*`）：
命中这些模式的改名发现不入方——不是「没问题」，是「这名不归医院管」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_CONFIG = "code-hospital.yaml"
_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9.]*$")
_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "nit": 3, "info": 4}
SEVERITY_PLAIN = {
    "critical": "必须先处理",
    "major": "明确缺陷",
    "minor": "一般问题",
    "nit": "风格建议",
    "info": "参考信息",
}

DEFAULT_FUNCTION_LINES = 80
DEFAULT_FILE_LINES = 500
DEFAULT_CYCLOMATIC = 15
DEFAULT_PARAMETERS = 5
DEFAULT_BRANCHES = 10
DEFAULT_JAVA_LINE_LENGTH = 120
DEFAULT_TS_LINE_LENGTH = 200
# git worktree 高危隔离（worktree 草案一期）：高危药味默认进隔离间预演。
WORKTREE_MODES = ("never", "high", "always")
DEFAULT_WORKTREE_MODE = "high"
DEFAULT_WORKTREE_LINK_DIRS = (".venv", "venv", "node_modules")


@dataclass(frozen=True)
class DiseasePolicy:
    function_lines: int = DEFAULT_FUNCTION_LINES
    file_lines: int = DEFAULT_FILE_LINES
    cyclomatic: int = DEFAULT_CYCLOMATIC
    parameters: int = DEFAULT_PARAMETERS
    branches: int = DEFAULT_BRANCHES
    java_line_length: int = DEFAULT_JAVA_LINE_LENGTH
    ts_line_length: int = DEFAULT_TS_LINE_LENGTH
    ignore: tuple[str, ...] = ()
    min_severity: str = ""
    ruff_select: tuple[str, ...] = ()
    # 正骨豁免：仓库的框架约定名（ast 的 visit_*、pytest fixture 这类）。
    # 命中这些符号的改名发现不入方——不是「没问题」，是「这名不归医院管」。
    rename_exempt: tuple[str, ...] = ()
    # 规则处置矩阵（catalog.rules.dispositions）：规范/规则分流与自动化上限，
    # 用户配置优先于 catalog.dispositions 的内置默认表，未命中维持现状。
    dispositions: tuple = ()
    # 隔离间：never 直写主仓（现状）；high 高危药味进隔离间（默认）；
    # always 全量隔离。hosp_dispense 的 isolation 入参当次覆盖。
    worktree: str = DEFAULT_WORKTREE_MODE
    # 验收环境布置：把主仓这些依赖目录以 junction/symlink 接进隔离间。
    worktree_link_dirs: tuple[str, ...] = DEFAULT_WORKTREE_LINK_DIRS
    notes: tuple[str, ...] = ()


def load_policy(project_root: str) -> DiseasePolicy:
    path = Path(project_root) / _CONFIG
    if not path.is_file():
        return DiseasePolicy()
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return DiseasePolicy(notes=("code-hospital.yaml 无法解析，病症口径用默认值",))
    if not isinstance(doc, dict):
        return DiseasePolicy(notes=("code-hospital.yaml 不是映射，病症口径用默认值",))
    catalog = doc.get("catalog") or {}
    if not isinstance(catalog, dict):
        catalog = {}
    structure = catalog.get("structure") or {}
    java = catalog.get("java") or {}
    typescript = catalog.get("ts") or {}
    rules = catalog.get("rules") or {}
    rename = catalog.get("rename") or {}
    safety = doc.get("safety") or {}
    if not isinstance(structure, dict):
        structure = {}
    if not isinstance(java, dict):
        java = {}
    if not isinstance(typescript, dict):
        typescript = {}
    if not isinstance(rules, dict):
        rules = {}
    if not isinstance(rename, dict):
        rename = {}
    if not isinstance(safety, dict):
        safety = {}
    notes: list[str] = []
    ignore, ignored_notes = _tokens(rules.get("ignore"))
    notes.extend(ignored_notes)
    selected, select_notes = _tokens(rules.get("ruff_select"))
    notes.extend(select_notes)
    exempt, exempt_notes = _globs(rename.get("exempt"))
    notes.extend(exempt_notes)
    from .dispositions import parse_dispositions

    dispositions, disposition_notes = parse_dispositions(rules.get("dispositions"))
    notes.extend(disposition_notes)
    floor = str(rules.get("min_severity") or "").strip().lower()
    if floor and floor not in _SEVERITY_RANK:
        notes.append(f"min_severity={floor} 无法识别，严重度不设下限")
        floor = ""
    worktree_mode = str(safety.get("worktree") or DEFAULT_WORKTREE_MODE).strip().lower()
    if worktree_mode not in WORKTREE_MODES:
        notes.append(f"safety.worktree={worktree_mode} 无法识别，按 {DEFAULT_WORKTREE_MODE} 处理")
        worktree_mode = DEFAULT_WORKTREE_MODE
    link_dirs, link_notes = _link_dirs(safety.get("worktree_link_dirs"))
    notes.extend(link_notes)
    return DiseasePolicy(
        function_lines=_limit(structure.get("function_lines"), DEFAULT_FUNCTION_LINES),
        file_lines=_limit(structure.get("file_lines"), DEFAULT_FILE_LINES),
        cyclomatic=_limit(structure.get("cyclomatic"), DEFAULT_CYCLOMATIC),
        parameters=_limit(structure.get("parameters"), DEFAULT_PARAMETERS),
        branches=_limit(structure.get("branches"), DEFAULT_BRANCHES),
        java_line_length=_limit(java.get("line_length"), DEFAULT_JAVA_LINE_LENGTH),
        ts_line_length=_limit(typescript.get("line_length"), DEFAULT_TS_LINE_LENGTH),
        ignore=ignore,
        min_severity=floor,
        ruff_select=selected,
        rename_exempt=exempt,
        dispositions=dispositions,
        worktree=worktree_mode,
        worktree_link_dirs=link_dirs,
        notes=tuple(notes),
    )


def keeps(rule_id: str, severity: str, policy: DiseasePolicy) -> bool:
    if _ignored(rule_id, policy.ignore):
        return False
    if not policy.min_severity:
        return True
    return _SEVERITY_RANK.get(severity, 9) <= _SEVERITY_RANK[policy.min_severity]


def describe(policy: DiseasePolicy, dropped: int) -> str:
    rulers = [
        _ruler("函数", policy.function_lines),
        _ruler("文件", policy.file_lines),
        _ruler("圈复杂度", policy.cyclomatic),
        _count_ruler("参数", policy.parameters, "个"),
        _count_ruler("分支", policy.branches, "个"),
        _java_line(policy.java_line_length),
        _ts_line(policy.ts_line_length),
    ]
    ignored = (
        "这些规则不写入报告：" + "、".join(policy.ignore)
        if policy.ignore else "没有额外排除的规则"
    )
    floor = (
        f"只保留「{SEVERITY_PLAIN[policy.min_severity]}」及更需要处理的"
        if policy.min_severity else "各档都保留"
    )
    selected = (
        "ruff 只查 " + "、".join(policy.ruff_select)
        if policy.ruff_select else "ruff 使用它自己的默认规则"
    )
    dropped_text = (
        f"因此有 {dropped} 条没有写入报告"
        if dropped else "没有条目因这条标准被丢掉"
    )
    extra = "".join(f"。{note}" for note in policy.notes)
    return (
        "这次怎么算问题："
        + "。".join([*rulers, ignored, floor, selected, dropped_text])
        + extra
    )


def _ts_line(limit: int) -> str:
    if limit <= 0:
        return "TypeScript 单行再长也不记入报告"
    return f"TypeScript 单行超过 {limit} 个字符算问题"


def _java_line(limit: int) -> str:
    if limit <= 0:
        return "Java 单行再长也不记入报告"
    return f"Java 单行超过 {limit} 个字符算问题"


def _count_ruler(name: str, limit: int, unit: str) -> str:
    if limit <= 0:
        return f"{name}再多也不记入报告"
    return f"{name}超过 {limit} {unit}算问题"


def _ruler(name: str, limit: int) -> str:
    if limit <= 0:
        return f"{name}再长也不记入报告"
    if name == "圈复杂度":
        return f"圈复杂度超过 {limit} 算问题"
    return f"{name}超过 {limit} 行算问题"


def _limit(value, default: int) -> int:
    if value is None:
        return default
    if value is False:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else 0


def _tokens(raw) -> tuple[tuple[str, ...], list[str]]:
    if raw is None:
        return (), []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        return (), ["规则开关不是列表，已忽略"]
    kept: list[str] = []
    notes: list[str] = []
    for item in raw:
        token = str(item).strip()
        if not token:
            continue
        if not _TOKEN.fullmatch(token):
            notes.append(f"规则开关 {token} 含非法字符，已跳过")
            continue
        kept.append(token)
    return tuple(dict.fromkeys(kept)), notes


def _globs(raw) -> tuple[tuple[str, ...], list[str]]:
    # 与 _tokens 同形，但保留 glob 字符（* ? [）：豁免清单匹配的是符号名。
    if raw is None:
        return (), []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        return (), ["正骨豁免清单不是列表，已忽略"]
    kept: list[str] = []
    notes: list[str] = []
    for item in raw:
        token = str(item).strip()
        if not token:
            continue
        if token != str(item) or any(ch.isspace() for ch in token):
            notes.append(f"正骨豁免 {item!r} 含空白，已跳过")
            continue
        kept.append(token)
    return tuple(dict.fromkeys(kept)), notes


def _ignored(rule_id: str, ignore: tuple[str, ...]) -> bool:
    return any(rule_id == item or rule_id.startswith(item) for item in ignore)


def _link_dirs(raw) -> tuple[tuple[str, ...], list[str]]:
    """隔离间依赖目录清单：相对目录名，拒绝绝对路径与上跳。None 用内置默认。"""
    if raw is None:
        return DEFAULT_WORKTREE_LINK_DIRS, []
    if not isinstance(raw, list):
        return DEFAULT_WORKTREE_LINK_DIRS, ["safety.worktree_link_dirs 不是列表，用内置默认"]
    kept: list[str] = []
    notes: list[str] = []
    for item in raw:
        token = str(item).strip().replace("\\", "/").strip("/")
        if not token:
            continue
        parts = token.split("/")
        if ".." in parts or ":" in token or token.startswith("~"):
            notes.append(f"依赖目录 {item!r} 必须是仓内相对路径，已跳过")
            continue
        kept.append(token)
    return tuple(dict.fromkeys(kept)), notes
