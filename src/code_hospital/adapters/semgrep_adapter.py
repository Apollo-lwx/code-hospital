"""semgrep 适配器：`semgrep scan --json`。

规则源优先级：仓库本地 `.semgrep.yml` / `.semgrep/` > 注册表 `auto`（需网络）。
进程外调用二进制（Semgrep CLI 为 LGPL-2.1，不链入代码）。
"""

from __future__ import annotations

import json
import os

from .base import AdapterError, RawFinding, ScannerAdapter, _run

_SEVERITY_MAP = {"ERROR": "major", "WARNING": "minor", "INFO": "info"}


def parse_semgrep_json(payload: str) -> list[RawFinding]:
    try:
        doc = json.loads(payload or "{}")
    except json.JSONDecodeError as e:
        raise AdapterError(f"semgrep 输出解析失败：{e}")
    findings: list[RawFinding] = []
    for row in doc.get("results", []):
        extra = row.get("extra") or {}
        start = row.get("start") or {}
        end = row.get("end") or {}
        findings.append(
            RawFinding(
                rule_id=row.get("check_id", "unknown"),
                message=extra.get("message", ""),
                file_path=(row.get("path") or "").replace("\\", "/"),
                line_start=int(start.get("line") or 0),
                line_end=int(end.get("line") or start.get("line") or 0),
                severity=_SEVERITY_MAP.get(str(extra.get("severity", "")).upper(), "info"),
            )
        )
    return findings


class SemgrepAdapter(ScannerAdapter):
    name = "semgrep"
    binary = "semgrep"
    extensions = frozenset()  # 全语言

    async def scan(self, repo_root: str, files: list[str], timeout_ms: int) -> list[RawFinding]:
        if not files:
            return []
        config = "auto"
        for local in (".semgrep.yml", ".semgrep.yaml", ".semgrep"):
            if os.path.exists(os.path.join(repo_root, local)):
                config = local
                break
        out, err = await _run(
            [self.binary, "scan", "--json", "--quiet", "--metrics=off",
             "--config", config, "--", *files],
            cwd=repo_root,
            timeout=timeout_ms / 1000,
        )
        if not out.strip():
            raise AdapterError(f"semgrep 执行失败：{err[:200] or '无输出'}")
        return parse_semgrep_json(out)
