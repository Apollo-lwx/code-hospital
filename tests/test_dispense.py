"""成药施治：逐味确认、外购不改代码、煎药不提交、dry_run 还原、冲突续行。"""

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
    pharmacist,
    prescriber,
    reception,
    scanner,
)
from code_hospital.departments.pharmacist import _ruff_binary, _settle_drift

_ORIGINAL = "def f():\n    x = 1\n    return 2\n"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class RuffFixAdapter(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="F841", message="unused", file_path="src/a.py",
            line_start=2, line_end=2, severity="major",
        )]


class SecretAdapter(ScannerAdapter):
    name = "gitleaks"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="generic-api-key", message="leaked", file_path="src/a.py",
            line_start=3, line_end=3, severity="critical",
        )]


class TwoFixAdapter(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="F841", message="unused", file_path="src/a.py",
                line_start=2, line_end=2, severity="major",
            ),
            RawFinding(
                rule_id="F841", message="unused", file_path="src/b.py",
                line_start=2, line_end=2, severity="major",
            ),
        ]


class FixThenPatchAdapter(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="F401", message="unused import", file_path="src/a.py",
                line_start=1, line_end=1, severity="major",
            ),
            RawFinding(
                rule_id="F821", message="undefined", file_path="src/c.py",
                line_start=2, line_end=2, severity="major",
            ),
        ]


