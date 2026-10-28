"""外聘仪器进程。命令名必须在 PATH 上，参数不能带 shell 元字符。

不经 shell。环境只留验收命令那份裁剪结果。超时和输出长度有上限。
进程沙箱默认关闭；名册写了 sandbox 也不在这一刀里隔离。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil

from ..safety.acceptance import scrub_env

ABSENT = "外聘仪器未到岗"
EMPTY = "外聘仪器已到岗，没有交产物，不能写成没问题"
QUIET = "外聘仪器已到岗，范围内没有命中"
SANDBOX_OFF = "进程沙箱默认关闭，这次没有隔离。"
_MAX_OUTPUT = 64 * 1024
_MAX_FINDINGS = 200
_META = set("|;&<>`$\\\n\r")
_TEXT_LINE = re.compile(
    r"^(?P<path>[^\s:]+):(?P<line>\d+)(?:-(?P<end>\d+))?:\s*(?P<msg>\S.*)$"
)


def safe_args(raw) -> list[str] | None:
    """参数必须是字符串列表。缺省是空列表。含元字符、绝对路径或 .. 则整份作废。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    kept: list[str] = []
    for item in raw:
        text = str(item)
        if not text or any(char in text for char in _META):
            return None
        path = text.replace("\\", "/")
        if path.startswith("/") or path.startswith("../") or "/../" in path or path == "..":
            return None
        if len(path) > 1 and path[1] == ":":
            return None
        kept.append(text)
    return kept


