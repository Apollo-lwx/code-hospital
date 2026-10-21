"""ULID 生成（零依赖实现）。

契约 §4：ID 一律 `前缀_ulid`，前缀集合：
case / rpt / fnd / dia / rx / rxit / dose / nlog / dis / lnk / ptk / q
"""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """48bit 毫秒时间戳 + 80bit 随机数，Crockford Base32 编码为 26 字符。"""
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(os.urandom(10), "big")
    value = (timestamp_ms << 80) | randomness
    chars = []
    for _ in range(26):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD[rem])
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """生成带前缀的资源 ID，如 new_id("case") -> case_01J..."""
    return f"{prefix}_{ulid()}"
