"""名册第一期：分流、药柜、检查单、结构尺、成药行裁剪、随包规则和分层契约。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import ScannerAdapter
from code_hospital.adapters.ruff_adapter import ruff_check_args
from code_hospital.catalog.formulary import (
    match_finding,
    scan_orders_for,
    scope_key,
    selected_instrument_ids,
)
from code_hospital.catalog.layers import resolve_contract, scan_layers
from code_hospital.catalog.packs import parse_pack_json
from code_hospital.catalog.policy import keeps, load_policy
from code_hospital.catalog.runners import eslint_configured, parse_eslint_json, parse_jscpd_json
from code_hospital.catalog.structure import parse_lizard_csv, scan_structure, thresholds
from code_hospital.catalog.typecheck import parse_mypy, parse_pyright, type_backend
from code_hospital.core.errors import HospitalError
from code_hospital.departments import diagnostician, reception, scanner
from code_hospital.departments.pharmacist import _clip_to_spans
from code_hospital.departments.reception import _check_catalog_request

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def test_match_order_and_confidences():
    debride = match_finding("ruff", "F841", "major")
    assert debride.formula.id == "debride"
    assert debride.method == "autofix"
    assert debride.confidence == 0.86
    assert debride.specialty_id == "python"

    repair = match_finding("ruff", "F821", "major")
    assert repair.formula.id == "repair"
    assert repair.confidence == 0.72

    leaked = match_finding("gitleaks", "generic-api-key", "critical")
    assert leaked.formula.id == "seal"
    assert leaked.confidence == 0.92
    assert leaked.specialty_id == "security"

    ruff_critical = match_finding("ruff", "S105", "critical")
    assert ruff_critical.formula.id == "seal"
    assert ruff_critical.confidence == 0.70

    semgrep_critical = match_finding("semgrep", "python.lang.security.audit", "critical")
    assert semgrep_critical.formula.id == "seal"
    assert semgrep_critical.confidence == 0.62

    unknown = match_finding("custom", "S105", "minor")
    assert unknown.matched is False
    assert unknown.confidence == 0.40
    assert unknown.formula is None


def test_python_request_does_not_select_type_ruler():
    chosen = selected_instrument_ids("outpatient", ["src/a.py"], ["python"], [])
    assert "type-ruler" not in chosen
    assert "hotspot-ruler" not in chosen
    assert "ruff" in chosen
    assert "structure-ruler" in chosen


def test_unknown_specialty_rejected():
    with pytest.raises(HospitalError) as exc:
        _check_catalog_request(["nope"], [])
    assert exc.value.code == "BadArgument"
    with pytest.raises(HospitalError) as exc:
        _check_catalog_request([], ["eslint"])
    assert exc.value.code == "BadArgument"


def test_retry_note_becomes_scan_order():
    orders = scan_orders_for([], [], [{"id": "ruff", "outcome": "retry"}])
    assert orders == [{"instrument_id": "ruff", "reason": "retry:ruff"}]
    done = scan_orders_for(["performance"], [], [{"id": "hotspot-ruler", "outcome": "terminal"}])
    assert done == []


def test_structure_ruler_keeps_function_span(tmp_path: Path):
    body = "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(90))
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    raws, limits = scan_structure(str(tmp_path), ["a.py"])
    spanned = next(row for row in raws if row["rule_id"] == "structure.function_lines")
    assert spanned["symbol"] == "big"
    assert spanned["severity"] == "nit"
    assert spanned["line_start"] == 1
    assert spanned["line_end"] >= 80
    if shutil.which("lizard"):
        assert "未测量圈复杂度" not in limits
        assert all(row["rule_id"] != "structure.cyclomatic" for row in raws)
    else:
        assert "未测量圈复杂度" in limits


def test_zero_thresholds_turn_the_rulers_off(tmp_path: Path):
    body = "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(90))
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  structure:\n    function_lines: 0\n    file_lines: 0\n    cyclomatic: 0\n",
        encoding="utf-8",
    )
    raws, limits = scan_structure(str(tmp_path), ["a.py"])
    assert raws == []
    assert limits == []
    assert thresholds(str(tmp_path)) == (0, 0, 0)


def test_rule_switches_drop_style_and_keep_pyflakes(tmp_path: Path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  rules:\n    ignore: [FURB, BLE]\n    min_severity: major\n    ruff_select: [F, E9]\n",
        encoding="utf-8",
    )
    policy = load_policy(str(tmp_path))
    assert keeps("F401", "major", policy) is True
    assert keeps("FURB122", "nit", policy) is False
    assert keeps("BLE001", "major", policy) is False
    assert keeps("I001", "nit", policy) is False
    assert ruff_check_args(policy.ruff_select)[-1] == "--select=F,E9"


def test_clip_keeps_only_the_named_lines():
    old = "def f():\n    x = 1\n    return 2\n"
    changed = "def f():\n    return 2\n"
    assert _clip_to_spans(old, changed, [[2, 2]]) == changed
    assert _clip_to_spans(old, "MUTATED\n    x = 1\n    return 2\n", [[2, 2]]) == old


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    x = 1\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


class _QuietRuff(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return []


async def test_performance_request_opens_one_scan_order(git_repo):
    adapters.clear()
    adapters.register(_QuietRuff())
    reg = await reception.register(
        title="性能", scope_type="staged", scope={},
        requested_specialties=["performance"], project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], stage_confirmed=True, project_root=git_repo)
    opened = await diagnostician.diagnose(
        case_id=reg["case_id"], stage_confirmed=True, project_root=git_repo,
    )
    if shutil.which("semgrep"):
        assert opened["status"] == "scanned"
        assert opened["scan_orders"] == [
            {"instrument_id": "hotspot-ruler", "reason": "requested:performance"},
        ]
        assert "diagnoses" not in opened
        return
    assert "diagnoses" in opened
    assert opened.get("scan_orders", []) == []


def test_pack_json_keeps_instrument_rule_and_symbol():
    payload = json.dumps({"results": [{
        "check_id": "src.rules.java.null-deref",
        "path": "A.java",
        "start": {"line": 4},
        "end": {"line": 4},
        "extra": {
            "message": "空引用",
            "severity": "ERROR",
            "metavars": {"$METHOD": {"abstract_content": "hashCode"}},
        },
    }]})
    raw = parse_pack_json(payload, "java-pack")[0]
    assert raw.rule_id == "java.null-deref"
    assert raw.extra["symbol"] == "hashCode"
    assert raw.severity == "major"


def test_normalize_writes_instrument_id(tmp_path: Path):
    (tmp_path / "A.java").write_text("class A {}\n", encoding="utf-8")
    from code_hospital.adapters import RawFinding

    finding = scanner.normalize(
        RawFinding(
            rule_id="java.null-deref", message="空引用", file_path="A.java",
            line_start=1, extra={"symbol": "hashCode"},
        ),
        "semgrep", "case_x", "rpt_x", str(tmp_path),
    )
    assert finding.tool == "java-pack"
    assert finding.symbol == "hashCode"


def test_layer_scope_key_uses_contract_id():
    class _Finding:
        file_path = "storage/db.py"
        rule_id = "layer.forbidden-import"
        tool = "layer-ruler"
        contract_id = "code-hospital"
        symbol = ""
        line_start = 1
        line_end = 1

    assert scope_key(_Finding()) == "contract:code-hospital#rule:layer.forbidden-import"


def test_catalog_layers_flag_upward_import(tmp_path: Path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  layers:\n    - api\n    - storage\n", encoding="utf-8",
    )
    (tmp_path / "api").mkdir()
    (tmp_path / "storage").mkdir()
    (tmp_path / "api" / "handlers.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "storage" / "db.py").write_text("from api.handlers import value\n", encoding="utf-8")
    assert resolve_contract(str(tmp_path))["id"] == "code-hospital"


async def test_import_linter_contract_beats_catalog_layers(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.importlinter]\n", encoding="utf-8")
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  layers:\n    - api\n    - storage\n", encoding="utf-8",
    )
    assert resolve_contract(str(tmp_path))["id"] == "import-linter"
    raws, limits = await scan_layers(str(tmp_path), [])
    assert raws == [] or all(row["contract_id"] == "import-linter" for row in raws)
    assert "未配置分层，分层尺未拍" not in limits
    if shutil.which("lint-imports") is None:
        assert "lint-imports 未到岗，分层尺未拍" in limits


async def test_code_hospital_layers_report_forbidden_import(tmp_path: Path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  layers:\n    - api\n    - storage\n", encoding="utf-8",
    )
    (tmp_path / "storage").mkdir()
    (tmp_path / "storage" / "db.py").write_text("from api.handlers import value\n", encoding="utf-8")
    raws, limits = await scan_layers(str(tmp_path), ["storage/db.py"])
    assert limits == []
    assert raws[0]["rule_id"] == "layer.forbidden-import"
    assert raws[0]["contract_id"] == "code-hospital"
    assert raws[0]["file_path"] == "storage/db.py"


def test_eslint_fixable_is_debride_and_plain_is_repair():
    fixed = match_finding("eslint", "no-unused-vars", "minor", fixable=True)
    assert fixed.formula.id == "debride"
    assert fixed.method == "autofix"
    assert fixed.confidence == 0.86
    assert fixed.specialty_id == "general"
    plain = match_finding("eslint", "no-unused-vars", "minor", fixable=False)
    assert plain.formula.id == "repair"
    assert plain.method == "llm_patch"
    assert plain.confidence == 0.58


def test_jscpd_clone_matches_dedupe():
    matched = match_finding("jscpd", "clone.duplicate", "major")
    assert matched.formula.id == "dedupe"
    assert matched.method == "llm_patch"
    assert matched.specialty_id == "structure"
    assert matched.formula.phase == 1


def test_parse_eslint_and_jscpd(tmp_path: Path):
    eslint_payload = json.dumps([{
        "filePath": str(tmp_path / "a.js"),
        "messages": [{
            "ruleId": "no-unused-vars",
            "severity": 2,
            "message": "x is unused",
            "line": 2,
            "endLine": 2,
            "fix": {"range": [0, 1], "text": ""},
        }],
    }])
    eslint_raw = parse_eslint_json(eslint_payload, str(tmp_path))[0]
    assert eslint_raw.rule_id == "no-unused-vars"
    assert eslint_raw.file_path == "a.js"
    assert eslint_raw.extra["fixable"] is True

    jscpd_payload = json.dumps({"duplicates": [{
        "lines": 8,
        "firstFile": {"name": "a.py", "startLoc": {"line": 1}, "endLoc": {"line": 8}},
        "secondFile": {"name": "b.py", "startLoc": {"line": 3}, "endLoc": {"line": 10}},
    }]})
    clone = parse_jscpd_json(jscpd_payload, str(tmp_path))[0]
    assert clone.rule_id == "clone.duplicate"
    assert clone.line_start == 1
    assert clone.line_end == 8
    assert "b.py" in clone.message

    assert eslint_configured(str(tmp_path)) is False
    (tmp_path / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    assert eslint_configured(str(tmp_path)) is True


def test_type_ruler_matches_repair():
    matched = match_finding("type-ruler", "assignment", "major")
    assert matched.matched is True
    assert matched.formula.id == "repair"
    assert matched.method == "llm_patch"
    assert matched.specialty_id == "python"
    assert matched.confidence == 0.58


def test_cyclomatic_matches_split():
    matched = match_finding("structure-ruler", "structure.cyclomatic", "major")
    assert matched.formula.id == "split"
    assert matched.method == "llm_patch"
    assert matched.specialty_id == "structure"


def test_parse_mypy_pyright_and_lizard(tmp_path: Path):
    mypy_raw = parse_mypy(
        "src/a.py:3:5: error: Incompatible types in assignment  [assignment]\n"
        "src/a.py:1: note: See docs\n",
        str(tmp_path),
    )
    assert mypy_raw[0].rule_id == "assignment"
    assert mypy_raw[0].severity == "major"
    assert mypy_raw[0].line_start == 3
    assert mypy_raw[0].file_path == "src/a.py"
    assert mypy_raw[1].rule_id == "type.note"
    assert mypy_raw[1].severity == "minor"

    pyright_raw = parse_pyright(json.dumps({"generalDiagnostics": [{
        "file": str(tmp_path / "a.py"),
        "severity": "error",
        "message": "类型不匹配",
        "rule": "reportAssignmentType",
        "range": {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 8}},
    }]}), str(tmp_path))[0]
    assert pyright_raw.rule_id == "reportAssignmentType"
    assert pyright_raw.line_start == 3
    assert pyright_raw.file_path == "a.py"
    assert pyright_raw.severity == "major"

    lizard = parse_lizard_csv(
        "40,16,100,2,40,big@1-40@a.py\n3,1,10,0,3,small@1-3@a.py\n",
        str(tmp_path),
        15,
    )
    assert len(lizard) == 1
    assert lizard[0]["rule_id"] == "structure.cyclomatic"
    assert lizard[0]["symbol"] == "big"
    assert lizard[0]["line_start"] == 1
    assert lizard[0]["line_end"] == 40

    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  structure:\n    cyclomatic: 7\n", encoding="utf-8",
    )
    assert thresholds(str(tmp_path))[2] == 7


async def test_missing_type_backend_does_not_open_scan_order(git_repo, monkeypatch):
    monkeypatch.setattr("code_hospital.catalog.typecheck.type_backend", lambda: "")
    adapters.clear()
    adapters.register(_QuietRuff())
    reg = await reception.register(
        title="类型", scope_type="staged", scope={},
        requested_instruments=["type-ruler"], project_root=git_repo,
    )
    scanned = await scanner.scan(case_id=reg["case_id"], stage_confirmed=True, project_root=git_repo)
    assert any(
        device["tool"] == "type-ruler" and device["outcome"] == "terminal"
        for device in scanned["devices"]
    )
    opened = await diagnostician.diagnose(
        case_id=reg["case_id"], stage_confirmed=True, project_root=git_repo,
    )
    assert "diagnoses" in opened
    assert opened.get("scan_orders", []) == []


@pytest.mark.skipif(not type_backend(), reason="需要 mypy 或 pyright")
async def test_type_ruler_request_opens_order_when_backend_exists(git_repo):
    adapters.clear()
    adapters.register(_QuietRuff())
    reg = await reception.register(
        title="类型", scope_type="staged", scope={},
        requested_instruments=["type-ruler"], project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], stage_confirmed=True, project_root=git_repo)
    opened = await diagnostician.diagnose(
        case_id=reg["case_id"], stage_confirmed=True, project_root=git_repo,
    )
    assert opened["scan_orders"] == [
        {"instrument_id": "type-ruler", "reason": "requested:type-ruler"},
    ]
    supplemented = await scanner.scan(
        case_id=reg["case_id"], stage_confirmed=True, project_root=git_repo,
    )
    assert any(
        device["tool"] == "type-ruler" and device["outcome"] == "ok"
        for device in supplemented["devices"]
    )


def test_js_autofix_uses_eslint():
    from code_hospital.departments.pharmacist import _autofix_engine

    assert _autofix_engine({"scope_paths": ["src/a.js"], "target_file": "src/a.js"}) == "eslint"
    assert _autofix_engine({"scope_paths": ["src/a.py"], "target_file": "src/a.py"}) == "ruff"
