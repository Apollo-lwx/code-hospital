"""python-review：参数、分支和四条与深度尺重叠的规则。函数行数仍归 structure-ruler。"""

from pathlib import Path

from code_hospital.catalog.depth import scan_depth
from code_hospital.catalog.formulary import route, selected_instrument_ids
from code_hospital.catalog.policy import describe, load_policy
from code_hospital.catalog.python_review import scan_python_review

_BODY = """
api_key = "super-secret-value"

def too_many(a, b, c, d, e, f):
    return a

class Box:
    def ok(self, a, b, c, d, e):
        return a

def branches(x):
    if x: return 1
    if x: return 1
    if x: return 1
    if x: return 1
    if x: return 1
    if x: return 1
    if x: return 1
    if x: return 1
    if x: return 1
    if x: return 1
    if x: return 1

def outer(x):
    if x:
        return 1
    def inner(y):
        if y:
            if y:
                if y:
                    if y:
                        if y:
                            if y:
                                if y:
                                    if y:
                                        if y:
                                            if y:
                                                if y:
                                                    return y
    return inner

def shared(items=[]):
    return items

def run(cmd):
    return eval(cmd)

def swallow():
    try:
        1 / 0
    except:
        seen = 1
        return seen
"""


def _ids(raws: list[dict]) -> set[str]:
    return {row["rule_id"] for row in raws}


def test_python_review_reports_its_own_rules(tmp_path: Path):
    (tmp_path / "app.py").write_text(_BODY, encoding="utf-8")
    raws, notes = scan_python_review(str(tmp_path), ["app.py"])
    assert _ids(raws) == {
        "python.too-many-params",
        "python.branch-count",
        "python.mutable-default",
        "python.exec",
        "python.bare-except",
        "python.hardcoded-secret",
    }
    assert any("structure-ruler" in note for note in notes)
    assert "python.too-many-params" not in {row["symbol"] for row in raws}


def test_nested_function_branches_stay_inside(tmp_path: Path):
    (tmp_path / "app.py").write_text(_BODY, encoding="utf-8")
    raws, _notes = scan_python_review(str(tmp_path), ["app.py"])
    symbols = {row["symbol"] for row in raws if row["rule_id"] == "python.branch-count"}
    assert symbols == {"branches", "inner"}
    assert "outer" not in symbols


def test_method_self_is_not_a_parameter(tmp_path: Path):
    (tmp_path / "app.py").write_text(_BODY, encoding="utf-8")
    raws, _notes = scan_python_review(str(tmp_path), ["app.py"])
    symbols = {row["symbol"] for row in raws if row["rule_id"] == "python.too-many-params"}
    assert symbols == {"too_many"}


def test_zero_thresholds_drop_params_and_branches(tmp_path: Path):
    (tmp_path / "app.py").write_text(_BODY, encoding="utf-8")
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  structure:\n    parameters: 0\n    branches: 0\n",
        encoding="utf-8",
    )
    raws, _notes = scan_python_review(str(tmp_path), ["app.py"])
    assert "python.too-many-params" not in _ids(raws)
    assert "python.branch-count" not in _ids(raws)
    assert "python.bare-except" in _ids(raws)
    text = describe(load_policy(str(tmp_path)), 0)
    assert "参数再多也不记入报告" in text
    assert "分支再多也不记入报告" in text


def test_secrets_in_tests_are_skipped(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        'api_key = "super-secret-value"\n',
        encoding="utf-8",
    )
    raws, _notes = scan_python_review(str(tmp_path), ["tests/test_app.py"])
    assert raws == []


def test_long_function_is_not_a_python_review_finding(tmp_path: Path):
    body = "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(90))
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    raws, _notes = scan_python_review(str(tmp_path), ["a.py"])
    assert raws == []


def test_depth_skips_overlap_when_python_review_is_selected(tmp_path: Path):
    (tmp_path / "app.py").write_text(_BODY, encoding="utf-8")
    files = ["app.py"]
    wanted = ["defect-ruler", "security-ruler"]
    held, _limits = scan_depth(str(tmp_path), files, wanted, python_review=True)
    rules = {row["rule_id"] for rows in held.values() for row in rows}
    assert "defect.bare-except" not in rules
    assert "defect.mutable-default" not in rules
    assert "security.exec" not in rules
    assert "security.hardcoded-secret" not in rules
    alone, _limits = scan_depth(str(tmp_path), files, wanted, python_review=False)
    alone_rules = {row["rule_id"] for rows in alone.values() for row in rows}
    assert {
        "defect.bare-except",
        "defect.mutable-default",
        "security.exec",
        "security.hardcoded-secret",
    } <= alone_rules


def test_checkup_selects_python_review_and_security_visit_does_not():
    checkup = selected_instrument_ids("checkup", ["src/a.py"], [], [])
    assert "python-review" in checkup
    security = selected_instrument_ids("checkup", ["src/a.py"], [], [], "security")
    assert "python-review" not in security
    defect = selected_instrument_ids("outpatient", ["src/a.py"], [], [], "defect")
    assert "python-review" in defect


def test_python_rules_route_to_python_except_critical():
    assert route("python-review", "python.too-many-params", "minor") == "python"
    assert route("python-review", "python.bare-except", "major") == "python"
    assert route("python-review", "python.exec", "critical") == "general"
