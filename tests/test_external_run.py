"""外聘仪器真跑：不经 shell，缺产物不写成没问题。"""

import asyncio
import sys
from types import SimpleNamespace

import json

from code_hospital.catalog.external_run import ABSENT, EMPTY, QUIET, SANDBOX_OFF, run_hired
from code_hospital.catalog.outside import overlay
from code_hospital.departments.scanner import collect_findings


def _case():
    return SimpleNamespace(
        id="case_ext",
        scope_type="paths",
        scope=SimpleNamespace(model_dump=lambda: {"paths": ["src/a.py"]}),
        triage_level="outpatient",
        requested_specialties=[],
        requested_instruments=[],
        visit_kind="checkup",
    )


def _patch_python(monkeypatch):
    def which(name):
        return sys.executable if name == "hired-probe" else None

    monkeypatch.setattr("code_hospital.catalog.external_run.shutil.which", which)


def test_missing_binary_is_absent(tmp_path):
    result = asyncio.run(run_hired(
        str(tmp_path),
        {"id": "team-tool", "binary": "definitely-missing-tool", "args": []},
        ["src/a.py"],
        1000,
    ))
    assert result["record"]["status"] == "absent"
    assert result["record"]["note"] == ABSENT
    assert result["raws"] == []
    assert "没有问题" not in result["record"]["note"]
    assert "检查通过" not in result["record"]["note"]


def test_unsafe_args_do_not_start(tmp_path, monkeypatch):
    called = {"n": 0}

    def which(name):
        called["n"] += 1
        return sys.executable

    monkeypatch.setattr("code_hospital.catalog.external_run.shutil.which", which)
    result = asyncio.run(run_hired(
        str(tmp_path),
        {"id": "team-tool", "binary": "hired-probe", "args": ["a;b"]},
        ["src/a.py"],
        1000,
    ))
    assert called["n"] == 0
    assert result["record"]["status"] == "failed"
    assert result["raws"] == []
    assert "没有问题" not in result["record"]["note"]


