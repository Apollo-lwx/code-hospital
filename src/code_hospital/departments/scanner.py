"""检验科 Scanner（设计 §2.2 / §3.2 节点 2）。

调度确定性设备 → 归一化 Finding（指纹+上下文锚+净化）→ 检验报告（PR 可贴）。
只读仓库代码；绝不带 fix。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from ..adapters import all_adapters, register_defaults
from ..adapters.base import AdapterError, RawFinding, ScannerAdapter
from ..core.algorithms import lookup_history, stamp_findings
from ..core.errors import invalid_operation, not_found
from ..core.fingerprint import context_anchor as make_anchor
from ..core.ids import new_id
from ..core.models import Finding, ReportRecord
from ..safety.repo_files import scope_file_list
from ..safety.sanitize import sanitize_evidence, sanitize_text
from ..storage.db import Database, hospital_root, now_ms
from ..storage.reports import render_scan_report


async def collect_findings(
    root: str,
    case,
    report_id: str,
    tools: list[str] | None = None,
    timeout_ms: int = 120_000,
    prior_coverage: dict | None = None,
) -> tuple[list[dict], list[str], list[Finding], dict]:
    """跑当前注册的设备并盖双键指纹。不入库，初诊和复诊共用。"""
    from ..core.models import SEVERITY_ORDER

    files, skipped = await scope_file_list(root, case.scope_type, case.scope.model_dump())
    limitations: list[str] = []
    if skipped:
        limitations.append(
            f"忽略规则跳过 {skipped} 个文件（内置排除目录 + code-hospital.yaml scan_ignore）"
        )
    registry: list[ScannerAdapter] = all_adapters()
    from ..catalog.formulary import instruments as catalog_instruments
    from ..catalog.formulary import selected_instrument_ids
    from ..catalog.structure import scan_structure

    catalog_ids = set(catalog_instruments())
    if tools and tools != ["auto"]:
        wanted = {t.strip() for t in tools}
        registry = [a for a in registry if a.name in wanted]
        selected = wanted
    else:
        selected = set(selected_instrument_ids(
            case.triage_level, files,
            getattr(case, "requested_specialties", []),
            getattr(case, "requested_instruments", []),
            getattr(case, "visit_kind", "checkup"),
        ))
        registry = [a for a in registry if a.name not in catalog_ids or a.name in selected]
    devices: list[dict] = []
    findings: list[Finding] = []
    for adapter in registry:
        available = adapter.is_available()
        version = await adapter.version() if available else ""
        if not available:
            devices.append({"tool": adapter.name, "available": False, "version": "", "outcome": "terminal"})
            if adapter.name in catalog_ids:
                limitations.append(catalog_instruments()[adapter.name]["limitation"])
            else:
                limitations.append(f"设备不可用: {adapter.name}（未安装或不在 PATH）")
            continue
        targets = adapter.filter_files(files)
        if not targets and adapter.extensions:
            devices.append({"tool": adapter.name, "available": True, "version": version, "outcome": "ok"})
            limitations.append(f"{adapter.name}: 本 scope 无适配文件，跳过")
            continue
        try:
            raws = await adapter.scan(root, targets or files, timeout_ms)
            findings.extend(normalize(r, adapter.name, case.id, report_id, root) for r in raws)
            devices.append({"tool": adapter.name, "available": True, "version": version, "outcome": "ok"})
        except AdapterError as e:
            devices.append({"tool": adapter.name, "available": True, "version": version, "outcome": "retry"})
            limitations.append(f"{adapter.name}: {e}")
    if "structure-ruler" in selected:
        raws, extra = scan_structure(root, files)
        limitations.extend(extra)
        for raw in raws:
            finding = normalize(
                RawFinding(
                    rule_id=raw["rule_id"], message=raw["message"], file_path=raw["file_path"],
                    line_start=raw["line_start"], line_end=raw["line_end"], severity=raw["severity"],
                ),
                "structure-ruler", case.id, report_id, root,
            )
            if raw.get("symbol"):
                finding.symbol = raw["symbol"]
            findings.append(finding)
        devices.append({"tool": "structure-ruler", "available": True, "version": "builtin", "outcome": "ok"})
    _attach_depth(root, case, report_id, files, selected, devices, findings, limitations, timeout_ms)
    _attach_business(root, case, report_id, files, selected, devices, findings, limitations)
    _attach_severe(root, case, report_id, files, selected, devices, findings, limitations)
    _attach_engines(root, case, report_id, files, selected, devices, findings, limitations, timeout_ms)
    await _run_catalog_instruments(
        root, case, report_id, files, selected, devices, findings, limitations, timeout_ms,
    )
    if "design" in getattr(case, "requested_specialties", []):
        limitations.append("设计科第一期无检验仪器")
    from ..catalog.policy import describe, keeps, load_policy

    policy = load_policy(root)
    before = len(findings)
    findings = [item for item in findings if keeps(item.rule_id, item.severity, policy)]
    limitations.append(describe(policy, before - len(findings)))
    from ..catalog.adjudicate import adjudicate_findings
    from ..catalog.coverage import build_ledger
    from ..catalog.visits import coverage_line

    adjudicate_findings(root, findings)
    full_scan = not tools or tools == ["auto"]
    ledger_lines, ledger = build_ledger(
        getattr(case, "visit_kind", "checkup") or "checkup",
        devices, root, files, prior_coverage, full_scan,
    )
    limitations.extend(ledger_lines)
    limitations.append(coverage_line(
        getattr(case, "visit_kind", "checkup") or "checkup",
        sorted(selected),
        devices,
    ))
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.file_path, f.line_start))
    stamp_findings(findings)
    return devices, limitations, findings, ledger


def detect_acceptance_commands(project_root: str) -> list[dict]:
    """验收命令零配置探测（设计 §4.6 冷启动）：探测≠执行。"""
    root = Path(project_root)
    suggestions: list[dict] = []
    ruff_name = "ruff.exe" if os.name == "nt" else "ruff"
    if shutil.which("ruff") or (Path(sys.executable).parent / ruff_name).is_file():
        suggestions.append({"alias": "lint", "command": "ruff check", "source": "ruff"})
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
    from ..catalog.packs import tool_for_rule

    tool = tool_for_rule(raw.rule_id, tool)
    symbol = str((raw.extra or {}).get("symbol") or "") or _symbol_hint(line_text)
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
        contract_id=str((raw.extra or {}).get("contract_id") or ""),
        fixable=bool((raw.extra or {}).get("fixable")),
        message=sanitize_text(raw.message),
        evidence=sanitize_evidence(evidence),
        fingerprint="",
        context_anchor=anchor,
        status="new",
        origin="initial",
        first_seen_round=1,
        last_seen_round=1,
    )


_DEPTH_IDS = (
    "security-ruler", "defect-ruler", "perf-ruler", "algo-ruler", "supply-ruler", "test-gap",
)


def _attach_business(root, case, report_id, files, selected, devices, findings, limitations):
    """业务号。没有写下来的规则时记为未查，不当成正常。"""
    if "business-ruler" not in selected:
        return
    from ..catalog.business import scan_business

    raws, extra, outcome = scan_business(
        root, files, getattr(case, "chief_complaint", "") or "",
    )
    limitations.extend(extra)
    for raw in raws:
        findings.append(normalize(
            RawFinding(
                rule_id=raw["rule_id"], message=raw["message"], file_path=raw["file_path"],
                line_start=raw["line_start"], line_end=raw["line_end"], severity=raw["severity"],
                extra={"symbol": raw.get("symbol") or ""},
            ),
            "business-ruler", case.id, report_id, root,
        ))
    devices.append({
        "tool": "business-ruler",
        "available": outcome == "ok",
        "version": "builtin",
        "outcome": outcome,
    })


def _remember(limitations: list[str], notes: list[str]) -> None:
    for note in notes:
        if note and note not in limitations:
            limitations.append(note)


def _append_raws(root, case, report_id, findings, raws, tool: str) -> None:
    for raw in raws:
        findings.append(normalize(
            RawFinding(
                rule_id=raw["rule_id"], message=raw["message"], file_path=raw["file_path"],
                line_start=raw["line_start"], line_end=raw["line_end"], severity=raw["severity"],
                extra={"symbol": raw.get("symbol") or ""},
            ),
            tool, case.id, report_id, root,
        ))


def _attach_severe(root, case, report_id, files, selected, devices, findings, limitations):
    """重症号只看本次改动。没有 diff 时两把尺都记为没拍成。"""
    wanted = [item for item in ("taint-ruler", "impact-review") if item in selected]
    if not wanted:
        return
    from ..catalog.severe import resolve_change_set, scan_impact, scan_taint

    narrowed, notes, usable = resolve_change_set(
        root, files, getattr(case, "scope_type", "repo") or "repo",
    )
    _remember(limitations, notes)
    runners = {"taint-ruler": scan_taint, "impact-review": scan_impact}
    for instrument_id in wanted:
        if not usable:
            devices.append({
                "tool": instrument_id, "available": False, "version": "builtin", "outcome": "terminal",
            })
            continue
        raws, extra, outcome = runners[instrument_id](root, narrowed)
        _remember(limitations, extra)
        _append_raws(root, case, report_id, findings, raws, instrument_id)
        devices.append({
            "tool": instrument_id,
            "available": outcome == "ok",
            "version": "builtin",
            "outcome": outcome,
        })


def _attach_engines(root, case, report_id, files, selected, devices, findings, limitations, timeout_ms):
    """外部仪器只从登记表取。重症号把同一份 diff 交给这些引擎。"""
    from ..catalog.engines import engines_for
    from ..catalog.severe import resolve_change_set

    engines = engines_for(selected)
    if not engines:
        return
    scan_files = files
    if (getattr(case, "visit_kind", "") or "") == "severe":
        scan_files, notes, usable = resolve_change_set(
            root, files, getattr(case, "scope_type", "repo") or "repo",
        )
        _remember(limitations, notes)
        if not usable:
            for engine in engines:
                devices.append({
                    "tool": engine.instrument_id, "available": False, "version": "", "outcome": "terminal",
                })
            return
    timeout_s = min(90.0, max(timeout_ms, 0) / 1000)
    for engine in engines:
        raws, extra, outcome = engine.run(root, scan_files, timeout_s)
        _remember(limitations, extra)
        _append_raws(root, case, report_id, findings, raws, engine.instrument_id)
        devices.append({
            "tool": engine.instrument_id,
            "available": outcome == "ok",
            "version": engine.instrument_id,
            "outcome": outcome,
        })


def _attach_depth(root, case, report_id, files, selected, devices, findings, limitations, timeout_ms):
    """号别点名的院内深度尺。外部漏洞扫描器没到岗时只留下限制项。"""
    wanted = [item for item in _DEPTH_IDS if item in selected]
    if not wanted:
        return
    from ..catalog.depth import run_supply_cve, scan_depth

    grouped, extra = scan_depth(
        root, files, wanted,
        scope_type=getattr(case, "scope_type", "repo"),
        visit_kind=getattr(case, "visit_kind", "checkup") or "checkup",
    )
    limitations.extend(extra)
    if "supply-ruler" in wanted:
        cve_raws, cve_notes = run_supply_cve(root, min(20.0, max(timeout_ms, 0) / 1000))
        grouped.setdefault("supply-ruler", []).extend(cve_raws)
        limitations.extend(cve_notes)
    for instrument_id in wanted:
        for raw in grouped.get(instrument_id) or []:
            findings.append(normalize(
                RawFinding(
                    rule_id=raw["rule_id"], message=raw["message"], file_path=raw["file_path"],
                    line_start=raw["line_start"], line_end=raw["line_end"], severity=raw["severity"],
                    extra={"symbol": raw.get("symbol") or ""},
                ),
                instrument_id, case.id, report_id, root,
            ))
        devices.append({
            "tool": instrument_id, "available": True, "version": "builtin", "outcome": "ok",
        })


_PACK_EXTENSIONS = {
    "java-pack": {".java"},
    "hotspot-ruler": {".py", ".java"},
}


def _pack_targets(instrument_id: str, files: list[str]) -> list[str]:
    exts = _PACK_EXTENSIONS.get(instrument_id, set())
    return [path for path in files if Path(path).suffix.lower() in exts]


async def _run_catalog_instruments(root, case, report_id, files, selected, devices, findings, limitations, timeout_ms):
    """院内仪器不进适配器注册表。java-pack / hotspot 走随包 semgrep，分层尺只跑第一份契约。"""
    from ..catalog.formulary import instruments as catalog_instruments
    from ..catalog.layers import scan_layers
    from ..catalog.packs import scan_pack, semgrep_available
    from ..catalog.typecheck import type_backend

    catalog = catalog_instruments()
    for instrument_id in ("java-pack", "hotspot-ruler"):
        if instrument_id not in selected:
            continue
        targets = _pack_targets(instrument_id, files)
        if not semgrep_available():
            limitations.append(catalog[instrument_id]["limitation"])
            devices.append({"tool": instrument_id, "available": False, "version": "", "outcome": "terminal"})
            continue
        if not targets:
            devices.append({"tool": instrument_id, "available": True, "version": "semgrep", "outcome": "ok"})
            limitations.append(f"{instrument_id}: 本 scope 无适配文件，跳过")
            continue
        try:
            raws = await scan_pack(root, targets, instrument_id, timeout_ms)
            findings.extend(normalize(raw, instrument_id, case.id, report_id, root) for raw in raws)
            devices.append({"tool": instrument_id, "available": True, "version": "semgrep", "outcome": "ok"})
        except AdapterError as exc:
            devices.append({"tool": instrument_id, "available": True, "version": "semgrep", "outcome": "retry"})
            limitations.append(f"{instrument_id}: {exc}")

    requested = getattr(case, "requested_specialties", [])
    if "performance" in requested and "hotspot-ruler" not in selected and not semgrep_available():
        limitations.append("semgrep 未到岗，热点尺未拍")
        devices.append({"tool": "hotspot-ruler", "available": False, "version": "", "outcome": "terminal"})
    requested_instruments = list(getattr(case, "requested_instruments", []) or [])
    if "type-ruler" in requested_instruments and "type-ruler" not in selected and not type_backend():
        limitations.append("类型尺未到岗")
        devices.append({"tool": "type-ruler", "available": False, "version": "", "outcome": "terminal"})

    await _scan_eslint(root, report_id, case, files, selected, devices, findings, limitations, timeout_ms)
    await _scan_jscpd(root, report_id, case, files, selected, devices, findings, limitations, timeout_ms, catalog)
    await _scan_type_ruler(root, report_id, case, files, selected, devices, findings, limitations, timeout_ms)

    if "layer-ruler" not in selected:
        return
    raws, extra = await scan_layers(root, files, timeout_ms)
    limitations.extend(extra)
    for raw in raws:
        findings.append(normalize(
            RawFinding(
                rule_id=raw["rule_id"], message=raw["message"], file_path=raw["file_path"],
                line_start=raw["line_start"], line_end=raw["line_end"], severity=raw["severity"],
                extra={"contract_id": raw.get("contract_id") or ""},
            ),
            "layer-ruler", case.id, report_id, root,
        ))
    failed = any("失败" in item for item in extra)
    if failed:
        outcome = "retry"
    elif extra and not raws:
        outcome = "terminal"
    else:
        outcome = "ok"
    devices.append({"tool": "layer-ruler", "available": outcome != "terminal", "version": "builtin", "outcome": outcome})


async def _scan_eslint(root, report_id, case, files, selected, devices, findings, limitations, timeout_ms):
    if "eslint" not in selected:
        return
    from ..catalog.runners import eslint_binary, eslint_configured, scan_eslint

    targets = [path for path in files if Path(path).suffix.lower() in {".js", ".jsx", ".ts", ".tsx"}]
    if not eslint_configured(root):
        limitations.append("项目未配置 eslint")
        devices.append({"tool": "eslint", "available": False, "version": "", "outcome": "terminal"})
        return
    if not eslint_binary():
        limitations.append("eslint 未到岗")
        devices.append({"tool": "eslint", "available": False, "version": "", "outcome": "terminal"})
        return
    if not targets:
        devices.append({"tool": "eslint", "available": True, "version": "eslint", "outcome": "ok"})
        limitations.append("eslint: 本 scope 无适配文件，跳过")
        return
    try:
        raws = await scan_eslint(root, targets, timeout_ms)
        findings.extend(normalize(raw, "eslint", case.id, report_id, root) for raw in raws)
        devices.append({"tool": "eslint", "available": True, "version": "eslint", "outcome": "ok"})
    except AdapterError as exc:
        devices.append({"tool": "eslint", "available": True, "version": "eslint", "outcome": "retry"})
        limitations.append(f"eslint: {exc}")


async def _scan_type_ruler(root, report_id, case, files, selected, devices, findings, limitations, timeout_ms):
    if "type-ruler" not in selected:
        return
    from ..catalog.typecheck import scan_type_ruler, type_backend

    targets = [path for path in files if Path(path).suffix.lower() == ".py"]
    if not type_backend():
        limitations.append("类型尺未到岗")
        devices.append({"tool": "type-ruler", "available": False, "version": "", "outcome": "terminal"})
        return
    if not targets:
        devices.append({"tool": "type-ruler", "available": True, "version": type_backend(), "outcome": "ok"})
        limitations.append("type-ruler: 本 scope 无适配文件，跳过")
        return
    try:
        raws = await scan_type_ruler(root, targets, timeout_ms)
        findings.extend(normalize(raw, "type-ruler", case.id, report_id, root) for raw in raws)
        devices.append({"tool": "type-ruler", "available": True, "version": type_backend(), "outcome": "ok"})
    except AdapterError as exc:
        devices.append({"tool": "type-ruler", "available": True, "version": type_backend(), "outcome": "retry"})
        limitations.append(f"type-ruler: {exc}")


async def _scan_jscpd(root, report_id, case, files, selected, devices, findings, limitations, timeout_ms, catalog):
    if "jscpd" not in selected:
        return
    import shutil

    from ..catalog.runners import scan_jscpd

    exts = {f".{str(item).lstrip('.')}" for item in (catalog["jscpd"].get("extensions") or [])}
    targets = [path for path in files if Path(path).suffix.lower() in exts]
    if shutil.which("jscpd") is None:
        limitations.append(catalog["jscpd"]["limitation"])
        devices.append({"tool": "jscpd", "available": False, "version": "", "outcome": "terminal"})
        return
    if not targets:
        devices.append({"tool": "jscpd", "available": True, "version": "jscpd", "outcome": "ok"})
        return
    try:
        raws = await scan_jscpd(root, targets, timeout_ms)
        findings.extend(normalize(raw, "jscpd", case.id, report_id, root) for raw in raws)
        devices.append({"tool": "jscpd", "available": True, "version": "jscpd", "outcome": "ok"})
    except AdapterError as exc:
        devices.append({"tool": "jscpd", "available": True, "version": "jscpd", "outcome": "retry"})
        limitations.append(f"jscpd: {exc}")


async def scan(
    case_id: str,
    tools: list[str] | None = None,
    timeout_ms: int = 120_000,
    stage_confirmed: bool = False,
    page: int = 1,
    size: int = 50,
    order_by: str = "severity desc",
    project_root: str = "",
) -> dict:
    """hosp_scan 主逻辑，返回契约 §3.2 出参。

    阶段确认门禁（设计 §4.11）：病例挂起（pending_gate 非空）且未带
    stage_confirmed=true 时返回 pending_question 变体，不执行扫描。
    """
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

        # 叫号检查：挂号单或补片检查单待确认时挂起，用户确认后重调本工具放行
        if case.pending_gate and not stage_confirmed:
            from ..core.questions import stage_gate_question

            reports = await db.list_reports(case_id)
            reg_report = next((r for r in reports if r.stage == "register"), None)
            prompt_meta = (
                f"分诊 {case.triage_level}，范围 {case.scope_type}"
                f"（报告 {reg_report.md_path}）。"
                if reg_report and reg_report.md_path
                else f"分诊 {case.triage_level}，范围 {case.scope_type}。"
            )
            return stage_gate_question(case_id, case.pending_gate, "hosp_scan", prompt_meta)
        if case.pending_gate:
            await db.update_case(case_id, pending_gate="")
            case.pending_gate = ""

        if case.scan_orders and case.status == "scanned":
            return await _supplement(db, root, case, timeout_ms)

        if case.status != "registered":
            raise invalid_operation(
                f"病例当前状态为 {case.status}，仅 registered 可初检，scanned 且有检查单时可补片",
                target="case_id",
                next_action="初检走 hosp_scan；补片需诊断先开出 scan_orders",
            )

        report_id = new_id("rpt")
        prior = await db.prior_coverage(root, case_id)
        devices, limitations, findings, ledger = await collect_findings(
            root, case, report_id, tools, timeout_ms, prior,
        )
        files, _skipped_files = await scope_file_list(root, case.scope_type, case.scope.model_dump())
        from ..catalog.checks import run_checks, summarize_checks

        checks, check_notes = run_checks(root, files)
        limitations.extend(check_notes)
        limitations.append(summarize_checks(checks))

        # 初诊调档：复发看 content_key（长相），身份指纹只区分同形的多处命中。
        fingerprints = sorted({f.fingerprint for f in findings if f.fingerprint})
        content_keys = sorted({f.content_key for f in findings if f.content_key})
        raw_history = await db.find_fingerprint_history(
            root, fingerprints, exclude_case_id=case_id, content_keys=content_keys,
        )
        recurring = 0
        for f in findings:
            refs = lookup_history(raw_history, f.fingerprint, f.content_key)
            f.history_refs = [
                {**r, "note": f"此前病例曾出现同形态发现（复发记录 {i + 1}/{len(refs)}）"}
                for i, r in enumerate(refs)
            ]
            if f.history_refs:
                recurring += 1
        if recurring:
            limitations.append(f"复发提醒：{recurring} 条发现在历史病例中出现过（详见各条 history_refs）")

        await db.insert_findings(findings)

        # 检验后分诊升级（设计 §3.2：只影响审批默认值，不动状态机；事件落 reports）
        triage_upgrade: dict = {}
        critical_hits = sum(1 for f in findings if f.severity == "critical")
        if critical_hits > 0 and case.triage_level != "emergency":
            triage_upgrade = {
                "from": case.triage_level,
                "to": "emergency",
                "reason": f"检出 {critical_hits} 条必须先处理的问题（密钥或同类安全项）",
            }
            await db.update_case(case_id, triage_level="emergency")
            await db.insert_report(ReportRecord(
                id=new_id("rpt"), case_id=case_id, stage="triage_upgrade", round=1,
                md_path="", summary=json.dumps(triage_upgrade, ensure_ascii=False),
                created_at=now_ms(),
            ))
            case.triage_level = "emergency"

        # 既往史读取（.rsi 项目记忆：有帮助但不可全信，advisory 上下文）
        from ..storage.rsi_memory import load_memory_brief

        memory_context = load_memory_brief(root)

        # 检验报告（PR 可贴）
        case.updated_at = now_ms()
        md = render_scan_report(
            case, report_id, devices, findings, limitations, memory_context, checks,
        )
        report_path = hospital_root(root) / "reports" / case_id / "scan-r1.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md, encoding="utf-8")
        await db.insert_report(ReportRecord(
            id=report_id, case_id=case_id, stage="scan", round=1,
            md_path=str(report_path),
            summary=f"{len(findings)} 条发现，其中必须先处理 {critical_hits} 条",
            created_at=now_ms(),
        ))
        # 检验报告产出后挂起等确认（非委托病例），用户确认后才放行诊断（P1）
        await db.update_case(
            case_id,
            status="scanned",
            pending_gate="" if case.auto_advance else "scanned",
            instrument_notes_json=json.dumps(
                [{"id": d["tool"], "outcome": d.get("outcome", "ok")} for d in devices],
                ensure_ascii=False,
            ),
            coverage_json=json.dumps(ledger, ensure_ascii=False),
        )

        # 分页出参（DB 重读后重挂派生的 history_refs）
        page_items, total = await db.list_findings(case_id, page=page, size=size, order_by=order_by)
        for f in page_items:
            refs = lookup_history(raw_history, f.fingerprint, f.content_key)
            f.history_refs = [
                {**r, "note": f"此前病例曾出现同形态发现（复发记录 {i + 1}/{len(refs)}）"}
                for i, r in enumerate(refs)
            ]
        from ..core.models import page_payload
        from ..core.questions import handoff

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
            "memory_context": memory_context,
            "checks": checks,
            "report_path": str(report_path),
            "handoff": handoff(case_id, "scanned", case.auto_advance, next_tool="hosp_diagnose"),
        }


async def _supplement(db, root: str, case, timeout_ms: int) -> dict:
    """检查单只跑点名仪器。新发现挂初检 report_id，另写补片报告，不覆盖 scan-r1.md。"""
    from ..catalog.formulary import instruments as catalog_instruments
    from ..core.models import page_payload
    from ..core.questions import handoff

    reports = await db.list_reports(case.id)
    scan_report = next((r for r in reports if r.stage == "scan"), None)
    report_id = scan_report.id if scan_report else new_id("rpt")
    ordered = [row["instrument_id"] for row in case.scan_orders]
    devices, limitations, findings, ledger = await collect_findings(
        root, case, report_id, ordered, timeout_ms, case.coverage,
    )
    catalog = catalog_instruments()
    seen = {device["tool"] for device in devices}
    notes = [row for row in case.instrument_notes if row.get("id") not in set(ordered)]
    for instrument_id in ordered:
        if instrument_id in seen:
            device = next(item for item in devices if item["tool"] == instrument_id)
            notes.append({"id": instrument_id, "outcome": device.get("outcome", "ok")})
            continue
        text = catalog[instrument_id]["limitation"] if instrument_id in catalog else f"{instrument_id} 未到岗"
        limitations.append(text)
        notes.append({"id": instrument_id, "outcome": "terminal"})

    fingerprints = sorted({f.fingerprint for f in findings if f.fingerprint})
    content_keys = sorted({f.content_key for f in findings if f.content_key})
    raw_history = await db.find_fingerprint_history(
        root, fingerprints, exclude_case_id=case.id, content_keys=content_keys,
    )
    for finding in findings:
        refs = lookup_history(raw_history, finding.fingerprint, finding.content_key)
        finding.history_refs = [
            {**ref, "note": "此前病例曾出现同形态发现（复发）"} for ref in refs
        ]
    await db.insert_findings(findings)

    lines = [
        f"# 补片检验 {case.id}",
        "",
        "检查单：" + "；".join(
            f"{row['instrument_id']}（{row['reason']}）" for row in case.scan_orders
        ),
        "",
        f"新发现 {len(findings)} 条。",
    ]
    if limitations:
        lines.extend(["", "## 覆盖与限制", *[f"- {item}" for item in limitations]])
    report_path = hospital_root(root) / "reports" / case.id / f"scan-r{case.round}-supplement.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    supplement_id = new_id("rpt")
    await db.insert_report(ReportRecord(
        id=supplement_id, case_id=case.id, stage="scan_supplement", round=case.round,
        md_path=str(report_path),
        summary=f"补片 {len(findings)} 条发现",
        created_at=now_ms(),
    ))
    await db.update_case(
        case.id,
        scan_orders_json="[]",
        instrument_notes_json=json.dumps(notes, ensure_ascii=False),
        pending_gate="" if case.auto_advance else "scanned",
        status="scanned",
        coverage_json=json.dumps(ledger, ensure_ascii=False),
    )
    page_items, total = await db.list_findings(case.id, page=1, size=50, order_by="severity desc")
    return {
        "case_id": case.id,
        "report_id": report_id,
        "supplement_report_id": supplement_id,
        "round": case.round,
        "devices": devices,
        "findings": page_payload(
            [f.model_dump() for f in page_items], 1, 50, "severity desc", total
        ),
        "limitations": limitations,
        "report_path": str(report_path),
        "handoff": handoff(case.id, "scanned", case.auto_advance, next_tool="hosp_diagnose"),
    }
