"""全库阅读调度：事项是提醒，已读才从下一批拿掉。"""

from code_hospital.catalog.host_scan import record, view, write_plan


def test_topics_do_not_limit_the_batch():
    files = [f"src/f{index}.py" for index in range(3)]
    shown = view(files, {}, topics=["这些 any 是不是抹掉了类型"])
    assert shown["topics_are_not_limits"] is True
    assert "不是诊断白名单" in shown["charter"]
    assert "自由文字不算产物" in shown["task"]
    assert shown["batch"] == files
    assert shown["unread"] == 3


def test_read_marks_advance_the_next_batch(tmp_path):
    files = [f"src/f{index}.py" for index in range(45)]
    coverage = record({}, files, {"host.scan": "已读： src/f0.py src/f1.py"})
    shown = view(files, coverage)
    assert "src/f0.py" not in shown["batch"]
    assert "src/f1.py" not in shown["batch"]
    assert shown["read"] == 2
    assert shown["unread"] == 43
    assert len(shown["batch"]) == 40
    other = tmp_path / "other-repo"
    other.mkdir()
    coverage = record(coverage, files, {"joint": str(other)})
    assert shown["joint"] == []
    again = view(files, coverage)
    assert again["joint"] == [str(other)]
    path = write_plan(str(tmp_path), "case_x", files, coverage)
    text = open(path, encoding="utf-8").read()
    assert "topics_are_not_limits" in text
    assert "src/f2.py" in text
