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


# ────────────── G6 移动剂型：willRenameFiles 的规格化 ──────────────


def _uri_path_cased(uri: str) -> str:
    """uri_path 的保案例：归一化分隔符但不 normcase。

    移动改的就是文件名大小写（FooBar.py → foo_bar.py），normcase 会把
    hunk 路径与 scope 的大小写对齐信息抹掉（Windows 上全转小写），
    移动链路全程用保案路径，比较时才 normcase。
    """
    if not uri.startswith("file:"):
        return ""
    path = unquote(urlparse(uri).path)
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return os.path.abspath(path)


def _edit_files_cased(edit: dict) -> dict[str, list[dict]]:
    """edit_files 的保案例（键是保案绝对路径）。"""
    out: dict[str, list[dict]] = {}

    def put(uri: str, text_edits) -> None:
        path = _uri_path_cased(str(uri or ""))
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
                continue
            put((entry.get("textDocument") or {}).get("uri"), entry.get("edits"))
    return out


def move_hunks(root: str, edit: dict, old_abs, new_abs) -> tuple[list, str]:
    """willRenameFiles 的 WorkspaceEdit → (行级 Hunk 列表, 被移动文件的键法)。

    规格化两件事（move-formula 草案 §3.2）：
    1. 各语言服务对被移动文件自身的编辑键法不同（旧 URI / 新 URI 都有），
       统一键到**旧路径**——编辑作用在内容上，内容施治前住在旧路径；
       实测键法作为台账字符串返回（"old" / "new" / "none"）。
    2. 文件级操作（documentChanges 里的 RenameFile 等）与新旧双键同现
       都属于意外——抛 EditApplyError，不猜。
    编辑全部落在施治前存在的文件上（被移动文件的编辑键旧路径、引用文件键自身），
    因此产出的 Hunk 可以直接过煎药四道校验，移动本身由药房另行特例校验。
    """
    from ..core.algorithms import Hunk

    document_changes = edit.get("documentChanges")
    if isinstance(document_changes, list):
        for entry in document_changes:
            if isinstance(entry, dict) and "textDocument" not in entry:
                kind = entry.get("kind") or "未知"
                raise EditApplyError(f"移动编辑带文件级操作（{kind}），不猜，拒绝施治")
    files = _edit_files_cased(edit)
    old_cased = os.path.abspath(str(old_abs))
    old_key = os.path.normcase(old_cased)
    new_key = os.path.normcase(os.path.abspath(str(new_abs)))
    by_norm = {os.path.normcase(path): path for path in files}
    if old_key in by_norm and new_key in by_norm:
        raise EditApplyError("移动编辑对被移动文件同时键了新旧两个 URI，不猜，拒绝施治")
    keying = "none"
    if new_key in by_norm:
        files[old_cased] = files.pop(by_norm[new_key])
        keying = "new"
    elif old_key in by_norm:
        files[old_cased] = files.pop(by_norm[old_key])
        keying = "old"
    base = os.path.abspath(root)
    hunks: list = []
    for path, text_edits in sorted(files.items()):
        rel = os.path.relpath(path, base).replace("\\", "/")
        if os.path.normcase(rel).startswith(".."):
            raise EditApplyError(f"移动编辑落到仓库外：{path}")
        target = Path(path)
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        after = _apply_text_edits(before, text_edits, path)
        if after == before:
            continue
        hunks.extend(_file_hunks(rel, before, after))
    return hunks, keying


def _file_hunks(rel: str, before: str, after: str) -> list:
    """同一文件的 before/after → 行级 Hunk 列表（不过文本 diff 那一道绕路）。

    变更块间隔 ≤2 行的合并成一块；纯插入块（search 为空）向上下各借 1 行
    上下文——锚点关要求 search 唯一匹配，空块会被判「处处匹配」拒掉，
    借行后锚点照常工作。合并后相邻块间隔 ≥3，借 1 行不会重叠。
    """
    from ..core.algorithms import Hunk

    old_lines = before.split("\n")
    new_lines = after.split("\n")
    changes = [
        (i1, i2, j1, j2)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old_lines, b=new_lines).get_opcodes()
        if tag != "equal"
    ]
    if not changes:
        return []
    merged = [list(changes[0])]
    for i1, i2, j1, j2 in changes[1:]:
        last = merged[-1]
        if i1 - last[1] <= 2:  # 间隔 ≤2 行：借行会撞，并成一块
            last[1], last[3] = i2, j2
        else:
            merged.append([i1, i2, j1, j2])
    hunks: list = []
    for block in merged:
        i1, i2, j1, j2 = block
        if i1 == i2:  # 纯插入：向上下各借 1 行上下文（夹在文件边界内）
            if i1 > 0:
                i1 -= 1
                j1 -= 1  # 上一行在两侧都前移，插入点保持在该行之后
            if i2 < len(old_lines) and i2 - i1 < 2:
                i2 += 1
                j2 += 1
        hunks.append(Hunk(
            path=rel,
            search=old_lines[i1:i2],
            replace=new_lines[j1:j2],
            line_hint=i1 + 1,
        ))
    return hunks


__all__ = ["EditApplyError", "edit_files", "move_hunks", "to_diff", "uri_path"]
