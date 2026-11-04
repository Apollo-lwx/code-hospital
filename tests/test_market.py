"""能力市场：装、验、跑、退，以及最重要的一条——装进来先别信。

验收标准是「装一个第三方仪器，能跑出结果并进覆盖账，但不进报告」。
这里把这句话拆成可断言的三件事：影子账有数、findings 表没有、报告里搜不到。
第四件事是转正后反过来：进了 findings 就必须在报告里看得见，否则转正等于白转。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.catalog import market
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
command: {command}
tool: lint
risk:
  reads_repo: true
  writes_repo: {writes}
"""


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    x = 1\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


def _write(root: str, *, command: str = "git", writes: str = "false", name: str = "acme.yaml") -> Path:
    path = Path(root) / name
    path.write_text(MANIFEST.format(command=command, writes=writes), encoding="utf-8")
    return path


def _set_tier(root: str, tier: str) -> None:
    index = Path(root) / market.MARKET_DIR / "index.json"
    data = json.loads(index.read_text(encoding="utf-8"))
    for key in data:
        data[key]["tier"] = tier
    index.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_install_lands_under_hospital_dir(git_repo):
    got = market.install(git_repo, _write(git_repo))
    assert got["ok"] and got["tier"] == "probation"
    base = Path(git_repo) / market.MARKET_DIR
    assert (base / "manifests" / "acme-pylint.yaml").is_file()
    index = json.loads((base / "index.json").read_text(encoding="utf-8"))
    assert index["acme-pylint"]["tier"] == "probation"


def test_install_keeps_the_original_file_verbatim(git_repo):
    """客户要能拿回去继续改，所以存原文而不是序列化结果。"""
    source = _write(git_repo)
    market.install(git_repo, source)
    stored = Path(git_repo) / market.MARKET_DIR / "manifests" / "acme-pylint.yaml"
    assert stored.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_bad_manifest_is_not_written(git_repo):
    bad = Path(git_repo) / "bad.yaml"
    bad.write_text("api: nope\nid: acme\n", encoding="utf-8")
    got = market.install(git_repo, bad)
    assert not got["ok"]
    assert not (Path(git_repo) / market.MARKET_DIR / "manifests" / "acme.yaml").exists()


def test_reusing_a_core_id_is_refused_at_install(git_repo):
    clash = Path(git_repo) / "clash.yaml"
    clash.write_text(MANIFEST.format(command="git", writes="false").replace("acme-pylint", "ruff"), encoding="utf-8")
    got = market.install(git_repo, clash)
    assert not got["ok"]
    assert any("已被占用" in item for item in got["problems"])


def test_verify_separates_installed_from_runnable(git_repo):
    """「装在」和「跑得动」是两件事，报告里不能混成一件。"""
    market.install(git_repo, _write(git_repo, command="git"))
    got = market.verify(git_repo)
    rows = {row["id"]: row for row in got["instruments"]}
    assert rows["acme-pylint"]["present"] is True
    assert rows["acme-pylint"]["manifest_ok"] is True


def test_verify_reports_a_command_that_is_not_on_path(git_repo):
    gen = Path(git_repo) / "ghost.yaml"
    gen.write_text(
        MANIFEST.format(command="acme-not-installed", writes="false").replace("acme-pylint", "acme-ghost"),
        encoding="utf-8",
    )
    market.install(git_repo, gen)
    rows = {row["id"]: row for row in market.verify(git_repo)["instruments"]}
    assert rows["acme-ghost"]["present"] is False
    assert "PATH" in rows["acme-ghost"]["absent"]


def test_retire_stops_it_without_deleting_anything(git_repo):
    market.install(git_repo, _write(git_repo))
    got = market.retire(git_repo, "acme-pylint")
    assert got["ok"]
    assert (Path(git_repo) / market.MARKET_DIR / "manifests" / "acme-pylint.yaml").is_file()
    assert market.live(git_repo) == {}
    assert market.installed(git_repo)["acme-pylint"]["tier"] == "retired"


def test_retiring_something_never_installed_is_refused(git_repo):
    got = market.retire(git_repo, "never-there")
    assert not got["ok"]


def test_stage_keeps_out_what_must_not_run(git_repo):
    """声明会写仓库的，影子期不跑：比的必须是同一份源码。"""
    market.install(git_repo, _write(git_repo, writes="true"))
    rows, notes = market.stage(git_repo)
    assert rows == {}
    assert any("写仓库" in item for item in notes)


def test_stage_ignores_substrate(git_repo):
    sub = Path(git_repo) / "mem.yaml"
    sub.write_text(
        "api: code-hospital/v1\nkind: substrate\nid: acme-memory\nconcern: 项目记忆基座\n",
        encoding="utf-8",
    )
    assert market.install(git_repo, sub)["ok"]
    rows, _notes = market.stage(git_repo)
    assert "acme-memory" not in rows


def test_a_manifest_edited_after_install_stops_counting(git_repo):
    """装的时候是好的、后来被改坏：不能显示成一台正常的 probation 然后一次也跑不起来。"""
    market.install(git_repo, _write(git_repo))
    stored = Path(git_repo) / market.MARKET_DIR / "manifests" / "acme-pylint.yaml"
    stored.write_text("api: wrong\nid: acme-pylint\n", encoding="utf-8")
    doc = market.installed(git_repo)["acme-pylint"]
    assert doc["tier"] == "retired"
    assert doc["problems"]
    assert market.live(git_repo) == {}
    rows, notes = market.stage(git_repo)
    assert rows == {} and any("校验不过" in item for item in notes)


