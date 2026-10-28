"""宿主能力探测。未声明或 elicit 失败时走约定通道；成功确认后覆盖账记 elicitation。"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from functools import wraps

GATE_LINE = "门禁降级为约定级"
EMPTY_GATE = "stage_confirmed 为真，但当前没有待确认的门禁"
NO_RECEIPT = "这次确认没有 elicitation 回执"

_log = logging.getLogger("code_hospital.gate")

_probe: ContextVar[dict | None] = ContextVar("code_hospital_host_probe", default=None)


def supports_elicitation(ctx) -> bool:
    """宿主声明了 elicitation 才算支持。读不到能力时按未支持。"""
    if ctx is None:
        return False
    try:
        caps = ctx.client_capabilities
    except Exception:
        return False
    if caps is None:
        return False
    if isinstance(caps, dict):
        return caps.get("elicitation") is not None
    return getattr(caps, "elicitation", None) is not None


def protocol_version(ctx) -> str:
    if ctx is None:
        return ""
    try:
        return str(getattr(ctx, "protocol_version", "") or "")
    except Exception:
        return ""


def bind_host(ctx) -> Token:
    """记下这一次工具调用看见的宿主能力。ctx 为空也记成未支持。"""
    return _probe.set({
        "elicitation": supports_elicitation(ctx),
        "protocol_version": protocol_version(ctx),
        "ctx": ctx,
        "degraded": False,
        "used_elicitation": False,
    })


def current_ctx():
    """这一次调用绑定的宿主上下文。没探测时为空。"""
    probe = _probe.get()
    if not probe:
        return None
    return probe.get("ctx")


def elicitation_open() -> bool:
    """宿主声明了 elicitation，且这次还没因失败降级。"""
    probe = _probe.get()
    return bool(
        probe
        and probe.get("elicitation")
        and probe.get("ctx") is not None
        and not probe.get("degraded")
    )


def mark_elicited() -> None:
    """这次调用里 elicit 已经走通。"""
    probe = _probe.get()
    if probe is not None:
        probe["used_elicitation"] = True


def mark_degraded() -> None:
    """elicit 失败或超时。之后按未支持处理，出参和覆盖账都标降级。"""
    probe = _probe.get()
    if probe is None:
        return
    probe["degraded"] = True
    probe["elicitation"] = False


def note_receipt() -> None:
    """这次调用里 elicit 已经 accept，并且选项允许继续。"""
    probe = _probe.get()
    if probe is not None:
        probe["receipt"] = True


def has_receipt() -> bool:
    probe = _probe.get()
    return bool(probe and probe.get("receipt"))


def require_receipt(claimed: bool, target: str) -> None:
    """声明了 elicitation 时，确认参数本身不是回执。未声明时沿用约定通道。"""
    if not claimed or not elicitation_open() or has_receipt():
        return
    from ..core.errors import forbidden

    raise forbidden(
        NO_RECEIPT,
        target=target,
        next_action="不要传确认参数，让本次调用弹出确认；未声明 elicitation 时才用约定参数",
    )


def admit_stage(stage_confirmed: bool, pending_gate: str) -> None:
    """有门禁又靠参数放行时，声明了 elicitation 就拒绝。门禁为空只记一条日志。"""
    gate = pending_gate or ""
    if stage_confirmed and not gate:
        _log.warning(EMPTY_GATE)
        return
    if gate and stage_confirmed:
        require_receipt(True, "stage_confirmed")


def reset_host(token: Token) -> None:
    _probe.reset(token)


def gate_limitation() -> str:
    """未支持 elicitation 时覆盖账要出现的那一句。支持或尚未探测时为空。"""
    probe = _probe.get()
    if not probe or probe.get("elicitation"):
        return ""
    return GATE_LINE


def remember_capabilities(ledger: dict | None, prior: dict | None = None) -> dict:
    """覆盖账记下这次探测。没有新探测时沿用上一份。"""
    out = dict(ledger or {})
    probe = _probe.get()
    if probe is None:
        previous = (prior or {}).get("host_capabilities")
        if previous:
            out["host_capabilities"] = previous
        return out
    used = bool(probe.get("used_elicitation")) and not probe.get("degraded")
    out["host_capabilities"] = {
        "elicitation": bool(probe.get("elicitation")),
        "protocol_version": probe.get("protocol_version") or "",
        "channel": "elicitation" if used else "convention",
    }
    return out


def finish(payload):
    """未支持 elicitation 时给出可见降级。支持或尚未探测时原样返回。"""
    if not isinstance(payload, dict):
        return payload
    probe = _probe.get()
    if not probe or probe.get("elicitation"):
        return payload
    stamped = dict(payload)
    stamped["degraded"] = True
    stamped["degraded_reason"] = GATE_LINE
    return stamped


def hosted(fn):
    """科室出口：探测已绑定时给返回值打上降级标记。"""

    @wraps(fn)
    async def wrapper(*args, **kwargs):
        return finish(await fn(*args, **kwargs))

    return wrapper
