"""rsi-boot 桥。医院进程调用本机 `rsi` 命令，不依赖 Cursor 里的 MCP 会话。

开方前召回只在仓库已有 `.rsi/identity.json` 时发生，避免给临时目录建记忆库。
出院蒸馏由调用方显式打开。命令不在、超时或失败时返回说明，不中断就诊。
禁止项和疗效账正文按不可信文本净化后再放进出参。
"""

from __future__ import annotations

import asyncio
import json
import shutil

from ..safety.sanitize import sanitize_text

_TIMEOUT_S = 20


def rsi_on_path() -> bool:
    return shutil.which("rsi") is not None


async def recall_for_prescribe(root: str, task: str) -> dict:
    """开方前召回禁止项和疗效账。未安装或项目未初始化时两者都是空列表。"""
    from pathlib import Path

    empty = {"prohibitions": [], "efficacy": []}
    if not rsi_on_path():
        return empty
    if not (Path(root) / ".rsi" / "identity.json").is_file():
        return empty
    payload = await _run(root, ["recall", task[:500]])
    if not isinstance(payload, dict):
        return empty
    return {
        "prohibitions": _clean_rows(payload.get("prohibitions"), limit=10),
        "efficacy": _clean_rows(
            [row for row in (payload.get("items") or []) if _is_efficacy(row)],
            limit=5,
        ),
    }


async def recall_prohibitions(root: str, task: str) -> list[dict]:
    """开方前召回禁止项。未安装或项目未初始化时返回空列表。"""
    found = await recall_for_prescribe(root, task)
    return found["prohibitions"]


def _is_efficacy(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    title = str(row.get("title") or "")
    if title.startswith("疗效账"):
        return True
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",")]
    return isinstance(tags, list) and "efficacy" in tags


def _clean_rows(rows, limit: int) -> list[dict]:
    cleaned = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = sanitize_text(str(row.get("title") or ""), limit=200)
        content = sanitize_text(str(row.get("content") or ""), limit=500)
        if title or content:
            cleaned.append({"title": title, "content": content})
        if len(cleaned) >= limit:
            break
    return cleaned


async def distill_lesson(root: str, title: str, content: str) -> str:
    """把出院计数写成 rsi documentation。不写发现原文。"""
    if not rsi_on_path():
        return "未找到 rsi 命令，本轮不写入外部记忆。"
    result = await _run(
        root,
        [
            "knowledge", "add",
            "--title", title[:200],
            "--content", content[:1400],
            "--type", "documentation",
        ],
        expect_json=False,
    )
    if result is None:
        return "rsi 未在时限内返回，本轮不写入外部记忆。"
    text = str(result).strip()
    if text.startswith("已添加"):
        return f"已写入 rsi 文档记忆：{text}"
    return f"rsi 未写入记忆：{text or '无输出'}"


async def distill_efficacy(root: str, content: str) -> str:
    """把疗效账写成 experience。没有成败记录时不调用。"""
    if not content.strip():
        return ""
    if not rsi_on_path():
        return "未找到 rsi 命令，疗效账不写入外部记忆。"
    result = await _run(
        root,
        [
            "knowledge", "add",
            "--title", "疗效账",
            "--content", content[:1400],
            "--type", "experience",
            "--tags", "efficacy",
        ],
        expect_json=False,
    )
    if result is None:
        return "rsi 未在时限内返回，疗效账不写入外部记忆。"
    text = str(result).strip()
    if text.startswith("已添加"):
        return f"已写入 rsi 疗效账：{text}"
    return f"rsi 未写入疗效账：{text or '无输出'}"


def _parse_json(text: str):
    try:
        return json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError:
        return {}


async def _run(root: str, args: list[str], expect_json: bool = True):
    binary = shutil.which("rsi")
    if not binary:
        return "未找到 rsi 命令，本轮不写入外部记忆。"
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, *args,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return f"无法启动 rsi：{exc}"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), _TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        return None
    text = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return err or text or f"rsi 退出码 {proc.returncode}"
    if not expect_json:
        return text
    return _parse_json(text)
