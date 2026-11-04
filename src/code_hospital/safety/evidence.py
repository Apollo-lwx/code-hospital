"""证据层：模型结论要定级，先得能被机器核对。

医院不判断「模型说得对不对」——那不是可复现的事。医院只判断一件确定的事：
**在模型点名的位置，能不能逐字找到它抄的那段原文。**

四档：

| tier | 条件 | 能做什么 |
|---|---|---|
| `anchored` | 文件在、引文在文件里逐字找得到 | 可以定级进报告（仍不进疗效） |
| `located` | 文件在、行号可用，但没抄原文 | 只能到文件行，定级上限压到 `minor` |
| `ungrounded` | 抄了原文，文件里找不到 | 不进结论区，记一条 `unfounded_claim` |
| `clue` | 文件不存在或落在仓库外 | 只进线索 |

三条边界写死，不允许为了「多进几条」放宽：

1. **引文对得上只证明模型读过这一行，不证明它的判断成立。** 报告措辞必须是
   「模型据此处原文判断……」，不是「检查通过」。定级进报告是呈现，不是背书。
2. **核对是纯字符串比对。** 不做大小写折叠、不做同义词、不调模型——任何一步
   「聪明」的处理都会让结论不可复现，而不可复现的结论不能进差分。
3. **行号可以漂移，引文不能。** 模型给的 `line_start` 偏了但引文在文件里别处找得到，
   按找到的位置纠正（`verified_moved`），因为行号漂移是常态；引文找不到则一律
   `ungrounded`，因为那是编的。

`located` 与 `clue` 的区别不是严重程度，而是「还能不能复核」：前者至少点到了文件，
下一轮可以照着读；后者连去哪儿看都不知道。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..core.models import SEVERITY_ORDER

TIERS = ("anchored", "located", "ungrounded", "clue")
TIER_PLAIN = {
    "anchored": "引文已核",
    "located": "只到文件行",
    "ungrounded": "引文对不上",
    "clue": "没有可用位置",
}
# 定级上限。锚不住的东西不能给高等级，否则「模型说这是 critical」就变成事实。
# 这三条是判断不是参数，不开放配置——开放了不同客户的报告就不可比。
TIER_CAP = {
    "anchored": "",
    "located": "minor",
    "ungrounded": "info",
    "clue": "info",
}
_STATUS_TIER = {
    "verified": "anchored",
    "verified_loose": "anchored",
    "verified_moved": "anchored",
    "mismatch": "ungrounded",
    "quote_absent": "ungrounded",
    "unquoted": "located",
    "file_only": "located",
    "no_file": "clue",
    "outside_root": "clue",
    "missing_file": "clue",
    "too_large": "clue",
    "unreadable": "clue",
}
_STATUS_PLAIN = {
    "verified": "引文与文件逐字一致",
    "verified_loose": "引文与文件一致（只是空白不同）",
    "verified_moved": "引文在文件里，但不在声称的行号上",
    "mismatch": "声称的行号上没有这段原文",
    "quote_absent": "整份文件里都找不到这段原文",
    "unquoted": "没有抄原文",
    "file_only": "只点到文件，没有行号也没有原文",
    "no_file": "没有给出文件",
    "outside_root": "文件不在这个仓库里",
    "missing_file": "文件不存在",
    "too_large": "文件太大，没有核对",
    "unreadable": "文件读不出来，没有核对",
}
MAX_SPAN = 200
MAX_BYTES = 2_000_000
# 太短的引文不当证据：「}」「);」「true」这种在哪个文件里都能找到，核对了也没有信息量。
QUOTE_MIN = 6


def check(
    project_root: str,
    file_path: str,
    line_start: int = 0,
    line_end: int = 0,
    quote: str = "",
    cache: dict | None = None,
) -> dict:
    """核对一条模型结论的锚。返回 tier / status / 纠正后的行号 / 内容指纹。

    `cache` 是同一次就诊内按文件复用的行缓存，传 None 时不缓存。
    """
    rel = _normalize_path(file_path)
    found = _blank(rel, line_start, line_end, quote)
    if not rel:
        return _done(found, "no_file")
    root = Path(project_root)
    target = _resolve(root, rel)
    if target is None:
        return _done(found, "outside_root")
    if not target.is_file():
        return _done(found, "missing_file")
    lines, problem = _lines(target, rel, cache)
    if problem:
        return _done(found, problem)
    total = len(lines)
    if line_start < 1 or line_start > total:
        start = end = 0
    else:
        start = line_start
        end = min(max(line_end or line_start, start), total, start + MAX_SPAN - 1)

    text = _fold(quote)
    if len(_squeeze(text)) >= QUOTE_MIN:
        return _locate_by_quote(found, lines, start, end, text)
    if start:
        found["line_start"] = start
        found["line_end"] = end
        return _done(found, "unquoted")
    return _done(found, "file_only")


def grade(severity: str, tier: str) -> tuple[str, str]:
    """按证据档定级，返回 (定级, 降级说明)。降了要说出来，不许偷偷改数。"""
    value = severity if severity in SEVERITY_ORDER else "major"
    cap = TIER_CAP.get(tier, "info")
    if not cap or SEVERITY_ORDER[value] >= SEVERITY_ORDER[cap]:
        return value, ""
    return cap, (
        f"证据只到「{TIER_PLAIN.get(tier, tier)}」，定级从 {value} 压到 {cap}。"
    )


def summary(rows: list[dict]) -> dict:
    """本轮证据账。报告要能看出「几条模型结论真的可核」。"""
    counts = {tier: 0 for tier in TIERS}
    for row in rows or []:
        tier = str((row or {}).get("tier") or "clue")
        counts[tier if tier in counts else "clue"] += 1
    total = sum(counts.values())
    return {
        "total": total,
        **counts,
        "note": _summary_note(counts),
    }


def limitation_line(book: dict) -> str:
    """写进报告限制项的一句话。全空时不写，不占版面。"""
    if not isinstance(book, dict) or not int(book.get("total") or 0):
        return ""
    return (
        f"模型交卷 {book['total']} 条：引文已核 {book['anchored']} 条、"
        f"只到文件行 {book['located']} 条、引文对不上 {book['ungrounded']} 条、"
        f"没有可用位置 {book['clue']} 条。只有引文已核的进结论区，"
        "其余进待核实线索，都不进疗效。"
    )


def evidence_lines(coverage: dict | None) -> list[str]:
    """诊断书「未查与缺口」里的证据账。没有模型交卷时不出现。"""
    book = (coverage or {}).get("evidence")
    if not isinstance(book, dict) or not int(book.get("total") or 0):
        return []
    line = limitation_line(book)
    note = str(book.get("note") or "")
    if note and note not in line:
        line = f"{line}{note}"
    return [line]


def digest_of(file_path: str, line_start: int, line_end: int, quote: str) -> str:
    """一条模型的「主张」的内容指纹。同一段原文重复交卷算同一条，便于下一轮去重。"""
    key = "|".join([
        _normalize_path(file_path),
        str(int(line_start or 0)),
        str(int(line_end or 0)),
        _fold(quote),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _locate_by_quote(found: dict, lines: list[str], start: int, end: int, text: str) -> dict:
    """先按声称的行号找，找不到再退回整份文件——行号漂移是常态，编造不是。

    四种命中的信任度从高到低：原位逐字、别处逐字、原位只差空白、别处只差空白。
    前两种纠正行号后按 anchored 记，后两种在说明里写清楚差在哪。
    """
    if start:
        hit = _search(lines, start, end, text)
        if hit:
            found.update(hit)
            return _done(found, "verified")
        moved = _search(lines, 1, len(lines), text)
        if moved:
            found.update(moved)
            found["detail"] = f"引文在 {moved['line_start']} 行，不在声称的 {start} 行。"
            return _done(found, "verified_moved")
        loose = _search(lines, start, end, text, loose=True)
        if loose:
            found.update(loose)
            found["detail"] = "原文一致，只是空白不同。"
            return _done(found, "verified_loose")
        loose_moved = _search(lines, 1, len(lines), text, loose=True)
        if loose_moved:
            found.update(loose_moved)
            found["detail"] = (
                f"原文一致（只是空白不同），在 {loose_moved['line_start']} 行，"
                f"不在声称的 {start} 行。"
            )
            return _done(found, "verified_moved")
        return _done(found, "mismatch")

    whole = _search(lines, 1, len(lines), text)
    if whole:
        found.update(whole)
        return _done(found, "verified")
    loose = _search(lines, 1, len(lines), text, loose=True)
    if loose:
        found.update(loose)
        return _done(found, "verified_loose")
    return _done(found, "quote_absent")


def _search(lines: list[str], start: int, end: int, text: str, loose: bool = False) -> dict | None:
    """在 [start, end] 里找引文。返回纠正后的行号和命中方式，找不到返回 None。"""
    if start < 1 or end < start or start > len(lines):
        return None
    stop = min(end, len(lines))
    if loose:
        body, where = _squeeze_indexed(lines, start, stop)
        at = body.find(_squeeze(text))
        if at < 0:
            return None
        first = where[at]
        return {"line_start": first, "line_end": first, "match": "loose"}

    folded = _fold("\n".join(lines[start - 1: stop]))
    at = folded.find(text)
    if at < 0:
        return None
    first = start + folded[:at].count("\n")
    span = max(1, text.count("\n") + 1)
    return {
        "line_start": first,
        "line_end": min(first + span - 1, len(lines)),
        "match": "exact",
    }


def _squeeze_indexed(lines: list[str], start: int, stop: int) -> tuple[str, list[int]]:
    """去掉全部空白后拼接，并记下每个字符原来的行号，用于反推命中位置。"""
    chunks: list[str] = []
    where: list[int] = []
    for number in range(start, stop + 1):
        body = "".join(lines[number - 1].split())
        if not body:
            continue
        chunks.append(body)
        where.extend([number] * len(body))
    return "".join(chunks), where


def _lines(target: Path, rel: str, cache: dict | None) -> tuple[list[str], str]:
    if cache is not None and rel in cache:
        lines, problem = cache[rel]
        return lines, problem
    try:
        if target.stat().st_size > MAX_BYTES:
            lines, problem = [], "too_large"
        else:
            raw = target.read_bytes().decode("utf-8", errors="replace")
            lines, problem = raw.splitlines(), ""
    except OSError:
        lines, problem = [], "unreadable"
    if cache is not None:
        cache[rel] = (lines, problem)
    return lines, problem


def _resolve(root: Path, rel: str) -> Path | None:
    """仓库内的相对路径。`..` 能跑出去的一律不核。"""
    try:
        base = root.resolve()
    except OSError:
        base = root
    try:
        target = (base / rel).resolve()
    except OSError:
        return None
    if target != base and base not in target.parents:
        return None
    return target


def _normalize_path(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _fold(text: str) -> str:
    """逐行去首尾空白、合并行内空白。行边界保留，所以不会跨行误配。"""
    return "\n".join(" ".join(line.split()) for line in str(text or "").splitlines())


def _squeeze(text: str) -> str:
    return "".join(str(text or "").split())


def _blank(rel: str, line_start: int, line_end: int, quote: str) -> dict:
    return {
        "tier": "clue",
        "status": "",
        "match": "",
        "file": rel,
        "line_start": max(0, int(line_start or 0)),
        "line_end": max(0, int(line_end or 0)),
        "quote": str(quote or ""),
        "digest": digest_of(rel, line_start, line_end, quote),
        "detail": "",
    }


def _done(found: dict, status: str) -> dict:
    found["status"] = status
    found["tier"] = _STATUS_TIER.get(status, "clue")
    if not found.get("detail"):
        found["detail"] = _STATUS_PLAIN.get(status, "")
    return found


def _summary_note(counts: dict) -> str:
    total = sum(counts.values())
    if not total:
        return "本轮没有模型交卷。"
    if counts["anchored"] == total:
        return "模型交卷全部附了原文并核对通过，可以定级。"
    if counts["anchored"]:
        return (
            "只有引文已核的那部分可以定级；没抄原文的压到一般问题，"
            "引文对不上的只作线索。"
        )
    return "没有一条模型结论能核对到原文，本轮只能作线索，不能定级。"
