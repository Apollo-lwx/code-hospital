"""药房前厅 · 处方师（设计 §3.2 节点 4）。

诊断书确认后开出有序处方草稿；批准是独立闸门，不再叠加阶段挂起。
低置信诊断默认不入方。高危项必须 approver=user 且 user_confirmed=true。
本工具不改业务源码。验收白名单经用户 confirm 后写入 code-hospital.yaml；
选择跳过则该项验收记 skipped。
仓库已有 .rsi 且本机有 rsi 命令时，草稿附上开方前召回的禁止项。
"""

from __future__ import annotations

from pathlib import Path

from ..catalog.formulary import (
    build_conditions,
    companion_for,
    effective_method,
    match_finding,
    opening_verdict,
    scope_key,
)
from ..core.algorithms import order_prescription_items
from ..core.efficacy import tally
from ..core.errors import bad_argument, forbidden, invalid_operation, not_found
from ..core.ids import new_id
from ..core.models import SEVERITY_ORDER, ReportRecord
from ..core.questions import STAGE_ARTIFACTS, ask, present, release, stage_gate_question
from ..storage.db import Database, hospital_root, now_ms
from ..storage.reports import efficacy_note, render_prescribe_report, render_reading_report

_METHOD_ORDER = {"autofix": 0, "llm_patch": 1, "manual": 2}
_RISK_RANK = {"low": 0, "medium": 1, "high": 2}
_MANIFESTS = {
    "pyproject.toml", "package.json", "package-lock.json", "go.mod", "go.sum",
    "requirements.txt", "cargo.toml", "pom.xml", "gemfile", "composer.json",
}
_LOW_CONF = {"", "adopt", "exclude", "defer"}
_ACCEPTANCE = {"", "skip", "pause", "confirm"}
_DECISION = {"", "reject", "downgrade"}


def _manifest(path: str) -> bool:
    return Path(path).name.lower() in _MANIFESTS


def _item_risk(method: str, paths: list[str]) -> tuple[str, bool]:
    touches = any(_manifest(p) for p in paths)
    if touches or method == "manual" or len(paths) > 10:
        return "high", touches
    if method == "llm_patch":
        return "medium", False
    return "low", False


def _risk_level(items: list[dict]) -> str:
    pending = [it for it in items if it["status"] == "pending"]
    if not pending:
        return "low"
    return max(pending, key=lambda it: _RISK_RANK[it["risk"]])["risk"]


def _policy(items: list[dict], risk_level: str) -> dict:
    required = [it["id"] for it in items if it["status"] == "pending" and it["risk"] == "high"]
    return {
        "auto_ok": risk_level == "low" and not required,
        "host_agent_ok": risk_level != "high" and not required,
        "user_required_items": required,
    }


def _acceptance(method: str) -> dict:
    if method == "autofix":
        return {"alias": "lint", "expect": "对应规则不再被该设备报出"}
    if method == "llm_patch":
        return {"alias": "test", "expect": "相关测试通过"}
    return {"alias": "", "expect": "人工处理，无命令验收"}


def _prompt(prescription_id: str, items: list[dict], risk_level: str) -> str:
    counts = {"autofix": 0, "llm_patch": 0, "manual": 0}
    for it in items:
        counts[it["method"]] = counts.get(it["method"], 0) + 1
    high = sum(1 for it in items if it["risk"] == "high" and it["status"] == "pending")
    return (
        f"处方 {prescription_id}：{len(items)} 项"
        f"（可自动修改 {counts['autofix']} / 需要补丁 {counts['llm_patch']} / 建议人工处理 {counts['manual']}），"
        f"风险 {risk_level}。必须你本人确认的有 {high} 项。"
        "同意后以 approve=true 重新调用；必须本人确认的项要 approver=user 且 user_confirmed=true。"
    )[:500]


