"""导诊台 Reception（设计 §2.2 / §3.2 节点 1）。

挂号四步：同仓互斥 → 轻量预检分诊（gitleaks 快扫）→ 病例查重 → 建档。
"""

from __future__ import annotations

import os

from ..adapters.gitleaks_adapter import GitleaksAdapter
from ..core.errors import HospitalError, bad_argument, conflict
from ..core.ids import new_id
from ..core.models import Case, Scope, ScopeType, TriageLevel
from ..safety.repo_files import (
    ensure_git_info_exclude,
    ensure_git_repo,
    scope_file_list,
)
from ..storage.db import Database, now_ms
from ..storage.reports import render_register_report


def resolve_project_root(project_root: str = "") -> str:
    """工作区绑定：入参 > HOSP_PROJECT_ROOT 环境变量 > 进程 cwd。"""
    root = project_root or os.environ.get("HOSP_PROJECT_ROOT", "") or os.getcwd()
    return str(os.path.abspath(root))


def _check_catalog_request(specialties: list[str], instruments: list[str]) -> None:
    from ..catalog.formulary import REQUESTABLE_INSTRUMENTS, SPECIALTY_IDS

    unknown = [item for item in specialties if item not in SPECIALTY_IDS]
    if unknown:
        raise bad_argument(
            f"requested_specialties 只能是 {list(SPECIALTY_IDS)}",
            target="requested_specialties",
        )
    unknown_tools = [item for item in instruments if item not in REQUESTABLE_INSTRUMENTS]
    if unknown_tools:
        raise bad_argument(
            "第一期 requested_instruments 只接受 type-ruler",
            target="requested_instruments",
        )


async def register(
    title: str,
    scope_type: str,
    scope: dict,
    chief_complaint: str = "",
    max_rounds: int = 3,
    force: bool = False,
    auto_advance: bool = False,
    requested_specialties: list[str] | None = None,
    requested_instruments: list[str] | None = None,
    visit_kind: str = "checkup",
    project_root: str = "",
) -> dict:
    """hosp_register 主逻辑，返回契约 §3.1 出参。

    auto_advance=false（默认）：挂号单产出后病例挂起（pending_gate=registered），
    用户确认后 hosp_scan(stage_confirmed=true) 才执行检验（设计 §4.11 叫号机制）；
    auto_advance=true：全权委托/绿色通道，全程不设阶段确认门禁。
    """
    root = resolve_project_root(project_root)
    if scope_type not in {e.value for e in ScopeType}:
        raise bad_argument(
            f"scope_type 须为 {[e.value for e in ScopeType]} 之一", target="scope_type"
        )
    if not title.strip():
        raise bad_argument("title 不能为空（给这次就诊起个名字）", target="title")
    if not (1 <= max_rounds <= 10):
        raise bad_argument("max_rounds 须在 1..10", target="max_rounds")
    specialties = list(requested_specialties or [])
    instruments = list(requested_instruments or [])
    _check_catalog_request(specialties, instruments)
    from ..catalog.visits import normalize_visit_kind

    try:
        kind = normalize_visit_kind(visit_kind)
    except ValueError as exc:
        raise bad_argument(f"visit_kind 只能是 {exc}", target="visit_kind") from exc
    await ensure_git_repo(root)

    scope_obj = Scope(
        paths=list(scope.get("paths") or []),
        commit=str(scope.get("commit") or ""),
        range=str(scope.get("range") or ""),
    )

    async with Database(root) as db:
        # 1) 同仓互斥（设计 §4.7）
        active = await db.get_active_case(root)
        if active is not None:
            if not force:
                raise conflict(
                    f"本仓库已有活跃病例 {active.id}（{active.title}，状态 {active.status}）",
                    target="case_id",
                    next_action="先完成该病例并出院，或以 force=true 将其转诊后重新挂号",
                )
            await db.update_case(active.id, status="referred")

        # 2) 轻量预检分诊（设计 §3.2：staged/diff/paths 快扫；repo 全量留给 scan 升级）
        files, _skipped = await scope_file_list(root, scope_type, scope_obj.model_dump())
        precheck_hits = 0
        if scope_type != ScopeType.REPO.value:
            precheck_hits = await _precheck_emergency(root, files)
        triage = (
            TriageLevel.EMERGENCY.value
            if precheck_hits > 0
            else (
                TriageLevel.CHECKUP.value
                if scope_type == ScopeType.REPO.value
                else TriageLevel.OUTPATIENT.value
            )
        )

        # 3) 病例查重（same_file 维度）
        duplicates = await db.find_duplicate_cases(root, files)

        # 4) 建档（默认挂起等叫号：用户确认挂号单后才放行检验）
        ts = now_ms()
        case = Case(
            id=new_id("case"),
            project_root=root,
            title=title.strip(),
            scope_type=scope_type,
            scope=scope_obj,
            triage_level=triage,
            status="registered",
            round=1,
            max_rounds=max_rounds,
            auto_advance=auto_advance,
            pending_gate="" if auto_advance else "registered",
            requested_specialties=specialties,
            requested_instruments=instruments,
            visit_kind=kind,
            chief_complaint=chief_complaint.strip()[:500],
            created_at=ts,
            updated_at=ts,
        )
        try:
            await db.insert_case(case)
        except Exception as e:  # 并发兜底：部分唯一索引命中
            raise conflict(f"挂号失败（并发撞单）：{e}", next_action="稍后重试") from e

        ensure_git_info_exclude(root)

        from ..core.models import ReportRecord
        from ..storage.db import hospital_root

        report_id = new_id("rpt")
        md = render_register_report(case, report_id, precheck_hits, duplicates)
        report_path = hospital_root(root) / "reports" / case.id / "register-r1.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md, encoding="utf-8")
        await db.insert_report(
            ReportRecord(
                id=report_id, case_id=case.id, stage="register", round=1,
                md_path=str(report_path),
                summary=f"挂号完成：{triage}，范围 {scope_type}（{len(files)} 个文件）",
                created_at=now_ms(),
            )
        )

    from ..core.questions import handoff

    return {
        "case_id": case.id,
        "triage_level": triage,
        "visit_kind": kind,
        "status": "registered",
        "duplicate_cases": duplicates,
        "precheck": {"ran": scope_type != ScopeType.REPO.value, "hits": precheck_hits},
        "report_path": str(report_path),
        "handoff": handoff(case.id, "registered", auto_advance, next_tool="hosp_scan"),
    }


async def _precheck_emergency(project_root: str, files: list[str]) -> int:
    """gitleaks 快扫 scope 文件，返回急诊特征命中数。设备不可用视为 0。"""
    if not files:
        return 0
    adapter = GitleaksAdapter()
    if not adapter.is_available():
        return 0
    try:
        findings = await adapter.scan(project_root, files, timeout_ms=30_000)
    except Exception:
        return 0
    return len(findings)


def error_payload(err: HospitalError) -> dict:
    return err.payload()
