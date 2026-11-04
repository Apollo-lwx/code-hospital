"""宿主能力探测。未声明或 elicit 失败时走约定通道；成功确认后覆盖账记 elicitation。

门禁有过一次实质升级：从「一个布尔参数」变成「一个可核的回执」。
`stage_confirmed` 只能证明宿主**发了这个参数**，证明不了它给用户看过产物。
协议级 elicitation 能证明（用户在这次调用里点的），但依赖宿主声明支持；
不支持时，改核 `ack` —— `handoff.artifact_digest` 是产物内容的摘要，
宿主只有拿到产物才拿得到它，拿到它就等于「这一趟它确实把文件取到手了」。

这仍然证明不了「人看见了」。任何机制都证明不了。它能证明的是
「宿主取到了这一阶段的具体产物并把它带回来了」，而这恰好是可审计的那一半。
"""

from __future__ import annotations

import hashlib
import logging
from contextvars import ContextVar, Token
from functools import wraps

GATE_LINE = "门禁降级为约定级"
EMPTY_GATE = "stage_confirmed 为真，但当前没有待确认的门禁"
NO_RECEIPT = "这次确认没有 elicitation 回执"
ACK_MISSING = "院规要求带回执，但这次放行没有可核的回执"

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


def admit_stage(
    stage_confirmed: bool,
    pending_gate: str,
    ack: str = "",
    expected: str = "",
    project_root: str = "",
) -> dict | None:
    """有门禁又靠参数放行时，核这条路走不走得通。

    返回这次放行的性质，供纪律账记一条：靠参数放行就是 gate_bypass，
    不是错误，但必须可数——靠参数放行的次数本身就是 R1 的证据。

    院规 `gate_receipt` 批下来之后，只认回执：
    - 协议回执（用户在这次调用里点的确认）：直接放行；
    - 没有协议回执就要 `ack` 等于 `expected`（产物的摘要）。
    两者都没有就拒绝——这时 `stage_confirmed=true` 什么也证明不了。
    产物摘要取不到时降级成 `silent_degrade`：说好要核却核不了，必须留账，
    不能悄悄当成核过了。
    """
    gate = pending_gate or ""
    if stage_confirmed and not gate:
        # 门禁本来就是空的，这个参数没有绕开任何东西，不记偏差。
        # 记了会有一条更坏的后果：auto_advance 的病例由医院自己内部推进，
        # 每一轮都会往纪律账里塞一条假的 gate_bypass，而纪律账正是
        # 决定「要不要加严」的唯一输入——自己的流水账不能算成宿主的违规。
        _log.warning(EMPTY_GATE)
        return None
    if not (gate and stage_confirmed):
        return None
    require_receipt(True, "stage_confirmed")
    if has_receipt():
        return None

    from ..catalog.discipline import requires

    if not requires(project_root, "gate_receipt"):
        return {
            "kind": "gate_bypass",
            "gate": gate,
            "detail": f"门禁 {gate} 靠参数放行，没有 elicitation 回执",
        }
    expected = str(expected or "").strip()
    if not expected:
        return {
            "kind": "silent_degrade",
            "gate": gate,
            "detail": f"院规要求回执，但 {gate} 的产物摘要取不到，这次没法核",
        }
    if str(ack or "").strip() != expected:
        from ..core.errors import forbidden

        raise forbidden(
            ACK_MISSING,
            target="ack",
            next_action=(
                f"先把 {gate} 的产物呈现给用户，再把它摘要原样作为 ack 传回；"
                '摘要丢了就 hosp_case(case_id=..., include="summary") 取 gate.artifact_digest'
            ),
        )
    return None


_GATE_ARTIFACT = {
    "registered": ("挂号单", ("register",)),
    "scanned": ("检验报告", ("scan", "scan_supplement")),
    "diagnosed": ("诊断书", ("diagnose",)),
}


def digest_text(text: str) -> str:
    return "g-" + hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:16]


def digest_file(path) -> str:
    """产物摘要。读不到返回空串，由调用方决定怎么说这件事。"""
    from pathlib import Path

    try:
        return digest_text(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


async def artifact_digest(db, case) -> tuple[str, str]:
    """这一阶段要用户确认的那份产物，以及它的摘要。

    摘要取的是**文件内容**，不是路径：回执核的是「你给他看的那一份」。
    检验报告有补片，诊断书有复诊，所以按时间取最新的一份——确认的永远是眼前这份。
    """
    from ..storage.db import hospital_root

    gate = str(getattr(case, "pending_gate", "") or "")
    if not gate:
        return "", ""
    if gate.startswith("dose:"):
        path = (
            hospital_root(case.project_root) / "reports" / case.id
            / f"dispense-r{case.round}.md"
        )
        return "护理记录", digest_file(path)
    name, stages = _GATE_ARTIFACT.get(gate, (gate, ()))
    if not stages:
        return name, ""
    reports = await db.list_reports(case.id)
    live = [item for item in reports if item.stage in stages]
    if not live:
        return name, ""
    latest = max(live, key=lambda item: (int(item.created_at or 0), int(item.round or 0)))
    return name, digest_file(latest.md_path)


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
    """未支持 elicitation 时给出可见降级。每个阶段再挂上宿主辅助位。"""
    from ..core.assist import attach_ai, current_assist, current_root

    if not isinstance(payload, dict):
        return payload
    probe = _probe.get()
    stamped = payload
    if probe and not probe.get("elicitation"):
        stamped = dict(payload)
        stamped["degraded"] = True
        stamped["degraded_reason"] = GATE_LINE
    return attach_ai(stamped, assist=current_assist(), project_root=current_root())


def hosted(fn):
    """科室出口：探测已绑定时给返回值打上降级标记，并收下这次宿主辅助。"""

    @wraps(fn)
    async def wrapper(*args, **kwargs):
        from ..core.assist import assist_scope

        assist = kwargs.pop("assist", None)
        root = str(kwargs.get("project_root") or "")
        with assist_scope(assist, root):
            return finish(await fn(*args, **kwargs))

    return wrapper
