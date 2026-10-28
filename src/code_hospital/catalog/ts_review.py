"""TypeScript 内置审查尺，只做文本层。解析器未建，函数长度是估算。"""

from __future__ import annotations

import re
from pathlib import Path

_SECRET = re.compile(
    r"""(?i)\b(password|passwd|secret|api[_-]?key|apikey|token|private[_-]?key)\b\s*[:=]\s*["']([^"']*)["']"""
)
_PLACEHOLDER = re.compile(r"^(x+|changeme|todo|placeholder|your[_-]?key|password|secret)$", re.I)
_ANY = re.compile(r"(?::\s*any\b|\bas\s+any\b|<\s*any\b|,\s*any\s*[,>]|\bany\s*\[\])")
_CONSOLE = re.compile(r"\bconsole\.log\s*\(")
_TODO = re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b")
_INNER = re.compile(r"\binnerHTML\s*=")
_DANGER = re.compile(r"\bdangerouslySetInnerHTML\b")
_FUNC = re.compile(r"(?:\bfunction\b|=>)")
_FUNC_NAME = re.compile(r"\bfunction\s+(\w+)")
_ASSIGN_NAME = re.compile(r"\b(?:const|let|var)\s+(\w+)")
_TYPE_LINE = re.compile(r"^\s*(?:export\s+)?(?:type|interface)\b")
_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//.*?$", re.M)
_STRING = re.compile(r"""("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\\n])*`)""")
_EXTS = {".ts", ".tsx"}


def scan_ts_review(project_root: str, files: list[str]) -> tuple[list[dict], list[str]]:
    from .policy import load_policy

    policy = load_policy(project_root)
    notes = [
        "TS 解析未建，函数长度按 function 和 => 估算。花括号要写在同一行，类方法若没有这两种写法就估算不到。"
        "文件行数仍由 structure-ruler 计。",
    ]
    root = Path(project_root)
    ts_files = [rel for rel in files if Path(rel).suffix.lower() in _EXTS]
    if not ts_files:
        notes.append("ts-review: 本 scope 无 TypeScript 文件，跳过")
        return [], notes
    if policy.ts_line_length <= 0:
        notes.append("TypeScript 单行再长也不记入报告")
    if policy.function_lines <= 0:
        notes.append("函数再长也不记入报告")
    raws: list[dict] = []
    for rel in ts_files:
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        _file(text, rel.replace("\\", "/"), policy.ts_line_length, policy.function_lines, raws)
    return raws, notes


def _file(text: str, file_path: str, line_length: int, function_lines: int, raws: list[dict]) -> None:
    test_file = _is_test(file_path)
    masked = _mask(text).splitlines()
    original = text.splitlines()
    for index, line in enumerate(original, 1):
        if line_length > 0 and len(line) > line_length:
            _add(raws, "ts.line-length", f"这一行有 {len(line)} 个字符，超过 {line_length}", file_path, index, index, "minor")
        if line.endswith((" ", "\t")):
            _add(raws, "ts.trailing-whitespace", "行尾有空白", file_path, index, index, "nit")
        if _TODO.search(line):
            _add(raws, "ts.todo", "留下了 TODO、FIXME、HACK 或 XXX", file_path, index, index, "nit")
        code = masked[index - 1] if index - 1 < len(masked) else ""
        if _ANY.search(code):
            _add(raws, "ts.any", "用了 any", file_path, index, index, "major")
        if _CONSOLE.search(code):
            _add(raws, "ts.console-log", "留下了 console.log", file_path, index, index, "minor")
        if _INNER.search(code):
            _add(raws, "ts.dom-write", "把字符串写入 innerHTML", file_path, index, index, "major")
        if _DANGER.search(code):
            _add(raws, "ts.dom-write", "使用 dangerouslySetInnerHTML", file_path, index, index, "major")
        if not test_file and not line.strip().startswith(("//", "*", "/*")):
            _secrets(line, file_path, index, raws)
    if function_lines > 0:
        for name, start, end in _estimate_functions(masked):
            count = end - start + 1
            if count > function_lines:
                _add(
                    raws, "ts.function-lines",
                    f"函数 {name} 估算有 {count} 行，超过 {function_lines}",
                    file_path, start, end, "minor", name,
                )


def _estimate_functions(lines: list[str]) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    pending: tuple[str, int] | None = None
    opened: list[tuple[str, int, int]] = []
    depth = 0
    for index, line in enumerate(lines, 1):
        if pending is None and not _TYPE_LINE.search(line) and _FUNC.search(line) and "{" in line:
            pending = (_name(line), index)
        for char in line:
            if char == "{":
                depth += 1
                if pending is not None:
                    opened.append((pending[0], pending[1], depth))
                    pending = None
            elif char == "}":
                depth = max(0, depth - 1)
                if opened and depth < opened[-1][2]:
                    name, start, _opened = opened.pop()
                    spans.append((name, start, index))
        pending = None
    for name, start, _opened in opened:
        spans.append((name, start, len(lines) or start))
    return spans


def _name(line: str) -> str:
    named = _FUNC_NAME.search(line)
    if named:
        return named.group(1)
    assigned = _ASSIGN_NAME.search(line)
    if assigned:
        return assigned.group(1)
    return "function"


def _secrets(line: str, file_path: str, index: int, raws: list[dict]) -> None:
    for match in _SECRET.finditer(line):
        literal = match.group(2).strip()
        if len(literal) < 8 or _PLACEHOLDER.match(literal) or "example" in literal.lower():
            continue
        name = match.group(1)
        _add(raws, "ts.hardcoded-secret", f"{name} 被写成了源码里的字面量", file_path, index, index, "critical", name)


def _mask(text: str) -> str:
    def keep_newlines(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    text = _BLOCK.sub(keep_newlines, text)
    text = _LINE_COMMENT.sub(lambda match: " " * len(match.group(0)), text)

    def blank_string(match: re.Match[str]) -> str:
        body = match.group(1)
        return body[0] + (" " * (len(body) - 2)) + body[-1]

    return _STRING.sub(blank_string, text)


def _is_test(file_path: str) -> bool:
    name = Path(file_path).name
    key = file_path.replace("\\", "/")
    return (
        name.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
        or "/__tests__/" in key
        or "/test/" in key
        or "/tests/" in key
    )


def _add(
    raws: list[dict], rule_id: str, message: str, file_path: str,
    line_start: int, line_end: int, severity: str, symbol: str = "",
) -> None:
    raws.append({
        "rule_id": rule_id,
        "message": message,
        "file_path": file_path,
        "line_start": line_start,
        "line_end": line_end,
        "severity": severity,
        "symbol": symbol,
    })
