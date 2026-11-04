"""manifest 契约。

这一层的价值全在「拒收得准」：装错一台仪器的代价不是它不工作，
而是它在报告里冒充一台内置尺子，之后所有溯源都跟着错。
所以测试重点不在「合法能过」，而在各种非法都要被挡下，且理由说得清。
"""

import yaml

from code_hospital.catalog import manifest as mf

GOOD = """
api: code-hospital/v1
kind: instrument
id: acme-pylint
name: Acme Pylint 桥
vendor: acme
version: "0.3.1"
concern: Python 变量使用与可维护性
extensions: [py]
triage: [outpatient, checkup]
specialty_id: python
transport: mcp
command: acme-lint
tool: lint
args: ["--quiet"]
risk:
  reads_repo: true
  writes_repo: false
"""


def _good() -> dict:
    return yaml.safe_load(GOOD)


def test_core_instruments_all_survive_the_manifest_shape():
    """内置全部要能转成 manifest 并过校验，否则「迁到 manifest 形态」是空话。"""
    core = mf.core_manifests()
    assert len(core) >= 13
    for ident, doc in core.items():
        again, problems = mf.validate(doc)
        assert problems == [], (ident, problems)
        assert again["id"] == ident
        assert again["tier"] == "core"
        assert again["origin"] == "core"


def test_transport_is_inferred_from_engine_not_from_builtin_flag():
    """`builtin` 只说明随包，不说明怎么跑。混为一谈会把 ruff 当成院内尺子。"""
    core = mf.core_manifests()
    assert core["structure-ruler"]["transport"] == "builtin"
    assert core["java-pack"]["transport"] == "semgrep"
    assert core["ruff"]["transport"] == "external"
    assert core["gitleaks"]["transport"] == "external"


def test_good_manifest_passes_and_starts_at_probation():
    doc, problems = mf.validate(_good(), known={"ruff"})
    assert problems == []
    assert doc["tier"] == "probation"
    assert doc["origin"] == "market"
    assert doc["transport"] == "mcp"
    assert doc["risk"]["reads_repo"] is True
    assert doc["args"] == ["--quiet"]


def test_wrong_api_is_rejected():
    raw = {**_good(), "api": "code-hospital/v2"}
    _doc, problems = mf.validate(raw)
    assert any("api" in item for item in problems)


def test_reusing_a_core_id_is_rejected():
    """第三方顶替内置，等于让两份结论共用一个名字，溯源就没法再信。"""
    raw = {**_good(), "id": "ruff"}
    _doc, problems = mf.validate(raw, known={"ruff"})
    assert any("已被占用" in item for item in problems)


def test_command_with_a_path_is_rejected():
    raw = {**_good(), "command": "tools/acme-lint"}
    _doc, problems = mf.validate(raw)
    assert any("不能带路径" in item for item in problems)


def test_bad_tool_name_is_rejected():
    raw = {**_good(), "tool": "../../etc/passwd"}
    _doc, problems = mf.validate(raw)
    assert any("工具名" in item for item in problems)


def test_shell_metacharacters_in_args_are_rejected():
    raw = {**_good(), "args": ["--out", "x;rm -rf /"]}
    _doc, problems = mf.validate(raw)
    assert any("args" in item for item in problems)


def test_third_party_core_claim_is_downgraded_not_rejected():
    """自称 core 不拒收，但压回 probation。core 只属于随包仪器。"""
    raw = {**_good(), "tier": "core"}
    doc, problems = mf.validate(raw)
    assert problems == []
    assert doc["tier"] == "probation"


def test_third_party_cannot_claim_builtin_transport():
    """builtin 意味着医院自己驱动一个它不认识的引擎。"""
    raw = {**_good(), "transport": "builtin", "engine": "whatever"}
    _doc, problems = mf.validate(raw)
    assert any("builtin" in item for item in problems)


def test_substrate_must_not_declare_how_to_scan():
    """基座供能力，不产发现；给它配命令说明写错了区。"""
    raw = {**_good(), "kind": "substrate"}
    doc, problems = mf.validate(raw)
    assert doc["kind"] == "substrate"
    assert any("substrate" in item for item in problems)


def test_risk_flags_must_be_boolean():
    raw = {**_good(), "risk": {"writes_repo": "yes"}}
    _doc, problems = mf.validate(raw)
    assert any("risk.writes_repo" in item for item in problems)


def test_unknown_risk_key_is_reported_but_not_fatal():
    """契约只增不改：不认识的键照样可用，但要说出来，别以为生效了。"""
    raw = {**_good(), "risk": {"reads_repo": True, "phones_home": True}}
    doc, problems = mf.validate(raw)
    assert any("不认识的键" in item for item in problems)
    assert doc["risk"]["reads_repo"] is True


def test_semgrep_rules_must_stay_inside_the_repo():
    raw = {**_good(), "transport": "semgrep", "rules": "../evil.yml", "command": "", "tool": ""}
    _doc, problems = mf.validate(raw)
    assert any("仓库内" in item for item in problems)


def test_acceptance_accepts_a_shorthand():
    raw = {**_good(), "acceptance": "sarif"}
    doc, problems = mf.validate(raw)
    assert problems == []
    assert doc["acceptance"]["artifact"] == "sarif"


def test_roundtrip_core_row_to_manifest_and_back():
    """往返要保住 id、怎么跑、能不能自动修——装配层只认来回之后的那份。"""
    from code_hospital.catalog.formulary import instruments

    for ident, row in instruments().items():
        doc = mf.from_instrument_row(row)
        back = mf.to_row(doc)
        assert back["id"] == ident
        assert back["engine"] == row["engine"]
        assert back["can_autofix"] == bool(row.get("can_autofix"))
        assert back["requires_contract"] == bool(row.get("requires_contract"))
        assert back["transport"] in mf.TRANSPORTS
