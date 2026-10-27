"""开方：诊断书确认门禁、低置信排除、验收跳过、高危真人审批。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.core.errors import HospitalError
from code_hospital.departments import diagnostician, prescriber, reception, scanner
from code_hospital.integrations import rsi as rsi_bridge
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class RuffAdapter(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="F841", message="unused", file_path="src/a.py",
            line_start=2, line_end=2, severity="major",
        )]


class UnknownAdapter(ScannerAdapter):
    name = "custom"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="S105", message="maybe secret", file_path="src/a.py",
            line_start=3, line_end=3, severity="minor", evidence="token = 'abc'",
        )]


class SecretAdapter(ScannerAdapter):
    name = "gitleaks"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="generic-api-key", message="leaked key", file_path="src/a.py",
            line_start=2, line_end=2, severity="critical",
        )]


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    x = 1\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


def _use(*cls: type[ScannerAdapter]) -> None:
    adapters.clear()
    for item in cls:
        adapters.register(item())


async def _ready(git_repo: str, *cls: type[ScannerAdapter], auto: bool = True) -> str:
    _use(*cls)
    reg = await reception.register(
        title="开方", scope_type="staged", scope={},
        auto_advance=auto, project_root=git_repo,
    )
    await scanner.scan(
        case_id=reg["case_id"], stage_confirmed=not auto, project_root=git_repo,
    )
    await diagnostician.diagnose(
        case_id=reg["case_id"], stage_confirmed=not auto, project_root=git_repo,
    )
    return reg["case_id"]


async def test_draft_carries_rsi_prohibitions(git_repo, monkeypatch):
    async def fake(_root, task):
        assert "用药禁忌" in task
        return {
            "prohibitions": [{"title": "禁止 eval", "content": "不要用 eval"}],
            "efficacy": [{"title": "疗效账", "content": "F401 autofix 成功 4 次，失败 1 次"}],
        }

    monkeypatch.setattr("code_hospital.integrations.rsi.recall_for_prescribe", fake)
    case_id = await _ready(git_repo, SecretAdapter)
    draft = await prescriber.prescribe(case_id=case_id, project_root=git_repo)
    assert "rsi_prohibitions" in draft, list(draft)
    assert draft["rsi_prohibitions"] == [{"title": "禁止 eval", "content": "不要用 eval"}]
    assert draft["rsi_efficacy"][0]["title"] == "疗效账"


async def test_diagnosis_gate_blocks_prescribe(git_repo):
    case_id = await _ready(git_repo, SecretAdapter, auto=False)
    blocked = await prescriber.prescribe(case_id=case_id, project_root=git_repo)
    assert blocked["question"]["kind"] == "stage_confirm"
    assert "hosp_prescribe" in blocked["resume_hint"]

    draft = await prescriber.prescribe(
        case_id=case_id, stage_confirmed=True, project_root=git_repo,
    )
    assert draft["status"] == "draft"
    assert draft["risk_level"] == "high"
    assert draft["items"][0]["method"] == "manual"
    assert draft["handoff"]["awaiting_confirmation"] is True
    assert "token" not in draft["confirmation_prompt"]
    async with Database(git_repo) as db:
        case = await db.get_case(case_id)
        assert case.status == "prescribed"
        assert case.pending_gate == ""


async def test_low_confidence_excluded_then_low_risk_approved(git_repo):
    case_id = await _ready(git_repo, RuffAdapter, UnknownAdapter)
    asked = await prescriber.prescribe(case_id=case_id, project_root=git_repo)
    assert asked["question"]["kind"] == "low_confidence_diagnosis"

    whitelist = await prescriber.prescribe(
        case_id=case_id, low_confidence="exclude", project_root=git_repo,
    )
    assert whitelist["question"]["kind"] == "acceptance_whitelist"

    draft = await prescriber.prescribe(
        case_id=case_id, low_confidence="exclude", acceptance_decision="skip",
        project_root=git_repo,
    )
    assert draft["risk_level"] == "low"
    assert [it["method"] for it in draft["items"]] == ["autofix"]
    assert draft["items"][0]["acceptance"]["expect"].endswith("skipped")
    assert draft["approval_policy"]["host_agent_ok"] is True
    assert "abc" not in draft["confirmation_prompt"]

    approved = await prescriber.prescribe(
        case_id=case_id, approve=True, approver="host_agent", project_root=git_repo,
    )
    assert approved["status"] == "approved"
    assert approved["human_in_loop"] is False
    assert approved["handoff"]["next_tool"] == "hosp_dispense"
    async with Database(git_repo) as db:
        case = await db.get_case(case_id)
        assert case.status == "dispensing"
        assert (await db.case_counts(case_id))["prescriptions"] == 1
        assert (await db.case_counts(case_id))["items_total"] == 1


async def test_high_risk_needs_user_then_reject(git_repo):
    case_id = await _ready(git_repo, SecretAdapter)
    draft = await prescriber.prescribe(case_id=case_id, project_root=git_repo)
    assert draft["approval_policy"]["user_required_items"]

    asked = await prescriber.prescribe(
        case_id=case_id, approve=True, approver="host_agent", project_root=git_repo,
    )
    assert asked["question"]["kind"] == "high_risk_approval"

    rejected = await prescriber.prescribe(
        case_id=case_id, approve=True, decision="reject", project_root=git_repo,
    )
    assert rejected["status"] == "rejected"
    async with Database(git_repo) as db:
        assert (await db.get_case(case_id)).status == "prescribed"


async def test_user_can_approve_high_risk(git_repo):
    case_id = await _ready(git_repo, SecretAdapter)
    await prescriber.prescribe(case_id=case_id, project_root=git_repo)
    approved = await prescriber.prescribe(
        case_id=case_id, approve=True, approver="user", user_confirmed=True,
        project_root=git_repo,
    )
    assert approved["status"] == "approved"
    assert approved["human_in_loop"] is True
    assert approved["approved_by"] == "user"
    report = Path(approved["report_path"]).read_text(encoding="utf-8")
    assert "⚠" in report and "建议人工处理" in report


async def test_downgrade_skips_high_risk_items(git_repo):
    case_id = await _ready(git_repo, RuffAdapter, SecretAdapter)
    await prescriber.prescribe(
        case_id=case_id, acceptance_decision="skip", project_root=git_repo,
    )
    approved = await prescriber.prescribe(
        case_id=case_id, approve=True, decision="downgrade",
        approver="host_agent", project_root=git_repo,
    )
    assert approved["status"] == "approved"
    assert approved["human_in_loop"] is False
    async with Database(git_repo) as db:
        rx = await db.latest_prescription(case_id)
        by_method = {it["method"]: it["status"] for it in rx["items"]}
        assert by_method["autofix"] == "pending"
        assert by_method["manual"] == "skipped"


async def test_prescribe_requires_diagnosis(git_repo):
    _use(RuffAdapter)
    reg = await reception.register(
        title="未诊", scope_type="staged", scope={},
        auto_advance=True, project_root=git_repo,
    )
    with pytest.raises(HospitalError) as exc:
        await prescriber.prescribe(case_id=reg["case_id"], project_root=git_repo)
    assert exc.value.code == "InvalidOperation"
