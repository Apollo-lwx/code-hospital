"""被诊疗仓库根目录 `code-hospital.yaml` 里的病症口径。

没有这份文件时用内置默认。结构尺写成 0 表示这把尺不算病。
`catalog.rules.ignore` 按规则号或前缀丢掉发现：`F401` 只丢这一条，`FURB` 丢掉以此开头的全部。
`min_severity` 留空表示各严重度都保留。`ruff_select` 非空时传给 `ruff --select`。
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


@dataclass(frozen=True)
class DiseasePolicy:
    function_lines: int = DEFAULT_FUNCTION_LINES
    file_lines: int = DEFAULT_FILE_LINES
    cyclomatic: int = DEFAULT_CYCLOMATIC
    ignore: tuple[str, ...] = ()
    min_severity: str = ""
    ruff_select: tuple[str, ...] = ()
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
    rules = catalog.get("rules") or {}
    if not isinstance(structure, dict):
        structure = {}
    if not isinstance(rules, dict):
        rules = {}
    notes: list[str] = []
    ignore, ignored_notes = _tokens(rules.get("ignore"))
    notes.extend(ignored_notes)
    selected, select_notes = _tokens(rules.get("ruff_select"))
    notes.extend(select_notes)
    floor = str(rules.get("min_severity") or "").strip().lower()
    if floor and floor not in _SEVERITY_RANK:
        notes.append(f"min_severity={floor} 无法识别，严重度不设下限")
        floor = ""
    return DiseasePolicy(
        function_lines=_limit(structure.get("function_lines"), DEFAULT_FUNCTION_LINES),
        file_lines=_limit(structure.get("file_lines"), DEFAULT_FILE_LINES),
        cyclomatic=_limit(structure.get("cyclomatic"), DEFAULT_CYCLOMATIC),
        ignore=ignore,
        min_severity=floor,
        ruff_select=selected,
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


def _ignored(rule_id: str, ignore: tuple[str, ...]) -> bool:
    return any(rule_id == item or rule_id.startswith(item) for item in ignore)
