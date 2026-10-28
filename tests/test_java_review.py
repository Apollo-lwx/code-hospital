"""java-review：正则层。方法行数仍归 structure-ruler。空 catch 与缺陷尺不双报。"""

from pathlib import Path

from code_hospital.catalog.depth import scan_depth
from code_hospital.catalog.formulary import route, selected_instrument_ids
from code_hospital.catalog.java_review import scan_java_review
from code_hospital.catalog.policy import describe, load_policy

_BODY = """
import java.util.concurrent.Executors;

class App {
    String password = "super-secret-value";

    void m(String name) {
        System.out.println(name);
        new Thread(() -> { }).start();
        Executors.newFixedThreadPool(2);
        stmt.execute("select * from t where name = " + name);
        try {
            work();
        } catch (Exception e) {
        }
        try { work(); } catch (Exception e) {}
        Runtime.getRuntime().exec(name);
        // System.out.println("not this");
        stmt.createStatement();
    }
}
"""

_LONG = "x" * 121


def _ids(raws: list[dict]) -> set[str]:
    return {row["rule_id"] for row in raws}


def test_java_review_reports_regex_rules(tmp_path: Path):
    (tmp_path / "App.java").write_text(_BODY + _LONG + "\n", encoding="utf-8")
    raws, notes = scan_java_review(str(tmp_path), ["App.java"])
    assert _ids(raws) == {
        "java.line-length",
        "java.system-out",
        "java.raw-thread",
        "java.executors",
        "java.sql-concat",
        "java.empty-catch",
        "java.hardcoded-secret",
    }
    assert sum(1 for row in raws if row["rule_id"] == "java.empty-catch") == 2
    assert sum(1 for row in raws if row["rule_id"] == "java.sql-concat") == 1
    assert any("structure-ruler" in note for note in notes)


def test_comments_imports_and_create_statement_stay_out(tmp_path: Path):
    (tmp_path / "Quiet.java").write_text(
        "import java.util.concurrent.Executors;\n"
        "// System.out.println(1);\n"
        "class Quiet { void m() { stmt.createStatement(); } }\n",
        encoding="utf-8",
    )
    raws, _notes = scan_java_review(str(tmp_path), ["Quiet.java"])
    assert raws == []


def test_secrets_in_tests_are_skipped(tmp_path: Path):
    path = tmp_path / "src" / "test" / "java"
    path.mkdir(parents=True)
    (path / "AppTest.java").write_text(
        'class AppTest { String password = "super-secret-value"; }\n',
        encoding="utf-8",
    )
    raws, _notes = scan_java_review(str(tmp_path), ["src/test/java/AppTest.java"])
    assert raws == []


def test_zero_line_length_drops_only_that_rule(tmp_path: Path):
    (tmp_path / "App.java").write_text("class App { void m() { System.out.println(1); } }\n" + _LONG + "\n", encoding="utf-8")
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  java:\n    line_length: 0\n",
        encoding="utf-8",
    )
    raws, notes = scan_java_review(str(tmp_path), ["App.java"])
    assert _ids(raws) == {"java.system-out"}
    assert any("再长也不记入报告" in note for note in notes)
    text = describe(load_policy(str(tmp_path)), 0)
    assert "Java 单行再长也不记入报告" in text


def test_short_methods_are_not_java_review_findings(tmp_path: Path):
    body = "class App {\n  void big() {\n" + "".join(f"    int x{i} = {i};\n" for i in range(40)) + "  }\n}\n"
    (tmp_path / "App.java").write_text(body, encoding="utf-8")
    raws, _notes = scan_java_review(str(tmp_path), ["App.java"])
    assert raws == []


def test_depth_skips_empty_catch_when_java_review_is_selected(tmp_path: Path):
    (tmp_path / "App.java").write_text(
        "class App { void m(String name) {\n"
        "  try { work(); } catch (Exception e) {}\n"
        "  Runtime.getRuntime().exec(name);\n"
        "} }\n",
        encoding="utf-8",
    )
    files = ["App.java"]
    wanted = ["defect-ruler", "security-ruler"]
    held, _limits = scan_depth(str(tmp_path), files, wanted, java_review=True)
    rules = {row["rule_id"] for rows in held.values() for row in rows}
    assert "defect.empty-catch" not in rules
    assert "security.shell" in rules
    alone, _limits = scan_depth(str(tmp_path), files, wanted, java_review=False)
    alone_rules = {row["rule_id"] for rows in alone.values() for row in rows}
    assert "defect.empty-catch" in alone_rules
    assert "security.shell" in alone_rules


def test_java_files_select_java_review_on_checkup_defect_and_security():
    checkup = selected_instrument_ids("checkup", ["src/App.java"], [], [])
    assert "java-review" in checkup
    assert "python-review" not in checkup
    defect = selected_instrument_ids("outpatient", ["src/App.java"], [], [], "defect")
    assert "java-review" in defect
    security = selected_instrument_ids("checkup", ["src/App.java"], [], [], "security")
    assert "java-review" in security
    python_security = selected_instrument_ids("checkup", ["src/a.py"], [], [], "security")
    assert "java-review" not in python_security


def test_java_rules_route_to_java_except_critical():
    assert route("java-review", "java.empty-catch", "major") == "java"
    assert route("java-review", "java.line-length", "minor") == "java"
    assert route("java-review", "java.sql-concat", "critical") == "general"
    text = describe(load_policy("."), 0)
    assert "Java 单行超过 120 个字符算问题" in text
