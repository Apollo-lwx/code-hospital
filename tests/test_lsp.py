"""LSP 基座：注册表（SPI）、假服务器上的客户端协议、池的降级与销账语义。"""

import json
import sys
from pathlib import Path

from code_hospital.lsp import registry
from code_hospital.lsp.client import Client
from code_hospital.lsp.pool import Pool
from code_hospital.lsp.registry import LanguageSpec

FIXTURE = Path(__file__).parent / "fixtures" / "fake_lsp_server.py"


def _fake_spec(**over) -> LanguageSpec:
    """指向假 LSP 服务器的规格：命令是解释器本体（绝对路径绕过 PATH 裸名校验，
    校验是注册表层的纪律，客户端只负责拉起）。"""
    base = dict(
        id="python", extensions=(".py",), language_id="python",
        command=sys.executable, args=(str(FIXTURE),),
        startup_timeout_s=15.0, request_timeout_s=10.0,
    )
    base.update(over)
    return LanguageSpec(**base)


# ---------- 注册表 ----------


def test_builtin_languages():
    specs = registry.builtin()
    assert {"python", "typescript", "java"} <= set(specs)
    by_ext = registry.by_extension(specs)
    assert by_ext[".py"].command == "pyright-langserver"
    assert by_ext[".ts"].command == "typescript-language-server"
    assert by_ext[".java"].command == "jdtls"
    # 方言：.js 的 languageId 是 javascript
    assert by_ext[".js"].language_id_for(".js") == "javascript"
    assert by_ext[".ts"].language_id_for(".ts") == "typescript"
    # 内置三语言都带安装指引（只展示，永不执行）
    assert "pyright" in specs["python"].install_hint
    assert "typescript-language-server" in specs["typescript"].install_hint
    assert specs["java"].install_hint


def test_repo_config_extends_and_overrides(tmp_path: Path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  lsp:\n    languages:\n"
        "      - id: go\n"
        "        extensions: [.go]\n"
        "        command: gopls\n"
        "        args: [serve]\n"
        "        install_hint: go install golang.org/x/tools/gopls@latest\n"
        "      - id: python\n"
        "        extensions: [.py]\n"
        "        command: basedpyright-langserver\n"
        "        args: [--stdio]\n"
        "        startup_timeout_s: 90\n",
        encoding="utf-8",
    )
    specs, notes = registry.for_project(str(tmp_path))
    assert notes == []
    assert specs["go"].command == "gopls"
    assert specs["go"].install_hint == "go install golang.org/x/tools/gopls@latest"
    # 仓级按 id 覆盖内置
    assert specs["python"].command == "basedpyright-langserver"
    assert specs["python"].startup_timeout_s == 90.0
    # 覆盖行没写 install_hint 就是空，不继承内置的
    assert specs["python"].install_hint == ""


def test_repo_config_rejects_bad_rows(tmp_path: Path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  lsp:\n    languages:\n"
        "      - id: evil\n"
        "        extensions: [.py]\n"
        "        command: C:/tools/evil.exe\n"   # 命令名只认 PATH 裸名
        "      - id: noext\n"
        "        command: whatever\n",
        encoding="utf-8",
    )
    specs, notes = registry.for_project(str(tmp_path))
    assert "evil" not in specs
    assert "noext" not in specs
    assert len(notes) == 2


# ---------- 客户端协议（假服务器）----------


def test_client_roundtrip_and_shutdown(tmp_path: Path, monkeypatch):
    target = tmp_path / "a.py"
    target.write_text("def Foo():\n    return 1\n", encoding="utf-8")
    other = tmp_path / "b.py"
    other.write_text("from a import Foo\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_LSP_REFS", json.dumps([
        {"uri": other.as_uri(), "line": 0, "character": 16},
    ]))
    client = Client.start(_fake_spec(), str(tmp_path))
    try:
        refs = client.references(target, 0, 4)
        assert [row["uri"] for row in refs] == [other.as_uri()]
    finally:
        client.close()
    assert client._proc.poll() is not None  # 会话不泄漏：进程已退


def test_client_absent_command(tmp_path: Path):
    spec = _fake_spec(command="definitely-not-a-real-lsp-server-x")
    try:
        Client.start(spec, str(tmp_path))
        raise AssertionError("应该抛 LspError")
    except Exception as exc:
        assert "PATH" in str(exc)


# ---------- 池：降级与销账语义 ----------


