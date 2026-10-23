"""报告渲染（契约 §5 模板定死）。

已实现：挂号单（register）、检验报告（scan，PR 可贴）、诊断书（diagnose，模板归因）。
所有 evidence 以代码块包裹（防 PR 评论二次注入）；位置一律仓库相对路径。
"""

from __future__ import annotations

import datetime as _dt
import re

from .. import __version__
from ..catalog.policy import SEVERITY_PLAIN
from ..core.models import SEVERITY_ORDER, Case, Finding, Severity

_SEV_LABEL = {
    Severity.CRITICAL.value: SEVERITY_PLAIN["critical"],
    Severity.MAJOR.value: SEVERITY_PLAIN["major"],
    Severity.MINOR.value: SEVERITY_PLAIN["minor"],
    Severity.NIT.value: SEVERITY_PLAIN["nit"],
    Severity.INFO.value: SEVERITY_PLAIN["info"],
}
_METHOD_PLAIN = {
    "autofix": "可自动修改",
    "llm_patch": "需要补丁",
    "manual": "建议人工处理",
}
_RISK_PLAIN = {
    "low": "改动小",
    "medium": "需要看过",
    "high": "必须本人确认",
}
_RX_STATUS_PLAIN = {
    "draft": "草稿",
    "approved": "已批准",
    "rejected": "已拒绝",
}

# 卡片上每种规则最多举几处。风格与提示只写条数。明细仍逐条保留。
_PREVIEW = 3
_STYLE = {Severity.NIT.value, Severity.INFO.value}
_RULE_IN_TEXT = re.compile(r"规则 ([A-Za-z0-9_.]+)")
_RULE_PLAIN = {
    "F401": "没用到的导入",
    "F541": "f-string 里没有占位",
    "F841": "赋值了但没用到",
    "E501": "行太长",
    "I001": "import 没有排好序",
    "BLE001": "过宽的异常捕获",
    "SIM117": "可以合并的 with",
    "structure.function_lines": "函数超过行数",
    "structure.file_lines": "文件超过行数",
    "structure.cyclomatic": "圈复杂度过高",
}
_FORMULA_PLAIN = {
    "debride": "删掉确定多余的代码",
    "split": "把过长的代码拆开",
    "contrast": "改完后核对行为",
    "guard": "把过宽的异常处理收窄",
    "repair": "补一份修改",
    "seal": "先别改",
    "dedupe": "去掉重复代码",
    "relocate": "把依赖放回约定的分层",
    "throttle": "降低热点上的开销",
    "rename": "重命名并改掉引用",
    "supply": "补上缺失的依赖或工具",
}


def _iso(ts_ms: int) -> str:
    return _dt.datetime.fromtimestamp(ts_ms / 1000).astimezone().isoformat(timespec="seconds")


