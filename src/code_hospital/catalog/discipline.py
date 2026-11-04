"""院规执行器：判断归记忆层，这里只负责执行已批的那几条。

设计文档 §1.1 第一句就是边界：

> rsi-boot 是「记忆层」，代码医院是「诊疗执行层」。

「要不要加严、加哪一条、上次批的管用没有」是**判断**，归记忆层。
「这个开关现在开着没有」是**状态**，在每一次门禁放行、每一次准入的路径上，
必须在本地一毫秒能查到，不能挂在外援的在线率上。

所以这个模块只做三件事：

1. `CHECKS`——医院**能执行**哪些校验（能力清单，带 effect / cost）。
   清单是封闭的：批不了一条不存在的规矩。
2. `rules.json`——哪些已经开着。**只存开关与引用**，不存证据正文。
   正文在记忆层，这里存 `ref` 指过去。两边各存一份必然分叉，
   这跟记忆端口「知识本体归 rsi，医院只记引用账」是同一条规矩。
3. `requires` / `rule_lines`——查询与呈现。

它**不做**的事，都是这里曾经的错：

- **不算门槛。** 几次才该动，是记忆层的判断。这里一个阈值常量都不留。
- **不生成候选。** 候选从 `rsi_recall` 来，或由宿主带着历史提出。
  医院摆出「偏差几次」和「能执行哪几条」，判断不在这里发生。
- **不记拒绝。** 拒了就是没批。医院不建一份「被拒过几次」的账——
  那个策略（证据翻倍再提）属于记忆，不属于执行。

一条铁律：**批准必须能说出「凭什么」。** `approve` 要 `ref`，
指向记忆层的一条经验。拿不出 `ref` 就不批——`apply_floor` 加的是
配置里关不掉的校验，关不掉的东西必须答得上「这条哪来的」。
没有记忆基座时医院只看看病，不给自己加不可撤销的约束。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from ..storage.db import hospital_root

RULES_FILE = "rules.json"


@dataclass(frozen=True)
class Check:
    """一条**已经能执行**的校验。`id` 就是开关名，不是给建议起的编号。"""

    id: str
    deviates: tuple[str, ...]
    title: str
    effect: str
    cost: str


CHECKS: tuple[Check, ...] = (
    Check(
        id="gate_receipt",
        deviates=("gate_bypass",),
        title="阶段放行必须带回执",
        effect=(
            "放行不再只认 stage_confirmed 这个布尔：没有协议回执时，"
            "还要带 ack=<上一阶段产物摘要>，核不上就拒绝放行"
        ),
        cost=(
            "破坏向后兼容。宿主必须把 handoff.artifact_digest 原样回传；"
            "不回传的老宿主会直接失败，要先去 hosp_case 取回 gate.artifact_digest 才能继续"
        ),
    ),
    Check(
        id="location_floor",
        deviates=("unfounded_claim", "unsupported_clear"),
        title="模型结论必须落到文件和行号",
        effect=(
            "准入强制打开 require_location 与 block_clearance："
            "落不到位置的模型结论丢弃，写「检查通过」的一律改成未核实"
        ),
        cost=(
            "没有兼容问题。只收紧模型结论的准入，仪器结论与其余口径不受影响；"
            "代价是宿主交不出位置时会少几条结论"
        ),
    ),
)

_BY_ID = {check.id: check for check in CHECKS}


# ---------------------------------------------------------------- 规则库


def _path(project_root: str) -> Path:
    return hospital_root(project_root) / RULES_FILE


def _blank() -> dict:
    return {"approved": {}, "history": []}


def state(project_root: str) -> dict:
    """已批的校验。读不出来就当一条都没批。

    宁可这一次不加严，也不要装成加过了——规则库坏掉却让报告写着「已固化」，
    比没有这条规则更糟。
    """
    try:
        doc = json.loads(_path(project_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _blank()
    if not isinstance(doc, dict):
        return _blank()
    out = _blank()
    approved = doc.get("approved")
    if isinstance(approved, dict):
        out["approved"] = {
            str(k): v for k, v in approved.items()
            if k in _BY_ID and isinstance(v, dict) and v.get("on")
        }
    if isinstance(doc.get("history"), list):
        out["history"] = doc["history"]
    return out


_CACHE: dict[str, tuple[float, dict]] = {}


def _cached(project_root: str) -> dict:
    """门禁每放行一次都要问一遍，别每次都读盘。按 mtime 失效。"""
    path = _path(project_root)
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return _blank()
    hit = _CACHE.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    fresh = state(project_root)
    _CACHE[str(path)] = (stamp, fresh)
    return fresh


def requires(project_root: str, check_id: str) -> bool:
    """这条校验批过没有。空 root 一律当没批——不能因为不知道在哪就加严。"""
    if not project_root:
        return False
    return check_id in (_cached(project_root).get("approved") or {})


def _write(project_root: str, rules: dict) -> None:
    path = _path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    _CACHE.pop(str(path), None)


# ---------------------------------------------------------------- 摆事实


def _push(bucket: list, value: str, cap: int) -> None:
    if value and value not in bucket:
        bucket.append(value)
    del bucket[:-cap]


def survey(cases) -> list[dict]:
    """把各病例的 `coverage.discipline[]` 按类型数一遍。

    这是**计数**，不是判断：说出「gate_bypass 出现了 7 次、涉及 4 个病例」，
    不说「7 次够了，该加严了」。后者要有历史才知道，归记忆层。

    没有单独的偏差表：账本来就落在病例里，再抄一份必然分叉，
    而且抄的时候一定会漏掉某个记偏差的地方。
    """
    agg: dict[str, dict] = {}
    for case in cases or []:
        book = getattr(case, "coverage", None) or {}
        for row in book.get("discipline") or []:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "")
            if not kind:
                continue
            slot = agg.get(kind)
            if slot is None:
                slot = agg[kind] = {
                    "kind": kind, "count": 0, "cases": [], "stages": [],
                    "detail": "", "last_at": 0,
                }
            slot["count"] += int(row.get("count") or 1)
            _push(slot["cases"], str(getattr(case, "id", "") or ""), 8)
            _push(slot["stages"], str(row.get("stage") or ""), 4)
            if row.get("detail"):
                slot["detail"] = str(row["detail"])[:200]
            slot["last_at"] = max(
                slot["last_at"], int(getattr(case, "updated_at", 0) or 0)
            )
    return sorted(agg.values(), key=lambda item: int(item["count"]), reverse=True)


def survey_line(rows: list[dict]) -> str:
    """给记忆层的召回任务描述用。

    不带文件路径与源码，只带「哪类偏差、几次」——记忆层要认出
    「这个客户以前也这么绕过」，不需要看代码。

    一条都没有时也照问：用户可能只是想看看这个仓库以前定过哪些规矩，
    以及那些规矩后来管不管用。空着不问，等于把既往史关掉。
    """
    if not rows:
        return "医院纪律：本轮没有记到偏差。问：这个仓库以前定过哪些校验、后来管不管用。"
    head = "；".join(f"{row['kind']} {row['count']} 次" for row in rows[:5])
    return f"医院纪律偏差：{head}。问：这类偏差以往有没有定过校验、管不管用。"


def check_catalog(project_root: str = "") -> list[dict]:
    """医院能执行哪几条，哪些已经开着。这是能力，不是建议。"""
    approved = (_cached(project_root).get("approved") or {}) if project_root else {}
    return [
        {
            "id": check.id,
            "title": check.title,
            "deviates": list(check.deviates),
            "effect": check.effect,
            "cost": check.cost,
            "approved": check.id in approved,
            "ref": str((approved.get(check.id) or {}).get("ref") or ""),
        }
        for check in CHECKS
    ]


async def collect(project_root: str, limit: int = 200) -> dict:
    """摆出事实：偏差几次、能执行哪几条。判断留给记忆层与宿主。"""
    from ..integrations.memory_port import open_port
    from ..storage.db import Database

    async with Database(project_root) as db:
        cases, total = await db.list_cases(project_root, page=1, size=limit)
    rows = survey(cases)
    rules = state(project_root)

    port = await open_port(project_root)
    task = survey_line(rows)
    recall = await port.recall(task, role="hospital-discipline", top_k=5)

    note = (
        "偏差次数是数出来的，不是判断。要不要加严、加哪条，看下面召回到的经验；"
        "医院这边只负责把已批的开关翻过去，并且真的执行。"
    )
    if port.channel == "absent":
        note = (
            "没有接上记忆基座：这一轮只摆事实，不出判断，也不给候选。"
            "纪律停留在记账——医院没有历史，就没有资格说「该加严了」。"
        )

    return {
        "project_root": project_root,
        "scanned_cases": len(cases),
        "total_cases": total,
        "deviations": rows,
        "checks": check_catalog(project_root),
        "approved": sorted((rules.get("approved") or {})),
        "memory": {
            "channel": port.channel,
            "task": task,
            "hits": [
                {"id": hit.get("id"), "title": hit.get("title"), "tier": hit.get("tier")}
                for hit in (recall.get("hits") or [])
            ],
            "note": str(recall.get("note") or ""),
        },
        "note": note,
        "case_window_note": (
            f"只看了最近 {len(cases)} 个病例（共 {total} 个）。"
            if total > len(cases) else ""
        ),
    }


# ---------------------------------------------------------------- 批准


def digest(check_id: str, ref: str) -> str:
    """约定通道的回执。绑的是「这条校验 + 这条依据」，不是别的。

    换一条经验就是另一件事了，凭据跟着变。
    """
    key = f"{check_id}|{ref}"
    return "ack-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def approve(
    project_root: str,
    check_id: str,
    *,
    ref: str,
    channel: str = "convention",
) -> dict:
    """把一条校验翻到开。`ref` 必须指向记忆层的一条经验。

    调用方负责先核回执（人确实看过）；这里只管两件事：
    这条校验存不存在、凭什么批。后者拿不出来就不批——
    `apply_floor` 加的是关不掉的校验，关不掉的东西必须可溯源。
    """
    check = _BY_ID.get(str(check_id or ""))
    if check is None:
        raise ValueError(f"没有这条可执行的校验：{check_id}")
    ref = str(ref or "").strip()
    if not ref:
        raise ValueError(
            "批准必须带 ref（记忆层那条经验的 id）。"
            "拿不出凭什么，就不该给医院加一条关不掉的约束。"
        )
    rules = state(project_root)
    entry = {
        "on": True,
        "ref": ref[:200],
        "channel": str(channel or "convention")[:40],
        "since": int(time.time() * 1000),
    }
    rules["approved"][check.id] = entry
    rules["history"].append({
        "action": "approve", "check": check.id, "ref": entry["ref"],
        "channel": entry["channel"], "at": entry["since"],
    })
    _write(project_root, rules)
    return {
        "check": check.id,
        "title": check.title,
        "effect": check.effect,
        "cost": check.cost,
        "ref": entry["ref"],
        "channel": entry["channel"],
        "at": entry["since"],
    }


def rule_lines(project_root: str) -> list[str]:
    """报告里那一行：这条校验是因为什么才加的。

    不写清楚来历，用户只会觉得「这医院怎么老要我多传一个参数」。
    写了 ref，它就从摩擦变成了对账——每一条加严都能追回到一条经验。
    """
    if not project_root:
        return []
    approved = _cached(project_root).get("approved") or {}
    out = []
    for check_id, entry in sorted(approved.items()):
        check = _BY_ID.get(check_id)
        title = check.title if check else check_id
        ref = str(entry.get("ref") or "")
        tail = f"（依据记忆条目 {ref}）" if ref else ""
        out.append(
            f"院规：{title}{tail}。"
            "这条校验在服务端强制执行，配置里关不掉。"
        )
    return out


__all__ = [
    "CHECKS",
    "RULES_FILE",
    "approve",
    "check_catalog",
    "collect",
    "digest",
    "requires",
    "rule_lines",
    "state",
    "survey",
    "survey_line",
]
