"""评测命中的三档口径。三档分开计，不合成一个分数。

行级：同一文件，行号区间有重叠。
函数级：同一文件，两边都有函数名并且相同。没有函数名的金标不进这一档。
语义等价：只认标注里的 equivalent。行号重合不算语义命中。
宿主交卷不参与行级和函数级。没有金标或这一档没有分母时，不算分。
"""

from __future__ import annotations

from ..core.models import counts_toward_efficacy

_NO_GOLD = "没有金标，这一档不算分。"
_NO_LINE = "金标没有文件和行号，行级不算分。"
_NO_SYMBOL = "金标没有函数名，函数级不算分。"
_NO_LABEL = "语义等价还没有标注，这一档不算分。"


def score_hits(gold: list, findings: list, mappings: list | None = None) -> dict:
    """返回行级、函数级、语义等价三档。rate 为空表示这一档不算分。"""
    if not gold:
        empty = {"gold": 0, "hit": 0, "unscoped": 0, "rate": None, "note": _NO_GOLD}
        return {
            "line": dict(empty),
            "function": dict(empty),
            "semantic": {"labeled": 0, "hit": 0, "unlabeled": 0, "rate": None, "note": _NO_GOLD},
        }
    return {
        "line": _line(gold, findings),
        "function": _function(gold, findings),
        "semantic": _semantic(gold, mappings or []),
    }


def render_hit_report(score: dict) -> str:
    return "\n".join([
        "## 命中口径",
        "",
        f"- 行级：{score['line']['note']}",
        f"- 函数级：{score['function']['note']}",
        f"- 语义等价：{score['semantic']['note']}",
        "",
        "行级看同一文件的行号是否重叠。函数级看同一文件的函数名是否相同。"
        "语义等价只认标注里的 equivalent。",
        "",
    ])


def render_eval_report(
    score: dict,
    *,
    source: str,
    line_ids: list[str] | None = None,
    semantic_ids: list[str] | None = None,
    coverage: dict | None = None,
) -> str:
    """三档写进同一份评测报告，分数旁写覆盖率。不合成一个分数。"""
    lines = [
        "# 评测报告",
        "",
        source.strip(),
        "",
        "三档分开计，不合成一个分数。",
        "",
    ]
    lines.extend(_coverage_section(coverage if coverage is not None else measure_coverage(None)))
    lines.extend(_tier_section("行级", score["line"], line_ids))
    lines.extend(_tier_section("函数级", score["function"], None))
    lines.extend(_tier_section("语义等价", score["semantic"], semantic_ids))
    lines.append(
        "行级看同一文件的行号是否重叠。函数级看同一文件的函数名是否相同。"
        "语义等价只认标注里的 equivalent。覆盖率看已读文件是否盖满范围，不并进上面三档。"
    )
    lines.append("")
    return "\n".join(lines)


def measure_coverage(scopes: list | None) -> dict:
    """已读文件盖满范围的份数。没有范围账时不算覆盖率。"""
    usable = []
    for row in scopes or []:
        files = _file_set(row.get("files") if isinstance(row, dict) else None)
        if not files:
            continue
        read = _file_set(row.get("read") if isinstance(row, dict) else None)
        usable.append(files <= read)
    if not usable:
        return {"scopes": 0, "covered": 0, "rate": None, "note": "没有范围账，覆盖率不算。"}
    covered = sum(1 for done in usable if done)
    rate = round(covered / len(usable), 4)
    note = f"{covered}/{len(usable)}"
    if covered == len(usable):
        note += "，已读文件盖满范围"
    else:
        note += "，未盖满的范围不算已经看完"
    return {"scopes": len(usable), "covered": covered, "rate": rate, "note": note}


def _coverage_section(coverage: dict) -> list[str]:
    lines = ["## 覆盖率", coverage["note"]]
    if coverage["rate"] is None:
        if "不算" not in coverage["note"]:
            lines.append("覆盖率不算。")
    else:
        lines.append(f"覆盖率是 {coverage['rate']}。")
    lines.append("")
    return lines


def _file_set(paths) -> set[str]:
    found = set()
    for path in paths or []:
        text = str(path or "").replace("\\", "/").strip()
        while text.startswith("./"):
            text = text[2:]
        text = text.lstrip("/")
        if text:
            found.add(text)
    return found


