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
    # 阶段确认门禁（设计 §4.11 叫号机制）：auto_advance=true 全权委托不设门禁；
    # pending_gate 为挂起待确认的阶段（如 registered/scanned/diagnosed/dose:<item_id>），
    # 空串 = 无挂起。门禁是状态内挂起，不改状态机。
    auto_advance: bool = False
    pending_gate: str = ""
    requested_specialties: list[str] = Field(default_factory=list)
    requested_instruments: list[str] = Field(default_factory=list)
    scan_orders: list[dict] = Field(default_factory=list)
    # 本轮仪器结局：[{id, outcome}]，outcome 为 ok / terminal / retry。检查单靠它判断「还没跑」。
    instrument_notes: list[dict] = Field(default_factory=list)
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
    # 分层尺采用的契约 id，写入 scope_key 的 contract: 段。
    contract_id: str = ""
    fixable: bool = False
    message: str
    evidence: str = ""
    fingerprint: str
    # 问题长相（不含出现序号）。复发调档用它；fingerprint 是含 occurrence 的身份指纹。
    content_key: str = ""
    context_anchor: str = ""
    status: str = FindingStatus.NEW.value
    origin: str = Origin.INITIAL.value
    first_seen_round: int = 1
    last_seen_round: int = 1
    # 同指纹历史复发记录（派生字段，检验时关联，不入库；初发为 []）
    history_refs: list[dict] = Field(default_factory=list)


class Diagnosis(BaseModel):
    """诊断书条目（契约 §2.2）。一个诊断簇一条，成员见 member_ids。"""

    id: str
    case_id: str
    report_id: str
    fingerprint: str
    category: str
    root_cause: str
    severity: str
    confidence: float
    fixability: str
    history_refs: list[dict] = Field(default_factory=list)
    referrals: list[dict] = Field(default_factory=list)
    # 本簇包含的 finding id。处方按它找回范围，不把簇收成单个指纹。
    member_ids: list[str] = Field(default_factory=list)
    specialty_id: str = ""
    formulary_matched: bool = True


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
