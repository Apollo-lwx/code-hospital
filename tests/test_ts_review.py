"""ts-review：文本层。解析未建，函数长度按 function 和 => 估算。"""

from pathlib import Path

from code_hospital.catalog.coverage import build_ledger
from code_hospital.catalog.depth import scan_depth
from code_hospital.catalog.formulary import route, selected_instrument_ids
from code_hospital.catalog.policy import describe, load_policy
from code_hospital.catalog.ts_review import scan_ts_review

_BODY = """
const password = "super-secret-value";
function grow(x: any) {
  const a = 1;
  const b = 2;
  return a + b;
}
const id = (n: number) => n + 1;
const also = (n: number) => {
  const a = 1;
  const b = 2;
  return a + b;
};
node.innerHTML = name;
const view = <div dangerouslySetInnerHTML={{ __html: name }} />;
console.log(name);
// console.log("skip");
// TODO: later
const spaced = 1; 
type Alias = (n: number) => string;
class Box {
  m() {
    const a = 1;
    const b = 2;
    const c = 3;
    return a;
  }
}
"""


def _ids(raws: list[dict]) -> set[str]:
    return {row["rule_id"] for row in raws}


def test_ts_review_reports_text_rules(tmp_path: Path):
    (tmp_path / "app.ts").write_text(_BODY + ("x" * 201) + "\n", encoding="utf-8")
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  structure:\n    function_lines: 3\n",
        encoding="utf-8",
    )
    raws, notes = scan_ts_review(str(tmp_path), ["app.ts"])
    assert {
        "ts.line-length",
        "ts.function-lines",
        "ts.any",
        "ts.console-log",
        "ts.todo",
        "ts.trailing-whitespace",
        "ts.hardcoded-secret",
        "ts.dom-write",
    } <= _ids(raws)
    symbols = {row["symbol"] for row in raws if row["rule_id"] == "ts.function-lines"}
    assert symbols == {"grow", "also"}
    assert sum(1 for row in raws if row["rule_id"] == "ts.console-log") == 1
    assert sum(1 for row in raws if row["rule_id"] == "ts.dom-write") == 2
    assert any("TS 解析未建" in note and "估算" in note for note in notes)


def test_comments_strings_and_javascript_stay_out(tmp_path: Path):
    (tmp_path / "quiet.ts").write_text(
        'const company = "any";\n'
        '// console.log(1);\n'
        'const label = "innerHTML = x";\n',
        encoding="utf-8",
    )
    (tmp_path / "page.js").write_text("node.innerHTML = html;\nconsole.log(1);\n", encoding="utf-8")
    raws, notes = scan_ts_review(str(tmp_path), ["quiet.ts", "page.js"])
    assert raws == []
    assert all("无 TypeScript" not in note for note in notes)


def test_secrets_in_tests_are_skipped(tmp_path: Path):
    (tmp_path / "app.test.ts").write_text(
        'const password = "super-secret-value";\n',
        encoding="utf-8",
    )
    raws, _notes = scan_ts_review(str(tmp_path), ["app.test.ts"])
    assert raws == []


def test_zero_thresholds_drop_length_rules(tmp_path: Path):
    (tmp_path / "app.ts").write_text(
        "function grow() {\n  const a = 1;\n  const b = 2;\n  return a;\n}\nconsole.log(1);\n" + ("x" * 50) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  structure:\n    function_lines: 0\n  ts:\n    line_length: 0\n",
        encoding="utf-8",
    )
    raws, notes = scan_ts_review(str(tmp_path), ["app.ts"])
    assert "ts.function-lines" not in _ids(raws)
    assert "ts.line-length" not in _ids(raws)
    assert "ts.console-log" in _ids(raws)
    assert any("单行再长也不记入报告" in note for note in notes)
    text = describe(load_policy(str(tmp_path)), 0)
    assert "TypeScript 单行再长也不记入报告" in text
    assert "函数再长也不记入报告" in text


def test_depth_skips_inner_html_on_typescript_only(tmp_path: Path):
    (tmp_path / "app.ts").write_text(
        "node.innerHTML = name;\neval(name);\n",
        encoding="utf-8",
    )
    (tmp_path / "page.js").write_text("node.innerHTML = html;\n", encoding="utf-8")
    held, _limits = scan_depth(
        str(tmp_path), ["app.ts", "page.js"], ["security-ruler"], ts_review=True,
    )
    rows = [row for rows in held.values() for row in rows]
    ts_dom = [row for row in rows if row["rule_id"] == "security.dom-write" and row["file_path"].endswith(".ts")]
    js_dom = [row for row in rows if row["rule_id"] == "security.dom-write" and row["file_path"].endswith(".js")]
    assert ts_dom == []
    assert js_dom
    assert any(row["rule_id"] == "security.exec" for row in rows)
    alone, _limits = scan_depth(str(tmp_path), ["app.ts"], ["security-ruler"], ts_review=False)
    alone_rules = {row["rule_id"] for rows in alone.values() for row in rows}
    assert "security.dom-write" in alone_rules


def test_ts_files_select_ts_review():
    checkup = selected_instrument_ids("checkup", ["src/app.ts"], [], [])
    assert "ts-review" in checkup
    assert "python-review" not in checkup
    assert "java-review" not in checkup
    script = selected_instrument_ids("checkup", ["src/page.js"], [], [])
    assert "ts-review" not in script
    defect = selected_instrument_ids("outpatient", ["src/app.tsx"], [], [], "defect")
    assert "ts-review" in defect
    security = selected_instrument_ids("checkup", ["src/app.ts"], [], [], "security")
    assert "ts-review" in security


def test_ts_rules_route_to_general_and_ledger_marks_the_estimate():
    assert route("ts-review", "ts.any", "major") == "general"
    assert route("ts-review", "ts.hardcoded-secret", "critical") == "general"
    text = describe(load_policy("."), 0)
    assert "TypeScript 单行超过 200 个字符算问题" in text
    lines, ledger = build_ledger("checkup", [{
        "tool": "ts-review", "available": True, "outcome": "ok",
    }], ".", ["app.ts"])
    assert any("TS 解析未建" in line and "估算" in line for line in lines)
    assert ledger["units"][0]["status"] == "partial"
