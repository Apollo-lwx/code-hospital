"""常驻 rsi serve 的 MCP 会话。

医院自己拉起一份 rsi（已拍板，接受与宿主各持一份会话），因为 `feedback` /
`review` / `conflicts` / `memory_health` 只在 MCP 有，CLI 没有闭环四件套。
没有这层，召回策略永远学不到「这次召回帮没帮上」。

**会话由一个专用 task 持有**，不是由调用方持有。原因是 anyio 的 cancel scope
必须在进入它的同一个 task 里退出：把 `AsyncExitStack` 挂在调用方身上，
下一次工具调用换了 task 就会炸「Attempted to exit cancel scope in a different
task」——进程虽然能退，但 stderr 一片红，而且子进程可能留在后台。
用一个常驻 task 收发任务，enter/exit 就永远在同一个 task 里。

会话按 project_root 复用：一个病例会召回两三次，每次重连要两秒多，
且 rsi 侧会重复建索引。

**记忆是外援，不是就诊必经环节。** 握手失败、超时、子进程挂了，
一律抛 MemoryUnavailable 由端口层降级，绝不中断就诊。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from contextlib import AsyncExitStack
from pathlib import Path

_HANDSHAKE_S = 20.0
_CALL_S = 30.0
_CLOSE_S = 10.0


class MemoryUnavailable(RuntimeError):
    """记忆基座连不上或调用失败。调用方据此降级，不据此报错给用户。"""


def _devnull():
    """子进程 stderr 不接到宿主的日志流上——rsi 的日志不该混进医院报告。"""
    return open(os.devnull, "w", encoding="utf-8", errors="replace")


class _Worker:
    """一个常驻任务，独占一份 MCP 会话。所有调用走队列，避免跨 task 关闭。"""

    __slots__ = ("root", "loop", "queue", "task", "ready")

    def __init__(self, root: str) -> None:
        self.root = root
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready: asyncio.Future = self.loop.create_future()
        self.task = self.loop.create_task(self._serve(), name=f"rsi-serve:{root}")

    async def _serve(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        try:
            binary = shutil.which("rsi")
            params = StdioServerParameters(
                command=binary or "rsi",
                args=["serve", "--project-root", self.root],
                env=None,
            )
            read, write = await stack.enter_async_context(
                stdio_client(params, errlog=_devnull())
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), _HANDSHAKE_S)
            if not self.ready.done():
                self.ready.set_result(True)
            while True:
                job = await self.queue.get()
                if job is None:
                    break
                tool, arguments, box = job
                if box.done():
                    continue
                try:
                    result = await asyncio.wait_for(
                        session.call_tool(tool, arguments or {}), _CALL_S,
                    )
                except Exception as exc:  # noqa: BLE001 —— 单个调用失败不该拖垮会话
                    box.set_exception(exc)
                else:
                    box.set_result(result)
        except Exception as exc:  # noqa: BLE001
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            # 与 enter 同一个 task，aclose 不会触发跨 task 的 cancel scope 错误。
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001
                pass

    @property
    def alive(self) -> bool:
        return not self.task.done()

    async def call(self, tool: str, arguments: dict):
        if not self.ready.done():
            await asyncio.wait_for(self.ready, _HANDSHAKE_S + 5)
        box: asyncio.Future = self.loop.create_future()
        await self.queue.put((tool, arguments or {}, box))
        # 兜底超时：会话若已经悄悄死掉，队列里的任务没人取，不设上限会一直挂着。
        return await asyncio.wait_for(box, _CALL_S + 5)

    async def close(self) -> None:
        if self.task.done():
            return
        await self.queue.put(None)
        try:
            await asyncio.wait_for(self.task, _CLOSE_S)
        except Exception:  # noqa: BLE001
            self.task.cancel()


_WORKERS: dict[str, _Worker] = {}
_FAILED: dict[str, str] = {}


def available() -> bool:
    """本机有没有 rsi 命令。项目有没有建库另看 identity.json。"""
    return shutil.which("rsi") is not None


def mark_failed(root: str, reason: str) -> None:
    """记住这个仓库连不上，同进程内不再重试握手。"""
    _FAILED[str(root)] = str(reason)[:300]


def failure_of(root: str) -> str:
    return _FAILED.get(str(root), "")


def _worker_for(root: str) -> _Worker:
    root = str(root)
    worker = _WORKERS.get(root)
    if worker is not None and worker.alive and worker.loop is asyncio.get_running_loop():
        return worker
    _WORKERS.pop(root, None)
    worker = _Worker(root)
    _WORKERS[root] = worker
    return worker


async def _ensure_ready(worker: _Worker) -> None:
    try:
        if not worker.ready.done():
            await asyncio.wait_for(asyncio.shield(worker.ready), _HANDSHAKE_S + 5)
        else:
            worker.ready.result()
    except Exception as exc:  # noqa: BLE001 —— 任何失败都只意味着「没记忆」
        raise MemoryUnavailable(f"{type(exc).__name__}: {exc}") from exc


async def call(root: str, tool: str, arguments: dict) -> dict:
    """调一个 rsi MCP 工具，返回 data 段。失败重建一次，再失败抛 MemoryUnavailable。"""
    root = str(root)
    if not available():
        raise MemoryUnavailable("找不到 rsi 命令")
    if not (Path(root) / ".rsi" / "identity.json").is_file():
        raise MemoryUnavailable("这个仓库没有 .rsi/identity.json，不建记忆库")

    last = ""
    for attempt in range(2):
        worker = _worker_for(root)
        try:
            await _ensure_ready(worker)
            result = await worker.call(tool, arguments)
            return _payload(result)
        except MemoryUnavailable as exc:
            last = str(exc)
        except Exception as exc:  # noqa: BLE001 —— 会话可能已死，重建再试一次
            last = f"{type(exc).__name__}: {exc}"
        _WORKERS.pop(root, None)
        await worker.close()
    mark_failed(root, last)
    raise MemoryUnavailable(last or "rsi MCP 调用失败")


def _payload(result) -> dict:
    """把 CallToolResult 摊平成 dict。rsi 的返回是 JSON 文本 + status 包一层。"""
    text = ""
    for block in getattr(result, "content", None) or []:
        piece = getattr(block, "text", "")
        if piece:
            text += piece
    if not text.strip():
        return {}
    try:
        loaded = json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError:
        return {"raw": text[:2000]}
    if not isinstance(loaded, dict):
        return {"raw": str(loaded)[:2000]}
    if str(loaded.get("status") or "") == "error":
        raise MemoryUnavailable(str(loaded.get("message") or "rsi 返回错误")[:300])
    data = loaded.get("data")
    if isinstance(data, dict):
        merged = dict(data)
        merged.setdefault("status", loaded.get("status"))
        return merged
    return loaded


async def aclose_all() -> None:
    """测试与进程收尾用。生产上子进程靠 stdin EOF 自己退。"""
    for root, worker in list(_WORKERS.items()):
        _WORKERS.pop(root, None)
        await worker.close()


def reset_cache() -> None:
    _WORKERS.clear()
    _FAILED.clear()
