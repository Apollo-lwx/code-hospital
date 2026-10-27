"""处方疗效的小样本估计。开方时现算，不另建统计表。

只统计药味结果为 success / failed 的记录。可自动修改和需要补丁如果没跑验收，
成功不计入。失败仍然计入。没有样本时调用方不展示，开方顺序保持名册。
"""

from __future__ import annotations


def classify_outcome(result: str, method: str, acceptance: dict | None) -> str | None:
    """返回 success、failure，或 None（不进入疗效账）。"""
    if result == "failed":
        return "failure"
    if result != "success":
        return None
    alias = ""
    if isinstance(acceptance, dict):
        alias = str(acceptance.get("alias") or "")
    if method in {"autofix", "llm_patch"} and not alias:
        return None
    return "success"


def posterior(success: int, failure: int) -> float:
    """Beta(1, 1) 后验均值。(1 + 成功) / (2 + 成功 + 失败)。"""
    return (1 + success) / (2 + success + failure)


def lesson_text(history: dict[tuple[str, str], tuple[int, int]]) -> str:
    """给外部记忆的疗效账。只有规则号和剂型，没有文件路径和发现原文。"""
    lines = []
    for rule_id, method in sorted(history):
        success, failure = history[(rule_id, method)]
        if success + failure <= 0:
            continue
        percent = round(100 * posterior(success, failure))
        lines.append(
            f"{rule_id} {method} 成功 {success} 次，失败 {failure} 次，估计成功率 {percent}%"
        )
        if len(lines) >= 20:
            break
    if not lines:
        return ""
    return "本仓库疗效账。不改变名册。\n" + "\n".join(lines)


def tally(rows: list[dict]) -> dict[tuple[str, str], tuple[int, int]]:
    """键是 (rule_id, method)，值是 (成功次数, 失败次数)。"""
    buckets: dict[tuple[str, str], tuple[int, int]] = {}
    for row in rows:
        kind = classify_outcome(row.get("result") or "", row.get("method") or "", row.get("acceptance"))
        if kind is None:
            continue
        key = (str(row.get("rule_id") or ""), str(row.get("method") or ""))
        if not key[0]:
            continue
        success, failure = buckets.get(key, (0, 0))
        if kind == "success":
            buckets[key] = (success + 1, failure)
        else:
            buckets[key] = (success, failure + 1)
    return buckets
