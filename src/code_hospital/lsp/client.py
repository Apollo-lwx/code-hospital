"""LSP 会话：stdio 上的 JSON-RPC（Content-Length 分帧），纯标准库。

薄客户端，一期只实现四个方法：initialize / didOpen / references / shutdown。
拉起失败、超时、协议错误一律抛 LspError，由 pool 记「未到岗」并降级——
客户端自己不吞错，吞错了守门就分不清「没引用」和「没查成」。

真实观察（2026-11-23，pyright 1.1.414）：索引完成前的 references 会回**空结果**，
空集在守门语义里是「销账」，方向最危险。两道防线：
1. didOpen 后等该文件的第一条 publishDiagnostics（证明分析管线跑过了）再查；
2. 空结果由 pool 做一次延迟复核（见 pool.cross_file_refs）。
服务器主动请求（window/workDoneProgress/create、workspace/configuration 等）
自动应答 null/空配置，不应答会把对方吊住。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

from .registry import LanguageSpec

# 首个文件的诊断屏障用这个上限（覆盖 jdtls 首次索引），后续文件分析很快。
_FIRST_OPEN_GRACE_S = 180.0
_LATER_OPEN_GRACE_S = 8.0


class LspError(Exception):
    """拉起、超时或协议错误。pool 捕获后记未到岗并降级。"""


class Client:
    """一条语言服务会话。不是线程安全的调用方不管：医院门诊是单线程顺序调用。"""

    def __init__(self, spec: LanguageSpec, proc: subprocess.Popen, root: str):
        self._spec = spec
        self._proc = proc
        self._root = root
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._dead: LspError | None = None
        self._lock = threading.Lock()    # 请求 id 与 pending 表
        self._wlock = threading.Lock()   # stdin 写入（读帧线程要回服务器请求）
        self._opened: set[str] = set()
        self._diag: dict[str, threading.Event] = {}
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @classmethod
    def start(cls, spec: LanguageSpec, root: str) -> "Client":
        resolved = shutil.which(spec.command)
        if not resolved:
            raise LspError(f"{spec.command} 不在 PATH 上")
        try:
            proc = subprocess.Popen(
                [resolved, *spec.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=root,
            )
        except OSError as exc:
            raise LspError(f"{spec.command} 拉起失败：{exc}") from exc
        client = cls(spec, proc, root)
        try:
            root_uri = Path(root).as_uri()
            client._request("initialize", {
                # processId 给出去：医院进程死了，语言服务跟着退，不留孤儿进程。
                "processId": os.getpid(),
                "rootUri": root_uri,
                "workspaceFolders": [{"uri": root_uri, "name": Path(root).name or "repo"}],
                "capabilities": {
                    "window": {"workDoneProgress": True},
                    "textDocument": {"references": {"dynamicRegistration": False}},
                },
            }, timeout=spec.startup_timeout_s)
            client._notify("initialized", {})
        except BaseException:
            client.close()
            raise
        return client

    def did_open(self, path: Path) -> None:
        """references 之前先开门，并等该文件的第一条诊断（分析管线跑过的证据）。

        屏障是软性的：对方始终不发诊断（比如纯 pull 模型的服务器），
        宽限期过了照样放行——空结果复核在 pool 那层兜底。
        """
        uri = path.as_uri()
        if uri in self._opened:
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LspError(f"读不到文件 {path}：{exc}") from exc
        event = threading.Event()
        self._diag[uri] = event
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": self._spec.language_id_for(path.suffix.lower()),
                "version": 1,
                "text": text,
            },
        })
        first = len(self._opened) == 0
        grace = min(self._spec.startup_timeout_s, _FIRST_OPEN_GRACE_S) if first else _LATER_OPEN_GRACE_S
        event.wait(timeout=max(grace, 1.0))
        self._opened.add(uri)

    def references(self, path: Path, line0: int, character: int) -> list[dict]:
        """符号的全部引用（不含声明处）。空列表 = 确实没查到引用。"""
        self.did_open(path)
        result = self._request("textDocument/references", {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": max(line0, 0), "character": max(character, 0)},
            "context": {"includeDeclaration": False},
        }, timeout=self._spec.request_timeout_s)
        if result is None:
            return []
        if not isinstance(result, list):
            raise LspError("references 应答不是列表")
        return [item for item in result if isinstance(item, dict)]

    def rename(self, path: Path, line0: int, character: int, new_name: str) -> dict:
        """符号级全仓改名 → WorkspaceEdit（不落盘，由 edits.py 翻译成 diff 走校验）。

        prepareRename 先探一次：探不动不拦（有的服务器不支持），改名请求自己会被拒。
        """
        self.did_open(path)
        position = {"line": max(line0, 0), "character": max(character, 0)}
        doc = {"uri": path.as_uri()}
        try:
            self._request("textDocument/prepareRename", {
                "textDocument": doc, "position": position,
            }, timeout=self._spec.request_timeout_s)
        except LspError:
            pass
        result = self._request("textDocument/rename", {
            "textDocument": doc, "position": position, "newName": str(new_name),
        }, timeout=self._spec.request_timeout_s)
        if result is None:
            raise LspError(f"{self._spec.id} 改名被拒（该位置可能不是可改名符号）")
        if not isinstance(result, dict):
            raise LspError("rename 应答不是 WorkspaceEdit")
        return result

    def close(self) -> None:
        """shutdown + exit；对面不听话就杀。幂等。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._request("shutdown", None, timeout=5.0)
            self._notify("exit", {})
            self._proc.wait(timeout=5.0)
        except Exception:
            try:
                self._proc.kill()
            except OSError:
                pass
        for event in self._diag.values():
            event.set()
        self._reader.join(timeout=2.0)

    # ---------- 分帧与收发 ----------

    def _request(self, method: str, params, timeout: float):
        if self._dead is not None:
            raise self._dead
        with self._lock:
            self._next_id += 1
            ident = self._next_id
            box: queue.Queue = queue.Queue(maxsize=1)
            self._pending[ident] = box
        try:
            message = {"jsonrpc": "2.0", "id": ident, "method": method}
            if params is not None:
                message["params"] = params
            self._send(message)
            try:
                reply = box.get(timeout=timeout)
            except queue.Empty:
                raise LspError(f"{self._spec.id} 的 {method} 超过 {timeout:.0f}s 没应答")
            if isinstance(reply, LspError):
                raise reply
            error = reply.get("error")
            if error:
                detail = error.get("message") if isinstance(error, dict) else str(error)
                raise LspError(f"{self._spec.id} 的 {method} 被拒：{str(detail)[:120]}")
            return reply.get("result")
        finally:
            self._pending.pop(ident, None)

    def _notify(self, method: str, params) -> None:
        if self._dead is not None:
            return
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict) -> None:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self._wlock:
            try:
                self._proc.stdin.write(frame)
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise LspError(f"{self._spec.id} 的 stdin 写不进去：{exc}") from exc

    def _read_loop(self) -> None:
        """读帧线程：应答按 id 分发；服务器请求自动应答；诊断通知点亮屏障。"""
        error: LspError | None = None
        try:
            while True:
                message = _read_message(self._proc.stdout)
                method = message.get("method")
                if method is not None:
                    self._on_server_message(message)
                    continue
                ident = message.get("id")
                box = self._pending.get(ident)
                if box is not None:
                    box.put(message)
        except (EOFError, OSError, ValueError, json.JSONDecodeError) as exc:
            if not self._closed:
                error = LspError(f"{self._spec.id} 的应答流断了：{exc}")
        if error is not None:
            self._dead = error
            for box in self._pending.values():
                box.put(error)
            for event in self._diag.values():
                event.set()

    def _on_server_message(self, message: dict) -> None:
        method = message["method"]
        if "id" in message:
            # 服务器主动请求：不应答会把对方吊住。配置问询回全 null（用默认），
            # 其余（workDoneProgress/create、registerCapability 之类）一律回 null。
            result = None
            if method == "workspace/configuration":
                items = (message.get("params") or {}).get("items") or []
                result = [None] * len(items)
            try:
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            except LspError:
                pass
            return
        if method == "textDocument/publishDiagnostics":
            uri = str((message.get("params") or {}).get("uri") or "")
            event = self._diag.get(uri)
            if event is not None:
                event.set()


def _read_message(stream) -> dict:
    """读一帧：头部到空行，按 Content-Length 取正文。EOF 抛 EOFError。"""
    length: int | None = None
    while True:
        line = stream.readline()
        if not line:
            raise EOFError("语言服务关了 stdout")
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    if length is None:
        raise ValueError("帧头没有 Content-Length")
    body = stream.read(length)
    if len(body) != length:
        raise EOFError("正文没读满流就断了")
    return json.loads(body.decode("utf-8"))


__all__ = ["Client", "LspError"]