def _handoff(case_id: str, approved: bool) -> dict:
    if approved:
        return {
            "stage": "prescribed",
            "artifact": "处方",
            "awaiting_confirmation": False,
            "next_tool": "hosp_dispense",
            "confirm_hint": "",
        }
    return {
        "stage": "prescribed",
        "artifact": "处方",
        "awaiting_confirmation": True,
        "next_tool": "hosp_prescribe",
        "confirm_hint": (
            f'请用户审阅处方。同意后 hosp_prescribe(case_id="{case_id}", '
            'approve=true, approver="user", user_confirmed=true)。'
            "低危可改用 approver=host_agent。"
        )[:300],
    }


async def _with_rsi(root: str, payload: dict) -> dict:
    from ..integrations.rsi import recall_for_prescribe

    found = await recall_for_prescribe(root, "开方前召回用药禁忌和疗效账")
    if found["prohibitions"]:
        payload["rsi_prohibitions"] = found["prohibitions"]
    if found["efficacy"]:
        payload["rsi_efficacy"] = found["efficacy"]
    return payload


def _draft_payload(case_id: str, rx: dict, report_path: str, reading_path: str = "") -> dict:
    items = rx["items"]
    risk = rx["risk_level"]
    payload = {
        "case_id": case_id,
        "prescription_id": rx["id"],
        "status": "draft",
        "risk_level": risk,
        "items": items,
        "confirmation_prompt": _prompt(rx["id"], items, risk),
        "approval_policy": _policy(items, risk),
        "report_path": report_path,
        "reading_path": reading_path,
        "handoff": _handoff(case_id, approved=False),
    }
    note = efficacy_note(rx.get("efficacy") or [])
    if note:
        payload["efficacy_note"] = note
    return payload


def _approval_payload(rx: dict, report_path: str) -> dict:
    approved = rx["status"] == "approved"
    return {
        "prescription_id": rx["id"],
        "status": rx["status"],
        "approved_by": rx["approved_by"],
        "human_in_loop": rx["human_in_loop"],
        "confirmation_channel": rx["confirmation_channel"] or "convention",
        "approved_at": rx["approved_at"],
        "report_path": report_path,
        "handoff": _handoff(rx["case_id"], approved=approved),
    }


_HOLD_FORMULAS = frozenset({"seal", "relocate"})


