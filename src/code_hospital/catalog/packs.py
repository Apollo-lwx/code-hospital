"""随包 semgrep 小规则。Finding.tool 写仪器 id，不写 semgrep。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..adapters.base import AdapterError, RawFinding, _run

_RULES = Path(__file__).with_name("rules")
_PACKS = {
    "java-pack": _RULES / "java-pack.yml",
    "hotspot-ruler": _RULES / "hotspot-ruler.yml",
}
_SYMBOL_KEYS = ("$FUNC", "$METHOD", "$RES", "$VAR", "$NAME")


def pack_path(instrument_id: str) -> Path:
    path = _PACKS.get(instrument_id)
    if path is None or not path.is_file():
        raise AdapterError(f"没有随包规则：{instrument_id}")
    return path


def tool_for_rule(rule_id: str, fallback: str) -> str:
    if rule_id.startswith("java."):
        return "java-pack"
    if rule_id.startswith("hotspot."):
        return "hotspot-ruler"
    return fallback


def semgrep_available() -> bool:
    return shutil.which("semgrep") is not None


def _symbol(extra: dict) -> str:
    metavars = extra.get("metavars") or {}
    for key in _SYMBOL_KEYS:
        meta = metavars.get(key) or {}
        text = str(meta.get("abstract_content") or "").strip()
        if text and text not in {"null", "..."}:
            return text[:80]
    return ""


def parse_pack_json(payload: str, instrument_id: str) -> list[RawFinding]:
    try:
        doc = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        raise AdapterError(f"semgrep 输出解析失败：{exc}") from exc
    findings: list[RawFinding] = []
    for row in doc.get("results") or []:
        extra = row.get("extra") or {}
        start = row.get("start") or {}
        end = row.get("end") or {}
        rule_id = str(row.get("check_id") or "unknown")
        short = rule_id
        for prefix in ("java.", "hotspot."):
            index = rule_id.find(prefix)
            if index >= 0:
                short = rule_id[index:]
                break
        severity = {"ERROR": "major", "WARNING": "minor", "INFO": "info"}.get(
            str(extra.get("severity", "")).upper(), "info"
        )
        findings.append(RawFinding(
            rule_id=short,
            message=str(extra.get("message") or ""),
            file_path=str(row.get("path") or "").replace("\\", "/"),
            line_start=int(start.get("line") or 0),
            line_end=int(end.get("line") or start.get("line") or 0),
            severity=severity,
            extra={"symbol": _symbol(extra), "instrument_id": instrument_id},
        ))
    return findings


async def scan_pack(repo_root: str, files: list[str], instrument_id: str, timeout_ms: int) -> list[RawFinding]:
    if not semgrep_available():
        raise AdapterError("semgrep 未到岗")
    if not files:
        return []
    config = pack_path(instrument_id)
    out, err = await _run(
        ["semgrep", "scan", "--json", "--quiet", "--metrics=off", "--config", str(config), "--", *files],
        cwd=repo_root,
        timeout=max(timeout_ms / 1000, 1),
    )
    if not out.strip():
        raise AdapterError(f"{instrument_id} 执行失败：{err[:200] or '无输出'}")
    return parse_pack_json(out, instrument_id)


async def scan_rules(
    repo_root: str, files: list[str], config: str, instrument_id: str, timeout_ms: int,
) -> list[RawFinding]:
    """跑仓库内的一份 semgrep 规则。配置路径由外援名册事先限定在仓库里。"""
    if not semgrep_available():
        raise AdapterError("semgrep 未到岗")
    if not files:
        return []
    out, err = await _run(
        ["semgrep", "scan", "--json", "--quiet", "--metrics=off", "--config", config, "--", *files],
        cwd=repo_root,
        timeout=max(timeout_ms / 1000, 1),
    )
    if not out.strip():
        raise AdapterError(f"{instrument_id} 执行失败：{err[:200] or '无输出'}")
    return parse_pack_json(out, instrument_id)
