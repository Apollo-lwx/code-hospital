"""宿主声明了 elicitation 时，确认通过就在同一次调用里继续；拒绝或失败不执行。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.api.capabilities import bind_host, reset_host
from code_hospital.core.questions import ask, present
from code_hospital.departments import reception, scanner
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class _Caps:
    elicitation = object()


class _Answer:
    def __init__(self, choice: str, text: str = ""):
        self.choice = choice
        self.text = text


class _Result:
    def __init__(self, action: str, data=None):
        self.action = action
        self.data = data


class Host:
    def __init__(self, action="accept", choice="confirm", text="", boom=False):
        self.client_capabilities = _Caps()
        self.protocol_version = "2026-07-28"
        self.action = action
        self.choice = choice
        self.text = text
        self.boom = boom
        self.calls = 0

    async def elicit(self, message, schema):
        self.calls += 1
        if self.boom:
            raise TimeoutError("slow")
        return _Result(self.action, _Answer(self.choice, self.text))


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


def _repo(path: Path) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    return str(path)


@pytest.fixture(autouse=True)
def quiet_adapters():
    adapters.clear()
    adapters.register(Quiet())
    yield
    adapters.clear()


def _question():
    return ask(
        "low_confidence_diagnosis",
        "有一条待复核。",
        [("adopt", "采用"), ("exclude", "排除"), ("defer", "先停")],
        "排除",
        "hosp_prescribe(low_confidence=\"exclude\")",
    )


async def test_accept_continues_and_pause_or_decline_stops():
    host = Host(choice="adopt")
    token = bind_host(host)
    try:
        presented = await present(_question())
    finally:
        reset_host(token)
    assert presented.stop is None
    assert presented.choice == "adopt"
    assert host.calls == 1

    host = Host(choice="defer")
    token = bind_host(host)
    try:
        presented = await present(_question())
    finally:
        reset_host(token)
    assert presented.stop is not None
    assert presented.stop["question"]["channel"] == "elicitation"
    assert presented.stop["question"]["elicitation_action"] == "accept"

    host = Host(action="decline")
    token = bind_host(host)
    try:
        presented = await present(_question())
    finally:
        reset_host(token)
    assert presented.stop["question"]["elicitation_action"] == "decline"
    assert "degraded" not in presented.stop


async def test_elicit_failure_falls_back_to_convention():
    host = Host(boom=True)
    token = bind_host(host)
    try:
        presented = await present(_question())
    finally:
        reset_host(token)
    assert presented.stop["status"] == "pending_question"
    assert presented.stop["question"]["channel"] == "convention"
    assert presented.stop["degraded"] is True
    assert presented.stop["degraded_reason"]


async def _register(root: str) -> str:
    reg = await reception.register(
        title="当场确认", scope_type="repo", scope={}, project_root=root,
    )
    return reg["case_id"]


async def test_accept_confirm_scans_in_the_same_call(tmp_path: Path):
    root = _repo(tmp_path / "go")
    case_id = await _register(root)
    host = Host(choice="confirm")
    token = bind_host(host)
    try:
        result = await scanner.scan(case_id=case_id, project_root=root)
    finally:
        reset_host(token)
    assert result.get("status") != "pending_question"
    assert Path(result["report_path"]).is_file()
    assert host.calls == 1
    async with Database(root) as db:
        case = await db.get_case(case_id)
    assert case.coverage["host_capabilities"]["channel"] == "elicitation"
    assert "degraded" not in result


async def test_decline_leaves_the_case_registered(tmp_path: Path):
    root = _repo(tmp_path / "stop")
    case_id = await _register(root)
    host = Host(action="cancel")
    token = bind_host(host)
    try:
        result = await scanner.scan(case_id=case_id, project_root=root)
    finally:
        reset_host(token)
    assert result["status"] == "pending_question"
    assert result["question"]["elicitation_action"] == "cancel"
    async with Database(root) as db:
        case = await db.get_case(case_id)
    assert case.status == "registered"
    assert not list(Path(root, ".hospital", "reports", case_id).glob("scan-r1.md"))
