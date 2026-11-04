"""三本账：能力缺口、纪律偏差、记忆引用。

只记事实，不记评价。三本都挂在覆盖账（coverage_json）下，不改表结构。
静默降级是这一层最大的敌人：匹配不到、交卷不合契约、补丁越界，
都必须留下一条账，否则「我不会」会被写成「不需要」。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

GAP_KINDS = ("missing_instrument", "missing_checklist", "missing_formula", "unread_by_model")
DEVIATION_KINDS = (
    "gate_bypass",
    "filing_invalid",
    "unsupported_clear",
    "unfounded_claim",
    "scope_creep",
    "silent_degrade",
)
STATUS = ("open", "nominated", "filled", "waived")
_GAP_CAP = 40
_DEVIATION_CAP = 30
# 纪律账在报告里说到多细。客户可配——不同安全等级的仓库，注意力分配不一样。
# 最粗只能到 summary：账出现就说明宿主没按约定交卷，一个字不提等于替它瞒下来。
EXPOSURES = ("summary", "typed", "located", "full")
_DEFAULT_EXPOSURE = "typed"
_FILE_CAP = 6
_CASE_CAP = 12

_GAP_PLAIN = {
    "missing_instrument": "没有在岗仪器能查这一类",
    "missing_checklist": "没有对应的检查清单",
    "missing_formula": "已检出，但药柜没有对应的治法",
    "unread_by_model": "文档没有人读过",
}
# 缺口按「谁能关掉它」分流：仪器缺口走市场招人，阅读缺口只能靠模型读。
# 混在一起，招聘信号会被「招一台 Markdown 检查器」污染。
_GAP_CLOSURE = {
    "missing_instrument": "market",
    "missing_checklist": "market",
    "missing_formula": "formulary",
    "unread_by_model": "host_llm",
}
_DEVIATION_PLAIN = {
    "gate_bypass": "阶段放行只靠参数",
    "filing_invalid": "交卷不合契约",
    "unsupported_clear": "写了「检查通过」类断言",
    "unfounded_claim": "抄的原文在文件里找不到",
    "scope_creep": "改动越出处方范围",
    "silent_degrade": "缺能力却没写限制项",
}
_MEMORY_KEYS = (
    "channel", "owner", "recalled", "injected", "withheld", "feedback",
    "token", "recurred", "suspects", "downgrade", "note",
)
_EVIDENCE_KEYS = ("total", "anchored", "located", "ungrounded", "clue", "note")
_SHADOW_KEYS = ("instrument", "tier", "ran", "findings", "overlap", "samples", "note")


def blank_shadow() -> dict:
    """影子观测账的空结构。

    影子期仪器的发现**不入 findings 表**，只在这里留位置键与条数。
    这样它们天然进不了报告、处方、疗效、差分——不是靠过滤，是靠不存在。
    """
    return {"observed": []}


def note_shadow(coverage: dict, rows: list[dict]) -> dict:
    """整段替换，不累加。影子观测反映的是「这一轮看到了什么」。

    累加会把三轮前的老结论留在这轮账上，转正判据就会拿过期样本做决定。
    """
    out = ensure(coverage)
    out["shadow"] = {"observed": [row for row in rows or [] if isinstance(row, dict)]}
    return out


def blank_memory(channel: str = "absent", owner: str = "hospital") -> dict:
    """记忆引用账的空结构。本轮有没有召回、有没有反馈，都要能被看见。

    `token` 是反馈凭据（recall 给的），只用于出院时回报，不进报告正文。
    `recurred` / `suspects` 是复发归因的结果，回答「这次复发牵连了哪几条经验」。
    """
    return {
        "channel": channel,
        "owner": owner,
        "recalled": [],
        "injected": 0,
        "withheld": 0,
        "feedback": "absent",
        "token": "",
        "recurred": 0,
        "suspects": [],
        "downgrade": [],
        "note": "",
    }


def blank_evidence() -> dict:
    """证据账的空结构。四档计数，回答「几条模型结论真的可核」。"""
    return {
        "total": 0,
        "anchored": 0,
        "located": 0,
        "ungrounded": 0,
        "clue": 0,
        "note": "",
    }


def note_evidence(coverage: dict, book: dict) -> dict:
    """把本轮证据账挂到覆盖账。答案不覆盖旧账——跨轮就诊要累加。"""
    out = ensure(coverage)
    merged = dict(out["evidence"])
    for key in ("total", "anchored", "located", "ungrounded", "clue"):
        merged[key] = int(merged.get(key) or 0) + int((book or {}).get(key) or 0)
    if (book or {}).get("note"):
        merged["note"] = str(book["note"])[:300]
    out["evidence"] = merged
    return out


def ensure(coverage: dict | None) -> dict:
    """就地补齐三本账的空结构。已有内容原样保留。"""
    out = coverage if isinstance(coverage, dict) else {}
    if not isinstance(out.get("capability_gap"), list):
        out["capability_gap"] = []
    if not isinstance(out.get("discipline"), list):
        out["discipline"] = []
    memory = out.get("memory")
    if not isinstance(memory, dict):
        memory = blank_memory()
    else:
        merged = blank_memory(str(memory.get("channel") or "absent"), str(memory.get("owner") or "hospital"))
        merged.update({key: memory[key] for key in _MEMORY_KEYS if key in memory})
        memory = merged
    out["memory"] = memory
    evidence = out.get("evidence")
    if not isinstance(evidence, dict):
        evidence = blank_evidence()
    else:
        merged_evidence = blank_evidence()
        merged_evidence.update({key: evidence[key] for key in _EVIDENCE_KEYS if key in evidence})
        evidence = merged_evidence
    out["evidence"] = evidence
    shadow = out.get("shadow")
    if not isinstance(shadow, dict) or not isinstance(shadow.get("observed"), list):
        shadow = blank_shadow()
    out["shadow"] = shadow
    return out


def note_memory(coverage: dict, *, channel: str = "", owner: str = "", **fields) -> dict:
    """记一条记忆引用。知识本体归 rsi，医院只记引用与反馈状态。"""
    out = ensure(coverage)
    memory = out["memory"]
    if channel:
        memory["channel"] = channel
    if owner:
        memory["owner"] = owner
    for key, value in fields.items():
        if key in _MEMORY_KEYS:
            memory[key] = value
    return out


def _gap_id(kind: str, signals: list[str]) -> str:
    """缺口按「哪一类问题」归并，不按扩展名拆开。

    同一条规则命中 ts 和 tsx，是同一个能力缺口；招聘时招的是一台仪器，
    不是给每种扩展名各招一台。
    """
    key = "|".join([kind, ",".join(sorted(signals))])
    return "gap-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def record_gap(
    coverage: dict,
    kind: str,
    *,
    signals: list[str] | None = None,
    files: list[str] | None = None,
    extensions: list[str] | None = None,
    raised_by: str = "code",
    case_id: str = "",
    note: str = "",
    count: int = 1,
    extra: dict | None = None,
) -> dict:
    """记一条能力缺口。同一形态的缺口合并计数，不重复堆。"""
    if kind not in GAP_KINDS:
        raise ValueError(f"未知缺口类型：{kind}")
    out = ensure(coverage)
    signals = [str(item) for item in (signals or []) if str(item).strip()][:6]
    extensions = [str(item).lstrip(".").lower() for item in (extensions or []) if str(item).strip()][:6]
    files = [str(item).replace("\\", "/") for item in (files or []) if str(item).strip()]
    gap_id = _gap_id(kind, signals)

    rows = out["capability_gap"]
    for row in rows:
        if row.get("id") != gap_id:
            continue
        row["occurrences"] = int(row.get("occurrences") or 0) + max(1, int(count))
        evidence = row.setdefault("evidence", {})
        _extend(evidence, "files", files, _FILE_CAP)
        _extend(evidence, "extensions", extensions, 6)
        _extend(row, "raised_in", [case_id] if case_id else [], _CASE_CAP)
        if note and note not in str(row.get("note") or ""):
            row["note"] = (str(row.get("note") or "") + " " + note).strip()[:300]
        return out

    row = {
        "id": gap_id,
        "kind": kind,
        "evidence": {
            "files": files[:_FILE_CAP],
            "extensions": extensions,
            "signals": signals,
            **{key: value for key, value in (extra or {}).items() if value not in (None, "", [])},
        },
        "raised_by": raised_by,
        "raised_in": [case_id] if case_id else [],
        "occurrences": max(1, int(count)),
        "status": "open",
        "note": note[:300],
    }
    rows.append(row)
    if len(rows) > _GAP_CAP:
        rows.sort(key=lambda item: int(item.get("occurrences") or 0), reverse=True)
        del rows[_GAP_CAP:]
    return out


def record_deviation(
    coverage: dict,
    kind: str,
    *,
    stage: str = "",
    case_id: str = "",
    round_no: int = 0,
    detail: str = "",
    count: int = 1,
) -> dict:
    """记一条执行偏差。同一阶段同一类型合并计数，只留最近一次的说明。"""
    if kind not in DEVIATION_KINDS:
        raise ValueError(f"未知偏差类型：{kind}")
    out = ensure(coverage)
    rows = out["discipline"]
    for row in rows:
        if row.get("kind") != kind or row.get("stage") != stage:
            continue
        row["count"] = int(row.get("count") or 0) + max(1, int(count))
        row["case_id"] = case_id or row.get("case_id") or ""
        row["round"] = int(round_no or row.get("round") or 0)
        if detail:
            row["detail"] = str(detail)[:300]
        return out
    rows.append({
        "kind": kind,
        "stage": stage,
        "case_id": case_id,
        "round": int(round_no or 0),
        "count": max(1, int(count)),
        "detail": str(detail)[:300],
    })
    if len(rows) > _DEVIATION_CAP:
        rows.sort(key=lambda item: int(item.get("count") or 0), reverse=True)
        del rows[_DEVIATION_CAP:]
    return out


_STYLE = frozenset({"nit", "info"})


def record_unmatched(coverage: dict, diagnosis, members: list, case_id: str = "") -> dict:
    """药柜对不上的一条诊断，记成 missing_formula。

    nit 和 info 是风格与参考信息，本来就按「不改」处理，不算能力缺口——
    把它们记进缺口账，招聘信号会被「查空格的仪器」淹没。
    被 verdict 挡下的诊断（待验证、已排除）同样不算：药有，只是先不动。
    """
    from .formulary import opening_verdict
    if getattr(diagnosis, "formulary_matched", True):
        return ensure(coverage)
    severity = str(getattr(diagnosis, "severity", "") or "")
    if severity in _STYLE:
        return ensure(coverage)
    if str(getattr(diagnosis, "concluded_by", "") or "") == "host_llm":
        # 引文没核上的模型结论连「已检出」都谈不上。记成缺口，招聘会去买一台
        # 处理「模型说了一句没法核的话」的仪器——那不是需求，是噪音。
        rows = list(members or [])
        if not rows or not all(getattr(item, "evidence_tier", "") == "anchored" for item in rows):
            return ensure(coverage)
    if opening_verdict(members):
        # 待验证和已排除是医院的策略性不开方：药有，只是先不动。
        # 记成能力缺口会把「先不开方」说成「不会治」。
        return ensure(coverage)
    members = list(members or [])
    signal = str(getattr(diagnosis, "category", "") or "")
    if not signal and members:
        signal = str(getattr(members[0], "rule_id", "") or "")
    tool = str(getattr(members[0], "tool", "") or "") if members else ""
    signals = [item for item in (signal, tool) if item]
    files = [str(getattr(item, "file_path", "") or "") for item in members]
    extensions = sorted({Path(path).suffix.lstrip(".").lower() for path in files if path})
    return record_gap(
        coverage,
        "missing_formula",
        signals=signals,
        files=files,
        extensions=extensions,
        raised_by=str(getattr(diagnosis, "concluded_by", "") or "code"),
        case_id=case_id,
        note=f"诊断 {getattr(diagnosis, 'id', '')} 没有对上现有药，进待复核。",
        extra={"severity": severity, "methods": [str(getattr(diagnosis, "fixability", "") or "")]},
    )


def record_scope_gaps(
    coverage: dict,
    files: list[str],
    devices: list[dict],
    case_id: str = "",
    project_root: str = "",
    read: set[str] | None = None,
) -> list[str]:
    """记录本轮两类覆盖缺口，返回要写进报告限制项的话。

    - `missing_instrument`：code 类扩展名没有在岗仪器（可数、可招人关掉）
    - `unread_by_model`：文档没有任何模型阅读（只能靠读来关掉，不许招个 linter 冒充）

    `.xml` 按内容归类（SQL 映射算代码，构建/运行时配置算配置），归不出来的按代码处理。
    配置类（yaml/json/pom）不报缺口：它们不是逐行审的对象，报出来只会淹掉真缺口。
    """
    from ..safety.repo_files import KIND_CODE, KIND_DOC, KIND_MARKUP, classify

    ensure(coverage)
    read_set = {str(item).replace("\\", "/").lstrip("./") for item in (read or [])}
    kinds = {path: classify(project_root, path) for path in files}
    code_files = [path for path in files if kinds[path][0] == KIND_CODE]
    read_only = [
        path for path in files if kinds[path][0] in (KIND_DOC, KIND_MARKUP)
    ]
    notes: list[str] = []

    notes.extend(_record_instrument_gap(
        coverage, code_files, devices, case_id, project_root, read_set,
    ))
    notes.extend(_record_reading_gap(coverage, read_only, case_id, read_set, kinds))
    return notes


def _record_instrument_gap(
    coverage: dict,
    code_files: list[str],
    devices: list[dict],
    case_id: str,
    project_root: str,
    read_set: set[str],
) -> list[str]:
    from .formulary import books_for
    from .market import retired_ids

    if not code_files:
        return []
    # 按扩展名分组，一个扩展名一条缺口。合并成一条会让 `_gap_id` 里的 signals
    # 变成"本轮缺的全部扩展名"，补上一类，id 就变成另一个——那条启事再也追不住，
    # 而且"22 个 .kt/.rb/.sh/.sql 文件"没人能凭它买一台仪器：一台仪器查不了四门语言。
    by_ext: dict[str, list[str]] = {}
    for path in code_files:
        ext = Path(path).suffix.lstrip(".").lower()
        if ext:
            by_ext.setdefault(ext, []).append(path)
    if not by_ext:
        return []
    suffixes = set(by_ext)
    catalog = books_for(project_root)[2]
    covered: set[str] = set()
    for row in devices or []:
        if not (row.get("available") and row.get("outcome") == "ok"):
            continue
        # 设备行自己声明了扩展名就用它——市场装的仪器不在随包清册里，
        # 只看清册会把"我不认识这台"写成"它什么都查"，于是所有缺口都不报了。
        declared = row.get("extensions")
        if declared is None:
            declared = (catalog.get(str(row.get("tool") or "")) or {}).get("extensions")
        if declared is None:
            continue
        exts = [str(item).lstrip(".").lower() for item in declared]
        if not exts:
            return []  # 没声明扩展名的仪器按全类型口径，不据此报缺口
        covered.update(exts)

    # 退役的仪器不跑，所以它覆盖的类型也不在 covered 里。但那是客户主动不要的，
    # 不是「缺」——记成缺口就会把它写进招聘启事，等于建议客户把刚退掉的东西装回来。
    # 只豁免它**声明过**的扩展名：没声明扩展名的按全类型口径，退了也不许顶掉所有类型。
    retired_exts: set[str] = set()
    for ident in retired_ids(project_root):
        row = catalog.get(ident) or {}
        retired_exts.update(
            str(item).lstrip(".").lower() for item in (row.get("extensions") or [])
        )
    notes: list[str] = []
    gone_hit = sorted((suffixes - covered) & retired_exts)
    if gone_hit:
        gone_text = "、".join(f".{item}" for item in gone_hit)
        notes.append(
            f"本次范围含 {gone_text} 文件，覆盖它的仪器已按客户要求退役，这一类本轮没有仪器复核。"
        )

    missing = sorted(suffixes - covered - retired_exts)
    if not missing:
        return notes

    for ext in missing:
        files = by_ext[ext]
        read_hit = [path for path in files if path in read_set]
        record_gap(
            coverage,
            "missing_instrument",
            signals=[f"*.{ext}"],
            files=files[:6],
            extensions=[ext],
            raised_by="coverage",
            case_id=case_id,
            note=f"没有在岗仪器覆盖 .{ext}。",
            count=len(files),
            extra={"model_read": len(read_hit)},
        )
        if len(read_hit) >= len(files):
            notes.append(
                f"本次范围含 {len(files)} 个 .{ext} 文件，没有在岗仪器覆盖；"
                f"模型已读其中 {len(read_hit)} 个，结论只能担保到「读过、没有仪器复核」。"
            )
            continue
        tail = f"其中 {len(read_hit)} 个模型已读，" if read_hit else ""
        notes.append(
            f"本次范围含 {len(files)} 个 .{ext} 文件，没有在岗仪器覆盖，{tail}其余记为未查。"
        )
    return notes


def _record_reading_gap(
    coverage: dict,
    files: list[str],
    case_id: str,
    read_set: set[str],
    kinds: dict[str, tuple[str, str]],
) -> list[str]:
    """文档和判不出结构的 XML 没人读，就是未查。

    它们不报「没有仪器覆盖」——那会招来一台 Markdown 检查器、一台 XML 检查器，
    而这类文件的正确关法只有一条：读。所以缺口带 `closure: host_llm`。
    """
    from ..safety.repo_files import KIND_DOC, KIND_MARKUP

    unread = [path for path in files if path not in read_set]
    if not unread:
        return []
    groups: dict[str, list[str]] = {KIND_DOC: [], KIND_MARKUP: []}
    for path in unread:
        kind = (kinds.get(path) or ("", ""))[0]
        groups.setdefault(kind or KIND_DOC, []).append(path)

    notes: list[str] = []
    for kind, bucket in groups.items():
        if not bucket:
            continue
        extensions = sorted({Path(path).suffix.lstrip(".").lower() for path in bucket} - {""})
        record_gap(
            coverage,
            "unread_by_model",
            signals=[f"*.{item}" for item in extensions] or ["read_only"],
            files=bucket[:6],
            extensions=extensions,
            raised_by="coverage",
            case_id=case_id,
            note=(
                "文档里写的是项目规范与流程，本轮没有模型阅读。"
                if kind == KIND_DOC else
                "这些 XML 判不出结构，写的是运行时行为与装配，本轮没有模型阅读。"
            ),
            count=len(bucket),
            extra={"closure": _GAP_CLOSURE["unread_by_model"], "read_group": kind},
        )
        exts = "、".join(f".{item}" for item in extensions)
        if kind == KIND_DOC:
            notes.append(
                f"本次范围含 {len(bucket)} 份文档（{exts}），本轮没有模型阅读，"
                "其中的项目规范与流程记为未查。"
            )
        else:
            notes.append(
                f"本次范围含 {len(bucket)} 份 XML（{exts}），判不出是构建还是 SQL 映射，"
                "只能靠阅读判断，本轮没有模型阅读，记为未查。"
            )
    return notes


def refresh_reading_gaps(
    coverage: dict,
    files: list[str],
    read,
    project_root: str = "",
    case_id: str = "",
) -> list[str]:
    """按最新的已读重算阅读缺口。

    阅读缺口的关闭条件就是「读」。只在下一轮检验才消账，会让同一份报告一边写着
    「引文已核」，一边写着「文档没有模型读过」——两句话都真，但读者只会认为医院没看。
    所以诊断收下交卷之后，当场把这一笔兑现。
    """
    from ..safety.repo_files import KIND_DOC, KIND_MARKUP, classify

    out = ensure(coverage)
    out["capability_gap"] = [
        row for row in out["capability_gap"] if row.get("kind") != "unread_by_model"
    ]
    read_set = {str(item).replace("\\", "/").lstrip("./") for item in (read or [])}
    kinds = {path: classify(project_root, path) for path in files}
    readable = [path for path in files if kinds[path][0] in (KIND_DOC, KIND_MARKUP)]
    return _record_reading_gap(out, readable, case_id, read_set, kinds)


def rows(coverage: dict | None, key: str) -> list[dict]:
    value = (coverage or {}).get(key)
    return list(value) if isinstance(value, list) else []


def _gap_plain(row: dict) -> str:
    """同一种缺口可能有两种性质（文档 / 判不出结构的 XML），标签要分开。"""
    from ..safety.repo_files import KIND_MARKUP

    kind = str(row.get("kind") or "")
    if kind == "unread_by_model" and (row.get("evidence") or {}).get("read_group") == KIND_MARKUP:
        return "判不出结构的 XML 没有人读过"
    return _GAP_PLAIN.get(kind, "能力缺口")


def gap_lines(coverage: dict | None, limit: int = 5) -> list[str]:
    """报告用的缺口说明。没有缺口时为空，不写「无」。"""
    found = rows(coverage, "capability_gap")
    if not found:
        return []
    ranked = sorted(found, key=lambda item: int(item.get("occurrences") or 0), reverse=True)
    lines = []
    for row in ranked[:limit]:
        kind = str(row.get("kind") or "")
        plain = _gap_plain(row)
        signals = list((row.get("evidence") or {}).get("signals") or [])
        body = "、".join(signals[:3]) if signals else "未标注范围"
        count = int(row.get("occurrences") or 1)
        if kind == "missing_formula":
            tail = f"已检出 {count} 处，进待复核，不开方。"
        elif kind == "unread_by_model":
            tail = f"共 {count} 份，没有模型读过，记为未查，不记为没有问题。"
        elif kind == "missing_instrument":
            read_n = int((row.get("evidence") or {}).get("model_read") or 0)
            tail = (
                f"共 {count} 个，模型已读 {read_n} 个但没有仪器复核，只能担保到「读过」。"
                if read_n >= count else
                f"共 {count} 个，记为未查，不记为没有问题。"
            )
        else:
            tail = f"累计 {count} 处，记为未查，不记为没有问题。"
        lines.append(f"能力缺口（{plain}）：{body}，{tail}")
    if len(ranked) > limit:
        lines.append(f"另有 {len(ranked) - limit} 条能力缺口记在覆盖账里。")
    return lines


def exposure_for(project_root: str = "", override: str = "") -> tuple[str, str]:
    """读 `discipline.exposure`。返回 (档位, 要写进报告的一句提醒)。

    写错了不阻止看病，但也不能悄悄按默认出——那等于用我们的默认值
    替客户做了他没做的决定。提醒一句，让他看得见配置没生效。
    """
    value = str(override or "").strip()
    if not value:
        value = _read_exposure_config(project_root)
    if not value:
        return _DEFAULT_EXPOSURE, ""
    if value in EXPOSURES:
        return value, ""
    return _DEFAULT_EXPOSURE, (
        f"discipline.exposure 的取值「{value}」不可用，按 {_DEFAULT_EXPOSURE} 出"
        f"（可写 {' / '.join(EXPOSURES)}）。没有关闭这一档："
        "账出现就说明宿主没按约定交卷，报告里必须留一句。"
    )


def _read_exposure_config(project_root: str) -> str:
    if not project_root:
        return ""
    from ..safety.repo_files import PROJECT_CONFIG

    path = Path(project_root) / PROJECT_CONFIG
    if not path.is_file():
        return ""
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    block = doc.get("discipline")
    if not isinstance(block, dict):
        return ""
    raw = block.get("exposure")
    if raw is None:
        return ""
    if isinstance(raw, bool):
        # YAML 把 on / off / yes / no 解析成布尔。原样返回词面，
        # 让上层当「不可用」报出来——写成 off 却被静默当成没配，是最坏的一种失效。
        return "on" if raw else "off"
    return str(raw).strip()


def deviation_lines(
    coverage: dict | None,
    limit: int = 3,
    project_root: str = "",
    exposure: str = "",
) -> list[str]:
    """报告用的偏差说明。

    说到多细由 `code-hospital.yaml` 的 `discipline.exposure` 决定：
    `summary`（一句话总数）/ `typed`（类型+阶段+次数，默认）/
    `located`（再加病例与轮次）/ `full`（再加说明原文）。

    粗细是客户的注意力分配，不是医院的判断——同一份账在不同安全等级的仓库里
    该说多少，只有客户知道。但**最粗也只能到一句话**：账出现了就说明宿主没按
    约定交卷，报告里一个字不提等于医院替它瞒了下来，那正是 `silent_degrade`
    这条偏差本身要抓的事。
    """
    found = rows(coverage, "discipline")
    if not found:
        return []
    level, warn = exposure_for(project_root, exposure)
    ranked = sorted(found, key=lambda item: int(item.get("count") or 0), reverse=True)
    lines = [warn] if warn else []

    if level == "summary":
        total = sum(int(item.get("count") or 1) for item in ranked)
        return lines + [f"执行偏差：本次记到 {total} 处，明细在覆盖账里。"]

    if level == "full":
        limit = max(limit, 30)
    for row in ranked[:limit]:
        plain = _DEVIATION_PLAIN.get(str(row.get("kind")), "执行偏差")
        stage = str(row.get("stage") or "")
        where = f"，在 {stage}" if stage else ""
        line = f"执行偏差：{plain}{where}，{int(row.get('count') or 1)} 次"
        if level in {"located", "full"}:
            case_id = str(row.get("case_id") or "")
            round_no = int(row.get("round") or 0)
            if case_id or round_no:
                tail = "，".join(
                    item for item in (
                        f"病例 {case_id}" if case_id else "",
                        f"第 {round_no} 轮" if round_no else "",
                    ) if item
                )
                line += f"（{tail}）"
        if level == "full" and row.get("detail"):
            line += f"；说明：{str(row['detail'])[:120]}"
        lines.append(line + "。")
    if len(ranked) > limit:
        lines.append(f"另有 {len(ranked) - limit} 类执行偏差记在覆盖账里。")
    return lines


def ledger_lines(coverage: dict | None, project_root: str = "") -> list[str]:
    return (
        gap_lines(coverage)
        + deviation_lines(coverage, project_root=project_root)
        + memory_lines(coverage)
    )


# 一个缺口要累积到几处才值得发招聘启事。
# 1 个 .java 文件可能是误放进来的一颗钉子，3 个才说明这个仓库真的在写这类代码。
RECRUIT_MIN_FILES = 3


def requisitions(coverage: dict | None) -> list[dict]:
    """够得上门槛、且只能靠装仪器关掉的那几类缺口。

    一条 = 一个扩展名。**不合并**：合并之后「这条启事」的身份是「本轮缺的全部扩展名」，
    装一台补上一类，id 就变成另一个，既追不住也接不上安装记录。
    调用方（渲染、提问）都从这里取，免得两处各算一遍慢慢算岔。
    """
    found: list[dict] = []
    for row in rows(coverage, "capability_gap"):
        if str(row.get("status") or "open") != "open":
            continue
        if _closure_of(row) != "market":
            continue
        count = int(row.get("occurrences") or 0)
        if count < RECRUIT_MIN_FILES:
            continue
        evidence = row.get("evidence") or {}
        found.append({
            "gap_id": str(row.get("id") or ""),
            "extensions": [str(item).lstrip(".") for item in evidence.get("extensions") or []],
            "files": [str(item) for item in evidence.get("files") or []],
            "file_count": count,
        })
    found.sort(key=lambda item: int(item["file_count"]), reverse=True)
    return found


def recruitment_lines(coverage: dict | None, project_root: str = "") -> list[str]:
    """招聘启事与招聘进度。

    两句话，回答两个不同的问题：
    - **缺什么**：有扩展名没人查，且够得上门槛 → 写进报告的限制项。
      这句话同时是对用户的诚实（这部分没查）和对采购的驱动（该招什么）。
    - **要不要你决定**：影子期仪器已经攒够样本、或者根本没到岗 → 需要签字。
      「样本 3/8 轮」这种进度不在这里说，限制项里已经有了，重复写只会稀释重点。

    缺口的 `closure` 决定流向：只有 `market` 进招聘，`host_llm` 只进「没有模型读过」，
    `formulary` 进待复核。混在一起，招聘信号会被「招一台 Markdown 检查器」污染。

    **已经招到的要接得上**：装了、还在影子期的仪器会在这里点名（「已招 X，第 3 轮」），
    否则读者分不清「没人管」和「已在试用」。认领关系从 manifest 的 extensions 推出来，
    不另存一张表——存了就有两份记录，迟早有一份是旧的。
    """
    from .market import claims

    found = requisitions(coverage)
    claimed = claims(project_root) if project_root else {}

    lines: list[str] = []
    for req in found[:3]:
        exts = req["extensions"]
        what = ("、".join(f".{item}" for item in exts)) if exts else "这一类检查"
        count = int(req["file_count"])
        body = f"招聘启事：本次范围含 {count} 个 {what} 文件，在岗仪器都不看这类文件。"
        holder = next((claimed[ext] for ext in exts if ext in claimed), None)
        if holder:
            body += (
                f"已招 {holder['instrument']}（{holder['state']}）。"
                "转正前它的结论不进报告，这类文件仍记为未查。"
            )
        else:
            body += (
                f"要关掉这一类，得装一台声明覆盖 {what} 的仪器"
                "（`hosp_market add` 一份 manifest；装上先跑影子期）。"
                "转正前这部分记为未查，不记为没有问题。"
            )
        lines.append(body)
    if len(found) > 3:
        lines.append(f"另有 {len(found) - 3} 类缺口适合走市场，记在覆盖账里。")
    lines.extend(_recruit_progress(project_root))
    return lines


def _closure_of(row: dict) -> str:
    evidence = row.get("evidence") or {}
    return str(evidence.get("closure") or _GAP_CLOSURE.get(str(row.get("kind") or ""), ""))


def _recruit_progress(project_root: str) -> list[str]:
    """已经招来、正在试用、需要人决定的那几台。

    只在「够格了」和「没到岗」两种情形说话：这两种都要人做决定。
    其余进度不在这里重复——限制项里那句「样本 x/8 轮」已经是全部事实了。
    """
    if not project_root:
        return []
    from . import market, probation

    out: list[str] = []
    for ident, doc in sorted(market.installed(project_root).items()):
        if doc.get("tier") != "probation" or doc.get("problems"):
            continue
        verdict = probation.verdict(doc.get("trial"))
        if verdict["promotable"]:
            out.append(
                f"招聘进度：{ident}。{verdict['line']}"
                "签字前它的结论仍然不进报告。"
            )
        elif verdict["state"] == "unavailable":
            out.append(f"招聘进度：{ident} 装上了但没到岗。{verdict['line']}")
    return out


def memory_lines(coverage: dict | None) -> list[str]:
    """报告用的记忆账。三态都要能看出来——尤其 `cli` 态的「未反馈」。

    断流的闭环比没有闭环更危险：看起来有记忆、有反馈，实际上策略学不到东西。
    所以 `cli` 必须显式写成「本轮未反馈」，不能只是不写。
    """
    book = (ensure(coverage) or {}).get("memory") or {}
    channel = str(book.get("channel") or "absent")
    if channel == "absent":
        return []
    recalled = [str(item) for item in (book.get("recalled") or []) if item]
    injected = int(book.get("injected") or 0)
    withheld = int(book.get("withheld") or 0)
    feedback = str(book.get("feedback") or "absent")

    if not recalled and not withheld:
        return [f"记忆（{channel}）：本轮召回没有命中本仓库的经验条目。"]

    body = f"记忆（{channel}）：引用 {len(recalled)} 条，其中 {injected} 条可作约束或参考"
    clues = len(recalled) - injected
    if clues > 0:
        body += f"，{clues} 条只作线索（要先核前提，不能当结论用）"
    if withheld:
        body += f"，另有 {withheld} 条因低权重或已判错误没有注入"
    body += "。"
    if feedback == "reported":
        body += "本轮已回报反馈。"
    elif feedback == "cli":
        body += "cli 通道没有反馈接口，本轮未反馈——召回策略这一轮学不到东西。"
    elif feedback == "skipped":
        body += "本轮没有可回报的结局，未反馈。"
    else:
        body += "尚未到出院判定，反馈待回报。"
    lines = [body]
    recurred = int(book.get("recurred") or 0)
    if recurred:
        names = [str(item)[:12] for item in (book.get("suspects") or [])][:3]
        tail = f"（牵连 {('、'.join(names))}）" if names else ""
        lines.append(
            f"复发归因：{recurred} 条此前判为已解决的发现复发{tail}。"
            "只有当初参与判定的经验才记嫌疑，没有记忆参与的复发不牵连。"
        )
    return lines


def _extend(host: dict, key: str, values: list[str], cap: int) -> None:
    bucket = host.get(key)
    if not isinstance(bucket, list):
        bucket = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in bucket:
            bucket.append(text)
    host[key] = bucket[-cap:]
