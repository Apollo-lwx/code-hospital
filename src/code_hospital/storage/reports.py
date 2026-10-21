"""报告渲染（契约 §5 模板定死）。

P0 实现：挂号单（register）与检验报告（scan，PR 可贴）。
所有 evidence 以代码块包裹（防 PR 评论二次注入）；位置一律仓库相对路径。
"""

from __future__ import annotations

import datetime as _dt

from .. import __version__
from ..core.models import Case, Finding, Severity

_SEV_LABEL = {
    Severity.CRITICAL.value: "critical",
    Severity.MAJOR.value: "major",
    Severity.MINOR.value: "minor",
    Severity.NIT.value: "nit",
    Severity.INFO.value: "info",
}

# 正文最多列出的发现数；超出仅列 critical+major（契约 §5.3 硬规则）
_MAX_ROWS = 50


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
        "| 范围 | 发现总数 | critical | major | minor | nit/info |",
        "|---|---|---|---|---|---|",
        f"| {scope_desc} | {len(findings)} | {counts['critical']} | {counts['major']}"
        f" | {counts['minor']} | {counts['nit'] + counts['info']} |",
        "",
        "## 发现明细（按严重度排序）\n",
        "| # | 严重度 | 规则 | 位置 | 摘要 |",
        "|---|---|---|---|---|",
    ]
    shown = findings
    if len(findings) > _MAX_ROWS:
        shown = [f for f in findings
                 if f.severity in (Severity.CRITICAL.value, Severity.MAJOR.value)]
    for i, f in enumerate(shown, 1):
        msg = f.message.replace("|", "\\|").replace("\n", " ")[:80]
        parts.append(
            f"| {i} | {f.severity} | {f.rule_id} | `{f.file_path}:{f.line_start}` | {msg} |"
        )
    if len(findings) > _MAX_ROWS:
        parts.append(
            f"\n> 仅列出 critical+major（{len(shown)} 条）；"
            f"完整 {len(findings)} 条见 hosp_case(include=findings)。"
        )

    evidenced = [f for f in shown if f.evidence]
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

    parts += [
        "## 覆盖与限制",
        "",
    ]
    parts += [f"- {ln}" for ln in limitations] if limitations else ["- 无"]
    parts.append(_footer(case))
    return "\n".join(parts)


def summarize_scan(findings: list[Finding], report_path: str) -> str:
    """文本通道摘要（契约 §6.1：≤500 字符 + 报告路径）。"""
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    brief = " ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "无发现"
    return f"检验完成：{len(findings)} 条发现（{brief}）。报告：{report_path}"
