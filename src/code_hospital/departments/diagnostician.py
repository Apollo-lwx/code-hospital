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
    opening_verdict,
    scan_orders_for,
    scope_key,
)
from ..core.algorithms import lookup_history
from ..core.errors import invalid_operation, not_found
from ..core.ids import new_id
from ..core.models import SEVERITY_ORDER, Diagnosis, Finding, ReportRecord, counts_toward_efficacy
from ..core.questions import STAGE_ARTIFACTS, ask, handoff, present, release, stage_gate_question
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


def _semantic_groups(findings: list[Finding]) -> list[list[Finding]]:
    """语义结论单独成簇。不和仪器发现合成一条 code 诊断。"""
    groups = []
    for members in group_findings(findings):
        host = [item for item in members if item.concluded_by == "host_llm"]
        other = [item for item in members if item.concluded_by != "host_llm"]
        if other:
            groups.append(other)
        if host:
            groups.append(host)
    return groups


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
    project_root: str = "",
) -> list[tuple[Diagnosis, list[Finding]]]:
    clusters: list[tuple[Diagnosis, list[Finding]]] = []
    for members in _semantic_groups(findings):
        body = [item for item in members if counts_toward_efficacy(item.concluded_by)] or members
        head = min(body, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.line_start))
        severity = _worst(body)
        matched = match_finding(
            head.tool, head.rule_id, severity,
            fixable=bool(getattr(head, "fixable", False)),
            verdict=opening_verdict(members),
            project_root=project_root,
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
            concluded_by=(
                "host_llm"
                if members and all(item.concluded_by == "host_llm" for item in members)
                else "code"
            ),
        )
        clusters.append((diagnosis, members))
    clusters.sort(key=lambda pair: (
        0 if any(counts_toward_efficacy(item.concluded_by) for item in pair[1]) else 1,
        SEVERITY_ORDER.get(pair[0].severity, 9),
        pair[0].category,
    ))
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


async def _file_host_diff(db, root: str, case, answers: dict | None) -> str:
    """合入门诊把本轮交卷写成发现。上一轮已在病历里的相同问题不再插一次。"""
    from ..adapters.base import RawFinding
    from ..catalog.checks import file_host_diff, run_checks
    from ..core.algorithms import stamp_findings
    from ..safety.repo_files import scope_file_list
    from .scanner import normalize

    files, _skipped = await scope_file_list(root, case.scope_type, case.scope.model_dump())
    checks, _notes = run_checks(root, files, getattr(case, "visit_kind", "") or "checkup")
    previous = (case.coverage or {}).get("filing")
    specs, note = file_host_diff(checks, answers, files, previous=previous)
    from ..catalog.filing import filing_ledger

    ledger = filing_ledger(checks, answers, files, previous=previous)
    coverage = dict(case.coverage or {})
    if ledger:
        coverage["filing"] = ledger
    if specs:
        existing = await db.all_findings(case.id)
        seen = {
            (item.file_path.replace("\\", "/"), item.line_start, item.line_end, item.message)
            for item in existing
            if item.rule_id == "diff.issue"
        }
        fresh = []
        batch = set(seen)
        for spec in specs:
            key = (spec["file_path"], spec["line_start"], spec["line_end"], spec["message"])
            if key in batch:
                continue
            batch.add(key)
            fresh.append(spec)
        specs = fresh
    stored_before = await db.all_findings(case.id)
    if not specs:
        note = _remember_links(coverage, ledger, stored_before, note)
        if ledger:
            await db.update_case(case.id, coverage_json=json.dumps(coverage, ensure_ascii=False))
            case.coverage = coverage
        return note
    reports = await db.list_reports(case.id)
    scan_report = next((row for row in reports if row.stage == "scan"), None)
    report_id = scan_report.id if scan_report else new_id("rpt")
    findings = [
        normalize(
            RawFinding(
                rule_id="diff.issue",
                message=spec["message"],
                file_path=spec["file_path"],
                line_start=spec["line_start"],
                line_end=spec["line_end"],
                severity="major",
                extra={"check_id": spec.get("check_id") or ""},
            ),
            "host",
            case.id,
            report_id,
            root,
        )
        for spec in specs
    ]
    stamp_findings(findings)
    await db.insert_findings(findings)
    from ..catalog.coverage import stamp_source_counts

    stored = await db.all_findings(case.id)
    note = _remember_links(coverage, ledger, stored, note)
    coverage = stamp_source_counts(coverage, stored)
    await db.update_case(case.id, coverage_json=json.dumps(coverage, ensure_ascii=False))
    case.coverage = coverage
    return note


def _remember_links(coverage: dict, ledger: dict | None, stored, note: str) -> str:
    if not ledger:
        return note
    from ..catalog.filing import link_text, relate_checks

    links = relate_checks(stored, ledger)
    updated = dict(ledger)
    updated["links"] = links
    coverage["filing"] = updated
    extra = link_text(links)
    if extra and extra not in note:
        return note + extra
    return note


