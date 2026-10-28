"""声明了 elicitation 时，确认参数没有本次回执就拒绝。没声明时约定通道仍放行。"""

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.api.capabilities import EMPTY_GATE, NO_RECEIPT, bind_host, reset_host
from code_hospital.core.errors import HospitalError
from code_hospital.departments import diagnostician, prescriber, reception, scanner
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class _Caps:
    elicitation = object()


class Host:
    client_capabilities = _Caps()
    protocol_version = "2026-07-28"

    def __init__(self):
        self.calls = 0

    async def elicit(self, message, schema):
        self.calls += 1
        raise AssertionError("参数放行不应该再去弹出确认")


class Quiet(ScannerAdapter):
    name = "quiet"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="Q001", message="quiet",
                file_path="src/a.py", line_start=1, line_end=1, severity="nit",
            )
        ]


class Secret(ScannerAdapter):
    name = "gitleaks"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="generic-api-key", message="leaked key",
                file_path="src/a.py", line_start=2, line_end=2, severity="critical",
            )
        ]


@pytest.fixture(autouse=True)
def only_quiet():
    adapters.clear()
    adapters.register(Quiet())
    yield
    adapters.clear()


def _repo(path: Path) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    return str(path)


async def test_stage_flag_without_receipt_is_forbidden(tmp_path: Path):
    root = _repo(tmp_path / "gate")
    reg = await reception.register(
        title="回执", scope_type="repo", scope={}, project_root=root,
    )
    token = bind_host(Host())
    try:
        with pytest.raises(HospitalError) as caught:
            await scanner.scan(
                case_id=reg["case_id"], stage_confirmed=True, project_root=root,
            )
    finally:
        reset_host(token)
    assert caught.value.code == "Forbidden"
    assert caught.value.message == NO_RECEIPT
    assert caught.value.target == "stage_confirmed"
    async with Database(root) as db:
        case = await db.get_case(reg["case_id"])
    assert case.status == "registered"
    assert case.pending_gate == "registered"


async def test_convention_stage_flag_still_scans(tmp_path: Path):
    root = _repo(tmp_path / "plain")
    reg = await reception.register(
        title="约定", scope_type="repo", scope={}, project_root=root,
    )
    result = await scanner.scan(
        case_id=reg["case_id"], stage_confirmed=True, project_root=root,
    )
    assert result.get("status") != "pending_question"
    assert Path(result["report_path"]).is_file()


async def test_empty_gate_confirmation_is_logged(tmp_path: Path, caplog):
    root = _repo(tmp_path / "empty")
    reg = await reception.register(
        title="空门禁", scope_type="repo", scope={}, project_root=root, auto_advance=True,
    )
    with caplog.at_level(logging.WARNING, logger="code_hospital.gate"):
        result = await scanner.scan(
            case_id=reg["case_id"], stage_confirmed=True, project_root=root,
        )
    assert EMPTY_GATE in caplog.text
    assert Path(result["report_path"]).is_file()


async def test_user_confirmed_without_receipt_does_not_approve(tmp_path: Path):
    adapters.clear()
    adapters.register(Secret())
    root = _repo(tmp_path / "rx")
    reg = await reception.register(
        title="高危", scope_type="repo", scope={}, project_root=root, auto_advance=True,
    )
    case_id = reg["case_id"]
    await scanner.scan(case_id=case_id, project_root=root)
    await diagnostician.diagnose(case_id=case_id, project_root=root)
    draft = await prescriber.prescribe(case_id=case_id, project_root=root)
    assert draft["risk_level"] == "high"
    token = bind_host(Host())
    try:
        with pytest.raises(HospitalError) as caught:
            await prescriber.prescribe(
                case_id=case_id, approve=True, approver="user", user_confirmed=True,
                project_root=root,
            )
    finally:
        reset_host(token)
    assert caught.value.code == "Forbidden"
    assert caught.value.target == "user_confirmed"
    async with Database(root) as db:
        rx = await db.latest_prescription(case_id)
    assert rx["status"] == "draft"
