"""ruff 适配器：`ruff check --output-format json`。"""

from __future__ import annotations

import json
import re

from .base import AdapterError, RawFinding, ScannerAdapter, _run


def map_severity(code: str) -> str:
    """ruff 规则粗映射：F 加数字（pyflakes）/E9（语法级）→ major；E/W → minor；其余 → nit。"""
    head = code.split(".")[-1]
    if re.fullmatch(r"F\d+", head) or head.startswith("E9"):
        return "major"
    if code.startswith(("E", "W")):
        return "minor"
    return "nit"


def ruff_check_args(select: tuple[str, ...]) -> list[str]:
    args = ["check", "--output-format", "json", "--no-cache", "--quiet"]
    if select:
        args.append("--select=" + ",".join(select))
    return args


# pep8-naming（N8xx）的消息固定把符号名放在第一对反引号里：
# "Variable `visit_Foo` in class scope should not be mixedCase" → visit_Foo。
# 正骨的守门、豁免、升档都要符号名；行文本兜底（_symbol_hint）只认 def/class，
# 赋值形态的类属性（visit_Foo = ...）提不到，必须从这里补。
_MESSAGE_SYMBOL = re.compile(r"`([^`]+)`")


def _n8_symbol(code: str, message: str) -> str:
    if not code.startswith("N8"):
        return ""
    hit = _MESSAGE_SYMBOL.search(message or "")
    return hit.group(1).strip() if hit else ""


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
        message = row.get("message", "")
        symbol = _n8_symbol(code, message)
        findings.append(
            RawFinding(
                rule_id=code,
                message=message,
                file_path=(row.get("filename") or "").replace("\\", "/"),
                line_start=int(loc.get("row") or 0),
                line_end=int(end.get("row") or loc.get("row") or 0),
                severity=map_severity(code),
                extra={"symbol": symbol} if symbol else {},
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
        from ..catalog.policy import load_policy

        command = [self.binary, *ruff_check_args(load_policy(repo_root).ruff_select), *targets]
        out, err = await _run(command, cwd=repo_root, timeout=timeout_ms / 1000)
        # ruff 有发现时 returncode=1，属正常；两者都空才是故障
        if not out.strip() and err.strip():
            raise AdapterError(f"ruff 执行失败：{err[:200]}")
        return parse_ruff_json(out)