def _header(stage_name: str, case: Case, report_id: str, devices: str = "") -> str:
    lines = [
        f"# {stage_name} · {case.title}",
        "",
        f"> case: `{case.id}` ｜ report: `{report_id}` ｜ round: {case.round}/{case.max_rounds}"
        f" ｜ 分诊: {case.triage_level}",
        f"> 时间: {_iso(case.updated_at)}"
        + (f" ｜ 设备: {devices}" if devices else ""),
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def _footer(case: Case) -> str:
    return (
        "\n---\n"
        f"> 由 code-hospital v{__version__} 生成 ｜ 病历: `.hospital/reports/{case.id}/`"
        " ｜ 本报告含不可信源码片段，一律以代码块包裹\n"
    )


def _worst(severities: list[str]) -> str:
    return min(severities, key=lambda sev: SEVERITY_ORDER.get(sev, 9))


def _card_title(rule_id: str) -> str:
    plain = _RULE_PLAIN.get(rule_id) or _FORMULA_PLAIN.get(rule_id)
    if plain:
        return f"{plain}（`{rule_id}`）"
    return f"`{rule_id}`"


def _bucket(rows: list, severity_of) -> tuple[list, list]:
    """非风格规则在前，风格规则收成后面的计数清单。"""
    groups: dict[str, list] = {}
    order: list[str] = []
    for row in rows:
        key = row[0]
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(row)
    ranked = sorted(order, key=lambda key: (
        SEVERITY_ORDER.get(_worst([severity_of(item) for item in groups[key]]), 9),
        -len(groups[key]),
        key,
    ))
    opened: list[tuple[str, list]] = []
    styled: list[tuple[str, list]] = []
    for key in ranked:
        bucket = groups[key]
        if _worst([severity_of(item) for item in bucket]) in _STYLE:
            styled.append((key, bucket))
        else:
            opened.append((key, bucket))
    return opened, styled


def _style_lines(styled: list[tuple[str, list]], count_of) -> list[str]:
    total = sum(count_of(bucket) for _, bucket in styled)
    lines = [f"### 风格与提示 · {total} 处", ""]
    for rule_id, bucket in styled:
        lines.append(f"- {_card_title(rule_id)} × {count_of(bucket)}")
    lines.append("")
    return lines


def _scan_cards(findings: list[Finding]) -> list[str]:
    tagged = [(f.rule_id, f) for f in findings]
    opened, styled = _bucket(tagged, lambda item: item[1].severity)
    lines = [
        "## 按规则看",
        "",
        "> 同一条规则收成一张卡片，最多举 3 处。风格与提示只写条数。每一处位置在后面逐条列出。",
        "",
    ]
    if not findings:
        lines += ["没有发现。", ""]
        return lines
    for rule_id, bucket in opened:
        rows = [item[1] for item in bucket]
        rows.sort(key=lambda f: (f.file_path, f.line_start))
        worst = _worst([f.severity for f in rows])
        lines += [
            f"### {_card_title(rule_id)} · {len(rows)} 处 · {_SEV_LABEL.get(worst, worst)}",
            "",
        ]
        sample = rows[:_PREVIEW]
        places = "、".join(f"`{f.file_path}:{f.line_start}`" for f in sample)
        lines.append(f"- 举例: {places}")
        extra = len(rows) - len(sample)
        if extra:
            lines.append(f"- 另有 {extra} 处，见下方明细")
        lines.append("")
    if styled:
        lines += _style_lines(styled, len)
    return lines


def _scan_previews(findings: list[Finding]) -> list[Finding]:
    tagged = [(f.rule_id, f) for f in findings]
    opened, _styled = _bucket(tagged, lambda item: item[1].severity)
    picked: list[Finding] = []
    for _rule_id, bucket in opened:
        rows = sorted((item[1] for item in bucket), key=lambda f: (f.file_path, f.line_start))
        picked.extend(rows[:_PREVIEW])
    return picked


def _cluster_rule(diagnosis, members: list[Finding]) -> str:
    if members:
        return members[0].rule_id
    return diagnosis.category


def _diagnose_cards(clusters: list[tuple]) -> list[str]:
    tagged = [(_cluster_rule(diagnosis, members), (diagnosis, members)) for diagnosis, members in clusters]
    opened, styled = _bucket(tagged, lambda item: item[1][0].severity)
    lines = [
        "## 按规则看",
        "",
        "> 同一条规则收成一张卡片，最多举 3 处。风格与提示只写条数。每一组的说明在后面逐条列出。",
        "",
    ]
    if not clusters:
        lines += ["没有发现。", ""]
        return lines
    for rule_id, bucket in opened:
        diagnoses = [item[1][0] for item in bucket]
        rows: list[Finding] = []
        for item in bucket:
            rows.extend(item[1][1])
        rows.sort(key=lambda finding: (finding.file_path, finding.line_start))
        worst = _worst([diagnosis.severity for diagnosis in diagnoses])
        methods = sorted({
            _METHOD_PLAIN.get(diagnosis.fixability, diagnosis.fixability) for diagnosis in diagnoses
        })
        lines += [
            f"### {_card_title(rule_id)} · {len(rows)} 处 · {_SEV_LABEL.get(worst, worst)}",
            "",
            f"- 建议怎么处理: {'、'.join(methods)}",
        ]
        sample = rows[:_PREVIEW]
        if sample:
            lines.append("- 举例: " + "、".join(f"`{f.file_path}:{f.line_start}`" for f in sample))
        if len(rows) > _PREVIEW:
            lines.append(f"- 另有 {len(rows) - _PREVIEW} 处，见下方问题分组")
        lines.append("")
    if styled:
        lines += _style_lines(styled, lambda bucket: sum(len(item[1][1]) for item in bucket))
    return lines


def _severity_from_rule(rule_id: str) -> str:
    """处方项入库前会丢掉 severity。展示时按规则补一档，和检验的分档对齐。"""
    if rule_id.startswith("structure."):
        return Severity.MAJOR.value
    head = rule_id.split(".")[-1]
    if re.fullmatch(r"F\d+", head) or head.startswith("E9"):
        return Severity.MAJOR.value
    if rule_id.startswith(("E", "W")):
        return Severity.MINOR.value
    return Severity.NIT.value


def _item_card_severity(item: dict) -> str:
    if item.get("severity"):
        return item["severity"]
    if item.get("risk") == "high":
        return Severity.CRITICAL.value
    return _severity_from_rule(_item_rule(item))


def _item_rule(item: dict) -> str:
    match = _RULE_IN_TEXT.search(item.get("instructions") or "")
    if match:
        return match.group(1)
    return item.get("formula_id") or "未命名"


def _item_place(item: dict) -> str:
    if item.get("target_symbol"):
        return f"`{item.get('target_file') or '—'}` · {item['target_symbol']}"
    if item.get("target_file"):
        return f"`{item['target_file']}`"
    return "—"


def _prescribe_cards(items: list[dict]) -> list[str]:
    primaries = [item for item in items if not item.get("parent_formula_id")]
    lines = [
        "## 按规则看",
        "",
        "> 同一条规则收成一张卡片。核对步骤挂在主项下面，不另算成新问题。完整顺序在后面逐条列出。",
        "",
    ]
    if not items:
        lines += ["没有要处理的项。", ""]
        return lines
    tagged = [(_item_rule(item), item) for item in primaries]
    opened, styled = _bucket(tagged, lambda item: _item_card_severity(item[1]))
    for rule_id, bucket in opened:
        rows = [item[1] for item in bucket]
        methods = sorted({_METHOD_PLAIN.get(item["method"], item["method"]) for item in rows})
        risks = sorted({_RISK_PLAIN.get(item["risk"], item["risk"]) for item in rows})
        lines += [
            f"### {_card_title(rule_id)} · {len(rows)} 项 · {'、'.join(methods)}",
            "",
            f"- 风险: {'、'.join(risks)}",
        ]
        places: list[str] = []
        for item in rows:
            place = _item_place(item)
            if place not in places:
                places.append(place)
            if len(places) == _PREVIEW:
                break
        if places:
            lines.append("- 举例: " + "、".join(places))
        if len(rows) > _PREVIEW:
            lines.append(f"- 另有 {len(rows) - _PREVIEW} 项，见下方处理清单")
        diag_ids = {item.get("diagnosis_id") for item in rows}
        checks = [
            item for item in items
            if item.get("parent_formula_id") and item.get("diagnosis_id") in diag_ids
        ]
        if checks:
            lines.append(f"- 核对步骤 {len(checks)} 项，挂在这些主项上，不另算成新问题")
        lines.append("")
    if styled:
        total = sum(len(bucket) for _, bucket in styled)
        lines += [f"### 风格与提示 · {total} 处", ""]
        for rule_id, bucket in styled:
            rows = [item[1] for item in bucket]
            diag_ids = {item.get("diagnosis_id") for item in rows}
            checks = [
                item for item in items
                if item.get("parent_formula_id") and item.get("diagnosis_id") in diag_ids
            ]
            extra = f"，另有核对步骤 {len(checks)} 项" if checks else ""
            lines.append(f"- {_card_title(rule_id)} × {len(rows)}{extra}")
        lines.append("")
    if not primaries:
        lines += ["这些项都挂在别的主项上，见下方处理清单。", ""]
    return lines


def render_register_report(case: Case, report_id: str, precheck_hits: int, duplicates: list[dict]) -> str:
    dup_rows = "\n".join(
        f"| `{d['case_id']}` | {d['title']} | {d['similarity']} | {d['outcome'] or '在治'} |"
        for d in duplicates
    )
    parts = [
        _header("挂号单", case, report_id),
        "## 就诊信息\n",
        "| 范围类型 | 分诊级别 | 疗程上限 | 预检急诊特征 |",
        "|---|---|---|---|",
        f"| {case.scope_type} | {case.triage_level} | {case.max_rounds} | {precheck_hits} 处 |",
        "",
    ]
    if duplicates:
        parts += [
            "## 病例查重（历史就诊记录）\n",
            "| 病例 | 标题 | 命中 | 结局 |",
            "|---|---|---|---|",
            dup_rows,
            "",
        ]
    parts.append(_footer(case))
    return "\n".join(parts)


def render_scan_report(
    case: Case,
    report_id: str,
    devices: list[dict],
    findings: list[Finding],
    limitations: list[str],
    memory_context: dict | None = None,
) -> str:
    counts = {sev: 0 for sev in _SEV_LABEL}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    device_str = ", ".join(
        f"{d['tool']}@{d['version']}" if d["available"] else f"{d['tool']}(不可用)"
        for d in devices
    )
    scope_desc = {
        "repo": "全仓库",
        "paths": "指定路径",
        "staged": "暂存区 diff",
        "commit": "单提交 diff",
        "range": "区间 diff",
    }.get(case.scope_type, case.scope_type)

    parts = [
        _header("检验报告", case, report_id, device_str),
        "## 概要\n",
        "| 范围 | 发现总数 | 必须先处理 | 明确缺陷 | 一般问题 | 风格与提示 |",
        "|---|---|---|---|---|---|",
        f"| {scope_desc} | {len(findings)} | {counts['critical']} | {counts['major']}"
        f" | {counts['minor']} | {counts['nit'] + counts['info']} |",
        "",
        "> 这五档是检查工具自己的分法，不是漏洞通告里的超危、高危。"
        "必须先处理指密钥或同类安全项；明确缺陷指已经能指出的代码问题，例如没用到的导入、超过行数；"
        "一般问题偏格式；风格建议是可改可不改的写法；参考信息并进「风格与提示」。",
        "",
    ]
    parts += _scan_cards(findings)
    parts += [
        "## 发现明细（按严重度排序）\n",
        "| # | 严重度 | 规则 | 位置 | 摘要 | 病史 |",
        "|---|---|---|---|---|---|",
    ]
    for i, f in enumerate(findings, 1):
        msg = f.message.replace("|", "\\|").replace("\n", " ")[:80]
        history = f"复发×{len(f.history_refs)}" if f.history_refs else "初发"
        parts.append(
            f"| {i} | {_SEV_LABEL.get(f.severity, f.severity)} | {f.rule_id} | `{f.file_path}:{f.line_start}` | {msg} | {history} |"
        )
    if findings:
        parts.append("\n> 明细逐条列出全部位置。卡片上每种规则只举了前 3 处。")

    evidenced = [f for f in _scan_previews(findings) if f.evidence]
    if evidenced:
        parts += ["", "## 证据", ""]
        for i, f in enumerate(evidenced, 1):
            parts += [
                f"### {i}. `{f.file_path}:{f.line_start}` · {f.rule_id}",
                "",
                "```",
                f.evidence,
                "```",
                "",
            ]

    # 项目记忆（既往史）：有帮助但不可全信，独立成节并附信任声明
    if memory_context and memory_context.get("found"):
        parts += [
            "## 项目记忆（未经证实，仅供坐诊参考）",
            "",
            f"> {memory_context.get('note', '')}",
            "",
        ]
        for item in memory_context["official"]["conventions"]:
            parts.append(f"- [convention·official] **{item['title']}**：{item['snippet']}")
        for item in memory_context["official"]["prohibitions"]:
            parts.append(f"- [prohibition·official] **{item['title']}**：{item['snippet']}")
        for item in memory_context["pending"]:
            parts.append(f"- [{item['type']}·pending] **{item['title']}**：{item['snippet']}")
        parts.append("")

    parts += [
        "## 覆盖与限制",
        "",
    ]
    parts += [f"- {ln}" for ln in limitations] if limitations else ["- 无"]
    parts.append(_footer(case))
    return "\n".join(parts)


def render_diagnose_report(
    case: Case,
    report_id: str,
    clusters: list[tuple],
    stats: dict,
    use_llm: bool = False,
) -> str:
    """诊断书（契约 §5.4）。clusters 为 (Diagnosis, findings) 对，位置只出现在报告里。"""
    parts = [
        _header("诊断书", case, report_id),
        "## 概要\n",
        "| 发现 | 分组 | 可自动修改 | 需要补丁 | 建议人工处理 |",
        "|---|---|---|---|---|",
        (
            f"| {stats['total_findings']} | {stats['clusters']} | {stats['autofix']}"
            f" | {stats['llm_patch']} | {stats['manual']} |"
        ),
        "",
    ]
    if use_llm:
        parts += ["> 宿主声明将自行增强解读。本诊断书仍是模板归因，未调用模型。", ""]
    else:
        parts += ["> 分组和处置建议来自规则表，不是模型判断。", ""]
    parts += _diagnose_cards(clusters)
    parts += ["## 问题分组", ""]
    if not clusters:
        parts.append("没有发现。")
    for i, (diagnosis, members) in enumerate(clusters, 1):
        places = "、".join(f"`{f.file_path}:{f.line_start}`" for f in members[:5])
        if len(members) > 5:
            places += f" 等 {len(members)} 处"
        review = "⚠ 待复核" if diagnosis.confidence < 0.5 else f"{diagnosis.confidence:.2f}"
        parts += [
            f"### 第 {i} 组 · {diagnosis.category}（{_SEV_LABEL.get(diagnosis.severity, diagnosis.severity)}，置信度 {review}）",
            "",
            f"- 说明: {diagnosis.root_cause}",
            f"- 涉及: {places}",
        ]
        if diagnosis.history_refs:
            hist = "；".join(
                f"`{h['case_id']}` {h.get('outcome') or '在治'} {h.get('rounds', 1)} 轮"
                for h in diagnosis.history_refs
            )
            parts.append(f"- 病史: {hist}")
        else:
            parts.append("- 病史: 初发")
        parts.append(
            f"- 建议怎么处理: {_METHOD_PLAIN.get(diagnosis.fixability, diagnosis.fixability)}"
        )
        if diagnosis.referrals:
            for ref in diagnosis.referrals:
                parts.append(f"- 会诊转诊: {ref.get('target_repo', '')} · {ref.get('hypothesis', '')}")
        parts.append("")
    parts.append(_footer(case))
    return "\n".join(parts)


def render_reading_report(
    case: Case,
    clusters: list[tuple],
    items: list[dict],
    requested: list[str] | None = None,
) -> str:
    """给人看的一页。按文件、再按函数，把落在同一处的问题和处理放在一起。"""
    from ..catalog.formulary import build_conditions

    conditions = build_conditions(clusters, requested or [])
    by_diag = {diagnosis.id: (diagnosis, members) for diagnosis, members in clusters}
    items_by_diag: dict[str, list[dict]] = {}
    for item in items:
        items_by_diag.setdefault(item.get("diagnosis_id") or "", []).append(item)
    findings = [finding for _diagnosis, members in clusters for finding in members]
    parts = [
        _header("就诊一页", case, "reading"),
        "> 只看这一页。同一函数里的问题放在一起。逐条底账仍在同目录的检验报告、诊断书和处方里。",
        "",
    ]
    parts += _reading_index(findings)
    parts += ["## 按代码位置", ""]
    if not conditions:
        parts += ["没有要一起看的问题。", ""]
        parts.append(_footer(case))
        return "\n".join(parts)

    files: dict[str, list[tuple]] = {}
    for condition in conditions:
        pairs = [by_diag[diag_id] for diag_id in condition["diagnosis_ids"] if diag_id in by_diag]
        if not pairs:
            continue
        path, symbol, line = _lesion_head(pairs)
        files.setdefault(path or "—", []).append((line, symbol, pairs))
    for path in sorted(files, key=lambda name: (name == "—", name)):
        parts += [f"### `{path}`", ""]
        functions = sorted(
            ((line, symbol, pairs) for line, symbol, pairs in files[path] if symbol),
            key=lambda row: (row[0], row[1]),
        )
        loose: list[tuple] = []
        styled: list[tuple] = []
        for _line, symbol, pairs in files[path]:
            if symbol:
                continue
            if any(not _is_style_diagnosis(diagnosis) for diagnosis, _members in pairs):
                loose.extend(pairs)
            else:
                styled.extend(pairs)
        for line, symbol, pairs in functions:
            parts += [f"#### `{symbol}`（{line} 行）", ""]
            parts += _lesion_lines(pairs, items_by_diag)
            parts.append("")
        if loose:
            parts += ["#### 没有落在某个函数里", ""]
            parts += _lesion_lines(loose, items_by_diag)
            parts.append("")
        if styled:
            parts += ["#### 其余写法", ""]
            parts += _lesion_lines(styled, items_by_diag)
            parts.append("")
    parts.append(_footer(case))
    return "\n".join(parts)


def _reading_index(findings: list[Finding]) -> list[str]:
    tagged = [(finding.rule_id, finding) for finding in findings]
    opened, styled = _bucket(tagged, lambda item: item[1].severity)
    lines = ["## 一共有哪些", ""]
    if not findings:
        lines += ["没有发现。", ""]
        return lines
    for rule_id, bucket in opened:
        lines.append(f"- {_card_title(rule_id)} × {len(bucket)}")
    if styled:
        total = sum(len(bucket) for _, bucket in styled)
        lines.append(f"- 风格与提示 × {total}")
    lines.append("")
    return lines


def _is_style_diagnosis(diagnosis) -> bool:
    return diagnosis.severity in _STYLE


def _lesion_head(pairs: list[tuple]) -> tuple[str, str, int]:
    fallback = ("", "", 0)
    for _diagnosis, members in pairs:
        for finding in members:
            if finding.rule_id == "structure.function_lines" and finding.symbol:
                return finding.file_path, finding.symbol, finding.line_start
            if not fallback[0]:
                fallback = (finding.file_path, "", finding.line_start)
    return fallback


def _lesion_lines(pairs: list[tuple], items_by_diag: dict[str, list[dict]]) -> list[str]:
    lines: list[str] = []
    style: dict[str, int] = {}
    defects: dict[str, dict] = {}
    order: list[str] = []
    for diagnosis, members in pairs:
        rule_id = members[0].rule_id if members else diagnosis.category
        count = len(members) or 1
        if _is_style_diagnosis(diagnosis):
            style[rule_id] = style.get(rule_id, 0) + count
            continue
        related = items_by_diag.get(diagnosis.id, [])
        primary = [item for item in related if not item.get("parent_formula_id")]
        checks = [item for item in related if item.get("parent_formula_id")]
        if primary:
            method = _METHOD_PLAIN.get(primary[0]["method"], primary[0]["method"])
        else:
            method = _METHOD_PLAIN.get(diagnosis.fixability, diagnosis.fixability)
        bucket = defects.get(rule_id)
        if bucket is None:
            bucket = {"count": 0, "method": method, "checks": False}
            defects[rule_id] = bucket
            order.append(rule_id)
        bucket["count"] += count
        bucket["checks"] = bucket["checks"] or bool(checks)
    for rule_id in order:
        bucket = defects[rule_id]
        text = f"- {_card_title(rule_id)} {bucket['count']} 处，{bucket['method']}"
        if bucket["checks"]:
            text += "。改完要核对行为"
        lines.append(text)
    if style:
        bits = "、".join(f"{_card_title(rule_id)} {count} 处" for rule_id, count in style.items())
        lines.append(f"- 写法：{bits}")
    return lines or ["- 没有要处理的问题"]


def render_prescribe_report(case: Case, prescription_id: str, rx: dict, report_path: str) -> str:
    """处方（契约 §5.5）。confirmation 正文不含仓库源码。"""
    items = rx.get("items") or []
    human = "是" if rx.get("human_in_loop") else "否"
    channel = rx.get("confirmation_channel") or "—"
    parts = [
        _header("处方", case, prescription_id),
        "## 风险总览\n",
        "| 处方 | 状态 | 风险 | 谁批的 | 需要你确认 | 确认方式 |",
        "|---|---|---|---|---|---|",
        (
            f"| `{prescription_id}` | {_RX_STATUS_PLAIN.get(rx.get('status', 'draft'), rx.get('status', 'draft'))}"
            f" | {_RISK_PLAIN.get(rx.get('risk_level', ''), rx.get('risk_level', ''))}"
            f" | {rx.get('approved_by') or '—'} | {human} | {channel} |"
        ),
        "",
    ]
    parts += _prescribe_cards(items)
    parts += [
        "## 处理清单（按顺序）\n",
        "| # | 改哪里 | 怎么处理 | 风险 | 怎么算改完 | 允许碰到的范围 |",
        "|---|---|---|---|---|---|",
    ]
    manuals = []
    if not items:
        parts.append("| — | 无 | — | — | 无需用药 | — |")
    for it in items:
        mark = "⚠ " if it["risk"] == "high" else ""
        expect = it["acceptance"].get("expect", "").replace("|", "\\|")
        scope = ", ".join(f"`{p}`" for p in it["scope_paths"]) or "—"
        target = f"`{it['target_file']}`" if it["target_file"] else "—"
        if it["target_symbol"]:
            target += f" · {it['target_symbol']}"
        parts.append(
            f"| {mark}{it['seq']} | {target} | {_METHOD_PLAIN.get(it['method'], it['method'])}"
            f" | {_RISK_PLAIN.get(it['risk'], it['risk'])} | {expect} | {scope} |"
        )
        if it["method"] == "manual":
            manuals.append(it)
    parts.append("")
    if manuals:
        parts += ["## 需要你自己改", ""]
        for it in manuals:
            parts.append(f"- `{it['target_file'] or '—'}`：{it['instructions'][:180]}")
        parts.append("")
    parts.append(_footer(case))
    _ = report_path
    return "\n".join(parts)


_OUTCOME_LABEL = {
    "cured": "治愈 ✅",
    "improved": "好转 🩹",
    "unchanged": "未愈 ⚠",
    "referred": "转院 🏥",
}


def render_discharge_report(case: Case, outcome: str, gate: dict, diff: dict, follow_path: str) -> str:
    """出院小结（契约 §5.7）。"""
    rows = []
    for check in gate["checks"]:
        rows.append(f"| {check['name']} | {check['verdict']} | {check['detail']} |")
    waiver = ""
    if gate.get("verdict") == "waived" and gate.get("waiver"):
        w = gate["waiver"]
        waiver = f" 豁免人 user · {w.get('confirmation_channel', 'convention')} · 理由 {w.get('reason', '')}"
    counts = diff["counts"]
    return "\n".join([
        f"# 出院小结 {case.id}",
        "",
        f"## 疗效判定：{_OUTCOME_LABEL.get(outcome, outcome)}",
        "",
        "## 门禁会签",
        "",
        "| 维度 | 结论 | 说明 |",
        "|---|---|---|",
        *rows,
        "",
        f"> 门禁: **{gate['verdict']}**（{gate['level']}）{waiver}",
        "",
        "## 复诊差分",
        "",
        "| resolved | unchanged | 医源性新增 | 覆盖差异新增 | 疑似消除 |",
        "|---|---|---|---|---|",
        f"| {counts['resolved']} | {counts['unchanged']} | {counts['new_related']} | {counts['new_unrelated']} | {counts['possibly_resolved']} |",
        "",
        "## 疗程回顾",
        "",
        f"- 疗程: {case.round} 轮",
        "",
        "## 随访计划",
        "",
        f"见 `{follow_path}`。",
        "",
        _footer(case),
    ])


def render_follow_up(case_id: str, watch_files: list[str], fingerprints: list[str]) -> str:
    lines = ["## 高危病灶（重点复访）", "", "| 文件 | 原因 |", "|---|---|"]
    if watch_files:
        lines.extend(f"| `{path}` | 本轮施治触及 |" for path in watch_files)
    else:
        lines.append("| — | 本轮没有留下需复访的文件 |")
    snippet = "\n".join(f"  - {fp}" for fp in fingerprints) or "  - （无）"
    lines += [
        "",
        "## CI 复扫片段（可复制）",
        "",
        "```yaml",
        "# 检出以下指纹复发即报警（复用 code-hospital 扫描适配器）",
        "watch_fingerprints:",
        snippet,
        "```",
        "",
        "## 复发处置",
        "",
        f"随访/CI 检出监视指纹复发 → 重新 `hosp_register`，病例查重将命中本病例（`{case_id}`）。",
        "",
    ]
    return "\n".join(lines)


def summarize_scan(findings: list[Finding], report_path: str) -> str:
    """文本通道摘要（契约 §6.1：≤500 字符 + 报告路径）。"""
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    brief = " ".join(f"{_SEV_LABEL.get(k, k)}:{v}" for k, v in sorted(counts.items())) or "无发现"
    cards = len({f.rule_id for f in findings if f.severity not in _STYLE})
    if any(f.severity in _STYLE for f in findings):
        cards += 1
    return f"检验完成：{len(findings)} 条发现，按规则收成 {cards} 张卡片（{brief}）。报告：{report_path}"
