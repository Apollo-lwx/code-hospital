"""规则处置矩阵：规范（convention）与规则（rule）分流，谁说了算、自动到什么程度。

三层优先级，逐层回落：

1. 仓库 `code-hospital.yaml` 的 `catalog.rules.dispositions`（用户说了算）；
2. 内置默认表（只收无争议的规范类：ruff N8* 命名族、ESLint naming-convention、
   各语言行长规则 java.line-length / ts.line-length / E501）；
3. 未命中维持现状——名册剂型 + 风险闸决定一切，不额外加确认。

规范（命名、风格、格式）不造成功能错误，默认**只告知**：落 advisory、不随处方执行、
逐项点名（include_advisory）才施治。规则（缺陷、安全、正确性）明确必须修，
默认同现状走批准闸。用户可以放宽规范类（auto_fix=auto / confirm=none）——
自己的仓自己说了算，但放宽时风险提前告知，写进药味 instructions。

match 语法：精确规则 id（`N802`）或 `*` 结尾的前缀（`N8*`）；用户与默认表合并后
按「用户优先、最长匹配优先」解析。
"""

from __future__ import annotations

from dataclasses import dataclass

_CLASSES = {"convention", "rule"}
_AUTO_FIX = {"never", "confirm", "auto"}
_CONFIRM = {"explicit", "approve", "none"}
_SEVERITIES = {"critical", "major", "minor", "nit", "info"}
# 用户等级覆盖联动到处方项风险下限：critical 明确必须修 → 高危强制真人。
_SEVERITY_RISK_FLOOR = {"critical": "high", "major": "medium"}


@dataclass(frozen=True)
class Disposition:
    """一条处置：命中规则怎么分诊、自动到什么程度、要不要二次确认。"""

    match: str
    cls: str = ""           # convention | rule；空 = 沿用默认分流
    auto_fix: str = ""      # never | confirm | auto；空 = 按 class 默认
    confirm: str = ""       # explicit | approve | none；空 = 按 class 默认
    severity: str = ""      # 等级覆盖；空 = 不覆盖

    @property
    def resolved_class(self) -> str:
        return self.cls or "rule"

    @property
    def resolved_auto_fix(self) -> str:
        if self.auto_fix:
            return self.auto_fix
        # 规范类默认只告知；规则类默认批准后执行（现状）
        return "never" if self.resolved_class == "convention" else "confirm"

    @property
    def resolved_confirm(self) -> str:
        if self.confirm:
            return self.confirm
        return "explicit" if self.resolved_class == "convention" else "approve"

    @property
    def advisory(self) -> bool:
        """告知建议项：不随处方执行，include_advisory 点名才翻 pending。"""
        return self.resolved_auto_fix == "never"

    @property
    def relaxed_convention(self) -> bool:
        """规范类被放宽（不再逐项点名）：风险要提前告知。"""
        return self.resolved_class == "convention" and not (
            self.resolved_auto_fix == "never" and self.resolved_confirm == "explicit"
        )

    @property
    def risk_floor(self) -> str:
        """用户显式配置的等级覆盖联动风险下限；默认表不联动。"""
        return _SEVERITY_RISK_FLOOR.get(self.severity, "")


# 内置默认表：只收没有争议的规范类。其余一律维持现状（rule + confirm + approve），
# 宁可漏不可错——把缺陷错标成风格，等于告诉用户「这个病不用治」。
DEFAULT_DISPOSITIONS: tuple[Disposition, ...] = (
    # ruff N8xx 命名族（正骨）：医生可以觉得名字不合规，不能强制人改名。
    Disposition("N8*", cls="convention"),
    # ESLint / TS 命名约定同上。
    Disposition("naming-convention", cls="convention"),
    Disposition("@typescript-eslint/naming-convention", cls="convention"),
    # 行长是纯排版规范（2026-11-23 真实仓观察后收入）：11 个 line-length 发现
    # 基线全开清创自动修，与「规范只告知」的分流口径冲突。Python 的 E501 同类。
    Disposition("java.line-length", cls="convention"),
    Disposition("ts.line-length", cls="convention"),
    Disposition("E501", cls="convention"),
    # 结构健康度量（拆分全族，2026-11-23 归属评估后收入）：超阈值不造成功能错误，
    # 且拆函数没有机械判据——修法是语义重构，自动拆等于替用户重写代码。
    # cyclomatic 保留 major 严重度（报告照显档），只是分流到告知级。
    Disposition("structure.function_lines", cls="convention"),
    Disposition("structure.file_lines", cls="convention"),
    Disposition("structure.cyclomatic", cls="convention"),
    Disposition("ts.function-lines", cls="convention"),
    Disposition("python.branch-count", cls="convention"),
    Disposition("python.too-many-params", cls="convention"),
    # 重复代码（jscpd 去重，2026-11-23 拍板收入）：重复不是缺陷，
    # 合并重构同样没有机械判据；刻意重复（测试夹具/协议对齐）由仓库配置放宽。
    Disposition("clone.duplicate", cls="convention"),
)


def parse_dispositions(raw) -> tuple[tuple[Disposition, ...], list[str]]:
    """解析 `catalog.rules.dispositions`。坏行跳过并写明原因，不静默吞掉。"""
    if raw is None:
        return (), []
    if not isinstance(raw, list):
        return (), ["处置矩阵 dispositions 不是列表，已忽略"]
    kept: list[Disposition] = []
    notes: list[str] = []
    for index, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            notes.append(f"处置矩阵第 {index} 行不是映射，已跳过")
            continue
        match = str(row.get("match") or "").strip()
        if not match:
            notes.append(f"处置矩阵第 {index} 行缺 match，已跳过")
            continue
        cls = str(row.get("class") or "").strip().lower()
        auto_fix = str(row.get("auto_fix") or "").strip().lower()
        confirm = str(row.get("confirm") or "").strip().lower()
        severity = str(row.get("severity") or "").strip().lower()
        bad = []
        if cls and cls not in _CLASSES:
            bad.append(f"class={cls}")
        if auto_fix and auto_fix not in _AUTO_FIX:
            bad.append(f"auto_fix={auto_fix}")
        if confirm and confirm not in _CONFIRM:
            bad.append(f"confirm={confirm}")
        if severity and severity not in _SEVERITIES:
            bad.append(f"severity={severity}")
        if bad:
            notes.append(f"处置矩阵 {match} 含非法值（{'、'.join(bad)}），已跳过")
            continue
        kept.append(Disposition(match, cls, auto_fix, confirm, severity))
    return tuple(kept), notes


def _matches(pattern: str, rule_id: str) -> bool:
    if pattern.endswith("*"):
        return rule_id.startswith(pattern[:-1])
    return rule_id == pattern


def resolve(rule_id: str, user_rows: tuple[Disposition, ...] = ()) -> Disposition | None:
    """规则 → 处置。用户配置优先于内置默认；同层最长匹配优先。未命中返回 None。"""
    for rows in (user_rows, DEFAULT_DISPOSITIONS):
        hits = [d for d in rows if _matches(d.match, rule_id)]
        if hits:
            return max(hits, key=lambda d: len(d.match))
    return None
