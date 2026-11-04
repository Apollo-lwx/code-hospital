"""三本账：能力缺口、纪律偏差、记忆引用。

只验账本身和记账的位置——不验判断结果，这一步不改判断。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.catalog import ledger
from code_hospital.departments import diagnostician, reception, scanner
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class UnknownAdapter(ScannerAdapter):
    """药柜里没有对这一类的药，诊断要留一条 missing_formula。"""

    name = "unknown-ruler"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="mystery.check", message="没见过的毛病",
                file_path="src/a.py", line_start=2, line_end=2, severity="major",
            ),
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


def _use(*classes):
    adapters.clear()
    for cls in classes:
        adapters.register(cls())


def test_ensure_fills_three_books_without_touching_existing():
    coverage = {"source_hash": "abc", "memory": {"channel": "cli", "recalled": ["kn-1"]}}
    out = ledger.ensure(coverage)
    assert out is coverage
    assert out["capability_gap"] == []
    assert out["discipline"] == []
    assert out["memory"]["channel"] == "cli"
    assert out["memory"]["recalled"] == ["kn-1"]
    assert out["memory"]["feedback"] == "absent"
    assert out["source_hash"] == "abc"


def test_ensure_repairs_broken_memory_block():
    out = ledger.ensure({"memory": "坏了"})
    assert out["memory"]["channel"] == "absent"
    assert out["memory"]["owner"] == "hospital"


def test_gap_id_is_stable_and_evidence_merges():
    first = ledger.record_gap(
        {}, "missing_instrument",
        signals=["*.java"], files=["src/A.java"], extensions=["java"], case_id="case-1",
    )
    second = ledger.record_gap(
        first, "missing_instrument",
        signals=["*.java"], files=["src/B.java"], extensions=["java"], case_id="case-2",
    )
    rows = second["capability_gap"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"].startswith("gap-")
    assert row["occurrences"] == 2
    assert row["evidence"]["files"] == ["src/A.java", "src/B.java"]
    assert row["raised_in"] == ["case-1", "case-2"]
    assert row["status"] == "open"


def test_different_signal_makes_a_different_gap():
    out = ledger.record_gap({}, "missing_formula", signals=["a"])
    ledger.record_gap(out, "missing_formula", signals=["b"])
    assert len(out["capability_gap"]) == 2


def test_same_rule_across_extensions_is_one_gap():
    out = ledger.record_gap({}, "missing_formula", signals=["ts-function-lines", "ts-review"], extensions=["ts"])
    ledger.record_gap(out, "missing_formula", signals=["ts-function-lines", "ts-review"], extensions=["tsx"])
    rows = out["capability_gap"]
    assert len(rows) == 1
    assert rows[0]["occurrences"] == 2
    assert set(rows[0]["evidence"]["extensions"]) == {"ts", "tsx"}


class _FakeDiagnosis:
    def __init__(self, severity="minor", matched=False):
        self.severity = severity
        self.formulary_matched = matched
        self.category = "console-log"
        self.concluded_by = "code"
        self.fixability = "manual"
        self.id = "dia-1"


class _FakeFinding:
    tool = "ts-review"
    rule_id = "ts.console-log"
    file_path = "src/a.ts"
    verdict = ""


class _WaitingFinding(_FakeFinding):
    verdict = "needs_validation"


def test_style_findings_are_not_capability_gaps():
    """nit 和 info 本来就按不改处理，记成缺口会让招聘信号失真。"""
    style: dict = {}
    ledger.record_unmatched(style, _FakeDiagnosis(severity="nit"), [_FakeFinding()], "c1")
    assert style["capability_gap"] == []

    real: dict = {}
    ledger.record_unmatched(real, _FakeDiagnosis(severity="minor"), [_FakeFinding()], "c1")
    assert real["capability_gap"][0]["evidence"]["severity"] == "minor"


def test_waiting_verdict_is_not_a_capability_gap():
    """待验证是有药但先不动，不是不会治。"""
    out: dict = {}
    ledger.record_unmatched(out, _FakeDiagnosis(severity="minor"), [_WaitingFinding()], "c1")
    assert out["capability_gap"] == []


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        ledger.record_gap({}, "missing_everything")
    with pytest.raises(ValueError):
        ledger.record_deviation({}, "vibes")


def test_deviation_merges_by_kind_and_stage():
    out = ledger.record_deviation({}, "gate_bypass", stage="scanned", case_id="c1", round_no=1)
    ledger.record_deviation(out, "gate_bypass", stage="scanned", case_id="c1", round_no=2)
    ledger.record_deviation(out, "gate_bypass", stage="diagnosed", case_id="c1", round_no=2)
    rows = out["discipline"]
    assert len(rows) == 2
    scanned = next(row for row in rows if row["stage"] == "scanned")
    assert scanned["count"] == 2
    assert scanned["round"] == 2


def test_lines_say_unchecked_not_clean():
    out = ledger.record_gap({}, "missing_instrument", signals=["*.java"], extensions=["java"])
    lines = ledger.ledger_lines(out)
    assert any("未查" in line for line in lines)
    assert all("没有问题" not in line or "不记为没有问题" in line for line in lines)


def test_no_books_means_no_lines():
    assert ledger.ledger_lines({}) == []
    assert ledger.ledger_lines(None) == []


def test_scope_gaps_only_fire_when_no_live_instrument_covers_extension():
    coverage: dict = {}
    notes = ledger.record_scope_gaps(
        coverage, ["src/A.java"], [{"tool": "ruff", "available": True, "outcome": "ok"}], "c1",
    )
    assert notes and ".java" in notes[0]
    assert coverage["capability_gap"][0]["kind"] == "missing_instrument"


def test_scope_gaps_are_quiet_when_covered():
    coverage: dict = {}
    notes = ledger.record_scope_gaps(
        coverage,
        ["src/A.java"],
        [{"tool": "spotbugs", "available": True, "outcome": "ok"}],
        "c1",
    )
    assert notes == []
    assert coverage["capability_gap"] == []


def test_scope_gaps_skip_config_files_and_wildcard_instruments():
    coverage: dict = {}
    notes = ledger.record_scope_gaps(
        coverage, ["a.yaml", "b.json", "c.toml"], [{"tool": "ruff", "available": True, "outcome": "ok"}], "c1",
    )
    assert notes == []
    # semgrep 没声明扩展名，按全类型口径，不据此报缺口
    notes = ledger.record_scope_gaps(
        coverage,
        ["src/A.java"],
        [
            {"tool": "ruff", "available": True, "outcome": "ok"},
            {"tool": "semgrep", "available": True, "outcome": "ok"},
        ],
        "c1",
    )
    assert notes == []
    assert coverage["capability_gap"] == []


async def test_diagnose_records_missing_formula_and_shows_it(git_repo):
    _use(UnknownAdapter)
    reg = await reception.register(
        title="缺药", scope_type="staged", scope={},
        auto_advance=True, project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], project_root=git_repo)
    res = await diagnostician.diagnose(case_id=reg["case_id"], project_root=git_repo)

    diagnosis = res["diagnoses"][0]
    assert diagnosis["formulary_matched"] is False

    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
    gaps = case.coverage["capability_gap"]
    assert any(row["kind"] == "missing_formula" for row in gaps)
    assert case.coverage["memory"]["channel"] == "absent"

    report = Path(res["report_path"]).read_text(encoding="utf-8")
    assert "## 未查与缺口" in report
    assert "能力缺口" in report


async def test_diagnose_records_gate_bypass_when_gate_is_passed_by_flag(git_repo):
    _use(UnknownAdapter)
    reg = await reception.register(
        title="门禁", scope_type="staged", scope={},
        auto_advance=False, project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], project_root=git_repo, stage_confirmed=True)
    await diagnostician.diagnose(case_id=reg["case_id"], project_root=git_repo, stage_confirmed=True)

    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
    rows = case.coverage["discipline"]
    assert any(row["kind"] == "gate_bypass" and row["stage"] == "diagnosed" for row in rows)


async def test_scope_creep_is_written_into_the_chart(git_repo):
    from code_hospital.departments import pharmacist

    reg = await reception.register(
        title="越界", scope_type="staged", scope={}, auto_advance=True, project_root=git_repo,
    )
    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
        await pharmacist._note_scope_creep(db, case, "现状仍有越出 scope_lines 的改动")
        stored = await db.get_case(reg["case_id"])

    rows = stored.coverage["discipline"]
    assert rows[0]["kind"] == "scope_creep"
    assert rows[0]["count"] == 1
    assert "scope_lines" in rows[0]["detail"]
    assert json.loads(json.dumps(stored.coverage, ensure_ascii=False))  # 可序列化


# —— 招聘启事：缺口流向哪条闭环 ——

def test_reading_gap_never_becomes_a_job_posting():
    """「文档没人读」的关法是读，不是招一台 Markdown 检查器。"""
    book = ledger.record_gap(
        {}, "unread_by_model", signals=["*.md"], extensions=["md"],
        count=8, extra={"closure": "host_llm"},
    )

    assert ledger.recruitment_lines(book) == []


def test_market_gap_becomes_a_job_posting():
    book = ledger.record_gap(
        {}, "missing_instrument", signals=["*.kt"], extensions=["kt"], count=4,
    )

    [line] = ledger.recruitment_lines(book)
    assert "招聘启事" in line
    assert ".kt" in line and "4 个" in line
    assert "记为未查" in line


def test_a_stray_file_is_not_worth_hiring_for():
    """一个文件可能只是误放进来的一颗钉子；门槛挡住的是"为它专门招一台"。"""
    book = ledger.record_gap(
        {}, "missing_instrument", signals=["*.kt"], extensions=["kt"], count=1,
    )

    assert ledger.recruitment_lines(book) == []


def test_formula_gap_does_not_become_a_job_posting():
    """药柜缺口走补药，招人招不来药。"""
    book = ledger.record_gap(
        {}, "missing_formula", signals=["mystery"], extensions=["py"], count=9,
    )

    assert ledger.recruitment_lines(book) == []


def test_waived_gap_stops_asking():
    book = ledger.record_gap(
        {}, "missing_instrument", signals=["*.kt"], extensions=["kt"], count=9,
    )
    book["capability_gap"][0]["status"] = "waived"

    assert ledger.recruitment_lines(book) == []


# —— 招聘启事：缺口流向哪条闭环 ——

def test_reading_gap_never_becomes_a_job_posting():
    """「文档没人读」的关法是读，不是招一台 Markdown 检查器。"""
    book = ledger.record_gap(
        {}, "unread_by_model", signals=["*.md"], extensions=["md"],
        count=8, extra={"closure": "host_llm"},
    )

    assert ledger.recruitment_lines(book) == []


def test_market_gap_becomes_a_job_posting():
    book = ledger.record_gap(
        {}, "missing_instrument", signals=["*.kt"], extensions=["kt"], count=4,
    )

    [line] = ledger.recruitment_lines(book)
    assert "招聘启事" in line
    assert ".kt" in line and "4 个" in line
    assert "记为未查" in line


def test_a_stray_file_is_not_worth_hiring_for():
    """一个文件可能只是误放进来的一颗钉子；门槛挡住的是"为它专门招一台"。"""
    book = ledger.record_gap(
        {}, "missing_instrument", signals=["*.kt"], extensions=["kt"], count=1,
    )

    assert ledger.recruitment_lines(book) == []


def test_formula_gap_does_not_become_a_job_posting():
    """药柜缺口走补药，招人招不来药。"""
    book = ledger.record_gap(
        {}, "missing_formula", signals=["mystery"], extensions=["py"], count=9,
    )

    assert ledger.recruitment_lines(book) == []


def test_waived_gap_stops_asking():
    book = ledger.record_gap(
        {}, "missing_instrument", signals=["*.kt"], extensions=["kt"], count=9,
    )
    book["capability_gap"][0]["status"] = "waived"

    assert ledger.recruitment_lines(book) == []


def test_promoted_market_instrument_closes_the_gap_it_covers():
    """转正以后它就是现任，扩展名要算进覆盖——否则缺口会一直报，招聘永远招不完。"""
    book = {}
    ledger.record_scope_gaps(
        book, ["src/a.kt"],
        [{"tool": "acme-kt", "available": True, "outcome": "ok", "extensions": ["kt"]}],
    )

    assert not [row for row in book["capability_gap"] if row["kind"] == "missing_instrument"]


def test_unknown_instrument_does_not_silence_the_gap_account():
    """市场装的仪器不在随包清册里。只查清册会把它当成「什么都查」，缺口一条都不报。"""
    book = {}
    ledger.record_scope_gaps(
        book, ["src/a.kt"],
        [{"tool": "acme-unknown", "available": True, "outcome": "ok"}],
    )

    assert [row for row in book["capability_gap"] if row["kind"] == "missing_instrument"]


def test_promoted_market_instrument_closes_the_gap_it_covers():
    """转正以后它就是现任，扩展名要算进覆盖——否则缺口会一直报，招聘永远招不完。"""
    book = {}
    ledger.record_scope_gaps(
        book, ["src/a.kt"],
        [{"tool": "acme-kt", "available": True, "outcome": "ok", "extensions": ["kt"]}],
    )

    assert not [row for row in book["capability_gap"] if row["kind"] == "missing_instrument"]


def test_unknown_instrument_does_not_silence_the_gap_account():
    """市场装的仪器不在随包清册里。只查清册会把它当成「什么都查」，缺口一条都不报。"""
    book = {}
    ledger.record_scope_gaps(
        book, ["src/a.kt"],
        [{"tool": "acme-unknown", "available": True, "outcome": "ok"}],
    )

    assert [row for row in book["capability_gap"] if row["kind"] == "missing_instrument"]