def test_json_findings_stay_in_scope(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _patch_python(monkeypatch)
    code = (
        "print('[{\"rule_id\":\"EXT1\",\"message\":\"hired hit\",\"file\":\"src/a.py\",\"line\":1},"
        "{\"rule_id\":\"EXT2\",\"message\":\"outside\",\"file\":\"other.py\",\"line\":1}]')"
    )
    result = asyncio.run(run_hired(
        str(tmp_path),
        {"id": "hired-probe", "binary": "hired-probe", "args": ["-c", code]},
        ["src/a.py"],
        5000,
    ))
    assert result["record"]["status"] == "ran"
    assert result["record"]["findings"] == 1
    assert result["raws"][0]["file_path"] == "src/a.py"
    assert result["record"]["sandbox"] is False


def test_empty_stdout_is_not_a_clean_bill(tmp_path, monkeypatch):
    _patch_python(monkeypatch)
    result = asyncio.run(run_hired(
        str(tmp_path),
        {"id": "hired-probe", "binary": "hired-probe", "args": ["-c", "print('')"]},
        ["src/a.py"],
        5000,
    ))
    assert result["record"]["status"] == "empty"
    assert result["record"]["note"] == EMPTY
    assert "没有问题" not in result["record"]["note"]
    assert result["raws"] == []


def test_secret_env_is_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_HOSPITAL_TEST_SECRET", "super-secret-value")
    _patch_python(monkeypatch)
    code = (
        "print(__import__('pathlib').Path('leak.txt').write_text("
        "__import__('os').environ.get('CODE_HOSPITAL_TEST_SECRET','none')) or '')"
    )
    asyncio.run(run_hired(
        str(tmp_path),
        {"id": "hired-probe", "binary": "hired-probe", "args": ["-c", code]},
        ["src/a.py"],
        5000,
    ))
    assert (tmp_path / "leak.txt").read_text(encoding="utf-8") == "none"


def test_timeout_does_not_claim_clean(tmp_path, monkeypatch):
    _patch_python(monkeypatch)
    result = asyncio.run(run_hired(
        str(tmp_path),
        {"id": "hired-probe", "binary": "hired-probe", "args": ["-c", "__import__('time').sleep(5)"]},
        ["src/a.py"],
        1000,
    ))
    assert result["record"]["status"] == "empty"
    assert result["record"]["note"] == EMPTY
    assert result["raws"] == []


def test_catalog_rejects_shell_args(tmp_path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n"
        "  instruments:\n"
        "    - id: team-bad\n"
        "      engine: external\n"
        "      binary: python\n"
        "      args: ['a;b']\n",
        encoding="utf-8",
    )
    extra = overlay(str(tmp_path))
    assert "team-bad" not in extra["instruments"]
    assert any("禁止字符" in note for note in extra["notes"])


def test_scan_stamps_external_and_sandbox_stays_off(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n"
        "  instruments:\n"
        "    - id: hired-probe\n"
        "      engine: external\n"
        "      binary: hired-probe\n"
        "      sandbox: true\n"
        "      args:\n"
        "        - -c\n"
        "        - 'print(''[{\"rule_id\":\"EXT1\",\"message\":\"hired hit\",\"file\":\"src/a.py\",\"line\":1}]'')'\n",
        encoding="utf-8",
    )
    _patch_python(monkeypatch)
    _devices, limitations, findings, ledger = asyncio.run(collect_findings(
        str(tmp_path), _case(), "rpt_ext", tools=["hired-probe"], timeout_ms=5000,
    ))
    assert len(findings) == 1
    assert findings[0].concluded_by == "external"
    assert findings[0].file_path == "src/a.py"
    hired = ledger["external"]
    assert hired[0]["id"] == "hired-probe"
    assert hired[0]["status"] == "ran"
    assert hired[0]["sandbox"] is False
    assert any(SANDBOX_OFF in item for item in limitations)
    blob = "\n".join(limitations)
    assert "没有问题" not in blob
    assert "检查通过" not in blob


def _print_file(name: str) -> str:
    return f"print(open({name!r}, encoding='utf-8').read())"


def test_sarif_stays_in_scope(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    payload = {
        "runs": [{
            "results": [
                {
                    "ruleId": "java:S1",
                    "level": "warning",
                    "message": {"text": "hired sarif"},
                    "locations": [{
                        "physicalLocation": {
                            "artifactLocation": {"uri": "file:///src/a.py"},
                            "region": {"startLine": 1, "endLine": 1},
                        },
                    }],
                },
                {
                    "ruleId": "java:S2",
                    "message": {"text": "outside"},
                    "locations": [{
                        "physicalLocation": {
                            "artifactLocation": {"uri": "other.py"},
                            "region": {"startLine": 2},
                        },
                    }],
                },
            ],
        }],
    }
    (tmp_path / "out.sarif").write_text(json.dumps(payload), encoding="utf-8")
    _patch_python(monkeypatch)
    result = asyncio.run(run_hired(
        str(tmp_path),
        {"id": "hired-probe", "binary": "hired-probe", "args": ["-c", _print_file("out.sarif")]},
        ["src/a.py"],
        5000,
    ))
    assert result["record"]["status"] == "ran"
    assert result["record"]["findings"] == 1
    assert result["raws"][0]["rule_id"] == "java:S1"
    assert result["raws"][0]["file_path"] == "src/a.py"
    assert result["raws"][0]["severity"] == "minor"
    assert "没有问题" not in result["record"]["note"]


def test_text_lines_stay_in_scope(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "out.txt").write_text(
        "src/a.py:1: hired line\nother.py:2: outside\n",
        encoding="utf-8",
    )
    _patch_python(monkeypatch)
    result = asyncio.run(run_hired(
        str(tmp_path),
        {"id": "hired-probe", "binary": "hired-probe", "args": ["-c", _print_file("out.txt")]},
        ["src/a.py"],
        5000,
    ))
    assert result["record"]["status"] == "ran"
    assert result["raws"][0]["message"] == "hired line"
    assert result["raws"][0]["file_path"] == "src/a.py"
    assert result["record"]["findings"] == 1


def test_empty_findings_are_quiet_not_missing(tmp_path, monkeypatch):
    (tmp_path / "out.json").write_text('{"findings":[]}', encoding="utf-8")
    _patch_python(monkeypatch)
    result = asyncio.run(run_hired(
        str(tmp_path),
        {"id": "hired-probe", "binary": "hired-probe", "args": ["-c", _print_file("out.json")]},
        ["src/a.py"],
        5000,
    ))
    assert result["record"]["status"] == "quiet"
    assert result["record"]["note"] == QUIET
    assert result["raws"] == []
    assert "没有问题" not in result["record"]["note"]
    assert "检查通过" not in result["record"]["note"]
    assert EMPTY not in result["record"]["note"]


def test_unparsed_text_is_not_a_clean_bill(tmp_path, monkeypatch):
    (tmp_path / "out.txt").write_text("this is not an artifact\n", encoding="utf-8")
    _patch_python(monkeypatch)
    result = asyncio.run(run_hired(
        str(tmp_path),
        {"id": "hired-probe", "binary": "hired-probe", "args": ["-c", _print_file("out.txt")]},
        ["src/a.py"],
        5000,
    ))
    assert result["record"]["status"] == "empty"
    assert result["record"]["note"] == EMPTY


def test_scan_files_sarif_as_external(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    payload = {
        "runs": [{
            "results": [{
                "ruleId": "java:S1",
                "message": {"text": "hired sarif"},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": "src/a.py"},
                        "region": {"startLine": 1},
                    },
                }],
            }],
        }],
    }
    (tmp_path / "out.sarif").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n"
        "  instruments:\n"
        "    - id: hired-probe\n"
        "      engine: external\n"
        "      binary: hired-probe\n"
        "      args: ['-c', 'print(open(\"out.sarif\", encoding=\"utf-8\").read())']\n",
        encoding="utf-8",
    )
    _patch_python(monkeypatch)
    _devices, limitations, findings, ledger = asyncio.run(collect_findings(
        str(tmp_path), _case(), "rpt_sarif", tools=["hired-probe"], timeout_ms=5000,
    ))
    assert len(findings) == 1
    assert findings[0].concluded_by == "external"
    assert findings[0].message == "hired sarif"
    assert findings[0].file_path == "src/a.py"
    assert ledger["external"][0]["status"] == "ran"
    assert "没有问题" not in "\n".join(limitations)
