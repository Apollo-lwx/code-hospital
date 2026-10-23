"""验收白名单：只跑已确认的别名，失败则把本味退回快照。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.departments import diagnostician, pharmacist, prescriber, reception, scanner
from code_hospital.departments.pharmacist import _ruff_binary
from code_hospital.safety.acceptance import load_commands, run_alias, save_detected


class RuffFixAdapter(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="F841", message="unused", file_path="src/a.py",
            line_start=2, line_end=2, severity="major",
        )]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


async def test_secret_env_is_not_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "leak-me")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    (tmp_path / "probe.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path('saw').write_text('yes' if os.environ.get('SECRET_KEY') else 'no')\n",
        encoding="utf-8",
    )
    (tmp_path / "code-hospital.yaml").write_text(
        yaml.safe_dump({"acceptance": {"commands": {"lint": "python probe.py"}}}),
        encoding="utf-8",
    )
    result = await run_alias(str(tmp_path), "lint")
    assert result["verdict"] == "pass"
    assert (tmp_path / "saw").read_text(encoding="utf-8") == "no"


async def test_shell_meta_is_not_executed(tmp_path):
    (tmp_path / "code-hospital.yaml").write_text(
        "acceptance:\n  commands:\n    lint: 'python -c \"print(1)\" & calc'\n",
        encoding="utf-8",
    )
    result = await run_alias(str(tmp_path), "lint")
    assert result["verdict"] == "skipped"
    assert not (tmp_path / "pwned").exists()


def test_confirm_keeps_existing_alias(tmp_path):
    (tmp_path / "code-hospital.yaml").write_text(
        "scan_ignore:\n  - secret.py\nacceptance:\n  commands:\n    lint: ruff check\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    saved = save_detected(str(tmp_path), [
        {"alias": "lint", "command": "ruff check --unsafe"},
        {"alias": "test", "command": "pytest -q"},
    ])
    assert saved["lint"] == "ruff check"
    assert saved["test"] == "pytest -q"
    doc = yaml.safe_load((tmp_path / "code-hospital.yaml").read_text(encoding="utf-8"))
    assert doc["scan_ignore"] == ["secret.py"]
    assert load_commands(str(tmp_path))["lint"] == "ruff check"


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    x = 1\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


async def test_failed_acceptance_restores_file(git_repo):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    (Path(git_repo) / "fail.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    (Path(git_repo) / "code-hospital.yaml").write_text(
        yaml.safe_dump({"acceptance": {"commands": {"lint": "python fail.py"}}}),
        encoding="utf-8",
    )
    adapters.clear()
    adapters.register(RuffFixAdapter())
    reg = await reception.register(
        title="验收", scope_type="staged", scope={}, auto_advance=False, project_root=git_repo,
    )
    case_id = reg["case_id"]
    await scanner.scan(case_id=case_id, stage_confirmed=True, project_root=git_repo)
    await diagnostician.diagnose(case_id=case_id, stage_confirmed=True, project_root=git_repo)
    draft = await prescriber.prescribe(
        case_id=case_id, stage_confirmed=True, low_confidence="exclude", project_root=git_repo,
    )
    assert draft["status"] == "draft"
    assert draft["items"][0]["acceptance"]["alias"] == "lint"
    await prescriber.prescribe(
        case_id=case_id, approve=True, approver="host_agent", project_root=git_repo,
    )
    failed = await pharmacist.dispense(prescription_id=draft["prescription_id"], project_root=git_repo)
    assert failed["status"] == "failed"
    assert failed["nursing"]["acceptance"] == "fail"
    assert "x = 1" in (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8")
