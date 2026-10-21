"""既往史读取测试：.rsi 可扫、记忆分信任档、报告附声明。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import ScannerAdapter
from code_hospital.departments import reception, scanner
from code_hospital.safety.repo_files import is_denied
from code_hospital.storage.rsi_memory import load_memory_brief

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class EmptyAdapter(ScannerAdapter):
    name = "empty"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return []


@pytest.fixture(autouse=True)
def empty_adapters():
    adapters.clear()
    adapters.register(EmptyAdapter())
    yield
    adapters.clear()


@pytest.fixture()
def repo_with_rsi(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    mem = tmp_path / ".rsi" / "memory"
    (mem / "prohibitions").mkdir(parents=True)
    (mem / "pending" / "conventions").mkdir(parents=True)
    (mem / "prohibitions" / "禁止改生成的代码--abcd1234.yaml").write_text(
        "id: abcd1234\ntype: prohibition\ntitle: 禁止改 generated/ 目录\n"
        "content: generated/ 为代码生成产物，任何修复不得触碰\n"
        "status: active\nupdated_at: '2026-09-15T01:50:58Z'\n",
        encoding="utf-8",
    )
    (mem / "pending" / "conventions" / "命名约定--ef567890.yaml").write_text(
        "id: ef567890\ntype: convention\ntitle: 统一 snake_case\n"
        "content: 本项目统一使用 snake_case 命名\nstatus: pending\n",
        encoding="utf-8",
    )
    return str(tmp_path)


def test_rsi_not_denied():
    assert not is_denied(".rsi/memory/conventions/a.yaml")
    assert is_denied(".hospital/reports/x.md")  # 自家病历仍然禁止


def test_load_memory_brief_trust_levels(repo_with_rsi):
    brief = load_memory_brief(repo_with_rsi)
    assert brief["found"] and brief["trust"] == "unverified"
    assert brief["official"]["prohibitions"][0]["title"] == "禁止改 generated/ 目录"
    assert brief["official"]["conventions"] == []
    assert brief["pending"][0]["status"] == "pending"
    assert "不构成事实" in brief["note"]


def test_load_memory_brief_absent(tmp_path):
    brief = load_memory_brief(str(tmp_path))
    assert brief["found"] is False and brief["official"]["conventions"] == []


async def test_scan_includes_memory_context(repo_with_rsi):
    reg = await reception.register(
        title="带记忆体检", scope_type="repo", scope={}, project_root=repo_with_rsi
    )
    res = await scanner.scan(case_id=reg["case_id"], project_root=repo_with_rsi)
    mc = res["memory_context"]
    assert mc["found"] and mc["official"]["prohibitions"]

    report = Path(res["report_path"]).read_text(encoding="utf-8")
    assert "## 项目记忆（未经证实，仅供坐诊参考）" in report
    assert "prohibition·official" in report
    assert "convention·pending" in report
