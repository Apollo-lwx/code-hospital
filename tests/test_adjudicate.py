"""安全科裁决和覆盖账。"""

from pathlib import Path

from code_hospital.catalog.adjudicate import adjudicate_findings
from code_hospital.catalog.coverage import build_ledger
from code_hospital.core.models import Finding


def _finding(tool: str, rule_id: str, file_path: str, line: int, severity: str = "critical") -> Finding:
    return Finding(
        id="fnd_test", case_id="case_test", report_id="rpt_test",
        tool=tool, rule_id=rule_id, severity=severity,
        file_path=file_path, line_start=line, line_end=line,
        message="仪器原文", evidence="仪器原文", fingerprint="sha256:abc",
    )


def test_constant_eval_is_rejected(tmp_path: Path):
    (tmp_path / "app.py").write_text('def f():\n    return eval("1")\n', encoding="utf-8")
    finding = _finding("security-ruler", "security.exec", "app.py", 2)
    adjudicate_findings(str(tmp_path), [finding])
    assert finding.verdict == "rejected"
    assert "字面量" in finding.message


def test_untraced_eval_stays_unprescribed(tmp_path: Path):
    (tmp_path / "app.py").write_text("def f(value):\n    return eval(value)\n", encoding="utf-8")
    finding = _finding("security-ruler", "security.exec", "app.py", 2)
    adjudicate_findings(str(tmp_path), [finding])
    assert finding.verdict == "needs_validation"
    assert finding.severity == "minor"
    assert finding.unresolved
    assert finding.attack_class == "injection"


def test_placeholder_secret_is_rejected(tmp_path: Path):
    (tmp_path / "app.py").write_text('token = "changeme"\n', encoding="utf-8")
    finding = _finding("gitleaks", "generic-api-key", "app.py", 1)
    adjudicate_findings(str(tmp_path), [finding])
    assert finding.verdict == "rejected"


def test_pem_header_example_is_rejected(tmp_path: Path):
    (tmp_path / "certificate.rs").write_text(
        'let key = SecretString::new("-----BEGIN PRIVATE KEY-----\\n...");\n',
        encoding="utf-8",
    )
    finding = _finding("gitleaks", "private-key", "certificate.rs", 1)
    adjudicate_findings(str(tmp_path), [finding])
    assert finding.verdict == "rejected"
    assert finding.severity == "info"


def test_intra_function_taint_is_confirmed_without_execution(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "def lookup(request):\n"
        "    name = request.args.get('name')\n"
        "    query = f\"select {name}\"\n"
        "    cur.execute(query)\n",
        encoding="utf-8",
    )
    finding = _finding("taint-ruler", "severe.taint-sql", "app.py", 4, "major")
    adjudicate_findings(str(tmp_path), [finding])
    assert finding.verdict == "confirmed"
    assert finding.severity == "major"
    assert "没有执行" in finding.message


def test_path_prefix_stays_major_when_the_separator_is_missing(tmp_path: Path):
    (tmp_path / "guard.js").write_text(
        "if (!repoDir.startsWith(path.resolve(repoBase))) return\n",
        encoding="utf-8",
    )
    finding = _finding("security-ruler", "security.path-prefix", "guard.js", 1, "major")
    adjudicate_findings(str(tmp_path), [finding])
    assert finding.verdict == "confirmed"
    assert finding.severity == "major"


def test_semgrep_pickle_without_untrusted_input_is_minor(tmp_path: Path):
    (tmp_path / "app.py").write_text("def load(path):\n    return torch.load(path)\n", encoding="utf-8")
    finding = _finding(
        "semgrep", "trailofbits.python.pickles-in-pytorch.pickles-in-pytorch", "app.py", 2, "major",
    )
    finding.message = "Functions reliant on pickle can result in arbitrary code execution."
    adjudicate_findings(str(tmp_path), [finding])
    assert finding.verdict == "needs_validation"
    assert finding.severity == "minor"


def test_security_ledger_leaves_auth_and_sandbox_uncovered():
    devices = [
        {"tool": "security-ruler", "available": True, "outcome": "ok"},
        {"tool": "gitleaks", "available": False, "outcome": "terminal"},
        {"tool": "semgrep", "available": False, "outcome": "terminal"},
    ]
    lines, ledger = build_ledger("security", devices, ".", ["app.py"])
    by_id = {unit["id"]: unit for unit in ledger["units"]}
    assert by_id["injection"]["status"] == "checked"
    assert by_id["authn"]["status"] == "uncovered"
    assert by_id["tenant"]["status"] == "uncovered"
    assert by_id["sandbox"]["status"] == "uncovered"
    assert by_id["secret-in-source"]["status"] == "partial"
    assert any("覆盖账" in line for line in lines)
    assert any("不能写成没有问题" in line for line in lines)


def test_unchanged_source_carries_the_previous_ledger(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    devices = [{"tool": "security-ruler", "available": True, "outcome": "ok"}]
    _lines, first = build_ledger("security", devices, str(tmp_path), ["app.py"])
    lines, second = build_ledger("security", devices, str(tmp_path), ["app.py"], first)
    assert second["source_hash"] == first["source_hash"]
    assert any("源码未变" in line for line in lines)
