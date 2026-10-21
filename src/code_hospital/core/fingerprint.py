"""Finding 指纹与上下文锚（设计 §4.5）。

fingerprint = sha256(rule_id | normalized_path | symbol | canonical_snippet | context_anchor)
context_anchor = 前后各 2 行归一化签名——「修复改变周围代码」时仍可二次匹配。
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
    """仓库相对路径统一为 POSIX 形式。"""
    return path.replace("\\", "/").lstrip("./")


def context_anchor(before_lines: list[str], after_lines: list[str]) -> str:
    """前后各 2 行归一化签名；取不到内容时返回 ""（指纹退化为 symbol 签名）。"""
    joined = canonical_snippet("\n".join(before_lines + after_lines))
    return sha256_str(joined) if joined else ""


def fingerprint(
    rule_id: str,
    file_path: str,
    symbol: str,
    snippet: str,
    anchor: str,
) -> str:
    material = "|".join(
        [rule_id, normalize_path(file_path), symbol, canonical_snippet(snippet), anchor]
    )
    return sha256_str(material)
