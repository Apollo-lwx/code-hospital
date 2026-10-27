"""自动档编排：挂号到出院走一遍。煎药处停住，不代宿主写补丁。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from ..core.errors import bad_argument, invalid_operation, not_found
from ..storage.db import Database
from . import diagnostician, discharge, pharmacist, prescriber, reception, scanner

_STEPS = 12
_DONE = {"discharged", "referred"}
_STAGE_AT = {"scanning": 1, "diagnosing": 2, "prescribing": 3, "dispensing": 4, "discharging": 5}
ProgressFn = Callable[[float, float | None, str], Awaitable[None]]


def _paused(payload: dict) -> bool:
    return payload.get("status") == "pending_question"


def _progress(rx: dict | None) -> dict:
    items = (rx or {}).get("items") or []
    applied = sum(1 for item in items if item.get("status") in {"applied", "skipped", "failed"})
    return {"applied": applied, "total": len(items)}


def _stage_of(status: str, pending: bool) -> str:
    if status == "dispensing" and not pending:
        return "discharging"
    return {
        "registered": "scanning",
        "scanned": "diagnosing",
        "diagnosed": "prescribing",
        "prescribed": "prescribing",
        "dispensing": "dispensing",
    }.get(status, "discharging")


def _pending_items(rx: dict | None) -> list[dict]:
    return [item for item in (rx or {}).get("items") or [] if item.get("status") == "pending"]


async def _emit(progress: ProgressFn | None, stage: str, rx: dict | None, item_id: str) -> None:
    if progress is None:
        return
    counts = _progress(rx)
    if counts["total"]:
        current, total = float(counts["applied"]), float(counts["total"])
    else:
        current, total = float(_STAGE_AT[stage]), 5.0
    message = f"{stage}:{item_id}" if item_id else stage
    await progress(current, total, message)


def _suspended(case_id: str, stage: str, rx: dict | None) -> dict:
    return {
        "status": "suspended",
        "case_id": case_id,
        "stage": stage,
        "progress": _progress(rx),
        "nextAction": f'hosp_treat(case_id="{case_id}", resume=true)',
    }


def _needs_downgrade(draft: dict) -> bool:
    policy = draft.get("approval_policy") or {}
    if policy.get("user_required_items"):
        return True
    if draft.get("risk_level") == "high":
        return True
    return any(
        item.get("status") == "pending" and item.get("risk") == "high"
        for item in draft.get("items") or []
    )


async def _case_rx(root: str, case_id: str) -> tuple[str, dict | None]:
    async with Database(root) as db:
        case = await db.get_case(case_id)
        if case is None:
            raise not_found(f"病例不存在：{case_id}", target="case_id", next_action="先 hosp_treat 挂号")
        return case.status, await db.latest_prescription(case_id)


async def treat(
    case_id: str = "",
    resume: bool = False,
    auto_approve_low_risk: bool = True,
    stop_on_drift: bool = True,
    title: str = "",
    scope_type: str = "",
    scope: dict | None = None,
    chief_complaint: str = "",
    max_rounds: int = 3,
    force: bool = False,
    requested_specialties: list[str] | None = None,
    requested_instruments: list[str] | None = None,
    visit_kind: str = "checkup",
    project_root: str = "",
    timeout_ms: int = 120_000,
    progress: ProgressFn | None = None,
) -> dict:
    """hosp_treat。新病例强制 auto_advance，阶段门禁不打断这一次编排。"""
    if timeout_ms < 0:
        raise bad_argument("timeout_ms 不能为负", target="timeout_ms")
    root = reception.resolve_project_root(project_root)
    deadline = time.monotonic() + timeout_ms / 1000
    if resume:
        if not case_id:
            raise bad_argument("resume 时 case_id 必填", target="case_id")
    else:
        if not title.strip() or not scope_type:
            raise bad_argument("自动档挂号需要 title 与 scope_type", target="title")
        opened = await reception.register(
            title=title,
            scope_type=scope_type,
            scope=scope or {},
            chief_complaint=chief_complaint,
            max_rounds=max_rounds,
            force=force,
            auto_advance=True,
            requested_specialties=requested_specialties,
            requested_instruments=requested_instruments,
            visit_kind=visit_kind,
            project_root=root,
        )
        if _paused(opened):
            return opened
        case_id = opened["case_id"]

    for _ in range(_STEPS):
        status, rx = await _case_rx(root, case_id)
        if status in _DONE:
            raise invalid_operation(
                f"病例已是 {status}，自动档不再续跑",
                target="case_id",
                next_action="另挂一号",
            )
        pending = _pending_items(rx)
        stage = _stage_of(status, bool(pending))
        item_id = pending[0]["id"] if status == "dispensing" and pending else ""
        await _emit(progress, stage, rx, item_id)
        if time.monotonic() >= deadline:
            return _suspended(case_id, stage, rx)
        if status == "registered":
            scanned = await scanner.scan(case_id, stage_confirmed=True, project_root=root)
            if _paused(scanned):
                return scanned
            continue
        if status == "scanned":
            diagnosed = await diagnostician.diagnose(case_id, stage_confirmed=True, project_root=root)
            if _paused(diagnosed):
                return diagnosed
            continue
        if status == "diagnosed":
            drafted = await prescriber.prescribe(
                case_id,
                stage_confirmed=True,
                low_confidence="exclude",
                acceptance_decision="skip",
                project_root=root,
            )
            if _paused(drafted):
                return drafted
            if not auto_approve_low_risk:
                return _suspended(case_id, "prescribing", drafted)
            approved = await _approve(case_id, root, drafted)
            if _paused(approved):
                return approved
            continue
        if status == "prescribed":
            if rx is None or rx["status"] == "rejected":
                raise invalid_operation("处方已拒绝，自动档不重开", target="case_id")
            if rx["status"] != "approved":
                if not auto_approve_low_risk:
                    return _suspended(case_id, "prescribing", rx)
                approved = await _approve(case_id, root, rx)
                if _paused(approved):
                    return approved
                continue
            status = "dispensing"
        if status == "dispensing":
            if not pending:
                closed = await discharge.discharge(case_id, stage_confirmed=True, project_root=root)
                return closed
            dose = await pharmacist.dispense(
                rx["id"], stage_confirmed=True, project_root=root,
            )
            if dose.get("status") == "need_patch":
                return {"status": "need_patch", "case_id": case_id, "need_patch": dose}
            if dose.get("blocked_on"):
                return dose
            if _paused(dose):
                return dose
            nursing = dose.get("nursing") or {}
            if stop_on_drift and (nursing.get("drift_files") or nursing.get("content_check") == "fail"):
                _status, latest = await _case_rx(root, case_id)
                return _suspended(case_id, "dispensing", latest)
            continue
        raise invalid_operation(f"病例状态 {status} 不在自动档路径上", target="case_id")

    status, rx = await _case_rx(root, case_id)
    pending = _pending_items(rx)
    return _suspended(case_id, _stage_of(status, bool(pending)), rx)


async def _approve(case_id: str, root: str, draft: dict) -> dict:
    decision = "downgrade" if _needs_downgrade(draft) else ""
    return await prescriber.prescribe(
        case_id,
        approve=True,
        approver="host_agent",
        decision=decision,
        stage_confirmed=True,
        low_confidence="exclude",
        acceptance_decision="skip",
        project_root=root,
    )
