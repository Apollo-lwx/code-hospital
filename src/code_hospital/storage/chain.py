"""会诊链聚合视图（design §4.8 二期·轻编排）：沿 consultation 链跨院走查。

只读查询，不写任何库。边界：链上只带病例卡片元数据（id/title/status 等），
病历内容（findings/patches/reports）不过链——跨 case 调档限定本 project_root 内
（design §4.8 权限约束），要看病历到对应仓库自己 hosp_case。主治医师角色
（先治哪个、按什么顺序复诊）留在宿主 Agent 侧，本模块只供链路查询。

已知不变量：转诊建档时两端各写各的库，每库的 case_links 行里 case_id 端
永远是本院病例、related_case_id 端是对端病例，note（"转诊自/至 <root>"）
带对端仓库根。解析不出对端根的行：链还在，走查止步并留不可达占位。
"""

from __future__ import annotations

import os

from .db import Database

# 走查防线：异常数据（环、错写的根、超长链）下查询也不失控。
MAX_CHAIN_NODES = 50
MAX_CHAIN_EDGES = 200

_PEER_PREFIXES = ("转诊自 ", "转诊至 ")


def _peer_root(note: str) -> str:
    """从链 note 里解析对端仓库根；两种书写格式之外的一律当走不过去。"""
    for prefix in _PEER_PREFIXES:
        if note.startswith(prefix):
            return note[len(prefix):].strip()
    return ""


def _norm(root: str) -> str:
    return os.path.normcase(os.path.normpath(root))


async def consultation_chain(seed_case_id: str, seed_root: str) -> dict:
    """从种子病例出发沿 consultation 链 BFS，跨院只读。

    返回 {seed, relation, nodes, edges, truncated, note}：
    - nodes 按 depth 升序。可达端带病例卡片元数据；不可达端（病案室没了、
      病例行缺失、对端根没记上）照样列出，reachable=false 写明原因，
      能绕过的断点（链行还在）越过它继续走。
    - edges 是实际走过的链。同一次转诊两端各写一行，按无序对去重只留一条。
    - truncated=true 表示撞上走查上限，链可能没走完。
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_nodes: set[tuple[str, str]] = set()
    seen_edges: set[frozenset] = set()
    queue: list[tuple[str, str, int]] = [(seed_root, seed_case_id, 0)]
    truncated = False

    while queue:
        if len(nodes) >= MAX_CHAIN_NODES or len(edges) >= MAX_CHAIN_EDGES:
            truncated = True
            break
        root, case_id, depth = queue.pop(0)
        key = (_norm(root), case_id)
        if key in seen_nodes:
            continue
        seen_nodes.add(key)

        node: dict = {
            "case_id": case_id,
            "project_root": root,
            "depth": depth,
            "is_seed": case_id == seed_case_id and _norm(root) == _norm(seed_root),
        }
        links: list[dict] = []
        # 先查文件再开库：Database.connect 会建 .hospital/ 目录与空库，
        # 只读走查绝不能在对端仓库留下任何写副作用。
        if not Database(root).db_path.is_file():
            node.update(reachable=False, reason="病案室不存在（仓库可能已移动）")
        else:
            async with Database(root) as db:
                case = await db.get_case(case_id)
                if case is None:
                    # 病例行没了但链行可能还在，越过断点继续走。
                    node.update(reachable=False, reason="病例不存在")
                    links = await db.list_case_links(case_id)
                else:
                    node.update(
                        reachable=True,
                        title=case.title,
                        status=case.status,
                        triage_level=case.triage_level,
                        visit_kind=case.visit_kind,
                        created_at=case.created_at,
                        updated_at=case.updated_at,
                    )
                    links = await db.list_case_links(case_id)
        nodes.append(node)

        for link in links:
            if link.get("relation") != "consultation":
                continue
            if link.get("case_id") != case_id:
                continue  # 不变量：case_id 端是本院病例；反向行不参与展开
            peer_id = link["related_case_id"]
            peer_root = _peer_root(link.get("note") or "")
            edge_key = frozenset((case_id, peer_id))
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append({
                "case_id": case_id,
                "related_case_id": peer_id,
                "relation": "consultation",
                "from_root": root,
                "to_root": peer_root,
                "note": link.get("note") or "",
                "created_at": link.get("created_at"),
            })
            if not peer_root:
                orphan_key = ("", peer_id)
                if orphan_key not in seen_nodes:
                    seen_nodes.add(orphan_key)
                    nodes.append({
                        "case_id": peer_id,
                        "project_root": "",
                        "depth": depth + 1,
                        "is_seed": False,
                        "reachable": False,
                        "reason": "对端仓库根未记录在链上",
                    })
            elif (_norm(peer_root), peer_id) not in seen_nodes:
                queue.append((peer_root, peer_id, depth + 1))

    nodes.sort(key=lambda n: (n["depth"], n.get("created_at") or 0))
    return {
        "seed": seed_case_id,
        "relation": "consultation",
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated,
        "note": (
            "链上只有病例卡片元数据；病历内容（findings/patches/reports）不过链，"
            "调档到对应仓库自己 hosp_case。先治哪个、按什么顺序复诊由宿主决定。"
        ),
    }
