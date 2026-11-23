"""门诊级 LSP 会话池：按语言懒拉起、复用、诊完统一退。

降级是常态不是异常：语言没注册、命令不在 PATH、拉起失败、请求超时——
全部返回 None 并在 notes 里留一条未到岗原因，调用方回退文本守门，
行为与没有 LSP 的现状逐字节一致（draft §3 降级闭环）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import registry
from .client import Client, LspError
from .registry import LanguageSpec

_NOTE_CAP = 20
# 空集复核的延迟（秒）。索引期抢答空结果的窗口实测数秒（pyright，2026-11-23）。
_EMPTY_CONFIRM_S = 2.0


class Pool:
    """一次门诊的 LSP 会话集合。用 ``with Pool(root) as pool`` 或手动 close。"""

    def __init__(self, project_root: str, specs: dict[str, LanguageSpec] | None = None):
        self._root = str(project_root)
        if specs is None:
            specs, notes = registry.for_project(project_root)
            self._notes: list[str] = list(notes)
        else:
            self._notes = []
        self._by_ext = registry.by_extension(specs)
        self._clients: dict[str, Client] = {}
        self._failed: set[str] = set()
        # 回过一次非空 references 的语言：分析管线确实在跑了，空集复核可以跳过。
        self._live: set[str] = set()
        self._closed = False

    def __enter__(self) -> "Pool":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def notes(self) -> list[str]:
        return list(self._notes)

    def cross_file_refs(self, file_path: str, line: int, symbol: str) -> set[str] | None:
        """符号在其他文件里的引用位置集合（仓库相对路径）。

        返回 None = 本语言 LSP 缺席（未注册/未到岗/超时），调用方回退文本守门；
        返回空集 = LSP 确认没有跨文件引用，守门可以销账。
        """
        if self._closed:
            return None
        ext = Path(file_path).suffix.lower()
        spec = self._by_ext.get(ext)
        if spec is None:
            return None
        symbol = str(symbol or "").strip()
        abs_path = Path(self._root) / file_path
        if not symbol or not abs_path.is_file():
            return None
        client = self._client_for(spec)
        if client is None:
            return None
        try:
            others = self._query_refs(client, spec, abs_path, line, symbol)
        except LspError as exc:
            self._clients.pop(spec.id, None)
            client.close()
            self._failed.add(spec.id)
            self._note(f"LSP 基座：{spec.id} 查询失败（{exc}），本轮降级文本守门")
            return None
        if not others and spec.id not in self._live:
            # 空集复核：索引未完成时服务器会回空（2026-11-23 真实观察，pyright 索引期
            # 抢答空结果），而空集的语义是销账——方向最危险，延迟复核一次再信。
            time.sleep(_EMPTY_CONFIRM_S)
            try:
                others = self._query_refs(client, spec, abs_path, line, symbol)
            except LspError as exc:
                self._clients.pop(spec.id, None)
                client.close()
                self._failed.add(spec.id)
                self._note(f"LSP 基座：{spec.id} 复核失败（{exc}），本轮降级文本守门")
                return None
        if others:
            self._live.add(spec.id)
        return others

    def _query_refs(
        self, client: Client, spec: LanguageSpec, abs_path: Path, line: int, symbol: str,
    ) -> set[str]:
        """一次 references 查询 → 其他文件的仓库相对路径集合（同文件引用不算）。"""
        refs = client.references(abs_path, int(line) - 1, _symbol_column(abs_path, line, symbol))
        if refs:
            self._live.add(spec.id)
        target = _norm(abs_path)
        others: set[str] = set()
        for ref in refs:
            other = _uri_path(str(ref.get("uri") or ""))
            if other and other != target:
                others.add(os.path.relpath(other, self._root).replace("\\", "/"))
        return others

    def close(self) -> None:
        """门诊结束统一退。每个客户端自己 shutdown/exit，对面不听话就杀。"""
        if self._closed:
            return
        self._closed = True
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    # ---------- 内部 ----------

    def _client_for(self, spec: LanguageSpec) -> Client | None:
        if spec.id in self._failed:
            return None
        client = self._clients.get(spec.id)
        if client is not None:
            return client
        try:
            client = Client.start(spec, self._root)
        except LspError as exc:
            self._failed.add(spec.id)
            self._note(f"LSP 基座：{spec.id} 未到岗（{exc}），本轮降级文本守门")
            return None
        self._clients[spec.id] = client
        return client

    def _note(self, text: str) -> None:
        if len(self._notes) < _NOTE_CAP and text not in self._notes:
            self._notes.append(text)


def _symbol_column(path: Path, line: int, symbol: str) -> int:
    """符号在该行的列（0 起）。找不到就 0——references 按行内最近符号解析，宁偏不炸。

    LSP 的 character 是 UTF-16 码元；Python str 下标是码点。BMP 内（含 CJK）两者一致，
    只有 >U+FFFF（emoji 之类）会偏，一期接受这个偏差。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        text = lines[int(line) - 1]
    except (OSError, IndexError):
        return 0
    column = text.find(symbol)
    return column if column >= 0 else 0


def _uri_path(uri: str) -> str:
    """file:// URI → 规范化绝对路径；不是 file URI 返回空。"""
    if not uri.startswith("file:"):
        return ""
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    # Windows 的 file URI 路径是 /D:/... 形态，剥掉前导斜杠。
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return _norm(path)


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


__all__ = ["Pool"]
