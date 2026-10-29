"""交卷契约。文本行和对象两种写法都收，校验后再计入覆盖账。

对象里没有读完全部范围文件时，不能写成没有功能问题。
"""

from __future__ import annotations

import json

VERSION = 1
CONTRACT_LINE = (
    "交卷契约 v1：文本仍可用；对象为 "
    '{"issues":[{"file":"仓库相对路径","line":1,"message":"说明"}],"read":["没写到问题的文件"]} '
    '或 {"clear":true,"read":["范围内每个文件"]}。'
    "写了问题的文件算看过，其余范围内文件仍要列入 read 或「已读：」，没列全不能诊断。"
)
_OPEN = "尚未交卷，不能写成没有功能问题。"
_BAD_JSON = "交卷契约不是合法 JSON。"
_EMPTY_MESSAGE = "交卷契约的说明不能为空。"
_BAD_ITEM = "交卷契约的 issues 每一项都要是对象。"


def contract_for(item_id: str) -> dict:
    return {
        "version": VERSION,
        "id": item_id,
        "text": "每行「文件:行: 说明」。写了问题的文件算看过，其余文件列入「已读：」。或一行「没有」再一行「已读：」列出范围内每个文件",
        "object": {
            "issues": [{"file": "仓库相对路径", "line": 1, "message": "说明"}],
            "clear": {"clear": True, "read": ["范围内每个文件"]},
        },
    }


def parse_answer(answer, scope_files: list[str]) -> tuple[list[dict], str]:
    """返回病历条目和错误说明。说明为空且条目为空时，表示范围内已通读且没有问题。"""
    if isinstance(answer, dict):
        return _parse_object(answer, scope_files)
    if isinstance(answer, list):
        return _parse_object({"issues": answer}, scope_files)
    text = str(answer or "").strip()
    if text[:1] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [], _BAD_JSON
        if isinstance(data, list):
            data = {"issues": data}
        if isinstance(data, dict):
            return _parse_object(data, scope_files)
        return [], _BAD_JSON
    from .checks import _parse_diff_issues

    return _parse_diff_issues(text, scope_files)


def filing_ledger(
    checks: list[dict],
    answers: dict | None,
    scope_files: list[str],
    *,
    accepted: bool = False,
    previous: dict | None = None,
) -> dict | None:
    """覆盖账上的累计交卷。已写入的事项留在病历里，本轮只追加新条目。"""
    del accepted
    _specs, _note, ledger = settle_filing(checks, answers, scope_files, previous)
    return ledger


def settle_filing(
    checks: list[dict],
    answers: dict | None,
    scope_files: list[str],
    previous: dict | None = None,
) -> tuple[list[dict], str, dict | None]:
    """本轮新条目、说明、累计覆盖账。相同文件行和说明不重复记入。"""
    targets = [
        item for item in checks
        if item.get("id") == "diff.issues" or item.get("group") == "checklist"
    ]
    if not targets:
        return [], "", None
    prior = {row["id"]: row for row in (previous or {}).get("items") or [] if row.get("id")}
    fresh: list[dict] = []
    rows = []
    for item in targets:
        rows.append(_settle_item(item, answers, scope_files, prior.get(item["id"]) or {}, fresh))
    ready = all(row["status"] in {"filed", "clear"} for row in rows)
    return fresh, _round_note(rows, fresh, ready), {"version": VERSION, "items": rows}


def _settle_item(item: dict, answers: dict | None, scope_files: list[str], prior: dict, fresh: list[dict]) -> dict:
    kept = [_kept_row(row) for row in prior.get("kept") or []]
    raw = (answers or {}).get(item["id"])
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        status = prior.get("status") if prior.get("status") in {"filed", "clear"} else "open"
        note = "已交卷" if status == "filed" else "已通读范围内文件" if status == "clear" else _OPEN
        return _ledger_row(item["id"], status, kept, note)
    specs, problem = parse_answer(raw, scope_files)
    if specs:
        for spec in specs:
            if any(_same_issue(spec, row) for row in kept):
                continue
            stamped = dict(spec)
            stamped["check_id"] = item["id"]
            kept.append(_kept_row(stamped))
            fresh.append(stamped)
    if problem:
        return _ledger_row(item["id"], "invalid", kept, problem)
    if specs:
        return _ledger_row(item["id"], "filed", kept, "已交卷")
    if kept:
        return _ledger_row(item["id"], "filed", kept, "已有交卷仍在病历里")
    return _ledger_row(item["id"], "clear", [], "已通读范围内文件")


def _ledger_row(item_id: str, status: str, kept: list[dict], note: str) -> dict:
    return {"id": item_id, "status": status, "issues": len(kept), "note": note, "kept": kept}


def _kept_row(spec: dict) -> dict:
    if "file_path" in spec:
        return {
            "file": spec["file_path"],
            "line": spec["line_start"],
            "end": spec["line_end"],
            "message": spec["message"],
            "check_id": spec.get("check_id") or "",
        }
    return {
        "file": spec.get("file") or "",
        "line": int(spec.get("line") or 0),
        "end": int(spec.get("end") or spec.get("line") or 0),
        "message": spec.get("message") or "",
        "check_id": spec.get("check_id") or "",
    }


def _same_issue(spec: dict, row: dict) -> bool:
    end = row["end"] if row.get("end") else row.get("line")
    return (
        spec["file_path"] == row.get("file")
        and spec["line_start"] == row.get("line")
        and spec["line_end"] == end
        and spec["message"] == row.get("message")
    )


