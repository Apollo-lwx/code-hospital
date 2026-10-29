"""模型结论的准入：只读报告只分区，自动档和施治不把线索写成处方。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.departments import diagnostician, prescriber, reception, scanner
from code_hospital.safety.admission import (
    UNVERIFIED,
    AdmissionRules,
    clue_wording,
    guard_clearance,
    rules_for,
    screen_diagnoses,
    settle_semantic,
)
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class _Diagnosis:
    def __init__(self, diagnosis_id, concluded_by, member_ids, confidence=0.9):
        self.id = diagnosis_id
        self.concluded_by = concluded_by
        self.member_ids = member_ids
        self.confidence = confidence
        self.root_cause = "规则 diff.issue"


class _Finding:
    def __init__(self, file_path, line_start):
        self.file_path = file_path
        self.line_start = line_start


class RuffAdapter(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="F841", message="unused", file_path="src/a.py",
            line_start=2, line_end=2, severity="major",
        )]


def test_default_lanes_and_yaml_override(tmp_path: Path):
    assert rules_for("", "report").clues_only is False
    assert rules_for("", "auto_advance").block_clearance is True
    assert rules_for("", "dispense").require_location is True
    (tmp_path / "code-hospital.yaml").write_text(
        "admission:\n  dispense:\n    clues_only: false\n",
        encoding="utf-8",
    )
    opened = rules_for(str(tmp_path), "dispense")
    assert opened.clues_only is False
    assert opened.require_location is True
    assert rules_for(str(tmp_path), "auto_advance").clues_only is True


def test_screen_keeps_instruments_and_records_host_clues():
    host = _Diagnosis("dia_host", "host_llm", ["f_host"])
    instrument = _Diagnosis("dia_tool", "code", ["f_tool"])
    findings = {
        "f_host": _Finding("src/a.py", 2),
        "f_tool": _Finding("src/a.py", 4),
    }
    kept, drops = screen_diagnoses(
        [host, instrument], findings, rules_for("", "dispense"),
    )
    assert [item.id for item in kept] == ["dia_tool"]
    assert drops[0]["reason"] == "待核实线索不进入处方"
    assert drops[0]["file"] == "src/a.py"
    assert drops[0]["line"] == 2

    loose = AdmissionRules(require_location=True, clues_only=False)
    missing = _Diagnosis("dia_missing", "host_llm", ["f_missing"])
    findings["f_missing"] = _Finding("", 0)
    kept, drops = screen_diagnoses([missing, host], findings, loose)
    assert [item.id for item in kept] == ["dia_host"]
    assert drops[0]["reason"] == "落不到文件和行号，已丢弃"


def test_unlocated_semantic_conclusion_drops_only_on_the_strict_lane():
    missing = _Finding("", 0)
    located = _Finding("src/a.py", 2)
    dropped = _Diagnosis("dia_missing", "host_llm", ["f_missing"])
    kept_rows, drops = settle_semantic(
        [(dropped, [missing])], rules_for("", "auto_advance"),
    )
    assert kept_rows == []
    assert drops[0]["reason"] == "落不到文件和行号，已丢弃"

    stayed = _Diagnosis("dia_host", "host_llm", ["f_host"])
    kept_rows, drops = settle_semantic(
        [(stayed, [located])], rules_for("", "auto_advance"),
    )
    assert drops == []
    assert kept_rows[0][0].root_cause.startswith("待核实线索：")

    report = _Diagnosis("dia_report", "host_llm", ["f_host"])
    kept_rows, drops = settle_semantic([(report, [located])], rules_for("", "report"))
    assert drops == []
    assert kept_rows[0][0].root_cause == "规则 diff.issue"


def test_clearance_and_wording_follow_the_lane():
    note = "宿主已通读本次 diff，并写明没有功能问题。"
    assert guard_clearance(note, rules_for("", "report")) == note
    strict = guard_clearance(note, rules_for("", "auto_advance"))
    assert strict.startswith("宿主已通读")
    assert strict.endswith(UNVERIFIED)
    assert "并写明没有功能问题" in strict
    assert clue_wording("待复核：规则 diff.issue", rules_for("", "auto_advance")).startswith("待核实线索：")
    assert clue_wording("规则 F841", rules_for("", "report")) == "规则 F841"


def _repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("def f():\n    x = 1\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    return str(path)


async def test_auto_advance_clean_filing_is_not_a_pass(tmp_path: Path):
    adapters.clear()
    adapters.register(RuffAdapter())
    root = _repo(tmp_path)
    reg = await reception.register(
        title="通读", scope_type="staged", scope={},
        auto_advance=True, visit_kind="admission", project_root=root,
    )
    clean = "没有\n已读：src/a.py"
    await scanner.scan(case_id=reg["case_id"], project_root=root)
    result = await diagnostician.diagnose(
        case_id=reg["case_id"],
        project_root=root,
        check_answers={
            "diff.issues": clean,
            "py.checklist.behavior": clean,
            "py.checklist.errors": clean,
            "py.checklist.state": clean,
            "diff.surface": clean,
        },
    )
    assert result["filing_note"].startswith("宿主已通读")
    assert UNVERIFIED in result["filing_note"]
    assert all(not item["root_cause"].startswith("待核实线索") for item in result["diagnoses"])
    adapters.clear()


async def test_host_clues_stay_out_of_the_prescription(tmp_path: Path):
    adapters.clear()
    adapters.register(RuffAdapter())
    root = _repo(tmp_path)
    reg = await reception.register(
        title="入方", scope_type="staged", scope={},
        auto_advance=True, visit_kind="admission", project_root=root,
    )
    case_id = reg["case_id"]
    await scanner.scan(case_id=case_id, project_root=root)
    diagnosed = await diagnostician.diagnose(
        case_id=case_id,
        project_root=root,
        check_answers={
            "diff.issues": "src/a.py:2: 返回值被丢掉",
            "py.checklist.behavior": "没有\n已读：src/a.py",
            "py.checklist.errors": "src/a.py:3: 异常被接住后继续返回",
            "py.checklist.state": "没有\n已读：src/a.py",
            "diff.surface": "没有\n已读：src/a.py",
        },
    )
    host_ids = {item["id"] for item in diagnosed["diagnoses"] if item["concluded_by"] == "host_llm"}
    assert host_ids
    assert all(item["root_cause"].startswith("待核实线索：") for item in diagnosed["diagnoses"] if item["id"] in host_ids)
    async with Database(root) as db:
        admitted = await db.get_case(case_id)
    assert admitted.coverage["admission"]["lane"] == "auto_advance"
    assert admitted.coverage["admission"]["semantic"] == len(host_ids)
    assert admitted.coverage["admission"]["dropped"] == []
    draft = await prescriber.prescribe(
        case_id=case_id, low_confidence="exclude", acceptance_decision="skip", project_root=root,
    )
    assert host_ids.isdisjoint({item["diagnosis_id"] for item in draft["items"]})
    assert draft["admission_drops"]
    assert any(row["reason"] == "待核实线索不进入处方" for row in draft["admission_drops"])
    async with Database(root) as db:
        case = await db.get_case(case_id)
    assert case.coverage["admission_drops"]
    adapters.clear()
