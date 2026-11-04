"""验收命令探测：只建议「在这台仓库上跑得起来」的命令。

真实仓库跑一轮时暴露的缺陷：纯 Java 仓库（calcite-avatica，0 个 .py）被建议
`ruff check`。本机装了 ruff 就出建议，没看仓库里有没有 Python——
它会扫 0 个文件、退出码 0，然后被写进验收白名单，之后每次疗效都记成通过。
"""

from __future__ import annotations

from pathlib import Path

from code_hospital.departments import scanner
from code_hospital.safety.repo_files import has_python_files


def _ruff_present(monkeypatch) -> None:
    """让「本机装了 ruff」恒为真，把变量收敛到仓库内容这一侧。"""
    monkeypatch.setattr(scanner.shutil, "which", lambda name: "/usr/bin/ruff")


def _lint(suggestions: list[dict]) -> list[dict]:
    return [s for s in suggestions if s["alias"] == "lint"]


def test_pure_java_repo_gets_no_lint_command(tmp_path, monkeypatch):
    """机器上装了 ruff，仓库里一行 Python 都没有——不能建议 ruff。"""
    _ruff_present(monkeypatch)
    src = tmp_path / "core" / "src" / "main" / "java"
    src.mkdir(parents=True)
    (src / "Main.java").write_text("class Main {}\n", encoding="utf-8")

    assert _lint(scanner.detect_acceptance_commands(str(tmp_path))) == []


def test_python_repo_still_gets_the_lint_command(tmp_path, monkeypatch):
    """有 Python 文件时，行为和以前一样——修复不该把正常的也一起关掉。"""
    _ruff_present(monkeypatch)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    got = _lint(scanner.detect_acceptance_commands(str(tmp_path)))
    assert [s["command"] for s in got] == ["ruff check"]


def test_python_inside_ignored_dirs_does_not_count(tmp_path, monkeypatch):
    """虚拟环境里的 .py 不是这个仓库的代码，不能拿它证明该跑 ruff。"""
    _ruff_present(monkeypatch)
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "site.py").write_text("x = 1\n", encoding="utf-8")

    assert _lint(scanner.detect_acceptance_commands(str(tmp_path))) == []


def test_scan_ignore_excludes_the_only_python_file(tmp_path, monkeypatch):
    """客户自己把 Python 目录排除了，就不该再建议一条只在那个目录上有效的命令。"""
    _ruff_present(monkeypatch)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "gen.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "code-hospital.yaml").write_text(
        "scan_ignore:\n  - tools/**\n", encoding="utf-8",
    )

    assert _lint(scanner.detect_acceptance_commands(str(tmp_path))) == []


def test_missing_ruff_binary_still_hides_the_command(tmp_path, monkeypatch):
    """原有闸门不能被新闸门顶掉：本机没 ruff，有 Python 文件也不建议。"""
    monkeypatch.setattr(scanner.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    assert _lint(scanner.detect_acceptance_commands(str(tmp_path))) == []


def test_has_python_files_walks_nested_directories(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "mod.pyi").write_text("x: int\n", encoding="utf-8")

    assert has_python_files(str(tmp_path)) is True


def test_has_python_files_on_empty_repo(tmp_path):
    assert has_python_files(str(tmp_path)) is False
