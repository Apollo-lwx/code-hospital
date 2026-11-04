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


def test_kind_of_separates_code_config_and_doc():
    from code_hospital.safety.repo_files import kind_of

    assert kind_of("src/main/java/App.java") == "code"
    assert kind_of("AGENTS.md") == "doc"
    assert kind_of("docs/guides/x.markdown") == "doc"
    assert kind_of("package.json") == "config"
    # 读不到内容时才退回文件名兜底
    assert kind_of("pom.xml") == "config"
    assert kind_of("chat2db-community-server/pom.xml") == "config"


def test_xml_is_classified_by_content_not_name(tmp_path: Path):
    """装 SQL 的 XML 是代码，构建声明是配置；判不出来按代码处理，不假设无害。"""
    from code_hospital.safety.repo_files import classify

    (tmp_path / "res").mkdir()
    (tmp_path / "res" / "UserMapper.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "x.dtd">\n'
        '<mapper namespace="x.UserMapper"><select id="a">select 1</select></mapper>',
        encoding="utf-8",
    )
    (tmp_path / "res" / "query.xml").write_text(
        "<mapper namespace='q'><update id='b'>update t set a=1</update></mapper>", encoding="utf-8"
    )
    (tmp_path / "res" / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion></project>", encoding="utf-8"
    )
    (tmp_path / "res" / "logback.xml").write_text(
        "<configuration><appender name='x'/></configuration>", encoding="utf-8"
    )

    root = str(tmp_path)
    assert classify(root, "res/UserMapper.xml") == ("code", "content:sql")
    # 名字里没有 mapper，但内容是 SQL —— 只看文件名就会漏掉
    assert classify(root, "res/query.xml")[0] == "code"
    assert classify(root, "res/pom.xml")[0] == "config"
    # 判不出结构：既不是构建声明也不是 SQL，只能靠读——不报「缺仪器」
    assert classify(root, "res/logback.xml") == ("markup", "content:unclear")


def test_build_plugin_mentioning_sql_is_not_a_mapper(tmp_path: Path):
    """Chat2DB 实测教训：`<sql` 这种宽标记会把 `<sql-maven-plugin>` 误判成 SQL 映射。"""
    from code_hospital.safety.repo_files import classify

    (tmp_path / "pom.xml").write_text(
        "<project><build><plugins><plugin>"
        "<artifactId>sql-maven-plugin</artifactId></plugin></plugins></build></project>",
        encoding="utf-8",
    )
    assert classify(str(tmp_path), "pom.xml")[0] == "config"


def test_mapper_xml_path_fallback_without_root():
    """没有 project_root 读不到内容时，路径兜底只作兜底。"""
    from code_hospital.safety.repo_files import kind_of

    assert kind_of("src/main/resources/mapper/UserMapper.xml") == "code"
    assert kind_of("src/main/resources/sqlmap/user.xml") == "code"


async def test_docs_enter_reading_scope_and_gap_ledger(repo):
    """文档进阅读范围；没人读就是未查，要记账，但不许记成「缺仪器」。"""
    (Path(repo) / "AGENTS.md").write_text("# 规范\n", encoding="utf-8")
    (Path(repo) / "pom.xml").write_text("<project/>", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    files, _ = await scope_file_list(repo, "repo", {})
    assert "AGENTS.md" in files

    from code_hospital.catalog import ledger

    coverage: dict = {}
    notes = ledger.record_scope_gaps(
        coverage, ["AGENTS.md", "pom.xml", "src/a.py", "scripts/deploy.rb"],
        [{"tool": "ruff", "available": True, "outcome": "ok", "extensions": ["py"]}],
        "c1", repo,
    )
    kinds = {row["kind"]: row for row in coverage["capability_gap"]}
    # rb 没有仪器覆盖：仪器缺口，可以靠招人关掉
    assert kinds["missing_instrument"]["evidence"]["extensions"] == ["rb"]
    # md 没人读：阅读缺口，只能靠读关掉，不许拿它去招 Markdown 检查器
    assert kinds["unread_by_model"]["evidence"]["extensions"] == ["md"]
    assert kinds["unread_by_model"]["evidence"]["closure"] == "host_llm"
    assert kinds["unread_by_model"]["occurrences"] == 1
    # pom.xml 是构建配置，既不报仪器缺口也不报阅读缺口
    assert "xml" not in kinds["missing_instrument"]["evidence"]["extensions"]
    assert any("文档" in note for note in notes)


async def test_read_docs_close_the_gap(repo):
    """模型读过之后，文档缺口应当消失——它的关闭条件是读，不是装工具。"""
    (Path(repo) / "AGENTS.md").write_text("# 规范\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)

    from code_hospital.catalog import ledger

    coverage: dict = {}
    notes = ledger.record_scope_gaps(
        coverage, ["AGENTS.md"], [], "c1", repo, read={"AGENTS.md"},
    )
    assert notes == []
    assert coverage["capability_gap"] == []