def _merge_spans(spans: list[list[int]]) -> list[list[int]]:
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [ordered[0][:]]
    for start, end in ordered[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _member_spans(members: list) -> list[list[int]]:
    spans = []
    for finding in members:
        start = finding.line_start or 1
        end = finding.line_end or start
        spans.append([start, max(start, end)])
    return _merge_spans(spans)


def _spans_overlap(left: list, right: list) -> bool:
    for a, b in left:
        for c, d in right:
            if a <= d and c <= b:
                return True
    return False


def _draft_item(
    diagnosis, members, prescription_id, hint, formula_id, parent_formula_id,
    method, specialty_id, attending, scope, spans,
) -> dict:
    paths = list(dict.fromkeys(f.file_path for f in members))
    symbols = list(dict.fromkeys(f.symbol for f in members if f.symbol))
    risk, touches = _item_risk(method, paths)
    head = members[0]
    return {
        "id": new_id("rxit"),
        "prescription_id": prescription_id,
        "seq": hint,
        "diagnosis_id": diagnosis.id,
        "target_file": paths[0] if paths else "",
        "target_symbol": symbols[0] if symbols else "",
        "method": method,
        "instructions": diagnosis.root_cause[:500],
        "acceptance": _acceptance(method),
        "rollback": "manual" if method == "manual" else "reverse_apply",
        "scope_paths": paths,
        "scope_symbols": symbols,
        "risk": risk,
        "touches_manifest": touches,
        "amended_from": "",
        "status": "pending",
        "formula_id": formula_id,
        "parent_formula_id": parent_formula_id,
        "specialty_id": specialty_id,
        "scope_key": scope,
        "scope_lines": spans if method == "autofix" or formula_id != "contrast" else [],
        "formulary_conflict": 0,
        "conflict_reason": "",
        "files": paths,
        "symbols": symbols,
        "severity": diagnosis.severity,
        "seq_hint": hint,
        "attending": attending,
    }


def _primary_items(
    diagnosis, members, prescription_id, hint, formula_id, method, specialty_id, attending, scope, spans,
) -> list[dict]:
    """归位按文件切块。诊断簇仍共用契约键，处方项的范围落到单个文件。"""
    if formula_id != "relocate":
        return [_draft_item(
            diagnosis, members, prescription_id, hint,
            formula_id, "", method, specialty_id, attending, scope, spans,
        )]
    items = []
    paths = list(dict.fromkeys(finding.file_path for finding in members))
    for offset, path in enumerate(paths):
        file_members = [finding for finding in members if finding.file_path == path]
        items.append(_draft_item(
            diagnosis, file_members, prescription_id, hint + offset,
            formula_id, "", method, specialty_id, attending,
            f"{scope}#file:{path}", _member_spans(file_members),
        ))
    return items


def build_items(
    diagnoses: list, findings_by_id: dict, prescription_id: str, include_low: bool,
    requested: list[str] | None = None,
    project_root: str = "",
) -> list[dict]:
    chosen = [
        d for d in diagnoses
        if d.formulary_matched and (include_low or d.confidence >= 0.5)
    ]
    clusters = []
    for diagnosis in chosen:
        members = [findings_by_id[i] for i in diagnosis.member_ids if i in findings_by_id]
        if not members:
            members = [f for f in findings_by_id.values() if f.fingerprint == diagnosis.fingerprint]
        if not members:
            continue
        clusters.append((diagnosis, members))
    clusters.sort(key=lambda pair: (
        _METHOD_ORDER.get(pair[0].fixability, 9),
        SEVERITY_ORDER.get(pair[0].severity, 9),
        pair[0].category,
    ))
    attending_of = {}
    for condition in build_conditions(clusters, requested or []):
        for diagnosis_id in condition["diagnosis_ids"]:
            attending_of[diagnosis_id] = condition["attending_specialty_id"]

    drafted = []
    hint = 1
    for diagnosis, members in clusters:
        head = min(members, key=lambda f: (f.line_start, f.file_path))
        matched = match_finding(
            head.tool, head.rule_id, diagnosis.severity,
            fixable=bool(getattr(head, "fixable", False)),
            verdict=opening_verdict(members),
            project_root=project_root,
        )
        if matched.formula is None:
            continue
        method = effective_method(matched.formula, acceptance_open=False)
        scope = scope_key(head)
        spans = _member_spans(members)
        attending = attending_of.get(diagnosis.id, matched.specialty_id)
        for item in _primary_items(
            diagnosis, members, prescription_id, hint,
            matched.formula.id, method, matched.specialty_id, attending, scope, spans,
        ):
            drafted.append(item)
            hint += 1
        companion = companion_for(
            matched.formula.id, method, matched.specialty_id, project_root,
        )
        if companion is None:
            continue
        drafted.append(_draft_item(
            diagnosis, members, prescription_id, hint,
            companion.id, matched.formula.id, companion.method, matched.specialty_id,
            attending, scope, [],
        ))
        hint += 1

    kept: dict[tuple, dict] = {}
    for item in drafted:
        key = (item["formula_id"], item["scope_key"], item["parent_formula_id"])
        previous = kept.get(key)
        if previous is None or _prefer_item(item, previous):
            kept[key] = item
    drafted = list(kept.values())
    _mark_conflicts(drafted)
    ordered = order_prescription_items(drafted)
    for seq, item in enumerate(ordered, 1):
        item["seq"] = seq
        for extra in ("files", "symbols", "severity", "seq_hint", "attending"):
            item.pop(extra, None)
    return ordered


def _prefer_item(candidate: dict, current: dict) -> bool:
    def rank(item: dict) -> tuple:
        attending_first = 0 if item["specialty_id"] == item.get("attending") else 1
        return (SEVERITY_ORDER.get(item["severity"], 9), attending_first, item["specialty_id"])
    return rank(candidate) < rank(current)


def _clash(left: dict, right: dict) -> str:
    if left["formula_id"] == right["formula_id"] and left["parent_formula_id"] == right["parent_formula_id"]:
        return ""
    shared = set(left["scope_paths"]) & set(right["scope_paths"])
    methods = {left["method"], right["method"]}
    if shared and "relocate" in {left["formula_id"], right["formula_id"]}:
        if "manual" in methods and methods & {"autofix", "llm_patch"}:
            return "parent_child"
    parent = (
        left["parent_formula_id"] == right["formula_id"]
        or right["parent_formula_id"] == left["formula_id"]
    )
    same = bool(left["scope_key"]) and left["scope_key"] == right["scope_key"]
    overlap = (
        left["target_file"] == right["target_file"]
        and _spans_overlap(left["scope_lines"], right["scope_lines"])
    )
    if parent:
        reason = "parent_child"
    elif same:
        reason = "same_scope"
    elif overlap:
        reason = "overlap"
    else:
        return ""
    methods = {left["method"], right["method"]}
    holds = left["formula_id"] in _HOLD_FORMULAS or right["formula_id"] in _HOLD_FORMULAS
    changes = bool(methods & {"autofix", "llm_patch"})
    if holds and changes:
        return reason
    if methods == {"autofix", "llm_patch"}:
        return reason
    return ""


def _mark_conflicts(items: list[dict]) -> None:
    for index, left in enumerate(items):
        for right in items[index + 1:]:
            reason = _clash(left, right)
            if not reason:
                continue
            for item in (left, right):
                item["formulary_conflict"] = 1
                item["conflict_reason"] = reason


def _efficacy_for(items: list[dict], diagnoses: list, history: dict) -> list[dict]:
    """本张处方里、本仓库已有成败记录的规则。不改变 items 的顺序。"""
    category = {diagnosis.id: diagnosis.category for diagnosis in diagnoses}
    seen: set[tuple[str, str]] = set()
    rows = []
    for item in items:
        rule_id = category.get(item.get("diagnosis_id") or "") or ""
        key = (rule_id, item.get("method") or "")
        if not rule_id or key in seen:
            continue
        counts = history.get(key)
        if not counts or counts[0] + counts[1] <= 0:
            continue
        seen.add(key)
        rows.append({
            "rule_id": rule_id,
            "method": key[1],
            "success": counts[0],
            "failure": counts[1],
        })
    return rows


def _skip_acceptance(items: list[dict]) -> None:
    for it in items:
        if it["acceptance"].get("alias"):
            it["acceptance"] = {"alias": "", "expect": "验收命令白名单为空，本项验收记 skipped"}


from ..api.capabilities import hosted


@hosted
async def prescribe(
    case_id: str,
    approve: bool = False,
    approver: str = "host_agent",
    user_confirmed: bool = False,
    stage_confirmed: bool = False,
    low_confidence: str = "",
    acceptance_decision: str = "",
    decision: str = "",
    project_root: str = "",
) -> dict:
    """hosp_prescribe。approve=false 出草稿；approve=true 按策略批准、拒绝或降级。"""
    from .reception import resolve_project_root
    from .scanner import detect_acceptance_commands

    if approver not in {"auto", "host_agent", "user"}:
        raise bad_argument("approver 须为 auto / host_agent / user", target="approver")
    if low_confidence not in _LOW_CONF:
        raise bad_argument("low_confidence 须为 adopt / exclude / defer", target="low_confidence")
    if acceptance_decision not in _ACCEPTANCE:
        raise bad_argument("acceptance_decision 须为 skip / confirm", target="acceptance_decision")
    if decision not in _DECISION:
        raise bad_argument("decision 须为 reject / downgrade", target="decision")
    if decision and not approve:
        raise bad_argument("reject / downgrade 须与 approve=true 一起使用", target="decision")

    root = resolve_project_root(project_root)
    async with Database(root) as db:
        case = await db.get_case(case_id)
        if case is None:
            raise not_found(f"病例不存在：{case_id}", target="case_id", next_action="先 hosp_register 挂号")
        existing = await db.latest_prescription(case_id)
        report_path = _prescribe_report_path(root, case_id, case.round)
        if case.status == "prescribed" and existing is not None:
            return await _decide(
                db, case, existing, approve, approver, user_confirmed, decision,
                str(report_path), root,
            )
        if case.status != "diagnosed":
            raise invalid_operation(
                f"病例当前状态为 {case.status}，须先完成诊断才能开方",
                target="case_id",
                next_action="先 hosp_diagnose，并确认诊断书",
            )
        from ..api.capabilities import admit_stage

        admit_stage(stage_confirmed, case.pending_gate or "")
        if case.pending_gate and not stage_confirmed:
            reports = await db.list_reports(case_id)
            diag = next((r for r in reports if r.stage == "diagnose"), None)
            where = diag.md_path if diag and diag.md_path else "诊断书"
            _artifact, next_tool = STAGE_ARTIFACTS.get(case.pending_gate, ("诊断书", "hosp_prescribe"))
            halted = await release(stage_gate_question(case_id, case.pending_gate, next_tool, f"诊断书见 {where}。"))
            if halted is not None:
                return halted
        if case.pending_gate and case.pending_gate != "diagnosed":
            raise invalid_operation(
                f"当前待确认的是 {case.pending_gate}，不能跳到开方",
                target="case_id",
                next_action="按 handoff 确认当前阶段产物",
            )

        diagnoses = await db.list_diagnoses(case_id)
        findings = await db.all_findings(case_id)
        by_id = {finding.id: finding for finding in findings}
        from ..safety.admission import rules_for, screen_diagnoses

        lane = "auto_advance" if case.auto_advance else "dispense"
        diagnoses, admission_drops = screen_diagnoses(diagnoses, by_id, rules_for(root, lane))
        low = [d for d in diagnoses if d.confidence < 0.5]
        if low and low_confidence == "":
            presented = await present(ask(
                "low_confidence_diagnosis",
                f"有 {len(low)} 条置信度低于 0.5 的诊断（待复核）。未确认前不入处方。",
                [("adopt", "采用这些待复核诊断"), ("exclude", "排除，不入处方"), ("defer", "先不开放，我补充上下文")],
                "排除（不入处方）",
                f'hosp_prescribe(case_id="{case_id}", stage_confirmed=true, low_confidence="exclude")',
            ))
            if presented.stop is not None:
                return presented.stop
            low_confidence = presented.choice
        if low_confidence == "defer":
            presented = await present(ask(
                "low_confidence_diagnosis",
                "开方已暂停，等待补充上下文。补充后用 adopt 或 exclude 重新调用。",
                [("adopt", "采用这些待复核诊断"), ("exclude", "排除，不入处方"), ("defer", "继续暂停")],
                "排除（不入处方）",
                f'hosp_prescribe(case_id="{case_id}", stage_confirmed=true, low_confidence="exclude")',
            ))
            if presented.stop is not None:
                return presented.stop
            low_confidence = presented.choice

        prescription_id = new_id("rx")
        items = build_items(
            diagnoses, by_id, prescription_id,
            include_low=low_confidence == "adopt",
            requested=case.requested_specialties,
            project_root=root,
        )
        if (case.coverage or {}).get("budget", {}).get("degraded"):
            from ..catalog.budget import downgrade_model_items

            downgrade_model_items(items)
        needs = {it["acceptance"].get("alias") for it in items if it["acceptance"].get("alias")}
        from ..safety.acceptance import load_commands, save_detected

        known = set(load_commands(root))
        missing = needs - known
        if acceptance_decision == "pause" or (missing and acceptance_decision not in {"skip", "confirm"}):
            suggestions = [s for s in detect_acceptance_commands(root) if s["alias"] in missing][:4]
            sug = "；".join(f"{s['alias']}={s['command']}" for s in suggestions) or "无"
            kept = low_confidence or "exclude"
            presented = await present(ask(
                "acceptance_whitelist",
                f"处方含需命令验收的药味，白名单还缺 {sorted(missing)}。探测到：{sug}。",
                [("confirm", "把探测到的命令写入 code-hospital.yaml"), ("skip", "跳过命令验收（报告记 skipped）"), ("pause", "暂停开方")],
                "验收记 skipped",
                f'hosp_prescribe(case_id="{case_id}", stage_confirmed=true, '
                f'low_confidence="{kept}", acceptance_decision="confirm")',
            ))
            if presented.stop is not None:
                return presented.stop
            acceptance_decision = presented.choice
        if acceptance_decision == "confirm":
            save_detected(root, detect_acceptance_commands(root))
            known = set(load_commands(root))
            for it in items:
                alias = it["acceptance"].get("alias")
                if alias and alias not in known:
                    it["acceptance"] = {"alias": "", "expect": "该别名未进入白名单，验收记 skipped"}
        elif acceptance_decision == "skip" or missing:
            _skip_acceptance(items)

        if case.pending_gate:
            await db.update_case(case_id, pending_gate="")
        risk = _risk_level(items)
        created = now_ms()
        header = {
            "id": prescription_id, "case_id": case_id, "status": "draft", "risk_level": risk,
            "created_at": created,
        }
        await db.insert_prescription(header, items)
        rx = {
            "id": prescription_id, "case_id": case_id, "status": "draft",
            "risk_level": risk, "items": items,
            "efficacy": _efficacy_for(items, diagnoses, tally(await db.list_efficacy_rows())),
        }
        case.updated_at = created
        clusters = []
        for diagnosis in diagnoses:
            members = [by_id[fid] for fid in diagnosis.member_ids if fid in by_id]
            if members:
                clusters.append((diagnosis, members))
        reading_path = report_path.with_name(f"reading-r{case.round}.md")
        reading_path.write_text(
            render_reading_report(case, clusters, items, case.requested_specialties),
            encoding="utf-8",
        )
        md = render_prescribe_report(case, prescription_id, rx, str(report_path))
        note = f"> 给人看的一页：`{reading_path.name}`。同一函数里的问题放在一起。\n\n"
        md = md.replace("\n---\n\n", "\n---\n\n" + note, 1)
        if admission_drops:
            listed = "\n".join(
                f"- {row['file'] or '未定位'}:{row['line'] or '-'} {row['reason']}"
                for row in admission_drops
            )
            md += f"\n## 未入方的线索\n\n{listed}\n"
            import json

            coverage = dict(case.coverage or {})
            coverage["admission_drops"] = admission_drops
            await db.update_case(case_id, coverage_json=json.dumps(coverage, ensure_ascii=False))
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md, encoding="utf-8")
        await db.insert_report(ReportRecord(
            id=new_id("rpt"), case_id=case_id, stage="prescribe", round=case.round,
            md_path=str(report_path),
            summary=f"处方草稿 {prescription_id}，风险 {risk}，{len(items)} 味",
            created_at=created,
        ))
        await db.update_case(case_id, status="prescribed", pending_gate="")
        drafted = await _with_rsi(root, _draft_payload(case_id, rx, str(report_path), str(reading_path)))
        if admission_drops:
            drafted["admission_drops"] = admission_drops
        return drafted


