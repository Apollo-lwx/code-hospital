"""P0 领域模型（Pydantic），与 IO 契约 §2/§3 一一对应。

wire = snake_case = Pydantic 字段 = SQLite 列名（三层零映射）。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ScopeType(str, Enum):
    REPO = "repo"
    PATHS = "paths"
    STAGED = "staged"
    COMMIT = "commit"
    RANGE = "range"


class TriageLevel(str, Enum):
    EMERGENCY = "emergency"
    OUTPATIENT = "outpatient"
    CHECKUP = "checkup"


class CaseStatus(str, Enum):
    REGISTERED = "registered"
    SCANNED = "scanned"
    DIAGNOSED = "diagnosed"
    PRESCRIBED = "prescribed"
    DISPENSING = "dispensing"
    DISCHARGING = "discharging"
    DISCHARGED = "discharged"
    REFERRED = "referred"


ACTIVE_STATUSES = {
    CaseStatus.REGISTERED.value,
    CaseStatus.SCANNED.value,
    CaseStatus.DIAGNOSED.value,
    CaseStatus.PRESCRIBED.value,
    CaseStatus.DISPENSING.value,
    CaseStatus.DISCHARGING.value,
}


class Severity(str, Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"
    INFO = "info"


SEVERITY_ORDER = {
    Severity.CRITICAL.value: 0,
    Severity.MAJOR.value: 1,
    Severity.MINOR.value: 2,
    Severity.NIT.value: 3,
    Severity.INFO.value: 4,
}


class FindingStatus(str, Enum):
    NEW = "new"
    UNCHANGED = "unchanged"
    RESOLVED = "resolved"
    POSSIBLY_RESOLVED = "possibly_resolved"
    SUPPRESSED = "suppressed"


class Origin(str, Enum):
    INITIAL = "initial"
    NEW_RELATED = "new_related"
    NEW_UNRELATED = "new_unrelated"


class Scope(BaseModel):
    """按 scope_type 取对应字段，多余字段保持空值（契约 §3.1）。"""

    paths: list[str] = Field(default_factory=list)
    commit: str = ""
    range: str = ""


class Case(BaseModel):
    id: str
    project_root: str
    title: str
    scope_type: str
    scope: Scope
    triage_level: str
    status: str
    round: int = 1
    max_rounds: int = 3
    created_at: int
    updated_at: int


class Finding(BaseModel):
    """检验发现（契约 §2.1）。"""

    id: str
    case_id: str
    report_id: str
    tool: str
    rule_id: str
    severity: str
    file_path: str
    line_start: int
    line_end: int
    symbol: str = ""
    message: str
    evidence: str = ""
    fingerprint: str
    context_anchor: str = ""
    status: str = FindingStatus.NEW.value
    origin: str = Origin.INITIAL.value
    first_seen_round: int = 1
    last_seen_round: int = 1


class ReportRecord(BaseModel):
    id: str
    case_id: str
    stage: str
    round: int
    md_path: str
    summary: str
    created_at: int


def page_payload(items: list, page: int, size: int, order_by: str, total: int) -> dict:
    """分页信封（契约 §0）：回显 $page/$size/$orderBy 并带 total。"""
    return {
        "$page": page,
        "$size": size,
        "$orderBy": order_by,
        "total": total,
        "data": items,
    }
