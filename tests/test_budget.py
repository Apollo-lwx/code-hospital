"""预算超限后仪器结果还在，覆盖账写明这次没做模型分析。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.catalog.budget import (
    BUDGET_LINE,
    downgrade_model_items,
    load_limits,
    note_budget,
)
from code_hospital.core.errors import HospitalError
from code_hospital.departments import diagnostician, reception, scanner, treat
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class Many(ScannerAdapter):
    name = "quiet"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="Q001", message=f"quiet {index} " + ("x" * 40),
                file_path="src/a.py", line_start=index, line_end=index, severity="nit",
            )
            for index in range(1, 4)
        ]


def test_yaml_and_env_caps(tmp_path: Path, monkeypatch):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  budget:\n    max_llm_calls: 8\n    max_llm_tokens: 100\n",
        encoding="utf-8",
    )
    loaded = load_limits(str(tmp_path))
    assert loaded.max_calls == 8
    assert loaded.max_tokens == 100
    monkeypatch.setenv("CODE_HOSPITAL_MAX_LLM_CALLS", "1")
    assert load_limits(str(tmp_path)).max_calls == 1
    monkeypatch.setenv("CODE_HOSPITAL_MAX_LLM_CALLS", "-3")
    assert load_limits(str(tmp_path)).max_calls is None


def test_note_budget_records_the_line_only_when_over():
    finding = type("F", (), {"message": "abcd" * 4, "concluded_by": "instrument"})()
    limits = []
    plain = note_budget("", [finding], [], limits, {})
    assert "budget" not in plain
    assert limits == []
    import os
    os.environ["CODE_HOSPITAL_MAX_LLM_CALLS"] = "0"
    try:
        limits = []
        stamped = note_budget("", [finding], [], limits, {})
    finally:
        del os.environ["CODE_HOSPITAL_MAX_LLM_CALLS"]
    assert stamped["budget"]["degraded"] is True
    assert stamped["budget"]["note"] == BUDGET_LINE
    assert limits == [BUDGET_LINE]


def test_downgrade_turns_model_patches_into_manual_work():
    items = [{"method": "llm_patch", "instructions": "补一段", "acceptance": {"alias": "test"}}]
    downgrade_model_items(items)
    assert items[0]["method"] == "manual"
    assert items[0]["acceptance"]["alias"] == ""
    assert items[0]["instructions"].startswith(BUDGET_LINE)


def test_hold_semantic_stops_pending_llm_items():
    from code_hospital.catalog.budget import hold_semantic

    checks = [{
        "id": "diff.issues",
        "status": "open",
        "executors": [
            {"kind": "llm", "status": "pending", "text": "请读"},
            {"kind": "code", "status": "done", "text": "事实"},
        ],
    }]
    hold_semantic(checks)
    assert checks[0]["status"] == "budget"
    assert checks[0]["note"] == BUDGET_LINE
    assert checks[0]["executors"][0]["text"] == BUDGET_LINE
    assert checks[0]["executors"][1]["status"] == "done"
    assert checks[0]["executors"][1]["text"] == "事实"


def _repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    return str(path)


async def test_many_findings_keep_the_scan_and_skip_model_reading(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODE_HOSPITAL_MAX_LLM_CALLS", "1")
    adapters.clear()
    adapters.register(Many())
    root = _repo(tmp_path / "exam")
    reg = await reception.register(
        title="预算", scope_type="repo", scope={}, project_root=root, auto_advance=True,
    )
    scanned = await scanner.scan(case_id=reg["case_id"], project_root=root)
    report = Path(scanned["report_path"]).read_text(encoding="utf-8")
    assert BUDGET_LINE in report
    assert scanned["findings"]["total"] == 3
    diagnosed = await diagnostician.diagnose(
        case_id=reg["case_id"], use_llm=True, project_root=root,
    )
    text = Path(diagnosed["report_path"]).read_text(encoding="utf-8")
    assert BUDGET_LINE in text
    assert "宿主声明将自行增强解读" not in text
    async with Database(root) as db:
        case = await db.get_case(reg["case_id"])
    assert case.coverage["budget"]["degraded"] is True
    assert case.coverage["budget"]["note"] == BUDGET_LINE
    adapters.clear()


async def test_treat_uses_the_same_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODE_HOSPITAL_MAX_LLM_TOKENS", "1")
    adapters.clear()
    adapters.register(Many())
    root = _repo(tmp_path / "auto")
    try:
        await treat.treat(title="自动档预算", scope_type="repo", project_root=root)
    except HospitalError:
        pass
    async with Database(root) as db:
        cases, _total = await db.list_cases(root)
    assert cases
    assert cases[0].coverage["budget"]["degraded"] is True
    assert cases[0].coverage["budget"]["note"] == BUDGET_LINE
    adapters.clear()


async def test_over_budget_does_not_file_semantic_conclusions(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODE_HOSPITAL_MAX_LLM_CALLS", "1")
    adapters.clear()
    adapters.register(Many())
    root = _repo(tmp_path / "semantic")
    reg = await reception.register(
        title="预算语义", scope_type="repo", scope={},
        project_root=root, auto_advance=True, visit_kind="admission",
    )
    scanned = await scanner.scan(case_id=reg["case_id"], project_root=root)
    scan_text = Path(scanned["report_path"]).read_text(encoding="utf-8")
    assert BUDGET_LINE in scan_text
    assert "因预算未做" in scan_text
    diagnosed = await diagnostician.diagnose(
        case_id=reg["case_id"],
        project_root=root,
        check_answers={"diff.issues": "src/a.py:2: 返回值被丢掉"},
    )
    assert "report_path" in diagnosed
    report = Path(diagnosed["report_path"]).read_text(encoding="utf-8")
    assert BUDGET_LINE in report
    assert "宿主已交卷" not in report
    async with Database(root) as db:
        stored = await db.all_findings(reg["case_id"])
        case = await db.get_case(reg["case_id"])
    assert case.status == "diagnosed"
    assert not any(item.rule_id == "diff.issue" for item in stored)
    assert any(item.rule_id == "Q001" for item in stored)
    adapters.clear()