def _prescribe_report_path(root: str, case_id: str, round_: int) -> Path:
    return hospital_root(root) / "reports" / case_id / f"prescribe-r{round_}.md"


async def _decide(
    db, case, rx, approve, approver, user_confirmed, decision, report_path: str, root: str,
    confirmation: str = "",
) -> dict:
    if not rx.get("efficacy"):
        diagnoses = await db.list_diagnoses(case.id)
        rx["efficacy"] = _efficacy_for(
            rx["items"], diagnoses, tally(await db.list_efficacy_rows()),
        )
    if rx["status"] == "approved":
        if approve or decision:
            return _approval_payload(rx, report_path)
        raise invalid_operation("处方已批准", target="prescription_id", next_action="等待 hosp_dispense（尚未开放）")
    if rx["status"] == "rejected":
        raise invalid_operation("这张处方已拒绝", target="prescription_id", next_action="本阶段暂不支持就地重开")
    channel = confirmation or "convention"
    if approve and approver == "user":
        from ..api.capabilities import require_receipt

        require_receipt(user_confirmed, "user_confirmed")
    if not approve:
        return await _with_rsi(root, _draft_payload(case.id, rx, report_path))

    items = rx["items"]
    if decision == "reject":
        await db.update_prescription(
            rx["id"], status="rejected", approved_by=approver,
            human_in_loop=approver == "user" and user_confirmed,
            confirmation_channel=channel, approved_at=now_ms(),
        )
        rx.update(status="rejected", approved_by=approver, human_in_loop=approver == "user" and user_confirmed,
                  confirmation_channel=channel, approved_at=now_ms())
        _rewrite(case, rx, report_path)
        await db.update_report_summary(await _report_id(db, case.id), f"处方已拒绝 {rx['id']}")
        return _approval_payload(rx, report_path)

    if decision == "downgrade":
        for it in items:
            if it["status"] == "pending" and it["risk"] == "high":
                it["status"] = "skipped"
                it["method"] = "manual"
                it["instructions"] = ("已降级为外购，不自动执行。" + it["instructions"])[:500]
                await db.update_prescription_item(
                    it["id"], status="skipped", method="manual", instructions=it["instructions"]
                )
        rx["risk_level"] = _risk_level(items)

    risk = _risk_level(items)
    policy = _policy(items, risk)
    if policy["user_required_items"] and approver == "host_agent":
        raise forbidden(
            "高危项不能由宿主代理自批",
            target="approver",
            next_action='请用户本人确认。未声明 elicitation 时用 approver="user" 且 user_confirmed=true',
        )
    if policy["user_required_items"] and not (approver == "user" and user_confirmed):
        question = ask(
            "high_risk_approval",
            (
                f"处方 {rx['id']} 含 {len(policy['user_required_items'])} 味高危药。"
                "批准用 approver=user 且 user_confirmed=true；"
                "拒绝用 decision=reject；降级用 decision=downgrade。"
            ),
            [("approve", "本人批准"), ("reject", "拒绝整张处方"), ("downgrade", "高危降级为外购后批准其余")],
            "降级外购",
            f'hosp_prescribe(case_id="{case.id}", approve=true, approver="user", user_confirmed=true)',
        )
        presented = await present(question)
        if presented.stop is not None:
            return presented.stop
        if presented.choice == "approve":
            return await _decide(
                db, case, rx, True, "user", True, decision, report_path, root, confirmation="elicitation",
            )
        if presented.choice == "reject":
            return await _decide(
                db, case, rx, True, "user", True, "reject", report_path, root, confirmation="elicitation",
            )
        if presented.choice == "downgrade":
            return await _decide(
                db, case, rx, True, "user", True, "downgrade", report_path, root, confirmation="elicitation",
            )
        return question
    if risk == "medium" and approver == "auto":
        raise forbidden("中危处方不能 auto 审批", target="approver", next_action='改用 approver="host_agent" 或 "user"')
    if risk == "high" and not (approver == "user" and user_confirmed):
        raise forbidden("高危项须本人确认", target="approver", next_action='approver="user" 且 user_confirmed=true')

    approved_at = now_ms()
    human = approver == "user" and user_confirmed
    await db.update_prescription(
        rx["id"], status="approved", risk_level=risk, approved_by=approver,
        human_in_loop=human, confirmation_channel=channel, approved_at=approved_at,
    )
    rx.update(
        status="approved", risk_level=risk, approved_by=approver, human_in_loop=human,
        confirmation_channel=channel, approved_at=approved_at, items=items,
    )
    _rewrite(case, rx, report_path)
    await db.update_report_summary(await _report_id(db, case.id), f"处方已批准 {rx['id']}，风险 {risk}")
    await db.update_case(case.id, status="dispensing", pending_gate="")
    return _approval_payload(rx, report_path)


def _rewrite(case, rx: dict, report_path: str) -> None:
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_prescribe_report(case, rx["id"], rx, report_path), encoding="utf-8")


async def _report_id(db, case_id: str) -> str:
    reports = await db.list_reports(case_id)
    found = [r for r in reports if r.stage == "prescribe"]
    return found[-1].id if found else ""