def _seed_repo(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("from a import Foo\n", encoding="utf-8")


def test_pool_returns_none_when_language_unregistered(tmp_path: Path):
    _seed_repo(tmp_path)
    with Pool(str(tmp_path), specs={}) as pool:
        assert pool.cross_file_refs("src/a.py", 1, "Foo") is None
        assert pool.notes() == []  # 语言没注册不算设备缺席，不留痕


def test_pool_absent_command_degrades_with_note(tmp_path: Path):
    _seed_repo(tmp_path)
    specs = {"python": _fake_spec(command="definitely-not-a-real-lsp-server-x")}
    with Pool(str(tmp_path), specs=specs) as pool:
        assert pool.cross_file_refs("src/a.py", 1, "Foo") is None
        assert pool.cross_file_refs("src/a.py", 1, "Foo") is None  # 缺席记一次就够
        notes = pool.notes()
    assert len(notes) == 1
    assert "未到岗" in notes[0]


def test_pool_absent_command_note_carries_install_hint(tmp_path: Path):
    """仪器缺席的降级提示带安装指引（只展示，永不执行）。"""
    _seed_repo(tmp_path)
    specs = {"python": _fake_spec(
        command="definitely-not-a-real-lsp-server-x",
        install_hint="pip install pyright",
    )}
    with Pool(str(tmp_path), specs=specs) as pool:
        assert pool.cross_file_refs("src/a.py", 1, "Foo") is None
        notes = pool.notes()
    assert "安装：pip install pyright" in notes[0]


def test_pool_separates_cross_file_refs(tmp_path: Path, monkeypatch):
    _seed_repo(tmp_path)
    target = tmp_path / "src" / "a.py"
    other = tmp_path / "src" / "b.py"
    # 同文件引用（本文件闭包内，正常）+ 跨文件引用各一处
    monkeypatch.setenv("FAKE_LSP_REFS", json.dumps([
        {"uri": target.as_uri(), "line": 0, "character": 4},
        {"uri": other.as_uri(), "line": 0, "character": 16},
    ]))
    specs = {"python": _fake_spec()}
    with Pool(str(tmp_path), specs=specs) as pool:
        assert pool.cross_file_refs("src/a.py", 1, "Foo") == {"src/b.py"}


def test_pool_empty_refs_means_clear(tmp_path: Path, monkeypatch):
    _seed_repo(tmp_path)
    monkeypatch.setenv("FAKE_LSP_REFS", "[]")
    specs = {"python": _fake_spec()}
    with Pool(str(tmp_path), specs=specs) as pool:
        assert pool.cross_file_refs("src/a.py", 1, "Foo") == set()
        assert pool.notes() == []


# ---------- WorkspaceEdit → diff（edits.py）----------


def test_edits_to_diff_changes_form(tmp_path: Path):
    from code_hospital.lsp.edits import to_diff

    target = tmp_path / "a.py"
    target.write_text("def Foo():\n    return Foo\n", encoding="utf-8")
    edit = {"changes": {target.as_uri(): [
        {"range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 7}},
         "newText": "foo"},
        {"range": {"start": {"line": 1, "character": 11}, "end": {"line": 1, "character": 14}},
         "newText": "foo"},
    ]}}
    diff, touched = to_diff(str(tmp_path), edit)
    assert touched == ["a.py"]
    assert "-def Foo():" in diff
    assert "+def foo():" in diff
    # 不落盘
    assert "Foo" in target.read_text(encoding="utf-8")


def test_edits_to_diff_document_changes_form(tmp_path: Path):
    from code_hospital.lsp.edits import to_diff

    target = tmp_path / "b.py"
    target.write_text("X = 1\n", encoding="utf-8")
    edit = {"documentChanges": [{
        "textDocument": {"uri": target.as_uri(), "version": 1},
        "edits": [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                   "newText": "x"}],
    }]}
    diff, touched = to_diff(str(tmp_path), edit)
    assert touched == ["b.py"]
    assert "-X = 1" in diff and "+x = 1" in diff


def test_edits_reject_overlapping_and_outside(tmp_path: Path):
    from code_hospital.lsp.edits import EditApplyError, to_diff

    target = tmp_path / "a.py"
    target.write_text("def Foo():\n", encoding="utf-8")
    overlap = {"changes": {target.as_uri(): [
        {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}},
         "newText": "x"},
        {"range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 7}},
         "newText": "y"},
    ]}}
    try:
        to_diff(str(tmp_path), overlap)
        raise AssertionError("重叠区间该被拒")
    except EditApplyError as exc:
        assert "重叠" in str(exc)

    outside = tmp_path.parent / "outside.py"
    outside.write_text("X = 1\n", encoding="utf-8")
    try:
        to_diff(str(tmp_path), {"changes": {outside.as_uri(): [
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
             "newText": "x"},
        ]}})
        raise AssertionError("仓外改动该被拒")
    except EditApplyError as exc:
        assert "仓库外" in str(exc)


