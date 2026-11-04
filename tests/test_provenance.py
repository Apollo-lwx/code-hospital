"""溯源与药柜映射：内置尺子的产物不许被记到 semgrep 规则包名下。"""

from pathlib import Path

from code_hospital.adapters import RawFinding
from code_hospital.catalog.formulary import match_finding, route
from code_hospital.catalog.packs import tool_for_rule
from code_hospital.departments import scanner


def test_builtin_ruler_keeps_its_own_name():
    """java-review 也用 java. 前缀，但它是内置正则尺，不是 semgrep 规则包。"""
    assert tool_for_rule("java.line-length", "java-review") == "java-review"
    assert tool_for_rule("java.empty-catch", "java-review") == "java-review"


def test_semgrep_rules_still_map_to_pack_instruments():
    assert tool_for_rule("java.null-deref", "semgrep") == "java-pack"
    assert tool_for_rule("hotspot.loop-io", "semgrep") == "hotspot-ruler"
    assert tool_for_rule("python.lang.security.audit.exec", "semgrep") == "semgrep"


def test_normalize_from_builtin_ruler_is_not_renamed(tmp_path: Path):
    (tmp_path / "A.java").write_text("class A {}\n", encoding="utf-8")
    finding = scanner.normalize(
        RawFinding(rule_id="java.line-length", message="行长超限", file_path="A.java", line_start=1),
        "java-review", "case_x", "rpt_x", str(tmp_path),
    )
    assert finding.tool == "java-review"


def test_ts_function_lines_lands_in_structure_cabinet():
    """全科药柜里没有「拆分」，函数过长必须先路由到结构科才开得出药。"""
    assert route("ts-review", "ts.function-lines", "minor") == "structure"
    matched = match_finding("ts-review", "ts.function-lines", "minor")
    assert matched.formula.id == "split"


def test_line_length_rules_reach_debride():
    """E501（Python 行长）已经有药，Java/TS 的同度量问题不能用同名不同命。"""
    assert match_finding("java-review", "java.line-length", "minor").formula.id == "debride"
    assert match_finding("ts-review", "ts.line-length", "minor").formula.id == "debride"


def test_language_rulers_share_the_same_treatment():
    assert match_finding("python-review", "python.branch-count", "minor").formula.id == "split"
    assert match_finding("python-review", "python.too-many-params", "minor").formula.id == "split"
    assert match_finding("java-review", "java.empty-catch", "major").formula.id == "guard"
    assert match_finding("python-review", "python.bare-except", "major").formula.id == "guard"
    assert match_finding("ts-review", "ts.dom-write", "major").formula.id == "guard"


def test_debug_output_stays_a_capability_gap():
    """打印日志该不该删要读上下文，项目没用 policy 表过态，不能假装有药。"""
    for tool, rule in (("java-review", "java.system-out"), ("ts-review", "ts.console-log")):
        matched = match_finding(tool, rule, "minor")
        assert matched.formula is None
        assert matched.matched is False
