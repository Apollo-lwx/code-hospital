"""门诊坐诊师 Diagnostician（设计 §3.2 节点 3，名册归因）。

分簇键是 (rule_id, scope_key)。剂型从药名册抄，不再用本地规则表。
不调用模型：use_llm=true 只表示宿主将自行增强解读，诊断书仍是模板。
置信度 < 0.5 的归因以「待复核：」开头，不进入后续处方。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from ..catalog.formulary import (
    build_conditions,
    effective_method,
    match_finding,
    scan_orders_for,
    scope_key,
)
from ..core.algorithms import lookup_history
from ..core.errors import invalid_operation, not_found
from ..core.ids import new_id
from ..core.models import SEVERITY_ORDER, Diagnosis, Finding, ReportRecord
from ..core.questions import STAGE_ARTIFACTS, handoff, stage_gate_question
from ..storage.db import Database, hospital_root, now_ms
from ..storage.reports import render_diagnose_report

_CATEGORY = {
    "F841": "unused-variable",
    "F401": "unused-import",
    "F541": "f-string-missing-placeholder",
}
_METHOD_TAIL = {
    "autofix": "可以自动改",
    "llm_patch": "需要补一份补丁",
    "manual": "建议人工处理",
}
_DRUG_PLAIN = {
    "清创": "删掉确定多余的代码",
    "拆分": "把过长的函数或文件拆开",
    "对照": "改完后核对行为有没有变",
    "续治": "补一份修改",
    "补罩": "把过宽的异常处理收窄",
    "封存": "先别改",
    "去重": "去掉重复代码",
    "归位": "把依赖放回约定的分层",
    "削峰": "降低热点上的开销",
    "正骨": "重命名并改掉引用",
    "进货": "补上缺失的依赖或工具",
}


def _category(tool: str, rule_id: str) -> str:
    if tool == "gitleaks":
        return "hardcoded-secret"
    if rule_id in _CATEGORY:
        return _CATEGORY[rule_id]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", rule_id).strip("-").lower()
    return (slug or "unclassified")[:60]


def group_findings(findings: list[Finding]) -> list[list[Finding]]:
    """A5：簇的主键是 (rule_id, scope_key)。content_key 只用于复发调档。"""
    buckets: dict[tuple, list[Finding]] = defaultdict(list)
    for finding in findings:
        buckets[(finding.rule_id, scope_key(finding))].append(finding)
    return list(buckets.values())


def _root_cause(
    tool: str, rule_id: str, fixability: str, places: int,
    history: list[dict], confidence: float, drug: str = "",
) -> str:
    tail = _METHOD_TAIL[fixability]
    plain = _DRUG_PLAIN.get(drug, "")
    if drug and plain:
        prefix = f"{drug}（{plain}）："
    elif drug:
        prefix = f"{drug}："
    else:
        prefix = ""
    if tool == "gitleaks":
        text = f"{prefix}规则 {rule_id} 检出疑似密钥，涉及 {places} 处，先不要自动改。"
    elif fixability == "autofix":
        text = f"{prefix}规则 {rule_id}（{tool}）可以确定地改掉，涉及 {places} 处，{tail}。"
    elif fixability == "llm_patch":
        text = f"{prefix}规则 {rule_id}（{tool}）要看上下文才能改，涉及 {places} 处，{tail}。"
    else:
        text = f"{prefix}规则 {rule_id}（{tool}）还不能自动判断怎么改，涉及 {places} 处，{tail}。"
    if history:
        ids = "、".join(h["case_id"] for h in history[:3])
        text += f"病史：曾在 {ids} 出现。"
    if confidence < 0.5:
        text = "待复核：" + text
    return text[:500]


def _worst(findings: list[Finding]) -> str:
    return min(findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9)).severity


def _history_for(members: list[Finding], history: dict[str, list[dict]]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for finding in members:
        for ref in lookup_history(history, finding.fingerprint, finding.content_key):
            if ref["case_id"] in seen:
                continue
            seen.add(ref["case_id"])
            merged.append(ref)
            if len(merged) >= 3:
                return merged
    return merged


def cluster_findings(
    findings: list[Finding],
    case_id: str,
    report_id: str,
    history: dict[str, list[dict]],
) -> list[tuple[Diagnosis, list[Finding]]]:
    clusters: list[tuple[Diagnosis, list[Finding]]] = []
    for members in group_findings(findings):
        head = min(members, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.line_start))
        severity = _worst(members)
        matched = match_finding(
            head.tool, head.rule_id, severity, fixable=bool(getattr(head, "fixable", False)),
        )
        fixability = matched.method
        if matched.formula is not None:
            fixability = effective_method(matched.formula, acceptance_open=False)
        drug = matched.formula.name if matched.formula else ""
        refs = _history_for(members, history)
        diagnosis = Diagnosis(
            id=new_id("dia"),
            case_id=case_id,
            report_id=report_id,
            fingerprint=head.fingerprint,
            category=_category(head.tool, head.rule_id),
            root_cause=_root_cause(
                head.tool, head.rule_id, fixability, len(members), refs, matched.confidence, drug,
            ),
            severity=severity,
            confidence=matched.confidence,
            fixability=fixability,
            history_refs=refs,
            referrals=[],
            member_ids=[f.id for f in members],
            specialty_id=matched.specialty_id,
            formulary_matched=matched.matched,
        )
        clusters.append((diagnosis, members))
    clusters.sort(key=lambda pair: (SEVERITY_ORDER.get(pair[0].severity, 9), pair[0].category))
    return clusters


def _stats(findings: list[Finding], diagnoses: list[Diagnosis]) -> dict:
    counts = {"autofix": 0, "llm_patch": 0, "manual": 0}
    for d in diagnoses:
        counts[d.fixability] = counts.get(d.fixability, 0) + 1
    return {
        "total_findings": len(findings),
        "clusters": len(diagnoses),
        **counts,
    }


async def diagnose(
    case_id: str,
    use_llm: bool = False,
    stage_confirmed: bool = False,
    project_root: str = "",
) -> dict:
    """hosp_diagnose 主逻辑。模板归因，不调模型。"""
    from .reception import resolve_project_root

    root = resolve_project_root(project_root)
    async with Database(root) as db:
        case = await db.get_case(case_id)
        if case is None:
            raise not_found(f"病例不存在：{case_id}", target="case_id", next_action="先 hosp_register 挂号")
        if case.status != "scanned":
            raise invalid_operation(
                f"病例当前状态为 {case.status}，须先完成检验才能诊断",
                target="case_id",
                next_action="先 hosp_scan 完成检验，并确认检验报告",
            )
        if case.pending_gate and not stage_confirmed:
            reports = await db.list_reports(case_id)
            scan_report = next((r for r in reports if r.stage == "scan"), None)
            where = scan_report.md_path if scan_report and scan_report.md_path else "检验报告"
            artifact, next_tool = STAGE_ARTIFACTS.get(case.pending_gate, (case.pending_gate, "hosp_diagnose"))
            return stage_gate_question(
                case_id, case.pending_gate, next_tool, f"{artifact}见 {where}。"
            )
        if case.pending_gate and case.pending_gate != "scanned":
            raise invalid_operation(
                f"当前待确认的是 {case.pending_gate}，不能跳到诊断",
                target="case_id",
                next_action="按 handoff 确认当前阶段产物",
            )
        if case.pending_gate:
            await db.update_case(case_id, pending_gate="")
            case.pending_gate = ""

        reports = await db.list_reports(case_id)
        supplemented = any(r.stage == "scan_supplement" for r in reports)
        if case.scan_orders and not case.auto_advance:
            return _orders_payload(case)
        orders = [] if supplemented else scan_orders_for(
            case.requested_specialties, case.requested_instruments, case.instrument_notes,
        )
        if orders:
            await _issue_orders(db, case, orders)
            if not case.auto_advance:
                return _orders_payload(case)
            follow_up = True
        else:
            follow_up = False

        if not follow_up:
            findings = [f for f in await db.all_findings(case_id) if f.status != "resolved"]
            fingerprints = sorted({f.fingerprint for f in findings if f.fingerprint})
            content_keys = sorted({f.content_key for f in findings if f.content_key})
            raw_history = await db.find_fingerprint_history(
                root, fingerprints, exclude_case_id=case_id, content_keys=content_keys,
            )
            history = {
                fp: [{**r, "note": "此前病例曾出现同形态发现（复发）"} for r in refs]
                for fp, refs in raw_history.items()
            }
            report_id = new_id("rpt")
            clusters = cluster_findings(findings, case_id, report_id, history)
            diagnoses = [d for d, _members in clusters]
            conditions = build_conditions(clusters, case.requested_specialties)
            stats = _stats(findings, diagnoses)
            await db.insert_diagnoses(diagnoses)

            case.updated_at = now_ms()
            md = render_diagnose_report(case, report_id, clusters, stats, use_llm)
            report_path = hospital_root(root) / "reports" / case_id / f"diagnose-r{case.round}.md"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(md, encoding="utf-8")
            await db.insert_report(ReportRecord(
                id=report_id, case_id=case_id, stage="diagnose", round=case.round,
                md_path=str(report_path),
                summary=(
                    f"{stats['clusters']} 个问题分组（可自动修改 {stats['autofix']}"
                    f" / 需要补丁 {stats['llm_patch']} / 建议人工处理 {stats['manual']}）"
                ),
                created_at=now_ms(),
            ))
            await db.update_case(
                case_id,
                status="diagnosed",
                pending_gate="" if case.auto_advance else "diagnosed",
            )
            return {
                "case_id": case_id,
                "report_id": report_id,
                "round": case.round,
                "diagnoses": [d.model_dump() for d in diagnoses],
                "conditions": conditions,
                "stats": stats,
                "report_path": str(report_path),
                "handoff": handoff(case_id, "diagnosed", case.auto_advance, next_tool="hosp_prescribe"),
            }
    from .scanner import scan as run_scan
    await run_scan(case_id, stage_confirmed=True, project_root=root)
    return await diagnose(case_id, use_llm, True, root)


def _orders_payload(case) -> dict:
    return {
        "case_id": case.id,
        "status": "scanned",
        "scan_orders": case.scan_orders,
        "handoff": handoff(case.id, "scanned", case.auto_advance, next_tool="hosp_scan"),
    }


async def _issue_orders(db: Database, case, orders: list[dict]) -> None:
    await db.update_case(
        case.id,
        scan_orders_json=json.dumps(orders, ensure_ascii=False),
        pending_gate="" if case.auto_advance else "scanned",
        status="scanned",
    )
    case.scan_orders = orders
    text = "；".join(f"{row['instrument_id']}（{row['reason']}）" for row in orders)
    await db.insert_nursing_log(
        case.id, "", "scan_order_issued", {"orders": orders, "text": text}, "warn",
    )
