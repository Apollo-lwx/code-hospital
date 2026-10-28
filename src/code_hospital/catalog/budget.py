"""模型分析预算。没配置上限时不限制。超限只停模型分析，仪器照常出结果。

调用次数和 token 上限来自环境变量，或仓库根目录 `code-hospital.yaml`
的 `catalog.budget`。环境变量优先。负数视为没配置。
一次仪器发现算一次调用；token 按结论文本长度估算，四个字符算一个。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

BUDGET_LINE = "本次因预算上限未做模型分析"
_CALLS = "CODE_HOSPITAL_MAX_LLM_CALLS"
_TOKENS = "CODE_HOSPITAL_MAX_LLM_TOKENS"
_CONFIG = "code-hospital.yaml"


@dataclass(frozen=True)
class Limits:
    max_calls: int | None = None
    max_tokens: int | None = None

    @property
    def configured(self) -> bool:
        return self.max_calls is not None or self.max_tokens is not None

    def exceeded(self, calls: int, tokens: int) -> bool:
        if self.max_calls is not None and calls > self.max_calls:
            return True
        if self.max_tokens is not None and tokens > self.max_tokens:
            return True
        return False


def load_limits(project_root: str) -> Limits:
    calls, tokens = _from_yaml(project_root)
    return Limits(
        max_calls=_from_env(_CALLS, calls),
        max_tokens=_from_env(_TOKENS, tokens),
    )


def measure(findings, checks=None) -> tuple[int, int]:
    """仪器发现和待做的模型事项各算一次。宿主交卷不再计。"""
    calls = 0
    tokens = 0
    for finding in findings or []:
        if getattr(finding, "concluded_by", "") == "host_llm":
            continue
        calls += 1
        text = str(getattr(finding, "message", "") or "")
        tokens += max(1, len(text) // 4)
    for check in checks or []:
        for executor in check.get("executors") or []:
            if executor.get("kind") != "llm":
                continue
            calls += 1
            text = str(executor.get("text") or "")
            tokens += max(1, len(text) // 4)
    return calls, tokens


def note_budget(project_root: str, findings, checks, limitations: list[str], ledger: dict | None) -> dict:
    """超限时在限制列表和覆盖账写上同一句。没设上限且此前没降级时不改覆盖账。"""
    out = dict(ledger or {})
    prior = out.get("budget") if isinstance(out.get("budget"), dict) else {}
    caps = load_limits(project_root)
    calls, tokens = measure(findings, checks)
    if not caps.configured and not prior.get("degraded"):
        return out
    degraded = bool(prior.get("degraded")) or caps.exceeded(calls, tokens)
    record = {
        "max_llm_calls": caps.max_calls,
        "max_llm_tokens": caps.max_tokens,
        "calls": calls,
        "tokens": tokens,
        "degraded": degraded,
    }
    if degraded:
        record["note"] = BUDGET_LINE
        if BUDGET_LINE not in limitations:
            limitations.append(BUDGET_LINE)
    out["budget"] = record
    return out


def hold_semantic(checks: list[dict] | None) -> None:
    """超限后宿主 LLM 事项不再待交卷。代码和外部工具的结果留着。"""
    for item in checks or []:
        held = False
        for executor in item.get("executors") or []:
            if executor.get("kind") != "llm" or executor.get("status") != "pending":
                continue
            executor["status"] = "budget"
            executor["text"] = BUDGET_LINE
            held = True
        if not held:
            continue
        item["status"] = "budget"
        item["note"] = BUDGET_LINE


def downgrade_model_items(items: list[dict]) -> None:
    """超限后需要模型补丁的药味改为人工处理，流程继续。"""
    for item in items:
        if item.get("method") != "llm_patch":
            continue
        item["method"] = "manual"
        item["acceptance"] = {"alias": "", "expect": "人工处理，无命令验收"}
        item["instructions"] = f"{BUDGET_LINE} {item.get('instructions') or ''}".strip()[:500]


def _from_env(name: str, fallback: int | None) -> int | None:
    if name not in os.environ:
        return fallback
    return _cap(os.environ.get(name))


def _from_yaml(project_root: str) -> tuple[int | None, int | None]:
    path = Path(project_root) / _CONFIG if project_root else None
    if path is None or not path.is_file():
        return None, None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None, None
    if not isinstance(doc, dict):
        return None, None
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    budget = catalog.get("budget") if isinstance(catalog.get("budget"), dict) else {}
    return _cap(budget.get("max_llm_calls")), _cap(budget.get("max_llm_tokens"))


def _cap(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number
