"""git worktree 高危隔离（worktree 草案一期）：配置矩阵、建间同步、零接触、
合回（成功/漂移/复验）、降级路径。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital.catalog.policy import load_policy
from code_hospital.core.errors import HospitalError
from code_hospital.core.ids import new_id
from code_hospital.departments import pharmacist, reception
from code_hospital.safety import worktree as wt
from code_hospital.storage.db import Database, hospital_root, now_ms

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")

_ORIGINAL = "def f():\n    x = 1\n    return 2\n"
_PATCH = "\n".join([
    "--- a/src/a.py",
    "+++ b/src/a.py",
    "@@ -1,3 +1,3 @@",
    " def f():",
    "-    x = 1",
    "+    x = 2",
    "     return 2",
])


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(_ORIGINAL, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return str(tmp_path)


def _write_config(root: str, text: str) -> None:
    (Path(root) / "code-hospital.yaml").write_text(text, encoding="utf-8")


def _no_worktrees(root: str) -> bool:
    """拆间干净 = 隔离间父目录下没有任何子目录（父目录本身是 prepare 时建的，可留）。"""
    parent = hospital_root(root) / "worktrees"
    return not parent.exists() or not any(parent.iterdir())


async def _high_risk_case(root: str, acceptance_alias: str = "") -> tuple[str, str]:
    """内置方剂的高危全是 manual——直接造一条高危煎药处方进 dispensing。"""
    reg = await reception.register(
        title="隔离", scope_type="staged", scope={},
        auto_advance=True, project_root=root,
    )
    case_id = reg["case_id"]
    rx_id = new_id("rx")
    item = {
        "id": new_id("rxit"), "seq": 1, "diagnosis_id": "",
        "target_file": "src/a.py", "target_symbol": "",
        "method": "llm_patch", "instructions": "把 x = 1 改成 x = 2",
        "acceptance": {"alias": acceptance_alias} if acceptance_alias else {},
        "rollback": "reverse_apply",
        "scope_paths": ["src/a.py"], "scope_symbols": [],
        "risk": "high", "touches_manifest": False, "amended_from": "",
        "status": "pending", "formula_id": "repair", "scope_lines": [],
    }
    async with Database(root) as db:
        await db.insert_prescription(
            {"id": rx_id, "case_id": case_id, "status": "approved",
             "risk_level": "high", "created_at": now_ms()},
            [item],
        )
        await db.update_case(case_id, status="dispensing")
    return case_id, rx_id


async def _take_and_submit(root: str, rx_id: str, patch: str, **kw) -> dict:
    taken = await pharmacist.dispense(prescription_id=rx_id, project_root=root)
    assert taken["status"] == "need_patch"
    return await pharmacist.dispense(
        prescription_id=rx_id, mode="submit_patch",
        patch_token=taken["patch_token"], snapshot_hash=taken["snapshot_hash"],
        patch=patch, project_root=root, **kw,
    )


# ---------- 配置与判定矩阵 ----------


def test_policy_worktree_defaults_and_parse(git_repo):
    assert load_policy(git_repo).worktree == "high"
    assert load_policy(git_repo).worktree_link_dirs == (".venv", "venv", "node_modules")

    _write_config(git_repo, "safety:\n  worktree: never\n")
    assert load_policy(git_repo).worktree == "never"

    _write_config(git_repo, "safety:\n  worktree: bogus\n")
    policy = load_policy(git_repo)
    assert policy.worktree == "high"
    assert any("无法识别" in note for note in policy.notes)

    _write_config(
        git_repo,
        "safety:\n  worktree_link_dirs: [\".venv\", \"../escape\", \"deps/cache\"]\n",
    )
    policy = load_policy(git_repo)
    assert policy.worktree_link_dirs == (".venv", "deps/cache")
    assert any("相对路径" in note for note in policy.notes)


def test_wants_isolation_matrix(git_repo):
    high = {"risk": "high"}
    low = {"risk": "low"}
    assert wt.wants_isolation(git_repo, high, None) is True
    assert wt.wants_isolation(git_repo, low, None) is False
    assert wt.wants_isolation(git_repo, low, True) is True  # 入参压过配置
    assert wt.wants_isolation(git_repo, high, False) is False

    _write_config(git_repo, "safety:\n  worktree: always\n")
    assert wt.wants_isolation(git_repo, low, None) is True
    _write_config(git_repo, "safety:\n  worktree: never\n")
    assert wt.wants_isolation(git_repo, high, None) is False


# ---------- 建间 / 拆间 ----------


def test_prepare_syncs_uncommitted_scope_and_removes(git_repo):
    dirty = "def f():\n    x = 42\n    return 2\n"
    (Path(git_repo) / "src" / "a.py").write_text(dirty, encoding="utf-8")
    workspace = wt.prepare(git_repo, "rxit-x", ["src/a.py"])
    # 检出的是 HEAD（x = 1），但本味文件必须与主仓工作区对齐（x = 42）
    assert (Path(workspace) / "src" / "a.py").read_text(encoding="utf-8") == dirty
    wt.remove(git_repo, "rxit-x")
    assert not Path(workspace).exists()
    listed = subprocess.run(
        ["git", "-C", git_repo, "worktree", "list"],
        capture_output=True, text=True,
    ).stdout
    assert "rxit-x" not in listed


def test_prepare_requires_a_commit(tmp_path: Path):
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=bare, check=True)
    (bare / "src").mkdir()
    (bare / "src" / "a.py").write_text(_ORIGINAL, encoding="utf-8")
    with pytest.raises(HospitalError):
        wt.prepare(str(bare), "rxit-x", ["src/a.py"])
    assert (bare / "src" / "a.py").read_text(encoding="utf-8") == _ORIGINAL


# ---------- 端到端：隔离施治 ----------


async def test_isolated_submit_patch_merges_back(git_repo):
    _, rx_id = await _high_risk_case(git_repo)
    applied = await _take_and_submit(git_repo, rx_id, _PATCH, isolation=True)
    assert applied["status"] == "applied"
    assert "x = 2" in (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8")
    # 成功即拆间
    assert _no_worktrees(git_repo)


async def test_isolated_validation_failure_zero_touch(git_repo):
    _, rx_id = await _high_risk_case(git_repo)
    outside = _PATCH.replace("src/a.py", "src/other.py")
    with pytest.raises(HospitalError):
        await _take_and_submit(git_repo, rx_id, outside, isolation=True)
    # 主仓逐字节不变 + 隔离间拆干净
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == _ORIGINAL
    assert not (Path(git_repo) / "src" / "other.py").exists()
    assert _no_worktrees(git_repo)


async def test_isolated_acceptance_failure_zero_touch(git_repo):
    _write_config(
        git_repo,
        "acceptance:\n  commands:\n    check: python --definitely-not-a-real-flag\n",
    )
    _, rx_id = await _high_risk_case(git_repo, acceptance_alias="check")
    failed = await _take_and_submit(git_repo, rx_id, _PATCH, isolation=True)
    assert failed["status"] == "failed"
    assert "主仓未受影响" in failed["dose"]["error"]
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == _ORIGINAL
    assert _no_worktrees(git_repo)


async def test_never_config_writes_main_directly(git_repo):
    _write_config(git_repo, "safety:\n  worktree: never\n")
    _, rx_id = await _high_risk_case(git_repo)
    applied = await _take_and_submit(git_repo, rx_id, _PATCH)
    assert applied["status"] == "applied"
    assert "x = 2" in (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8")
    assert _no_worktrees(git_repo)


async def test_isolation_default_high_catches_high_risk(git_repo):
    """默认 high：不显式传 isolation，高危煎药也进隔离间（全程主仓不动，最后才合回）。"""
    _, rx_id = await _high_risk_case(git_repo)
    applied = await _take_and_submit(git_repo, rx_id, _PATCH)
    assert applied["status"] == "applied"
    assert "x = 2" in (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8")
    assert _no_worktrees(git_repo)


# ---------- 合回单元 ----------


async def _settle_args(root: str, case_id: str, rx_id: str):
    async with Database(root) as db:
        case = await db.get_case(case_id)
        rx = await db.get_prescription(rx_id)
    return db, case, rx


async def test_merge_back_success_copies_bytes(git_repo):
    case_id, rx_id = await _high_risk_case(git_repo)
    workspace = wt.prepare(git_repo, "rxit-1", ["src/a.py"])
    after_text = "def f():\n    x = 2\n    return 2\n"
    async with Database(git_repo) as db:
        case = await db.get_case(case_id)
        rx = await db.get_prescription(rx_id)
        nursing = {"acceptance": "pass", "verdict": "pass"}
        settled = await pharmacist._settle_isolated(
            db, git_repo, workspace, case, rx, rx["items"][0],
            {"src/a.py": _ORIGINAL}, {"src/a.py": after_text}, "llm_patch", nursing,
        )
    assert settled is None
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == after_text
    wt.remove(git_repo, "rxit-1")


async def test_merge_back_drift_blocked_and_scene_preserved(git_repo):
    case_id, rx_id = await _high_risk_case(git_repo)
    workspace = wt.prepare(git_repo, "rxit-2", ["src/a.py"])
    user_edit = "def f():\n    x = 99\n    return 2\n"
    (Path(git_repo) / "src" / "a.py").write_text(user_edit, encoding="utf-8")
    async with Database(git_repo) as db:
        case = await db.get_case(case_id)
        rx = await db.get_prescription(rx_id)
        nursing = {"acceptance": "pass", "verdict": "pass"}
        payload = await pharmacist._settle_isolated(
            db, git_repo, workspace, case, rx, rx["items"][0],
            {"src/a.py": _ORIGINAL}, {"src/a.py": "def f():\n    x = 2\n    return 2\n"},
            "llm_patch", nursing,
        )
    assert payload["status"] == "blocked"
    assert payload["blocked_on"] == "rollback_conflict"
    assert "合回漂移" in payload["dose"]["error"]
    # 用户改动一个字节都不动，隔离间现场保留供人工核对
    assert (Path(git_repo) / "src" / "a.py").read_text(encoding="utf-8") == user_edit
    assert Path(workspace).exists()
    wt.remove(git_repo, "rxit-2")
