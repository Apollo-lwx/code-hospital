"""交卷契约：对象和文本都校验，结果进覆盖账。"""

from code_hospital.catalog.checks import file_host_diff, filing_accepted, run_checks
from code_hospital.catalog.filing import CONTRACT_LINE, filing_ledger, parse_answer


def _pending():
    return {
        "id": "diff.issues",
        "executors": [{"kind": "llm", "status": "pending", "text": "task"}],
    }


def test_object_issues_file_like_text():
    specs, problem = parse_answer(
        {"issues": [{"file": "src/a.py", "line": 2, "message": "返回值被丢掉"}]},
        ["src/a.py"],
    )
    assert problem == ""
    assert specs == [{
        "file_path": "src/a.py",
        "line_start": 2,
        "line_end": 2,
        "message": "返回值被丢掉",
    }]
    filed, note = file_host_diff(
        [_pending()],
        {"diff.issues": {"issues": [{"file": "src/a.py", "line": 2, "message": "返回值被丢掉"}]}},
        ["src/a.py"],
    )
    assert len(filed) == 1
    assert filing_accepted(note)
    assert "写入病历" in note


def test_clear_object_requires_every_file():
    specs, problem = parse_answer({"clear": True, "read": ["src/a.py"]}, ["src/a.py", "src/b.py"])
    assert specs == []
    assert "src/b.py" in problem
    assert "没有功能问题" in problem
    specs, problem = parse_answer(
        '{"clear": true, "read": ["src/a.py"]}',
        ["src/a.py"],
    )
    assert specs == []
    assert problem == ""
    _filed, note = file_host_diff(
        [_pending()],
        {"diff.issues": {"clear": True, "read": ["src/a.py"]}},
        ["src/a.py"],
    )
    assert note == "宿主已通读本次 diff，并写明没有功能问题。"


def test_object_rejects_a_mixed_or_outside_filing():
    _specs, mixed = parse_answer(
        {"clear": True, "issues": [{"file": "src/a.py", "line": 1, "message": "有"}]},
        ["src/a.py"],
    )
    assert "既写问题又写没有" in mixed
    _specs, outside = parse_answer(
        {"issues": [{"file": "other.py", "line": 1, "message": "有"}]},
        ["src/a.py"],
    )
    assert "不在这次合入范围里" in outside
    _specs, blank = parse_answer({"issues": []}, ["src/a.py"])
    assert "文件:行: 说明" in blank
    assert "没有功能问题" not in blank


def test_ledger_keeps_a_finished_item_while_others_stay_open():
    checks = [
        _pending(),
        {"id": "py.checklist.behavior", "group": "checklist", "question": "行为", "executors": []},
    ]
    answers = {"diff.issues": {"issues": [{"file": "src/a.py", "line": 2, "message": "丢掉"}]}}
    specs, note = file_host_diff(checks, answers, ["src/a.py"])
    assert len(specs) == 1
    assert not filing_accepted(note)
    assert "未闭合" in note
    ledger = filing_ledger(checks, answers, ["src/a.py"], accepted=False)
    by_id = {row["id"]: row for row in ledger["items"]}
    assert by_id["diff.issues"]["status"] == "filed"
    assert by_id["diff.issues"]["issues"] == 1
    assert by_id["py.checklist.behavior"]["status"] == "open"


def test_later_round_adds_without_repeating_the_first_issue():
    from code_hospital.catalog.filing import settle_filing

    checks = [
        _pending(),
        {"id": "py.checklist.behavior", "group": "checklist", "question": "行为", "executors": []},
    ]
    _first, _note, ledger = settle_filing(
        checks,
        {"diff.issues": {"issues": [{"file": "src/a.py", "line": 2, "message": "丢掉"}]}},
        ["src/a.py"],
    )
    added, note, later = settle_filing(
        checks,
        {
            "diff.issues": {"issues": [
                {"file": "src/a.py", "line": 2, "message": "丢掉"},
                {"file": "src/a.py", "line": 4, "message": "又一条"},
            ]},
            "py.checklist.behavior": {"clear": True, "read": ["src/a.py"]},
        },
        ["src/a.py"],
        ledger,
    )
    assert added == [{
        "file_path": "src/a.py", "line_start": 4, "line_end": 4,
        "message": "又一条", "check_id": "diff.issues",
    }]
    assert filing_accepted(note)
    assert "病历里共 2 条" in note
    assert "本轮新写入 1 条" in note
    by_id = {row["id"]: row for row in later["items"]}
    assert by_id["diff.issues"]["status"] == "filed"
    assert by_id["diff.issues"]["issues"] == 2
    assert by_id["py.checklist.behavior"]["status"] == "clear"
    cleared, clear_note, kept = settle_filing(
        checks,
        {"diff.issues": {"clear": True, "read": ["src/a.py"]}},
        ["src/a.py"],
        later,
    )
    assert cleared == []
    assert "没有功能问题" not in clear_note
    assert {row["id"]: row["issues"] for row in kept["items"]}["diff.issues"] == 2


def test_checklist_and_finding_point_at_each_other():
    from code_hospital.catalog.filing import link_text, relate_checks, settle_filing

    checks = [
        _pending(),
        {"id": "team.pay", "group": "checklist", "question": "金额", "executors": []},
    ]
    specs, _note, ledger = settle_filing(
        checks,
        {
            "diff.issues": {"issues": [{"file": "src/a.py", "line": 2, "message": "丢掉"}]},
            "team.pay": {"issues": [
                {"file": "src/a.py", "line": 2, "message": "丢掉"},
                {"file": "src/a.py", "line": 4, "message": "金额被改了两次"},
            ]},
        },
        ["src/a.py"],
    )
    assert [row["check_id"] for row in specs] == ["diff.issues", "team.pay", "team.pay"]
    findings = [
        {"id": "f1", "tool": "host", "rule_id": "diff.issue", "file_path": "src/a.py",
         "line_start": 2, "line_end": 2, "message": "丢掉", "check_id": "diff.issues"},
        {"id": "f2", "tool": "host", "rule_id": "diff.issue", "file_path": "src/a.py",
         "line_start": 4, "line_end": 4, "message": "金额被改了两次", "check_id": "team.pay"},
    ]
    links = relate_checks(findings, ledger)
    assert links["by_finding"]["f1"] == ["diff.issues", "team.pay"]
    assert links["by_finding"]["f2"] == ["team.pay"]
    assert [row["finding_id"] for row in links["by_check"]["team.pay"]] == ["f1", "f2"]
    text = link_text(links)
    assert "team.pay → " in text
    assert "src/a.py:4 金额被改了两次" in text
    assert "清单与发现" in text


def test_task_sheet_carries_the_contract(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    rows, _notes = run_checks(str(tmp_path), ["a.py"], visit_kind="admission")
    issues = next(row for row in rows if row["id"] == "diff.issues")
    llm = next(item for item in issues["executors"] if item["kind"] == "llm")
    assert llm["contract"]["version"] == 1
    assert "issues" in llm["contract"]["object"]
    assert CONTRACT_LINE in llm["text"]
    assert "要交出" in llm["text"]
