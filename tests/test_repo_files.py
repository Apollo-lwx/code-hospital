"""忽略三层测试：.gitignore / 内置排除目录 / code-hospital.yaml scan_ignore。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital.safety.repo_files import (
    is_denied,
    load_scan_ignores,
    scope_file_list,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


@pytest.fixture()
def repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "b.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("gen/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


def test_is_denied_builtin_and_custom():
    assert is_denied("node_modules/pkg/index.js")
    assert is_denied("src/__pycache__/a.py")
    assert is_denied(".hospital/x.md")
    assert not is_denied("src/a.py")
    assert is_denied("gen/pb/a_pb2.py", ["gen/**", "*_pb2.py"])
    assert not is_denied("src/a.py", ["gen/**"])


async def test_repo_scope_respects_gitignore(repo):
    files, skipped = await scope_file_list(repo, "repo", {})
    assert "src/a.py" in files
    assert not any(f.startswith("gen/") for f in files)


async def test_custom_scan_ignore(repo):
    (Path(repo) / "code-hospital.yaml").write_text(
        'scan_ignore:\n  - "gen/**"\n  - "src/a.py"\n', encoding="utf-8"
    )
    assert load_scan_ignores(repo) == ["gen/**", "src/a.py"]
    # staged scope 同样生效（src/a.py 已暂存但被自定义规则跳过）
    files, skipped = await scope_file_list(repo, "staged", {})
    assert "src/a.py" not in files
    assert skipped >= 1


async def test_invalid_config_tolerated(repo):
    (Path(repo) / "code-hospital.yaml").write_text("::: 不是yaml", encoding="utf-8")
    assert load_scan_ignores(repo) == []
    files, _ = await scope_file_list(repo, "repo", {})
    assert "src/a.py" in files
