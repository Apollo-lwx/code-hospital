"""证据层：模型结论能不能定级，取决于引文能不能被机器核对。

核对是纯字符串比对，所以每一条都能复现。这组测试守的就是「可复现」：
同一份文件、同一段引文，无论跑多少次都得到同一个档。
"""

import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.core.models import Case, Diagnosis, Finding, Scope, counts_in_report, finding_rank
from code_hospital.departments import diagnostician, reception, scanner
from code_hospital.safety import evidence
from code_hospital.storage.db import Database
from code_hospital.storage.reports import render_diagnose_report, render_scan_report

_DOC = "# 发布规范\n\n必须走 PR，不允许直接推主干。\n合并前要过 CI。\n"


@pytest.fixture()
def root(tmp_path: Path) -> str:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(_DOC, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    x = 1\n    return 2\n", encoding="utf-8")
    return str(tmp_path)


def test_exact_quote_on_the_claimed_line_is_anchored(root: str):
    got = evidence.check(root, "docs/guide.md", 3, 3, "必须走 PR，不允许直接推主干。")
    assert got["tier"] == "anchored"
    assert got["status"] == "verified"
    assert (got["line_start"], got["line_end"]) == (3, 3)


def test_quote_without_a_line_still_gets_located_by_the_hospital(root: str):
    """文档级的结论常给不出行号。只要抄了原文，医院自己能把行找出来。"""
    got = evidence.check(root, "docs/guide.md", 0, 0, "合并前要过 CI。")
    assert got["tier"] == "anchored"
    assert got["line_start"] == 4


def test_line_drift_is_corrected_but_whitespace_is_forgiven(root: str):
    moved = evidence.check(root, "docs/guide.md", 1, 2, "必须走 PR，不允许直接推主干。")
    assert moved["tier"] == "anchored"
    assert moved["status"] == "verified_moved"
    assert moved["line_start"] == 3

    loose = evidence.check(root, "docs/guide.md", 3, 3, "必须走  PR，不允许直接推主干。")
    assert loose["tier"] == "anchored"
    assert loose["status"] in {"verified_loose", "verified"}


def test_fabricated_quote_is_ungrounded_and_reported(root: str):
    assert evidence.check(root, "docs/guide.md", 3, 3, "必须走 PR，允许直接推主干。")["tier"] == "ungrounded"
    # 没给行号也一样：整份文件里都找不到，就是编的。
    absent = evidence.check(root, "docs/guide.md", 0, 0, "这段话文档里根本没有")
    assert absent["tier"] == "ungrounded"
    assert absent["status"] == "quote_absent"


def test_no_quote_only_reaches_the_file(root: str):
    bare = evidence.check(root, "docs/guide.md", 3, 3, "")
    assert (bare["tier"], bare["status"]) == ("located", "unquoted")
    whole = evidence.check(root, "docs/guide.md", 0, 0, "")
    assert (whole["tier"], whole["status"]) == ("located", "file_only")


def test_short_quote_is_not_evidence(root: str):
    """「}`」这种在哪个文件里都能找到，核对了也没有信息量。"""
    got = evidence.check(root, "src/a.py", 3, 3, "2")
    assert got["tier"] == "located"


def test_missing_and_escaping_files_are_clues(root: str):
    assert evidence.check(root, "", 1, 1, "x")["tier"] == "clue"
    assert evidence.check(root, "docs/none.md", 1, 1, "x")["status"] == "missing_file"
    outside = evidence.check(root, "../outside.md", 1, 1, "x")
    assert (outside["tier"], outside["status"]) == ("clue", "outside_root")


def test_line_out_of_range_is_corrected_by_the_quote(root: str):
    got = evidence.check(root, "docs/guide.md", 500, 500, "必须走 PR，不允许直接推主干。")
    # 行号越界但引文在文件里：纠正行号，按 anchored 记。
    assert got["tier"] == "anchored"
    assert got["line_start"] == 3


def test_grade_caps_by_tier():
    assert evidence.grade("critical", "anchored") == ("critical", "")
    value, why = evidence.grade("critical", "located")
    assert value == "minor" and "压到 minor" in why
    assert evidence.grade("nit", "located")[0] == "nit"
    assert evidence.grade("major", "ungrounded")[0] == "info"
    assert evidence.grade("major", "clue")[0] == "info"


def test_summary_and_limitation_line(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    rows = [evidence.check(str(tmp_path), "a.py", 1, 1, "")]
    book = evidence.summary(rows)
    assert book["total"] == 1 and book["located"] == 1
    assert "只到文件行 1 条" in evidence.limitation_line(book)
    assert evidence.limitation_line(evidence.summary([])) == ""


def test_digest_is_stable_and_ignores_whitespace():
    one = evidence.digest_of("docs/guide.md", 3, 3, "必须走 PR")
    two = evidence.digest_of("./docs/guide.md", 3, 3, "必须走   PR")
    assert one == two
    assert one != evidence.digest_of("docs/guide.md", 4, 4, "必须走 PR")


def _case() -> Case:
    return Case(
        id="case_x", project_root=".", title="示例", scope_type="repo",
        scope=Scope(), triage_level="checkup", status="scanned",
        created_at=1_700_000_000_000, updated_at=1_700_000_000_000,
    )


def _host(fid: str, tier: str, severity: str, line: int, message: str = "文档与代码不一致") -> Finding:
    return Finding(
        id=fid, case_id="case_x", report_id="r1", tool="host", rule_id="diff.issue",
        severity=severity, file_path="docs/guide.md", line_start=line, line_end=line,
        message=message, evidence="必须走 PR，不允许直接推主干。", fingerprint=fid,
        concluded_by="host_llm", evidence_tier=tier,
    )


def test_anchored_model_finding_counts_in_the_report():
    anchored = _host("f1", "anchored", "major", 3)
    located = _host("f2", "located", "major", 3)
    assert counts_in_report(anchored) and not counts_in_report(located)
    assert finding_rank(anchored) < finding_rank(located)


def test_scan_report_puts_anchored_above_the_leads():
    md = render_scan_report(
        _case(), "r1", [], [_host("f1", "anchored", "major", 3), _host("f2", "located", "critical", 3)], [],
    )
    head, _, tail = md.partition("## 待核实线索")
    assert "引文已与文件逐字核对" in head
    assert "模型（引文已核）" in head
    assert "docs/guide.md:3" in head
    # 没抄原文的那条只在线索区，并且写明档位。
    assert "[只到文件行]" in tail
    assert tail.count("docs/guide.md:3") == 1


def test_anchored_conclusion_is_not_labelled_unverified():
    """引文已经核上了，还加「待核实线索」前缀就是自相矛盾。不进处方是另一道闸。"""
    from code_hospital.safety.admission import rules_for, settle_semantic

    anchored = Diagnosis(
        id="d1", case_id="c", report_id="r", fingerprint="f", category="diff.issue",
        root_cause="文档与代码不一致", severity="major", confidence=0.8, fixability="manual",
        member_ids=["f1"], concluded_by="host_llm",
    )
    kept, _drops = settle_semantic([(anchored, [_host("f1", "anchored", "major", 3)])], rules_for("", "auto_advance"))
    assert kept[0][0].root_cause == "文档与代码不一致"

    bare = Diagnosis(
        id="d2", case_id="c", report_id="r", fingerprint="f2", category="diff.issue",
        root_cause="文档与代码不一致", severity="minor", confidence=0.8, fixability="manual",
        member_ids=["f2"], concluded_by="host_llm",
    )
    kept, _drops = settle_semantic([(bare, [_host("f2", "located", "minor", 3)])], rules_for("", "auto_advance"))
    assert kept[0][0].root_cause.startswith("待核实线索：")


def test_ungrounded_claim_is_not_a_capability_gap():
    """核不上的话连「已检出」都谈不上，记成缺口就是拿噪音当招聘需求。"""
    from code_hospital.catalog.ledger import record_unmatched

    def diagnosis(member_id: str) -> Diagnosis:
        return Diagnosis(
            id="d", case_id="c", report_id="r", fingerprint="f", category="diff.issue",
            root_cause="x", severity="major", confidence=0.8, fixability="manual",
            member_ids=[member_id], formulary_matched=False, concluded_by="host_llm",
        )

    book = record_unmatched({}, diagnosis("f1"), [_host("f1", "ungrounded", "info", 3)])
    assert book["capability_gap"] == []
    book = record_unmatched({}, diagnosis("f2"), [_host("f2", "anchored", "major", 3)])
    assert [row["kind"] for row in book["capability_gap"]] == ["missing_formula"]


def test_reading_gap_is_settled_once_the_model_reads(root: str):
    """同一份报告不能一边写「引文已核」，一边写「文档没有模型读过」。"""
    from code_hospital.catalog.ledger import record_scope_gaps, refresh_reading_gaps

    files = ["docs/guide.md", "src/a.py"]
    book: dict = {}
    notes = record_scope_gaps(book, files, [], "case-1", root, read=set())
    assert any("没有模型阅读" in line for line in notes)
    assert any(row["kind"] == "unread_by_model" for row in book["capability_gap"])

    refresh_reading_gaps(book, files, ["docs/guide.md", "src/a.py"], root, "case-1")
    assert not [row for row in book["capability_gap"] if row["kind"] == "unread_by_model"]


def test_diagnose_report_labels_the_source_by_tier():
    case = _case()

    def pair(fid: str, tier: str) -> tuple:
        finding = _host(fid, tier, "major", 3)
        diagnosis = Diagnosis(
            id=f"d-{fid}", case_id=case.id, report_id="r1", fingerprint=fid,
            category="diff.issue", root_cause="文档与代码不一致", severity="major",
            confidence=0.8, fixability="manual", member_ids=[finding.id],
            concluded_by="host_llm",
        )
        return diagnosis, [finding]

    md = render_diagnose_report(
        case, "r1", [pair("f1", "anchored"), pair("f2", "ungrounded")], 
        {"total_findings": 2, "clusters": 2, "autofix": 0, "llm_patch": 0, "manual": 2},
    )
    body, _, leads = md.partition("## 待核实线索")
    assert "模型（引文已核 1 处）" in body
    assert "模型（引文与文件对不上，仅作线索）" in leads


class _Ruff(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="F841", message="unused", file_path="src/a.py",
            line_start=2, line_end=2, severity="major",
        )]


async def test_diagnose_files_three_tiers_and_books_the_evidence(root: str):
    adapters.clear()
    adapters.register(_Ruff())
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    reg = await reception.register(
        title="引文", scope_type="repo", scope={},
        auto_advance=True, visit_kind="admission", project_root=root,
    )
    case_id = reg["case_id"]
    await scanner.scan(case_id=case_id, project_root=root)

    answer = {
        "issues": [
            {
                "file": "docs/guide.md", "line": 3, "severity": "major",
                "message": "文档要求走 PR，但仓库没有对应校验",
                "evidence": "必须走 PR，不允许直接推主干。",
            },
            {
                "file": "docs/guide.md", "line": 4, "severity": "critical",
                "message": "CI 要求没有落到流水线配置",
            },
            {
                "file": "src/a.py", "line": 2, "severity": "major",
                "message": "这行有个问题",
                "evidence": "这行原文在文件里并不存在",
            },
        ],
        "read": ["docs/guide.md", "src/a.py"],
    }
    async with Database(root) as db:
        case = await db.get_case(case_id)
        await diagnostician._file_host_diff(db, root, case, {"diff.issues": answer})
        stored = [f for f in await db.all_findings(case_id) if f.concluded_by == "host_llm"]
        case = await db.get_case(case_id)
    adapters.clear()

    by_message = {f.message.split("（")[0][:12]: f for f in stored}
    anchored = next(f for f in stored if "走 PR" in f.message)
    located = next(f for f in stored if "CI 要求" in f.message)
    ungrounded = next(f for f in stored if "这行有个问题" in f.message)
    assert by_message  # 三条都落库了
    assert (anchored.evidence_tier, anchored.severity) == ("anchored", "major")
    assert (located.evidence_tier, located.severity) == ("located", "minor")
    assert (ungrounded.evidence_tier, ungrounded.severity) == ("ungrounded", "info")
    book = case.coverage["evidence"]
    assert book["anchored"] == 1 and book["located"] == 1 and book["ungrounded"] == 1
    kinds = [row["kind"] for row in case.coverage["discipline"]]
    assert "unfounded_claim" in kinds
