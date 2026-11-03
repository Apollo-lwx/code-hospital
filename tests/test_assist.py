"""每个阶段和每个检查步骤都留宿主辅助位。不交不挡流程，交了不改仪器状态。"""

import json
import shutil
import subprocess

import pytest

from code_hospital.catalog.checks import _result, run_checks
from code_hospital.core.assist import attach_ai
from code_hospital.core.models import Case, Scope
from code_hospital.departments import reception
from code_hospital.storage.reports import render_scan_report


def test_stage_result_keeps_a_host_slot(tmp_path):
    report = tmp_path / ".hospital" / "reports" / "case_x" / "scan-r1.md"
    report.parent.mkdir(parents=True)
    report.write_text("# 检验报告\n", encoding="utf-8")
    payload = attach_ai(
        {
            "case_id": "case_x",
            "report_path": str(report),
            "handoff": {"stage": "scanned", "next_tool": "hosp_diagnose"},
            "checks": [{
                "id": "arch.map",
                "question": "当前架构是什么",
                "status": "done",
                "executors": [{"kind": "code", "status": "done", "text": "模块：api"}],
            }],
        },
        assist={"note": "api 是入口，还要读调用方", "arch.map": "只有一层路由"},
        project_root=str(tmp_path),
    )
    assert payload["checks"][0]["status"] == "done"
    assert payload["ai"]["stage"] == "scanned"
    assert payload["ai"]["optional"] is True
    assert "交卷契约" in payload["ai"]["file_via"]
    assert "仓库相对路径" in payload["ai"]["output"]["issues"][0]["file"]
    assert "referrals" in payload["ai"]["output"]["issues"][0]
    assert payload["ai"]["filed"]["note"]["status"] == "invalid"
    assert payload["ai"]["filed"]["arch.map"]["status"] == "invalid"
    assert payload["ai"]["filed"]["note"]["issues"] == []
    stored = json.loads((tmp_path / ".hospital" / "assist" / "case_x.json").read_text(encoding="utf-8"))
    assert stored["scanned"]["arch.map"]["status"] == "invalid"
    text = report.read_text(encoding="utf-8")
    assert "宿主辅助" not in text
    assert "api 是入口" not in text
    again = attach_ai(payload, project_root=str(tmp_path))
    assert again["ai"]["filed"]["note"]["status"] == "invalid"


def test_check_step_assist_does_not_change_status():
    row = _result(
        {"id": "arch.map", "question": "当前架构是什么", "artifact": "结构名称", "needs": []},
        "done",
        [{"kind": "code", "status": "done", "text": "模块：api"}],
        "",
    )
    assert row["status"] == "done"
    assert row["assist"]["optional"] is True
    assert row["assist"]["role"] == "host_llm"
    assert row["executors"] == [{"kind": "code", "status": "done", "text": "模块：api"}]


def test_checkup_steps_all_offer_assist(tmp_path):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    rows, _notes = run_checks(str(tmp_path), ["app.py"])
    assert rows
    assert all(row["assist"]["role"] == "host_llm" for row in rows)


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
async def test_register_returns_the_host_slot(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("print(1)\n", encoding="utf-8")
    result = await reception.register(
        title="辅助位",
        scope_type="repo",
        scope={},
        auto_advance=True,
        project_root=str(tmp_path),
        assist={"note": "这次先看入口"},
    )
    assert result["ai"]["stage"] == "registered"
    assert result["ai"]["filed"]["note"]["status"] == "invalid"
    assert result["ai"]["filed"]["note"]["issues"] == []
    assert result["handoff"]["next_tool"] == "hosp_scan"
    assert (tmp_path / ".hospital" / "assist" / f"{result['case_id']}.json").is_file()


def test_assist_lines_become_model_findings(tmp_path):
    from code_hospital.core.assist import assist_scope, model_assisted, model_findings
    from code_hospital.core.models import source_summary

    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    with assist_scope({
        "note": "章程还要读上下文",
        "hits": "app.py:1: 这里把已有类型抹掉了",
    }):
        assert model_assisted() is True
        rows = model_findings("case_x", "rpt_x", str(tmp_path), ["app.py"])
    assert len(rows) == 1
    assert rows[0].concluded_by == "host_llm"
    assert rows[0].file_path == "app.py"
    assert rows[0].line_start == 1
    assert source_summary(rows) == "仪器 0 条章程 / 模型 1 条"


def test_object_contract_is_kept_and_prose_is_not(tmp_path):
    from code_hospital.core.assist import assist_scope, format_notes, model_findings

    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    with assist_scope({
        "note": "我觉得这里有问题，先记一笔",
        "hits": {
            "issues": [{"file": "app.py", "line": 1, "message": "返回值没有用上"}],
            "read": ["app.py"],
        },
    }):
        rows = model_findings("case_x", "rpt_x", str(tmp_path), ["app.py"])
        notes = format_notes()
    assert len(rows) == 1
    assert rows[0].file_path == "app.py"
    assert rows[0].message == "返回值没有用上"
    assert rows[0].concluded_by == "host_llm"
    assert any(item.startswith("note ") and "不能算产物" in item for item in notes)
    assert all("我觉得这里有问题" not in item for item in notes)


def test_report_prints_step_assist():
    case = Case(
        id="case_x", project_root=".", title="示例", scope_type="repo",
        scope=Scope(), triage_level="checkup", status="scanned",
        created_at=1_700_000_000_000, updated_at=1_700_000_000_000,
    )
    checks = [{
        "id": "arch.map",
        "question": "当前架构和设计是什么",
        "artifact": "结构名称",
        "needs": [],
        "status": "open",
        "note": "",
        "executors": [],
        "assist": {
            "filed": {
                "status": "held",
                "issues": [{"file": "app.py", "line": 1, "message": "入口把类型抹掉了"}],
                "note": "还有文件没列入已读（lib.py），不能诊断。",
            },
        },
    }]
    md = render_scan_report(case, "r1", [], [], [], checks=checks)
    assert "app.py:1: 入口把类型抹掉了" in md
    assert "还有文件没列入已读（lib.py），不能诊断。" in md
    assert "宿主辅助" not in md
