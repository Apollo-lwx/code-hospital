"""诊断（模板归因）与检验报告确认门禁。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.core.errors import HospitalError
from code_hospital.departments import diagnostician, reception, scanner
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class RuffAdapter(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="F841", message="unused variable",
                file_path="src/a.py", line_start=2, line_end=2, severity="major",
            ),
            RawFinding(
                rule_id="F841", message="unused variable",
                file_path="src/a.py", line_start=2, line_end=2, severity="major",
            ),
        ]


class UnknownAdapter(ScannerAdapter):
    """不在名册里的 critical 发现改挂全科封存。"""

    name = "custom"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="S105", message="possible secret",
                file_path="src/a.py", line_start=3, line_end=3, severity="critical",
                evidence="token = 'abc'",
            ),
        ]


class SecretAdapter(ScannerAdapter):
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


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def f():\n    x = 1\n    return 2\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


def _use(*adapters_to_use: type[ScannerAdapter]):
    adapters.clear()
    for cls in adapters_to_use:
        adapters.register(cls())


async def test_template_diagnosis_clusters_and_fixability(git_repo):
    _use(RuffAdapter, UnknownAdapter)
    reg = await reception.register(
        title="诊断", scope_type="staged", scope={},
        auto_advance=True, project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], project_root=git_repo)
    res = await diagnostician.diagnose(case_id=reg["case_id"], project_root=git_repo)

    assert res["stats"]["total_findings"] == 3
    assert res["stats"]["clusters"] == 2  # 两条 F841 同指纹，合成一簇
    assert res["stats"]["autofix"] == 1
    assert res["stats"]["manual"] == 1
    by_cat = {d["category"]: d for d in res["diagnoses"]}
    assert by_cat["unused-variable"]["fixability"] == "autofix"
    assert by_cat["unused-variable"]["confidence"] >= 0.5
    assert by_cat["unused-variable"]["specialty_id"] == "python"
    assert by_cat["s105"]["fixability"] == "manual"
    assert by_cat["s105"]["specialty_id"] == "general"
    assert by_cat["s105"]["confidence"] == 0.62
    assert "封存" in by_cat["s105"]["root_cause"]
    assert "待复核" not in by_cat["s105"]["root_cause"]
    assert res["handoff"]["stage"] == "diagnosed"
    assert res["handoff"]["awaiting_confirmation"] is False

    report = Path(res["report_path"]).read_text(encoding="utf-8")
    assert "## 问题分组" in report
    assert "建议人工处理" in report
    assert "src/a.py:2" in report

    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        assert case.status == "diagnosed"
        assert case.pending_gate == ""
        assert (await db.case_counts(reg["case_id"]))["diagnoses"] == 2
        stored = await db.all_findings(reg["case_id"])
        f841 = [f for f in stored if f.rule_id == "F841"]
        assert len(f841) == 2
        assert len({f.content_key for f in f841}) == 1
        assert len({f.fingerprint for f in f841}) == 2


async def test_admission_without_filing_stays_scanned(git_repo):
    _use(RuffAdapter)
    reg = await reception.register(
        title="合入未交卷", scope_type="staged", scope={},
        auto_advance=True, visit_kind="admission", project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], project_root=git_repo)
    blocked = await diagnostician.diagnose(case_id=reg["case_id"], project_root=git_repo)
    assert blocked["status"] == "pending_question"
    assert blocked["question"]["kind"] == "diff_filing"
    assert "diff.issues" in blocked["resume_hint"]
    assert "py.checklist.behavior" in blocked["question"]["prompt"]
    assert "未闭合" in blocked["question"]["prompt"]
    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        assert case.status == "scanned"
        assert not any(row.stage == "diagnose" for row in await db.list_reports(reg["case_id"]))


async def test_admission_writes_host_diff_into_the_chart(git_repo):
    _use(RuffAdapter)
    reg = await reception.register(
        title="合入交卷", scope_type="staged", scope={},
        auto_advance=True, visit_kind="admission", project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], project_root=git_repo)
    res = await diagnostician.diagnose(
        case_id=reg["case_id"],
        project_root=git_repo,
        check_answers={
            "diff.issues": "src/a.py:2: 返回值被丢掉",
            "py.checklist.behavior": "没有\n已读：src/a.py",
            "py.checklist.errors": "src/a.py:3: 异常被接住后继续返回",
            "py.checklist.state": "没有\n已读：src/a.py",
            "diff.surface": "没有\n已读：src/a.py",
        },
    )
    by_cat = {d["category"]: d for d in res["diagnoses"]}
    assert by_cat["diff-issue"]["confidence"] < 0.5
    assert "待复核" in by_cat["diff-issue"]["root_cause"]
    assert "写入病历" in res["filing_note"]
    report = Path(res["report_path"]).read_text(encoding="utf-8")
    assert "## 合入交卷" in report
    assert "清单与发现" in report
    assert "py.checklist.errors → " in report
    async with Database(git_repo) as db:
        stored = await db.all_findings(reg["case_id"])
        host = [f for f in stored if f.rule_id == "diff.issue"]
        assert len(host) == 2
        assert {item.line_start for item in host} == {2, 3}
        assert {item.tool for item in host} == {"host"}
        by_line = {item.line_start: item.check_id for item in host}
        assert by_line[2] == "diff.issues"
        assert by_line[3] == "py.checklist.errors"
        case = await db.get_case(reg["case_id"])
        links = case.coverage["filing"]["links"]
        assert links["by_finding"][next(item.id for item in host if item.line_start == 2)] == ["diff.issues"]
        assert any(row["line"] == 3 for row in links["by_check"]["py.checklist.errors"])
        by_id = {row["id"]: row for row in case.coverage["filing"]["items"]}
        assert by_id["diff.issues"]["status"] == "filed"
        assert by_id["py.checklist.behavior"]["status"] == "clear"
        assert by_id["py.checklist.errors"]["status"] == "filed"


async def test_later_round_keeps_the_first_host_issue(git_repo):
    _use(RuffAdapter)
    reg = await reception.register(
        title="分轮交卷", scope_type="staged", scope={},
        auto_advance=True, visit_kind="admission", project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], project_root=git_repo)
    first = await diagnostician.diagnose(
        case_id=reg["case_id"],
        project_root=git_repo,
        check_answers={"diff.issues": "src/a.py:2: 返回值被丢掉"},
    )
    assert first["status"] == "pending_question"
    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        assert case.status == "scanned"
        stored = [item for item in await db.all_findings(reg["case_id"]) if item.rule_id == "diff.issue"]
        assert [(item.line_start, item.message) for item in stored] == [(2, "返回值被丢掉")]
        by_id = {row["id"]: row for row in case.coverage["filing"]["items"]}
        assert by_id["diff.issues"]["status"] == "filed"
    second = await diagnostician.diagnose(
        case_id=reg["case_id"],
        project_root=git_repo,
        check_answers={
            "diff.issues": "src/a.py:2: 返回值被丢掉\nsrc/a.py:3: 异常被接住后继续返回",
            "py.checklist.behavior": "没有\n已读：src/a.py",
            "py.checklist.errors": "没有\n已读：src/a.py",
            "py.checklist.state": "没有\n已读：src/a.py",
            "diff.surface": "没有\n已读：src/a.py",
        },
    )
    assert "病历里共 2 条" in second["filing_note"]
    assert "本轮新写入 1 条" in second["filing_note"]
    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        assert case.status == "diagnosed"
        stored = [item for item in await db.all_findings(reg["case_id"]) if item.rule_id == "diff.issue"]
        assert sorted((item.line_start, item.message) for item in stored) == [
            (2, "返回值被丢掉"),
            (3, "异常被接住后继续返回"),
        ]


async def test_gitleaks_is_manual_secret(git_repo):
    _use(SecretAdapter)
    reg = await reception.register(
        title="密钥", scope_type="staged", scope={},
        auto_advance=True, project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], project_root=git_repo)
    res = await diagnostician.diagnose(case_id=reg["case_id"], project_root=git_repo)
    only = res["diagnoses"][0]
    assert only["category"] == "hardcoded-secret"
    assert only["fixability"] == "manual"
    assert only["confidence"] > 0.5
    assert "待复核" not in only["root_cause"]


async def test_checkup_gate_blocks_diagnose(git_repo):
    _use(SecretAdapter)
    reg = await reception.register(
        title="叫号", scope_type="staged", scope={}, project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], stage_confirmed=True, project_root=git_repo)
    blocked = await diagnostician.diagnose(case_id=reg["case_id"], project_root=git_repo)
    assert blocked["status"] == "pending_question"
    assert blocked["question"]["kind"] == "stage_confirm"
    assert "hosp_diagnose" in blocked["resume_hint"]

    released = await diagnostician.diagnose(
        case_id=reg["case_id"], stage_confirmed=True, project_root=git_repo,
    )
    assert released["stats"]["clusters"] == 1
    assert released["handoff"]["awaiting_confirmation"] is True
    assert released["handoff"]["next_tool"] == "hosp_prescribe"
    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        assert case.status == "diagnosed"
        assert case.pending_gate == "diagnosed"


async def test_diagnose_requires_scan(git_repo):
    reg = await reception.register(
        title="未检", scope_type="staged", scope={},
        auto_advance=True, project_root=git_repo,
    )
    with pytest.raises(HospitalError) as exc:
        await diagnostician.diagnose(case_id=reg["case_id"], project_root=git_repo)
    assert exc.value.code == "InvalidOperation"
