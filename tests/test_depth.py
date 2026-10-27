"""号别和院内深度尺：安全、缺陷、性能、算法、供应链、测试缺口。"""

import subprocess
from pathlib import Path

import pytest

from code_hospital.catalog.depth import scan_depth
from code_hospital.catalog.formulary import match_finding, selected_instrument_ids
from code_hospital.catalog.visits import normalize_visit_kind
from code_hospital.core.errors import HospitalError
from code_hospital.departments import reception, scanner

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], check=False).returncode != 0, reason="需要 git")


_SAMPLE = """
def make_api_key():
    import random
    api_key = "super-secret-value"
    return random.random()

def run(cmd):
    import os
    os.system(cmd)
    return eval(cmd)

def load(blob):
    import pickle
    return pickle.loads(blob)

def query(cur, name):
    cur.execute(f"select * from t where name = {name}")

def branches(flag):
    if flag:
        return 1
    else:
        return 1

def shared(items=[]):
    items.append(1)
    return items

def swallow():
    try:
        1 / 0
    except Exception:
        pass

def same(value):
    return value == value

def later():
    return 1
    leftover = 2
    return leftover

def cubic(xs, ys, zs):
    for x in xs:
        for y in ys:
            for z in zs:
                z

def hot(rows):
    out = ""
    for row in rows:
        open(row)
        out += row
        if row in items:
            pass
        rows.sort()
"""


def _rules(grouped: dict[str, list[dict]]) -> set[str]:
    return {row["rule_id"] for rows in grouped.values() for row in rows}


def test_depth_rulers_catch_real_patterns(tmp_path: Path):
    (tmp_path / "app.py").write_text(_SAMPLE, encoding="utf-8")
    (tmp_path / "page.js").write_text("function paint(html) { node.innerHTML = html; eval(html); }\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(
        "flask==2.0.0\nrequests\ngit+https://example.com/lib.git\nhttp://example.com/pkg.tgz\n",
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text('{"dependencies": {"left-pad": "*"}}\n', encoding="utf-8")
    grouped, _limits = scan_depth(
        str(tmp_path),
        ["app.py", "page.js", "requirements.txt", "package.json"],
        ["security-ruler", "defect-ruler", "perf-ruler", "algo-ruler", "supply-ruler", "test-gap"],
        scope_type="paths",
        visit_kind="general",
    )
    rules = _rules(grouped)
    for rule_id, rows in (
        ("algo.sort-in-loop", "minor"),
        ("algo.nested-loop", "minor"),
        ("perf.loop-io", "minor"),
        ("defect.swallowed", "major"),
    ):
        assert {row["severity"] for rows_ in grouped.values() for row in rows_ if row["rule_id"] == rule_id} == {rows}
    assert {
        "security.hardcoded-secret",
        "security.weak-random",
        "security.shell",
        "security.exec",
        "security.deserialize",
        "security.sql-format",
        "security.dom-write",
        "defect.identical-branches",
        "defect.mutable-default",
        "defect.swallowed",
        "defect.self-compare",
        "defect.unreachable",
        "algo.nested-loop",
        "algo.string-concat-loop",
        "algo.linear-membership",
        "algo.sort-in-loop",
        "perf.loop-io",
        "supply.unpinned",
        "supply.unpinned-vcs",
        "supply.http-source",
        "supply.wildcard",
        "supply.missing-lock",
        "test.uncovered-file",
    } <= rules


def test_repo_general_does_not_file_every_missing_test(tmp_path: Path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    grouped, limits = scan_depth(
        str(tmp_path), ["app.py"], ["test-gap"], scope_type="repo", visit_kind="general",
    )
    assert grouped["test-gap"] == []
    assert any("test" in item for item in limits)


def test_visit_panels_leave_checkup_shallow():
    files = ["src/a.py"]
    checkup = selected_instrument_ids("checkup", files, [], [])
    assert "security-ruler" not in checkup
    assert "structure-ruler" in checkup
    security = selected_instrument_ids("checkup", files, [], [], "security")
    assert "security-ruler" in security
    assert "structure-ruler" not in security
    general = selected_instrument_ids("outpatient", files, [], [], "general")
    assert "security-ruler" in general
    assert "ruff" in general
    assert "type-ruler" in general


def test_depth_rules_enter_the_formulary():
    assert match_finding("security-ruler", "security.exec", "critical").formula.id == "seal"
    assert match_finding("security-ruler", "security.exec", "critical").specialty_id == "security"
    assert match_finding(
        "security-ruler", "security.exec", "minor", verdict="needs_validation",
    ).matched is False
    assert match_finding("security-ruler", "security.tls-verify", "major").formula.id == "guard"
    assert match_finding("defect-ruler", "defect.mutable-default", "major").formula.id == "guard"
    assert match_finding("defect-ruler", "defect.empty-catch", "major").specialty_id == "java"
    assert match_finding("algo-ruler", "algo.sort-in-loop", "major").formula.id == "throttle"
    assert match_finding("perf-ruler", "perf.loop-io", "major").formula.id == "throttle"
    assert match_finding("supply-ruler", "supply.missing-lock", "major").formula.id == "supply"
    assert match_finding("test-gap", "test.uncovered-file", "major").formula.id == "contrast"


def test_unknown_visit_kind_is_rejected():
    with pytest.raises(ValueError):
        normalize_visit_kind("nope")


async def test_security_visit_survives_without_external_scanners(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("def f(value):\n    return eval(value)\n", encoding="utf-8")
    with pytest.raises(HospitalError):
        await reception.register(
            title="坏号", scope_type="paths", scope={"paths": ["a.py"]},
            visit_kind="nope", project_root=str(tmp_path),
        )
    opened = await reception.register(
        title="安全", scope_type="paths", scope={"paths": ["a.py"]},
        visit_kind="security", project_root=str(tmp_path),
    )
    assert opened["visit_kind"] == "security"
    scanned = await scanner.scan(
        case_id=opened["case_id"], stage_confirmed=True, project_root=str(tmp_path),
    )
    rules = {row["rule_id"] for row in scanned["findings"]["data"]}
    assert "security.exec" in rules
    held = next(row for row in scanned["findings"]["data"] if row["rule_id"] == "security.exec")
    assert held["verdict"] == "needs_validation"
    assert held["severity"] == "minor"
    assert any(row["tool"] == "security-ruler" and row["outcome"] == "ok" for row in scanned["devices"])
    report = Path(scanned["report_path"]).read_text(encoding="utf-8")
    assert "号别安全" in report
    assert "覆盖账" in report
    assert "认证边界" in report
    assert "未覆盖" in report
