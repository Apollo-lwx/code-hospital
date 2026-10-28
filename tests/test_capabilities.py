"""宿主没声明 elicitation 时，出参和覆盖账都写明门禁降级为约定级。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.api.capabilities import (
    GATE_LINE,
    bind_host,
    reset_host,
    supports_elicitation,
)
from code_hospital.departments import reception, scanner
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class _Caps:
    def __init__(self, elicitation):
        self.elicitation = elicitation


class _Host:
    def __init__(self, elicitation, version="2025-06-18"):
        self.client_capabilities = None if elicitation is _MISSING else _Caps(elicitation)
        self.protocol_version = version


class _DictHost:
    client_capabilities = {"elicitation": {"form": {}}}
    protocol_version = "2026-07-28"


class _Broken:
    protocol_version = None

    @property
    def client_capabilities(self):
        raise ValueError("no request")


_MISSING = object()


class Quiet(ScannerAdapter):
    name = "quiet"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="Q001", message="quiet",
                file_path="src/a.py", line_start=1, line_end=1, severity="nit",
            )
        ]


def _repo(path: Path) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    return str(path)


@pytest.fixture(autouse=True)
def quiet_adapters():
    adapters.clear()
    adapters.register(Quiet())
    yield
    adapters.clear()


def test_supports_elicitation_reads_capabilities_and_protocol():
    assert supports_elicitation(None) is False
    assert supports_elicitation(_Host(_MISSING)) is False
    assert supports_elicitation(_Host(None)) is False
    assert supports_elicitation(_Broken()) is False
    assert supports_elicitation(_Host(object())) is True
    assert supports_elicitation(_DictHost()) is True
    assert _Host(object()).protocol_version == "2025-06-18"


async def _scan(root: str, host) -> dict:
    reg = await reception.register(
        title="能力探测", scope_type="repo", scope={}, project_root=root, auto_advance=True,
    )
    token = bind_host(host)
    try:
        return await scanner.scan(case_id=reg["case_id"], project_root=root)
    finally:
        reset_host(token)


async def test_missing_elicitation_is_visible_on_the_report(tmp_path: Path):
    root = _repo(tmp_path / "off")
    result = await _scan(root, _Host(None, "2025-03-26"))
    report = Path(result["report_path"]).read_text(encoding="utf-8")
    assert GATE_LINE in report
    assert result["degraded"] is True
    assert result["degraded_reason"] == GATE_LINE
    async with Database(root) as db:
        case = await db.get_case(result["case_id"])
    recorded = case.coverage["host_capabilities"]
    assert recorded["elicitation"] is False
    assert recorded["protocol_version"] == "2025-03-26"
    assert recorded["channel"] == "convention"


async def test_declared_elicitation_omits_the_degraded_line(tmp_path: Path):
    root = _repo(tmp_path / "on")
    result = await _scan(root, _Host(object(), "2026-07-28"))
    report = Path(result["report_path"]).read_text(encoding="utf-8")
    assert GATE_LINE not in report
    assert "degraded" not in result
    async with Database(root) as db:
        case = await db.get_case(result["case_id"])
    recorded = case.coverage["host_capabilities"]
    assert recorded["elicitation"] is True
    assert recorded["protocol_version"] == "2026-07-28"
    assert recorded["channel"] == "convention"
