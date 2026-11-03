"""每个阶段、每个步骤的宿主辅助位。

编码和正则只留下章程事实。宿主模型可以读当前产物后交判断。
不交也不改变这一步的状态，交了也不代替尚未闭合的清单。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_assist: ContextVar[dict | None] = ContextVar("code_hospital_assist", default=None)
_root: ContextVar[str] = ContextVar("code_hospital_assist_root", default="")
_format_notes: ContextVar[tuple[str, ...]] = ContextVar("code_hospital_format_notes", default=())

CHARTER = "编码和正则只做规范化和章程。阅读、归因和取舍由宿主模型完成。交卷必须是契约里的对象或「文件:行: 说明」，自由文字不写入报告，也不算产物。"

_TASKS = {
    "registered": "读挂号范围和主诉。尺子还没跑，不要把范围写成已经查完。有结论就按交卷契约写。",
    "scanned": "读未读文件和仪器章程。结论按交卷契约写，不限于章程里已经点到的事项。",
    "diagnosed": "读诊断簇和未读文件。同一缺陷、不该改的命中，都按交卷契约写到文件和行。",
    "prescribed": "读处方草稿。不该自动改的项、还缺上下文的项，按交卷契约写到文件和行。",
    "approved": "读已批准的处方。越界或还没读懂的项按交卷契约写。",
    "dispensing": "读当前这一味。改出范围按交卷契约写。补丁只针对备好的锚点。",
    "dose": "读这一味的施治结果。超出处方范围的改动按交卷契约写。",
    "discharged": "读出院小结。还没读的文件不能写成没有问题。结论按交卷契约写。",
    "need_patch": "药师已经备好锚点。只针对候选块写修复，不要自己另找锚点。",
    "question": "这一步停在门禁。先按交卷契约写下结论，再按 resume_hint 继续。",
    "default": "读这一步的章程事实和未读文件。结论按交卷契约写，自由文字不算产物。",
}


@contextmanager
def assist_scope(assist: dict | None, project_root: str = ""):
    """这一次工具调用里宿主交回的判断。用完即弃。"""
    assist_token = _assist.set(assist if isinstance(assist, dict) else None)
    root_token = _root.set(project_root or "")
    try:
        yield
    finally:
        _assist.reset(assist_token)
        _root.reset(root_token)


def current_assist() -> dict | None:
    return _assist.get()


def current_root() -> str:
    return _root.get() or ""


def step_slot(item: dict) -> dict:
    """一个检查步骤的辅助位。不参与状态计算。"""
    return {
        "optional": True,
        "role": "host_llm",
        "charter": "代码和正则只交出章程事实，不代替读上下文。",
        "task": str(item.get("question") or item.get("id") or ""),
    }


def model_assisted() -> bool:
    """这次调用里有一份对上交卷契约的产物。自由文字不算。"""
    from ..catalog.filing import host_output

    return any(host_output(value)["status"] in {"filed", "held", "clear"} for value in _entries(current_assist()).values())


def format_notes() -> list[str]:
    return list(_format_notes.get())


def model_findings(case_id: str, report_id: str, project_root: str, scope_files: list[str] | None = None) -> list:
    """只收交卷契约里的问题。自由文字不进病历。"""
    from .ids import new_id
    from .models import Finding
    from ..catalog.filing import host_output
    from ..departments.scanner import _read_window
    from .fingerprint import context_anchor as make_anchor

    rows: list = []
    notes: list[str] = []
    seen: set[tuple[str, int, str]] = set()
    files = list(scope_files or [])
    for key, value in _entries(current_assist()).items():
        outcome = host_output(value, files)
        if outcome["status"] == "invalid":
            notes.append(f"{key} 没有按交卷契约写，不能算产物。{outcome['note']}")
            continue
        if outcome["note"] and outcome["status"] != "filed":
            notes.append(f"{key}：{outcome['note']}")
        for issue in outcome["issues"]:
            path = str(issue["file"])
            start = int(issue["line"] or 0)
            end = int(issue["end"] or start)
            message = str(issue["message"])
            ident = (path, start, message)
            if ident in seen:
                continue
            seen.add(ident)
            line_text, before, after = _read_window(project_root, path, start) if path and start else ("", "", "")
            rows.append(Finding(
                id=new_id("fnd"),
                case_id=case_id,
                report_id=report_id,
                tool="host",
                rule_id=str(issue.get("rule_id") or "host.review"),
                severity=str(issue.get("severity") or "major"),
                file_path=path,
                line_start=start,
                line_end=end,
                symbol=str(issue.get("symbol") or ""),
                message=message,
                evidence=str(issue.get("evidence") or line_text),
                fingerprint="",
                context_anchor=make_anchor(before, after),
                concluded_by="host_llm",
                check_id=key if "." in key else "",
            ))
    _format_notes.set(tuple(notes))
    return rows


def attach_ai(payload: dict, assist: dict | None = None, project_root: str = "") -> dict:
    """给出参挂上宿主辅助位。已挂过且这次没有新交卷时保持原样。"""
    if "error" in payload:
        return payload
    if payload.get("ai") and not assist:
        return payload
    from ..catalog.filing import CONTRACT_LINE, CONTRACT_OBJECT, host_output

    stage = _stage(payload)
    incoming = {key: host_output(value) for key, value in _entries(assist).items()}
    case_id = str(payload.get("case_id") or payload.get("id") or "")
    root = project_root or current_root() or _root_from_report(str(payload.get("report_path") or ""))
    filed = _load(root, case_id, stage)
    if incoming and case_id and root:
        filed = _store(root, case_id, stage, incoming)
    elif incoming:
        filed = {**filed, **incoming}
    stamped = dict(payload)
    stamped["ai"] = {
        "role": "host_llm",
        "optional": True,
        "stage": stage,
        "charter": CHARTER,
        "task": _task(stage),
        "file_via": CONTRACT_LINE,
        "output": CONTRACT_OBJECT,
        "filed": filed,
    }
    checks = stamped.get("checks")
    if isinstance(checks, list):
        stamped["checks"] = [_stamp_step(item, filed) for item in checks]
    return stamped


def _stage(payload: dict) -> str:
    handoff = payload.get("handoff")
    if isinstance(handoff, dict) and handoff.get("stage"):
        return str(handoff["stage"])
    status = str(payload.get("status") or "")
    if status == "pending_question":
        kind = str((payload.get("question") or {}).get("kind") or "pending")
        return f"question:{kind}"
    if status == "need_patch":
        return "need_patch"
    return status or "stage"


def _task(stage: str) -> str:
    if stage in _TASKS:
        return _TASKS[stage]
    if stage.startswith("dose:"):
        return _TASKS["dose"]
    if stage.startswith("question:"):
        return _TASKS["question"]
    return _TASKS["default"]


def _entries(assist: dict | None) -> dict:
    """交卷条目。joint 是联合仓库路径，不是这一步的产物。"""
    if not isinstance(assist, dict):
        return {}
    found = {}
    for key, value in assist.items():
        if str(key) == "joint" or value in (None, "", [], {}):
            continue
        found[str(key)] = value
    return found


def _stamp_step(item, filed: dict) -> dict:
    if not isinstance(item, dict):
        return item
    row = dict(item)
    slot = dict(row.get("assist") or step_slot(row))
    note = filed.get(str(row.get("id") or ""))
    if note:
        slot["filed"] = note
    row["assist"] = slot
    return row


def _path(root: str, case_id: str) -> Path:
    from ..storage.db import hospital_root

    return hospital_root(root) / "assist" / f"{case_id}.json"


def _load(root: str, case_id: str, stage: str) -> dict:
    if not root or not case_id:
        return {}
    path = _path(root, case_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    bucket = data.get(stage)
    return dict(bucket) if isinstance(bucket, dict) else {}


def _store(root: str, case_id: str, stage: str, notes: dict) -> dict:
    path = _path(root, case_id)
    current: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, json.JSONDecodeError):
            current = {}
    bucket = dict(current.get(stage) or {})
    bucket.update(notes)
    current[stage] = bucket
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return bucket


def _root_from_report(report_path: str) -> str:
    if not report_path:
        return ""
    parts = Path(report_path).parts
    if ".hospital" not in parts:
        return ""
    index = parts.index(".hospital")
    if index <= 0:
        return ""
    return str(Path(*parts[:index]))
