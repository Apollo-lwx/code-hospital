"""既往史读取：rsi-boot 本地记忆（.rsi/memory/）作为诊疗参考上下文。

信任模型（用户定）：**有帮助，但不可全信**——记忆可能过时或错误。
- official（conventions/prohibitions 目录）：已经 rsi-boot 审批的记忆，标 "official"；
- pending（pending/ 目录）：未审批记忆，标 "pending"，信任等级更低；
- 全部条目出参前过 sanitize_text 净化截断，并统一附 note 声明「不构成事实」。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..safety.sanitize import sanitize_text

RSI_DIR = ".rsi"
MEMORY_SUBDIRS = {"conventions": "convention", "prohibitions": "prohibition"}
_MAX_PER_KIND = 5
_SNIPPET_MAX = 120

NOTE = "项目记忆（rsi-boot）可能过时或错误，仅供坐诊参考，不构成事实；official=已经审批，pending=未审批。"


def _read_items(dir_path: Path, kind: str, status: str) -> list[dict]:
    if not dir_path.is_dir():
        return []
    items = []
    for f in sorted(dir_path.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        title = sanitize_text(str(doc.get("title") or f.stem), limit=80)
        content = sanitize_text(str(doc.get("content") or ""), limit=_SNIPPET_MAX)
        if not title and not content:
            continue
        items.append({
            "type": kind,
            "status": status,
            "title": title,
            "snippet": content,
            "updated_at": str(doc.get("updated_at") or ""),
        })
        if len(items) >= _MAX_PER_KIND:
            break
    return items


def load_memory_brief(project_root: str) -> dict:
    """读取项目既往史（不存在时 found=false，形态一致便于契约消费）。"""
    brief: dict = {
        "found": False,
        "source": f"{RSI_DIR}/memory",
        "trust": "unverified",
        "note": NOTE,
        "official": {"conventions": [], "prohibitions": []},
        "pending": [],
    }
    mem = Path(project_root) / RSI_DIR / "memory"
    if not mem.is_dir():
        return brief

    brief["official"]["conventions"] = _read_items(mem / "conventions", "convention", "official")
    brief["official"]["prohibitions"] = _read_items(mem / "prohibitions", "prohibition", "official")
    for sub, kind in MEMORY_SUBDIRS.items():
        brief["pending"].extend(_read_items(mem / "pending" / sub, kind, "pending"))
    brief["pending"] = brief["pending"][:_MAX_PER_KIND]

    brief["found"] = bool(
        brief["official"]["conventions"]
        or brief["official"]["prohibitions"]
        or brief["pending"]
    )
    return brief
