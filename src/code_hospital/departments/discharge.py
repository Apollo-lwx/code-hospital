"""复诊与出院（设计 §4.5，算法 A2 / A3 / A9）。

全部药味终态后调用。先复扫差分：有医源性新症且疗程未满则再入院，不跑门禁。
否则做四维会签。门禁失败先问是否接受未愈/转院，或由用户本人豁免。
本工具不改仓库代码。白名单里已有的别名会执行；为空时验收项仍记通过且不跑命令。
distill_to_rsi 为真且本机有 rsi 命令时，把结局计数写入 documentation，并把疗效账写成 experience；没有命令则记未写入。
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..core.algorithms import (
    Check,
    FindingRow,
    attribute_new_findings,
    footprint_from_unified_diff,
    gate_verdict,
    health_check,
    match_findings,
)
from ..core.errors import bad_argument, forbidden, invalid_operation, not_found
from ..core.ids import new_id
from ..core.models import Finding, ReportRecord
from ..core.questions import STAGE_ARTIFACTS, ask, handoff, stage_gate_question
from ..storage.db import Database, hospital_root, now_ms
from ..storage.reports import render_discharge_report, render_follow_up

_ROUNDS = {"", "extend", "refer"}
_GATE = {"", "accept"}
_CHECK_NAME = {
    "effect": "疗效项",
    "health_all": "全仓健康项",
    "health_critical": "全仓健康项",
    "acceptance": "验收项",
    "archive": "档案项",
}


def _row(finding: Finding) -> FindingRow:
    return FindingRow(
        rule_id=finding.rule_id,
        file_path=finding.file_path,
        symbol=finding.symbol,
        snippet=finding.evidence,
        line_start=finding.line_start,
        anchor=finding.context_anchor,
        content_key=finding.content_key,
        identity_fp=finding.fingerprint,
        payload={"severity": finding.severity, "tool": finding.tool},
    )


def _diff_public(case_round: int, buckets: dict[str, list[Finding]], devices: list[str]) -> dict:
    return {
        "baseline_round": 1,
        "current_round": case_round,
        "resolved": [f.fingerprint for f in buckets["resolved"]],
        "unchanged": [f.fingerprint for f in buckets["unchanged"]],
        "new_related": [f.fingerprint for f in buckets["new_related"]],
        "new_unrelated": [f.fingerprint for f in buckets["new_unrelated"]],
        "possibly_resolved": [f.fingerprint for f in buckets["possibly_resolved"]],
        "device_baseline": devices,
        "counts": {name: len(items) for name, items in buckets.items()},
    }


def _apply_overrides(buckets: dict[str, list[Finding]], overrides: list[dict]) -> None:
    by_fp = {f.fingerprint: f for group in buckets.values() for f in group}
    for item in overrides:
        finding = by_fp.get(item["fingerprint"])
        if finding is None:
            continue
        for name in ("resolved", "unchanged", "new_related", "new_unrelated", "possibly_resolved"):
            buckets[name] = [f for f in buckets[name] if f.fingerprint != finding.fingerprint]
        if item["verdict"] == "suppressed":
            finding.status = "suppressed"
            buckets.setdefault("suppressed", []).append(finding)
        else:
            finding.status = "unchanged"
            buckets["unchanged"].append(finding)


async def _compare(db, case, current: list[Finding]) -> tuple[dict[str, list[Finding]], list[str]]:
    baseline = [f for f in await db.all_findings(case.id) if f.status != "resolved"]
    prev_rows = [_row(f) for f in baseline]
    curr_rows = [_row(f) for f in current]
    prev_of = {id(row): finding for row, finding in zip(prev_rows, baseline)}
    curr_of = {id(row): finding for row, finding in zip(curr_rows, current)}
    diff = match_findings(prev_rows, curr_rows)
    possibly = {id(row) for row in diff.possibly_resolved}
    buckets: dict[str, list[Finding]] = {
        "resolved": [prev_of[id(row)] for row in diff.resolved],
        "unchanged": [
            prev_of[id(pair.prev)] for pair in diff.matched if id(pair.prev) not in possibly
        ],
        "possibly_resolved": [prev_of[id(row)] for row in diff.possibly_resolved],
        "new_related": [],
        "new_unrelated": [],
    }
    patches = await db.list_patch_texts(case.id)
    footprint: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for text in patches:
        for path, spans in footprint_from_unified_diff(text).items():
            footprint[path.replace("\\", "/")].extend(spans)
    attributed = attribute_new_findings(diff.added, footprint)
    related_ids = {id(row) for row, _level in attributed["new_related"]}
    for row in diff.added:
        finding = curr_of[id(row)]
        if id(row) in related_ids:
            finding.origin = "new_related"
            buckets["new_related"].append(finding)
        else:
            finding.origin = "new_unrelated"
            buckets["new_unrelated"].append(finding)
    devices = sorted({f.tool for f in baseline} & {f.tool for f in current})
    return buckets, devices


def _gate(case_round_findings: dict[str, list[Finding]], baseline: list[Finding], current: list[Finding], devices: list[str], level: str, pending: bool, acceptance: Check | None = None) -> dict:
    related = case_round_findings["new_related"]
    effect = Check(
        "effect",
        "fail" if related else "pass",
        f"resolved {len(case_round_findings['resolved'])}，医源性新增 {len(related)}",
    )
    base_counts: dict[tuple[str, str], int] = Counter((f.tool, f.rule_id) for f in baseline)
    cur_counts: dict[tuple[str, str], int] = Counter((f.tool, f.rule_id) for f in current)
    health = health_check(base_counts, cur_counts, set(devices) or {f.tool for f in baseline}, level)
    critical = [f for f in case_round_findings["new_unrelated"] if f.severity == "critical"]
    if critical and level != "lax":
        health = Check(health.name if level == "strict" else "health_critical", "fail", f"critical 覆盖差异 {len(critical)} 条")
    if acceptance is None:
        acceptance = Check("acceptance", "pass", "验收命令白名单为空，本轮未执行命令")
    archive = Check("archive", "fail" if pending else "pass", "仍有未终态药味" if pending else "无未决回滚或悬空药味")
    checks = [effect, health, acceptance, archive]
    summary = gate_verdict(checks, level)
    presented = [
        {"name": _CHECK_NAME.get(c.name, c.name), "verdict": c.verdict, "detail": c.detail[:300]}
        for c in checks
    ]
    return {"verdict": summary["verdict"], "level": level, "checks": presented, "waiver": {}}


def _outcome(buckets: dict[str, list[Finding]], gate: dict, has_manual: bool) -> str:
    resolved = len(buckets["resolved"])
    initial = resolved + len(buckets["unchanged"]) + len(buckets["possibly_resolved"])
    if buckets["new_related"]:
        return "referred"
    if gate["verdict"] == "fail":
        return "referred" if has_manual else "unchanged"
    if resolved > 0 and resolved >= initial:
        return "cured"
    if resolved > 0:
        return "improved"
    return "unchanged"


async def discharge(
    case_id: str,
    overrides: list[dict] | None = None,
    waive: bool = False,
    waiver_user_confirmed: bool = False,
    waive_reason: str = "",
    distill_to_rsi: bool = False,
    stage_confirmed: bool = False,
    rounds_decision: str = "",
    gate_decision: str = "",
    level: str = "standard",
    project_root: str = "",
) -> dict:
    """hosp_discharge。不改仓库代码。"""
    from .reception import resolve_project_root
    from .scanner import all_adapters, collect_findings, register_defaults

    if rounds_decision not in _ROUNDS:
        raise bad_argument("rounds_decision 须为 extend / refer", target="rounds_decision")
    if gate_decision not in _GATE:
        raise bad_argument("gate_decision 须为 accept", target="gate_decision")
    if level not in {"strict", "standard", "lax"}:
        raise bad_argument("level 须为 strict / standard / lax", target="level")
    overrides = overrides or []
    for item in overrides:
        if item.get("verdict") not in {"unresolved", "suppressed"} or not str(item.get("fingerprint", "")).startswith("sha256:"):
            raise bad_argument("overrides 须含 fingerprint 与 verdict", target="overrides")

    if not all_adapters():
        register_defaults()
    root = resolve_project_root(project_root)
    async with Database(root) as db:
        case = await db.get_case(case_id)
        if case is None:
            raise not_found(f"病例不存在：{case_id}", target="case_id", next_action="先 hosp_register 挂号")
        if case.status != "dispensing":
            raise invalid_operation(
                f"病例当前状态为 {case.status}，须完成施治才能出院",
                target="case_id",
                next_action="先 hosp_dispense 发完药味",
            )
        if case.pending_gate.startswith("dose:") and not stage_confirmed:
            return stage_gate_question(
                case_id, case.pending_gate, "hosp_discharge", "确认最后一味护理记录后开始复诊。"
            )
        if case.pending_gate and not case.pending_gate.startswith("dose:"):
            artifact, nxt = STAGE_ARTIFACTS.get(case.pending_gate, (case.pending_gate, "hosp_discharge"))
            raise invalid_operation(
                f"当前待确认的是 {artifact}，不能出院",
                target="case_id",
                next_action=f"按 handoff 调用 {nxt}",
            )
        if case.pending_gate:
            await db.update_case(case_id, pending_gate="")
            case.pending_gate = ""

        rx = await db.latest_prescription(case_id)
        if rx is None or rx["status"] != "approved":
            raise invalid_operation("没有已批准的处方", target="case_id", next_action="先 hosp_prescribe 批准")
        pending = [it for it in rx["items"] if it["status"] == "pending"]
        if pending:
            raise invalid_operation(
                f"还有 {len(pending)} 味药未执行",
                target="case_id",
                next_action="继续 hosp_dispense",
            )

        report_id = new_id("rpt")
        _devices, _limits, current, _ledger = await collect_findings(root, case, report_id)
        buckets, devices = await _compare(db, case, current)
        _apply_overrides(buckets, overrides)
        public = _diff_public(case.round, buckets, devices)
        has_manual = any(it["method"] == "manual" for it in rx["items"])

        if buckets["new_related"] and case.round < case.max_rounds and rounds_decision != "refer":
            return await _readmit(db, root, case, buckets, current, public)

        if buckets["new_related"] and case.round >= case.max_rounds and rounds_decision == "":
            return ask(
                "max_rounds_reached",
                f"疗程已到 {case.round}/{case.max_rounds}，仍有 {len(buckets['new_related'])} 条医源性新症。",
                [("extend", "延长 1 轮再入院"), ("refer", "不再治疗，转院出院")],
                "转院",
                f'hosp_discharge(case_id="{case_id}", rounds_decision="refer")',
            )
        if rounds_decision == "extend":
            case.max_rounds += 1
            await db.update_case(case_id, max_rounds=case.max_rounds)
            return await _readmit(db, root, case, buckets, current, public)
        if rounds_decision == "refer":
            gate_decision = gate_decision or "accept"

        from ..safety.acceptance import acceptance_check

        aliases = [(it.get("acceptance") or {}).get("alias") or "" for it in rx["items"]]
        checked = await acceptance_check(root, aliases)
        acceptance = Check("acceptance", checked["verdict"], checked["detail"])
        gate = _gate(
            buckets, [f for f in await db.all_findings(case.id) if f.status != "resolved"],
            current, devices, level, pending=False, acceptance=acceptance,
        )
        if gate["verdict"] == "fail" and waive:
            if not waiver_user_confirmed or not waive_reason.strip():
                raise forbidden(
                    "豁免须用户本人确认并填写理由",
                    target="waive",
                    next_action="豁免须用户确认后重新调用",
                )
            gate["verdict"] = "waived"
            gate["waiver"] = {
                "approved_by": "user",
                "user_confirmed": True,
                "confirmation_channel": "convention",
                "reason": waive_reason[:300],
            }
        elif gate["verdict"] == "fail" and gate_decision != "accept":
            return ask(
                "gate_waive",
                "出院门禁未通过。可以按未愈或转院归档，也可以请用户本人豁免。",
                [("accept", "接受门禁结果并归档"), ("waive", "请用户确认后豁免")],
                "接受门禁结果并归档",
                f'hosp_discharge(case_id="{case_id}", gate_decision="accept")',
            )

        outcome = _outcome(buckets, gate, has_manual)
        return await _close(db, root, case, buckets, current, public, gate, outcome, report_id, rx, distill_to_rsi)


async def _persist_diff(db, case, buckets: dict[str, list[Finding]], current: list[Finding]) -> None:
    for finding in buckets["resolved"]:
        finding.status = "resolved"
        await db.update_finding(finding.id, status="resolved", last_seen_round=case.round)
    for finding in buckets["unchanged"] + buckets["possibly_resolved"]:
        status = "possibly_resolved" if finding in buckets["possibly_resolved"] else "unchanged"
        finding.status = status
        await db.update_finding(finding.id, status=status, last_seen_round=case.round)
    for finding in buckets.get("suppressed", []):
        await db.update_finding(finding.id, status="suppressed", last_seen_round=case.round)
    fresh = buckets["new_related"] + buckets["new_unrelated"]
    if fresh:
        await db.insert_findings(fresh)


async def _readmit(db, root, case, buckets, current, public) -> dict:
    await _persist_diff(db, case, buckets, current)
    await db.clear_diagnoses(case.id)
    nxt = case.round + 1
    gate = "" if case.auto_advance else "scanned"
    await db.update_case(case.id, status="scanned", round=nxt, pending_gate=gate)
    case.round = nxt
    case.status = "scanned"
    return {
        "status": "readmitted",
        "case_id": case.id,
        "round": nxt,
        "diff": {k: v for k, v in public.items() if k != "counts"},
        "nextAction": "新症待诊断，确认复诊检验报告后调用 hosp_diagnose",
        "handoff": handoff(case.id, "scanned", case.auto_advance, "hosp_diagnose"),
    }


async def _close(db, root, case, buckets, current, public, gate, outcome, report_id, rx, distill: bool) -> dict:
    await _persist_diff(db, case, buckets, current)
    status = "referred" if outcome == "referred" else "discharged"
    discharge_id = new_id("dis")
    folder = hospital_root(root) / "reports" / case.id
    folder.mkdir(parents=True, exist_ok=True)
    watch = list(dict.fromkeys(path for it in rx["items"] for path in it["scope_paths"]))
    follow_path = folder / "follow-up.md"
    follow_path.write_text(
        render_follow_up(case.id, watch, public["resolved"]),
        encoding="utf-8",
    )
    summary_path = folder / "discharge-final.md"
    summary_path.write_text(
        render_discharge_report(case, outcome, gate, public, str(follow_path)),
        encoding="utf-8",
    )
    reports = await db.list_reports(case.id)
    await db.insert_report(ReportRecord(
        id=report_id, case_id=case.id, stage="discharge", round=case.round,
        md_path=str(summary_path),
        summary=f"{outcome} / 门禁 {gate['verdict']}",
        created_at=now_ms(),
    ))
    await db.update_case(case.id, status=status, pending_gate="")
    note = ""
    if distill:
        from ..integrations.rsi import distill_lesson

        resolved = public.get("resolved") or []
        unresolved = public.get("unresolved") or []
        note = await distill_lesson(
            root,
            f"病例 {case.id} 出院",
            (
                f"结局 {outcome}，门禁 {gate.get('verdict', '')}。"
                f"已解决 {len(resolved)} 项，未解决 {len(unresolved)} 项。"
                "发现原文不写入记忆。"
            ),
        )
        from ..core.efficacy import lesson_text, tally
        from ..integrations.rsi import distill_efficacy

        lesson = lesson_text(tally(await db.list_efficacy_rows()))
        extra = await distill_efficacy(root, lesson)
        if extra:
            note = f"{note} {extra}".strip()
    return {
        "status": "discharged",
        "case_id": case.id,
        "outcome": outcome,
        "diff": {k: v for k, v in public.items() if k != "counts"},
        "gate": gate,
        "discharge_id": discharge_id,
        "summary_path": str(summary_path),
        "follow_up": {
            "watch_files": watch,
            "watch_fingerprints": public["resolved"],
            "ci_snippet_path": str(follow_path),
        },
        "report_chain": [r.id for r in reports] + [report_id],
        "handoff": handoff(case.id, "discharged", True, ""),
        "note": note,
    }
