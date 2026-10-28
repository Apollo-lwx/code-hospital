"""三档命中分开计。行号重合不算语义等价。"""

from code_hospital.catalog.hits import render_hit_report, score_hits


def test_line_overlap_is_not_a_function_or_semantic_hit():
    gold = [{"id": "g1", "file": "src/a.py", "line": 2, "end": 3}]
    findings = [{"file_path": "src/a.py", "line_start": 3, "line_end": 4, "symbol": ""}]
    score = score_hits(gold, findings)
    assert score["line"]["hit"] == 1
    assert score["line"]["rate"] == 1
    assert score["function"]["rate"] is None
    assert "函数级不算分" in score["function"]["note"]
    assert score["semantic"]["rate"] is None
    assert "语义等价还没有标注" in score["semantic"]["note"]


def test_same_symbol_hits_function_when_lines_differ():
    gold = [{"id": "g1", "file": "src/a.py", "line": 2, "symbol": "pay"}]
    findings = [{"file": "src/a.py", "line": 40, "symbol": "pay"}]
    score = score_hits(gold, findings)
    assert score["line"]["hit"] == 0
    assert score["function"]["hit"] == 1
    assert score["function"]["rate"] == 1
    assert score["semantic"]["hit"] == 0


def test_semantic_label_does_not_follow_the_line():
    gold = [
        {"id": "g1", "file": "src/a.py", "line": 2, "symbol": "pay"},
        {"id": "g2", "file": "src/b.py", "line": 8, "symbol": "ship"},
    ]
    findings = [{"file": "src/a.py", "line": 2, "symbol": "other"}]
    score = score_hits(gold, findings, [
        {"gold_id": "g2", "equivalent": True},
        {"gold_id": "g1", "equivalent": False},
    ])
    assert score["line"]["hit"] == 1
    assert score["line"]["rate"] == 0.5
    assert score["function"]["hit"] == 0
    assert score["semantic"]["labeled"] == 2
    assert score["semantic"]["hit"] == 1
    assert score["semantic"]["rate"] == 0.5
    report = render_hit_report(score)
    assert "行级：1/2" in report
    assert "函数级：0/2" in report
    assert "语义等价：1/2" in report


def test_host_filing_does_not_count_for_line_or_function():
    gold = [{"id": "g1", "file": "src/a.py", "line": 2, "symbol": "pay"}]
    findings = [{
        "file": "src/a.py", "line": 2, "symbol": "pay", "concluded_by": "host_llm",
    }]
    score = score_hits(gold, findings, [{"gold_id": "g1", "equivalent": True}])
    assert score["line"]["hit"] == 0
    assert score["function"]["hit"] == 0
    assert score["semantic"]["hit"] == 1


def test_missing_location_is_unscoped_not_a_miss():
    gold = [
        {"id": "g1", "file": "src/a.py", "line": 2, "symbol": "pay"},
        {"id": "g2", "file": "src/a.py"},
    ]
    findings = [{"file": "src/a.py", "line": 2, "symbol": "pay"}]
    score = score_hits(gold, findings)
    assert score["line"]["gold"] == 1
    assert score["line"]["unscoped"] == 1
    assert score["line"]["rate"] == 1
    assert "不进这一档" in score["line"]["note"]
    assert score["function"]["gold"] == 1
    assert score["function"]["unscoped"] == 1


def test_empty_gold_does_not_score():
    score = score_hits([], [{"file": "src/a.py", "line": 1}])
    assert score["line"]["rate"] is None
    assert score["function"]["rate"] is None
    assert score["semantic"]["rate"] is None
    blob = render_hit_report(score)
    assert "不算分" in blob
    assert "没有问题" not in blob
