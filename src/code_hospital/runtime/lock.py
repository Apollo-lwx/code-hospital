"""同仓互斥。默认仍是进程内检查：读活跃病例，冲突或转诊。

不写文件锁，也不跨进程。撞单的唯一索引仍在病例表上，作并发兜底。
"""

from __future__ import annotations

from typing import Protocol

from ..core.errors import conflict


class CaseLock(Protocol):
    async def claim(self, db, project_root: str, *, force: bool) -> None:
        """占用本仓的活跃名额。已有活跃病例且未 force 时抛 Conflict。"""


class InProcessLock:
    """当前挂号用的那一查。同一进程里先看库，再决定拒绝或转诊。"""

    async def claim(self, db, project_root: str, *, force: bool) -> None:
        active = await db.get_active_case(project_root)
        if active is None:
            return
        if not force:
            raise conflict(
                f"本仓库已有活跃病例 {active.id}（{active.title}，状态 {active.status}）",
                target="case_id",
                next_action="先完成该病例并出院，或以 force=true 将其转诊后重新挂号",
            )
        await db.update_case(active.id, status="referred")


case_lock: CaseLock = InProcessLock()
