"""适配器解析层单测（纯函数，不依赖设备真实安装）。"""

import json

from code_hospital.adapters.gitleaks_adapter import parse_gitleaks_json
from code_hospital.adapters.ruff_adapter import map_severity, parse_ruff_json
from code_hospital.adapters.semgrep_adapter import parse_semgrep_json, semgrep_argv


def test_ruff_parse_and_severity():
    payload = json.dumps([
        {"code": "F841", "message": "local variable 'x' is assigned to but never used",
         "filename": "src/a.py", "location": {"row": 3, "column": 4},
         "end_location": {"row": 3, "column": 9}},
        {"code": "E501", "message": "line too long", "filename": "src/b.py",
         "location": {"row": 10, "column": 80}, "end_location": {"row": 10, "column": 99}},
        {"code": None, "message": "syntax error", "filename": "src/c.py",
         "location": {"row": 1, "column": 1}, "end_location": {"row": 1, "column": 2}},
    ])
    findings = parse_ruff_json(payload)
    assert len(findings) == 3
    assert findings[0].severity == "major" and findings[0].rule_id == "F841"
    assert findings[1].severity == "minor"
    assert findings[2].rule_id == "syntax-error"
    assert map_severity("F401") == "major" and map_severity("W605") == "minor"
    assert map_severity("C901") == "nit"
    assert map_severity("FURB122") == "nit"


def test_semgrep_parse():
    payload = json.dumps({
        "results": [{
            "check_id": "python.lang.security.audit.exec-used",
            "path": "src/x.py",
            "start": {"line": 7}, "end": {"line": 7},
            "extra": {"message": "exec used", "severity": "ERROR"},
        }]
    })
    findings = parse_semgrep_json(payload)
    assert findings[0].severity == "major" and findings[0].line_start == 7


def test_gitleaks_parse_masks_secret():
    payload = json.dumps([{
        "RuleID": "aws-access-key", "Description": "AWS Access Key",
        "File": "deploy/keys.txt", "StartLine": 2, "EndLine": 2,
        "Secret": "AKIAZZZZZZZZZZZZZZZZ",
    }])
    findings = parse_gitleaks_json(payload)
    assert findings[0].severity == "critical"
    assert "AKIAZZZZZZZZZZZZZZZZ" not in findings[0].evidence


def test_semgrep_registry_stays_metrics_off():
    argv = semgrep_argv("semgrep", "auto", ["src/a.py"])
    assert "--metrics=off" in argv
    assert "auto" not in argv
    assert "p/python" in argv
    assert "p/security-audit" in argv
    local = semgrep_argv("semgrep", ".semgrep.yml", ["src/a.py"])
    assert local[local.index("--config") + 1] == ".semgrep.yml"
    assert "p/python" not in local
