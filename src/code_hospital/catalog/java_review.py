"""Java 内置审查尺，只做正则层。方法行数仍由 structure-ruler 计。"""

from __future__ import annotations

import re
from pathlib import Path

_SECRET = re.compile(
    r"""(?i)\b(password|passwd|secret|api[_-]?key|apikey|token|private[_-]?key)\b\s*=\s*"([^"]*)\""""
)
_PLACEHOLDER = re.compile(r"^(x+|changeme|todo|placeholder|your[_-]?key|password|secret)$", re.I)
_SQL = re.compile(
    r"""(?i)(?:\bexecute\s*\(\s*["'][^"']*\+|["']\s*(?:select|insert|update|delete)\b[^"']*["']\s*\+)"""
)
_SYSOUT = re.compile(r"System\.out\.print")
_THREAD = re.compile(r"\bnew\s+Thread\s*\(")
_EXECUTORS = re.compile(r"\bExecutors\.\w+\s*\(")
_EMPTY_CATCH = re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}")


def scan_java_review(project_root: str, files: list[str]) -> tuple[list[dict], list[str]]:
    from .policy import load_policy

    policy = load_policy(project_root)
    notes = [
        "方法行数和文件行数由 structure-ruler 计。"
        "java-review 只看行长、空 catch、System.out.print、硬编码字面量、new Thread、Executors 工厂方法和 SQL 拼接。",
    ]
    root = Path(project_root)
    java_files = [rel for rel in files if Path(rel).suffix.lower() == ".java"]
    if not java_files:
        notes.append("java-review: 本 scope 无 Java 文件，跳过")
        return [], notes
    if policy.java_line_length <= 0:
        notes.append("Java 单行再长也不记入报告")
    raws: list[dict] = []
    for rel in java_files:
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        key = rel.replace("\\", "/")
        _file(text, key, policy.java_line_length, raws)
    return raws, notes


def _file(text: str, file_path: str, line_length: int, raws: list[dict]) -> None:
    test_file = _is_test(file_path)
    for index, line in enumerate(text.splitlines(), 1):
        if line_length > 0 and len(line) > line_length:
            _add(raws, "java.line-length", f"这一行有 {len(line)} 个字符，超过 {line_length}", file_path, index, "minor")
        if _comment_line(line):
            continue
        if _SYSOUT.search(line):
            _add(raws, "java.system-out", "用了 System.out.print", file_path, index, "minor")
        if _THREAD.search(line):
            _add(raws, "java.raw-thread", "直接 new Thread", file_path, index, "minor")
        if _EXECUTORS.search(line):
            _add(raws, "java.executors", "直接调用 Executors 工厂方法", file_path, index, "minor")
        if _SQL.search(line):
            _add(raws, "java.sql-concat", "SQL 用拼接组成", file_path, index, "critical")
        if not test_file:
            _secrets(line, file_path, index, raws)
    for match in _EMPTY_CATCH.finditer(text):
        line = text[:match.start()].count("\n") + 1
        _add(raws, "java.empty-catch", "空的 catch 吃掉了异常", file_path, line, "major")


def _secrets(line: str, file_path: str, index: int, raws: list[dict]) -> None:
    for match in _SECRET.finditer(line):
        literal = match.group(2).strip()
        if len(literal) < 8 or _PLACEHOLDER.match(literal) or "example" in literal.lower():
            continue
        name = match.group(1)
        _add(raws, "java.hardcoded-secret", f"{name} 被写成了源码里的字面量", file_path, index, "critical", name)


def _comment_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(("//", "*", "/*"))


def _is_test(file_path: str) -> bool:
    name = Path(file_path).name
    key = file_path.replace("\\", "/")
    return "/test/" in key or "/tests/" in key or name.endswith("Test.java") or name.startswith("Test")


def _add(
    raws: list[dict], rule_id: str, message: str, file_path: str, line: int,
    severity: str, symbol: str = "",
) -> None:
    raws.append({
        "rule_id": rule_id,
        "message": message,
        "file_path": file_path,
        "line_start": line,
        "line_end": line,
        "severity": severity,
        "symbol": symbol,
    })
