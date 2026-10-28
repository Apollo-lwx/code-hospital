"""出院样本在下次检验里算复发率。没有样本时不算。"""

import json
from types import SimpleNamespace

from code_hospital.catalog.loop import measure, note_recurrence, record_discharge


def _finding(key: str, concluded_by: str = "instrument"):
    return SimpleNamespace(
        content_key=key,
        rule_id="F841",
        file_path="src/a.py",
        concluded_by=concluded_by,
    )


_COVERED = {"files": ["src/a.py"], "read": ["src/a.py"]}


def _save(tmp_path, case_id, outcome, findings, scope=None):
    return record_discharge(str(tmp_path), case_id, outcome, findings, scope=scope or _COVERED)


def test_later_scan_counts_a_returned_finding(tmp_path):
    _save(tmp_path, "case_a", "cured", [_finding("k1"), _finding("k2")])
    row = measure(str(tmp_path), [_finding("k2")])
    assert row["resolved"] == 2
    assert row["recurred"] == 1
    assert row["rate"] == 0.5
    assert "又出现 1 条" in row["note"]
    limitations = []
    ledger = note_recurrence(str(tmp_path), [_finding("k2")], limitations, {"units": []})
    assert ledger["recurrence"]["recurred"] == 1
    assert limitations


def test_clean_rescan_does_not_claim_there_was_no_sample(tmp_path):
    _save(tmp_path, "case_a", "cured", [_finding("k1")])
    row = measure(str(tmp_path), [_finding("other")])
    assert row["recurred"] == 0
    assert row["rate"] == 0
    assert "没有再出现" in row["note"]
    assert "没有问题" not in row["note"]


def test_empty_set_is_not_a_rate(tmp_path):
    assert measure(str(tmp_path), [_finding("k1")]) is None
    limitations = []
    ledger = note_recurrence(str(tmp_path), [], limitations, {"units": []})
    assert "recurrence" not in ledger
    assert limitations == []


def test_host_filing_does_not_count_as_recurrence(tmp_path):
    _save(tmp_path, "case_a", "cured", [_finding("k1")])
    row = measure(str(tmp_path), [_finding("k1", "host_llm")])
    assert row["recurred"] == 0
    assert row["rate"] == 0


def test_same_case_replaces_its_sample(tmp_path):
    _save(tmp_path, "case_a", "improved", [_finding("k1")])
    _save(tmp_path, "case_a", "cured", [_finding("k9")])
    row = measure(str(tmp_path), [_finding("k1")])
    assert row["resolved"] == 1
    assert row["recurred"] == 0
    row = measure(str(tmp_path), [_finding("k9")])
    assert row["recurred"] == 1


def test_discharge_without_keys_does_not_invent_a_rate(tmp_path):
    _save(tmp_path, "case_a", "unchanged", [])
    row = measure(str(tmp_path), [])
    assert row["rate"] is None
    assert "复发率不算" in row["note"]


def test_uncovered_sample_is_kept_and_left_out_of_the_rate(tmp_path):
    _save(
        tmp_path, "case_open", "cured", [_finding("k-open")],
        scope={"files": ["src/a.py", "src/b.py"], "read": ["src\\a.py"]},
    )
    _save(tmp_path, "case_full", "cured", [_finding("k-full")])
    saved = json.loads((tmp_path / ".hospital" / "eval" / "set.json").read_text(encoding="utf-8"))
    assert [item["case_id"] for item in saved["samples"]] == ["case_open", "case_full"]
    row = measure(str(tmp_path), [_finding("k-open"), _finding("k-full")])
    assert row["resolved"] == 1
    assert row["recurred"] == 1
    assert row["skipped"] == 1
    assert "另有 1 份未盖满范围，不计入" in row["note"]
    assert "没有问题" not in row["note"]


def test_only_uncovered_samples_are_not_a_rate(tmp_path):
    _save(tmp_path, "case_open", "cured", [_finding("k1")], scope={"files": ["src/a.py"], "read": []})
    row = measure(str(tmp_path), [_finding("k1")])
    assert row["rate"] is None
    assert row["recurred"] == 0
    assert "盖满范围" in row["note"]
    assert "没有问题" not in row["note"]


def test_clear_filing_counts_as_fully_read():
    from code_hospital.catalog.loop import scope_account

    account = scope_account(
        {"filing": {"items": [
            {"id": "diff.issues", "status": "clear"},
            {"id": "py.checklist.behavior", "status": "clear"},
        ]}},
        ["src/a.py"],
    )
    assert account["read"] == ["src/a.py"]
    partial = scope_account(
        {"filing": {"items": [{"id": "diff.issues", "status": "filed"}]}},
        ["src/a.py"],
    )
    assert partial["read"] == []
