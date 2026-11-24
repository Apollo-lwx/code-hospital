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


class RenameAdapter(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="N802", message="Function name `BadName` should be lowercase",
            file_path="src/named.py", line_start=1, line_end=1, severity="minor",
        )]


class _AbsentPool:
    """LSP 缺席的确定性桩：守门一律回退文本扫描（同 test_rename.py 的约定）。"""

    def __init__(self, *args, **kwargs):
        pass

    def cross_file_refs(self, file_path, line, symbol):
        return None

    def notes(self):
        return []

    def close(self):
        pass


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


async def test_advisory_item_cannot_be_dispensed(git_repo, monkeypatch):
    """告知建议项批准时未点名：药房拒抓，指路 include_advisory。"""
    monkeypatch.setattr("code_hospital.lsp.pool.Pool", _AbsentPool)
    (Path(git_repo) / "src" / "named.py").write_text(
        "def BadName():\n    return 1\n", encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    _use(RenameAdapter)
    reg = await reception.register(
        title="告知", scope_type="staged", scope={},
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
            case_id=case_id, stage_confirmed=True, low_confidence="adopt",
            acceptance_decision="skip", project_root=git_repo,
        )
    assert draft["status"] == "draft"
    advisory = next(it for it in draft["items"] if it["status"] == "advisory")
    approved = await prescriber.prescribe(
        case_id=case_id, approve=True, approver="host_agent", project_root=git_repo,
    )
    assert approved["status"] == "approved"
    with pytest.raises(HospitalError) as exc:
        await pharmacist.dispense(
            prescription_id=draft["prescription_id"], item_id=advisory["id"],
            mode="lsp_rename", new_name="bad_name", project_root=git_repo,
        )
    assert exc.value.code == "InvalidOperation"
    assert "告知建议项" in str(exc.value)
    assert "include_advisory" in str(exc.value.next_action)
    # 拒抓不改代码
    assert "BadName" in (Path(git_repo) / "src" / "named.py").read_text(encoding="utf-8")


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


# ---------- Java 成药执行器（semgrep --autofix）----------

_JAVA_SOURCE = "class A {\n    Integer x = new Integer(5);\n}\n"


class JavaPackFixAdapter(ScannerAdapter):
    """java-pack 检出可机械修的弃用构造器（规范类 → 告知级）。"""

    name = "java-pack"
    binary = "git"
    extensions = frozenset({".java"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="java.boxing-constructor", message="包装类构造器已弃用",
            file_path="src/A.java", line_start=2, line_end=2, severity="minor",
        )]


class JavaLineAdapter(ScannerAdapter):
    """java-review 检出行长（清创在册但 java-pack 没有这条规则的机械修法）。"""

    name = "java-review"
    binary = "git"
    extensions = frozenset({".java"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="java.line-length", message="这一行有 121 个字符，超过 120",
            file_path="src/A.java", line_start=2, line_end=2, severity="minor",
        )]


def _seed_java_repo(git_repo: str) -> None:
    (Path(git_repo) / "src" / "A.java").write_text(_JAVA_SOURCE, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)


async def _java_prescription(git_repo: str, adapter, monkeypatch) -> tuple[str, dict]:
    """跑到批准后的处方，返回 (prescription_id, 被点名的告知项)。"""
    monkeypatch.setattr("code_hospital.lsp.pool.Pool", _AbsentPool)
    _use(adapter)
    reg = await reception.register(
        title="java 成药", scope_type="staged", scope={},
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
            case_id=case_id, stage_confirmed=True, low_confidence="adopt",
            acceptance_decision="skip", project_root=git_repo,
        )
    assert draft["status"] == "draft"
    advisory = next(it for it in draft["items"] if it["status"] == "advisory")
    approved = await prescriber.prescribe(
        case_id=case_id, approve=True, approver="host_agent",
        include_advisory=[advisory["id"]], project_root=git_repo,
    )
    assert approved["status"] == "approved"
    return draft["prescription_id"], advisory


async def test_java_autofix_applies_semgrep_fix(git_repo, monkeypatch):
    """桩 semgrep 就地改写，走通成药全管线（快照/裁剪/验收/落账）。"""
    _seed_java_repo(git_repo)
    rx_id, advisory = await _java_prescription(git_repo, JavaPackFixAdapter, monkeypatch)

    async def fake_autofix(repo_root, files, instrument_id, timeout_s):
        assert instrument_id == "java-pack"
        path = Path(repo_root) / "src" / "A.java"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("new Integer(5)", "Integer.valueOf(5)"), encoding="utf-8")

    monkeypatch.setattr("code_hospital.catalog.packs.semgrep_available", lambda: True)
    monkeypatch.setattr("code_hospital.catalog.packs.autofix_pack", fake_autofix)

    res = await pharmacist.dispense(
        prescription_id=rx_id, item_id=advisory["id"], project_root=git_repo,
    )
    assert res["status"] == "applied"
    assert res["dose"]["executor"] == "semgrep --autofix"
    assert res["dose"]["result"] == "success"
    text = (Path(git_repo) / "src" / "A.java").read_text(encoding="utf-8")
    assert "Integer.valueOf(5)" in text
    assert "new Integer" not in text


async def test_java_autofix_without_semgrep_fails(git_repo, monkeypatch):
    """semgrep 不在岗：成药失败留痕，文件不动。"""
    _seed_java_repo(git_repo)
    rx_id, advisory = await _java_prescription(git_repo, JavaPackFixAdapter, monkeypatch)
    monkeypatch.setattr("code_hospital.catalog.packs.semgrep_available", lambda: False)

    res = await pharmacist.dispense(
        prescription_id=rx_id, item_id=advisory["id"], project_root=git_repo,
    )
    assert res["status"] == "failed"
    assert "semgrep" in res["dose"]["error"]
    assert (Path(git_repo) / "src" / "A.java").read_text(encoding="utf-8") == _JAVA_SOURCE


async def test_java_autofix_rejects_rule_without_fix(git_repo, monkeypatch):
    """规则在清创适应证里但包里没有 fix：明说没有机械修法，不空跑。"""
    _seed_java_repo(git_repo)
    rx_id, advisory = await _java_prescription(git_repo, JavaLineAdapter, monkeypatch)
    monkeypatch.setattr("code_hospital.catalog.packs.semgrep_available", lambda: True)

    res = await pharmacist.dispense(
        prescription_id=rx_id, item_id=advisory["id"], project_root=git_repo,
    )
    assert res["status"] == "failed"
    assert "没有机械修法" in res["dose"]["error"]
    assert (Path(git_repo) / "src" / "A.java").read_text(encoding="utf-8") == _JAVA_SOURCE


def test_autofix_engine_dispatch_by_extension():
    assert pharmacist._autofix_engine({"scope_paths": ["src/A.java"]}) == "semgrep"
    assert pharmacist._autofix_engine({"scope_paths": ["src/a.py"]}) == "ruff"
    assert pharmacist._autofix_engine({"scope_paths": ["src/a.ts"]}) == "eslint"
    assert pharmacist._autofix_engine({"scope_paths": [], "target_file": "a.jsx"}) == "eslint"


def test_fixable_rules_reads_pack():
    from code_hospital.catalog.packs import fixable_rules

    rules = fixable_rules("java-pack")
    assert {"java.boxing-constructor", "java.literal-equals-first"} <= rules
    # 没有 fix 的规则不在其列
    assert "java.null-deref" not in rules
    assert fixable_rules("no-such-pack") == set()
