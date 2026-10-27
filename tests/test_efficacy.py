"""疗效账：跳过验收的成功不计入，草稿只展示、不改顺序。"""

import pytest

from code_hospital.core.efficacy import classify_outcome, lesson_text, posterior, tally
from code_hospital.core.models import Case, Diagnosis, Scope
from code_hospital.departments.prescriber import _efficacy_for
from code_hospital.storage.db import Database
from code_hospital.storage.reports import render_prescribe_report


def test_skipped_acceptance_success_is_not_a_cure():
    assert classify_outcome("success", "autofix", {"alias": ""}) is None
    assert classify_outcome("success", "llm_patch", {}) is None
    assert classify_outcome("failed", "autofix", {"alias": ""}) == "failure"
    assert classify_outcome("success", "autofix", {"alias": "lint"}) == "success"
    assert classify_outcome("rollback_conflict", "autofix", {"alias": "lint"}) is None


def test_lesson_text_has_counts_and_no_path():
    text = lesson_text({("F401", "autofix"): (4, 1), ("F841", "autofix"): (0, 0)})
    assert "F401 autofix 成功 4 次，失败 1 次，估计成功率 71%" in text
    assert "F841" not in text
    assert "a.py" not in text
    assert lesson_text({}) == ""


def test_posterior_uses_a_flat_prior():
    assert posterior(0, 0) == 0.5
    assert posterior(4, 1) == pytest.approx(5 / 7)


def test_draft_mentions_history_without_reordering():
    case = Case(
        id="case_x", project_root=".", title="示例", scope_type="repo",
        scope=Scope(), triage_level="checkup", status="prescribed",
        created_at=1_700_000_000_000, updated_at=1_700_000_000_000,
    )
    items = [
        {
            "seq": 1, "method": "llm_patch", "risk": "medium", "target_file": "a.py",
            "target_symbol": "load", "scope_paths": ["a.py"],
            "acceptance": {"expect": "相关测试通过"}, "instructions": "拆开",
            "diagnosis_id": "d1", "formula_id": "split", "parent_formula_id": "",
        },
        {
            "seq": 2, "method": "autofix", "risk": "low", "target_file": "a.py",
            "target_symbol": "", "scope_paths": ["a.py"],
            "acceptance": {"expect": "规则不再报出"}, "instructions": "删导入",
            "diagnosis_id": "d2", "formula_id": "debride", "parent_formula_id": "",
        },
    ]
    plain = render_prescribe_report(case, "rx1", {"status": "draft", "risk_level": "low", "items": items}, "")
    assert "以往疗效" not in plain
    assert plain.index("| 1 |") < plain.index("| 2 |")

    diagnosed = [
        Diagnosis(
            id="d2", case_id=case.id, report_id="r1", fingerprint="d2",
            category="F401", root_cause="说明", severity="major",
            confidence=0.9, fixability="autofix",
        ),
    ]
    history = tally([
        {"rule_id": "F401", "method": "autofix", "result": "success", "acceptance": {"alias": "lint"}},
        {"rule_id": "F401", "method": "autofix", "result": "success", "acceptance": {"alias": "lint"}},
        {"rule_id": "F401", "method": "autofix", "result": "success", "acceptance": {"alias": "lint"}},
        {"rule_id": "F401", "method": "autofix", "result": "success", "acceptance": {"alias": "lint"}},
        {"rule_id": "F401", "method": "autofix", "result": "failed", "acceptance": {"alias": ""}},
        {"rule_id": "F401", "method": "autofix", "result": "success", "acceptance": {"alias": ""}},
    ])
    notes = _efficacy_for(items, diagnosed, history)
    assert notes == [{"rule_id": "F401", "method": "autofix", "success": 4, "failure": 1}]
    told = render_prescribe_report(
        case, "rx1",
        {"status": "draft", "risk_level": "low", "items": items, "efficacy": notes},
        "",
    )
    assert "仍按名册" in told
    assert "成功 4 次，失败 1 次，估计成功率 71%" in told
    assert told.index("以往疗效") < told.index("## 处理清单")
    assert told.index("| 1 |") < told.index("| 2 |")


@pytest.mark.asyncio
async def test_efficacy_rows_come_from_existing_doses(tmp_path):
    case = Case(
        id="case_x", project_root=str(tmp_path), title="示例", scope_type="repo",
        scope=Scope(), triage_level="checkup", status="discharged",
        created_at=1_700_000_000_000, updated_at=1_700_000_000_000,
    )
    diagnosis = Diagnosis(
        id="d1", case_id=case.id, report_id="r1", fingerprint="d1",
        category="F401", root_cause="说明", severity="major",
        confidence=0.9, fixability="autofix",
    )
    item = {
        "id": "rxit1", "seq": 1, "diagnosis_id": "d1", "target_file": "a.py",
        "target_symbol": "", "method": "autofix", "instructions": "删",
        "acceptance": {"alias": "lint", "expect": "规则不再报出"},
        "rollback": "reverse_apply", "scope_paths": ["a.py"], "scope_symbols": [],
        "risk": "low", "touches_manifest": False, "amended_from": "", "status": "applied",
        "formula_id": "debride", "parent_formula_id": "",
    }
    async with Database(str(tmp_path)) as db:
        await db.insert_case(case)
        await db.insert_diagnoses([diagnosis])
        await db.insert_prescription(
            {"id": "rx1", "case_id": case.id, "status": "approved", "risk_level": "low", "created_at": 1},
            [item],
        )
        await db.insert_dose({
            "id": "dose1", "item_id": "rxit1", "round": 1, "executor": "autofix",
            "patch_path": "", "snapshot_hash": "", "diff_stat": "+0/-1",
            "result": "success", "error": "", "created_at": 2,
        })
        rows = await db.list_efficacy_rows()
    assert tally(rows) == {("F401", "autofix"): (1, 0)}
