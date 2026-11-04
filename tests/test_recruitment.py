"""招聘闭环（第 5 步）：攒样本 → 判格 → 签字 → 进报告。

验收标准是「一台 probation 仪器攒够样本后转正，报告的覆盖率相应变化，且可回溯」。
这里把它拆成四件事：
1. 样本够了，判据说够格（判的是"够不够格"，不是"好不好用"）；
2. 没签字转不了正——判据不代签；
3. 签了字，下一轮它的结论真的进了 findings 和报告；
4. 为什么转的正，账上查得到。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.catalog import market, probation
from code_hospital.departments import reception, scanner
from code_hospital.storage.db import Database

pytestmark = [pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")]

MANIFEST = """
api: code-hospital/v1
kind: instrument
id: acme-pylint
name: Acme Pylint 桥
vendor: acme
version: "0.3.1"
concern: Python 变量使用与可维护性
extensions: [py]
triage: [outpatient, checkup]
specialty_id: python
transport: mcp
command: git
tool: lint
risk:
  reads_repo: true
  writes_repo: false
"""


@pytest.fixture()
def repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    x = 1\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


def _install(root: str) -> None:
    path = Path(root) / "acme.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    assert market.install(root, path)["ok"]


class _Incumbent(ScannerAdapter):
    """在岗仪器：在 src/a.py 第 2 行报一条 F841。"""

    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="F841", message="局部变量未使用", file_path="src/a.py",
            line_start=2, line_end=2, severity="major",
        )]


def _shadow(monkeypatch, *, file="src/a.py", line=2, rule="ACME001", raws=True):
    """第三方仪器的应答。默认和现任指着同一处，但规则号完全不同。"""

    async def fake(_root, _inst, _files, _timeout):
        rows = []
        if raws:
            rows = [{
                "rule_id": rule, "message": "变量赋值后没用",
                "file_path": file, "line_start": line, "line_end": line,
                "severity": "major",
            }]
        return {
            "record": {"id": "acme-pylint", "status": "ran" if rows else "quiet",
                       "findings": len(rows), "note": f"{len(rows)} 条"},
            "raws": rows,
            "limitations": [],
        }

    monkeypatch.setattr("code_hospital.catalog.mcp_client.run_mcp", fake)


async def _one_scan(root: str, title: str):
    adapters.clear()
    adapters.register(_Incumbent())
    reg = await reception.register(
        title=title, scope_type="staged", scope={}, auto_advance=False,
        force=True, project_root=root,
    )
    out = await scanner.scan(case_id=reg["case_id"], stage_confirmed=True, project_root=root)
    adapters.clear()
    return reg["case_id"], out


def _sha(root: str) -> dict:
    path = Path(root) / market.MARKET_DIR / "index.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_promotion_round_trip(repo, monkeypatch):
    """攒样本 → 判格 → 拒未签字 → 签字 → 结论进报告。"""
    _install(repo)
    _shadow(monkeypatch)
    monkeypatch.setattr(probation, "MIN_RUNS", 2)
    monkeypatch.setattr(probation, "MIN_CHECKABLE", 1)

    await _one_scan(repo, "第一轮")
    await _one_scan(repo, "第二轮")

    row = market.review(repo)["instruments"][0]
    assert row["state"] == "ready", row["line"]
    assert row["runs"] == 2
    # 跨厂商规则号不同，能对上是因为按位置比而不是按 rule_id 比
    assert row["corroborated"] == 2
    assert row["blocked"] == ""

    unsigned = market.promote(repo, "acme-pylint")
    assert not unsigned["ok"] and unsigned["needs_confirm"]
    assert market.installed(repo)["acme-pylint"]["tier"] == "probation", "判据不能自己签字"

    signed = market.promote(repo, "acme-pylint", confirm=True)
    assert signed["ok"] and signed["tier"] == "active"

    index = _sha(repo)["acme-pylint"]
    assert index["promotion"]["corroborated"] == 2
    assert [item["action"] for item in index["history"]] == ["install", "promote"]

    case_id, out = await _one_scan(repo, "转正后")
    async with Database(repo) as db:
        findings = await db.all_findings(case_id)
    assert [item for item in findings if item.tool == "acme-pylint"], "转正后结论要进 findings"
    md = Path(out["report_path"]).read_text(encoding="utf-8")
    assert "ACME001" in md
    assert "市场仪器（影子期）" not in md, "转正了就不是影子了"


@pytest.mark.asyncio
async def test_quiet_instrument_cannot_be_promoted(repo, monkeypatch):
    """跑了没交出东西，判不出好坏。不够格就是不够格，不给签字的口子。"""
    _install(repo)
    _shadow(monkeypatch, raws=False)
    monkeypatch.setattr(probation, "MIN_RUNS", 2)

    await _one_scan(repo, "第一轮")
    await _one_scan(repo, "第二轮")

    got = market.promote(repo, "acme-pylint", confirm=True)
    assert not got["ok"]
    assert "不够格" in got["note"]
    assert market.installed(repo)["acme-pylint"]["tier"] == "probation"


@pytest.mark.asyncio
async def test_instrument_pointing_at_nothing_is_refused(repo, monkeypatch):
    """行号越界是最硬的误报：不需要金标就能判。"""
    _install(repo)
    _shadow(monkeypatch, line=900)
    monkeypatch.setattr(probation, "MIN_RUNS", 2)
    monkeypatch.setattr(probation, "MIN_CHECKABLE", 1)

    await _one_scan(repo, "第一轮")
    await _one_scan(repo, "第二轮")

    row = market.review(repo)["instruments"][0]
    assert row["state"] == "noisy"
    assert not market.promote(repo, "acme-pylint", confirm=True)["ok"]


def test_retired_instrument_cannot_be_promoted_back(repo):
    """退役是下架，不是暂停。要重新转正就得重装、重新攒样本。"""
    _install(repo)
    market.retire(repo, "acme-pylint")

    got = market.promote(repo, "acme-pylint", confirm=True)
    assert not got["ok"]
    assert "退役" in got["note"]


@pytest.mark.asyncio
async def test_job_posting_appears_in_the_scan_report(repo, monkeypatch):
    """没有仪器覆盖的扩展名要变成招聘启事，而不是沉默地记为"没问题"。"""
    for i in range(3):
        (Path(repo) / "src" / f"M{i}.kt").write_text("class M\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)

    _case, out = await _one_scan(repo, "招聘")
    md = Path(out["report_path"]).read_text(encoding="utf-8")

    assert "招聘启事" in md
    assert ".kt" in md
    assert "记为未查，不记为没有问题" in md


@pytest.mark.asyncio
async def test_one_stray_file_is_not_worth_hiring_for(repo, monkeypatch):
    (Path(repo) / "src" / "OnlyOne.kt").write_text("class X\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)

    _case, out = await _one_scan(repo, "一颗钉子")
    md = Path(out["report_path"]).read_text(encoding="utf-8")

    assert "招聘启事" not in md
    assert ".kt" in md, "缺口仍要写，只是还不值得为它招人"


@pytest.mark.asyncio
async def test_ready_instrument_is_announced_in_the_report(repo, monkeypatch):
    """够格了但没人签字，报告要说出这件事——否则它永远停在影子期都没人知道。"""
    _install(repo)
    _shadow(monkeypatch)
    monkeypatch.setattr(probation, "MIN_RUNS", 2)
    monkeypatch.setattr(probation, "MIN_CHECKABLE", 1)

    await _one_scan(repo, "第一轮")
    _case, out = await _one_scan(repo, "第二轮")
    md = Path(out["report_path"]).read_text(encoding="utf-8")

    assert "招聘进度" in md
    assert "够格转正" in md
    assert "签字前它的结论仍然不进报告" in md
