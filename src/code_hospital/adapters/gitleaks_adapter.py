"""gitleaks 适配器：密钥泄露快扫（急诊分诊预检也用它）。

gitleaks dir 按目录扫描；文件级过滤由扫描师按 scope 清单完成。
证据一律脱敏（mask_secret），绝不把明文密钥写进病历。
"""

from __future__ import annotations

import json
import os
import tempfile

from ..safety.sanitize import mask_secret
from .base import AdapterError, RawFinding, ScannerAdapter, _run


def parse_gitleaks_json(payload: str) -> list[RawFinding]:
    try:
        rows = json.loads(payload or "[]")
    except json.JSONDecodeError as e:
        raise AdapterError(f"gitleaks 输出解析失败：{e}")
    findings: list[RawFinding] = []
    for row in rows:
        findings.append(
            RawFinding(
                rule_id=row.get("RuleID", "secret"),
                message=row.get("Description") or "疑似硬编码密钥",
                file_path=(row.get("File") or "").replace("\\", "/"),
                line_start=int(row.get("StartLine") or 0),
                line_end=int(row.get("EndLine") or row.get("StartLine") or 0),
                severity="critical",
                evidence=mask_secret(row.get("Secret") or ""),
            )
        )
    return findings


class GitleaksAdapter(ScannerAdapter):
    name = "gitleaks"
    binary = "gitleaks"
    extensions = frozenset()  # 全文件

    async def scan(self, repo_root: str, files: list[str], timeout_ms: int) -> list[RawFinding]:
        if not files:
            return []
        # gitleaks dir 无文件级参数：扫仓库根目录后按 scope 过滤（P0 简化）
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            report_path = tmp.name
        try:
            await _run(
                [self.binary, "dir", "--no-banner", "--exit-code", "0",
                 "--report-format", "json", "--report-path", report_path, "."],
                cwd=repo_root,
                timeout=timeout_ms / 1000,
            )
            with open(report_path, encoding="utf-8") as f:
                payload = f.read()
        finally:
            os.unlink(report_path) if os.path.exists(report_path) else None
        wanted = {f.replace("\\", "/") for f in files}
        return [r for r in parse_gitleaks_json(payload) if r.file_path in wanted]
