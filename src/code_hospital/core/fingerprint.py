"""Finding 指纹与上下文锚（设计 §4.5，算法 A1）。

content_key = sha256(rule_id | path | symbol | canonical_snippet)  认长相，复发调档用它。
identity    = sha256(content_key | anchor | occurrence)           认身份，差分与入库指纹用它。
context_anchor = 前后各 2 行归一化签名。occurrence 必须在一批发现上统一补，见 algorithms.stamp_findings。
"""

from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")


def sha256_str(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_snippet(text: str) -> str:
    """空白归一化：逐行去首尾空白、行内连续空白压缩为一个空格，去空行。"""
    lines = [_WS.sub(" ", ln.strip()) for ln in text.strip().splitlines()]
    return "\n".join(ln for ln in lines if ln)


def normalize_path(path: str) -> str:
    """仓库相对路径统一为 POSIX 形式（只去一个前导 './'，保护 .hospital 类点目录）。"""
    posix = path.replace("\\", "/")
    return posix[2:] if posix.startswith("./") else posix


def context_anchor(before_lines: list[str], after_lines: list[str]) -> str:
    """前后各 2 行归一化签名；取不到内容时返回 ""（指纹退化为 symbol 签名）。"""
    joined = canonical_snippet("\n".join(before_lines + after_lines))
    return sha256_str(joined) if joined else ""


def make_keys(
    rule_id: str,
    file_path: str,
    symbol: str,
    snippet: str,
    anchor: str,
    occurrence: int = 0,
) -> tuple[str, str]:
    """返回 (content_key, identity_fp)。occurrence 区分同文件里复制粘贴的多处命中。"""
    content = sha256_str(
        "|".join([rule_id, normalize_path(file_path), symbol, canonical_snippet(snippet)])
    )
    identity = sha256_str("|".join([content, anchor, str(occurrence)]))
    return content, identity


def fingerprint(
    rule_id: str,
    file_path: str,
    symbol: str,
    snippet: str,
    anchor: str,
    occurrence: int = 0,
) -> str:
    """单条身份指纹。同一批里有多处同形态命中时，先用 algorithms.stamp_findings 补序号。"""
    return make_keys(rule_id, file_path, symbol, snippet, anchor, occurrence)[1]
