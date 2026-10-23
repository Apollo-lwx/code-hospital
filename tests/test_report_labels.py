"""报告正文用中文档名。结构化字段仍是 critical / major / minor / nit / info。"""

from code_hospital.catalog.policy import describe, load_policy
from code_hospital.core.models import Case, Finding, Scope
from code_hospital.storage.reports import render_scan_report


def test_default_policy_reads_as_plain_language():
    text = describe(load_policy("."), 0)
    assert "函数超过 80 行算问题" in text
    assert "没有条目因这条标准被丢掉" in text
    assert "病症口径" not in text


def test_scan_report_uses_plain_severity_labels():
    case = Case(
        id="case_x", project_root=".", title="示例", scope_type="repo",
        scope=Scope(), triage_level="checkup", status="scanned",
        created_at=1_700_000_000_000, updated_at=1_700_000_000_000,
    )
    finding = Finding(
        id="f1", case_id=case.id, report_id="r1", tool="ruff", rule_id="F401",
        severity="major", file_path="a.py", line_start=1, line_end=1,
        message="imported but unused", fingerprint="fp",
    )
    md = render_scan_report(case, "r1", [], [finding], ["函数超过 80 行算问题"])
    assert "明确缺陷" in md
    assert "必须先处理" in md
    assert "| 1 | major |" not in md
    assert "不是漏洞通告里的超危、高危" in md


def _case() -> Case:
    return Case(
        id="case_x", project_root=".", title="示例", scope_type="repo",
        scope=Scope(), triage_level="checkup", status="scanned",
        created_at=1_700_000_000_000, updated_at=1_700_000_000_000,
    )


def _finding(fid: str, rule: str, severity: str, path: str, line: int) -> Finding:
    return Finding(
        id=fid, case_id="case_x", report_id="r1", tool="ruff", rule_id=rule,
        severity=severity, file_path=path, line_start=line, line_end=line,
        message="msg", fingerprint=fid,
    )


def test_scan_cards_preview_three_and_keep_every_row():
    majors = [_finding(f"f{i}", "F401", "major", "a.py", i) for i in range(1, 5)]
    nit = _finding("n1", "I001", "nit", "style.py", 9)
    md = render_scan_report(_case(), "r1", [], majors + [nit], [])
    head, _, tail = md.partition("## 发现明细")
    assert "没用到的导入" in head
    assert "另有 1 处" in head
    assert head.count("`a.py:") == 3
    assert "style.py" not in head
    assert "import 没有排好序" in head
    assert "`style.py:9`" in tail
    for line in range(1, 5):
        assert f"`a.py:{line}`" in tail


def test_prescribe_card_nests_check_step_without_dropping_rows():
    from code_hospital.storage.reports import render_prescribe_report

    def item(seq, formula, parent, diagnosis_id, severity, method, risk):
        return {
            "seq": seq,
            "method": method,
            "risk": risk,
            "severity": severity,
            "target_file": "a.py",
            "target_symbol": "load_report" if formula == "split" else "",
            "scope_paths": ["a.py"],
            "acceptance": {"expect": "相关测试通过"},
            "instructions": f"规则 structure.function_lines（structure-ruler） seq {seq}",
            "parent_formula_id": parent,
            "formula_id": formula,
            "diagnosis_id": diagnosis_id,
        }

    items = [
        item(1, "split", "", "d1", "major", "llm_patch", "medium"),
        item(2, "contrast", "split", "d1", "major", "llm_patch", "low"),
    ]
    for row in items:
        row.pop("severity")
    md = render_prescribe_report(
        _case(), "rx1",
        {"status": "draft", "risk_level": "medium", "items": items},
        "",
    )
    head, _, tail = md.partition("## 处理清单")
    assert "函数超过行数" in head
    assert "核对步骤 1 项" in head
    assert "### 改完后核对行为" not in head
    assert "| 1 |" in tail and "| 2 |" in tail


def test_reading_page_groups_one_function():
    from code_hospital.core.models import Diagnosis
    from code_hospital.storage.reports import render_reading_report

    case = _case()

    def finding(fid, rule, severity, line, end, symbol=""):
        row = _finding(fid, rule, severity, "a.py", line)
        row.line_end = end
        row.symbol = symbol
        return row

    fn = finding("f-fn", "structure.function_lines", "major", 10, 80, "ingest")
    inner = finding("f-imp", "F401", "major", 20, 20)
    style = finding("f-style", "I001", "nit", 30, 30)
    loose_a = finding("f-top", "F401", "major", 1, 1)
    loose_b = finding("f-top2", "F401", "major", 2, 2)

    def diagnosis(did, row, fixability):
        return Diagnosis(
            id=did, case_id=case.id, report_id="r1", fingerprint=did,
            category=row.rule_id, root_cause="说明", severity=row.severity,
            confidence=0.8, fixability=fixability, member_ids=[row.id],
        )

    clusters = [
        (diagnosis("d-fn", fn, "llm_patch"), [fn]),
        (diagnosis("d-imp", inner, "autofix"), [inner]),
        (diagnosis("d-style", style, "autofix"), [style]),
        (diagnosis("d-top", loose_a, "autofix"), [loose_a]),
        (diagnosis("d-top2", loose_b, "autofix"), [loose_b]),
    ]
    items = [
        {"diagnosis_id": "d-fn", "method": "llm_patch", "parent_formula_id": ""},
        {"diagnosis_id": "d-fn", "method": "llm_patch", "parent_formula_id": "split"},
        {"diagnosis_id": "d-imp", "method": "autofix", "parent_formula_id": ""},
    ]
    md = render_reading_report(case, clusters, items)
    body = md.split("#### `ingest`", 1)[1]
    assert "函数超过行数" in body
    assert "没用到的导入" in body
    assert "改完要核对行为" in body
    assert "写法：" in body
    assert "import 没有排好序" in body
    outside = md.split("#### 没有落在某个函数里", 1)[1]
    assert md.count("#### 没有落在某个函数里") == 1
    assert "没用到的导入（`F401`） 2 处" in outside
