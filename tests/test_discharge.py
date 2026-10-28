"""复诊差分、再入院与出院门禁。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.core.errors import HospitalError
from code_hospital.departments import (
    diagnostician,
    discharge,
    pharmacist,
    prescriber,
    reception,
    scanner,
)
from code_hospital.departments.pharmacist import _ruff_binary
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class Scripted(ScannerAdapter):
    binary = "git"
    extensions = frozenset({".py"})

    def __init__(self, name: str, rounds: list[list[RawFinding]]):
        self.name = name
        self.rounds = rounds

    async def scan(self, repo_root, files, timeout_ms):
        if not self.rounds:
            return []
        if len(self.rounds) == 1:
            return self.rounds[0]
        return self.rounds.pop(0)


def _f841():
    return RawFinding(
        rule_id="F841", message="unused", file_path="src/a.py",
        line_start=2, line_end=2, severity="major",
    )


def _secret():
    return RawFinding(
        rule_id="generic-api-key", message="leaked", file_path="src/a.py",
        line_start=2, line_end=2, severity="critical",
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def f():\n    x = 1\n    return 2\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


def _use(*items: ScannerAdapter) -> None:
    adapters.clear()
    for item in items:
        adapters.register(item)


async def _ready(git_repo: str, *items: ScannerAdapter, user: bool = False) -> str:
    _use(*items)
    reg = await reception.register(
        title="出院", scope_type="staged", scope={},
        auto_advance=False, project_root=git_repo,
    )
    case_id = reg["case_id"]
    await scanner.scan(case_id=case_id, stage_confirmed=True, project_root=git_repo)
    await diagnostician.diagnose(case_id=case_id, stage_confirmed=True, project_root=git_repo)
    draft = await prescriber.prescribe(
        case_id=case_id, stage_confirmed=True, acceptance_decision="skip",
        project_root=git_repo,
    )
    if draft.get("status") == "pending_question":
        draft = await prescriber.prescribe(
            case_id=case_id, stage_confirmed=True, low_confidence="exclude",
            acceptance_decision="skip", project_root=git_repo,
        )
    assert draft["status"] == "draft"
    approved = await prescriber.prescribe(
        case_id=case_id, approve=True,
        approver="user" if user else "host_agent",
        user_confirmed=user, project_root=git_repo,
    )
    assert approved["status"] == "approved"
    return case_id, approved["prescription_id"]


async def test_resolved_rescan_discharges_cured(git_repo):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    device = Scripted("ruff", [[_f841()], []])
    case_id, rx_id = await _ready(git_repo, device)
    done = await pharmacist.dispense(prescription_id=rx_id, project_root=git_repo)
    assert done["status"] == "applied"
    result = await discharge.discharge(case_id=case_id, project_root=git_repo)
    assert result["status"] == "discharged"
    assert result["outcome"] == "cured"
    assert result["gate"]["verdict"] == "pass"
    assert len(result["diff"]["resolved"]) == 1
    assert Path(result["summary_path"]).is_file()
    assert Path(result["eval_set"]).is_file()
    saved = json.loads(Path(result["eval_set"]).read_text(encoding="utf-8"))
    assert saved["samples"][0]["case_id"] == case_id
    assert saved["samples"][0]["resolved"]
    assert Path(result["follow_up"]["ci_snippet_path"]).is_file()
    async with Database(git_repo) as db:
        case = await db.get_case(case_id)
        assert case.status == "discharged"


async def test_new_related_readmits(git_repo):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    fresh = RawFinding(
        rule_id="E501", message="long", file_path="src/a.py",
        line_start=2, line_end=2, severity="major",
    )
    device = Scripted("ruff", [[_f841()], [fresh]])
    case_id, rx_id = await _ready(git_repo, device)
    await pharmacist.dispense(prescription_id=rx_id, project_root=git_repo)
    result = await discharge.discharge(case_id=case_id, project_root=git_repo)
    assert result["status"] == "readmitted"
    assert result["round"] == 2
    assert result["diff"]["new_related"]
    assert "gate" not in result
    async with Database(git_repo) as db:
        case = await db.get_case(case_id)
        assert case.status == "scanned"
        assert case.pending_gate == "scanned"


async def test_critical_unrelated_asks_then_waive_needs_user(git_repo):
    fresh = RawFinding(
        rule_id="generic-api-key", message="another", file_path="src/b.py",
        line_start=1, line_end=1, severity="critical",
    )
    device = Scripted("gitleaks", [[_secret()], [fresh]])
    case_id, rx_id = await _ready(git_repo, device, user=True)
    skipped = await pharmacist.dispense(prescription_id=rx_id, project_root=git_repo)
    assert skipped["status"] == "skipped"
    asked = await discharge.discharge(case_id=case_id, project_root=git_repo)
    assert asked["question"]["kind"] == "gate_waive"
    with pytest.raises(HospitalError) as exc:
        await discharge.discharge(case_id=case_id, waive=True, project_root=git_repo)
    assert exc.value.code == "Forbidden"
    closed = await discharge.discharge(
        case_id=case_id, waive=True, waiver_user_confirmed=True,
        waive_reason="用户接受残留风险", project_root=git_repo,
    )
    assert closed["status"] == "discharged"
    assert closed["gate"]["verdict"] == "waived"


async def test_pending_item_blocks_discharge(git_repo):
    device = Scripted("gitleaks", [[_secret()]])
    case_id, _rx_id = await _ready(git_repo, device, user=True)
    with pytest.raises(HospitalError) as exc:
        await discharge.discharge(case_id=case_id, project_root=git_repo)
    assert exc.value.code == "InvalidOperation"
