"""自动档入口。循环在 runtime.driver，默认仍是同步 12 步。"""

from __future__ import annotations

from ..api.capabilities import hosted
from ..core.errors import bad_argument
from ..runtime.driver import ProgressFn, driver

__all__ = ["treat", "driver", "ProgressFn"]


@hosted
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
    """hosp_treat。负超时在入口拒绝。其余交给当前 Driver。"""
    if timeout_ms < 0:
        raise bad_argument("timeout_ms 不能为负", target="timeout_ms")
    return await driver.run(
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
        progress=progress,
    )
