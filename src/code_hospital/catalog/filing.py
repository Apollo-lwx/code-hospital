"""交卷契约。文本行和对象两种写法都收，校验后再计入覆盖账。

对象里没有读完全部范围文件时，不能写成没有功能问题。
"""

from __future__ import annotations

import json

VERSION = 1
CONTRACT_OBJECT = {
    "issues": [{
        "file": "仓库相对路径，没有单点时可以空",
        "line": 0,
        "end": 0,
        "symbol": "",
        "message": "结论",
        "severity": "critical|major|minor|nit|info",
        "evidence": "",
        "rule_id": "",
        "category": "",
        "root_cause": "",
        "confidence": 0,
        "fixability": "autofix|llm_patch|manual",
        "files": ["同一结论里的其他文件"],
        "referrals": [{
            "target_repo": "别的仓库路径",
            "hypothesis": "业务是否对得上",
            "evidence": "",
            "suggested_triage": "checkup",
        }],
    }],
    "read": ["没写到问题的文件"],
}
CONTRACT_LINE = (
    "交卷契约 v1。文本行仍是「文件:行: 说明」。"
    "对象用 issues，也可以用 findings、diagnoses 或 items。"
    "条目可以写文件、行、符号、结论、严重度、证据、病种、归因、置信度、处置、相关文件和跨库转诊。"
    "行号可以是 0，文件可以空。file_path、line_start、summary、root_cause 这些别名同样算。"
    "同一批里对不上的只跳过那一条，不把写对的退回。"
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
            "issues": CONTRACT_OBJECT["issues"],
            "clear": {"clear": True, "read": ["范围内每个文件"]},
        },
    }


def host_output(value, scope_files: list[str] | None = None) -> dict:
    """把宿主交回的内容收成交卷契约。自由文字不是产物。"""
    if not _is_contract(value):
        return {
            "version": VERSION,
            "status": "invalid",
            "issues": [],
            "note": "交卷要按契约写。文本用「文件:行: 说明」；对象用 issues、findings、diagnoses 或 items。自由文字不算产物。",
        }
    provisional = scope_files is None
    files = list(scope_files) if scope_files is not None else _mentioned(value)
    specs, problem = parse_answer(value, files)
    issues = [_public_issue(spec) for spec in specs]
    if specs and (problem or provisional):
        status = "held"
        note = problem or "格式符合交卷契约。还要按这次范围核对已读，没列全不能写成已经看完。"
    elif specs:
        status = "filed"
        note = "已交卷"
    elif not problem:
        status = "clear" if not provisional else "held"
        note = "已通读范围内文件" if status == "clear" else "写了没有。还要按这次范围核对已读。"
    else:
        status = "invalid"
        note = problem
    return {"version": VERSION, "status": status, "issues": issues, "note": note}


def _public_issue(spec: dict) -> dict:
    return {
        "file": spec.get("file_path") or "",
        "line": spec.get("line_start") or 0,
        "end": spec.get("line_end") or spec.get("line_start") or 0,
        "symbol": spec.get("symbol") or "",
        "message": spec.get("message") or "",
        "severity": spec.get("severity") or "major",
        "evidence": spec.get("evidence") or "",
        "rule_id": spec.get("rule_id") or "",
        "files": list(spec.get("files") or []),
        "referrals": list(spec.get("referrals") or []),
    }


def _is_contract(value) -> bool:
    if isinstance(value, dict):
        if any(key in value for key in ("issues", "findings", "diagnoses", "items", "clear", "read")):
            return True
        return _looks_like_entry(value)
    if isinstance(value, list):
        return bool(value)
    text = str(value or "").strip()
    if not text:
        return False
    if text[0] in "{[":
        return True
    from .checks import _ISSUE_LINE, _NONE_LINE, _READ_LINE

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _NONE_LINE.match(line) or _READ_LINE.match(line) or _ISSUE_LINE.match(line):
            continue
        return False
    return True


def _mentioned(value) -> list[str]:
    paths: list[str] = []
    if isinstance(value, list):
        value = {"issues": value}
    if isinstance(value, dict):
        for key in ("issues", "findings", "diagnoses", "items"):
            for row in value.get(key) or []:
                if isinstance(row, dict):
                    paths.extend(_row_paths(row))
        if _looks_like_entry(value):
            paths.extend(_row_paths(value))
        for token in value.get("read") or []:
            if str(token).strip():
                paths.append(str(token).strip())
        return paths
    from .checks import _ISSUE_LINE, _READ_LINE

    for raw in str(value).splitlines():
        line = raw.strip()
        issue = _ISSUE_LINE.match(line)
        if issue:
            paths.append(issue.group("path").strip())
            continue
        read = _READ_LINE.match(line)
        if read:
            paths.extend(token for token in read.group("body").replace("，", " ").replace(",", " ").split() if token)
    return paths


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
    rows = _entry_rows(data)
    clear = data.get("clear")
    if rows and clear:
        return [], "同一份交卷里不能既写问题又写没有。"
    if rows:
        specs = []
        skipped = []
        for row in rows:
            spec, problem = _normalize_entry(row, known)
            if spec:
                specs.append(spec)
            elif problem:
                skipped.append(problem)
        if not specs:
            return [], skipped[0] if skipped else _EMPTY_MESSAGE
        from .checks import _require_scope

        covered = {spec["file_path"] for spec in specs if spec.get("file_path")}
        for spec in specs:
            covered.update(spec.get("files") or [])
        read = data.get("read")
        if isinstance(read, list):
            for token in read:
                found = _resolve_path(str(token), known)
                if found:
                    covered.add(found)
        specs, scope_problem = _require_scope(covered, known, specs)
        note = scope_problem
        if skipped:
            tail = "同批跳过：" + "；".join(skipped[:4])
            note = f"{note} {tail}".strip() if note else tail
        return specs, note
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


