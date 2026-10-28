"""外聘 MCP 仪器。医院当客户端，用 stdio 拉起用户安装的服务并调用一个工具。

命令名必须在 PATH 上，参数不能带 shell 元字符。环境用 MCP SDK 的默认裁剪，不透传密钥。
工具文本要能解析成发现；没有产物不写成没问题。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil

from .external_run import ABSENT, EMPTY, QUIET, parse_artifact, safe_args

_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,64}$")
_MAX_OUTPUT = 64 * 1024


def tool_name(raw) -> str | None:
    text = str(raw or "").strip()
    if _TOOL.fullmatch(text):
        return text
    return None


async def run_mcp(root: str, instrument: dict, files: list[str], timeout_ms: int) -> dict:
    ident = str(instrument.get("id") or instrument.get("command") or "mcp")
    command = str(instrument.get("command") or "").strip()
    if not command or any(char in command for char in "\\/\n"):
        return _stop(ident, "failed", "命令名不合法，没有启动")
    name = tool_name(instrument.get("tool"))
    if name is None:
        return _stop(ident, "failed", "工具名不合法，没有启动")
    args = safe_args(instrument.get("args"))
    if args is None:
        return _stop(ident, "failed", "参数含禁止字符，没有启动")
    resolved = shutil.which(command)
    if not resolved:
        return _stop(ident, "absent", ABSENT)
    timeout = max(float(timeout_ms or 0) / 1000, 1.0)
    try:
        text = await asyncio.wait_for(_call(resolved, args, root, name, files), timeout)
    except TimeoutError:
        return _stop(ident, "empty", EMPTY)
    except OSError:
        return _stop(ident, "absent", ABSENT)
    except Exception:
        return _stop(ident, "empty", EMPTY)
    scope = {item.replace("\\", "/").lstrip("./") for item in files}
    raws, kind = parse_artifact(text, scope)
    if kind == "quiet":
        return _stop(ident, "quiet", QUIET)
    if kind != "findings" or not raws:
        return _stop(ident, "empty", EMPTY)
    note = f"外聘 MCP 已交产物 {len(raws)} 条"
    return {
        "record": _record(ident, "ran", note, len(raws)),
        "raws": raws,
        "limitations": [],
    }


async def _call(command: str, args: list[str], root: str, tool: str, files: list[str]) -> str:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=command, args=args, cwd=root)
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    tool,
                    {"project_root": root, "files": list(files)},
                )
    if getattr(result, "isError", False):
        return ""
    chunks: list[str] = []
    size = 0
    for block in getattr(result, "content", None) or []:
        piece = getattr(block, "text", None)
        if not isinstance(piece, str) or not piece:
            continue
        chunks.append(piece)
        size += len(piece)
        if size >= _MAX_OUTPUT:
            break
    return "".join(chunks)[:_MAX_OUTPUT]


def _stop(ident: str, status: str, note: str) -> dict:
    return {
        "record": _record(ident, status, note, 0),
        "raws": [],
        "limitations": [f"{ident}：{note}"],
    }


def _record(ident: str, status: str, note: str, findings: int) -> dict:
    return {
        "id": ident,
        "status": status,
        "note": note,
        "findings": findings,
        "sandbox": False,
        "transport": "mcp",
    }
