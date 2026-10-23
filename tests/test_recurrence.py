"""初诊调档轻量版：二次入院时同指纹发现标复发，不从头来。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.departments import reception, scanner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


class RecurringAdapter(ScannerAdapter):
    name = "recurring"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [
            RawFinding(
                rule_id="REC001", message="recurring issue",
                file_path="src/a.py", line_start=2, line_end=2, severity="major",
            )
        ]


@pytest.fixture(autouse=True)
def recurring_adapters():
    adapters.clear()
    adapters.register(RecurringAdapter())
    yield
    adapters.clear()


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    x = 1\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


async def test_second_admission_marks_recurrence(git_repo):
    # 首次入院：全部初发（auto_advance 直通，聚焦复发语义而非门禁）
    reg1 = await reception.register(
        title="首次", scope_type="staged", scope={},
        auto_advance=True, project_root=git_repo
    )
    res1 = await scanner.scan(case_id=reg1["case_id"], project_root=git_repo)
    assert res1["findings"]["data"][0]["history_refs"] == []

    # 二次入院（强制转诊旧病例）：同指纹发现必须标复发并指向首次病例
    reg2 = await reception.register(
        title="二次入院", scope_type="staged", scope={}, force=True,
        auto_advance=True, project_root=git_repo
    )
    res2 = await scanner.scan(case_id=reg2["case_id"], project_root=git_repo)
    refs = res2["findings"]["data"][0]["history_refs"]
    assert len(refs) == 1
    assert refs[0]["case_id"] == reg1["case_id"]
    assert refs[0]["outcome"] == "referred"  # 被强制转诊的旧病例
    assert "复发" in refs[0]["note"]

    # 检验报告含「病史」列与复发标记
    report = Path(res2["report_path"]).read_text(encoding="utf-8")
    assert "| 摘要 | 病史 |" in report
    assert "复发×1" in report
    # 覆盖与限制含复发提醒
    assert "复发提醒" in report
