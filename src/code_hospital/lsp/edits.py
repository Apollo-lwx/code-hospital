"""WorkspaceEdit → unified diff。

LSP rename / willRenameFiles 的产物**不直接落盘**，翻译成 diff 后进煎药的
四道校验（format/anchor/scope/content）——LSP 是更准的取数，煎与验还是医院的
（lsp-scope 草案 §3）。
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from urllib.parse import unquote, urlparse


class EditApplyError(Exception):
    """WorkspaceEdit 结构不合法或区间越界。"""


def uri_path(uri: str) -> str:
    """file:// URI → 规范化绝对路径；不是 file URI 返回空。"""
    if not uri.startswith("file:"):
        return ""
    path = unquote(urlparse(uri).path)
    # Windows 的 file URI 路径是 /D:/... 形态，剥掉前导斜杠。
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return os.path.normcase(os.path.abspath(path))


def edit_files(edit: dict) -> dict[str, list[dict]]:
    """WorkspaceEdit → {绝对路径: [TextEdit]}。changes 与 documentChanges 两种形态都收。"""
    out: dict[str, list[dict]] = {}

    def put(uri: str, text_edits) -> None:
        path = uri_path(str(uri or ""))
        if not path or not isinstance(text_edits, list):
            return
        out.setdefault(path, []).extend(e for e in text_edits if isinstance(e, dict))

    changes = edit.get("changes")
    if isinstance(changes, dict):
        for uri, text_edits in changes.items():
            put(uri, text_edits)
    document_changes = edit.get("documentChanges")
    if isinstance(document_changes, list):
        for entry in document_changes:
            if not isinstance(entry, dict) or "textDocument" not in entry:
                continue  # 文件级操作（RenameFile 等）由调用方另行处理
            put((entry.get("textDocument") or {}).get("uri"), entry.get("edits"))
    return out


def to_diff(root: str, edit: dict) -> tuple[str, list[str]]:
    """(unified diff, 涉及文件的仓库相对路径)。edit 不含文本改动时返回 ("", [])。"""
    base = os.path.normcase(os.path.abspath(root))
    chunks: list[str] = []
    touched: list[str] = []
    for path, text_edits in sorted(edit_files(edit).items()):
        rel = os.path.relpath(path, base).replace("\\", "/")
        if rel.startswith(".."):
            raise EditApplyError(f"改名落到仓库外：{path}")
        before = Path(path).read_text(encoding="utf-8", errors="replace") \
            if Path(path).is_file() else ""
        after = _apply_text_edits(before, text_edits, path)
        if after == before:
            continue
        chunks.append("".join(difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )))
        touched.append(rel)
    return "".join(chunks), touched


def _apply_text_edits(text: str, edits: list[dict], path: str) -> str:
    """按区间从后往前应用 TextEdit（前面的编辑不改后面编辑的坐标）。"""
    spans = []
    for edit in edits:
        rng = edit.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        spans.append((
            int(start.get("line") or 0), int(start.get("character") or 0),
            int(end.get("line") or 0), int(end.get("character") or 0),
            str(edit.get("newText") or ""),
        ))
    # 区间重叠是服务器违约，拒绝比错位改名好。
    ordered = sorted(spans, key=lambda s: (s[0], s[1]))
    for left, right in zip(ordered, ordered[1:]):
        if (right[0], right[1]) < (left[2], left[3]):
            raise EditApplyError(f"{path} 的 TextEdit 区间重叠")
    lines = text.split("\n")
    for sl, sc, el, ec, new_text in reversed(ordered):
        if sl >= len(lines) or el >= len(lines) or sc > len(lines[sl]) or ec > len(lines[el]):
            raise EditApplyError(f"{path} 的 TextEdit 区间越界（L{sl}:{sc}–L{el}:{ec}）")
        head = lines[sl][:sc] + new_text + lines[el][ec:]
        lines[sl:el + 1] = head.split("\n")
    return "\n".join(lines)


__all__ = ["EditApplyError", "edit_files", "to_diff", "uri_path"]
