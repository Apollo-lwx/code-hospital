"""把评测里反复出现的误报从明确缺陷降到风格。发现仍留在病历里。"""

from __future__ import annotations

import ast
from pathlib import Path


def soften_findings(project_root: str, visit_kind: str, findings: list) -> None:
    """合入门诊的未使用导入，以及星号导入、TYPE_CHECKING 里的未定义名字。"""
    contexts: dict[str, tuple[bool, list[tuple[int, int]]]] = {}
    root = Path(project_root)
    for finding in findings:
        rule = finding.rule_id or ""
        if visit_kind == "admission" and rule in {"F401", "F541"}:
            finding.severity = "nit"
        if rule != "F821":
            continue
        star, spans = contexts.setdefault(
            finding.file_path, _undefined_context(root, finding.file_path),
        )
        if star or _inside(finding.line_start, spans):
            finding.severity = "nit"


def _undefined_context(root: Path, rel: str) -> tuple[bool, list[tuple[int, int]]]:
    path = root / rel
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (OSError, UnicodeError, SyntaxError):
        return False, []
    star = False
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            star = True
        if isinstance(node, ast.If) and _type_checking(node.test):
            end = getattr(node, "end_lineno", None) or node.lineno
            spans.append((node.lineno, end))
    return star, spans


def _type_checking(test: ast.AST) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _inside(line: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in spans)
