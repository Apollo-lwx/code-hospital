"""记忆权重账（设计 4.8）：出院判的「有效」只是快照，复发才是判决。

沿用药柜的 Beta(1,1) 约定，一条记忆一个后验：

    alpha = 1 + 0.5 × 出院确认有效 + 1.0 × 人审确认有效
    beta  = 1 + 1.0 × 复发牵涉     + 1.0 × memory_health 标 incorrect
    权重  = alpha / (alpha + beta)

证据强度**故意不对称**：「看起来修好了」和「确实修好了」不能同权。
出院只给半票——可能只是仪器没跑，或行号变了。

**不加时间衰减函数。** 复发事件本身就是时间的代理，而且可数、可审计。
硬加 `exp(-λt)` 要拍一个 λ，直接破坏确定性优先。一条记忆三年没被引用
不等于它错；三年没被引用**且期间复发 5 次**才是证据。

医院只自动做三件事：记嫌疑、降权、停止注入——都是本地可逆的。
跨项目的降级与废弃必须人批，因为那影响的不只是这一个仓库。
"""

from __future__ import annotations

ALPHA_EFFECTIVE = 0.5   # 出院判有效：弱票，可能只是仪器没跑
ALPHA_CONFIRMED = 1.0   # 人审确认有效：强票
BETA_RECUR = 1.0        # 复发牵涉：强票，客观事实
BETA_INCORRECT = 1.0    # rsi 侧已归因的错误

# 权重分档。判断不是参数，不开放配置——四个客户四套阈值，报告就不可比了。
SUSPECT_BAND = 0.5      # 低于此：注入但标「存疑」
INJECT_FLOOR = 0.3      # 低于此：不注入，转待核实线索
DOWNGRADE_FLOOR = 0.25  # 低于此且复发达阈值：建议降级（人批）
DOWNGRADE_RECUR = 3

ACTIVE = "active"
SUSPECT = "suspect"
DEGRADED = "degraded"


def weight(row: dict | None) -> float:
    """Beta 后验均值。没有记录时是 0.5 的先验——既不信也不疑。"""
    if not row:
        return 0.5
    alpha = float(row.get("alpha", 1.0) or 1.0)
    beta = float(row.get("beta", 1.0) or 1.0)
    if alpha <= 0 or beta <= 0:
        return 0.5
    return alpha / (alpha + beta)


def policy(row: dict | None) -> str:
    """注入决策：normal / suspect / skip。只影响注入顺序与措辞，不进疗效。"""
    if row and str(row.get("status") or ACTIVE) == DEGRADED:
        return "skip"
    score = weight(row)
    if score < INJECT_FLOOR:
        return "skip"
    if score < SUSPECT_BAND:
        return "suspect"
    return "normal"


def apply_policy(hits: list[dict], rows: dict[str, dict]) -> tuple[list[dict], int]:
    """按权重账给召回的条目分档。返回（保留下来的条目，被扣下的条数）。

    `skip` 的不注入——但**不删除记录**，报告里写「N 条因低权重未注入」。
    降级到 pending 要人批，医院只负责停下来。
    """
    kept: list[dict] = []
    withheld = 0
    for hit in hits:
        row = rows.get(hit.get("id") or "")
        decision = policy(row)
        if decision == "skip":
            withheld += 1
            continue
        if decision == "suspect":
            hit = {**hit, "tier": "clue", "weighed": round(weight(row), 2)}
        elif row:
            hit = {**hit, "weighed": round(weight(row), 2)}
        kept.append(hit)
    return kept, withheld


def should_propose_downgrade(row: dict | None) -> bool:
    """跌破 0.25 且复发 ≥ 3 才建议降级。一次复发不算，两次也不算。"""
    if not row:
        return False
    if str(row.get("status") or ACTIVE) == DEGRADED:
        return False
    return weight(row) < DOWNGRADE_FLOOR and int(row.get("recurred") or 0) >= DOWNGRADE_RECUR


def weight_line(row: dict) -> str:
    score = round(weight(row), 2)
    return (
        f"{row.get('memory_id', '')[:12]} 权重 {score}"
        f"（有效 {int(row.get('effective') or 0)} 次 / 复发 {int(row.get('recurred') or 0)} 次）"
    )


def downgrade_lines(rows: list[dict]) -> list[str]:
    """出院报告里的「建议降级」。写建议，不写动作——动作要人批。"""
    out = []
    for row in rows:
        if not should_propose_downgrade(row):
            continue
        out.append(
            f"记忆 {row.get('memory_id', '')[:12]} 权重 {round(weight(row), 2)}、"
            f"复发 {int(row.get('recurred') or 0)} 次，建议降级到 pending 由人复核"
            "（医院不自动降级）。"
        )
    return out


# ---------- 写入端 ----------


async def note_effective(db, project_root: str, ids: list[str], *, confirmed: bool = False) -> list[dict]:
    """出院判有效 → 半票。人审确认过 → 整票。

    只记 alpha 与 effective，不改 status：单次有效不该让一条存疑记忆「转正」。
    """
    gain = ALPHA_CONFIRMED if confirmed else ALPHA_EFFECTIVE
    out = []
    for memory_id in dict.fromkeys(item for item in ids or [] if item):
        row = await db.bump_memory(memory_id, project_root, alpha=gain, effective=1)
        if row:
            out.append(row)
    return out


async def note_recurrence(db, project_root: str, fingerprints: list[str]) -> dict:
    """复发归因。三重门槛同时成立才记嫌疑，否则权重账会变成噪音池。

    1. 记忆**参与了**判定已修好（那轮 `coverage.memory.recalled` 非空）
    2. 同类问题真的回来了（同 fingerprint 复发）
    3. 判断是仪器做的、记忆没参与的，不牵连

    第 1 条不成立时**不记**——这是最容易被写错的一步：
    把复发无脑记到记忆头上，半年后没人再看这张表。
    """
    refs = await db.resolved_case_refs(fingerprints, project_root)
    if not refs:
        return {"suspects": [], "unblamed": 0, "recurred": 0}

    seen: dict[str, str] = {}
    unblamed: set[str] = set()
    for fingerprint, case_id in refs:
        if fingerprint in seen or fingerprint in unblamed:
            continue
        case = await db.get_case(case_id)
        coverage = dict(getattr(case, "coverage", None) or {})
        memory = coverage.get("memory") if isinstance(coverage.get("memory"), dict) else {}
        recalled = [str(item) for item in (memory.get("recalled") or []) if item]
        if not recalled:
            # 当初那轮没有记忆参与 → 复发与记忆无关，不牵连。
            unblamed.add(fingerprint)
            continue
        seen[fingerprint] = case_id

    hit_ids: list[str] = []
    for fingerprint, case_id in seen.items():
        case = await db.get_case(case_id)
        coverage = dict(getattr(case, "coverage", None) or {})
        memory = coverage.get("memory") if isinstance(coverage.get("memory"), dict) else {}
        hit_ids.extend(str(item) for item in (memory.get("recalled") or []) if item)

    updated = []
    for memory_id in dict.fromkeys(hit_ids):
        row = await db.bump_memory(
            memory_id, project_root, beta=BETA_RECUR, recurred=1,
        )
        if row:
            updated.append(row)
    return {
        "suspects": updated,
        "unblamed": len(unblamed),
        "recurred": len(seen),
    }
