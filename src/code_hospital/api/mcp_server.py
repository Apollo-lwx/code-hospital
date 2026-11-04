"""MCP server（`hosp serve`，stdio 传输，mcp 2.x SDK 的 MCPServer）。

已注册 hosp_register / hosp_scan / hosp_diagnose / hosp_prescribe / hosp_dispense / hosp_discharge / hosp_treat / hosp_case。
错误处理：HospitalError → {"error": {code,message,target,nextAction}} 结构化返回
（契约 §0；isError 语义随 SDK 能力在 P1 收紧）。
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from .capabilities import bind_host, finish, reset_host

from ..core.algorithms import lookup_history
from ..core.errors import HospitalError, internal_error
from ..departments import (
    diagnostician,
    discharge,
    pharmacist,
    prescriber,
    reception,
    scanner,
    treat,
)
from ..storage.db import Database
from ..storage.reports import summarize_scan
from .descriptions import (
    CASE_DESC,
    DIAGNOSE_DESC,
    DISCHARGE_DESC,
    DISPENSE_DESC,
    INSTRUCTIONS,
    MARKET_DESC,
    PRESCRIBE_DESC,
    REGISTER_DESC,
    RULE_DESC,
    SCAN_DESC,
    TREAT_DESC,
)

mcp = MCPServer("code-hospital", instructions=INSTRUCTIONS)


def _guard(payload_or_error):
    """统一错误封装：业务错误 → 契约 Error 对象；未知异常 → InternalError。"""
    if isinstance(payload_or_error, HospitalError):
        return payload_or_error.payload()
    return payload_or_error


@mcp.tool(
    name="hosp_register",
    description=REGISTER_DESC,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
)
async def hosp_register(
    title: str,
    scope_type: str,
    scope: dict | None = None,
    chief_complaint: str = "",
    max_rounds: int = 3,
    force: bool = False,
    auto_advance: bool = False,
    requested_specialties: list[str] | None = None,
    requested_instruments: list[str] | None = None,
    visit_kind: str = "checkup",
    project_root: str = "",
    assist: dict | None = None,
    ctx: Context | None = None,
) -> dict:
    token = bind_host(ctx)
    try:
        return finish(await reception.register(
            title=title,
            scope_type=scope_type,
            scope=scope or {},
            chief_complaint=chief_complaint,
            max_rounds=max_rounds,
            force=force,
            auto_advance=auto_advance,
            requested_specialties=requested_specialties,
            requested_instruments=requested_instruments,
            visit_kind=visit_kind,
            project_root=project_root,
            assist=assist,
        ))
    except HospitalError as e:
        return finish(_guard(e))
    except Exception as e:  # noqa: BLE001
        return finish(internal_error(f"{type(e).__name__}: {e}").payload())
    finally:
        reset_host(token)


@mcp.tool(
    name="hosp_scan",
    description=SCAN_DESC,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False),
)
async def hosp_scan(
    case_id: str,
    tools: list[str] | None = None,
    timeout_ms: int = 120_000,
    stage_confirmed: bool = False,
    ack: str = "",
    page: int = 1,
    size: int = 50,
    order_by: str = "severity desc",
    project_root: str = "",
    assist: dict | None = None,
    ctx: Context | None = None,
) -> dict:
    token = bind_host(ctx)
    try:
        return finish(await scanner.scan(
            case_id=case_id,
            tools=tools,
            timeout_ms=timeout_ms,
            stage_confirmed=stage_confirmed,
            ack=ack,
            page=page,
            size=size,
            order_by=order_by,
            project_root=project_root,
            assist=assist,
        ))
    except HospitalError as e:
        return finish(_guard(e))
    except Exception as e:  # noqa: BLE001
        return finish(internal_error(f"{type(e).__name__}: {e}").payload())
    finally:
        reset_host(token)


@mcp.tool(
    name="hosp_diagnose",
    description=DIAGNOSE_DESC,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False),
)
async def hosp_diagnose(
    case_id: str,
    use_llm: bool = False,
    stage_confirmed: bool = False,
    ack: str = "",
    check_answers: dict | None = None,
    project_root: str = "",
    assist: dict | None = None,
    ctx: Context | None = None,
) -> dict:
    token = bind_host(ctx)
    try:
        return finish(await diagnostician.diagnose(
            case_id=case_id,
            use_llm=use_llm,
            stage_confirmed=stage_confirmed,
            project_root=project_root,
            check_answers=check_answers,
            ack=ack,
            assist=assist,
        ))
    except HospitalError as e:
        return finish(_guard(e))
    except Exception as e:  # noqa: BLE001
        return finish(internal_error(f"{type(e).__name__}: {e}").payload())
    finally:
        reset_host(token)


@mcp.tool(
    name="hosp_prescribe",
    description=PRESCRIBE_DESC,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False),
)
async def hosp_prescribe(
    case_id: str,
    approve: bool = False,
    approver: str = "host_agent",
    user_confirmed: bool = False,
    stage_confirmed: bool = False,
    low_confidence: str = "",
    acceptance_decision: str = "",
    decision: str = "",
    project_root: str = "",
    assist: dict | None = None,
    ctx: Context | None = None,
) -> dict:
    token = bind_host(ctx)
    try:
        return finish(await prescriber.prescribe(
            case_id=case_id,
            approve=approve,
            approver=approver,
            user_confirmed=user_confirmed,
            stage_confirmed=stage_confirmed,
            low_confidence=low_confidence,
            acceptance_decision=acceptance_decision,
            decision=decision,
            project_root=project_root,
            assist=assist,
        ))
    except HospitalError as e:
        return finish(_guard(e))
    except Exception as e:  # noqa: BLE001
        return finish(internal_error(f"{type(e).__name__}: {e}").payload())
    finally:
        reset_host(token)


@mcp.tool(
    name="hosp_dispense",
    description=DISPENSE_DESC,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
)
async def hosp_dispense(
    prescription_id: str,
    item_id: str = "",
    mode: str = "apply",
    patch_token: str = "",
    snapshot_hash: str = "",
    patch: str = "",
    resolution: str = "",
    dry_run: bool = False,
    stage_confirmed: bool = False,
    ack: str = "",
    follow_autofix: bool = False,
    project_root: str = "",
    assist: dict | None = None,
    ctx: Context | None = None,
) -> dict:
    token = bind_host(ctx)
    try:
        return finish(await pharmacist.dispense(
            prescription_id=prescription_id,
            item_id=item_id,
            mode=mode,
            patch_token=patch_token,
            snapshot_hash=snapshot_hash,
            patch=patch,
            resolution=resolution,
            dry_run=dry_run,
            stage_confirmed=stage_confirmed,
            ack=ack,
            follow_autofix=follow_autofix,
            project_root=project_root,
            assist=assist,
        ))
    except HospitalError as e:
        return finish(_guard(e))
    except Exception as e:  # noqa: BLE001
        return finish(internal_error(f"{type(e).__name__}: {e}").payload())
    finally:
        reset_host(token)


@mcp.tool(
    name="hosp_discharge",
    description=DISCHARGE_DESC,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False),
)
async def hosp_discharge(
    case_id: str,
    overrides: list | None = None,
    waive: bool = False,
    waiver_user_confirmed: bool = False,
    waive_reason: str = "",
    distill_to_rsi: bool = False,
    stage_confirmed: bool = False,
    ack: str = "",
    rounds_decision: str = "",
    gate_decision: str = "",
    level: str = "standard",
    project_root: str = "",
    assist: dict | None = None,
    ctx: Context | None = None,
) -> dict:
    token = bind_host(ctx)
    try:
        return finish(await discharge.discharge(
            case_id=case_id,
            overrides=overrides,
            waive=waive,
            waiver_user_confirmed=waiver_user_confirmed,
            waive_reason=waive_reason,
            distill_to_rsi=distill_to_rsi,
            stage_confirmed=stage_confirmed,
            ack=ack,
            rounds_decision=rounds_decision,
            gate_decision=gate_decision,
            level=level,
            project_root=project_root,
            assist=assist,
        ))
    except HospitalError as e:
        return finish(_guard(e))
    except Exception as e:  # noqa: BLE001
        return finish(internal_error(f"{type(e).__name__}: {e}").payload())
    finally:
        reset_host(token)


@mcp.tool(
    name="hosp_treat",
    description=TREAT_DESC,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
)
async def hosp_treat(
    case_id: str = "",
    resume: bool = False,
    auto_approve_low_risk: bool = True,
    stop_on_drift: bool = True,
    title: str = "",
    scope_type: str = "",
    scope: dict | None = None,
    chief_complaint: str = "",
    max_rounds: int = 3,
    force: bool = False,
    requested_specialties: list[str] | None = None,
    requested_instruments: list[str] | None = None,
    visit_kind: str = "checkup",
    project_root: str = "",
    timeout_ms: int = 120_000,
    assist: dict | None = None,
    ctx: Context | None = None,
) -> dict:
    async def _report(amount: float, total: float | None, message: str) -> None:
        if ctx is not None:
            await ctx.report_progress(amount, total, message)

    token = bind_host(ctx)
    try:
        return finish(await treat.treat(
            case_id=case_id,
            resume=resume,
            auto_approve_low_risk=auto_approve_low_risk,
            stop_on_drift=stop_on_drift,
            title=title,
            scope_type=scope_type,
            scope=scope,
            chief_complaint=chief_complaint,
            max_rounds=max_rounds,
            force=force,
            requested_specialties=requested_specialties,
            requested_instruments=requested_instruments,
            visit_kind=visit_kind,
            project_root=project_root,
            timeout_ms=timeout_ms,
            progress=_report,
            assist=assist,
        ))
    except HospitalError as e:
        return finish(_guard(e))
    except Exception as e:  # noqa: BLE001
        return finish(internal_error(f"{type(e).__name__}: {e}").payload())
    finally:
        reset_host(token)


@mcp.tool(
    name="hosp_market",
    description=MARKET_DESC,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
)
async def hosp_market(
    action: str = "list",
    manifest_path: str = "",
    instrument_id: str = "",
    confirm: bool = False,
    project_root: str = "",
    question_id: str = "",
    answer: str = "",
    choice: str = "",
    requisition_id: str = "",
    ctx: Context | None = None,
) -> dict:
    token = bind_host(ctx)
    try:
        return finish(await _market(
            action, manifest_path, instrument_id, confirm, project_root,
            question_id=question_id, answer=answer, choice=choice,
            requisition_id=requisition_id,
        ))
    except HospitalError as e:
        return finish(_guard(e))
    except Exception as e:  # noqa: BLE001
        return finish(internal_error(f"{type(e).__name__}: {e}").payload())
    finally:
        reset_host(token)


async def _market(action: str, manifest_path: str, instrument_id: str,
                  confirm: bool, project_root: str,
                  question_id: str = "", answer: str = "", choice: str = "",
                  requisition_id: str = "") -> dict:
    """能力市场。装仪器不改仓库，装进来一律先跑影子。

    `add` **不是一步装完**：先问人。医院不替客户决定「要不要让第三方读这个仓库」，
    宿主也不能替——所以没有回执的安装一律先返回 pending_question。
    """
    from ..catalog import market
    from ..core.errors import invalid_operation
    from ..departments.reception import resolve_project_root

    root = resolve_project_root(project_root)
    act = (action or "list").strip().lower()
    if act == "list":
        return market.listing(root)
    if act == "verify":
        ids = [instrument_id] if instrument_id else []
        got = market.verify(root, ids)
        got["note"] = (
            "manifest 通过不等于跑得动：命令不在 PATH 上就是「装好了但没到岗」。"
            if any(row["present"] is False for row in got["instruments"])
            else "已装的全部能读到 manifest。"
        )
        return got
    if act == "add":
        if not manifest_path:
            raise invalid_operation(
                "装仪器要给 manifest_path（一份 manifest.yaml）",
                target="manifest_path",
                next_action="写一份 api: code-hospital/v1 的 manifest，再带路径调一次",
            )
        from ..catalog import manifest as mf
        from ..core.questions import present

        doc, problems = mf.load_file(manifest_path)
        if problems:
            raise invalid_operation(
                "manifest 没通过校验：" + "；".join(problems),
                target=str(doc.get("id") or "manifest_path"),
                next_action="按问题清单改 manifest 再装；缺字段就补，不要为绕过校验改 id",
            )
        requisition = await _resolve_requisition(root, requisition_id)
        if requisition:
            # 问之前先查：补不上这条启事的仪器，不能把「它是来补 .kt 的」
            # 写在问题上让人点同意——假前提换来的同意不算同意。
            why = market.requisition_problem(doc, requisition)
            if why:
                raise invalid_operation(
                    why, target="requisition_id",
                    next_action=(
                        "换一台声明覆盖这类文件的仪器，或者这次不带 requisition_id 装；"
                        "但别把它记成已招到——它查不动这一类"
                    ),
                )
        if not question_id:
            # 第一次调 add：不写盘，先问。宿主拿到 pending_question 去问用户，
            # 回答后带上 question_id + choice + answer 再来。票据一次性，用掉即作废。
            asked = _install_question(doc, requisition)
            qid = str(asked["question"]["id"])
            asked["resume_hint"] = (
                f'hosp_market(action="add", manifest_path="{manifest_path}", '
                f'question_id="{qid}", choice="<用户选的那个：install / skip / pause>", '
                f'answer="<用户的原话>")'
            )
            presented = await present(asked)
            if presented.stop:
                market.note_question(root, qid, doc)
                return presented.stop
            if presented.choice != "install":
                return {
                    "ok": False,
                    "id": str(doc.get("id") or ""),
                    "choice": presented.choice,
                    "note": "没装。用户选了暂不安装，这一类文件继续记为未查。",
                }
            consent = {"answer": presented.text or "用户当场确认安装", "via": "elicitation"}
        else:
            ok, why = market.consume_question(root, question_id)
            if not ok:
                raise invalid_operation(
                    why, target="question_id",
                    next_action="先调一次 add（不带 question_id）把问题交给用户，再带回答回来",
                )
            picked = choice.strip().lower()
            if not picked:
                # 只给原话不给选择：医院读不出「暂停」和「装」的分别，也不该去读。
                # 猜错的代价是替用户签了字，所以宁可拒绝一次。
                raise invalid_operation(
                    "带 question_id 回来时还要带 choice，说明用户选了什么",
                    target="choice",
                    next_action='choice="install" 才装；用户选不装或稍后再定，就原样带回 "skip" / "pause"',
                )
            if picked not in ("install", "skip", "pause"):
                raise invalid_operation(
                    f"choice「{picked}」认不出来，只能是 install / skip / pause",
                    target="choice",
                    next_action="把用户的选项原样带回来，不要自己归纳成别的词",
                )
            if picked != "install":
                return {
                    "ok": False,
                    "id": str(doc.get("id") or ""),
                    "choice": picked,
                    "note": "没装。用户选了暂不安装，这一类文件继续记为未查。",
                }
            if not answer.strip():
                raise invalid_operation(
                    "安装要人点头：把用户的原话带回来（answer）",
                    target="answer",
                    next_action="不要自己编一个回答；把问题原样给用户，再把他的选择带回来",
                )
            consent = {"answer": answer, "via": "convention"}
        got = market.install(root, manifest_path, consent=consent, requisition=requisition)
        if not got.get("ok"):
            raise invalid_operation(
                "manifest 没装成：" + "；".join(got.get("problems") or ["未知原因"]),
                target=got.get("id") or "manifest_path",
                next_action="按问题清单改 manifest 再装，不要改 id 去顶替已有仪器",
            )
        return got
    if act == "retire":
        got = market.retire(root, instrument_id)
        if not got.get("ok"):
            raise invalid_operation(
                got.get("note") or "没有这台仪器", target="instrument_id",
            )
        return got
    if act == "restore":
        got = market.restore(root, instrument_id)
        if not got.get("ok"):
            raise invalid_operation(
                got.get("note") or "没有这台仪器的记录", target="instrument_id",
            )
        return got
    if act == "review":
        ids = [instrument_id] if instrument_id else []
        return market.review(root, ids)
    if act == "promote":
        got = market.promote(root, instrument_id, confirm=confirm)
        if not got.get("ok"):
            # 不够格、要签字、已退役——三种都不是参数写错，是状态不允许。
            # 用 invalid_operation 是为了让宿主把 note 原样带回给人看。
            raise invalid_operation(
                got.get("note") or "转正没成",
                target=got.get("id") or "instrument_id",
                next_action=(
                    "够格但没签字：同一次调用带 confirm=true。"
                    if got.get("needs_confirm") else
                    "先按 review 看三个数，缺哪个补哪个；不够格就是不够格，不要绕过。"
                ),
            )
        return got
    raise invalid_operation(
        f"不认识的市场动作：{action}", target="action",
        next_action="action 只能是 list / verify / add / retire / restore / review / promote",
    )


@mcp.tool(
    name="hosp_rule",
    description=RULE_DESC,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
)
async def hosp_rule(
    action: str = "list",
    rule_id: str = "",
    ref: str = "",
    ack: str = "",
    reason: str = "",
    confirm: bool = False,
    project_root: str = "",
    ctx: Context | None = None,
) -> dict:
    token = bind_host(ctx)
    try:
        return finish(await _rule(action, rule_id, ref, ack, reason, confirm, project_root))
    except HospitalError as e:
        return finish(_guard(e))
    except Exception as e:  # noqa: BLE001
        return finish(internal_error(f"{type(e).__name__}: {e}").payload())
    finally:
        reset_host(token)


async def _rule(action: str, rule_id: str, ref: str, ack: str, reason: str,
                confirm: bool, project_root: str) -> dict:
    """院规：摆事实、收批准、执行。

    判断不在这里。要不要加严、加哪条，是记忆层的活——医院没有历史，
    就没有资格说「该加严了」。这里只做三件事：把偏差数出来、
    把能执行的校验摆出来、把人批过的那条翻到开。
    """
    from ..catalog import discipline
    from ..core.errors import invalid_operation
    from ..core.questions import present
    from ..departments.reception import resolve_project_root

    root = resolve_project_root(project_root)
    act = (action or "list").strip().lower()
    if act == "list":
        return await discipline.collect(root)
    if act == "reject":
        raise invalid_operation(
            "不批不需要医院记账",
            target="action",
            next_action=(
                "不批就是不调用 approve，什么都不落盘。"
                "「被拒过几次、什么时候再提」是记忆层的判断，"
                "医院不建这份账——换个地方记就会跟记忆层分叉"
            ),
        )
    if act != "approve":
        raise invalid_operation(
            f"不认识的院规动作：{action}", target="action",
            next_action="action 只能是 list / approve",
        )

    gathered = await discipline.collect(root)
    if gathered["memory"]["channel"] == "absent":
        raise invalid_operation(
            "没有接上记忆基座，不能批准",
            target="ref",
            next_action=(
                "apply_floor 加的是配置里关不掉的校验，关不掉的东西必须能回答「凭什么」。"
                "接上 rsi 再来批；这一轮医院只看看病，不给自己加不可撤销的约束"
            ),
        )

    catalog = {row["id"]: row for row in gathered["checks"]}
    check_id = (rule_id or "").strip()
    if not check_id and len(catalog) == 1:
        check_id = next(iter(catalog))
    check = catalog.get(check_id)
    if check is None:
        raise invalid_operation(
            f"没有这条可执行的校验：{rule_id or '（空）'}",
            target="rule_id",
            next_action=(
                "只能从 list 返回的 checks 里挑一条；"
                "医院执行不了的东西批准了也不会生效，那是纸面规范"
            ),
        )

    ref = (ref or "").strip()
    if not ref:
        raise invalid_operation(
            "批准必须带 ref（记忆层那条经验的 id）",
            target="ref",
            next_action=(
                "先 action=list 看 memory.hits，挑一条作为依据；"
                "记忆里没有相关经验时，说明这件事还不该被固化"
            ),
        )

    approved_in_call = False
    if not confirm:
        # 先让人看一眼再批。有 elicitation 就当场弹卡片，回执由服务端记；
        # 没有就走约定问题，宿主把话带给用户之后再带 confirm 回来。
        presented = await present(_approval_question(check, gathered["deviations"], ref))
        if presented.stop is not None:
            acted = str((presented.stop.get("question") or {}).get("elicitation_action") or "")
            if acted in {"decline", "cancel"}:
                return {"status": "declined", "check": check_id, "note": "没批，什么都没改。"}
            return presented.stop
        if presented.choice != "approve":
            return {"status": "declined", "check": check_id, "note": "没批，什么都没改。"}
        # 当场批的，用户那一下就是回执，不再要 ack——
        # 要求一个「证明你看过」的字段，在已经证明你看过之后就是多余的摩擦。
        approved_in_call = True
    if not approved_in_call:
        want = discipline.digest(check_id, ref)
        if str(ack or "").strip() != want:
            raise invalid_operation(
                "批准凭据对不上",
                target="ack",
                next_action=(
                    f"ack 应为 {want}（绑定 check_id 与 ref）；"
                    "换一条依据就是另一件事，凭据跟着变"
                ),
            )
    from ..api.capabilities import has_receipt

    entry = discipline.approve(
        root, check_id, ref=ref,
        channel="elicitation" if has_receipt() else "convention",
    )
    return {
        "status": "approved",
        "rule": entry,
        "note": (
            f"已批：{entry['check']}。从现在起服务端会执行这条校验，"
            "`code-hospital.yaml` 里关不掉。代价是：" + entry["cost"]
        ),
    }


def _approval_question(check: dict, rows: list[dict], ref: str) -> dict:
    from ..core.questions import ask

    related = [row for row in rows if row["kind"] in check["deviates"]]
    count = sum(int(row["count"]) for row in related)
    cases = {case for row in related for case in (row.get("cases") or [])}
    kinds = "、".join(check["deviates"])
    return ask(
        "rule_approve",
        (
            f"要不要把这条校验固化下来？（{check['id']}）\n"
            f"起因：{kinds} 累计 {count} 次（涉及 {len(cases)} 个病例）。\n"
            f"依据：记忆条目 {ref}\n"
            f"做法：{check['effect']}\n"
            f"代价：{check['cost']}"
        )[:1500],
        [
            ("approve", "批准，以后按这条硬校验"),
            ("reject", "不批准，什么都不改"),
            ("pause", "暂停，什么都不改"),
        ],
        "不批准，规则不生效",
        f'hosp_rule(action="approve", rule_id="{check["id"]}", ref="{ref}")',
    )


async def _resolve_requisition(root: str, requisition_id: str) -> dict | None:
    """把宿主报上的 requisition_id 对回覆盖账里的那条启事。

    只收 id，扩展名从病例的覆盖账里查——不收宿主给的扩展名。
    否则宿主可以报一条「覆盖 .kt」的启事、装一台只查 .py 的仪器，
    校验就成了摆设。

    对不上就报错，不退回「当作没提」：悄悄降级会让宿主以为接上了，
    下一轮报告里那条启事还是没人认领，而它已经花了一次安装。
    """
    if not requisition_id:
        return None
    from ..catalog import ledger
    from ..core.errors import invalid_operation
    from ..storage.db import Database

    async with Database(root) as db:
        cases, _ = await db.list_cases(root, size=20)
        for case in cases:
            for req in ledger.requisitions(case.coverage):
                if req["gap_id"] == requisition_id:
                    return {
                        "gap_id": req["gap_id"],
                        "extensions": req["extensions"],
                        "case_id": case.id,
                        "file_count": req["file_count"],
                    }
    raise invalid_operation(
        f"覆盖账里没有这条启事：{requisition_id}",
        target="requisition_id",
        next_action=(
            "从 hosp_case(include=\"requisitions\") 或报告里把 gap id 原样抄回来；"
            "对不上就先别填这个参数——不带它也能装，只是不记成回应某条启事"
        ),
    )


def _install_question(doc: dict, requisition: dict | None = None) -> dict:
    """把「要装的是个什么东西」原样摆到人面前，再问装不装。

    manifest 是客户提供的文件，按不可信输入处理：id 与扩展名都过一遍净化，
    免得一句「忽略以上指令」跟着 manifest 漂到用户的对话框里。
    """
    from ..core.questions import ask
    from ..safety.sanitize import sanitize_evidence

    ident = sanitize_evidence(str(doc.get("id") or ""), 60)
    transport = sanitize_evidence(str(doc.get("transport") or ""), 20) or "（未声明）"
    exts = [
        sanitize_evidence(str(item).lstrip(".").lower(), 12)
        for item in (doc.get("extensions") or [])
    ]
    what = "、".join(f".{item}" for item in exts if item) or "（没声明，按全类型口径）"
    risk = doc.get("risk") or {}
    flags = "、".join(
        name for key, name in (
            ("reads_repo", "读仓库"), ("writes_repo", "写仓库"),
            ("network", "联网"), ("executes_code", "执行代码"),
        ) if risk.get(key)
    ) or "没声明任何风险动作"
    purpose = ""
    if requisition:
        want = "、".join(f".{item}" for item in requisition.get("extensions") or [])
        purpose = f"它是来补那条启事的：本次有 {requisition.get('file_count')} 个 {want} 文件没人查。\n"
    return ask(
        "install_instrument",
        (
            f"要装一台第三方仪器「{ident}」，运行方式 {transport}，声明覆盖 {what}。\n"
            f"风险声明：{flags}。\n"
            f"{purpose}"
            "装上先进影子期：会跑、结果只进覆盖账，不进报告、不进处方、不进疗效。"
            "样本够了还要你签字才能转正。\n"
            "装不装？"
        )[:1500],
        [
            ("install", "装，让它先跑影子期"),
            ("skip", "先不装，这部分继续记为未查"),
            ("pause", "暂停，稍后再定"),
        ],
        "不装，什么都不发生（不写文件、不写索引）",
        "把用户的选择带回来：choice=\"install\" | \"skip\" | \"pause\"",
    )


@mcp.tool(
    name="hosp_case",
    description=CASE_DESC,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
)
async def hosp_case(
    case_id: str = "",
    status: str = "",
    include: str = "summary",
    report_id: str = "",
    page: int = 1,
    size: int = 50,
    order_by: str = "created_at desc",
    project_root: str = "",
    assist: dict | None = None,
    ctx: Context | None = None,
) -> dict:
    from ..core.assist import assist_scope

    token = bind_host(ctx)
    try:
        with assist_scope(assist, project_root):
            return finish(await _case_query(case_id, status, include, report_id, page, size, order_by, project_root))
    except HospitalError as e:
        return finish(_guard(e))
    except Exception as e:  # noqa: BLE001
        return finish(internal_error(f"{type(e).__name__}: {e}").payload())
    finally:
        reset_host(token)


async def _case_query(
    case_id: str, status: str, include: str, report_id: str,
    page: int, size: int, order_by: str, project_root: str,
) -> dict:
    from ..core.errors import not_found
    from ..core.models import page_payload
    from ..departments.reception import resolve_project_root

    root = resolve_project_root(project_root)
    async with Database(root) as db:
        if not case_id:
            cases, total = await db.list_cases(root, status=status, page=page, size=size)
            data = []
            for c in cases:
                counts = await db.case_counts(c.id)
                data.append({
                    "id": c.id, "title": c.title, "status": c.status,
                    "triage_level": c.triage_level, "round": c.round,
                    "max_rounds": c.max_rounds,
                    "pending_gate": c.pending_gate, "auto_advance": c.auto_advance,
                    "visit_kind": c.visit_kind,
                    "counts": counts,
                    "created_at": c.created_at, "updated_at": c.updated_at,
                })
            return page_payload(data, page, size, order_by, total)

        case = await db.get_case(case_id)
        if case is None:
            raise not_found(f"病例不存在：{case_id}", target="case_id")
        result = {
            "id": case.id, "title": case.title, "status": case.status,
            "triage_level": case.triage_level, "visit_kind": case.visit_kind,
            "scope_type": case.scope_type,
            "scope": case.scope.model_dump(), "round": case.round,
            "max_rounds": case.max_rounds,
            "pending_gate": case.pending_gate, "auto_advance": case.auto_advance,
            "counts": await db.case_counts(case.id),
            "created_at": case.created_at, "updated_at": case.updated_at,
        }
        if case.pending_gate:
            # 门禁块的唯一用途是「摘要丢了还能取回来」。宿主不主动调也行，
            # 但它被院规挡住的时候，这就是它唯一的出路——所以必须能取到。
            from ..api.capabilities import artifact_digest
            from ..catalog.discipline import requires

            artifact, digest = await artifact_digest(db, case)
            result["gate"] = {
                "gate": case.pending_gate,
                "artifact": artifact,
                "artifact_digest": digest,
                "requires_ack": requires(root, "gate_receipt"),
                "note": (
                    "确认产物后带 stage_confirmed=true 重调下一阶段；"
                    "院规要求回执时还要带 ack=artifact_digest。"
                ),
            }
        if include == "findings":
            items, total = await db.list_findings(case_id, page=page, size=size)
            # history_refs 为派生字段不入库，查询时按指纹重挂（语义：截至当前的历史）
            from ..core.models import counts_toward_efficacy

            durable = [f for f in items if counts_toward_efficacy(f.concluded_by)]
            history = await db.find_fingerprint_history(
                root,
                sorted({f.fingerprint for f in durable if f.fingerprint}),
                exclude_case_id=case_id,
                content_keys=sorted({f.content_key for f in durable if f.content_key}),
            )
            for f in items:
                if not counts_toward_efficacy(f.concluded_by):
                    f.history_refs = []
                    continue
                f.history_refs = [
                    {**r, "note": "此前病例曾出现同形态发现（复发）"}
                    for r in lookup_history(history, f.fingerprint, f.content_key)
                ]
            result["findings"] = page_payload(
                [f.model_dump() for f in items], page, size, order_by, total
            )
        elif include == "reports":
            reports = await db.list_reports(case_id)
            result["reports"] = [
                {**r.model_dump(), "content": _read_report(r.md_path) if report_id == r.id else ""}
                for r in reports
                if not report_id or r.id == report_id
            ]
        elif include == "requisitions":
            # 招聘启事的机器可读版本。报告里那句话是给人看的，这个块是给宿主接的：
            # 装仪器时要报 `requisition_id`，值只能从这儿（或报告）原样抄，不能自己编。
            from ..catalog import ledger as _ledger
            from ..catalog.market import claimed_by as _claimed_by

            rows = []
            for req in _ledger.requisitions(case.coverage):
                rows.append({
                    **req,
                    "claimed_by": _claimed_by(root, req["gap_id"]),
                    "install_hint": (
                        f'hosp_market(action="add", manifest_path="<manifest>", '
                        f'requisition_id="{req["gap_id"]}")'
                    ),
                })
            result["requisitions"] = rows
        return result


def _read_report(md_path: str) -> str:
    from pathlib import Path

    p = Path(md_path)
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def serve() -> None:
    mcp.run()


__all__ = ["mcp", "serve", "summarize_scan"]
