"""阶段确认门禁（设计 §4.11 叫号机制）：默认每阶段产物须用户确认才放行下一阶段。

覆盖：默认挂起拦截 → pending_question 变体结构 → stage_confirmed 放行；
auto_advance 绿色通道直通；handoff 交接块出参；存量库迁移兼容。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.departments import reception, scanner
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class GateAdapter(ScannerAdapter):
    name = "gate"
    binary = "git"  # 借 git 的存在性让 is_available 为 True
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="GATE001", message="gate finding",
                file_path="src/a.py", line_start=2, line_end=2, severity="minor",
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


@pytest.fixture(autouse=True)
def gate_adapters():
    adapters.clear()
    adapters.register(GateAdapter())
    yield
    adapters.clear()


async def test_default_register_parks_at_gate(git_repo):
    """默认挂号后病例挂起：handoff 指向 hosp_scan，等待用户确认挂号单。"""
    reg = await reception.register(
        title="默认就诊", scope_type="staged", scope={}, project_root=git_repo
    )
    handoff = reg["handoff"]
    assert handoff["stage"] == "registered"
    assert handoff["artifact"] == "挂号单"
    assert handoff["awaiting_confirmation"] is True
    assert handoff["next_tool"] == "hosp_scan"
    assert "stage_confirmed=true" in handoff["confirm_hint"]
    assert reg["case_id"] in handoff["confirm_hint"]

    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        assert case.pending_gate == "registered"
        assert case.auto_advance is False


async def test_gate_blocks_scan_until_confirmed(git_repo):
    """未确认调 scan：返回 pending_question(stage_confirm)，不执行扫描。"""
    reg = await reception.register(
        title="挂起", scope_type="staged", scope={}, project_root=git_repo
    )
    res = await scanner.scan(case_id=reg["case_id"], project_root=git_repo)

    assert res["status"] == "pending_question"
    q = res["question"]
    assert q["kind"] == "stage_confirm"
    assert q["blocking"] is True
    assert q["channel"] == "convention"
    assert [o["value"] for o in q["options"]] == ["confirm", "pause"]
    assert "挂起" in q["default_action"]
    assert reg["case_id"] in q["prompt"]
    assert res["resume_hint"] == (
        f'hosp_scan(case_id="{reg["case_id"]}", stage_confirmed=true)'
    )

    # 未执行扫描：病例仍在 registered 状态、无 findings、门禁未清
    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        assert case.status == "registered"
        assert case.pending_gate == "registered"
        assert (await db.case_counts(reg["case_id"]))["findings"] == 0

    # 重复裸调依然挂起（幂等，无副作用）
    again = await scanner.scan(case_id=reg["case_id"], project_root=git_repo)
    assert again["status"] == "pending_question"


async def test_stage_confirmed_releases_gate(git_repo):
    """用户确认后带 stage_confirmed=true：放行执行，检验报告产出后挂起下一道门禁。"""
    reg = await reception.register(
        title="确认进诊室", scope_type="staged", scope={}, project_root=git_repo
    )
    res = await scanner.scan(
        case_id=reg["case_id"], stage_confirmed=True, project_root=git_repo
    )
    assert res["findings"]["total"] == 1

    handoff = res["handoff"]
    assert handoff["stage"] == "scanned"
    assert handoff["artifact"] == "检验报告"
    assert handoff["awaiting_confirmation"] is True
    assert handoff["next_tool"] == "hosp_diagnose"
    assert "stage_confirmed=true" in handoff["confirm_hint"]

    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        assert case.status == "scanned"
        assert case.pending_gate == "scanned"


async def test_auto_advance_skips_all_gates(git_repo):
    """绿色通道：挂号委托后 scan 无需确认直通，两道 handoff 都标不等待。"""
    reg = await reception.register(
        title="全权委托", scope_type="staged", scope={},
        auto_advance=True, project_root=git_repo
    )
    assert reg["handoff"]["awaiting_confirmation"] is False
    assert reg["handoff"]["confirm_hint"] == ""

    res = await scanner.scan(case_id=reg["case_id"], project_root=git_repo)
    assert res["findings"]["total"] == 1
    assert res["handoff"]["awaiting_confirmation"] is False

    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        assert case.pending_gate == ""
        assert case.auto_advance is True


async def test_legacy_db_migrates_gate_columns(git_repo):
    """存量库（无门禁列的老库）重开后自动补列，老病例默认挂起语义可用。"""
    import aiosqlite

    db_path = Path(git_repo) / ".hospital" / "hospital.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 造一个 v1.0 老库：cases 表无 auto_advance / pending_gate
    async with aiosqlite.connect(db_path) as raw:
        await raw.execute(
            "CREATE TABLE cases ("
            " id TEXT PRIMARY KEY, project_root TEXT NOT NULL,"
            " title TEXT NOT NULL DEFAULT '', scope_type TEXT NOT NULL,"
            " scope_json TEXT NOT NULL DEFAULT '{}', triage_level TEXT NOT NULL,"
            " status TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 1,"
            " max_rounds INTEGER NOT NULL DEFAULT 3,"
            " created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        await raw.commit()

    # Database 重连触发迁移，随后正常挂号（新列生效）
    reg = await reception.register(
        title="老库新挂", scope_type="staged", scope={}, project_root=git_repo
    )
    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        assert case.pending_gate == "registered"
        assert case.auto_advance is False
