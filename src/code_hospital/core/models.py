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
    """初发、医源性新症或无关新症。结论来源写在 concluded_by。"""

    INITIAL = "initial"
    NEW_RELATED = "new_related"
    NEW_UNRELATED = "new_unrelated"


class ConcludedBy(str, Enum):
    """谁下的结论。疗效和指纹差分统计 instrument、external、code。"""

    INSTRUMENT = "instrument"
    HOST_LLM = "host_llm"
    EXTERNAL = "external"
    CODE = "code"


def classify_source(tool: str, rule_id: str = "", engine: str = "") -> str:
    """仪器产物、外聘引擎、宿主交卷。模板归因不走这里。"""
    if tool == "host" or rule_id == "diff.issue":
        return ConcludedBy.HOST_LLM.value
    if engine == "external":
        return ConcludedBy.EXTERNAL.value
    return ConcludedBy.INSTRUMENT.value


def counts_toward_efficacy(concluded_by: str) -> bool:
    return concluded_by != ConcludedBy.HOST_LLM.value


def concluded_by_counts(findings) -> dict[str, int]:
    counts = {item.value: 0 for item in ConcludedBy}
    for finding in findings:
        key = getattr(finding, "concluded_by", "") or ConcludedBy.INSTRUMENT.value
        if key not in counts:
            key = ConcludedBy.INSTRUMENT.value
        counts[key] += 1
    return counts


def source_summary(findings) -> str:
    counts = concluded_by_counts(findings)
    parts = [f"仪器 {counts['instrument']} 条", f"模型 {counts['host_llm']} 条"]
    if counts["external"]:
        parts.append(f"外聘 {counts['external']} 条")
    if counts["code"]:
        parts.append(f"代码 {counts['code']} 条")
    return " / ".join(parts)


def finding_rank(finding) -> tuple:
    """宿主交卷排在仪器发现后面，不占严重度的前排。"""
    lead = 0 if counts_toward_efficacy(getattr(finding, "concluded_by", "")) else 1
    return (
        lead,
        SEVERITY_ORDER.get(getattr(finding, "severity", ""), 9),
        getattr(finding, "file_path", "") or "",
        int(getattr(finding, "line_start", 0) or 0),
    )


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
    # 号别：这场要查什么。checkup 是原来的体检套餐。
    visit_kind: str = "checkup"
    # 挂号主诉。业务号把它当作写下来、但这一期不能机械核对的规则。
    chief_complaint: str = ""
    scan_orders: list[dict] = Field(default_factory=list)
    # 本轮仪器结局：[{id, outcome}]，outcome 为 ok / terminal / retry。检查单靠它判断「还没跑」。
    instrument_notes: list[dict] = Field(default_factory=list)
    # 安全号 / 重症号覆盖账。{source_hash, units}。空字典表示这场没建账。
    coverage: dict = Field(default_factory=dict)
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
    # 这条发现来自哪条清单。空字符串表示不是清单交卷。
    check_id: str = ""
    fixable: bool = False
    # 安全科裁决。空字符串表示这台仪器不走独立核实。
    verdict: str = ""
    unresolved: str = ""
    attack_class: str = ""
    message: str
    evidence: str = ""
    fingerprint: str
    # 问题长相（不含出现序号）。复发调档用它；fingerprint 是含 occurrence 的身份指纹。
    content_key: str = ""
    context_anchor: str = ""
    status: str = FindingStatus.NEW.value
    origin: str = Origin.INITIAL.value
    # 谁下的结论。默认定性仪器。宿主交卷写 host_llm。
    concluded_by: str = ConcludedBy.INSTRUMENT.value
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
    # 模板归因写 code。整簇都是宿主交卷时写 host_llm。
    concluded_by: str = ConcludedBy.CODE.value


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
