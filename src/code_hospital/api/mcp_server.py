"""MCP server（`hosp serve`，stdio 传输，mcp 2.x SDK 的 MCPServer）。

已注册 hosp_register / hosp_scan / hosp_diagnose / hosp_prescribe / hosp_dispense / hosp_discharge / hosp_treat / hosp_case。
错误处理：HospitalError → {"error": {code,message,target,nextAction}} 结构化返回
（契约 §0；isError 语义随 SDK 能力在 P1 收紧）。
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from ..core.algorithms import lookup_history
from ..core.errors import HospitalError, internal_error
from ..departments import (
    diagnostician,
    discharge,
    pharmacist,
    prescriber,
    reception,
    scanner,
    treat,
)
from ..storage.db import Database
from ..storage.reports import summarize_scan
from .descriptions import (
    CASE_DESC,
    DIAGNOSE_DESC,
    DISCHARGE_DESC,
    DISPENSE_DESC,
    INSTRUCTIONS,
    PRESCRIBE_DESC,
    REGISTER_DESC,
    SCAN_DESC,
    TREAT_DESC,
)

mcp = MCPServer("code-hospital", instructions=INSTRUCTIONS)


def _guard(payload_or_error):
    """统一错误封装：业务错误 → 契约 Error 对象；未知异常 → InternalError。"""
    if isinstance(payload_or_error, HospitalError):
        return payload_or_error.payload()
    return payload_or_error


@mcp.tool(
    name="hosp_register",
    description=REGISTER_DESC,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
)
async def hosp_register(
    title: str,
    scope_type: str,
    scope: dict | None = None,
    chief_complaint: str = "",
    max_rounds: int = 3,
    force: bool = False,
    auto_advance: bool = False,
    requested_specialties: list[str] | None = None,
    requested_instruments: list[str] | None = None,
    visit_kind: str = "checkup",
    project_root: str = "",
) -> dict:
    try:
        return await reception.register(
            title=title,
            scope_type=scope_type,
            scope=scope or {},
            chief_complaint=chief_complaint,
            max_rounds=max_rounds,
            force=force,
            auto_advance=auto_advance,
            requested_specialties=requested_specialties,
            requested_instruments=requested_instruments,
            visit_kind=visit_kind,
            project_root=project_root,
        )
    except HospitalError as e:
        return _guard(e)
    except Exception as e:  # noqa: BLE001
        return internal_error(f"{type(e).__name__}: {e}").payload()


@mcp.tool(
    name="hosp_scan",
    description=SCAN_DESC,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False),
)
async def hosp_scan(
    case_id: str,
    tools: list[str] | None = None,
    timeout_ms: int = 120_000,
    stage_confirmed: bool = False,
    page: int = 1,
    size: int = 50,
    order_by: str = "severity desc",
    project_root: str = "",
) -> dict:
    try:
        return await scanner.scan(
            case_id=case_id,
            tools=tools,
            timeout_ms=timeout_ms,
            stage_confirmed=stage_confirmed,
            page=page,
            size=size,
            order_by=order_by,
            project_root=project_root,
        )
    except HospitalError as e:
        return _guard(e)
    except Exception as e:  # noqa: BLE001
        return internal_error(f"{type(e).__name__}: {e}").payload()


@mcp.tool(
    name="hosp_diagnose",
    description=DIAGNOSE_DESC,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False),
)
async def hosp_diagnose(
    case_id: str,
    use_llm: bool = False,
    stage_confirmed: bool = False,
    project_root: str = "",
) -> dict:
    try:
        return await diagnostician.diagnose(
            case_id=case_id,
            use_llm=use_llm,
            stage_confirmed=stage_confirmed,
            project_root=project_root,
        )
    except HospitalError as e:
        return _guard(e)
    except Exception as e:  # noqa: BLE001
        return internal_error(f"{type(e).__name__}: {e}").payload()


@mcp.tool(
    name="hosp_prescribe",
    description=PRESCRIBE_DESC,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False),
)
async def hosp_prescribe(
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
    try:
        return await prescriber.prescribe(
            case_id=case_id,
            approve=approve,
            approver=approver,
            user_confirmed=user_confirmed,
            stage_confirmed=stage_confirmed,
            low_confidence=low_confidence,
            acceptance_decision=acceptance_decision,
            decision=decision,
            project_root=project_root,
        )
    except HospitalError as e:
        return _guard(e)
    except Exception as e:  # noqa: BLE001
        return internal_error(f"{type(e).__name__}: {e}").payload()


@mcp.tool(
    name="hosp_dispense",
    description=DISPENSE_DESC,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
)
async def hosp_dispense(
    prescription_id: str,
    item_id: str = "",
    mode: str = "apply",
    patch_token: str = "",
    snapshot_hash: str = "",
    patch: str = "",
    resolution: str = "",
    dry_run: bool = False,
    stage_confirmed: bool = False,
    follow_autofix: bool = False,
    project_root: str = "",
) -> dict:
    try:
        return await pharmacist.dispense(
            prescription_id=prescription_id,
            item_id=item_id,
            mode=mode,
            patch_token=patch_token,
            snapshot_hash=snapshot_hash,
            patch=patch,
            resolution=resolution,
            dry_run=dry_run,
            stage_confirmed=stage_confirmed,
            follow_autofix=follow_autofix,
            project_root=project_root,
        )
    except HospitalError as e:
        return _guard(e)
    except Exception as e:  # noqa: BLE001
        return internal_error(f"{type(e).__name__}: {e}").payload()


@mcp.tool(
    name="hosp_discharge",
    description=DISCHARGE_DESC,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False),
)
async def hosp_discharge(
    case_id: str,
    overrides: list | None = None,
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
    try:
        return await discharge.discharge(
            case_id=case_id,
            overrides=overrides,
            waive=waive,
            waiver_user_confirmed=waiver_user_confirmed,
            waive_reason=waive_reason,
            distill_to_rsi=distill_to_rsi,
            stage_confirmed=stage_confirmed,
            rounds_decision=rounds_decision,
            gate_decision=gate_decision,
            level=level,
            project_root=project_root,
        )
    except HospitalError as e:
        return _guard(e)
    except Exception as e:  # noqa: BLE001
        return internal_error(f"{type(e).__name__}: {e}").payload()


@mcp.tool(
    name="hosp_treat",
    description=TREAT_DESC,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
)
async def hosp_treat(
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
    ctx: Context | None = None,
) -> dict:
    async def _report(amount: float, total: float | None, message: str) -> None:
        if ctx is not None:
            await ctx.report_progress(amount, total, message)

    try:
        return await treat.treat(
            case_id=case_id,
            resume=resume,
            auto_approve_low_risk=auto_approve_low_risk,
            stop_on_drift=stop_on_drift,
            title=title,
            scope_type=scope_type,
            scope=scope,
            chief_complaint=chief_complaint,
            max_rounds=max_rounds,
            force=force,
            requested_specialties=requested_specialties,
            requested_instruments=requested_instruments,
            visit_kind=visit_kind,
            project_root=project_root,
            timeout_ms=timeout_ms,
            progress=_report,
        )
    except HospitalError as e:
        return _guard(e)
    except Exception as e:  # noqa: BLE001
        return internal_error(f"{type(e).__name__}: {e}").payload()


@mcp.tool(
    name="hosp_case",
    description=CASE_DESC,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
)
async def hosp_case(
    case_id: str = "",
    status: str = "",
    include: str = "summary",
    report_id: str = "",
    page: int = 1,
    size: int = 50,
    order_by: str = "created_at desc",
    project_root: str = "",
) -> dict:
    try:
        return await _case_query(case_id, status, include, report_id, page, size, order_by, project_root)
    except HospitalError as e:
        return _guard(e)
    except Exception as e:  # noqa: BLE001
        return internal_error(f"{type(e).__name__}: {e}").payload()


async def _case_query(
    case_id: str, status: str, include: str, report_id: str,
    page: int, size: int, order_by: str, project_root: str,
) -> dict:
    from ..core.errors import not_found
    from ..core.models import page_payload
    from ..departments.reception import resolve_project_root

    root = resolve_project_root(project_root)
    async with Database(root) as db:
        if not case_id:
            cases, total = await db.list_cases(root, status=status, page=page, size=size)
            data = []
            for c in cases:
                counts = await db.case_counts(c.id)
                data.append({
                    "id": c.id, "title": c.title, "status": c.status,
                    "triage_level": c.triage_level, "round": c.round,
                    "max_rounds": c.max_rounds,
                    "pending_gate": c.pending_gate, "auto_advance": c.auto_advance,
                    "visit_kind": c.visit_kind,
                    "counts": counts,
                    "created_at": c.created_at, "updated_at": c.updated_at,
                })
            return page_payload(data, page, size, order_by, total)

        case = await db.get_case(case_id)
        if case is None:
            raise not_found(f"病例不存在：{case_id}", target="case_id")
        result = {
            "id": case.id, "title": case.title, "status": case.status,
            "triage_level": case.triage_level, "visit_kind": case.visit_kind,
            "scope_type": case.scope_type,
            "scope": case.scope.model_dump(), "round": case.round,
            "max_rounds": case.max_rounds,
            "pending_gate": case.pending_gate, "auto_advance": case.auto_advance,
            "counts": await db.case_counts(case.id),
            "created_at": case.created_at, "updated_at": case.updated_at,
        }
        if include == "findings":
            items, total = await db.list_findings(case_id, page=page, size=size)
            # history_refs 为派生字段不入库，查询时按指纹重挂（语义：截至当前的历史）
            history = await db.find_fingerprint_history(
                root,
                sorted({f.fingerprint for f in items if f.fingerprint}),
                exclude_case_id=case_id,
                content_keys=sorted({f.content_key for f in items if f.content_key}),
            )
            for f in items:
                f.history_refs = [
                    {**r, "note": "此前病例曾出现同形态发现（复发）"}
                    for r in lookup_history(history, f.fingerprint, f.content_key)
                ]
            result["findings"] = page_payload(
                [f.model_dump() for f in items], page, size, order_by, total
            )
        elif include == "reports":
            reports = await db.list_reports(case_id)
            result["reports"] = [
                {**r.model_dump(), "content": _read_report(r.md_path) if report_id == r.id else ""}
                for r in reports
                if not report_id or r.id == report_id
            ]
        return result


def _read_report(md_path: str) -> str:
    from pathlib import Path

    p = Path(md_path)
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def serve() -> None:
    mcp.run()


__all__ = ["mcp", "serve", "summarize_scan"]