_ENTRY_LISTS = ("issues", "findings", "diagnoses", "items")
_SEVERITY = {"critical", "major", "minor", "nit", "info"}
_METHOD = {"autofix", "llm_patch", "manual"}
_TRIAGE = {"emergency", "outpatient", "checkup"}


def _entry_rows(data: dict) -> list:
    rows: list = []
    listed = False
    for key in _ENTRY_LISTS:
        if key not in data:
            continue
        listed = True
        value = data.get(key)
        if isinstance(value, list):
            rows.extend(value)
    if rows or listed:
        return rows
    if _looks_like_entry(data):
        return [data]
    return []


def _looks_like_entry(row: dict) -> bool:
    return any(
        isinstance(row.get(key), str) and str(row.get(key)).strip()
        for key in ("message", "msg", "summary", "description", "root_cause", "hypothesis", "instructions")
    )


def _row_paths(row: dict) -> list[str]:
    found = []
    token = _text(row, "file", "file_path", "path", "target_file")
    if token:
        found.append(token)
    for key in ("files", "related"):
        for item in row.get(key) or []:
            text = str(item).strip()
            if text:
                found.append(text)
    return found


def _text(row: dict, *names: str) -> str:
    for name in names:
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_entry(row, known: dict) -> tuple[dict | None, str]:
    from .checks import _resolve_path

    if not isinstance(row, dict):
        return None, _BAD_ITEM
    message = _text(row, "message", "msg", "summary", "description", "instructions")
    root_cause = _text(row, "root_cause", "hypothesis")
    if not message:
        message = root_cause
        root_cause = ""
    if not message:
        return None, _EMPTY_MESSAGE
    token = _text(row, "file", "file_path", "path", "target_file")
    path = ""
    if token:
        path = _resolve_path(token, known)
        if not path:
            return None, f"「{token}」不在这次合入范围里，不能写入病历。"
    related: list[str] = []
    for item in list(row.get("files") or []) + list(row.get("related") or []):
        found = _resolve_path(str(item).strip(), known) if str(item).strip() else ""
        if found and found != path and found not in related:
            related.append(found)
    if not path and related:
        path, related = related[0], related[1:]
    referrals = _referrals(row.get("referrals"))
    symbol = _text(row, "symbol", "target_symbol")
    start, end = _span(row)
    severity = _text(row, "severity").lower()
    if severity not in _SEVERITY:
        severity = "major"
    category = _text(row, "category", "rule_id")
    fixability = _text(row, "fixability", "method").lower()
    if fixability not in _METHOD:
        fixability = ""
    evidence = _text(row, "evidence")[:200]
    if root_cause and root_cause not in message:
        message = f"{message} 归因：{root_cause}"
    if related:
        message = f"{message} 还涉及 {'、'.join(related)}"
    for ref in referrals:
        extra = ref["hypothesis"] or ref["evidence"]
        message = f"{message} 跨库 {ref['target_repo']}：{extra}"
    if fixability:
        message = f"{message} 处置：{fixability}"
    return {
        "file_path": path,
        "line_start": start,
        "line_end": end,
        "message": message[:500],
        "severity": severity,
        "symbol": symbol,
        "evidence": evidence,
        "rule_id": category,
        "files": related,
        "referrals": referrals,
        "fixability": fixability,
    }, ""


def _span(row: dict) -> tuple[int, int]:
    start = _number(row.get("line") if row.get("line") not in (None, "") else row.get("line_start"), 0)
    end_raw = row.get("end") if row.get("end") not in (None, "") else row.get("line_end")
    end = _number(end_raw, start)
    if start < 0:
        start = 0
    if end < start:
        end = start
    return start, end


def _number(value, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _referrals(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    found = []
    for row in value:
        if not isinstance(row, dict):
            continue
        target = str(row.get("target_repo") or row.get("repo") or "").strip()
        if not target:
            continue
        triage = str(row.get("suggested_triage") or "checkup").strip()
        if triage not in _TRIAGE:
            triage = "checkup"
        found.append({
            "target_repo": target,
            "evidence": str(row.get("evidence") or "").strip()[:200],
            "hypothesis": str(row.get("hypothesis") or row.get("message") or "").strip()[:300],
            "suggested_triage": triage,
        })
    return found
