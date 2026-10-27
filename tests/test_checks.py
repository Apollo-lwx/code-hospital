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


def test_checkup_skips_principle_and_java_packs(tmp_path):
    rows, notes = run_checks(str(tmp_path), ["a.py"])
    ids = [row["id"] for row in rows]
    assert "principle.srp" not in ids
    assert "pattern.recognize" not in ids
    assert "ej.equals-hash" not in ids
    assert "access.authz" not in ids
    assert "naming.clear" not in ids
    assert notes == []


def test_architecture_visit_adds_principles_and_patterns(tmp_path):
    (tmp_path / "svc.py").write_text(
        "class Child(Parent):\n"
        "    def run(self):\n"
        "        return Handler()\n",
        encoding="utf-8",
    )
    rows, notes = run_checks(str(tmp_path), ["svc.py"], visit_kind="architecture")
    ids = [row["id"] for row in rows]
    assert "principle.srp" in ids
    assert "pattern.recognize" in ids
    assert "ej.equals-hash" not in ids
    assert notes == []
    signals = next(row for row in rows if row["id"] == "design.signals")
    text = next(item["text"] for item in signals["executors"] if item["kind"] == "code")
    assert "Child 继承 Parent" in text
    assert "直接构造 Handler" in text
    assert signals["status"] == "done"


def test_java_pack_on_architecture_visit(tmp_path):
    (tmp_path / "Box.java").write_text(
        "class Box extends Crate {\n"
        "  public boolean equals(Object o) { return true; }\n"
        "}\n",
        encoding="utf-8",
    )
    rows, notes = run_checks(str(tmp_path), ["Box.java"], visit_kind="architecture")
    assert notes == []
    assert "ej.equals-hash" in [row["id"] for row in rows]
    signals = next(row for row in rows if row["id"] == "ej.signals")
    text = next(item["text"] for item in signals["executors"] if item["kind"] == "code")
    assert "有 equals 没有 hashCode" in text
    assert "Box 继承 Crate" in text
    close = next(row for row in rows if row["id"] == "ej.close")
    assert close["status"] == "open"
    assert "resource.leak" in close["needs"]


def test_named_pack_without_java_is_skipped(tmp_path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  checks:\n    packs: [effective-java, principles]\n",
        encoding="utf-8",
    )
    rows, notes = run_checks(str(tmp_path), ["a.py"])
    ids = [row["id"] for row in rows]
    assert "principle.srp" in ids
    assert "ej.equals-hash" not in ids
    assert any("effective-java" in note for note in notes)


def test_security_visit_checks_access_concurrency_and_serialization(tmp_path):
    (tmp_path / "orders.py").write_text(
        "import pickle\n"
        "import threading\n"
        "def delete_order(order_id):\n"
        "    db.delete(order_id)\n"
        "def read_cache(key):\n"
        "    item = cache.get(key)\n"
        "    return item\n"
        "def start():\n"
        "    threading.Thread(target=delete_order).start()\n"
        "def load(blob):\n"
        "    return pickle.loads(blob)\n",
        encoding="utf-8",
    )
    rows, _notes = run_checks(str(tmp_path), ["orders.py"], visit_kind="security")
    ids = [row["id"] for row in rows]
    assert "access.authz" in ids
    assert "access.cache" in ids
    assert "concurrency.shared" in ids
    assert "serialization.boundary" in ids
    assert "naming.clear" not in ids
    access = next(row for row in rows if row["id"] == "access.signals")
    access_text = next(item["text"] for item in access["executors"] if item["kind"] == "code")
    assert "delete_order" in access_text
    assert "把缓存里取出的对象交出去" in access_text
    concurrency = next(row for row in rows if row["id"] == "concurrency.signals")
    concurrency_text = next(item["text"] for item in concurrency["executors"] if item["kind"] == "code")
    assert "没看见 Lock" in concurrency_text
    serial = next(row for row in rows if row["id"] == "serialization.signals")
    serial_text = next(item["text"] for item in serial["executors"] if item["kind"] == "code")
    assert "pickle.loads" in serial_text


def test_architecture_visit_checks_naming(tmp_path):
    (tmp_path / "api.py").write_text(
        "def f(user):\n"
        "    return user\n",
        encoding="utf-8",
    )
    rows, _notes = run_checks(str(tmp_path), ["api.py"], visit_kind="architecture")
    ids = [row["id"] for row in rows]
    assert "naming.docs" in ids
    assert "access.authz" in ids
    naming = next(row for row in rows if row["id"] == "naming.signals")
    text = next(item["text"] for item in naming["executors"] if item["kind"] == "code")
    assert "名字过短" in text
    assert "没有说明" in text