async def run_hired(root: str, instrument: dict, files: list[str], timeout_ms: int) -> dict:
    ident = str(instrument.get("id") or instrument.get("binary") or "external")
    if instrument.get("sandbox"):
        limitations = [f"{ident}：{SANDBOX_OFF}"]
    else:
        limitations = []
    args = safe_args(instrument.get("args"))
    if args is None:
        limitations.append(f"{ident}：参数含禁止字符，没有启动")
        return {
            "record": _record(ident, "failed", "参数含禁止字符，没有启动", 0),
            "raws": [],
            "limitations": limitations,
        }
    resolved = shutil.which(str(instrument.get("binary") or ""))
    if not resolved:
        limitations.append(f"{ident}：{ABSENT}")
        return {
            "record": _record(ident, "absent", ABSENT, 0),
            "raws": [],
            "limitations": limitations,
        }
    timeout = max(float(timeout_ms or 0) / 1000, 1.0)
    argv = [resolved, *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=root,
            env=scrub_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        limitations.append(f"{ident}：{ABSENT}")
        return {
            "record": _record(ident, "absent", ABSENT, 0),
            "raws": [],
            "limitations": limitations,
        }
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        limitations.append(f"{ident}：{EMPTY}")
        return {
            "record": _record(ident, "empty", EMPTY, 0),
            "raws": [],
            "limitations": limitations,
        }
    text = out[:_MAX_OUTPUT].decode("utf-8", errors="replace")
    scope = {item.replace("\\", "/").lstrip("./") for item in files}
    raws, kind = parse_artifact(text, scope)
    if kind == "quiet":
        limitations.append(f"{ident}：{QUIET}")
        return {
            "record": _record(ident, "quiet", QUIET, 0),
            "raws": [],
            "limitations": limitations,
        }
    if kind != "findings" or not raws:
        limitations.append(f"{ident}：{EMPTY}")
        return {
            "record": _record(ident, "empty", EMPTY, 0),
            "raws": [],
            "limitations": limitations,
        }
    note = f"外聘仪器已交产物 {len(raws)} 条"
    return {
        "record": _record(ident, "ran", note, len(raws)),
        "raws": raws,
        "limitations": limitations,
    }


def parse_raws(text: str, scope: set[str]) -> list[dict]:
    raws, _kind = parse_artifact(text, scope)
    return raws


def parse_artifact(text: str, scope: set[str]) -> tuple[list[dict], str]:
    """认出 JSON findings、SARIF、文本行。返回 (行, findings|quiet|unparsed)。"""
    body = text.strip()
    if not body:
        return [], "unparsed"
    if body[0] in "{[":
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return [], "unparsed"
        return _parse_json(data, scope)
    return _parse_lines(body, scope)


def _parse_json(data, scope: set[str]) -> tuple[list[dict], str]:
    if isinstance(data, dict) and isinstance(data.get("runs"), list):
        return _parse_sarif(data, scope)
    rows = data if isinstance(data, list) else None
    if rows is None and isinstance(data, dict) and "findings" in data:
        rows = data.get("findings")
    if not isinstance(rows, list):
        return [], "unparsed"
    if not rows:
        return [], "quiet"
    raws: list[dict] = []
    recognized = False
    for row in rows[:_MAX_FINDINGS]:
        if not isinstance(row, dict):
            continue
        path_text = str(row.get("file") or row.get("file_path") or row.get("path") or "")
        message = str(row.get("message") or row.get("msg") or "").strip()
        if not path_text and not message:
            continue
        recognized = True
        path = _in_scope(path_text, scope)
        line, end = _lines(row.get("line") or row.get("line_start"), row.get("line_end"))
        if not path or line < 1 or not message:
            continue
        raws.append(_raw(
            str(row.get("rule_id") or "external"),
            message,
            path,
            line,
            end,
            "major",
        ))
    if not recognized:
        return [], "unparsed"
    if not raws:
        return [], "quiet"
    return raws, "findings"


def _parse_sarif(data: dict, scope: set[str]) -> tuple[list[dict], str]:
    results: list[dict] = []
    for run in data.get("runs") or []:
        if isinstance(run, dict) and isinstance(run.get("results"), list):
            results.extend(item for item in run["results"] if isinstance(item, dict))
    if not results:
        return [], "quiet"
    raws: list[dict] = []
    for result in results[:_MAX_FINDINGS]:
        locations = result.get("locations")
        if not isinstance(locations, list) or not locations:
            continue
        physical = locations[0].get("physicalLocation") if isinstance(locations[0], dict) else None
        if not isinstance(physical, dict):
            continue
        artifact = physical.get("artifactLocation") or {}
        region = physical.get("region") or {}
        path = _in_scope(str(artifact.get("uri") or ""), scope)
        line, end = _lines(region.get("startLine"), region.get("endLine"))
        message = result.get("message")
        if isinstance(message, dict):
            message = str(message.get("text") or "")
        message = str(message or "").strip()
        if not path or line < 1 or not message:
            continue
        level = str(result.get("level") or "")
        raws.append(_raw(
            str(result.get("ruleId") or "external"),
            message,
            path,
            line,
            end,
            "minor" if level == "warning" else "major",
        ))
    if not raws:
        return [], "quiet"
    return raws, "findings"


def _parse_lines(text: str, scope: set[str]) -> tuple[list[dict], str]:
    raws: list[dict] = []
    saw = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _TEXT_LINE.match(line)
        if match is None:
            return [], "unparsed"
        saw = True
        path = _in_scope(match.group("path"), scope)
        start, end = _lines(match.group("line"), match.group("end"))
        message = match.group("msg").strip()
        if not path or start < 1 or not message:
            continue
        raws.append(_raw("external", message, path, start, end, "major"))
        if len(raws) >= _MAX_FINDINGS:
            break
    if not saw:
        return [], "unparsed"
    if not raws:
        return [], "quiet"
    return raws, "findings"


def _in_scope(path: str, scope: set[str]) -> str:
    text = path.replace("\\", "/").split("?", 1)[0]
    if text.startswith("file://"):
        text = text[7:]
    text = text.lstrip("/")
    while text.startswith("./"):
        text = text[2:]
    if text in scope:
        return text
    for item in scope:
        if text.endswith("/" + item):
            return item
    return ""


def _lines(start, end) -> tuple[int, int]:
    try:
        line = int(start or 0)
    except (TypeError, ValueError):
        return 0, 0
    try:
        stop = int(end or 0)
    except (TypeError, ValueError):
        stop = line
    if stop < line:
        stop = line
    return line, stop or line


def _raw(rule_id: str, message: str, path: str, line: int, end: int, severity: str) -> dict:
    return {
        "rule_id": rule_id[:80] or "external",
        "message": message[:500],
        "file_path": path,
        "line_start": line,
        "line_end": end,
        "severity": severity,
    }


def _record(ident: str, status: str, note: str, findings: int) -> dict:
    return {
        "id": ident,
        "status": status,
        "note": note,
        "findings": findings,
        "sandbox": False,
    }
