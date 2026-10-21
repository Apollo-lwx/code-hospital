"""检验科 Scanner（设计 §2.2 / §3.2 节点 2）。

调度确定性设备 → 归一化 Finding（指纹+上下文锚+净化）→ 检验报告（PR 可贴）。
只读仓库代码；绝不带 fix。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..adapters import all_adapters, register_defaults
from ..adapters.base import AdapterError, RawFinding, ScannerAdapter
from ..core.errors import invalid_operation, not_found
from ..core.fingerprint import context_anchor as make_anchor
from ..core.fingerprint import fingerprint as make_fingerprint
from ..core.ids import new_id
from ..core.models import Finding, ReportRecord
from ..safety.repo_files import scope_file_list
from ..safety.sanitize import sanitize_evidence, sanitize_text
from ..storage.db import Database, hospital_root, now_ms
from ..storage.reports import render_scan_report


def detect_acceptance_commands(project_root: str) -> list[dict]:
    """验收命令零配置探测（设计 §4.6 冷启动）：探测≠执行。"""
    root = Path(project_root)
    suggestions: list[dict] = []
    if (root / "pytest.ini").exists() or (
        (root / "pyproject.toml").exists()
        and "[tool.pytest" in (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
    ):
        suggestions.append({"alias": "test", "command": "pytest -q", "source": "pytest.ini/pyproject.toml"})
    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")) or {}).get("scripts") or {}
            if "test" in scripts:
                suggestions.append({"alias": "test", "command": "npm test", "source": "package.json"})
        except json.JSONDecodeError:
            pass
    if (root / "go.mod").exists():
        suggestions.append({"alias": "test", "command": "go test ./...", "source": "go.mod"})
    return suggestions


def _read_window(project_root: str, file_path: str, line: int) -> tuple[str, list[str], list[str]]:
    """读取定位行内容与前后各 2 行（证据 + 上下文锚）。文件缺失时返回空。"""
    p = Path(project_root) / file_path
    if not p.is_file() or line <= 0:
        return "", [], []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", [], []
    idx = line - 1
    if idx >= len(lines):
        return "", [], []
    before = lines[max(0, idx - 2): idx]
    after = lines[idx + 1: idx + 3]
    return lines[idx], before, after


def _symbol_hint(line_text: str) -> str:
    """P0 简化符号提示：取行内 def/class 名；二期接 LSP 后替换。"""
    text = line_text.strip()
    for kw in ("def ", "class ", "function ", "func "):
        if text.startswith(kw):
            name = text[len(kw):].split("(")[0].split(":")[0].strip()
            return name
    return ""


def normalize(
    raw: RawFinding, tool: str, case_id: str, report_id: str, project_root: str
) -> Finding:
    """RawFinding → Finding：净化、证据补全、上下文锚、指纹（设计 §4.5）。"""
    file_path = raw.file_path.replace("\\", "/")
    abs_p = Path(file_path)
    if abs_p.is_absolute():
        # 部分设备（如 ruff 0.17）输出绝对路径：契约要求仓库相对 POSIX 路径
        try:
            file_path = abs_p.resolve().relative_to(Path(project_root).resolve()).as_posix()
        except ValueError:
            file_path = file_path[2:] if file_path.startswith("./") else file_path
    elif file_path.startswith("./"):
        file_path = file_path[2:]
    line_text, before, after = _read_window(project_root, file_path, raw.line_start)
    evidence = raw.evidence or line_text
    anchor = make_anchor(before, after)
    symbol = _symbol_hint(line_text)
    return Finding(
        id=new_id("fnd"),
        case_id=case_id,
        report_id=report_id,
        tool=tool,
        rule_id=raw.rule_id,
        severity=raw.severity,
        file_path=file_path,
        line_start=raw.line_start,
        line_end=raw.line_end or raw.line_start,
        symbol=symbol,
        message=sanitize_text(raw.message),
        evidence=sanitize_evidence(evidence),
        fingerprint=make_fingerprint(raw.rule_id, file_path, symbol, evidence, anchor),
        context_anchor=anchor,
        status="new",
        origin="initial",
        first_seen_round=1,
        last_seen_round=1,
    )


async def scan(
    case_id: str,
    tools: list[str] | None = None,
    timeout_ms: int = 120_000,
    page: int = 1,
    size: int = 50,
    order_by: str = "severity desc",
    project_root: str = "",
) -> dict:
    """hosp_scan 主逻辑，返回契约 §3.2 出参。"""
    from .reception import resolve_project_root

    # 注册表为空时才装默认适配器——测试注入替身后不再混入真实设备
    if not all_adapters():
        register_defaults()
    root = resolve_project_root(project_root)
    async with Database(root) as db:
        case = await db.get_case(case_id)
        if case is None:
            raise not_found(f"病例不存在：{case_id}", target="case_id",
                            next_action="先 hosp_register 挂号")
        if case.status != "registered":
            raise invalid_operation(
                f"病例当前状态为 {case.status}，P0 仅支持 registered 状态下检验",
                target="case_id",
                next_action="P1 起支持复诊复扫（内嵌于 hosp_discharge）",
            )

        files, skipped = await scope_file_list(root, case.scope_type, case.scope.model_dump())
        report_id = new_id("rpt")
        if skipped:
            limitations_note = f"忽略规则跳过 {skipped} 个文件（内置排除目录 + code-hospital.yaml scan_ignore）"
        else:
            limitations_note = ""

        # 设备调度：auto = 全部可用适配器；指定 tools 时按名过滤
        registry: list[ScannerAdapter] = all_adapters()
        if tools and tools != ["auto"]:
            wanted = {t.strip() for t in tools}
            registry = [a for a in registry if a.name in wanted]
        devices, limitations, findings = [], [], []
        if limitations_note:
            limitations.append(limitations_note)
        for adapter in registry:
            available = adapter.is_available()
            version = await adapter.version() if available else ""
            devices.append({"tool": adapter.name, "available": available, "version": version})
            if not available:
                limitations.append(f"设备不可用: {adapter.name}（未安装或不在 PATH）")
                continue
            targets = adapter.filter_files(files)
            if not targets:
                limitations.append(f"{adapter.name}: 本 scope 无适配文件，跳过")
                continue
            try:
                raws = await adapter.scan(root, targets, timeout_ms)
                findings.extend(normalize(r, adapter.name, case_id, report_id, root) for r in raws)
            except AdapterError as e:
                limitations.append(f"{adapter.name}: {e}")

        # 排序：severity 优先（报告与出参一致）
        from ..core.models import SEVERITY_ORDER
        findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.file_path, f.line_start))

        await db.insert_findings(findings)

        # 检验后分诊升级（设计 §3.2：只影响审批默认值，不动状态机；事件落 reports）
        triage_upgrade: dict = {}
        critical_hits = sum(1 for f in findings if f.severity == "critical")
        if critical_hits > 0 and case.triage_level != "emergency":
            triage_upgrade = {
                "from": case.triage_level,
                "to": "emergency",
                "reason": f"检出 {critical_hits} 条 critical 发现（含密钥/高危特征）",
            }
            await db.update_case(case_id, triage_level="emergency")
            await db.insert_report(ReportRecord(
                id=new_id("rpt"), case_id=case_id, stage="triage_upgrade", round=1,
                md_path="", summary=json.dumps(triage_upgrade, ensure_ascii=False),
                created_at=now_ms(),
            ))
            case.triage_level = "emergency"

        # 检验报告（PR 可贴）
        case.updated_at = now_ms()
        md = render_scan_report(case, report_id, devices, findings, limitations)
        report_path = hospital_root(root) / "reports" / case_id / "scan-r1.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md, encoding="utf-8")
        await db.insert_report(ReportRecord(
            id=report_id, case_id=case_id, stage="scan", round=1,
            md_path=str(report_path),
            summary=f"{len(findings)} 条发现（critical {critical_hits}）",
            created_at=now_ms(),
        ))
        await db.update_case(case_id, status="scanned")

        # 分页出参
        page_items, total = await db.list_findings(case_id, page=page, size=size, order_by=order_by)
        from ..core.models import page_payload

        return {
            "case_id": case_id,
            "report_id": report_id,
            "round": 1,
            "devices": devices,
            "findings": page_payload(
                [f.model_dump() for f in page_items], page, size, order_by, total
            ),
            "triage_upgrade": triage_upgrade,
            "suggested_acceptance_commands": detect_acceptance_commands(root),
            "report_path": str(report_path),
        }
