"""重症号与 SpotBugs 仪器：只看本次改动，缺仪器不能写成正常。"""

import subprocess
from pathlib import Path

import pytest

from code_hospital.catalog.engines import engines_for
from code_hospital.catalog.formulary import match_finding, selected_instrument_ids
from code_hospital.catalog.severe import resolve_change_set, scan_impact, scan_taint
from code_hospital.catalog.spotbugs import SpotBugsEngine, parse_spotbugs_xml
from code_hospital.catalog.visits import coverage_line
from code_hospital.departments import reception, scanner

_CODE = """
def lookup(request):
    name = request.args.get("name")
    query = f"select * from t where name = '{name}'"
    cur.execute(query)

def run(request):
    cmd = request.form.get("cmd")
    os.system(cmd)

def read(request):
    path = request.args.get("path")
    joined = "data/" + path
    open(joined)

def fetch(request):
    url = request.args.get("url")
    requests.get(url)

def constant(request):
    cur.execute("select 1")

def cleaned(request):
    name = request.args.get("name")
    name = "fixed"
    cur.execute(name)
"""

_XML = """
<BugCollection>
  <BugInstance type="NP_NULL_ON_SOME_PATH" priority="2">
    <LongMessage>possible null pointer</LongMessage>
    <SourceLine sourcepath="com/foo/Bar.java" start="12"/>
  </BugInstance>
  <BugInstance type="DMI_HARDCODED" priority="3">
    <ShortMessage>other file</ShortMessage>
    <SourceLine sourcepath="com/other/Skip.java" start="1"/>
  </BugInstance>
</BugCollection>
"""


def test_intra_function_taint_stops_at_clean_assign(tmp_path: Path):
    (tmp_path / "app.py").write_text(_CODE, encoding="utf-8")
    raws, limits, outcome = scan_taint(str(tmp_path), ["app.py"])
    assert outcome == "ok"
    rules = {row["rule_id"] for row in raws}
    assert rules == {
        "severe.taint-sql",
        "severe.taint-command",
        "severe.taint-path",
        "severe.taint-request",
    }
    assert any("跨函数没有追" in item for item in limits)
    symbols = {row["symbol"] for row in raws}
    assert "constant" not in symbols
    assert "cleaned" not in symbols


def test_optional_import_is_not_a_swallowed_exception(tmp_path: Path):
    from code_hospital.catalog.depth import scan_depth

    (tmp_path / "probe.py").write_text(
        "def kicad_available():\n"
        "    try:\n"
        "        import pcbnew\n"
        "        return True\n"
        "    except Exception:\n"
        "        pass\n"
        "    return False\n",
        encoding="utf-8",
    )
    grouped, _limits = scan_depth(str(tmp_path), ["probe.py"], ["defect-ruler"])
    assert "defect.swallowed" not in {row["rule_id"] for row in grouped["defect-ruler"]}


def test_ambiguous_name_does_not_cross_files(tmp_path: Path):
    (tmp_path / "cli.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text(
        "def main():\n    return 2\n\ndef caller():\n    main()\n",
        encoding="utf-8",
    )
    raws, _limits, outcome = scan_impact(str(tmp_path), ["cli.py"])
    assert outcome == "ok"
    assert len(raws) == 1
    assert "other.py" not in raws[0]["message"]
    assert "没有找到调用方" in raws[0]["message"]


def test_same_file_reference_counts_as_a_use(tmp_path: Path):
    (tmp_path / "cli.py").write_text(
        "def _cmd_diff():\n"
        "    return 1\n\n"
        "def build_parser():\n"
        "    sub.set_defaults(func=_cmd_diff)\n",
        encoding="utf-8",
    )
    raws, _limits, outcome = scan_impact(str(tmp_path), ["cli.py"])
    assert outcome == "ok"
    command = next(row for row in raws if row["symbol"] == "_cmd_diff")
    assert "cli.py 的 build_parser" in command["message"]


def test_impact_lists_only_functions_the_diff_touches(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("def keep():\n    return 1\n\ndef touch():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=tester", "commit", "-q", "-m", "init"],
        cwd=tmp_path, check=True,
    )
    (tmp_path / "app.py").write_text("def keep():\n    return 1\n\ndef touch():\n    return 3\n", encoding="utf-8")
    raws, limits, outcome = scan_impact(str(tmp_path), ["app.py"])
    assert outcome == "ok"
    assert {row["symbol"] for row in raws} == {"touch"}
    assert any("没改到的函数没有单列" in item for item in limits)


