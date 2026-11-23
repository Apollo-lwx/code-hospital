"""G6 移动剂型（move-formula 草案）：新名推导、规格化键法、N999 入方、
机械移动落盘与字节级回滚、快照 v2 兼容、worktree 合回的 None 语义。"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from code_hospital.catalog.dispositions import resolve
from code_hospital.catalog.rename import module_snake_name
from code_hospital.core.algorithms import Hunk
from code_hospital.core.errors import HospitalError
from code_hospital.core.ids import new_id
from code_hospital.core.models import Diagnosis, Finding
from code_hospital.departments import pharmacist, reception
from code_hospital.departments.prescriber import build_items
from code_hospital.lsp.edits import EditApplyError, move_hunks
from code_hospital.lsp.pool import Pool
from code_hospital.lsp.registry import LanguageSpec
from code_hospital.storage.db import Database, now_ms

FIXTURE = Path(__file__).parent / "fixtures" / "fake_lsp_server.py"


def _fake_spec(**over) -> LanguageSpec:
    base = dict(
        id="python", extensions=(".py",), language_id="python",
        command=sys.executable, args=(str(FIXTURE),),
        startup_timeout_s=15.0, request_timeout_s=10.0,
    )
    base.update(over)
    return LanguageSpec(**base)


# ---------- 新名推导（机械映射）----------


def test_module_snake_name():
    assert module_snake_name("FooBar") == "foo_bar"
    assert module_snake_name("HTTPResponse") == "http_response"
    assert module_snake_name("my-module") == "my_module"
    assert module_snake_name("FooBAR") == "foo_bar"
    assert module_snake_name("foo_bar") == "foo_bar"  # 已合规：推导即现名（入方侧会拦）
    assert module_snake_name("") == ""


# ---------- 处置矩阵：N999 默认规范类告知 ----------


def test_n999_default_disposition_is_convention_advisory():
    disposition = resolve("N999")
    assert disposition is not None
    assert disposition.resolved_class == "convention"
    assert disposition.advisory
    # 用户配置优先于默认表
    from code_hospital.catalog.dispositions import Disposition

    relaxed = resolve("N999", (Disposition("N999", cls="convention", auto_fix="auto"),))
    assert relaxed.resolved_auto_fix == "auto"


# ---------- edits.move_hunks：规格化与键法 ----------


def _edit(uri: str, old: str, new: str) -> dict:
    return {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": len(old)}},
            "newText": new}


def test_move_hunks_old_uri_keying(tmp_path: Path):
    old = tmp_path / "FooBar.py"
    old.write_text("X = 1\n", encoding="utf-8")
    new = tmp_path / "foo_bar.py"
    edit = {"changes": {old.as_uri(): [_edit(old.as_uri(), "X", "Y")]}}
    hunks, keying = move_hunks(str(tmp_path), edit, old, new)
    assert keying == "old"
    assert [(h.path, h.search, h.replace) for h in hunks] == [("FooBar.py", ["X = 1"], ["Y = 1"])]


def test_move_hunks_new_uri_keyed_edits_rekeyed_to_old(tmp_path: Path):
    """键新 URI 的编辑规格化到旧路径（对内容的编辑），键法记台账。"""
    old = tmp_path / "FooBar.py"
    old.write_text("X = 1\n", encoding="utf-8")
    new = tmp_path / "foo_bar.py"
    edit = {"changes": {new.as_uri(): [_edit(new.as_uri(), "X", "Y")]}}
    hunks, keying = move_hunks(str(tmp_path), edit, old, new)
    assert keying == "new"
    assert [h.path for h in hunks] == ["FooBar.py"]


def test_move_hunks_ref_file_and_unexpected_forms_rejected(tmp_path: Path):
    old = tmp_path / "FooBar.py"
    old.write_text("X = 1\n", encoding="utf-8")
    ref = tmp_path / "user.py"
    ref.write_text("import FooBar\n", encoding="utf-8")
    new = tmp_path / "foo_bar.py"
    # 引用文件编辑正常进 hunk，被移动文件自身无编辑 → 键法 none
    edit = {"changes": {ref.as_uri(): [_edit(ref.as_uri(), "import FooBar", "import foo_bar")]}}
    hunks, keying = move_hunks(str(tmp_path), edit, old, new)
    assert keying == "none"
    assert [(h.path, h.search, h.replace) for h in hunks] == [
        ("user.py", ["import FooBar"], ["import foo_bar"]),
    ]
    # 新旧 URI 双键同现：意外即拒，不猜
    both = {"changes": {
        old.as_uri(): [_edit(old.as_uri(), "X", "Y")],
        new.as_uri(): [_edit(new.as_uri(), "X", "Z")],
    }}
    with pytest.raises(EditApplyError, match="新旧两个 URI"):
        move_hunks(str(tmp_path), both, old, new)
    # documentChanges 带文件级操作：意外即拒
    with pytest.raises(EditApplyError, match="文件级操作"):
        move_hunks(str(tmp_path), {"documentChanges": [{"kind": "rename", "oldUri": old.as_uri(), "newUri": new.as_uri()}]}, old, new)
    # 编辑落到仓库外：拒
    outside = tmp_path.parent / "outside.py"
    outside.write_text("A = 1\n", encoding="utf-8")
    with pytest.raises(EditApplyError, match="仓库外"):
        move_hunks(str(tmp_path), {"changes": {outside.as_uri(): [_edit(outside.as_uri(), "A", "B")]}}, old, new)


def test_file_hunks_pure_insert_borrows_context(tmp_path: Path):
    """纯插入块向上下借 1 行上下文，锚点关的唯一匹配才查得着。"""
    target = tmp_path / "a.py"
    target.write_text("first\nlast\n", encoding="utf-8")
    edit = {"changes": {target.as_uri(): [{
        "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 0}},
        "newText": "middle\n",
    }]}}
    hunks, _ = move_hunks(str(tmp_path), edit, target, tmp_path / "b.py")
    assert len(hunks) == 1
    assert hunks[0].search  # search 非空
    assert hunks[0].replace != hunks[0].search
    assert "middle" in "\n".join(hunks[0].replace)


# ---------- pool：能力探测与 move_payload ----------


def _seed_repo(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "FooBar.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "src" / "user.py").write_text("import FooBar\n", encoding="utf-8")


def test_pool_move_support_detection(tmp_path: Path, monkeypatch):
    _seed_repo(tmp_path)
    specs = {"python": _fake_spec()}
    # 假服务器不认领 willRenameFiles → 在岗但不支持
    with Pool(str(tmp_path), specs=specs) as pool:
        assert pool.move_support("src/FooBar.py") is False
        assert any("willRenameFiles" in note for note in pool.notes())
        assert pool.move_payload("src/FooBar.py", "src/foo_bar.py") is None
    # 认领 → True，payload 正常产出
    monkeypatch.setenv("FAKE_LSP_WILL_RENAME_CAP", "1")
    ref = tmp_path / "src" / "user.py"
    monkeypatch.setenv("FAKE_LSP_WILL_RENAME", json.dumps({"changes": {ref.as_uri(): [
        _edit(ref.as_uri(), "import FooBar", "import foo_bar"),
    ]}}))
    with Pool(str(tmp_path), specs=specs) as pool:
        assert pool.move_support("src/FooBar.py") is True
        payload = pool.move_payload("src/FooBar.py", "src/foo_bar.py")
        assert payload is not None
        assert payload["move"] == {"from": "src/FooBar.py", "to": "src/foo_bar.py"}
        assert [(h.path, h.search, h.replace) for h in payload["edits"]] == [
            ("src/user.py", ["import FooBar"], ["import foo_bar"]),
        ]


def test_pool_move_payload_degrades(tmp_path: Path):
    _seed_repo(tmp_path)
    specs = {"python": _fake_spec(command="definitely-not-a-real-lsp-server-x")}
    with Pool(str(tmp_path), specs=specs) as pool:
        assert pool.move_support("src/FooBar.py") is None
        assert pool.move_payload("src/FooBar.py", "src/foo_bar.py") is None
        assert any("未到岗" in note for note in pool.notes())


# ---------- 开方：N999 入方 ----------


class _MovePool:
    """开方侧的确定性桩：能力/干跑结果显式给，与本机装没装 pyright 无关。"""

    def __init__(self, support=True, payload=None, notes=()):
        self._support = support
        self._payload = payload
        self._notes = list(notes)

    def cross_file_refs(self, file_path, line, symbol):
        return None

    def move_support(self, file_path):
        return self._support

    def move_payload(self, file_path, new_file_path):
        return self._payload

    def notes(self):
        return list(self._notes)

    def close(self):
        pass


def _n999(file_path: str) -> Finding:
    return Finding(
        id=f"fnd-N999-{file_path}", case_id="case-x", report_id="rpt-x",
        tool="ruff", rule_id="N999", severity="nit",
        file_path=file_path, line_start=1, line_end=1,
        symbol="", message=f"Invalid module name: '{Path(file_path).stem}'",
        fingerprint=f"fp-N999-{file_path}",
    )


def _build_g6(tmp_path: Path, members: list[Finding], pool) -> tuple[list, list]:
    diagnoses = [
        Diagnosis(
            id=f"dia-{m.id}", case_id="case-x", report_id="rpt-x",
            fingerprint=m.fingerprint, category="N999",
            root_cause=f"模块名 {Path(m.file_path).stem} 不符合 snake_case", severity="nit",
            confidence=0.9, fixability="llm_patch", member_ids=[m.id],
            specialty_id="python", formulary_matched=True,
        )
        for m in members
    ]
    by_id = {f.id: f for f in members}
    drops: list[dict] = []
    items = build_items(
        diagnoses, by_id, "rx-x", True,
        project_root=str(tmp_path), drops_out=drops, rename_pool=pool,
    )
    return items, drops


def test_g6_prescribed_with_derived_scope(tmp_path: Path):
    """LSP 到岗且认领 willRenameFiles：入方，scope = 新旧路径 ∪ 干跑引用集，默认告知级。"""
    _seed_repo(tmp_path)
    payload = {"move": {"from": "src/FooBar.py", "to": "src/foo_bar.py"}, "edits": [
        Hunk(path="src/user.py", search=["import FooBar"], replace=["import foo_bar"], line_hint=1),
    ]}
    items, drops = _build_g6(tmp_path, [_n999("src/FooBar.py")], _MovePool(True, payload))
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert len(rename_items) == 1
    item = rename_items[0]
    assert item["move_to"] == "src/foo_bar.py"
    assert set(item["scope_paths"]) == {"src/FooBar.py", "src/foo_bar.py", "src/user.py"}
    assert item["method"] == "llm_patch"
    assert item["status"] == "advisory"  # 处置矩阵默认表：N999 规范类告知
    assert "lsp_move" in item["instructions"]
    assert "已知盲区" in item["instructions"]
    assert drops == []


def test_g6_dropped_when_lsp_absent_or_incapable(tmp_path: Path):
    """缺席 / 不认领 / 干跑失败：不入方、待复核写明原因，行为与现状逐字节一致。"""
    _seed_repo(tmp_path)
    items, drops = _build_g6(tmp_path, [_n999("src/FooBar.py")], _MovePool(None))
    assert [it for it in items if it["formula_id"] == "rename"] == []
    assert any("LSP 未到岗" in row["reason"] for row in drops)

    items, drops = _build_g6(tmp_path, [_n999("src/FooBar.py")], _MovePool(False))
    assert [it for it in items if it["formula_id"] == "rename"] == []
    assert any("没认领 willRenameFiles" in row["reason"] for row in drops)

    items, drops = _build_g6(tmp_path, [_n999("src/FooBar.py")], _MovePool(True, None))
    assert [it for it in items if it["formula_id"] == "rename"] == []
    assert any("干跑没跑成" in row["reason"] for row in drops)


def test_g6_exempted_by_repo_naming_convention(tmp_path: Path):
    _seed_repo(tmp_path)
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  rename:\n    exempt: [FooBar]\n", encoding="utf-8",
    )
    payload = {"move": {"from": "src/FooBar.py", "to": "src/foo_bar.py"}, "edits": []}
    items, drops = _build_g6(tmp_path, [_n999("src/FooBar.py")], _MovePool(True, payload))
    assert [it for it in items if it["formula_id"] == "rename"] == []
    assert any("命名约定" in row["reason"] for row in drops)


def test_g6_batch_merges_into_manual(tmp_path: Path):
    """全仓 N999 ≥ 20 个文件：合并一味外购批量迁移，不开移动剂型。"""
    (tmp_path / "src").mkdir()
    members = []
    for idx in range(20):
        path = f"src/Mod{idx}.py"
        (tmp_path / "src" / f"Mod{idx}.py").write_text("X = 1\n", encoding="utf-8")
        members.append(_n999(path))
    items, drops = _build_g6(tmp_path, members, _MovePool(True, None))
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert len(rename_items) == 1
    item = rename_items[0]
    assert item["method"] == "manual"
    assert "批量迁移" in item["instructions"]
    assert "move_to" not in item


# ---------- 药房：mode=lsp_move ----------


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("需要 git")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


async def _move_case(root: str, scope_paths: list[str], move_to: str, acceptance_alias: str = "") -> tuple[str, str]:
    reg = await reception.register(
        title="移动", scope_type="staged", scope={},
        auto_advance=True, project_root=root,
    )
    case_id = reg["case_id"]
    rx_id = new_id("rx")
    item = {
        "id": new_id("rxit"), "seq": 1, "diagnosis_id": "",
        "target_file": "src/FooBar.py", "target_symbol": "",
        "method": "llm_patch", "instructions": "G6 机械移动",
        "acceptance": {"alias": acceptance_alias} if acceptance_alias else {},
        "rollback": "reverse_apply",
        "scope_paths": scope_paths, "scope_symbols": [],
        "risk": "medium", "touches_manifest": False, "amended_from": "",
        "status": "pending", "formula_id": "rename", "scope_lines": [],
        "move_to": move_to,
    }
    async with Database(root) as db:
        await db.insert_prescription(
            {"id": rx_id, "case_id": case_id, "status": "approved",
             "risk_level": "medium", "created_at": now_ms()},
            [item],
        )
        await db.update_case(case_id, status="dispensing")
    return case_id, rx_id


def _arm_fake_lsp(tmp_path: Path, monkeypatch, edits: dict | None = None):
    monkeypatch.setenv("FAKE_LSP_WILL_RENAME_CAP", "1")
    if edits is not None:
        monkeypatch.setenv("FAKE_LSP_WILL_RENAME", json.dumps(edits))
    monkeypatch.setattr(
        "code_hospital.lsp.pool.registry.for_project",
        lambda root: ({"python": _fake_spec()}, []),
    )


def _ref_edit(tmp_path: Path) -> dict:
    ref = tmp_path / "src" / "user.py"
    return {"changes": {ref.as_uri(): [_edit(ref.as_uri(), "import FooBar", "import foo_bar")]}}


async def test_lsp_move_end_to_end(git_repo: Path, monkeypatch):
    tmp_path = git_repo
    """移动落盘：写新删旧 + 引用更新；剂量/快照 v2（新路径记 null）落账。"""
    _seed_repo(tmp_path)
    _arm_fake_lsp(tmp_path, monkeypatch, _ref_edit(tmp_path))
    _, rx_id = await _move_case(
        str(tmp_path), ["src/FooBar.py", "src/foo_bar.py", "src/user.py"], "src/foo_bar.py",
    )
    result = await pharmacist.dispense(prescription_id=rx_id, mode="lsp_move", project_root=str(tmp_path))
    assert result["status"] == "applied"
    assert result["dose"]["executor"] == "lsp_move"
    old = tmp_path / "src" / "FooBar.py"
    new = tmp_path / "src" / "foo_bar.py"
    assert not old.exists()
    assert new.read_text(encoding="utf-8") == "X = 1\n"
    assert (tmp_path / "src" / "user.py").read_text(encoding="utf-8") == "import foo_bar\n"
    # 快照 v2：新路径记 null（施治前不存在），旧文件与引用文件持内容
    snapshot = json.loads(Path(_snapshot_file(tmp_path)).read_text(encoding="utf-8"))
    assert snapshot["src/foo_bar.py"] is None
    assert snapshot["src/FooBar.py"] == "X = 1\n"
    # 结构化 payload 同目录存档
    assert Path(result["dose"]["patch_path"]).with_suffix(".move.json").is_file()


def _snapshot_file(root: Path) -> str:
    found = sorted((root / ".hospital" / "patches").rglob("*.snapshot.json"))
    assert found, "快照没落盘"
    return str(found[-1])


async def test_lsp_move_dry_run_lands_nothing(git_repo: Path, monkeypatch):
    tmp_path = git_repo
    _seed_repo(tmp_path)
    _arm_fake_lsp(tmp_path, monkeypatch, _ref_edit(tmp_path))
    _, rx_id = await _move_case(
        str(tmp_path), ["src/FooBar.py", "src/foo_bar.py", "src/user.py"], "src/foo_bar.py",
    )
    result = await pharmacist.dispense(
        prescription_id=rx_id, mode="lsp_move", dry_run=True, project_root=str(tmp_path),
    )
    assert result["status"] == "dry_run"
    assert "rename from src/FooBar.py" in result["diff"]
    assert "rename to src/foo_bar.py" in result["diff"]
    assert (tmp_path / "src" / "FooBar.py").read_text(encoding="utf-8") == "X = 1\n"
    assert not (tmp_path / "src" / "foo_bar.py").exists()
    assert (tmp_path / "src" / "user.py").read_text(encoding="utf-8") == "import FooBar\n"


async def test_lsp_move_rollback_restores_byte_level(git_repo: Path, monkeypatch):
    tmp_path = git_repo
    """验收失败 → v2 快照恢复：旧文件写回、新路径删除、引用还原，逐字节一致。"""
    _seed_repo(tmp_path)
    _arm_fake_lsp(tmp_path, monkeypatch, _ref_edit(tmp_path))
    (tmp_path / "code-hospital.yaml").write_text(
        "acceptance:\n  commands:\n    check: python --definitely-not-a-real-flag\n",
        encoding="utf-8",
    )
    _, rx_id = await _move_case(
        str(tmp_path), ["src/FooBar.py", "src/foo_bar.py", "src/user.py"], "src/foo_bar.py",
        acceptance_alias="check",
    )
    result = await pharmacist.dispense(prescription_id=rx_id, mode="lsp_move", project_root=str(tmp_path))
    assert result["status"] == "failed"
    assert (tmp_path / "src" / "FooBar.py").read_text(encoding="utf-8") == "X = 1\n"
    assert not (tmp_path / "src" / "foo_bar.py").exists()
    assert (tmp_path / "src" / "user.py").read_text(encoding="utf-8") == "import FooBar\n"


async def test_lsp_move_guards(git_repo: Path, monkeypatch):
    tmp_path = git_repo
    """适用面 / 撞名即拒 / 越出 scope 即拒 / new_path 只准改文件名部分。"""
    _seed_repo(tmp_path)
    _arm_fake_lsp(tmp_path, monkeypatch, _ref_edit(tmp_path))
    case_id, rx_id = await _move_case(
        str(tmp_path), ["src/FooBar.py", "src/foo_bar.py", "src/user.py"], "src/foo_bar.py",
    )
    # new_path 换目录：拒
    with pytest.raises(HospitalError, match="只改文件名部分"):
        await pharmacist.dispense(
            prescription_id=rx_id, mode="lsp_move", new_path="other/foo_bar.py",
            project_root=str(tmp_path),
        )
    # new_path 换扩展名：拒
    with pytest.raises(HospitalError, match="扩展名"):
        await pharmacist.dispense(
            prescription_id=rx_id, mode="lsp_move", new_path="src/foo_bar.txt",
            project_root=str(tmp_path),
        )
    # 撞名即拒（锚点特例）
    (tmp_path / "src" / "foo_bar.py").write_text("Y = 2\n", encoding="utf-8")
    with pytest.raises(HospitalError, match="撞名即拒"):
        await pharmacist.dispense(prescription_id=rx_id, mode="lsp_move", project_root=str(tmp_path))
    (tmp_path / "src" / "foo_bar.py").unlink()
    # 适用面：非 G6 味（没有 move_to）拒
    async with Database(str(tmp_path)) as db:
        await db.update_prescription_item((await db.get_prescription(rx_id))["items"][0]["id"], move_to="")
    with pytest.raises(HospitalError, match="只适用于正骨 G6"):
        await pharmacist.dispense(prescription_id=rx_id, mode="lsp_move", project_root=str(tmp_path))


async def test_lsp_move_out_of_scope_edit_rejected(git_repo: Path, monkeypatch):
    tmp_path = git_repo
    """LSP 实际触及越出开方 scope：即拒，不落盘（scope 特例）。"""
    _seed_repo(tmp_path)
    (tmp_path / "src" / "other.py").write_text("import FooBar\n", encoding="utf-8")
    edits = _ref_edit(tmp_path)
    other = tmp_path / "src" / "other.py"
    edits["changes"][other.as_uri()] = [_edit(other.as_uri(), "import FooBar", "import foo_bar")]
    _arm_fake_lsp(tmp_path, monkeypatch, edits)
    _, rx_id = await _move_case(
        str(tmp_path), ["src/FooBar.py", "src/foo_bar.py", "src/user.py"], "src/foo_bar.py",
    )
    with pytest.raises(HospitalError, match="不在处方范围"):
        await pharmacist.dispense(prescription_id=rx_id, mode="lsp_move", project_root=str(tmp_path))
    assert (tmp_path / "src" / "FooBar.py").read_text(encoding="utf-8") == "X = 1\n"
    assert (tmp_path / "src" / "other.py").read_text(encoding="utf-8") == "import FooBar\n"


# ---------- 快照 v2 与恢复 ----------


def test_snapshot_v2_roundtrip_and_v1_compat(tmp_path: Path):
    from code_hospital.departments.pharmacist import _load_snapshot, _write_snapshot

    # v2：null 原样往返
    _write_snapshot(str(tmp_path), "case-x", "rxit-x", 1, {"a.py": "A\n", "b.py": None})
    loaded = _load_snapshot(str(tmp_path), "case-x", "rxit-x", 1)
    assert loaded == {"a.py": "A\n", "b.py": None}
    # v1（全 str）行为不变
    _write_snapshot(str(tmp_path), "case-x", "rxit-y", 1, {"a.py": "A\n"})
    assert _load_snapshot(str(tmp_path), "case-x", "rxit-y", 1) == {"a.py": "A\n"}


def test_restore_snapshot_deletes_and_prunes(tmp_path: Path):
    from code_hospital.departments.pharmacist import _restore_snapshot

    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "new.py").write_text("N\n", encoding="utf-8")
    (tmp_path / "old.py").write_text("tampered\n", encoding="utf-8")
    bad = _restore_snapshot(tmp_path, {"old.py": "original\n", "pkg/sub/new.py": None})
    assert bad == []
    assert (tmp_path / "old.py").read_text(encoding="utf-8") == "original\n"
    assert not (tmp_path / "pkg" / "sub" / "new.py").exists()
    # 空目录逐级修剪
    assert not (tmp_path / "pkg" / "sub").exists()
    assert not (tmp_path / "pkg").exists()
    # root 本身绝不动
    assert tmp_path.exists()
