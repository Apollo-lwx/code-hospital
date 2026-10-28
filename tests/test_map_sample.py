"""映射样本：24 条金标有标注，行号碰上不自动变成语义等价。"""

from code_hospital.catalog.hits import measure_coverage, render_eval_report, score_hits
from code_hospital.catalog.map_sample import load_sample, sample_report, score_sample


def _overlaps(gold: dict, finding: dict) -> bool:
    end = finding.get("end", finding["line"])
    return gold["file"] == finding["file"] and gold["line"] <= end and finding["line"] <= gold["end"]


def test_sample_has_twenty_to_thirty_labeled_golds():
    sample = load_sample()
    assert 20 <= len(sample["gold"]) <= 30
    assert len(sample["mappings"]) == len(sample["gold"])
    assert all(isinstance(row["equivalent"], bool) for row in sample["mappings"])
    assert all(row["gold_id"] for row in sample["mappings"])
    extras = [row for row in sample["items"] if row["relation"] == "extra"]
    assert extras
    assert all(row["gold"] is None for row in extras)
    assert len(sample["items"]) <= 30


def test_line_overlap_is_not_copied_into_equivalent():
    sample = load_sample()
    by_id = {row["gold_id"]: row for row in sample["mappings"]}
    datadir = next(row for row in sample["gold"] if row["id"] == "rb-apilix-datadir")
    prefix = next(row for row in sample["findings"] if row["line"] == 1044)
    assert _overlaps(datadir, prefix)
    assert by_id["rb-apilix-datadir"]["equivalent"] is False
    assert by_id["rb-apilix-prefix"]["equivalent"] is True

    kmax = next(row for row in sample["gold"] if row["id"] == "rb-protrider-kmax")
    unused = next(row for row in sample["findings"] if row["rule_id"] == "F841" and "k_max" in row["message"])
    assert not _overlaps(kmax, unused)
    assert by_id["rb-protrider-kmax"]["equivalent"] is True


def test_same_relation_is_the_only_semantic_hit():
    sample = load_sample()
    score = score_sample()
    same = [row["gold_id"] for row in sample["items"] if row["relation"] == "same"]
    hits = [row["gold_id"] for row in sample["mappings"] if row["equivalent"]]
    assert hits == same
    assert score["semantic"]["hit"] == len(same) == 2
    assert score["semantic"]["labeled"] == 24
    assert score["semantic"]["rate"] == round(2 / 24, 4)
    assert score["line"]["hit"] == 2
    assert score["line"]["rate"] == round(2 / 24, 4)
    assert score["function"]["rate"] is None
    line_ids = {
        row["id"]
        for row in sample["gold"]
        if any(_overlaps(row, found) for found in sample["findings"])
    }
    assert line_ids == {"rb-apilix-prefix", "rb-apilix-datadir"}
    assert set(hits) == {"rb-apilix-prefix", "rb-protrider-kmax"}


def test_sample_report_prints_three_scores():
    report = sample_report()
    assert report.startswith("# 评测报告")
    assert "不合成一个分数" in report
    assert "## 行级" in report
    assert "## 函数级" in report
    assert "## 语义等价" in report
    assert "行级" in report.split("## 函数级")[0]
    line, function, semantic = _sections(report)
    assert "2/24" in line
    assert "这一档的比例是 0.0833" in line
    assert "rb-apilix-datadir" in line
    assert "rb-apilix-prefix" in line
    assert "不算分" in function
    assert "这一档的比例" not in function
    assert "2/24" in semantic
    assert "rb-protrider-kmax" in semantic
    assert "rb-apilix-datadir" not in semantic
    assert "合计" not in report
    assert "没有问题" not in report
    assert "不进这三档" in report
    assert "## 覆盖率" in report.split("## 行级", 1)[0]
    assert "没有范围账，覆盖率不算。" in report
    assert "覆盖率是" not in report


def test_empty_eval_report_keeps_three_sections():
    score = score_hits([], [])
    report = render_eval_report(score, source="没有金标。")
    line, function, semantic = _sections(report)
    assert "不算分" in line
    assert "不算分" in function
    assert "不算分" in semantic
    assert "这一档的比例" not in report
    assert "不合成一个分数" in report
    assert "合计" not in report
    assert "没有问题" not in report
    assert "没有范围账，覆盖率不算。" in report.split("## 行级", 1)[0]


def test_coverage_sits_beside_the_scores_and_stays_separate():
    score = score_hits(
        [{"id": "g1", "file": "src/a.py", "line": 2}],
        [{"file": "src/a.py", "line": 2}],
        [{"gold_id": "g1", "equivalent": False}],
    )
    partial = measure_coverage([
        {"files": ["src/a.py", "src/b.py"], "read": ["./src/a.py"]},
        {"files": ["src/c.py"], "read": ["src\\c.py"]},
    ])
    assert partial["covered"] == 1
    assert partial["scopes"] == 2
    assert partial["rate"] == 0.5
    report = render_eval_report(score, source="两份范围。", coverage=partial)
    head, body = report.split("## 行级", 1)
    assert "1/2" in head
    assert "覆盖率是 0.5" in head
    assert "未盖满的范围不算已经看完" in head
    assert "1/1" in body
    assert "0.5" not in body
    full = measure_coverage([{"files": ["src/a.py"], "read": ["src/a.py"]}])
    assert "已读文件盖满范围" in full["note"]
    assert measure_coverage([{"files": [], "read": ["src/a.py"]}])["rate"] is None
    assert measure_coverage(None)["rate"] is None


def _sections(report: str) -> tuple[str, str, str]:
    rest = report.split("## 行级", 1)[1]
    line, rest = rest.split("## 函数级", 1)
    function, semantic = rest.split("## 语义等价", 1)
    return line, function, semantic