def test_impact_lists_callers_and_stays_unprescribed(tmp_path: Path):
    (tmp_path / "app.py").write_text("def lookup(request):\n    return request\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text("def caller():\n    lookup(None)\n", encoding="utf-8")
    raws, _limits, outcome = scan_impact(str(tmp_path), ["app.py"])
    assert outcome == "ok"
    assert len(raws) == 1
    assert raws[0]["rule_id"] == "severe.review-impact"
    assert "caller.py 的 caller" in raws[0]["message"]
    assert "这是谁的数据" in raws[0]["message"]
    assert "失败时返回了什么" in raws[0]["message"]
    assert "状态有没有跳步" in raws[0]["message"]
    impact = match_finding("impact-review", "severe.review-impact", "info")
    assert impact.matched is False
    assert impact.confidence < 0.5
    for rule_id in (
        "severe.taint-sql", "severe.taint-command", "severe.taint-path", "severe.taint-request",
    ):
        matched = match_finding("taint-ruler", rule_id, "major", verdict="confirmed")
        assert matched.matched is True
        assert matched.formula.id == "repair"
        assert matched.specialty_id == "security"
        blocked = match_finding("taint-ruler", rule_id, "major", verdict="needs_validation")
        assert blocked.matched is False


def test_repo_without_diff_is_unchecked(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("def lookup():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=tester", "commit", "-q", "-m", "init"],
        cwd=tmp_path, check=True,
    )
    files, notes, usable = resolve_change_set(str(tmp_path), ["app.py"], "repo")
    assert usable is False
    assert files == []
    assert any("重症未查" in item for item in notes)


def test_spotbugs_xml_keeps_java_in_the_slice():
    raws = parse_spotbugs_xml(_XML, ["src/main/java/com/foo/Bar.java"])
    assert len(raws) == 1
    assert raws[0]["rule_id"] == "NP_NULL_ON_SOME_PATH"
    assert raws[0]["severity"] == "major"
    assert raws[0]["file_path"] == "src/main/java/com/foo/Bar.java"
    assert raws[0]["line_start"] == 12
    assert raws[0]["tool"] == "spotbugs"


def test_spotbugs_missing_binary_or_classes_is_terminal(tmp_path: Path, monkeypatch):
    (tmp_path / "A.java").write_text("class A {}\n", encoding="utf-8")
    engine = SpotBugsEngine()
    raws, notes, outcome = engine.run(str(tmp_path), ["A.java"], 5)
    assert raws == []
    assert outcome == "terminal"
    assert any("未到岗" in item or "没有找到" in item for item in notes)
    monkeypatch.setattr("code_hospital.catalog.spotbugs.shutil.which", lambda _name: None)
    raws, notes, outcome = engine.run(str(tmp_path), ["A.java"], 5)
    assert outcome == "terminal"
    assert any("未到岗" in item for item in notes)


def test_engine_table_and_visit_panels():
    assert engines_for({"spotbugs"})
    assert engines_for({"ruff"}) == []
    checkup = selected_instrument_ids("checkup", ["a.py"], [], [])
    assert "taint-ruler" not in checkup
    assert "spotbugs" not in checkup
    severe_py = selected_instrument_ids("checkup", ["a.py"], [], [], "severe")
    assert severe_py == ["taint-ruler", "impact-review"]
    severe_java = selected_instrument_ids("checkup", ["A.java"], [], [], "severe")
    assert severe_java == ["spotbugs"]
    general = selected_instrument_ids("checkup", ["src/a.py"], [], [], "general")
    assert "taint-ruler" not in general
    assert "impact-review" not in general
    assert "spotbugs" not in general
    assert "重症未查" in coverage_line("general", ["ruff"], [{
        "tool": "ruff", "available": True, "outcome": "ok",
    }])


@pytest.mark.skipif(subprocess.run(["git", "--version"], check=False).returncode != 0, reason="需要 git")
async def test_severe_visit_scans_the_given_paths(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text(_CODE, encoding="utf-8")
    (tmp_path / "caller.py").write_text("def caller():\n    lookup(None)\n", encoding="utf-8")
    opened = await reception.register(
        title="重症", scope_type="paths", scope={"paths": ["app.py"]},
        visit_kind="severe", project_root=str(tmp_path),
    )
    scanned = await scanner.scan(
        case_id=opened["case_id"], stage_confirmed=True, project_root=str(tmp_path),
    )
    rules = {row["rule_id"] for row in scanned["findings"]["data"]}
    assert "severe.taint-sql" in rules
    assert "severe.review-impact" in rules
    taint = next(row for row in scanned["findings"]["data"] if row["rule_id"] == "severe.taint-sql")
    assert taint["verdict"] == "confirmed"
    impact = next(row for row in scanned["findings"]["data"] if row["rule_id"] == "severe.review-impact")
    assert impact["verdict"] == "needs_validation"
    assert any(row["tool"] == "taint-ruler" and row["outcome"] == "ok" for row in scanned["devices"])
    report = Path(scanned["report_path"]).read_text(encoding="utf-8")
    assert "号别重症" in report
    assert "跨函数" in report
    assert "未覆盖" in report


@pytest.mark.skipif(subprocess.run(["git", "--version"], check=False).returncode != 0, reason="需要 git")
async def test_repo_severe_without_diff_says_unchecked(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".hospital\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("def lookup():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=tester", "commit", "-q", "-m", "init"],
        cwd=tmp_path, check=True,
    )
    opened = await reception.register(
        title="空重症", scope_type="repo", scope={},
        visit_kind="severe", project_root=str(tmp_path),
    )
    scanned = await scanner.scan(
        case_id=opened["case_id"], stage_confirmed=True, project_root=str(tmp_path),
    )
    by_tool = {row["tool"]: row for row in scanned["devices"]}
    assert by_tool["taint-ruler"]["outcome"] == "terminal"
    assert by_tool["impact-review"]["outcome"] == "terminal"
    report = Path(scanned["report_path"]).read_text(encoding="utf-8")
    assert "重症未查" in report
