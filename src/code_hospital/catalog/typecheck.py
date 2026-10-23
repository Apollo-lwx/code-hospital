"""类型尺：点名后进程外调用 mypy，没有 mypy 时改用 pyright。"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from ..adapters.base import AdapterError, RawFinding, _run

_MYPY_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?:\d+:)?\s*"
    r"(?P<sev>error|warning|note):\s*(?P<msg>.*?)"
    r"(?:\s+\[(?P<code>[A-Za-z0-9_-]+)\])?\s*$"
)
_SEVERITY = {"error": "major", "warning": "minor", "note": "minor", "information": "minor"}


def type_backend() -> str:
    """优先 mypy。两边都不在 PATH 时返回空串。"""
    if shutil.which("mypy"):
        return "mypy"
    if shutil.which("pyright"):
        return "pyright"
    return ""


def _relative(project_root: str, path: str) -> str:
    text = str(path or "").replace("\\", "/")
    candidate = Path(text)
    if not candidate.is_absolute():
        return text.removeprefix("./")
    try:
        return candidate.resolve().relative_to(Path(project_root).resolve()).as_posix()
    except ValueError:
        return candidate.name


def parse_mypy(payload: str, project_root: str) -> list[RawFinding]:
    findings: list[RawFinding] = []
    for line in (payload or "").splitlines():
        matched = _MYPY_LINE.match(line.strip())
        if not matched:
            continue
        severity = matched.group("sev")
        code = matched.group("code") or f"type.{severity}"
        row = int(matched.group("line"))
        findings.append(RawFinding(
            rule_id=code,
            message=matched.group("msg").strip(),
            file_path=_relative(project_root, matched.group("file")),
            line_start=row,
            line_end=row,
            severity=_SEVERITY.get(severity, "minor"),
        ))
    return findings


def parse_pyright(payload: str, project_root: str) -> list[RawFinding]:
    try:
        doc = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        raise AdapterError(f"pyright 输出解析失败：{exc}") from exc
    findings: list[RawFinding] = []
    for item in doc.get("generalDiagnostics") or []:
        span = ((item.get("range") or {}).get("start") or {})
        end = ((item.get("range") or {}).get("end") or {})
        start_line = int(span.get("line") or 0) + 1
        end_line = int(end.get("line") or span.get("line") or 0) + 1
        severity = str(item.get("severity") or "error")
        findings.append(RawFinding(
            rule_id=str(item.get("rule") or f"type.{severity}"),
            message=str(item.get("message") or ""),
            file_path=_relative(project_root, str(item.get("file") or "")),
            line_start=start_line,
            line_end=max(start_line, end_line),
            severity=_SEVERITY.get(severity, "minor"),
        ))
    return findings


async def scan_type_ruler(project_root: str, files: list[str], timeout_ms: int) -> list[RawFinding]:
    backend = type_backend()
    if not backend:
        raise AdapterError("类型尺未到岗")
    timeout = max(timeout_ms / 1000, 1)
    if backend == "mypy":
        cache = tempfile.mkdtemp(prefix="ch-mypy-")
        try:
            out, err = await _run(
                [
                    "mypy",
                    "--hide-error-context",
                    "--no-color-output",
                    "--show-column",
                    "--no-error-summary",
                    "--follow-imports=silent",
                    f"--cache-dir={cache}",
                    *files,
                ],
                cwd=project_root,
                timeout=timeout,
            )
        finally:
            shutil.rmtree(cache, ignore_errors=True)
        if not out.strip() and "Traceback" in err:
            tail = err.strip().splitlines()[-1][:200]
            raise AdapterError(tail or "mypy 执行失败")
        return parse_mypy(out, project_root)
    out, err = await _run(
        ["pyright", "--outputjson", *files],
        cwd=project_root,
        timeout=timeout,
    )
    if not out.strip():
        tail = err.strip().splitlines()[-1][:200] if err.strip() else "pyright 无输出"
        raise AdapterError(tail)
    return parse_pyright(out, project_root)
