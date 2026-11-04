"""全库阅读调度。内置事项是提醒，不是宿主能看的白名单。

模型常常不会自己扫完。医院把未读文件分批交出去，并记下已读。
业务如果落到别的仓库，只把路径记进联合分析，不把没读的仓写成已经正确。
"""

from __future__ import annotations

import json
from pathlib import Path

BATCH = 40
CHARTER = (
    "内置能力是规范、指标和药方，不是诊断白名单。"
    "行数、any 这类事项可以提醒，不能限制宿主还诊断什么。"
)
TASK = (
    "读这一批里的每个文件。业务是否正确、类型、安全、跨文件调用都要看，不限于提醒事项。"
    "文档（md/rst/adoc）和 XML 也在范围内：文档写的是这个项目的规范与流程；"
    "XML 要按内容判断它是什么——SQL 映射、构建声明还是运行时行为，不看文件名猜。"
    "交卷按契约：可以写结论、严重度、符号、证据、归因、相关文件和跨库转诊。行和文件可以空。"
    "文档和 XML 的结论要进报告就抄原文：把那一句逐字写进 evidence，医院拿它比文件。"
    "没抄原文的结论只能落到文件行，定级压到一般问题；抄了但对不上会记执行偏差。"
    "同一批里对不上的只跳过那一条。自由文字不算产物。没读的文件不能写成已经分析完。"
)
_DOC_SUFFIX = {".md", ".markdown", ".rst", ".adoc", ".txt"}
JOINT = (
    "业务正确性如果依赖别的仓库，把仓库路径写进 assist.joint。"
    "那些路径记入联合分析，不自动当成已经读过，也不能写成行为已经正确。"
)


def record(coverage: dict | None, files: list[str], assist: dict | None = None, answers: dict | None = None) -> dict:
    """记下本轮已读和联合仓库，返回覆盖账。"""
    out = dict(coverage or {})
    state = dict(out.get("host_scan") or {})
    read = {str(item) for item in (state.get("read") or [])}
    joint = [str(item) for item in (state.get("joint") or [])]
    read.update(_reads(assist))
    read.update(_reads(answers))
    for path in _joint_paths(assist):
        if path not in joint:
            joint.append(path)
    known = set(_normalize(files))
    out["host_scan"] = {
        "read": sorted(path for path in read if path in known),
        "joint": joint,
    }
    return out


def view(files: list[str], coverage: dict | None, topics: list[str] | None = None) -> dict:
    """下一批未读文件。事项只作为提醒附在旁边。"""
    ordered = _normalize(files)
    read = set((coverage or {}).get("host_scan", {}).get("read") or [])
    unread = [path for path in ordered if path not in read]
    batch = unread[:BATCH]
    start = 0 if not unread else ordered.index(batch[0]) // BATCH + 1
    return {
        "role": "host_llm",
        "charter": CHARTER,
        "task": TASK,
        "batch_note": batch_note(batch),
        "topics_are_not_limits": True,
        "topics": list(topics or []),
        "batch_index": start,
        "batch": batch,
        "unread": len(unread),
        "read": len(ordered) - len(unread),
        "total": len(ordered),
        "joint": list((coverage or {}).get("host_scan", {}).get("joint") or []),
        "joint_note": JOINT,
    }


def batch_note(batch: list[str]) -> str:
    """这一批里混了文档和 XML 时要说一声，否则它们最容易被当成「不是源码」跳过。"""
    docs = [path for path in batch if Path(path).suffix.lower() in _DOC_SUFFIX]
    xmls = [path for path in batch if Path(path).suffix.lower() == ".xml"]
    if not docs and not xmls:
        return ""
    bits = []
    if docs:
        bits.append(f"{len(docs)} 份文档")
    if xmls:
        bits.append(f"{len(xmls)} 份 XML")
    return (
        f"本批含 {'、'.join(bits)}。文档要读规范与流程；XML 要读内容判断是 SQL 映射、"
        "构建声明还是运行时配置，不要因为不是源码就跳过，也不要只凭文件名归类。"
    )


def write_plan(project_root: str, case_id: str, files: list[str], coverage: dict | None) -> str:
    """把全部分批写到病历，宿主不用一次把库读完也能接着下一批。"""
    from ..storage.db import hospital_root

    ordered = _normalize(files)
    read = set((coverage or {}).get("host_scan", {}).get("read") or [])
    batches = [ordered[index:index + BATCH] for index in range(0, len(ordered), BATCH)]
    payload = {
        "charter": CHARTER,
        "task": TASK,
        "topics_are_not_limits": True,
        "batches": [
            {
                "index": index + 1,
                "files": batch,
                "read": all(path in read for path in batch),
                **({"note": note} if (note := batch_note(batch)) else {}),
            }
            for index, batch in enumerate(batches)
        ],
        "joint": list((coverage or {}).get("host_scan", {}).get("joint") or []),
        "joint_note": JOINT,
    }
    path = hospital_root(project_root) / "host-scan" / f"{case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _normalize(files: list[str]) -> list[str]:
    found = []
    seen = set()
    for raw in files:
        path = str(raw).replace("\\", "/").lstrip("./")
        if not path or path in seen:
            continue
        seen.add(path)
        found.append(path)
    return sorted(found)


def _reads(payload: dict | None) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    found = set()
    for value in payload.values():
        found.update(_read_value(value))
    return found


def _read_value(value) -> set[str]:
    if isinstance(value, dict):
        found = set()
        read = value.get("read")
        if isinstance(read, list):
            for item in read:
                path = str(item).replace("\\", "/").lstrip("./")
                if path:
                    found.add(path)
        for key, item in value.items():
            if key == "read":
                continue
            if isinstance(item, str) and "已读" in item:
                found.update(_read_line(item))
        return found
    if isinstance(value, str) and "已读" in value:
        return _read_line(value)
    return set()


def _read_line(text: str) -> set[str]:
    found = set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("已读"):
            body = line.split("：", 1)[-1].split(":", 1)[-1]
            for token in body.replace("，", " ").replace(",", " ").split():
                path = token.replace("\\", "/").lstrip("./")
                if path:
                    found.add(path)
    return found


def _joint_paths(assist: dict | None) -> list[str]:
    if not isinstance(assist, dict) or "joint" not in assist:
        return []
    value = assist.get("joint")
    if isinstance(value, str):
        parts = [value]
    elif isinstance(value, list):
        parts = [str(item) for item in value]
    else:
        return []
    found = []
    for raw in parts:
        text = raw.strip()
        if text and Path(text).exists():
            found.append(str(Path(text)))
    return found
