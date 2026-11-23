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


def test_repo_config_extends_and_overrides(tmp_path: Path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  lsp:\n    languages:\n"
        "      - id: go\n"
        "        extensions: [.go]\n"
        "        command: gopls\n"
        "        args: [serve]\n"
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
    # 仓级按 id 覆盖内置
    assert specs["python"].command == "basedpyright-langserver"
    assert specs["python"].startup_timeout_s == 90.0


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
