"""用户介入出参变体（契约 §2.9）与阶段交接块（§2.10）的构造器。

pending_question 是**出参变体而非错误**：工具返回它时不执行本阶段逻辑，
流程挂起等待用户决策；回答经既有工具的确认参数回流，不为问题单开工具。
"""

from __future__ import annotations

from .ids import new_id

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
            "prompt": prompt[:500],
            "options": [{"value": value, "label": label} for value, label in options],
            "channel": "convention",
            "blocking": True,
            "default_action": default_action,
        },
        "resume_hint": resume_hint[:300],
    }


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
