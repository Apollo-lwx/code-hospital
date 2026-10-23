"""院内结构尺：数行数。lizard 在 PATH 时再记圈复杂度，否则只留限制项。"""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
from pathlib import Path

_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(")
_EXT = {".py", ".java", ".js", ".jsx", ".ts", ".tsx", ".go"}
_LIZARD_BATCH = 40


def thresholds(project_root: str) -> tuple[int, int, int]:
    from .policy import load_policy

    policy = load_policy(project_root)
    return policy.function_lines, policy.file_lines, policy.cyclomatic


def _python_functions(text: str) -> list[tuple[str, int, int]]:
    lines = text.splitlines()
    defs: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines):
        matched = _DEF.match(line)
        if matched:
            defs.append((len(matched.group(1)), matched.group(2), index + 1))
    spans = []
    for cursor, (indent, name, start) in enumerate(defs):
        end = len(lines)
        for indent2, _name2, start2 in defs[cursor + 1:]:
            if indent2 <= indent:
                end = start2 - 1
                break
        spans.append((name, start, max(start, end)))
    return spans


def _split_lizard_location(location: str) -> tuple[str, int, int, str] | None:
    file_at = location.rfind("@")
    if file_at <= 0:
        return None
    head = location[:file_at]
    span_at = head.rfind("@")
    if span_at <= 0 or "-" not in head[span_at + 1:]:
        return None
    start_text, end_text = head[span_at + 1:].split("-", 1)
    if not start_text.isdigit() or not end_text.isdigit():
        return None
    return head[:span_at], int(start_text), int(end_text), location[file_at + 1:].replace("\\", "/")


def parse_lizard_csv(payload: str, project_root: str, limit: int) -> list[dict]:
    """lizard --csv 里圈复杂度超过阈值的函数。location 形如 name@start-end@file。"""
    raws: list[dict] = []
    root = Path(project_root).resolve()
    for row in csv.reader(io.StringIO(payload or "")):
        if len(row) < 6:
            continue
        try:
            complexity = int(row[1])
        except ValueError:
            continue
        if complexity <= limit:
            continue
        parsed = _split_lizard_location(row[5].strip())
        if parsed is None:
            continue
        name, start, end, file_path = parsed
        candidate = Path(file_path)
        if candidate.is_absolute():
            try:
                file_path = candidate.resolve().relative_to(root).as_posix()
            except ValueError:
                file_path = candidate.as_posix()
        raws.append({
            "rule_id": "structure.cyclomatic",
            "message": f"函数 {name} 圈复杂度 {complexity}，超过 {limit}",
            "file_path": file_path,
            "line_start": start,
            "line_end": max(start, end),
            "severity": "major",
            "symbol": name,
        })
    return raws


def _measure_cyclomatic(project_root: str, files: list[str], limit: int) -> tuple[bool, list[dict]]:
    """返回 (是否量到, 超阈值发现)。二进制缺失或整批失败时第一项为 False。"""
    if shutil.which("lizard") is None:
        return False, []
    root = Path(project_root)
    targets = [
        rel for rel in files
        if Path(rel).suffix.lower() in _EXT and (root / rel).is_file()
    ]
    if not targets:
        return True, []
    measured = False
    raws: list[dict] = []
    for offset in range(0, len(targets), _LIZARD_BATCH):
        batch = targets[offset:offset + _LIZARD_BATCH]
        try:
            proc = subprocess.run(
                ["lizard", "--csv", *batch],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode not in (0, 1) and not proc.stdout.strip():
            continue
        measured = True
        raws.extend(parse_lizard_csv(proc.stdout, project_root, limit))
    return measured, raws


def scan_structure(project_root: str, files: list[str]) -> tuple[list[dict], list[str]]:
    """返回 (raw dicts, limitations)。raw dict 交给扫描师归一化。"""
    function_limit, file_limit, cyclomatic_limit = thresholds(project_root)
    raws: list[dict] = []
    root = Path(project_root)
    for rel in files:
        if Path(rel).suffix.lower() not in _EXT:
            continue
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if file_limit and len(lines) > file_limit:
            raws.append({
                "rule_id": "structure.file_lines",
                "message": f"文件 {len(lines)} 行，超过 {file_limit}",
                "file_path": rel.replace("\\", "/"),
                "line_start": 1,
                "line_end": len(lines),
                "severity": "major",
                "symbol": "",
            })
        if path.suffix.lower() != ".py":
            continue
        for name, start, end in _python_functions(text):
            if function_limit and end - start + 1 > function_limit:
                raws.append({
                    "rule_id": "structure.function_lines",
                    "message": f"函数 {name} 有 {end - start + 1} 行，超过 {function_limit}",
                    "file_path": rel.replace("\\", "/"),
                    "line_start": start,
                    "line_end": end,
                    "severity": "major",
                    "symbol": name,
                })
    limitations = []
    if cyclomatic_limit:
        measured, cyclomatic = _measure_cyclomatic(project_root, files, cyclomatic_limit)
        if measured:
            raws.extend(cyclomatic)
        elif any(Path(item).suffix.lower() == ".py" for item in files):
            limitations.append("未测量圈复杂度")
    return raws, limitations
