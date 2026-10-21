"""提示词注入防护（设计 §6.1 不可信内容边界）。

findings 的 message/evidence 等字段源自仓库源码，属不可信数据：
出参前统一去控制字符、转义、定长截断、过滤疑似指令模式。
"""

from __future__ import annotations

import re

# 控制字符（保留 \n \t）、零宽字符、双向覆盖字符（防视觉欺骗）
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH = re.compile(r"[​‌‍⁠﻿]")
_BIDI = re.compile(r"[‪-‮⁦-⁩]")
# 疑似指令模式（提示层注入常见起手式），命中后以 [FILTERED] 替换
_INSTRUCTIONISH = re.compile(
    r"(ignore\s+(all\s+)?(previous|above)\s+instructions?"
    r"| disregard\s+(all\s+)?(previous|above)"
    r"| 忽略(之前|上述|所有)的?(指令|指示|命令)"
    r"| system\s*prompt\s*:| you\s+are\s+now\s)",
    re.IGNORECASE,
)

EVIDENCE_MAX = 200
TEXT_MAX = 500


def _clean(text: str) -> str:
    text = _CONTROL.sub("", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _BIDI.sub("", text)
    return text


def sanitize_evidence(text: str, limit: int = EVIDENCE_MAX) -> str:
    """evidence 通道：净化 + 截断（≤200 字符）+ 疑似指令过滤。"""
    cleaned = _INSTRUCTIONISH.sub("[FILTERED]", _clean(text))
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def sanitize_text(text: str, limit: int = TEXT_MAX) -> str:
    """message 等短文本通道：净化 + 疑似指令过滤 + 截断。

    message 同为仓库来源的不可信数据，与 evidence 同等设防。
    """
    cleaned = _INSTRUCTIONISH.sub("[FILTERED]", _clean(text))
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def mask_secret(secret: str, keep: int = 4) -> str:
    """gitleaks 证据脱敏：绝不把明文密钥写进报告/病历。"""
    if not secret:
        return "(masked)"
    head = secret[:keep] if len(secret) > keep else secret[:1]
    return f"{head}…(masked,{len(secret)} chars)"