class _Scripted(ScannerAdapter):
    """替身设备：不依赖本机是否装了 ruff，只保证有一批正式发现。"""

    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    def __init__(self) -> None:
        self.rounds = [[RawFinding(
            rule_id="F841", message="局部变量未使用", file_path="src/a.py",
            line_start=2, line_end=2, severity="major",
        )]]

    async def scan(self, repo_root, files, timeout_ms):
        return self.rounds.pop() if self.rounds else []


def _shadow_answer(monkeypatch):
    async def fake(_root, _inst, _files, _timeout):
        return {
            "record": {"id": "acme-pylint", "status": "ran", "findings": 1, "note": "1 条"},
            "raws": [{
                "rule_id": "ACME001", "message": "变量赋值后没用",
                "file_path": "src/a.py", "line_start": 2, "line_end": 2, "severity": "major",
            }],
            "limitations": [],
        }

    monkeypatch.setattr("code_hospital.catalog.mcp_client.run_mcp", fake)


async def _scan(git_repo: str, title: str):
    adapters.clear()
    adapters.register(_Scripted())
    reg = await reception.register(
        title=title, scope_type="staged", scope={}, auto_advance=False,
        force=True, project_root=git_repo,
    )
    case_id = reg["case_id"]
    out = await scanner.scan(case_id=case_id, stage_confirmed=True, project_root=git_repo)
    async with Database(git_repo) as db:
        case = await db.get_case(case_id)
        findings = await db.all_findings(case_id)
    md = Path(out["report_path"]).read_text(encoding="utf-8")
    adapters.clear()
    return case, findings, md


async def test_probation_runs_and_stays_out_of_the_report(git_repo, monkeypatch):
    """第 4 步的验收：跑出结果、进覆盖账、不进报告。"""
    market.install(git_repo, _write(git_repo))
    _shadow_answer(monkeypatch)

    case, findings, md = await _scan(git_repo, "影子期")

    observed = (case.coverage or {}).get("shadow", {}).get("observed") or []
    assert [row["instrument"] for row in observed] == ["acme-pylint"]
    assert observed[0]["ran"] == "ran" and observed[0]["findings"] == 1
    # 影子发现不在 findings 表里——不是被过滤，是不存在
    assert not [item for item in findings if item.tool == "acme-pylint"]
    # 报告里搜不到那条结论，但能看见「装了、跑了、几条」
    assert "ACME001" not in md
    assert "市场仪器（影子期）" in md
    assert "不进结论、不进处方、不进疗效" in md


async def test_promoted_instrument_does_reach_the_report(git_repo, monkeypatch):
    """转正是有意义的：同一台仪器换成 active，结论就进正文了。"""
    market.install(git_repo, _write(git_repo))
    _set_tier(git_repo, "active")
    _shadow_answer(monkeypatch)

    case, findings, md = await _scan(git_repo, "转正后")

    assert [item for item in findings if item.tool == "acme-pylint"], "转正后应进 findings"
    assert "ACME001" in md
    assert not (case.coverage or {}).get("shadow", {}).get("observed")


async def test_retired_instrument_does_not_run(git_repo, monkeypatch):
    market.install(git_repo, _write(git_repo))
    market.retire(git_repo, "acme-pylint")
    _shadow_answer(monkeypatch)

    case, findings, md = await _scan(git_repo, "退役")
    assert not (case.coverage or {}).get("shadow", {}).get("observed")
    assert not [item for item in findings if item.tool == "acme-pylint"]
    assert "市场仪器（影子期）" not in md


async def test_broken_instrument_does_not_break_the_checkup(git_repo, monkeypatch):
    """第三方仪器挂掉是常态，不该带崩这次检验，但要留一条账。"""
    market.install(git_repo, _write(git_repo))

    async def boom(*_args, **_kwargs):
        raise RuntimeError("仪器自己炸了")

    monkeypatch.setattr("code_hospital.catalog.mcp_client.run_mcp", boom)
    case, _findings, md = await _scan(git_repo, "仪器炸了")
    observed = (case.coverage or {}).get("shadow", {}).get("observed") or []
    assert observed[0]["ran"] == "failed"
    assert "没跑成" in md


async def test_supplement_does_not_pay_for_the_shadow_again(git_repo, monkeypatch):
    """补片只为查一两味指定设备，不该顺手再拉一遍第三方服务。"""
    market.install(git_repo, _write(git_repo))
    _shadow_answer(monkeypatch)
    adapters.clear()
    adapters.register(_Scripted())
    reg = await reception.register(
        title="补片", scope_type="staged", scope={}, auto_advance=False,
        force=True, project_root=git_repo,
    )
    async with Database(git_repo) as db:
        case = await db.get_case(reg["case_id"])
    _d, _l, _f, ledger = await scanner.collect_findings(
        git_repo, case, "rpt-x", None, 60_000, None, shadow=False,
    )
    adapters.clear()
    assert "shadow" not in ledger
