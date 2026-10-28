"""用户介入出参变体（契约 §2.9）与阶段交接块（§2.10）的构造器。

pending_question 是**出参变体而非错误**：工具返回它时不执行本阶段逻辑，
流程挂起等待用户决策；回答经既有工具的确认参数回流，不为问题单开工具。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .ids import new_id

_HALT = {"pause", "defer"}
_ELICIT_TIMEOUT = 20

# 阶段门禁的产物名与下一阶段工具映射（设计 §4.11 门禁点）
STAGE_ARTIFACTS = {
    "registered": ("挂号单", "hosp_scan"),
    "scanned": ("检验报告", "hosp_diagnose"),
    "diagnosed": ("诊断书", "hosp_prescribe"),
}


def stage_gate_question(
    case_id: str,
    stage: str,
    next_tool: str,
    prompt_meta: str,
) -> dict:
    """常规门禁（叫号）：阶段产物待确认，提示用户确认后以 stage_confirmed=true 重调。

    prompt 只含元数据与报告路径，不拼仓库原文（§6.1 不可信内容边界）。
    """
    artifact = "护理记录" if stage.startswith("dose:") else STAGE_ARTIFACTS.get(stage, (stage, next_tool))[0]
    return {
        "status": "pending_question",
        "question": {
            "id": new_id("q"),
            "kind": "stage_confirm",
            "prompt": (
                f"{artifact}已产出（病例 {case_id}）。{prompt_meta}"
                "请确认产物无误后进入下一阶段。"
            )[:500],
            "options": [
                {"value": "confirm", "label": "确认产物无误，进入下一阶段"},
                {"value": "pause", "label": "暂停就诊（病例保留，稍后仍可确认续行）"},
            ],
            "channel": "convention",
            "blocking": True,
            "default_action": "挂起不执行（流程停在当前阶段，无副作用）",
        },
        "resume_hint": f'{next_tool}(case_id="{case_id}", stage_confirmed=true)',
    }


def ask(
    kind: str,
    prompt: str,
    options: list[tuple[str, str]],
    default_action: str,
    resume_hint: str,
) -> dict:
    """异常/审批类 pending_question（Q3–Q5 等）。prompt 不含仓库原文。"""
    return {
        "status": "pending_question",
        "question": {
            "id": new_id("q"),
            "kind": kind,
            "prompt": prompt[:1500],
            "options": [{"value": value, "label": label} for value, label in options],
            "channel": "convention",
            "blocking": True,
            "default_action": default_action,
        },
        "resume_hint": resume_hint[:300],
    }


@dataclass
class Presented:
    """present 的结果。stop 有值时调用方原样返回，本阶段不执行。"""

    choice: str = ""
    text: str = ""
    stop: dict | None = None


def _answer_model(options: list[str]):
    from typing import Literal

    from pydantic import create_model

    allowed = tuple(dict.fromkeys(options)) or ("confirm", "pause")
    choice_type = Literal.__getitem__(allowed)
    return create_model("GateAnswer", choice=(choice_type, ...), text=(str, ""))


def _copied(payload: dict, *, channel: str, action: str = "") -> dict:
    stopped = dict(payload)
    question = dict(stopped.get("question") or {})
    question["channel"] = channel
    if action:
        question["elicitation_action"] = action
    stopped["question"] = question
    return stopped


def _degraded_stop(payload: dict) -> dict:
    from ..api.capabilities import GATE_LINE, mark_degraded

    mark_degraded()
    stopped = _copied(payload, channel="convention")
    stopped["degraded"] = True
    stopped["degraded_reason"] = GATE_LINE
    return stopped


def _action_of(result) -> str:
    if isinstance(result, dict):
        return str(result.get("action") or "")
    return str(getattr(result, "action", "") or "")


def _field(result, name: str) -> str:
    data = result.get("data") if isinstance(result, dict) else getattr(result, "data", None)
    if data is None:
        return ""
    if isinstance(data, dict):
        return str(data.get(name) or "")
    return str(getattr(data, name, "") or "")


async def present(payload: dict) -> Presented:
    """宿主声明了 elicitation 时当场确认。失败、超时、未声明都退回原来的约定问题。"""
    from ..api.capabilities import current_ctx, elicitation_open, mark_elicited, note_receipt

    if not elicitation_open():
        return Presented(stop=payload)
    ctx = current_ctx()
    options = [item["value"] for item in (payload.get("question") or {}).get("options") or []]
    try:
        schema = _answer_model(options)
        result = await asyncio.wait_for(
            ctx.elicit(payload["question"]["prompt"], schema),
            timeout=_ELICIT_TIMEOUT,
        )
    except Exception:
        return Presented(stop=_degraded_stop(payload))
    action = _action_of(result)
    if action in {"decline", "cancel"}:
        mark_elicited()
        return Presented(stop=_copied(payload, channel="elicitation", action=action))
    if action != "accept":
        return Presented(stop=_degraded_stop(payload))
    choice = _field(result, "choice")
    text = _field(result, "text")
    if choice not in options:
        return Presented(stop=_degraded_stop(payload))
    mark_elicited()
    if choice in _HALT:
        return Presented(choice=choice, text=text, stop=_copied(payload, channel="elicitation", action="accept"))
    note_receipt()
    return Presented(choice=choice, text=text, stop=None)


async def release(payload: dict) -> dict | None:
    """阶段门禁。返回 None 表示确认通过，调用方继续。返回字典则原样交回，不执行。"""
    presented = await present(payload)
    return presented.stop


def handoff(
    case_id: str,
    stage: str,
    auto_advance: bool,
    next_tool: str = "",
) -> dict:
    """阶段交接块（契约 §2.10）：指引宿主呈现产物并按模板等待用户确认。"""
    awaiting = not auto_advance and bool(next_tool)
    return {
        "stage": stage,
        "artifact": "护理记录" if stage.startswith("dose:") else STAGE_ARTIFACTS.get(stage, (stage, ""))[0],
        "awaiting_confirmation": awaiting,
        "next_tool": next_tool,
        "confirm_hint": (
            f'请用户确认产物；确认后调用 {next_tool}(case_id="{case_id}", '
            "stage_confirmed=true) 进入下一阶段。"
        )
        if awaiting
        else "",
    }
