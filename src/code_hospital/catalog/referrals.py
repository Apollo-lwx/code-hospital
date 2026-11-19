"""会诊转诊单（docs/code-hospital-referrals-draft.md 已定稿）。

四条触发规则都可机械判定：
- R1 仓外依赖违反：import 目标解析到仓库根之外的兄弟仓（分层尺发 layer.external-import）；
- R2 主战场在上游的供应链发现：supply-ruler 点名了具体依赖（升级/替换在上游）；
- R3 宿主交卷点名仓外：合入门诊条目自带 referrals；
- R4 反复复发的仓外根因：同形态复发 ≥3 且记忆层条目含仓外路径。

可及判定刻意保守：target_repo 只接受本地路径；URL 一律纸面。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_EVIDENCE_CAP = 200
_HYPOTHESIS_CAP = 300
# R4：同一内容形态复发达到这个次数，才值得为它问一次记忆层。
R4_MIN_RECURRENCE = 3
# R4 每次诊断最多召回一次，这是召回的 top_k。
_R4_TOP_K = 5

_URLISH = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
# 路径形态 token：../ 开头、绝对路径、或至少两段的分隔路径。不含空格——
# 空格一进字符类，Windows 盘符的「C」会被前一个 token 吞掉，绝对路径就再也匹配不上。
_PATH_TOKEN = re.compile(r"(?:[A-Za-z]:[\\/]|\.\.[\\/]|/)[\w.\\/-]+")


def resolve_target(target_repo: str, project_root: str) -> tuple[str, bool]:
    """可及判定。返回 (resolved_path, local)。URL 或不存在 → ("", False)。"""
    text = str(target_repo or "").strip()
    if not text or _urlish(text):
        return "", False
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = Path(project_root) / text
    try:
        resolved = candidate.resolve()
    except OSError:
        return "", False
    if not resolved.is_dir():
        return "", False
    return str(resolved), True


def _urlish(text: str) -> bool:
    return bool(_URLISH.match(text)) or text.startswith(("git@", "ssh://"))


def _slip(rule: str, target_repo: str, evidence: str, hypothesis: str,
          finding_ids: list[str], project_root: str, suggested_triage: str = "checkup") -> dict:
    resolved, local = resolve_target(target_repo, project_root)
    return {
        "target_repo": str(target_repo)[:200],
        "evidence": str(evidence)[:_EVIDENCE_CAP],
        "hypothesis": str(hypothesis)[:_HYPOTHESIS_CAP],
        "suggested_triage": suggested_triage if suggested_triage in {"emergency", "outpatient", "checkup"} else "checkup",
        "rule": rule,
        "finding_ids": list(finding_ids),
        "resolved_path": resolved,
        "local": local,
    }


# ---------- R1：仓外 import（信号由 catalog/layers.py 发出） ----------

_EXTERNAL_IMPORT_RULE = "layer.external-import"


def internal_tops(scope_files: list[str]) -> set[str]:
    """本仓范围文件覆盖的顶层包名与单文件模块名。"""
    tops: set[str] = set()
    for rel in scope_files:
        posix = str(rel).replace("\\", "/")
        tops.add(posix.split("/")[0])
        if posix.endswith(".py") and "/" not in posix:
            tops.add(posix[:-3])
    return tops


def sibling_repo_for(module: str, project_root: str, internal: set[str]) -> str:
    """import 的顶层包不在本仓、但解析为兄弟仓目录时，返回该目录名；否则空串。

    判定是纯机械的：本仓范围文件里没有这个顶层包，且 <project_root父目录>/<top>
    是存在的目录。标准库与已安装的第三方包没有兄弟目录，自然不会命中。
    internal 来自 internal_tops(scope_files)，由调用方算一次复用。
    """
    top = str(module or "").split(".")[0].strip()
    if not top or top in sys.stdlib_module_names or top in internal:
        return ""
    root = Path(project_root)
    if (root / f"{top}.py").is_file() or (root / top).is_dir():
        return ""
    sibling = root.parent / top
    if sibling.is_dir() and sibling.resolve() != root.resolve():
        return top
    return ""


def r1_slips(members: list, project_root: str, used: set[str]) -> list[dict]:
    """layer.external-import 的成员按兄弟仓目录各开一单。"""
    slips: list[dict] = []
    by_target: dict[str, list] = {}
    for finding in members:
        if finding.rule_id != _EXTERNAL_IMPORT_RULE or finding.id in used:
            continue
        top = str(getattr(finding, "symbol", "") or "").split(".")[0].strip()
        if not top:
            continue
        by_target.setdefault(top, []).append(finding)
    for top, rows in by_target.items():
        head = rows[0]
        used.update(row.id for row in rows)
        slips.append(_slip(
            "R1", f"../{top}",
            f"{head.file_path} import 了仓外模块 {head.symbol or top}",
            f"依赖解析到兄弟仓 {top}，这个问题的主战场可能在 {top}，不在本仓",
            [row.id for row in rows], project_root, suggested_triage="outpatient",
        ))
    return slips


# ---------- R2：主战场在上游的供应链发现 ----------

_DEP_NAME = re.compile(
    r"^依赖 (?P<a>\S+?) 没有|^(?P<b>\S+?) (?:使用明文 HTTP|的版本是通配|的 Git 依赖)"
)


def _dep_name(message: str) -> str:
    matched = _DEP_NAME.search(str(message or ""))
    if not matched:
        return ""
    return (matched.group("a") or matched.group("b") or "").strip()


def r2_slips(members: list, project_root: str, used: set[str]) -> list[dict]:
    """supply-ruler 点名了具体依赖的发现：修复面是升级/替换，在上游。

    依赖名提取不出来的（如 known-vuln 只带漏洞编号）不开单——
    没有 target 的单子等于「我觉得你有病但说不出哪疼」。
    """
    slips: list[dict] = []
    by_dep: dict[str, list] = {}
    for finding in members:
        if not finding.rule_id.startswith("supply.") or finding.id in used:
            continue
        name = _dep_name(finding.message)
        if not name:
            continue
        by_dep.setdefault(name, []).append(finding)
    for dep, rows in by_dep.items():
        head = rows[0]
        used.update(row.id for row in rows)
        slips.append(_slip(
            "R2", dep,
            head.message,
            f"修复主战场在上游依赖 {dep}（升级/替换/钉版本），不是改本仓业务代码",
            [row.id for row in rows], project_root,
        ))
    return slips


# ---------- R3：宿主交卷点名仓外 ----------

def r3_slips(members: list, project_root: str, used: set[str]) -> list[dict]:
    """合入门诊条目自带的 referrals。宿主没点名就不开（R3 不猜）。"""
    slips: list[dict] = []
    for finding in members:
        if finding.id in used:
            continue
        filed = [row for row in (getattr(finding, "referrals", None) or []) if isinstance(row, dict)]
        filed = [row for row in filed if str(row.get("target_repo") or "").strip()]
        if not filed:
            continue
        used.add(finding.id)
        for row in filed:
            slips.append(_slip(
                "R3", row["target_repo"],
                str(row.get("evidence") or finding.message),
                str(row.get("hypothesis") or "宿主交卷点名仓外目标"),
                [finding.id], project_root,
                suggested_triage=str(row.get("suggested_triage") or "checkup"),
            ))
    return slips


# ---------- R4：反复复发的仓外根因（记忆层） ----------

def _outside_paths(text: str, project_root: str) -> list[str]:
    """条目文本里解析到 project_root 之外的路径形态 token。"""
    root = Path(project_root).resolve()
    found: list[str] = []
    for matched in _PATH_TOKEN.finditer(str(text or "")):
        token = matched.group(0).strip().rstrip("，。；、。)）")
        if not token or _urlish(token):
            continue
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = (Path(project_root) / token)
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(root)
            continue  # 仓内路径不算
        except ValueError:
            pass
        if resolved.is_dir():
            found.append(str(resolved))
    return found


async def r4_slips(clusters: list[tuple], project_root: str, used: set[str]) -> list[tuple[object, dict]]:
    """复发 ≥3 的簇才配问记忆层；每次诊断最多召回一次。channel=absent 静默跳过。

    返回 [(diagnosis, slip)]，由调用方挂到诊断行——记忆命中哪个簇，
    单子就挂哪个簇，不撒胡椒面。
    """
    candidates = [
        (diagnosis, members)
        for diagnosis, members in clusters
        if len(getattr(diagnosis, "history_refs", None) or []) >= R4_MIN_RECURRENCE
    ]
    if not candidates:
        return []
    from ..integrations.memory_port import open_port

    try:
        port = await open_port(project_root)
        if getattr(port, "channel", "absent") == "absent":
            return []
        rules = sorted({getattr(m, "rule_id", "") for _, members in candidates for m in members} - {""})
        task = "复发问题的根因是否指向仓库之外。规则：" + "、".join(rules[:4])
        got = await port.recall(task, top_k=_R4_TOP_K)
    except Exception:  # noqa: BLE001 —— 记忆挂了不该带崩诊断
        return []
    targets: list[str] = []
    for hit in got.get("hits") or []:
        text = f"{hit.get('title') or ''} {hit.get('content') or ''}"
        targets.extend(_outside_paths(text, project_root))
    targets = list(dict.fromkeys(targets))
    if not targets:
        return []
    out: list[tuple[object, dict]] = []
    for diagnosis, members in candidates:
        fresh = [m.id for m in members if m.id not in used]
        if not fresh:
            continue
        used.update(fresh)
        target = targets[0]
        out.append((diagnosis, _slip(
            "R4", target,
            f"同形态发现已复发 {len(diagnosis.history_refs)} 次",
            "记忆层有条目把这类复发的根因指向仓外路径，主战场可能不在本仓",
            fresh, project_root,
        )))
    return out


def attach_referrals(clusters: list[tuple], project_root: str, used: set[str] | None = None) -> set[str]:
    """R1–R3 是纯字符串与文件系统判定，当场挂到诊断行。R4 由 r4_slips 单独追加。

    一条发现最多进一条转诊单（used 集合保证）；规则优先级 R1 > R2 > R3。
    返回已占用的 finding id 集合，供 R4 继续避让。
    """
    if used is None:
        used = set()
    for diagnosis, members in clusters:
        slips = [
            *r1_slips(members, project_root, used),
            *r2_slips(members, project_root, used),
            *r3_slips(members, project_root, used),
        ]
        if slips:
            diagnosis.referrals = [*diagnosis.referrals, *slips]
    return used
