"""调用链：静态调用走出闭包，动态调用和对不上的名字保持未闭合。"""

from pathlib import Path

from code_hospital.catalog.coverage import build_ledger
from code_hospital.catalog.severe import chain_report_lines, close_chains
from code_hospital.core.models import Case, Scope
from code_hospital.storage.reports import render_discharge_report


def test_static_calls_close_and_getattr_stays_open(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "def main():\n"
        "    return work()\n"
        "def work(name):\n"
        "    return getattr(helper, name)\n"
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    chains = close_chains(str(tmp_path), ["app.py"])
    text = "\n".join(chain_report_lines(chains))
    assert "app.py 的 main → app.py 的 work" in text
    assert "app.py 的 work → app.py 的 helper" not in text
    assert any("动态调用 getattr" in item for item in chains["open"])
    assert "没有漏洞" not in text
    assert "未闭合链还在" in text


def test_ambiguous_name_and_other_languages_stay_open(tmp_path: Path):
    (tmp_path / "a.py").write_text("def main():\n    return run()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("def broken(\n", encoding="utf-8")
    chains = close_chains(str(tmp_path), ["a.py", "bad.py", "App.java"])
    opened = "\n".join(chains["open"])
    assert "调用 run 同名不止一处" in opened
    assert "bad.py：语法解析失败" in opened
    assert "App.java：这条链未建" in opened


def test_discharge_lists_open_chains_without_a_clean_bill(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "def main():\n    return getattr(helper, 'go')\n",
        encoding="utf-8",
    )
    chains = close_chains(str(tmp_path), ["app.py"])
    case = Case(
        id="case_x", project_root=str(tmp_path), title="出院", scope_type="repo",
        scope=Scope(), triage_level="checkup", status="dispensing",
        created_at=1, updated_at=1,
    )
    gate = {"checks": [], "verdict": "pass", "level": "放行"}
    diff = {"counts": {
        "resolved": 0, "unchanged": 0, "new_related": 0,
        "new_unrelated": 0, "possibly_resolved": 0,
    }}
    report = render_discharge_report(case, "cured", gate, diff, "follow.md", None, chains)
    section = report.split("## 调用链", 1)[1]
    assert "未闭合" in section
    assert "没有漏洞" not in section
    assert "没有功能问题" not in report
    assert "功能结论停在未闭合清单，这次不出清。" in report
    assert "治愈" in report
    _lines, ledger = build_ledger(
        "severe",
        [{"tool": "taint-ruler", "available": True, "outcome": "ok"}],
        str(tmp_path),
        ["app.py"],
    )
    note = next(unit["note"] for unit in ledger["units"] if unit["id"] == "taint-cross")
    assert "没有漏洞" not in note
    assert "未闭合" in note


def test_full_repo_discharge_does_not_claim_a_functional_clearance(tmp_path: Path):
    case = Case(
        id="case_repo", project_root=str(tmp_path), title="全仓", scope_type="repo",
        scope=Scope(), triage_level="checkup", visit_kind="checkup", status="dispensing",
        created_at=1_700_000_000_000, updated_at=1_700_000_000_000,
    )
    gate = {"checks": [], "verdict": "pass", "level": "放行"}
    diff = {"counts": {
        "resolved": 1, "unchanged": 0, "new_related": 0,
        "new_unrelated": 0, "possibly_resolved": 0,
    }}
    report = render_discharge_report(
        case, "cured", gate, diff, "follow.md", None, {"closed": ["app.py 的 main"], "open": []},
    )
    assert "治愈" in report
    assert "疗效判定只统计仪器发现是否消除" in report
    assert "这次是全仓出院。功能结论只写到已读完的调用链，这一节不给出清。" in report
    assert "没有功能问题" not in report
