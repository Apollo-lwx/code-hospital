"""记忆端口：医院只认六个方法，不认 rsi 的传输方式。

知识本体归 rsi，医院只记**引用与反馈**。两边各存一份必然分叉，
而且医院这份会过期——所以这里一个字的正文都不落库。

三态通道（写进 `coverage.memory.channel`）：

| 通道 | 能力 | 报告怎么写 |
|---|---|---|
| `mcp` | 六方法齐全，反馈闭环可用 | 引用 N 条、反馈 M 条 |
| `cli` | 只有 recall / propose，**反馈断流** | 引用 N 条、**本轮未反馈** |
| `absent` | 没有记忆基座 | 本轮无记忆 |

注入分区是**纯规则**，不调模型：

| 条目的 rsi 侧标记 | 医院怎么用 |
|---|---|
| 禁止项、无告警 | 硬约束区（「必须遵守」） |
| 其余条目、无告警 | 参考区（「本项目惯例」） |
| `low_confidence` 或 `health.kind=stale_source` | 线索区（「存疑，先核前提」） |
| `health.kind=incorrect` 且未结案 | **不注入**，计入 `withheld` |

判断在 rsi 侧已经做完了（`memory_health` 的归因、反馈累积的置信度），
医院这里只是照着分区，不重新判断，也不「聪明加工」——加工会引入不可复现的判断。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..safety.sanitize import sanitize_text

CHANNELS = ("mcp", "cli", "absent")

# 反馈动作。与 rsi_feedback 的枚举一致，医院不新增语义。
ACTIONS = ("accepted", "applied", "modified", "copied", "referenced", "ignored", "rejected")

# 注入分区。tier 决定这句话放报告的哪一区、用什么措辞。
TIERS = ("hard", "reference", "clue", "withheld")

_OPEN_HEALTH = ("open", "awaiting_edit")


class MemoryPort:
    """六个方法，返回结构统一。子类只换传输，不换语义。"""

    channel = "absent"
    owner = "hospital"

    def __init__(self, root: str) -> None:
        self.root = str(root or ".")

    async def recall(self, task: str, *, role: str = "", top_k: int | None = None) -> dict:
        return _blank_recall(self.channel, "没有记忆基座，本轮不召回。")

    async def feedback(self, token: str, action: str, *, rating: int | None = None,
                       comment: str = "") -> dict:
        return _blank_feedback(self.channel, "没有记忆基座，不回报反馈。")

    async def propose(self, title: str, content: str, *, content_type: str = "documentation",
                      source_url: str = "", trigger: str = "", expiry: str = "") -> dict:
        return {"ok": False, "channel": self.channel, "id": "", "note": "没有记忆基座，不写入。"}

    async def review(self, target: dict) -> dict:
        return _unsupported(self.channel, "review")

    async def conflicts(self, target: dict) -> dict:
        return _unsupported(self.channel, "conflicts")

    async def health(self, target: dict) -> dict:
        return _unsupported(self.channel, "health")


class NullPort(MemoryPort):
    """没有 rsi，或用户关掉了记忆。医院照常看病，只是每轮都记「无记忆」。"""


class RsiCliPort(MemoryPort):
    """包住现有的 `rsi` 命令行实现。零行为变化。

    CLI 只有 recall / knowledge add，**没有 feedback**——这一条必须
    在报告里显式写成「本轮未反馈」，不能让断流的闭环看起来是通的。
    """

    channel = "cli"

    async def recall(self, task: str, *, role: str = "", top_k: int | None = None) -> dict:
        from . import rsi

        raw = await rsi.run_recall(self.root, task, role=role, top_k=top_k)
        if not isinstance(raw, dict):
            return _blank_recall(self.channel, str(raw or "rsi 无输出")[:200])
        return normalize_recall(raw, self.channel, task=task, top_k=top_k)

    async def feedback(self, token: str, action: str, *, rating: int | None = None,
                       comment: str = "") -> dict:
        return _blank_feedback(
            self.channel,
            "cli 通道没有 feedback 接口，本轮未反馈。接 MCP 才能闭环。",
        )

    async def propose(self, title: str, content: str, *, content_type: str = "documentation",
                      source_url: str = "", trigger: str = "", expiry: str = "") -> dict:
        from . import rsi

        note = await rsi.distill_lesson(self.root, title, content)
        return {"ok": note.startswith("已写入"), "channel": self.channel, "id": "", "note": note[:300]}


class RsiMcpPort(MemoryPort):
    """六方法齐全。feedback 是它存在的主要理由。"""

    channel = "mcp"

    async def recall(self, task: str, *, role: str = "", top_k: int | None = None) -> dict:
        from . import rsi_mcp

        args: dict = {"task": task[:8192]}
        if role:
            args["role"] = role
        if top_k:
            args["top_k"] = int(top_k)
        try:
            raw = await rsi_mcp.call(self.root, "rsi_recall", args)
        except rsi_mcp.MemoryUnavailable as exc:
            return _blank_recall(self.channel, f"MCP 召回失败：{exc}")
        return normalize_recall(raw, self.channel, task=task, top_k=top_k)

    async def feedback(self, token: str, action: str, *, rating: int | None = None,
                       comment: str = "") -> dict:
        from . import rsi_mcp

        if not token:
            return _blank_feedback(self.channel, "本轮召回没有返回 feedback_token，无法反馈。")
        if action not in ACTIONS:
            return _blank_feedback(self.channel, f"未知反馈动作：{action}")
        args: dict = {"feedback_token": token, "action": action}
        if rating:
            args["rating"] = int(rating)
        if comment:
            args["comment"] = sanitize_text(comment, limit=500)
        try:
            raw = await rsi_mcp.call(self.root, "rsi_feedback", args)
        except rsi_mcp.MemoryUnavailable as exc:
            return _blank_feedback(self.channel, f"反馈未送达：{exc}")
        return {
            "ok": str(raw.get("status") or "success") != "error",
            "channel": self.channel,
            "action": action,
            "note": "",
        }

    async def propose(self, title: str, content: str, *, content_type: str = "documentation",
                      source_url: str = "", trigger: str = "", expiry: str = "") -> dict:
        from . import rsi_mcp

        args: dict = {
            "title": sanitize_text(title, limit=200),
            "content": sanitize_text(content, limit=1400),
            "content_type": content_type,
        }
        if source_url:
            args["source_url"] = source_url[:300]
        if trigger:
            args["trigger_condition"] = trigger[:300]
        if expiry:
            args["expiry_condition"] = expiry[:300]
        try:
            raw = await rsi_mcp.call(self.root, "rsi_knowledge_add", args)
        except rsi_mcp.MemoryUnavailable as exc:
            return {"ok": False, "channel": self.channel, "id": "", "note": f"写入未送达：{exc}"}
        return {
            "ok": str(raw.get("status") or "") != "error",
            "channel": self.channel,
            "id": str(raw.get("id") or raw.get("memory_id") or ""),
            "status": str(raw.get("state") or raw.get("status") or ""),
            "note": str(raw.get("message") or "")[:300],
        }

    async def review(self, target: dict) -> dict:
        return await self._passthrough("rsi_knowledge_review", target)

    async def conflicts(self, target: dict) -> dict:
        return await self._passthrough("rsi_conflicts", target)

    async def health(self, target: dict) -> dict:
        return await self._passthrough("rsi_memory_health", target)

    async def _passthrough(self, tool: str, target: dict) -> dict:
        """三个审批类方法医院不解释参数——卡片由 rsi 出，医院只负责端到用户面前。"""
        from . import rsi_mcp

        try:
            raw = await rsi_mcp.call(self.root, tool, dict(target or {}))
        except rsi_mcp.MemoryUnavailable as exc:
            return _unsupported(self.channel, tool, str(exc))
        return {"ok": True, "channel": self.channel, "tool": tool, "data": raw}


# ---------- 归一化与分区 ----------


def normalize_recall(raw: dict, channel: str, *, task: str = "",
                     top_k: int | None = None) -> dict:
    """rsi 的返回 → 医院内部结构。只换形状，不加判断。"""
    hits: list[dict] = []
    for row in raw.get("prohibitions") or []:
        hit = _hit(row, "prohibition")
        if hit:
            hits.append(hit)
    for row in raw.get("items") or []:
        hit = _hit(row, "item")
        if hit:
            hits.append(hit)
    for row in raw.get("skills") or []:
        hit = _hit(row, "skill")
        if hit:
            hits.append(hit)

    limit = int(top_k or 8)
    kept: list[dict] = []
    withheld = 0
    for hit in hits:
        if hit["tier"] == "withheld":
            withheld += 1
            continue
        if len(kept) >= limit:
            withheld += 1
            continue
        kept.append(hit)

    return {
        "channel": channel,
        "task": str(task or "")[:300],
        "hits": kept,
        "recalled": [hit["id"] for hit in kept if hit["id"]],
        "injected": sum(1 for hit in kept if hit["tier"] in ("hard", "reference")),
        "withheld": withheld,
        "token": str(raw.get("feedback_token") or ""),
        "decisions": _sanitize(raw.get("decisions") or [], 3),
        "skills": [hit["title"] for hit in kept if hit["kind"] == "skill"][:10],
        "note": "",
    }


def _hit(row, kind: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    title = sanitize_text(str(row.get("title") or ""), limit=200)
    content = sanitize_text(str(row.get("content") or ""), limit=500)
    if not title and not content:
        return None
    health = row.get("health") if isinstance(row.get("health"), dict) else {}
    hit = {
        "id": str(row.get("id") or ""),
        "kind": kind,
        "title": title,
        "content": content,
        "content_type": str(row.get("content_type") or ""),
        "source_path": str(row.get("source_path") or "")[:300],
        "low_confidence": bool(row.get("low_confidence")),
        "health": {
            "kind": str(health.get("kind") or ""),
            "status": str(health.get("status") or ""),
        } if health else {},
        "condition": row.get("condition") if isinstance(row.get("condition"), dict) else {},
    }
    hit["tier"] = _tier(hit)
    return hit


def _tier(hit: dict) -> str:
    """照着 rsi 的标记分区。这里不重新判断对错，只决定「放哪个区」。"""
    health = hit.get("health") or {}
    if health.get("kind") == "incorrect" and health.get("status") in _OPEN_HEALTH:
        return "withheld"
    if hit.get("low_confidence") or health.get("kind") == "stale_source":
        return "clue"
    return "hard" if hit.get("kind") == "prohibition" else "reference"


def _sanitize(rows, limit: int) -> list[dict]:
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        out.append({
            "id": str(row.get("id") or ""),
            "kind": str(row.get("kind") or ""),
            "title": sanitize_text(str(row.get("title") or ""), limit=160),
            "prompt": sanitize_text(str(row.get("prompt") or row.get("question") or ""), limit=400),
        })
        if len(out) >= limit:
            break
    return out


def _blank_recall(channel: str, note: str) -> dict:
    return {
        "channel": channel, "task": "", "hits": [], "recalled": [], "injected": 0,
        "withheld": 0, "token": "", "decisions": [], "skills": [], "note": note,
    }


def _blank_feedback(channel: str, note: str) -> dict:
    return {"ok": False, "channel": channel, "action": "", "note": note}


def _unsupported(channel: str, what: str, note: str = "") -> dict:
    return {
        "ok": False,
        "channel": channel,
        "tool": what,
        "note": note or f"{channel} 通道没有 {what}，本轮跳过。",
    }


# ---------- 选通道 ----------

_PROBE: dict[str, str] = {}


def reset_probe() -> None:
    _PROBE.clear()


def mode() -> str:
    """`HOSPITAL_MEMORY=auto|mcp|cli|off`。默认 auto。"""
    return os.environ.get("HOSPITAL_MEMORY", "auto").strip().lower() or "auto"


def rsi_ready(root: str) -> bool:
    """本机有 rsi 命令，且这个仓库建过记忆库。临时目录不建库。"""
    if shutil.which("rsi") is None:
        return False
    return (Path(root) / ".rsi" / "identity.json").is_file()


async def open_port(root: str, *, prefer: str = "") -> MemoryPort:
    """选一个通道。优先 MCP（反馈闭环唯一来源），失败退 CLI，都没有就 absent。"""
    root = str(root or ".")
    wanted = (prefer or mode()).lower()
    if wanted in ("off", "none", "absent"):
        return NullPort(root)
    if wanted == "cli":
        return RsiCliPort(root) if rsi_ready(root) else NullPort(root)
    if not rsi_ready(root):
        return NullPort(root)

    from . import rsi_mcp

    known = _PROBE.get(root)
    if known == "cli":
        return RsiCliPort(root)
    if known == "mcp":
        # 会话可能已死，但 call() 会自己重建；这里不重复握手。
        return RsiMcpPort(root)

    port = RsiMcpPort(root)
    probe = await port.recall("医院连通性自检", top_k=1)
    if probe.get("note", "").startswith("MCP 召回失败"):
        _PROBE[root] = "cli"
        return RsiCliPort(root) if rsi_ready(root) else NullPort(root)
    _PROBE[root] = "mcp"
    return port
