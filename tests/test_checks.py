"""检查事项名册：代码交事实，宿主 LLM 保持待交卷。"""

from code_hospital.catalog.checks import load_checks, run_checks
from code_hospital.core.models import Case, Scope
from code_hospital.storage.reports import render_scan_report


def test_builtin_architecture_items_are_ordered():
    items, note = load_checks(".")
    ids = [item["id"] for item in items]
    assert note == ""
    assert ids.index("arch.map") < ids.index("arch.tradeoff")
    assert ids.index("arch.problems") < ids.index("arch.patterns")
    assert "complexity.bound" in ids
    assert "resource.leak" in ids


def test_arch_map_sees_cross_module_import(tmp_path):
    api = tmp_path / "api"
    core = tmp_path / "core"
    api.mkdir()
    core.mkdir()
    (api / "routes.py").write_text("import core.db\n", encoding="utf-8")
    (core / "db.py").write_text("x = 1\n", encoding="utf-8")
    rows, notes = run_checks(str(tmp_path), ["api/routes.py", "core/db.py"])
    assert notes == []
    arch = next(row for row in rows if row["id"] == "arch.map")
    code = next(item["text"] for item in arch["executors"] if item["kind"] == "code")
    assert "api → core" in code
    trade = next(row for row in rows if row["id"] == "arch.tradeoff")
    assert trade["status"] == "open"
    llm = next(item for item in trade["executors"] if item["kind"] == "llm")
    assert llm["status"] == "pending"
    assert "api → core" in llm["text"]


def test_off_and_extra_items(tmp_path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  checks:\n    disable: [arch.patterns]\n"
        "    items:\n      - id: custom.note\n        question: 还要看什么\n"
        "        needs: [arch.map]\n        executors: [llm]\n        artifact: 一段结论\n",
        encoding="utf-8",
    )
    rows, _notes = run_checks(str(tmp_path), [])
    ids = [row["id"] for row in rows]
    assert "arch.patterns" not in ids
    assert ids[-1] == "custom.note"
    custom = rows[-1]
    assert custom["status"] == "open"
    assert "arch.map" in custom["executors"][0]["text"] or custom["needs"] == ["arch.map"]


def test_complexity_and_naked_open(tmp_path):
    (tmp_path / "work.py").write_text(
        "def run(rows):\n"
        "    for row in rows:\n"
        "        for cell in row:\n"
        "            sink(cell)\n"
        "def leak():\n"
        "    handle = open('x')\n"
        "    return handle\n",
        encoding="utf-8",
    )
    rows, _notes = run_checks(str(tmp_path), ["work.py"])
    complexity = next(row for row in rows if row["id"] == "complexity.bound")
    code = next(item["text"] for item in complexity["executors"] if item["kind"] == "code")
    assert "work.py run" in code
    assert "2 层循环" in code
    resources = next(row for row in rows if row["id"] == "resource.leak")
    resource_text = next(item["text"] for item in resources["executors"] if item["kind"] == "code")
    assert "open" in resource_text
    assert "leak" in resource_text


def test_report_prints_pending_llm_item():
    case = Case(
        id="case_x", project_root=".", title="示例", scope_type="repo",
        scope=Scope(), triage_level="checkup", status="scanned",
        created_at=1_700_000_000_000, updated_at=1_700_000_000_000,
    )
    checks = [{
        "id": "arch.map",
        "question": "当前架构和设计是什么",
        "artifact": "结构名称",
        "needs": [],
        "status": "open",
        "note": "",
        "executors": [
            {"kind": "code", "status": "done", "text": "模块：api"},
            {"kind": "llm", "status": "pending", "text": "事项 arch.map"},
        ],
    }]
    md = render_scan_report(case, "r1", [], [], ["检查事项 1 项"], checks=checks)
    assert "## 检查事项" in md
    assert "待交卷" in md
    assert "模块：api" in md
    assert md.index("## 检查事项") < md.index("## 覆盖与限制")
