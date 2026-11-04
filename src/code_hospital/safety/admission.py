"""模型结论回到医院之后的准入。只读报告只分区；自动档和施治默认四条都开。

仓库根目录 `code-hospital.yaml` 的 `admission` 可以按链路改开关。
缺文件或写坏时用内置默认，不中断就诊。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import yaml

_CONFIG = "code-hospital.yaml"
_LANES = ("report", "auto_advance", "dispense")
_FLAGS = ("partition", "clues_only", "require_location", "block_clearance")
UNVERIFIED = "尚未核实，不能写成检查通过。"
_PASS_WORDS = ("检查通过", "没有漏洞")


@dataclass(frozen=True)
class AdmissionRules:
    partition: bool = True
    clues_only: bool = False
    require_location: bool = False
    block_clearance: bool = False


_LOOSE = AdmissionRules()
_STRICT = AdmissionRules(
    partition=True,
    clues_only=True,
    require_location=True,
    block_clearance=True,
)
_DEFAULTS = {
    "report": _LOOSE,
    "auto_advance": _STRICT,
    "dispense": _STRICT,
}


def rules_for(project_root: str, lane: str) -> AdmissionRules:
    """这一路用哪一套开关。不认识的链路按施治的严档。

    纪律固化批下来的校验在这里落地成**下限**：客户可以在 `code-hospital.yaml`
    里放宽准入，但放宽不了已经被固化过的那几条。被绕过三次才加的规矩，
    用一行配置就能关掉的话，等于没加。
    """
    return apply_floor(_configured(project_root, lane), project_root)


def apply_floor(rules: AdmissionRules, project_root: str) -> AdmissionRules:
    """把已批准的准入类校验压到规则上。只加不减。"""
    from ..catalog.discipline import requires

    if not requires(project_root, "location_floor"):
        return rules
    return replace(rules, require_location=True, block_clearance=True)


def _configured(project_root: str, lane: str) -> AdmissionRules:
    base = _DEFAULTS.get(lane, _STRICT)
    overlay = _overlay(project_root).get(lane)
    if not isinstance(overlay, dict):
        return base
    updates = {}
    for name in _FLAGS:
        value = overlay.get(name)
        if isinstance(value, bool):
            updates[name] = value
    if not updates:
        return base
    return replace(base, **updates)


def has_location(finding) -> bool:
    path = str(getattr(finding, "file_path", "") or "").strip()
    try:
        line = int(getattr(finding, "line_start", 0) or 0)
    except (TypeError, ValueError):
        line = 0
    if path and line >= 1:
        return True
    if path:
        return True
    return bool(str(getattr(finding, "message", "") or "").strip())


def screen_diagnoses(diagnoses, findings_by_id: dict, rules: AdmissionRules) -> tuple[list, list[dict]]:
    """严格档把宿主交卷挡在处方外。落不到文件和行号的单独记下。"""
    if not rules.clues_only and not rules.require_location:
        return list(diagnoses), []
    kept = []
    drops = []
    for diagnosis in diagnoses:
        if getattr(diagnosis, "concluded_by", "") != "host_llm":
            kept.append(diagnosis)
            continue
        members = [
            findings_by_id[item]
            for item in getattr(diagnosis, "member_ids", []) or []
            if item in findings_by_id
        ]
        located = bool(members) and all(has_location(item) for item in members)
        head = members[0] if members else None
        if not located and rules.require_location:
            reason = "落不到文件和行号，已丢弃"
        elif rules.clues_only:
            reason = "待核实线索不进入处方"
        else:
            kept.append(diagnosis)
            continue
        drops.append({
            "diagnosis_id": diagnosis.id,
            "reason": reason,
            "file": getattr(head, "file_path", "") if head else "",
            "line": int(getattr(head, "line_start", 0) or 0) if head else 0,
        })
    return kept, drops


def guard_clearance(note: str, rules: AdmissionRules) -> str:
    """宿主写了没有，严格档不能把这句话收成检查通过。"""
    text = note or ""
    if not rules.block_clearance or not text.startswith("宿主已通读"):
        return text
    for word in _PASS_WORDS:
        text = text.replace(word, "未核实")
    if UNVERIFIED not in text:
        text = text.rstrip("。") + "。" + UNVERIFIED
    return text


def settle_semantic(clusters, rules: AdmissionRules) -> tuple[list, list[dict]]:
    """语义结论留在诊断里当线索。严格档落不到文件和行号的丢掉并记下。

    引文已经核对上的簇不再加「待核实线索」前缀：那句话的用途是标记「没有可核的东西」，
    已经核过的再加就是自相矛盾。但「不进处方」照旧由 `screen_diagnoses` 管，
    两件事各管一段，不互相顶替。
    """
    kept = []
    drops = []
    for diagnosis, members in clusters:
        if getattr(diagnosis, "concluded_by", "") != "host_llm":
            kept.append((diagnosis, members))
            continue
        if rules.require_location and not (members and all(has_location(item) for item in members)):
            head = members[0] if members else None
            drops.append({
                "diagnosis_id": diagnosis.id,
                "reason": "落不到文件和行号，已丢弃",
                "file": getattr(head, "file_path", "") if head else "",
                "line": int(getattr(head, "line_start", 0) or 0) if head else 0,
            })
            continue
        diagnosis.root_cause = clue_wording(
            diagnosis.root_cause, rules, anchored=_anchored(members),
        )
        kept.append((diagnosis, members))
    return kept, drops


def _anchored(members) -> bool:
    return bool(members) and all(
        getattr(item, "evidence_tier", "") == "anchored" for item in members
    )


def clue_wording(text: str, rules: AdmissionRules, anchored: bool = False) -> str:
    """严格档的宿主结论保留待核实，不写成已经坐实。

    「检查通过」这类断言在所有档位都要打掉转成「未核实」；「待核实线索」前缀只加给
    还核不上的那些。引文已核的结论仍然不进处方，那是另一道闸。
    """
    if not rules.clues_only:
        return text
    cleaned = text or ""
    for word in _PASS_WORDS:
        cleaned = cleaned.replace(word, "未核实")
    if anchored or cleaned.startswith("待核实"):
        return cleaned[:500]
    return ("待核实线索：" + cleaned)[:500]


def _overlay(project_root: str) -> dict:
    path = Path(project_root) / _CONFIG if project_root else None
    if path is None or not path.is_file():
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict):
        return {}
    block = doc.get("admission")
    if not isinstance(block, dict):
        return {}
    return {name: block.get(name) for name in _LANES}
