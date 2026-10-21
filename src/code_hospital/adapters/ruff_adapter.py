"""ruff 适配器：`ruff check --output-format json`。"""

from __future__ import annotations

import json

from .base import AdapterError, RawFinding, ScannerAdapter, _run


def map_severity(code: str) -> str:
    """ruff 规则粗映射：F（pyflakes）/E9（语法级）→ major；E/W → minor；其余 → nit。"""
    if code.startswith("F") or code.startswith("E9"):
        return "major"
    if code.startswith(("E", "W")):
        return "minor"
    return "nit"


def parse_ruff_json(payload: str) -> list[RawFinding]:
    """解析 ruff JSON 输出（纯函数，便于单测）。"""
    try:
        rows = json.loads(payload or "[]")
    except json.JSONDecodeError as e:
        raise AdapterError(f"ruff 输出解析失败：{e}")
    findings: list[RawFinding] = []
    for row in rows:
        code = row.get("code") or "syntax-error"
        loc = row.get("location") or {}
        end = row.get("end_location") or {}
        findings.append(
            RawFinding(
                rule_id=code,
                message=row.get("message", ""),
                file_path=(row.get("filename") or "").replace("\\", "/"),
                line_start=int(loc.get("row") or 0),
                line_end=int(end.get("row") or loc.get("row") or 0),
                severity=map_severity(code),
            )
        )
    return findings


class RuffAdapter(ScannerAdapter):
    name = "ruff"
    binary = "ruff"
    extensions = frozenset({".py"})

    async def scan(self, repo_root: str, files: list[str], timeout_ms: int) -> list[RawFinding]:
        targets = self.filter_files(files)
        if not targets:
            return []
        out, err = await _run(
            [self.binary, "check", "--output-format", "json", "--no-cache", "--quiet", *targets],
            cwd=repo_root,
            timeout=timeout_ms / 1000,
        )
        # ruff 有发现时 returncode=1，属正常；两者都空才是故障
        if not out.strip() and err.strip():
            raise AdapterError(f"ruff 执行失败：{err[:200]}")
        return parse_ruff_json(out)
