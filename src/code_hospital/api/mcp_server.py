"""FastMCP server（`hosp serve`，stdio 传输）。

P0 注册 hosp_register / hosp_scan / hosp_case；P1 起陆续开放其余 5 个工具。
错误处理：HospitalError → {"error": {code,message,target,nextAction}} 结构化返回
（契约 §0；isError 语义随 SDK 能力在 P1 收紧）。
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.errors import HospitalError, internal_error
from ..departments import reception, scanner
from ..storage.db import Database
from ..storage.reports import summarize_scan
from .descriptions import CASE_DESC, INSTRUCTIONS, REGISTER_DESC, SCAN_DESC

mcp = FastMCP("code-hospital", instructions=INSTRUCTIONS)


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
                    "max_rounds": c.max_rounds, "counts": counts,
                    "created_at": c.created_at, "updated_at": c.updated_at,
                })
            return page_payload(data, page, size, order_by, total)

        case = await db.get_case(case_id)
        if case is None:
            raise not_found(f"病例不存在：{case_id}", target="case_id")
        result = {
            "id": case.id, "title": case.title, "status": case.status,
            "triage_level": case.triage_level, "scope_type": case.scope_type,
            "scope": case.scope.model_dump(), "round": case.round,
            "max_rounds": case.max_rounds, "counts": await db.case_counts(case.id),
            "created_at": case.created_at, "updated_at": case.updated_at,
        }
        if include == "findings":
            items, total = await db.list_findings(case_id, page=page, size=size)
            # history_refs 为派生字段不入库，查询时按指纹重挂（语义：截至当前的历史）
            history = await db.find_fingerprint_history(
                root, sorted({f.fingerprint for f in items}), exclude_case_id=case_id
            )
            for f in items:
                f.history_refs = [
                    {**r, "note": "此前病例曾出现同指纹发现（复发）"}
                    for r in history.get(f.fingerprint, [])
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