from ..api.capabilities import hosted


@hosted
async def diagnose(
    case_id: str,
    use_llm: bool = False,
    stage_confirmed: bool = False,
    project_root: str = "",
    check_answers: dict | None = None,
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
        from ..api.capabilities import admit_stage

        admit_stage(stage_confirmed, case.pending_gate or "")
        if case.pending_gate and not stage_confirmed:
            reports = await db.list_reports(case_id)
            scan_report = next((r for r in reports if r.stage == "scan"), None)
            where = scan_report.md_path if scan_report and scan_report.md_path else "检验报告"
            artifact, next_tool = STAGE_ARTIFACTS.get(case.pending_gate, (case.pending_gate, "hosp_diagnose"))
            halted = await release(stage_gate_question(
                case_id, case.pending_gate, next_tool, f"{artifact}见 {where}。"
            ))
            if halted is not None:
                return halted
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
            from ..catalog.budget import BUDGET_LINE

            degraded = bool((case.coverage or {}).get("budget", {}).get("degraded"))
            if degraded:
                filing_note = BUDGET_LINE
            else:
                filing_note = await _file_host_diff(db, root, case, check_answers)
            from ..catalog.checks import filing_accepted

            if case.visit_kind == "admission" and not degraded and not filing_accepted(filing_note):
                filing_question = ask(
                    "diff_filing",
                    f"合入门诊还不能诊断（病例 {case_id}）。{filing_note}",
                    [("file", "交上 diff.issues 后再诊断"), ("pause", "先停在已检验")],
                    "停在已检验，不写诊断书，不开方",
                    (
                        f'hosp_diagnose(case_id="{case_id}", stage_confirmed=true, '
                        'check_answers={"diff.issues": "文件:行: 说明"})'
                    ),
                )
                presented = await present(filing_question)
                if presented.stop is not None:
                    return presented.stop
                filed = False
                if presented.choice == "file" and presented.text.strip():
                    answers = dict(check_answers or {})
                    answers["diff.issues"] = presented.text.strip()
                    filing_note = await _file_host_diff(db, root, case, answers)
                    filed = filing_accepted(filing_note)
                if not filed:
                    return filing_question
            from ..safety.admission import guard_clearance, rules_for, settle_semantic

            admission_lane = "auto_advance" if case.auto_advance else "report"
            admission_rules = rules_for(root, admission_lane)
            filing_note = guard_clearance(filing_note, admission_rules)
            if (case.coverage or {}).get("budget", {}).get("degraded"):
                use_llm = False
            findings = [f for f in await db.all_findings(case_id) if f.status != "resolved"]
            durable = [f for f in findings if counts_toward_efficacy(f.concluded_by)]
            fingerprints = sorted({f.fingerprint for f in durable if f.fingerprint})
            content_keys = sorted({f.content_key for f in durable if f.content_key})
            raw_history = await db.find_fingerprint_history(
                root, fingerprints, exclude_case_id=case_id, content_keys=content_keys,
            )
            history = {
                fp: [{**r, "note": "此前病例曾出现同形态发现（复发）"} for r in refs]
                for fp, refs in raw_history.items()
            }
            report_id = new_id("rpt")
            clusters, admission_drops = settle_semantic(
                cluster_findings(findings, case_id, report_id, history, root),
                admission_rules,
            )
            semantic = sum(1 for diagnosis, _members in clusters if diagnosis.concluded_by == "host_llm")
            if semantic or admission_drops:
                coverage = dict(case.coverage or {})
                coverage["admission"] = {
                    "lane": admission_lane,
                    "semantic": semantic,
                    "dropped": admission_drops,
                    "note": "语义结论标成 host_llm。落不到文件和行号的不进诊断。其余按这条链路留在待核实线索里。",
                }
                await db.update_case(
                    case_id, coverage_json=json.dumps(coverage, ensure_ascii=False),
                )
                case.coverage = coverage
            diagnoses = [d for d, _members in clusters]
            conditions = build_conditions(clusters, case.requested_specialties)
            stats = _stats(findings, diagnoses)
            await db.insert_diagnoses(diagnoses)
            from ..catalog.outside import overlay as outside_overlay

            questions = outside_overlay(root)["diagnose"]
            case.updated_at = now_ms()
            md = render_diagnose_report(
                case, report_id, clusters, stats, use_llm, questions, filing_note,
                budget_note=(case.coverage or {}).get("budget", {}).get("note") or "",
            )
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
                "questions": questions,
                "filing_note": filing_note,
                "report_path": str(report_path),
                "handoff": handoff(case_id, "diagnosed", case.auto_advance, next_tool="hosp_prescribe"),
            }
    from .scanner import scan as run_scan
    await run_scan(case_id, stage_confirmed=True, project_root=root)
    return await diagnose(case_id, use_llm, True, root, check_answers)


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