class LogicAdapter(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="F821", message="undefined", file_path="src/a.py",
            line_start=2, line_end=2, severity="major",
        )]


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(_ORIGINAL, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


def _use(*cls: type[ScannerAdapter]) -> None:
    adapters.clear()
    for item in cls:
        adapters.register(item())


async def _approved(git_repo: str, *cls: type[ScannerAdapter], user: bool = False) -> str:
    _use(*cls)
    reg = await reception.register(
        title="施治", scope_type="staged", scope={},
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
    return draft["prescription_id"]


async def test_autofix_writes_patch_and_gates_next_item(git_repo):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    rx_id = await _approved(git_repo, RuffFixAdapter, SecretAdapter, user=True)
    source = Path(git_repo) / "src" / "a.py"
    first = await pharmacist.dispense(prescription_id=rx_id, project_root=git_repo)
    assert first["status"] == "applied"
    assert first["dose"]["result"] == "success"
    assert first["nursing"]["verdict"] in {"pass", "warn"}
    assert Path(first["dose"]["patch_path"]).is_file()
    assert "x = 1" not in source.read_text(encoding="utf-8")
    assert first["next_item_id"]
    assert first["handoff"]["awaiting_confirmation"] is True

    blocked = await pharmacist.dispense(prescription_id=rx_id, project_root=git_repo)
    assert blocked["question"]["kind"] == "stage_confirm"
    assert "x = 1" not in source.read_text(encoding="utf-8")

    second = await pharmacist.dispense(
        prescription_id=rx_id, stage_confirmed=True, project_root=git_repo,
    )
    assert second["status"] == "skipped"
    assert second["dose"] is None
    assert second["next_item_id"] == ""
    assert source.read_text(encoding="utf-8").count("return 2") == 1


async def test_dry_run_restores_file(git_repo):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    rx_id = await _approved(git_repo, RuffFixAdapter)
    source = Path(git_repo) / "src" / "a.py"
    preview = await pharmacist.dispense(
        prescription_id=rx_id, dry_run=True, project_root=git_repo,
    )
    assert preview["status"] == "dry_run"
    assert "x = 1" in source.read_text(encoding="utf-8")
    assert preview["diff"]


async def test_llm_patch_does_not_edit(git_repo):
    rx_id = await _approved(git_repo, LogicAdapter)
    before = (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8")
    res = await pharmacist.dispense(prescription_id=rx_id, project_root=git_repo)
    assert res["status"] == "need_patch"
    assert res["patch_token"].startswith("ptk_")
    assert res["attempts_left"] == 3
    assert res["snapshot_hash"].startswith("sha256:")
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == before
    with pytest.raises(HospitalError) as exc:
        await pharmacist.dispense(
            prescription_id=rx_id, mode="submit_patch",
            patch_token=res["patch_token"], snapshot_hash=res["snapshot_hash"],
            patch="不是补丁", project_root=git_repo,
        )
    assert exc.value.code == "InvalidOperation"
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == before
    patch = "\n".join([
        "--- a/src/a.py",
        "+++ b/src/a.py",
        "@@ -1,3 +1,3 @@",
        " def f():",
        "-    x = 1",
        "+    x = 2",
        "     return 2",
    ])
    applied = await pharmacist.dispense(
        prescription_id=rx_id, mode="submit_patch",
        patch_token=res["patch_token"], snapshot_hash=res["snapshot_hash"],
        patch=patch, project_root=git_repo,
    )
    assert applied["status"] == "applied"
    assert "x = 2" in (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8")


async def test_unapproved_prescription_rejected(git_repo):
    _use(RuffFixAdapter)
    reg = await reception.register(
        title="未批", scope_type="staged", scope={},
        auto_advance=True, project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], project_root=git_repo)
    await diagnostician.diagnose(case_id=reg["case_id"], project_root=git_repo)
    draft = await prescriber.prescribe(
        case_id=reg["case_id"], acceptance_decision="skip", project_root=git_repo,
    )
    with pytest.raises(HospitalError) as exc:
        await pharmacist.dispense(prescription_id=draft["prescription_id"], project_root=git_repo)
    assert exc.value.code == "InvalidOperation"


def test_outside_hunk_reverts_and_inside_change_stays():
    old = (
        "def f():\n"
        "    x = 1\n"
        "    return 2\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "def g():\n"
        "    y = 9\n"
        "    return y\n"
    )
    new = (
        "def f():\n"
        "    return 2\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "def g():\n"
        "    return 9\n"
    )
    kept, drifted, reason = _settle_drift("src/a.py", old, new, [[2, 2]])
    assert reason == ""
    assert drifted is True
    assert "    x = 1\n" not in kept
    assert "    return 2\n" in kept
    assert "    y = 9\n" in kept
    assert "    return y\n" in kept


def _anchor_lost(rel, old, new, spans):
    return old, True, "replace 区块被改动，无法定位逆替换锚点"


async def _conflicted(git_repo, monkeypatch):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    monkeypatch.setattr(pharmacist, "_settle_drift", _anchor_lost)
    rx_id = await _approved(git_repo, RuffFixAdapter, SecretAdapter, user=True)
    blocked = await pharmacist.dispense(prescription_id=rx_id, project_root=git_repo)
    assert blocked["status"] == "blocked"
    assert blocked["blocked_on"] == "rollback_conflict"
    source = Path(git_repo) / "src" / "a.py"
    assert "x = 1" not in source.read_text(encoding="utf-8")
    saved = json.loads(next(Path(git_repo).rglob("*.snapshot.json")).read_text(encoding="utf-8"))
    assert saved["src/a.py"] == _ORIGINAL
    return rx_id, blocked["item_id"], source


async def test_verify_only_missing_prescription(git_repo):
    with pytest.raises(HospitalError) as exc:
        await pharmacist.dispense(
            prescription_id="rx_missing", mode="verify_only", project_root=git_repo,
        )
    assert exc.value.code == "NotFound"


async def test_resolve_rejects_unknown_resolution(git_repo, monkeypatch):
    rx_id, item_id, _source = await _conflicted(git_repo, monkeypatch)
    with pytest.raises(HospitalError) as exc:
        await pharmacist.dispense(
            prescription_id=rx_id, item_id=item_id, mode="resolve_conflict",
            resolution="checkout", project_root=git_repo,
        )
    assert exc.value.code == "BadArgument"


async def test_skip_item_restores_snapshot(git_repo, monkeypatch):
    rx_id, item_id, source = await _conflicted(git_repo, monkeypatch)
    asked = await pharmacist.dispense(prescription_id=rx_id, item_id=item_id, project_root=git_repo)
    assert asked["status"] == "pending_question"
    with pytest.raises(HospitalError) as exc:
        await pharmacist.dispense(
            prescription_id=rx_id, item_id=item_id, stage_confirmed=True, project_root=git_repo,
        )
    assert exc.value.code == "InvalidOperation"
    done = await pharmacist.dispense(
        prescription_id=rx_id, item_id=item_id, mode="resolve_conflict",
        resolution="skip_item", project_root=git_repo,
    )
    assert done["status"] == "skipped"
    assert done["dose"]["result"] == "reverted"
    assert done["case_status"] == "dispensing"
    assert source.read_text(encoding="utf-8") == _ORIGINAL
    assert done["next_item_id"]


async def test_manual_resolved_keeps_in_scope_and_rejects_outside(git_repo, monkeypatch):
    rx_id, item_id, source = await _conflicted(git_repo, monkeypatch)
    dirty = source.read_text(encoding="utf-8") + "# outside-scope-marker\n"
    source.write_text(dirty, encoding="utf-8")
    blocked = await pharmacist.dispense(
        prescription_id=rx_id, item_id=item_id, mode="resolve_conflict",
        resolution="manual_resolved", project_root=git_repo,
    )
    assert blocked["blocked_on"] == "rollback_conflict"
    assert source.read_text(encoding="utf-8") == dirty
    source.write_text(dirty.split("# outside-scope-marker\n")[0], encoding="utf-8")
    accepted = await pharmacist.dispense(
        prescription_id=rx_id, item_id=item_id, mode="resolve_conflict",
        resolution="manual_resolved", project_root=git_repo,
    )
    assert accepted["status"] == "applied"
    assert "x = 1" not in source.read_text(encoding="utf-8")
    assert "# outside-scope-marker" not in source.read_text(encoding="utf-8")


async def test_retry_settles_outside_edit(git_repo, monkeypatch):
    rx_id, item_id, source = await _conflicted(git_repo, monkeypatch)
    source.write_text(source.read_text(encoding="utf-8") + "# outside-scope-marker\n", encoding="utf-8")
    same = await pharmacist.dispense(
        prescription_id=rx_id, item_id=item_id, mode="resolve_conflict",
        resolution="retry", project_root=git_repo,
    )
    assert same["blocked_on"] == "rollback_conflict"
    assert source.read_text(encoding="utf-8").endswith("# outside-scope-marker\n")
    monkeypatch.setattr(pharmacist, "_settle_drift", _settle_drift)
    source.write_text(_ORIGINAL, encoding="utf-8")
    idle = await pharmacist.dispense(
        prescription_id=rx_id, item_id=item_id, mode="resolve_conflict",
        resolution="retry", project_root=git_repo,
    )
    assert idle["blocked_on"] == "rollback_conflict"
    assert "没有可重试的差异" in idle["dose"]["error"]
    source.write_text(
        "def f():\n    return 2\n# outside-scope-marker\n", encoding="utf-8",
    )
    done = await pharmacist.dispense(
        prescription_id=rx_id, item_id=item_id, mode="resolve_conflict",
        resolution="retry", project_root=git_repo,
    )
    assert done["status"] == "applied"
    text = source.read_text(encoding="utf-8")
    assert "x = 1" not in text
    assert "# outside-scope-marker" not in text
    assert "return 2" in text


async def test_verify_only_reports_without_writing(git_repo):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    rx_id = await _approved(git_repo, RuffFixAdapter, SecretAdapter, user=True)
    with pytest.raises(HospitalError) as missing:
        await pharmacist.dispense(
            prescription_id=rx_id, mode="verify_only", project_root=git_repo,
        )
    assert missing.value.code == "InvalidOperation"
    applied = await pharmacist.dispense(prescription_id=rx_id, project_root=git_repo)
    assert applied["status"] == "applied"
    source = Path(git_repo) / "src" / "a.py"
    kept = source.read_text(encoding="utf-8")
    verified = await pharmacist.dispense(
        prescription_id=rx_id, item_id=applied["item_id"], mode="verify_only",
        project_root=git_repo,
    )
    assert verified["status"] == "verified"
    assert verified["nursing"]["verdict"] == "pass"
    assert verified["nursing"]["scope_check"] == "pass"
    assert verified["nursing"]["acceptance"] == "skipped"
    assert verified["nursing"]["env_precheck"] == "skipped"
    assert verified["nursing"]["logs"]
    assert source.read_text(encoding="utf-8") == kept
    whole = await pharmacist.dispense(
        prescription_id=rx_id, mode="verify_only", project_root=git_repo,
    )
    assert whole["status"] == "verified"
    assert whole["item_id"] == ""
    assert whole["nursing"]["verdict"] == "pass"

    source.write_text(kept + "# outside-scope-marker\n", encoding="utf-8")
    drifted = await pharmacist.dispense(
        prescription_id=rx_id, item_id=applied["item_id"], mode="verify_only",
        project_root=git_repo,
    )
    assert drifted["status"] == "verified"
    assert drifted["nursing"]["scope_check"] == "fail"
    assert drifted["nursing"]["verdict"] == "fail"
    assert source.read_text(encoding="utf-8").endswith("# outside-scope-marker\n")

    source.write_text("def f():\n    eval(\"x\")\n    return 2\n", encoding="utf-8")
    poisoned = await pharmacist.dispense(
        prescription_id=rx_id, item_id=applied["item_id"], mode="verify_only",
        project_root=git_repo,
    )
    assert poisoned["nursing"]["content_check"] == "fail"
    assert poisoned["nursing"]["verdict"] == "fail"
    assert 'eval("x")' in source.read_text(encoding="utf-8")


def _unused(name: str) -> str:
    return f"def {name}():\n    x = 1\n    return 2\n"


async def test_default_dispense_stops_after_one_autofix(git_repo):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    (Path(git_repo) / "src" / "b.py").write_text(_unused("g"), encoding="utf-8")
    rx_id = await _approved(git_repo, TwoFixAdapter)
    done = await pharmacist.dispense(prescription_id=rx_id, project_root=git_repo)
    assert done["status"] == "applied"
    assert "batch" not in done
    assert "follow_autofix" in done["handoff"]["confirm_hint"]
    changed = [
        path.name
        for path in (Path(git_repo) / "src").glob("*.py")
        if "x = 1" not in path.read_text(encoding="utf-8")
    ]
    assert changed == ["a.py"] or changed == ["b.py"]
    assert len(changed) == 1


async def test_follow_autofix_applies_remaining_and_stops_before_patch(git_repo):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    (Path(git_repo) / "src" / "a.py").write_text("import os\n\ndef f():\n    return 1\n", encoding="utf-8")
    (Path(git_repo) / "src" / "c.py").write_text("def g():\n    return missing\n", encoding="utf-8")
    rx_id = await _approved(git_repo, FixThenPatchAdapter)
    done = await pharmacist.dispense(
        prescription_id=rx_id, follow_autofix=True, project_root=git_repo,
    )
    assert done["status"] == "applied"
    assert done["batch"]["applied"] == 1
    assert done["batch"]["stopped"] == "llm_patch"
    assert "import os" not in (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8")
    assert "missing" in (Path(git_repo) / "src" / "c.py").read_text(encoding="utf-8")


async def test_follow_autofix_applies_both_autofix_items(git_repo):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    (Path(git_repo) / "src" / "b.py").write_text(_unused("g"), encoding="utf-8")
    rx_id = await _approved(git_repo, TwoFixAdapter)
    both = await pharmacist.dispense(
        prescription_id=rx_id, follow_autofix=True, project_root=git_repo,
    )
    assert both["batch"]["applied"] == 2
    assert both["batch"]["stopped"] == "done"
    assert both["batch"]["remaining"] == 0
    assert "x = 1" not in (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8")
    assert "x = 1" not in (Path(git_repo) / "src" / "b.py").read_text(encoding="utf-8")


async def test_follow_autofix_stops_at_cap(git_repo, monkeypatch):
    if not _ruff_binary():
        pytest.skip("需要 ruff")
    (Path(git_repo) / "src" / "b.py").write_text(_unused("g"), encoding="utf-8")
    monkeypatch.setattr(pharmacist, "_FOLLOW_CAP", 1)
    rx_id = await _approved(git_repo, TwoFixAdapter)
    capped = await pharmacist.dispense(
        prescription_id=rx_id, follow_autofix=True, project_root=git_repo,
    )
    assert capped["batch"]["applied"] == 1
    assert capped["batch"]["stopped"] == "limit"
    assert capped["batch"]["remaining"] == 1
    assert "follow_autofix" in capped["handoff"]["confirm_hint"]
