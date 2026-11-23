"""规则处置矩阵：规范/规则分流、用户配置覆盖内置默认、放宽告知、等级联动风险。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital import adapters
from code_hospital.adapters import RawFinding, ScannerAdapter
from code_hospital.catalog.dispositions import (
    Disposition,
    parse_dispositions,
    resolve,
)
from code_hospital.catalog.policy import load_policy
from code_hospital.core.errors import HospitalError
from code_hospital.core.models import Diagnosis, Finding
from code_hospital.departments import diagnostician, prescriber, reception, scanner
from code_hospital.departments.prescriber import build_items

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


# ---------- 解析与校验 ----------


def test_parse_dispositions_keeps_good_rows_and_notes_bad_ones():
    rows, notes = parse_dispositions([
        {"match": "N8*", "class": "convention", "auto_fix": "auto", "confirm": "none"},
        {"match": "F841", "class": "rule", "severity": "critical"},
        {"match": "", "class": "rule"},
        {"match": "X1", "class": "style"},
        {"match": "X2", "auto_fix": "sometimes"},
        "not-a-dict",
    ])
    assert [row.match for row in rows] == ["N8*", "F841"]
    assert len(notes) == 4
    assert any("缺 match" in note for note in notes)
    assert any("非法值" in note for note in notes)


def test_parse_dispositions_rejects_non_list():
    rows, notes = parse_dispositions({"match": "N8*"})
    assert rows == ()
    assert notes


def test_resolve_user_beats_default_and_longest_match_wins():
    user = (Disposition("N802", auto_fix="auto", confirm="none"),)
    hit = resolve("N802", user)
    assert hit is not None and hit.match == "N802"
    assert hit.resolved_auto_fix == "auto"
    assert resolve("N803", user).match == "N8*"
    assert resolve("F841", user) is None
    longer = (Disposition("N8*", cls="convention"), Disposition("N80*", auto_fix="auto"))
    assert resolve("N802", longer).match == "N80*"


def test_default_table_only_covers_conventions():
    """内置默认只收无争议的规范类；缺陷类规则未命中，维持现状。"""
    assert resolve("N801").resolved_class == "convention"
    assert resolve("N801").advisory is True
    assert resolve("naming-convention").advisory is True
    assert resolve("F821") is None
    assert resolve("F841") is None


def test_policy_loads_dispositions_with_notes(tmp_path: Path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  rules:\n    dispositions:\n"
        "      - {match: 'F841', class: rule, severity: critical}\n"
        "      - {match: 'BAD', class: nope}\n",
        encoding="utf-8",
    )
    policy = load_policy(str(tmp_path))
    assert [row.match for row in policy.dispositions] == ["F841"]
    assert any("非法值" in note for note in policy.notes)


# ---------- 开方落点（build_items 级）----------


class _AbsentPool:
    """LSP 缺席的确定性桩（同 test_rename.py 的约定）。"""

    def cross_file_refs(self, file_path, line, symbol):
        return None

    def notes(self):
        return []

    def close(self):
        pass


def _finding(rule_id: str, file_path: str, line: int, symbol: str) -> Finding:
    return Finding(
        id=f"fnd-{rule_id}-{line}-{symbol}", case_id="case-x", report_id="rpt-x",
        tool="ruff", rule_id=rule_id, severity="minor",
        file_path=file_path, line_start=line, line_end=line,
        symbol=symbol, message=f"{rule_id}: {symbol}", fingerprint=f"fp-{rule_id}-{symbol}",
    )


def _diagnosis(members: list[Finding], fixability: str = "llm_patch") -> Diagnosis:
    return Diagnosis(
        id=f"dia-{members[0].rule_id}-{members[0].line_start}", case_id="case-x", report_id="rpt-x",
        fingerprint=members[0].fingerprint, category=members[0].rule_id,
        root_cause="", severity="minor", confidence=0.8,
        fixability=fixability, member_ids=[m.id for m in members],
        specialty_id="python", formulary_matched=True,
    )


def _build(tmp_path: Path, rule_id: str, symbol: str, yaml_text: str = "",
           fixability: str = "llm_patch") -> list[dict]:
    if yaml_text:
        (tmp_path / "code-hospital.yaml").write_text(yaml_text, encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.py").write_text(
        f"def {symbol or 'f'}():\n    x = 1\n    return 2\n", encoding="utf-8",
    )
    finding = _finding(rule_id, "src/a.py", 1, symbol)
    return build_items(
        [_diagnosis([finding], fixability)], {finding.id: finding}, "rx-t",
        include_low=False, project_root=str(tmp_path),
        drops_out=[], rename_pool=_AbsentPool(),
    )


def test_default_table_drives_rename_advisory(tmp_path: Path):
    """不配任何东西：N802 走内置默认表，正骨全组告知建议（含对照药）。"""
    items = _build(tmp_path, "N802", "Foo")
    statuses = {item["formula_id"]: item["status"] for item in items}
    assert statuses == {"rename": "advisory", "contrast": "advisory"}


def test_user_can_relax_convention_to_auto_with_risk_note(tmp_path: Path):
    """用户把规范类放宽为 auto：正骨项落 pending 随处方执行，风险提前告知。"""
    items = _build(
        tmp_path, "N802", "Foo",
        "catalog:\n  rules:\n    dispositions:\n"
        "      - {match: 'N8*', class: convention, auto_fix: auto, confirm: none}\n",
    )
    statuses = {item["formula_id"]: item["status"] for item in items}
    assert statuses == {"rename": "pending", "contrast": "pending"}
    rename_item = next(item for item in items if item["formula_id"] == "rename")
    assert "规范类规则" in rename_item["instructions"]
    assert "API 兼容性" in rename_item["instructions"]


def test_user_severity_override_beats_rename_risk_cap(tmp_path: Path):
    """用户把 N802 拉成 critical：风险联动 high、压过正骨上限，强制真人审批。"""
    items = _build(
        tmp_path, "N802", "Foo",
        "catalog:\n  rules:\n    dispositions:\n"
        "      - {match: 'N802', severity: critical}\n",
    )
    rename_item = next(item for item in items if item["formula_id"] == "rename")
    assert rename_item["risk"] == "high"
    # 只配了等级没配 auto_fix：class 未知按 rule 默认 confirm，落 pending 走强批闸
    assert rename_item["status"] == "pending"


def test_convention_classification_applies_beyond_rename(tmp_path: Path):
    """处置矩阵不只管正骨：F841 被用户标成规范类后同样只告知。"""
    items = _build(
        tmp_path, "F841", "", fixability="autofix",
        yaml_text=(
            "catalog:\n  rules:\n    dispositions:\n"
            "      - {match: 'F841', class: convention}\n"
        ),
    )
    assert items[0]["formula_id"] == "debride"
    assert items[0]["status"] == "advisory"


def test_unmatched_rule_keeps_current_behavior(tmp_path: Path):
    """未命中任何处置的规则维持现状：pending、风险由剂型决定。"""
    items = _build(tmp_path, "F841", "", fixability="autofix")
    assert items[0]["status"] == "pending"
    assert items[0]["risk"] == "low"


# ---------- 流程级：等级拉高强制真人 ----------


class RuffAdapter(ScannerAdapter):
    name = "ruff"
    binary = "git"
    extensions = frozenset({".py"})

    async def scan(self, repo_root, files, timeout_ms):
        return [RawFinding(
            rule_id="F841", message="unused", file_path="src/a.py",
            line_start=2, line_end=2, severity="major",
        )]


@pytest.fixture()
def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    x = 1\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


async def test_severity_override_forces_user_approval(git_repo):
    """明确必须修的规则（severity=critical）拉进 user_required：宿主代理不能自批。"""
    (Path(git_repo) / "code-hospital.yaml").write_text(
        "catalog:\n  rules:\n    dispositions:\n"
        "      - {match: 'F841', class: rule, severity: critical}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    adapters.clear()
    adapters.register(RuffAdapter())
    reg = await reception.register(
        title="必须修", scope_type="staged", scope={},
        auto_advance=True, project_root=git_repo,
    )
    await scanner.scan(case_id=reg["case_id"], project_root=git_repo)
    await diagnostician.diagnose(case_id=reg["case_id"], project_root=git_repo)
    draft = await prescriber.prescribe(
        case_id=reg["case_id"], acceptance_decision="skip", project_root=git_repo,
    )
    assert draft["status"] == "draft"
    assert draft["risk_level"] == "high"
    assert draft["approval_policy"]["user_required_items"]
    with pytest.raises(HospitalError) as exc:
        await prescriber.prescribe(
            case_id=reg["case_id"], approve=True, approver="host_agent",
            project_root=git_repo,
        )
    assert exc.value.code == "Forbidden"
    approved = await prescriber.prescribe(
        case_id=reg["case_id"], approve=True, approver="user", user_confirmed=True,
        project_root=git_repo,
    )
    assert approved["status"] == "approved"
    assert approved["human_in_loop"] is True
