"""记忆闭环的端到端：召回记账 → 出院反馈 → 权重落表。

不连真 rsi（握手 3.5 秒且依赖本机安装），用假端口验医院这一侧的四件事：
引用账写进去了、反馈回报了、半票加上了、报告里有那一节。
rsi 侧的行为（pending 不进召回、official 才生效）不在医院能改的范围。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.departments import (
    diagnostician,
    discharge,
    pharmacist,
    prescriber,
    reception,
    scanner,
)
from code_hospital.departments.pharmacist import _ruff_binary
from code_hospital.integrations import memory_port as mp
from code_hospital.storage.db import Database

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="需要 git"),
    pytest.mark.skipif(_ruff_binary() is None, reason="需要 ruff"),
]

MEMORY_ID = "kn-f841"


class Ruler(ScannerAdapter):
    binary = "git"
    extensions = frozenset({".py"})
    name = "ruff"

    def __init__(self):
        self.rounds = [[
            RawFinding(
                rule_id="F841", message="unused", file_path="src/a.py",
                line_start=2, line_end=2, severity="major",
            ),
        ], []]

    async def scan(self, repo_root, files, timeout_ms):
        return self.rounds[0] if len(self.rounds) == 1 else self.rounds.pop(0)


class FakePort:
    """假的 MCP 端口：记得自己回报了什么，方便断言。"""

    channel = "mcp"
    owner = "hospital"

    def __init__(self):
        self.reported: list[tuple[str, str]] = []
        self.tasks: list[str] = []

    async def recall(self, task, *, role="", top_k=None):
        self.tasks.append(task)
        return {
            "channel": self.channel, "task": task, "token": "tok-1",
            "hits": [{
                "id": MEMORY_ID, "kind": "item", "tier": "reference",
                "title": "F841 不要直接删变量",
                "content": "先确认是不是漏了赋值再删。",
                "content_type": "convention", "source_path": "", "health": {},
                "low_confidence": False, "condition": {},
            }],
            "recalled": [MEMORY_ID], "injected": 1, "withheld": 0,
            "decisions": [], "skills": [], "note": "",
        }

    async def feedback(self, token, action, *, rating=None, comment=""):
        self.reported.append((token, action))
        return {"ok": True, "channel": "mcp", "action": action, "note": ""}


class CliPort(FakePort):
    """只有 CLI 的情况：能召回，但反馈断流。"""

    channel = "cli"

    async def feedback(self, token, action, *, rating=None, comment=""):
        return {"ok": False, "channel": "cli", "action": "", "note": "cli 通道没有 feedback 接口"}


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def f():\n    x = 1\n    return 2\n", encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


def _use(port) -> None:
    adapters.clear()
    adapters.register(Ruler())
    mp._PROBE.clear()


async def _visit(root: str, port) -> tuple[str, dict]:
    adapters.clear()
    adapters.register(Ruler())
    reg = await reception.register(
        title="记忆闭环", scope_type="staged", scope={},
        auto_advance=False, project_root=root,
    )
    case_id = reg["case_id"]
    await scanner.scan(case_id=case_id, stage_confirmed=True, project_root=root)
    await diagnostician.diagnose(case_id=case_id, stage_confirmed=True, project_root=root)
    draft = await prescriber.prescribe(
        case_id=case_id, stage_confirmed=True, acceptance_decision="skip", project_root=root,
    )
    if draft.get("status") == "pending_question":
        draft = await prescriber.prescribe(
            case_id=case_id, stage_confirmed=True, low_confidence="exclude",
            acceptance_decision="skip", project_root=root,
        )
    approved = await prescriber.prescribe(
        case_id=case_id, approve=True, approver="host_agent", project_root=root,
    )
    await pharmacist.dispense(prescription_id=approved["prescription_id"], project_root=root)
    out = await discharge.discharge(case_id=case_id, project_root=root)
    return case_id, draft, out


async def test_recall_is_booked_and_feedback_reported(git_repo, monkeypatch):
    port = FakePort()

    async def fake(_root, **_kw):
        return port

    monkeypatch.setattr(mp, "open_port", fake)
    _use(port)
    case_id, draft, out = await _visit(git_repo, port)

    # 召回描述带上了本次问题类型（规则 id 与病种名都要有，否则捞不到）
    assert "F841" in port.tasks[0]
    assert draft["memory"]["references"][0]["title"] == "F841 不要直接删变量"

    # 出院判有效 → 回报 accepted
    assert port.reported == [("tok-1", "accepted")]

    async with Database(git_repo) as db:
        case = await db.get_case(case_id)
        book = case.coverage["memory"]
        assert book["channel"] == "mcp"
        assert book["recalled"] == [MEMORY_ID]
        assert book["feedback"] == "reported"
        # 出院判有效只给半票
        weights = await db.memory_weights([MEMORY_ID], git_repo)
        assert weights[MEMORY_ID]["alpha"] == pytest.approx(1.5)
        assert weights[MEMORY_ID]["effective"] == 1

    summary = Path(out["summary_path"]).read_text(encoding="utf-8")
    assert "## 记忆与经验" in summary
    assert "引用 1 条" in summary
    assert "本轮已回报反馈" in summary


async def test_cli_only_backend_admits_it_did_not_report(git_repo, monkeypatch):
    """CLI 没有反馈接口——报告要写出来，不能让断掉的闭环看起来是通的。"""
    port = CliPort()

    async def fake(_root, **_kw):
        return port

    monkeypatch.setattr(mp, "open_port", fake)
    _use(port)
    case_id, _draft, out = await _visit(git_repo, port)

    async with Database(git_repo) as db:
        case = await db.get_case(case_id)
        assert case.coverage["memory"]["feedback"] == "cli"
        # 反馈断流不影响权重：反馈给 rsi 学召回策略，权重是医院自己的账。
        # 这次确实治好了，半票照给。
        weights = await db.memory_weights([MEMORY_ID], git_repo)
        assert weights[MEMORY_ID]["alpha"] == pytest.approx(1.5)

    summary = Path(out["summary_path"]).read_text(encoding="utf-8")
    assert "未反馈" in summary


async def test_memory_failure_does_not_block_the_visit(git_repo, monkeypatch):
    """记忆连不上只能降级，不能拦就诊。"""

    async def dead(_root, **_kw):
        raise RuntimeError("rsi 挂了")

    monkeypatch.setattr(mp, "open_port", dead)
    adapters.clear()
    adapters.register(Ruler())
    _use(None)
    case_id, draft, out = await _visit(git_repo, None)

    assert draft["memory"]["channel"] == "absent"
    assert out["status"] == "discharged"
    async with Database(git_repo) as db:
        case = await db.get_case(case_id)
        assert case.coverage["memory"]["channel"] == "absent"
        assert case.coverage["memory"]["feedback"] == "absent"
    # 没有记忆就不写那一节，不占版面
    assert "## 记忆与经验" not in Path(out["summary_path"]).read_text(encoding="utf-8")
