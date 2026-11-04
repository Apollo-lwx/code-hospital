"""文件引用链：非代码文件有没有人指向它。

以前非 Python 文件一进范围就写「这条链未建」——那不是检查结果，
是把没查写成没有。现在真去查：按约定生效的不算孤儿，其余看有没有
别的文件在正文里提到它的文件名。
"""

from __future__ import annotations

from code_hospital.catalog.refs import check, note


def _repo(tmp_path, files: dict[str, str]) -> str:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return str(tmp_path)


def _keys(files: dict[str, str]) -> list[str]:
    return list(files)


def test_file_mentioned_by_another_file_is_not_an_orphan(tmp_path):
    files = {"docs/guide.md": "# guide\n", "README.md": "见 [指南](docs/guide.md)\n"}
    root = _repo(tmp_path, files)

    got = check(root, _keys(files))

    assert got["orphan_count"] == 0
    assert note(got) == ""


def test_file_nobody_mentions_is_reported(tmp_path):
    files = {"docs/leftover.md": "# 没人要了\n", "README.md": "# 项目\n"}
    root = _repo(tmp_path, files)

    got = check(root, _keys(files))

    assert got["orphans"] == ["docs/leftover.md"]
    assert "1 个非代码文件" in note(got)


def test_self_reference_does_not_count(tmp_path):
    """文件里提到自己的名字，不算「有人引用它」。"""
    files = {"assembly/assembly.xml": "<id>assembly</id>\n"}
    root = _repo(tmp_path, files)

    got = check(root, _keys(files))

    assert got["orphans"] == ["assembly/assembly.xml"]


def test_dotfiles_are_read_by_name_not_by_reference(tmp_path):
    files = {".stylelintrc.json": "{}\n"}
    root = _repo(tmp_path, files)

    assert check(root, _keys(files))["orphan_count"] == 0


def test_github_directory_works_without_being_mentioned(tmp_path):
    files = {".github/workflows/ci.yml": "on: push\n"}
    root = _repo(tmp_path, files)

    assert check(root, _keys(files))["orphan_count"] == 0


def test_classpath_resources_load_by_name(tmp_path):
    files = {"src/main/resources/log4j2-test.xml": "<Configuration/>\n"}
    root = _repo(tmp_path, files)

    assert check(root, _keys(files))["orphan_count"] == 0


def test_files_under_a_site_root_follow_the_generator(tmp_path):
    """Jekyll / MkDocs 这类的文档由目录结构渲染，不需要逐个被链接。"""
    files = {
        "site/_config.yml": "title: x\n",
        "site/_docs/api.md": "# api\n",
        "site/_data/docs.yml": "- api\n",
    }
    root = _repo(tmp_path, files)

    assert check(root, _keys(files))["orphan_count"] == 0


def test_nested_readme_is_package_metadata(tmp_path):
    files = {"packages/core/readme.md": "# core\n"}
    root = _repo(tmp_path, files)

    assert check(root, _keys(files))["orphan_count"] == 0


def test_code_files_are_not_part_of_this_ledger(tmp_path):
    """代码之间的链归调用图，这里不重复算。"""
    files = {"app.py": "x = 1\n"}
    root = _repo(tmp_path, files)

    got = check(root, _keys(files))

    assert got["candidates"] == 0
    assert note(got) == ""


def test_missing_project_root_is_not_checked(tmp_path):
    assert check("", ["docs/a.md"]) == {}


def test_note_stays_quiet_when_no_orphans(tmp_path):
    files = {"docs/a.md": "# a\n", "README.md": "docs/a.md\n"}
    root = _repo(tmp_path, files)

    assert note(check(root, _keys(files))) == ""


def test_examples_are_capped_but_the_count_is_not(tmp_path):
    files = {"README.md": "# 项目\n"}
    for i in range(9):
        files[f"docs/orphan{i}.md"] = f"# {i}\n"
    root = _repo(tmp_path, files)

    got = check(root, _keys(files))

    assert got["orphan_count"] == 9
    assert len(got["orphans"]) == 5
    assert "9 个非代码文件" in note(got)