def _round_note(rows: list[dict], fresh: list[dict], ready: bool) -> str:
    total = sum(row["issues"] for row in rows if row["status"] == "filed")
    if ready and total:
        if len(fresh) == total:
            return (
                f"宿主已交卷 diff.issues，写入病历 {total} 条。"
                "对不上现有药的进入待复核，不进处方。"
            )
        return (
            f"宿主已交卷 diff.issues，病历里共 {total} 条，本轮新写入 {len(fresh)} 条。"
            "对不上现有药的进入待复核，不进处方。"
        )
    if ready:
        return "宿主已通读本次 diff，并写明没有功能问题。"
    pending = [
        f"{row['id']}：{row['note']}"
        for row in rows
        if row["status"] not in {"filed", "clear"}
    ]
    written = f"本轮写入病历 {len(fresh)} 条。" if fresh else ""
    return (
        written
        + "按这份清单读 diff 并逐项交卷。未交的项保持未闭合。"
        + "；".join(pending)
        + "。"
    )


def relate_checks(findings, ledger: dict | None) -> dict:
    """清单项指向发现，发现指回清单项。同一条问题可以挂在多条清单上。"""
    by_check: dict[str, list[dict]] = {}
    index: dict[tuple, list[str]] = {}
    for row in (ledger or {}).get("items") or []:
        check_id = str(row.get("id") or "")
        by_check.setdefault(check_id, [])
        for kept in row.get("kept") or []:
            key = _issue_key(kept.get("file"), kept.get("line"), kept.get("end"), kept.get("message"))
            bucket = index.setdefault(key, [])
            if check_id and check_id not in bucket:
                bucket.append(check_id)
    by_finding: dict[str, list[str]] = {}
    for finding in findings or []:
        rule_id = str(_link_field(finding, "rule_id") or "")
        tool = str(_link_field(finding, "tool") or "")
        if rule_id != "diff.issue" and tool != "host":
            continue
        finding_id = str(_link_field(finding, "id") or "")
        if not finding_id:
            continue
        key = _issue_key(
            _link_field(finding, "file_path", "file"),
            _link_field(finding, "line_start", "line"),
            _link_field(finding, "line_end", "end"),
            _link_field(finding, "message"),
        )
        ids = list(index.get(key) or [])
        own = str(_link_field(finding, "check_id") or "")
        if own and own not in ids:
            ids.append(own)
        if not ids:
            continue
        by_finding[finding_id] = ids
        ref = {
            "finding_id": finding_id,
            "file": key[0],
            "line": key[1],
            "message": key[3],
        }
        for check_id in ids:
            bucket = by_check.setdefault(check_id, [])
            if not any(item["finding_id"] == finding_id for item in bucket):
                bucket.append(ref)
    return {"by_check": by_check, "by_finding": by_finding}


def link_text(links: dict | None) -> str:
    lines = []
    for check_id, refs in (links or {}).get("by_check", {}).items():
        if not refs:
            continue
        body = "；".join(f"{ref['file']}:{ref['line']} {ref['message']}" for ref in refs)
        lines.append(f"{check_id} → {body}")
    if not lines:
        return ""
    return "清单与发现：" + "。".join(lines) + "。"


def _issue_key(file, line, end, message) -> tuple:
    try:
        start = int(line or 0)
    except (TypeError, ValueError):
        start = 0
    try:
        stop = int(end or 0) or start
    except (TypeError, ValueError):
        stop = start
    path = str(file or "").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path, start, stop, str(message or "")


def _link_field(item, *names):
    if isinstance(item, dict):
        for name in names:
            if item.get(name) not in (None, ""):
                return item[name]
        return ""
    for name in names:
        if hasattr(item, name):
            value = getattr(item, name)
            if value not in (None, ""):
                return value
    return ""


def _parse_object(data: dict, scope_files: list[str]) -> tuple[list[dict], str]:
    from .checks import _resolve_path, _scope_index

    known = _scope_index(scope_files)
    issues = data.get("issues")
    clear = data.get("clear")
    if issues and clear:
        return [], "同一份交卷里不能既写问题又写没有。"
    if isinstance(issues, list) and issues:
        specs = []
        for row in issues:
            if not isinstance(row, dict):
                return [], _BAD_ITEM
            token = str(row.get("file") or row.get("file_path") or row.get("path") or "").strip()
            path = _resolve_path(token, known)
            if not path:
                shown = token or "（空）"
                return [], f"「{shown}」不在这次合入范围里，不能写入病历。"
            try:
                start = int(row.get("line") or row.get("line_start") or 0)
                end = int(row.get("end") or row.get("line_end") or start)
            except (TypeError, ValueError):
                return [], f"{path} 的行号无效。"
            if start < 1 or end < start:
                return [], f"{path} 的行号无效。"
            message = str(row.get("message") or row.get("msg") or "").strip()
            if not message:
                return [], _EMPTY_MESSAGE
            specs.append({
                "file_path": path,
                "line_start": start,
                "line_end": end,
                "message": message,
            })
        from .checks import _require_scope

        covered = {spec["file_path"] for spec in specs}
        read = data.get("read")
        if isinstance(read, list):
            for token in read:
                found = _resolve_path(str(token), known)
                if found:
                    covered.add(found)
        return _require_scope(covered, known, specs)
    if clear is True:
        found: set[str] = set()
        read = data.get("read")
        if isinstance(read, list):
            for token in read:
                path = _resolve_path(str(token), known)
                if path:
                    found.add(path)
        missing = [path for path in known["paths"] if path not in found]
        if missing:
            shown = "、".join(missing[:8])
            return [], f"还有文件没列入已读（{shown}），不能写成没有功能问题。"
        return [], ""
    from .checks import _parse_diff_issues

    return _parse_diff_issues("", scope_files)
