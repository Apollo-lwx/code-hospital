"""自动档：煎药停住等补丁，高危降级后不改代码。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.core.errors import HospitalError
from code_hospital.departments import treat

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")

_SOURCE = "def f():\n    x = 1\n    return 2\n"


class _Logic(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="F821", message="undefined", file_path="src/a.py",
            line_start=2, line_end=2, severity="major",
        )]


class _Secret(ScannerAdapter):
    name = "gitleaks"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="generic-api-key", message="leaked", file_path="src/a.py",
            line_start=3, line_end=3, severity="critical",
        )]


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(_SOURCE, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    adapters.clear()
    yield str(tmp_path)
    adapters.clear()


async def test_treat_timeout_suspends_before_scan(git_repo: str):
    adapters.register(_Logic())
    seen: list[str] = []

    async def spy(amount: float, total: float | None, message: str) -> None:
        seen.append(message)

    opened = await treat.treat(
        title="超时", scope_type="staged", project_root=git_repo, timeout_ms=0, progress=spy,
    )
    assert opened["status"] == "suspended"
    assert opened["stage"] == "scanning"
    assert opened["progress"] == {"applied": 0, "total": 0}
    assert seen == ["scanning"]
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == _SOURCE
    again = await treat.treat(
        case_id=opened["case_id"], resume=True, project_root=git_repo, timeout_ms=0,
    )
    assert again["status"] == "suspended"
    assert again["stage"] == "scanning"


async def test_resume_requires_case_id():
    with pytest.raises(HospitalError) as exc:
        await treat.treat(resume=True)
    assert exc.value.code == "BadArgument"


async def test_treat_pauses_for_llm_patch(git_repo: str):
    adapters.register(_Logic())
    seen: list[str] = []

    async def spy(amount: float, total: float | None, message: str) -> None:
        seen.append(message)

    opened = await treat.treat(
        title="煎药", scope_type="staged", project_root=git_repo, progress=spy,
    )
    assert opened["status"] == "need_patch"
    assert opened["need_patch"]["status"] == "need_patch"
    assert opened["need_patch"]["patch_token"]
    assert "scanning" in seen
    assert any(message.startswith("dispensing:") for message in seen)
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == _SOURCE
    again = await treat.treat(case_id=opened["case_id"], resume=True, project_root=git_repo)
    assert again["status"] == "need_patch"
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == _SOURCE


async def test_treat_downgrades_high_risk_without_editing(git_repo: str):
    adapters.register(_Secret())
    opened = await treat.treat(title="高危", scope_type="staged", project_root=git_repo)
    assert opened["status"] in {"discharged", "readmitted", "pending_question"}
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == _SOURCE


async def test_treat_holds_prescription_when_auto_approve_is_off(git_repo: str):
    adapters.register(_Logic())
    opened = await treat.treat(
        title="待批", scope_type="staged", project_root=git_repo, auto_approve_low_risk=False,
    )
    assert opened["status"] == "suspended"
    assert opened["stage"] == "prescribing"
    assert opened["progress"]["total"] >= 1
    assert opened["progress"]["applied"] == 0
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == _SOURCE
