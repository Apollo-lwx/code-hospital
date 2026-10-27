"""被诊疗仓库的外援名册。"""

from code_hospital.catalog.checks import load_checks
from code_hospital.catalog.formulary import books_for, match_finding, route, selected_instrument_ids
from code_hospital.catalog.outside import overlay
from code_hospital.catalog.visits import normalize_visit_kind, panel


def _write(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "team.yml").write_text("rules: []\n", encoding="utf-8")
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n"
        "  formulas:\n"
        "    - id: team-note\n"
        "      name: 补记\n"
        "      method: llm_patch\n"
        "      indications:\n"
        "        - {kind: rule, rule_ids: [PAY001]}\n"
        "    - id: team-fix\n"
        "      name: 自动\n"
        "      method: autofix\n"
        "      indications:\n"
        "        - {kind: rule, rule_ids: [PAY001]}\n"
        "  specialties:\n"
        "    - id: payments\n"
        "      name: 支付科\n"
        "      formulary: [team-note, guard]\n"
        "  routes:\n"
        "    - {tool: ruff, rule: PAY001, specialty: payments}\n"
        "    - {tool: gitleaks, prefix: x, specialty: payments}\n"
        "  visits:\n"
        "    - id: pay-review\n"
        "      label: 支付复查\n"
        "      mode: only\n"
        "      instruments: [ruff, team-semgrep, team-tool, missing-ruler]\n"
        "      packs: [access]\n"
        "  instruments:\n"
        "    - id: team-semgrep\n"
        "      concern: 小组规则\n"
        "      engine: semgrep\n"
        "      rules: rules/team.yml\n"
        "      extensions: [py]\n"
        "    - id: team-tool\n"
        "      concern: 到岗\n"
        "      engine: external\n"
        "      binary: definitely-missing-tool\n"
        "    - id: team-shell\n"
        "      concern: 越界\n"
        "      engine: semgrep\n"
        "      rules: ../outside.yml\n"
        "  diagnose:\n"
        "    items:\n"
        "      - id: pay.why\n"
        "        question: 金额为什么在这里改\n"
        "        artifact: 指到函数\n"
        "  discharge:\n"
        "    items:\n"
        "      - id: pay.ship\n"
        "        question: 还有没有未对上的金额\n"
        "        artifact: 一句结论\n",
        encoding="utf-8",
    )


def test_user_visit_doctor_drug_and_instrument(tmp_path):
    _write(tmp_path)
    root = str(tmp_path)
    extra = overlay(root)
    assert normalize_visit_kind("pay-review", root) == "pay-review"
    mode, instruments = panel("pay-review", root)
    assert mode == "only"
    assert instruments == ("ruff", "team-semgrep", "team-tool")
    assert "team-fix" not in extra["formulas"]
    assert "team-shell" not in extra["instruments"]
    assert any("仓库" in note for note in extra["notes"])
    assert any("autofix" in note or "剂型" in note for note in extra["notes"])
    matched = match_finding("ruff", "PAY001", "minor", project_root=root)
    assert matched.formula is not None
    assert matched.formula.id == "team-note"
    assert matched.formula.method == "llm_patch"
    assert matched.specialty_id == "payments"
    assert route("gitleaks", "x1", "major", root) == "security"
    chosen = selected_instrument_ids("checkup", ["a.py"], [], [], "pay-review", root)
    assert chosen == ["ruff", "team-semgrep", "team-tool"]
    assert books_for(root)[2]["team-tool"]["can_autofix"] is False
    question = extra["diagnose"][0]
    assert question["id"] == "pay.why"
    assert question["status"] == "open"
    assert question["executors"][0]["status"] == "pending"
    assert extra["discharge"][0]["id"] == "pay.ship"
    rows, _notes = load_checks(root, visit_kind="pay-review", files=["a.py"])
    assert "access.authz" in [row["id"] for row in rows]


def test_checkup_does_not_take_user_visit_packs(tmp_path):
    _write(tmp_path)
    rows, _notes = load_checks(str(tmp_path), visit_kind="checkup", files=["a.py"])
    ids = [row["id"] for row in rows]
    assert "access.authz" not in ids
    assert "team-note" not in ids