# ---------- pool.rename_patch ----------


def test_pool_rename_patch_roundtrip(tmp_path: Path, monkeypatch):
    _seed_repo(tmp_path)
    target = tmp_path / "src" / "a.py"
    monkeypatch.setenv("FAKE_LSP_RENAME", json.dumps({"changes": {target.as_uri(): [
        {"range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 7}},
         "newText": "foo"},
    ]}}))
    specs = {"python": _fake_spec()}
    with Pool(str(tmp_path), specs=specs) as pool:
        diff = pool.rename_patch("src/a.py", 1, "Foo", "foo")
        assert diff is not None
        assert "-def Foo():" in diff and "+def foo():" in diff
        assert pool.notes() == []


def test_pool_rename_patch_degrades(tmp_path: Path):
    _seed_repo(tmp_path)
    specs = {"python": _fake_spec(command="definitely-not-a-real-lsp-server-x")}
    with Pool(str(tmp_path), specs=specs) as pool:
        assert pool.rename_patch("src/a.py", 1, "Foo", "foo") is None
        assert any("未到岗" in note for note in pool.notes())
    # 新名字不合法：不拉起服务器直接拒
    with Pool(str(tmp_path), specs={"python": _fake_spec()}) as pool:
        assert pool.rename_patch("src/a.py", 1, "Foo", "not a name") is None
        assert any("合法标识符" in note for note in pool.notes())


# ---------- 药房 mode=lsp_rename（干跑，不碰 db）----------


def _item(**over):
    base = {
        "id": "rxit-x", "formula_id": "rename", "method": "llm_patch",
        "target_file": "src/a.py", "target_symbol": "Foo", "rename_line": 1,
        "scope_paths": ["src/a.py"], "touches_manifest": False,
    }
    base.update(over)
    return base


class _Case:
    id = "case-x"
    round = 1
    status = "dispensing"


def test_lsp_rename_guards(tmp_path: Path, monkeypatch):
    import pytest

    from code_hospital.core.errors import HospitalError
    from code_hospital.departments.pharmacist import _lsp_rename

    async def run(item, new_name="foo"):
        return await _lsp_rename(None, str(tmp_path), _Case(), None, item, new_name, True)

    with pytest.raises(HospitalError, match="只适用于正骨"):
        asyncio_run(run(_item(formula_id="clean")))
    with pytest.raises(HospitalError, match="缺符号定位"):
        asyncio_run(run(_item(target_symbol="")))
    with pytest.raises(HospitalError, match="新名字"):
        asyncio_run(run(_item(), new_name="not a name"))
    # LSP 缺席：注册表覆盖成「命令不存在」的规格，与本机装没装 pyright 无关
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(
        "code_hospital.lsp.pool.registry.for_project",
        lambda root: ({"python": _fake_spec(command="definitely-not-a-real-lsp-server-x")}, []),
    )
    with pytest.raises(HospitalError, match="没跑成"):
        asyncio_run(run(_item()))


def test_lsp_rename_dry_run_applies_nothing(tmp_path: Path, monkeypatch):
    from code_hospital.departments.pharmacist import _lsp_rename

    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "a.py"
    target.write_text("def Foo():\n    return Foo\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_LSP_RENAME", json.dumps({"changes": {target.as_uri(): [
        {"range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 7}},
         "newText": "foo"},
        {"range": {"start": {"line": 1, "character": 11}, "end": {"line": 1, "character": 14}},
         "newText": "foo"},
    ]}}))
    monkeypatch.setattr(
        "code_hospital.lsp.pool.registry.for_project",
        lambda root: ({"python": _fake_spec()}, []),
    )

    async def run():
        return await _lsp_rename(None, str(tmp_path), _Case(), None, _item(), "foo", True)

    result = asyncio_run(run())
    assert result["status"] == "dry_run"
    assert result["dose"]["executor"] == "lsp_rename"
    assert "+def foo():" in result["diff"]
    # 干跑不落盘
    assert "Foo" in target.read_text(encoding="utf-8")


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
