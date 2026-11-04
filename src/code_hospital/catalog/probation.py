"""试用期账：一台 probation 仪器攒够什么才够格转正。

三个数，缺一不可：

1. **样本**——跑够 `MIN_RUNS` 轮。少于这个数，任何比率都只是噪音
   （`1/1` 和 `2/3` 都能算出 0.6 以上，但它们说明不了任何事）。
2. **重合**——它点名的位置里，有多少现任也在同一处点了名。
   这里按**位置**比，不按规则号比：跨厂商的 rule_id 从不相同（`F841` vs `ACME001`），
   按规则号比等于永远比不出重合，那这个判据就是白写的。
3. **误报**——它点名的位置里，有多少根本不存在（文件不在范围、行号越界）。
   真伪判不了，位置在不在能判。这是唯一不需要金标的"说错了"。

不问「它说的对不对」：没有金标，问了也答不了，答了也不可复现。
只问「现任同不同意」和「它指的地方在不在」。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

MIN_RUNS = 8            # 样本下限，轮
MIN_CHECKABLE = 5       # 可比对条数下限：少于 5 条，0.6 这个门槛会退化成整数游戏
MIN_CORROBORATION = 0.6  # 与现任同处的比例下限
LINE_TOLERANCE = 2      # 行号容差：不同仪器常把同一处缺陷标在相邻行
EVERY_NTH = 3           # 样本够了之后每几轮跑一次，控成本（默认值，可配）
MAX_PER_SCAN = 3        # 每轮最多跑几台影子仪器，控成本（默认值，可配）
GIVE_UP_ROUNDS = 3      # 连续几轮没跑成才敢说"没到岗"

_CONFIG = "code-hospital.yaml"
_ENV_EVERY_NTH = "CODE_HOSPITAL_SHADOW_EVERY_NTH"
_ENV_MAX_PER_SCAN = "CODE_HOSPITAL_SHADOW_MAX_PER_SCAN"


@dataclass(frozen=True)
class ShadowCaps:
    """成本闸门的两个参数。

    **只有成本可配，判据不可配。** `MIN_RUNS` / `MIN_CORROBORATION` /
    `MIN_CHECKABLE` 改一个数，两个仓库的重合率就不可比了；而每几轮跑一次、
    每轮跑几台，只影响攒样本的快慢，不影响攒够之后得出什么结论。
    这条线不是洁癖：跨客户的可比性是招聘判据唯一的立足点。
    """

    every_nth: int = EVERY_NTH
    max_per_scan: int = MAX_PER_SCAN


def limits(project_root: str = "") -> ShadowCaps:
    """读影子期的成本参数。环境变量优先，其次 `catalog.shadow`，都没有用默认。"""
    every_nth, max_per_scan = _from_yaml(project_root)
    return ShadowCaps(
        every_nth=_positive(_from_env(_ENV_EVERY_NTH, every_nth), EVERY_NTH),
        max_per_scan=_non_negative(_from_env(_ENV_MAX_PER_SCAN, max_per_scan), MAX_PER_SCAN),
    )


def _from_env(name: str, fallback):
    if name not in os.environ:
        return fallback
    return os.environ.get(name)


def _from_yaml(project_root: str):
    path = Path(project_root) / _CONFIG if project_root else None
    if path is None or not path.is_file():
        return None, None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return None, None
    if not isinstance(doc, dict):
        return None, None
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    shadow = catalog.get("shadow") if isinstance(catalog.get("shadow"), dict) else {}
    return shadow.get("every_nth"), shadow.get("max_per_scan")


def _positive(value, fallback: int) -> int:
    """写错或非正数都落回默认。`every_nth=0` 会让取模变成恒定命中，语义不清。"""
    number = _number(value)
    return number if number and number > 0 else fallback


def _non_negative(value, fallback: int) -> int:
    """允许 0：预算是可以为零的。关了要说出来，不能静默关。"""
    number = _number(value)
    return number if number is not None and number >= 0 else fallback


def _number(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


STATES = (
    "insufficient",     # 样本不够，等
    "unavailable",      # 一直没跑成：不是不合格，是没到岗
    "quiet",            # 跑了但一条没交：判不了
    "noisy",            # 指的位置不存在，比现任多：硬拦
    "uncorroborated",   # 都落在现任不看的位置：无从比对，要人签字
    "contentious",      # 与现任分歧大：可能是新价值，也可能是噪音，要人签字
    "ready",            # 与现任基本一致、没有指错位置：够格，等人签字
)
_PROMOTABLE = frozenset({"uncorroborated", "contentious", "ready"})


def blank_trial() -> dict:
    """一台仪器的试用期账。样本、重合、误报三组数都在这里。"""
    return {
        "runs": 0,               # 跑完的轮次（含 quiet）
        "failed": 0,             # 没跑成的轮次
        "skipped": 0,            # 因成本闸门没跑的轮次
        "claims": 0,             # 累计交出条数
        "corroborated": 0,       # 累计与现任同处的条数
        "checkable": 0,          # 累计落在现任也看的文件上的条数
        "bad": 0,                # 累计点名的位置不存在
        "incumbent_claims": 0,   # 同一批现任发现数（误报率的对照分母）
        "incumbent_bad": 0,
        "first_run": 0,
        "last_run": 0,
        "last_error": "",
        "cases": [],
        "samples": [],
        "seen": [],
    }


def load(raw) -> dict:
    """把账本里存的那份读成完整结构。缺字段按空算，坏字段按空算。"""
    if not isinstance(raw, dict):
        return blank_trial()
    out = blank_trial()
    for key in out:
        if key not in raw:
            continue
        if key in ("cases", "samples", "seen"):
            value = raw.get(key)
            out[key] = list(value) if isinstance(value, list) else []
        elif key in ("last_error",):
            out[key] = str(raw.get(key) or "")
        else:
            try:
                out[key] = int(raw.get(key) or 0)
            except (TypeError, ValueError):
                out[key] = 0
    return out


def observe(trial, run: dict, report_id: str = "") -> dict:
    """把一轮观测并进账本。

    同一份报告重跑不重复记账——否则补一次片、重扫一次，
    样本数就凭空调高，转正判据会被"多扫了几遍"喂饱。
    """
    out = load(trial)
    if report_id and report_id in out["seen"]:
        return out
    status = str(run.get("status") or "ran")
    now = int(run.get("at") or time.time() * 1000)
    out["last_run"] = now
    out["first_run"] = out["first_run"] or now
    if status == "failed":
        out["failed"] += 1
        out["last_error"] = str(run.get("note") or "")[:200]
    else:
        out["runs"] += 1
        for key in ("claims", "corroborated", "checkable", "bad",
                    "incumbent_claims", "incumbent_bad"):
            out[key] += max(0, int(run.get(key) or 0))
    _push(out["cases"], str(run.get("case_id") or ""), 12)
    for sample in (run.get("samples") or [])[:8]:
        if not isinstance(sample, dict):
            continue
        row = {
            "file": str(sample.get("file") or ""),
            "line": int(sample.get("line") or 0),
            "rule_id": str(sample.get("rule_id") or ""),
            "hit": bool(sample.get("hit")),
        }
        _push(out["samples"], row, 24, key=lambda item: (item.get("file"), item.get("line")))
    if report_id:
        _push(out["seen"], report_id, 12)
    return out


def should_run(trial, every_nth: int = EVERY_NTH) -> tuple[bool, str]:
    """这一轮该不该跑它。

    样本不够就每轮都跑（缺的就是样本）；样本够了改成每 `every_nth` 轮跑一次
    （还要能看见它变了没有，但不必每轮付一次进程钱）。
    抽样按轮次取模，不掷随机数：同一份仓库重跑一次要得到同一套账。
    `every_nth` 是成本参数，可从配置来；它不改变「够不够格」的任何判据。
    """
    t = load(trial)
    rounds = t["runs"] + t["failed"]
    if rounds < MIN_RUNS:
        return True, ""
    step = every_nth if every_nth and every_nth > 0 else EVERY_NTH
    if rounds % step:
        return False, f"样本已够（{rounds} 轮），改为每 {step} 轮跑一次以控成本"
    return True, ""


def spots(findings) -> dict[str, list[tuple[int, int]]]:
    """现任发现的位置索引。只留位置，不留规则号——跨厂商只有位置可比。"""
    from ..core.fingerprint import normalize_path

    out: dict[str, list[tuple[int, int]]] = {}
    for item in findings or []:
        path = normalize_path(str(getattr(item, "file_path", "") or ""))
        if not path:
            continue
        start = int(getattr(item, "line_start", 0) or 0)
        end = max(start, int(getattr(item, "line_end", 0) or 0))
        out.setdefault(path, []).append((start, end))
    return out


def near(a_start: int, a_end: int, b_start: int, b_end: int, tol: int = LINE_TOLERANCE) -> bool:
    """两段行区间是否算同一处。容差吸收"标在声明还是标在语句"的差异。"""
    return not (a_end + tol < b_start or b_end + tol < a_start)


def corroborated(samples, indexed: dict[str, list[tuple[int, int]]]) -> int:
    """样本里有多少条，现任在同一处也点了名。"""
    hit = 0
    for sample in samples or []:
        path = str(sample.get("file") or "")
        line = int(sample.get("line") or 0)
        if path and any(near(line, line, start, end) for start, end in indexed.get(path) or []):
            hit += 1
    return hit


def checkable_count(samples, covered: set[str] | None) -> int:
    """样本里有多少条落在现任也看的文件类型上。

    `covered is None` 表示现任里有一台按全类型口径干活（semgrep 这类），
    那就都可比对；`set()` 表示一台能比的都没有——这时候"重合率 0"说明不了任何事，
    只能算"无从比对"，不能算"它全说错了"。
    """
    if covered is None:
        return len(samples or [])
    out = 0
    for sample in samples or []:
        suffix = Path(str(sample.get("file") or "")).suffix.lstrip(".").lower()
        if suffix and suffix in covered:
            out += 1
    return out


def count_bad(samples, line_counts: dict[str, int | None]) -> int:
    """样本里有多少条点名的位置根本不存在。"""
    out = 0
    for sample in samples or []:
        path = str(sample.get("file") or "")
        line = int(sample.get("line") or 0)
        if not path or line < 1:
            out += 1
            continue
        total = line_counts.get(path)
        if total is None:
            out += 1
        elif line > total:
            out += 1
    return out


def covered_extensions(project_root: str, devices: list[dict]) -> set[str] | None:
    """本轮在岗仪器覆盖的扩展名。

    返回 `None` 表示"全类型"（有一台不声明扩展名的仪器在岗），
    返回空集表示"一台能比的都没有"——这两种在判据里意思完全相反，不能都返回空集。
    """
    from .formulary import books_for

    catalog = books_for(project_root)[2]
    live = [
        str(row.get("tool") or "")
        for row in devices or []
        if row.get("available") and row.get("outcome") == "ok"
    ]
    known = [tool for tool in live if tool in catalog]
    if not known:
        return set()
    covered: set[str] = set()
    for tool in known:
        exts = [str(item).lstrip(".").lower() for item in (catalog[tool].get("extensions") or [])]
        if not exts:
            return None
        covered.update(exts)
    return covered


def verdict(trial) -> dict:
    """按三个数判一个结论。判的是"够不够格转正"，不是"好不好用"。"""
    t = load(trial)
    runs, failed = t["runs"], t["failed"]
    claims, checkable = t["claims"], t["checkable"]
    corroborated, bad = t["corroborated"], t["bad"]
    rounds = runs + failed
    rate = (corroborated / checkable) if checkable else 0.0
    bad_rate = (bad / claims) if claims else 0.0
    inc_rate = (t["incumbent_bad"] / t["incumbent_claims"]) if t["incumbent_claims"] else 0.0
    out = {
        "state": "insufficient",
        "promotable": False,
        "runs": runs,
        "failed": failed,
        "claims": claims,
        "corroborated": corroborated,
        "checkable": checkable,
        "rate": round(rate, 3),
        "bad": bad,
        "bad_rate": round(bad_rate, 3),
        "incumbent_rate": round(inc_rate, 3),
        "line": "",
        "reasons": [],
    }
    pct = lambda value: f"{round(value * 100)}%"  # noqa: E731

    if runs == 0 and failed >= GIVE_UP_ROUNDS:
        out.update(
            state="unavailable",
            line=(
                f"{failed} 轮都没跑成（最后一条：{t['last_error'] or '无说明'}）："
                "不是不合格，是没到岗。"
            ),
            reasons=["装好了但没跑起来，先解决到岗问题再谈转正"],
        )
        return out
    if rounds < MIN_RUNS:
        out.update(
            line=f"样本 {rounds}/{MIN_RUNS} 轮，还差 {MIN_RUNS - rounds} 轮才够判。",
            reasons=[f"样本不足：{rounds}/{MIN_RUNS}"],
        )
        return out
    if claims == 0:
        out.update(
            state="quiet",
            line=(
                f"跑了 {runs} 轮一条也没交出来：判不出是代码干净还是没接上，"
                "不转正（转正是给「能干活」的，不是给「不出声」的）。"
            ),
            reasons=["影子期没交过任何一条，无法判断"],
        )
        return out
    if bad_rate > inc_rate:
        out.update(
            state="noisy",
            line=(
                f"交 {claims} 条，其中 {bad} 条点的位置不存在（{pct(bad_rate)}），"
                f"比现任（{pct(inc_rate)}）高。不转正。"
            ),
            reasons=[f"指的位置有 {bad} 条不存在，误报率高于现任"],
        )
        return out
    if checkable < MIN_CHECKABLE:
        out.update(
            state="uncorroborated",
            promotable=True,
            line=(
                f"样本 {runs} 轮，交 {claims} 条，但只有 {checkable} 条落在现任也看的文件上："
                "无从比对，精度没人替它作证。转正要人签字。"
            ),
            reasons=[f"可比对样本 {checkable} 条，低于 {MIN_CHECKABLE} 条，重合率不算数"],
        )
        return out
    if rate < MIN_CORROBORATION:
        out.update(
            state="contentious",
            promotable=True,
            line=(
                f"样本 {runs} 轮，交 {claims} 条，只有 {corroborated}/{checkable} 条"
                f"与现任同处（{pct(rate)}）。其余可能是新价值，也可能是噪音。转正要人签字。"
            ),
            reasons=[f"与现任同处 {pct(rate)}，低于 {pct(MIN_CORROBORATION)}"],
        )
        return out
    out.update(
        state="ready",
        promotable=True,
        line=(
            f"样本 {runs} 轮，交 {claims} 条，{corroborated}/{checkable} 条与现任同处"
            f"（{pct(rate)}），没有指错位置。够格转正，等人签字。"
        ),
        reasons=[],
    )
    return out


def _push(bucket: list, value, cap: int, key=None) -> None:
    if value in (None, ""):
        return
    if key:
        for existing in bucket:
            if key(existing) == key(value):
                return
    elif value in bucket:
        return
    bucket.append(value)
    del bucket[:-cap]


__all__ = [
    "MAX_PER_SCAN",
    "MIN_CORROBORATION",
    "MIN_CHECKABLE",
    "MIN_RUNS",
    "STATES",
    "ShadowCaps",
    "blank_trial",
    "checkable_count",
    "corroborated",
    "count_bad",
    "covered_extensions",
    "limits",
    "load",
    "near",
    "observe",
    "should_run",
    "spots",
    "verdict",
]