def _tier_section(title: str, tier: dict, ids: list[str] | None) -> list[str]:
    lines = [f"## {title}", tier["note"]]
    if tier["rate"] is None:
        if "不算分" not in tier["note"]:
            lines.append("这一档不算分。")
    else:
        lines.append(f"这一档的比例是 {tier['rate']}。")
        if ids is not None:
            lines.append("命中：" + ("、".join(ids) if ids else "无"))
    lines.append("")
    return lines


def _line(gold: list, findings: list) -> dict:
    located = [item for item in gold if _path(item) and _span(item)]
    unscoped = len(gold) - len(located)
    if not located:
        return {"gold": 0, "hit": 0, "unscoped": unscoped, "rate": None, "note": _NO_LINE}
    candidates = [item for item in findings if _automatic(item) and _path(item) and _span(item)]
    hit = 0
    for item in located:
        span = _span(item)
        path = _path(item)
        if any(path == _path(found) and _overlap(span, _span(found)) for found in candidates):
            hit += 1
    return _ratio(hit, len(located), unscoped)


def _function(gold: list, findings: list) -> dict:
    scoped = [item for item in gold if _path(item) and _symbol(item)]
    unscoped = len(gold) - len(scoped)
    if not scoped:
        return {"gold": 0, "hit": 0, "unscoped": unscoped, "rate": None, "note": _NO_SYMBOL}
    candidates = [item for item in findings if _automatic(item) and _path(item) and _symbol(item)]
    hit = 0
    for item in scoped:
        path = _path(item)
        symbol = _symbol(item)
        if any(path == _path(found) and symbol == _symbol(found) for found in candidates):
            hit += 1
    return _ratio(hit, len(scoped), unscoped)


def _semantic(gold: list, mappings: list) -> dict:
    labels: dict[str, bool] = {}
    for row in mappings:
        if not isinstance(row, dict) or not isinstance(row.get("equivalent"), bool):
            continue
        gold_id = str(row.get("gold_id") or "")
        if gold_id:
            labels[gold_id] = row["equivalent"]
    labeled = 0
    hit = 0
    unlabeled = 0
    for item in gold:
        gold_id = _id(item)
        if gold_id not in labels:
            unlabeled += 1
            continue
        labeled += 1
        if labels[gold_id]:
            hit += 1
    if labeled == 0:
        return {"labeled": 0, "hit": 0, "unlabeled": unlabeled, "rate": None, "note": _NO_LABEL}
    rate = round(hit / labeled, 4)
    note = f"{hit}/{labeled}"
    if unlabeled:
        note += f"，另有 {unlabeled} 条还没标注"
    return {"labeled": labeled, "hit": hit, "unlabeled": unlabeled, "rate": rate, "note": note}


def _ratio(hit: int, total: int, unscoped: int) -> dict:
    rate = round(hit / total, 4)
    note = f"{hit}/{total}"
    if unscoped:
        note += f"，另有 {unscoped} 条不进这一档"
    return {"gold": total, "hit": hit, "unscoped": unscoped, "rate": rate, "note": note}


def _automatic(finding) -> bool:
    concluded = str(_value(finding, "concluded_by") or "instrument")
    return counts_toward_efficacy(concluded)


def _overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def _path(item) -> str:
    return str(_value(item, "file", "file_path", "path") or "").replace("\\", "/").lstrip("./")


def _span(item) -> tuple[int, int] | None:
    start = _value(item, "line", "line_start")
    if start in ("", None):
        return None
    try:
        start_n = int(start)
    except (TypeError, ValueError):
        return None
    end = _value(item, "end", "line_end")
    try:
        end_n = start_n if end in ("", None) else int(end)
    except (TypeError, ValueError):
        return None
    if end_n < 1:
        end_n = start_n
    if start_n < 1 or end_n < start_n:
        return None
    return start_n, end_n


def _symbol(item) -> str:
    return str(_value(item, "symbol") or "").strip()


def _id(item) -> str:
    return str(_value(item, "id") or "")


def _value(item, *names):
    if isinstance(item, dict):
        for name in names:
            if name in item and item[name] not in (None, ""):
                return item[name]
        return ""
    for name in names:
        if hasattr(item, name):
            value = getattr(item, name)
            if value not in (None, ""):
                return value
    return ""
