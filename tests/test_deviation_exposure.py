"""纪律账在报告里说到多细，由 `discipline.exposure` 定。

粗细是客户的注意力分配：同一份账，安全等级高的仓库要多看细节，
低的不想把报告读成流程日志。但最粗只能到一句话——账出现了就说明
宿主没按约定交卷，报告里一个字不提，等于医院替它瞒了下来。
"""

from __future__ import annotations

from code_hospital.catalog.ledger import deviation_lines, exposure_for

COVERAGE = {
    "discipline": [
        {"kind": "gate_bypass", "stage": "scan", "count": 4, "case_id": "case_A", "round": 2,
         "detail": "宿主传 stage_confirmed=true 但没带回执"},
        {"kind": "unfounded_claim", "stage": "diagnose", "count": 2, "case_id": "case_A",
         "round": 2, "detail": "第 3 条结论引文与文件不符"},
        {"kind": "filing_invalid", "stage": "diagnose", "count": 1, "case_id": "case_A", "round": 1},
        {"kind": "silent_degrade", "stage": "scan", "count": 1, "case_id": "case_A", "round": 1},
    ]
}


def _write_config(root, exposure: str) -> None:
    (root / "code-hospital.yaml").write_text(
        f"discipline:\n  exposure: {exposure}\n", encoding="utf-8",
    )


def test_default_is_typed_when_nothing_is_configured(tmp_path):
    lines = deviation_lines(COVERAGE, project_root=str(tmp_path))

    assert lines[0] == "执行偏差：阶段放行只靠参数，在 scan，4 次。"
    assert any("另有 1 类执行偏差" in ln for ln in lines)


def test_summary_collapses_to_one_number(tmp_path):
    _write_config(tmp_path, "summary")
    lines = deviation_lines(COVERAGE, project_root=str(tmp_path))

    assert len(lines) == 1
    assert "本次记到 8 处" in lines[0]


def test_located_adds_case_and_round_but_not_the_detail(tmp_path):
    _write_config(tmp_path, "located")
    lines = deviation_lines(COVERAGE, project_root=str(tmp_path))

    assert "病例 case_A" in lines[0]
    assert "第 2 轮" in lines[0]
    assert "没带回执" not in lines[0]


def test_full_adds_the_detail_text(tmp_path):
    _write_config(tmp_path, "full")
    lines = deviation_lines(COVERAGE, project_root=str(tmp_path))

    assert any("没带回执" in ln for ln in lines)


def test_full_keeps_more_than_the_three_line_default(tmp_path):
    many = {"discipline": [
        {"kind": "gate_bypass", "stage": f"s{i}", "count": 1, "detail": "x"} for i in range(9)
    ]}
    _write_config(tmp_path, "full")

    assert len(deviation_lines(many, project_root=str(tmp_path))) == 9


def test_full_still_caps_at_thirty_lines():
    many = {"discipline": [
        {"kind": "gate_bypass", "stage": f"s{i}", "count": 1} for i in range(40)
    ]}
    lines = deviation_lines(many, exposure="full")

    assert len(lines) == 31  # 30 条 + 一句「另有」
    assert "另有 10 类" in lines[-1]


def test_unknown_value_falls_back_but_says_so(tmp_path):
    """写错了不阻止看病，但不能悄悄按默认出——配置没生效要让人看得见。"""
    _write_config(tmp_path, "verbose")

    lines = deviation_lines(COVERAGE, project_root=str(tmp_path))

    assert "不可用" in lines[0]
    assert "verbose" in lines[0]
    assert lines[1].startswith("执行偏差：")


def test_the_floor_is_one_sentence_never_silence(tmp_path):
    """没有 off 这一档：账出现了就必须在报告里留下痕迹。"""
    _write_config(tmp_path, "off")

    lines = deviation_lines(COVERAGE, project_root=str(tmp_path))

    assert lines, "关不掉：最粗也只能到一句话"
    assert "不可用" in lines[0]
    assert "off" in lines[0]


def test_no_deviation_no_line_at_any_level(tmp_path):
    for level in ("summary", "typed", "located", "full"):
        assert deviation_lines({"discipline": []}, exposure=level) == []


def test_exposure_for_reports_the_value_and_no_warning(tmp_path):
    _write_config(tmp_path, "located")

    assert exposure_for(str(tmp_path)) == ("located", "")


def test_exposure_for_without_project_root_uses_default():
    assert exposure_for("") == ("typed", "")
