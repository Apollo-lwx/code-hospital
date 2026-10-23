"""ESLint 与 jscpd。进程外调用，缺二进制或配置记终态限制。"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from ..adapters.base import AdapterError, RawFinding, _run

_ESLINT_FILES = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
)
_JS = {".js", ".jsx", ".ts", ".tsx"}


def eslint_configured(project_root: str) -> bool:
    root = Path(project_root)
    if any((root / name).is_file() for name in _ESLINT_FILES):
        return True
    package = root / "package.json"
    if not package.is_file():
        return False
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and "eslintConfig" in data


def eslint_binary() -> str:
    return shutil.which("eslint") or ""


def _relative(project_root: str, path: str) -> str:
    text = str(path or "").replace("\\", "/")
    candidate = Path(text)
    if not candidate.is_absolute():
        return text[2:] if text.startswith("./") else text
    try:
        return candidate.resolve().relative_to(Path(project_root).resolve()).as_posix()
    except ValueError:
        return candidate.as_posix()


def parse_eslint_json(payload: str, project_root: str) -> list[RawFinding]:
    try:
        doc = json.loads(payload or "[]")
    except json.JSONDecodeError as exc:
        raise AdapterError(f"eslint 输出解析失败：{exc}") from exc
    if isinstance(doc, dict):
        doc = doc.get("results") or []
    findings: list[RawFinding] = []
    for file_row in doc:
        rel = _relative(project_root, str(file_row.get("filePath") or ""))
        for message in file_row.get("messages") or []:
            rule_id = str(message.get("ruleId") or "")
            if not rule_id:
                continue
            line = int(message.get("line") or 1)
            end = int(message.get("endLine") or line)
            severity = "major" if int(message.get("severity") or 1) >= 2 else "minor"
            findings.append(RawFinding(
                rule_id=rule_id,
                message=str(message.get("message") or ""),
                file_path=rel,
                line_start=line,
                line_end=end,
                severity=severity,
                extra={"fixable": bool(message.get("fix"))},
            ))
    return findings


async def scan_eslint(project_root: str, files: list[str], timeout_ms: int) -> list[RawFinding]:
    binary = eslint_binary()
    if not binary:
        raise AdapterError("eslint 未到岗")
    targets = [path for path in files if Path(path).suffix.lower() in _JS]
    if not targets:
        return []
    out, err = await _run(
        [binary, "--format", "json", "--no-error-on-unmatched-pattern", *targets],
        cwd=project_root,
        timeout=max(timeout_ms / 1000, 1),
    )
    if not out.strip():
        raise AdapterError(f"eslint 执行失败：{err[:200] or '无输出'}")
    return parse_eslint_json(out, project_root)


def parse_jscpd_json(payload: str, project_root: str) -> list[RawFinding]:
    try:
        doc = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        raise AdapterError(f"jscpd 输出解析失败：{exc}") from exc
    clones = doc.get("duplicates") or doc.get("clones") or []
    findings: list[RawFinding] = []
    for row in clones:
        first = row.get("firstFile") or {}
        second = row.get("secondFile") or {}
        start_loc = first.get("startLoc") or {}
        end_loc = first.get("endLoc") or {}
        start = int(first.get("start") or start_loc.get("line") or 1)
        end = int(first.get("end") or end_loc.get("line") or start)
        other = _relative(project_root, str(second.get("name") or ""))
        findings.append(RawFinding(
            rule_id="clone.duplicate",
            message=f"与 {other} 重复 {row.get('lines') or 0} 行",
            file_path=_relative(project_root, str(first.get("name") or "")),
            line_start=start,
            line_end=max(start, end),
            severity="major",
        ))
    return findings


async def scan_jscpd(project_root: str, files: list[str], timeout_ms: int) -> list[RawFinding]:
    binary = shutil.which("jscpd")
    if not binary:
        raise AdapterError("jscpd 未到岗")
    if not files:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        out, err = await _run(
            [binary, "--silent", "--min-lines", "5", "--reporters", "json", "--output", tmp, *files],
            cwd=project_root,
            timeout=max(timeout_ms / 1000, 1),
        )
        report = Path(tmp) / "jscpd-report.json"
        payload = report.read_text(encoding="utf-8") if report.is_file() else out
    if not str(payload).strip():
        raise AdapterError(f"jscpd 执行失败：{err[:200] or '无输出'}")
    return parse_jscpd_json(payload, project_root)
