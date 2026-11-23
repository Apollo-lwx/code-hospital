"""正骨（rename）：G1–G4 入名册、跨文件守门、批量迁移合并、touches_public_api 升档、命名约定豁免。"""

from pathlib import Path

from code_hospital.catalog import formulary
from code_hospital.catalog.formulary import match_finding
from code_hospital.catalog.rename import (
    RENAME_BATCH_MIN,
    cross_file_reference,
    touches_public_api,
)
from code_hospital.core.models import Diagnosis, Finding
from code_hospital.departments.prescriber import build_items


class _AbsentPool:
    """LSP 缺席的确定性桩：守门一律回退文本扫描，不产台账行（等价于没装语言服务）。

    本文件的守门测试不该依赖本机有没有装 pyright——一律显式缺席。
    LSP 在岗时的行为见文件末尾「LSP 守门销账」一节与 tests/test_lsp.py。
    """

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


def _diagnosis(members: list[Finding]) -> Diagnosis:
    return Diagnosis(
        id=f"dia-{members[0].symbol}-{members[0].line_start}", case_id="case-x", report_id="rpt-x",
        fingerprint=members[0].fingerprint, category=members[0].rule_id,
        root_cause="", severity="minor", confidence=0.8,
        fixability="llm_patch", member_ids=[m.id for m in members],
        specialty_id="python", formulary_matched=True,
    )


# ---------- 名册 ----------


def test_rename_rules_enumerated_and_active():
    formula = formulary.formulas()["rename"]
    names = {row for ind in formula.indications for row in (ind.get("rule_ids") or [])}
    assert {"N801", "N802", "N803", "N804", "N805", "N806", "N807", "N815", "N816", "N818"} <= names
    # G5 import 别名（第二批）已入册
    assert {"N811", "N812", "N813", "N814", "N817"} <= names
    # G6 模块文件名不在范围内
    assert "N999" not in names
    # phase 已降到 1：真实仓观察通过后转正，直接可匹配
    assert formula.phase == 1
    matched = match_finding("ruff", "N802", "minor")
    assert matched.formula.id == "rename"


def test_rename_matches_when_phase_open():
    matched = match_finding("ruff", "N802", "minor")
    assert matched.formula.id == "rename"
    assert matched.specialty_id == "python"
    assert matched.method == "llm_patch"


# ---------- 守门与升档的判定函数 ----------


def test_cross_file_reference_detects_from_import(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("from a import Foo\n", encoding="utf-8")
    assert cross_file_reference(str(tmp_path), "src/a.py", "Foo") is True


def test_cross_file_reference_clean_when_unreferenced(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("def other():\n    return 2\n", encoding="utf-8")
    assert cross_file_reference(str(tmp_path), "src/a.py", "Foo") is False


def test_touches_public_api_via_dunder_all(tmp_path: Path):
    (tmp_path / "a.py").write_text(
        '__all__ = ["Foo"]\n\ndef Foo():\n    return 1\n', encoding="utf-8",
    )
    assert touches_public_api(str(tmp_path), "a.py", "Foo") is True
    assert touches_public_api(str(tmp_path), "a.py", "Bar") is False


def test_touches_public_api_via_package_init(tmp_path: Path):
    (tmp_path / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert touches_public_api(str(tmp_path), "__init__.py", "VALUE") is True
    # 非 __init__ 文件里光定义不算公开 API
    (tmp_path / "b.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert touches_public_api(str(tmp_path), "b.py", "VALUE") is False


# ---------- build_items 三道闸 ----------


def _build(tmp_path: Path, members: list[Finding]):
    diagnosis = _diagnosis(members)
    by_id = {f.id: f for f in members}
    drops: list[dict] = []
    items = build_items(
        [diagnosis], by_id, "rx-x", True,
        project_root=str(tmp_path), drops_out=drops, rename_pool=_AbsentPool(),
    )
    return items, drops


def test_g1_local_names_skip_the_guard(tmp_path: Path):
    """G1 局部名字没有跨文件引用的概念，提取不到符号也照常入方。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    X = 1\n    return X\n", encoding="utf-8")
    members = [_finding("N806", "src/a.py", 2, "")]
    items, drops = _build(tmp_path, members)
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert len(rename_items) == 1
    assert rename_items[0]["method"] == "llm_patch"
    assert drops == []


def test_cross_file_reference_guards_the_item(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("from a import Foo\n", encoding="utf-8")
    members = [_finding("N802", "src/a.py", 1, "Foo")]
    items, drops = _build(tmp_path, members)
    assert [it for it in items if it["formula_id"] == "rename"] == []
    assert len(drops) == 1
    assert "LSP001" in drops[0]["reason"]


def test_single_file_rename_prescribed_when_clean(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return Foo\n", encoding="utf-8")
    members = [_finding("N802", "src/a.py", 1, "Foo")]
    items, drops = _build(tmp_path, members)
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert len(rename_items) == 1
    assert rename_items[0]["method"] == "llm_patch"
    assert rename_items[0]["risk"] == "medium"
    assert drops == []
    # 伴随药：对照（改名后跑验收）
    assert any(it["formula_id"] == "contrast" for it in items)


def test_public_api_escalates_risk(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        '__all__ = ["Foo"]\n\ndef Foo():\n    return 1\n', encoding="utf-8",
    )
    members = [_finding("N802", "src/a.py", 3, "Foo")]
    items, _drops = _build(tmp_path, members)
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert rename_items[0]["risk"] == "high"


def test_batch_merge_over_threshold(tmp_path: Path):
    """同规则同文件达到阈值 → 一味外购批量迁移，不开煎药。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    members = [
        _finding("N803", "src/a.py", line, f"arg{line}")
        for line in range(1, RENAME_BATCH_MIN + 1)
    ]
    # 批量键是簇级（同规则同文件），每处发现各是一条诊断
    diagnoses = [_diagnosis([m]) for m in members]
    by_id = {f.id: f for f in members}
    drops: list[dict] = []
    items = build_items(
        diagnoses, by_id, "rx-x", True, project_root=str(tmp_path), drops_out=drops,
        rename_pool=_AbsentPool(),
    )
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert len(rename_items) == 1
    batch = rename_items[0]
    assert batch["method"] == "manual"
    assert batch["risk"] == "high"
    assert "批量迁移" in batch["instructions"]
    assert "发现数应为 0" in batch["acceptance"]["expect"]
    assert not any(it["formula_id"] == "contrast" for it in items)


def test_below_threshold_stays_llm_patch(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f(A, B):\n    return A\n", encoding="utf-8")
    members = [_finding("N803", "src/a.py", 1, "A"), _finding("N803", "src/a.py", 2, "B")]
    diagnoses = [_diagnosis([m]) for m in members]
    by_id = {f.id: f for f in members}
    items = build_items(
        diagnoses, by_id, "rx-x", True, project_root=str(tmp_path), drops_out=[],
        rename_pool=_AbsentPool(),
    )
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert rename_items
    assert all(it["method"] == "llm_patch" for it in rename_items)


# ---------- G5 import 别名（第二批）----------


def test_g5_alias_prescribed_without_guard(tmp_path: Path):
    """别名是导入文件内的局部绑定：不做跨文件守门，直接入方。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("import os as OS\n", encoding="utf-8")
    # 另一个文件恰好有同名符号的 from-import——别名不该因此被守
    (tmp_path / "src" / "b.py").write_text("from a import OS\n", encoding="utf-8")
    members = [_finding("N812", "src/a.py", 1, "OS")]
    items, drops = _build(tmp_path, members)
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert len(rename_items) == 1
    assert rename_items[0]["method"] == "llm_patch"
    assert drops == []


def test_g5_alias_in_init_drops_out(tmp_path: Path):
    """__init__.py 里的别名是包的重导出名：入待复核，写明 LSP001。"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from collections import OrderedDict as OD\n", encoding="utf-8",
    )
    members = [_finding("N817", "pkg/__init__.py", 1, "OD")]
    items, drops = _build(tmp_path, members)
    assert [it for it in items if it["formula_id"] == "rename"] == []
    assert len(drops) == 1
    assert "重导出" in drops[0]["reason"]
    assert "LSP001" in drops[0]["reason"]


def test_g5_alias_without_symbol_drops_out(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("import os as OS\n", encoding="utf-8")
    members = [_finding("N812", "src/a.py", 1, "")]
    items, drops = _build(tmp_path, members)
    assert [it for it in items if it["formula_id"] == "rename"] == []
    assert len(drops) == 1
    assert "提取不到别名" in drops[0]["reason"]


def test_g5_alias_public_api_escalates(tmp_path: Path):
    """非 __init__ 模块的 __all__ 收了别名（* 号导出），风险照升 high。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        '__all__ = ["OS"]\n\nimport os as OS\n', encoding="utf-8",
    )
    members = [_finding("N812", "src/a.py", 3, "OS")]
    items, _drops = _build(tmp_path, members)
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert rename_items[0]["risk"] == "high"


# ---------- 命名约定豁免（catalog.rename.exempt）----------


def test_exempt_symbol_drops_out(tmp_path: Path):
    """仓库声明的框架约定名（ast 的 visit_*）命中豁免：不入方，进待复核。"""
    (tmp_path / "code-hospital.yaml").write_text(
        'catalog:\n  rename:\n    exempt: ["visit_*"]\n', encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "class V:\n    def visit_Foo(self):\n        return 1\n", encoding="utf-8",
    )
    members = [_finding("N815", "src/a.py", 2, "visit_Foo")]
    items, drops = _build(tmp_path, members)
    assert [it for it in items if it["formula_id"] == "rename"] == []
    assert len(drops) == 1
    assert "命名约定" in drops[0]["reason"]
    assert "visit_Foo" in drops[0]["reason"]


def test_exempt_does_not_cover_other_symbols(tmp_path: Path):
    """豁免只盖住声明的模式，别的符号照常入方。"""
    (tmp_path / "code-hospital.yaml").write_text(
        'catalog:\n  rename:\n    exempt: ["visit_*"]\n', encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return Foo\n", encoding="utf-8")
    members = [_finding("N802", "src/a.py", 1, "Foo")]
    items, drops = _build(tmp_path, members)
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert len(rename_items) == 1
    assert drops == []


def test_exempt_policy_parsing(tmp_path: Path):
    from code_hospital.catalog.policy import load_policy

    # glob 字符（* ? [）原样保留，不被 _TOKEN 校验吞掉
    (tmp_path / "code-hospital.yaml").write_text(
        'catalog:\n  rename:\n    exempt: ["visit_*", "on_?[a-z]", "visit_*"]\n',
        encoding="utf-8",
    )
    policy = load_policy(str(tmp_path))
    assert policy.rename_exempt == ("visit_*", "on_?[a-z]")

    # 类型不对：记 note，用空清单
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  rename:\n    exempt: 42\n", encoding="utf-8",
    )
    policy = load_policy(str(tmp_path))
    assert policy.rename_exempt == ()
    assert any("正骨豁免" in note for note in policy.notes)

    # 没声明：内置默认为空
    (tmp_path / "code-hospital.yaml").write_text("catalog:\n  rules: {}\n", encoding="utf-8")
    assert load_policy(str(tmp_path)).rename_exempt == ()


# ---------- LSP 守门销账（references 在岗时的行为）----------


class _StubPool:
    """LSP 在岗的桩：references 结果由测试直接给定。refs=None 表示缺席。"""

    def __init__(self, refs, notes=()):
        self._refs = refs
        self._notes = list(notes)
        self.closed = False

    def cross_file_refs(self, file_path, line, symbol):
        return self._refs

    def notes(self):
        return list(self._notes)

    def close(self):
        self.closed = True


def _build_with_pool(tmp_path: Path, members: list[Finding], pool):
    diagnosis = _diagnosis(members)
    by_id = {f.id: f for f in members}
    drops: list[dict] = []
    items = build_items(
        [diagnosis], by_id, "rx-x", True,
        project_root=str(tmp_path), drops_out=drops, rename_pool=pool,
    )
    return items, drops


def test_lsp_clears_guard_when_no_cross_file_refs(tmp_path: Path):
    """文本守门拦下（b.py import 了 Foo），但 LSP 确认没有跨文件引用 → 销账入方。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("from a import Foo\n", encoding="utf-8")
    members = [_finding("N802", "src/a.py", 1, "Foo")]
    items, drops = _build_with_pool(tmp_path, members, _StubPool(set()))
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert len(rename_items) == 1
    assert rename_items[0]["method"] == "llm_patch"
    assert drops == []


def test_lsp_confirmed_refs_stay_in_review(tmp_path: Path):
    """LSP 确认有跨文件引用 → 维持待复核，reason 写明证据而不是「等 LSP001」。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return 1\n", encoding="utf-8")
    members = [_finding("N802", "src/a.py", 1, "Foo")]
    items, drops = _build_with_pool(tmp_path, members, _StubPool({"src/b.py", "src/c.py"}))
    assert [it for it in items if it["formula_id"] == "rename"] == []
    assert len(drops) == 1
    assert "LSP 确认" in drops[0]["reason"]
    assert "2 个文件" in drops[0]["reason"]
    assert "LSP001" not in drops[0]["reason"]


def test_lsp_clears_init_reexport_when_unreferenced(tmp_path: Path):
    """__init__.py 重导出名：LSP 确认没有包外引用 → 销账入方（原来是必然待复核）。"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from collections import OrderedDict as OD\n", encoding="utf-8",
    )
    members = [_finding("N817", "pkg/__init__.py", 1, "OD")]
    items, drops = _build_with_pool(tmp_path, members, _StubPool(set()))
    rename_items = [it for it in items if it["formula_id"] == "rename"]
    assert len(rename_items) == 1
    assert drops == []


def test_lsp_confirmed_init_reexport_stays_in_review(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from collections import OrderedDict as OD\n", encoding="utf-8",
    )
    members = [_finding("N817", "pkg/__init__.py", 1, "OD")]
    items, drops = _build_with_pool(tmp_path, members, _StubPool({"app/main.py"}))
    assert [it for it in items if it["formula_id"] == "rename"] == []
    assert len(drops) == 1
    assert "重导出" in drops[0]["reason"]
    assert "app/main.py" in drops[0]["reason"]


def test_default_pool_notes_landed_in_drops(tmp_path: Path):
    """基座未到岗的台账行跟着待复核进报告（file 为空表示这不是某条发现的行）。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return 1\n", encoding="utf-8")
    members = [_finding("N802", "src/a.py", 1, "Foo")]
    pool = _StubPool(None, notes=["LSP 基座：python 未到岗（pyright-langserver 不在 PATH 上），本轮降级文本守门"])
    items, drops = _build_with_pool(tmp_path, members, pool)
    assert any(row["file"] == "" and "未到岗" in row["reason"] for row in drops)
    # 缺席时回退文本守门：没有跨文件引用的符号照常入方
    assert any(it["formula_id"] == "rename" for it in items)


def test_default_pool_created_and_closed(tmp_path: Path, monkeypatch):
    """不传池子时 build_items 自建自关：会话不泄漏（draft 验收断言）。"""
    created = []

    class _TrackingPool(_StubPool):
        def __init__(self, root):
            super().__init__(None)
            created.append(self)

    # prescriber 是函数内 from ..lsp.pool import Pool，打的是 lsp.pool 模块属性
    monkeypatch.setattr("code_hospital.lsp.pool.Pool", _TrackingPool)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def Foo():\n    return 1\n", encoding="utf-8")
    members = [_finding("N802", "src/a.py", 1, "Foo")]
    _build(tmp_path, members)  # _build 传了桩池，自建路径单独验
    assert created == []

    diagnosis = _diagnosis(members)
    by_id = {f.id: f for f in members}
    build_items([diagnosis], by_id, "rx-x", True, project_root=str(tmp_path), drops_out=[])
    assert len(created) == 1
    assert created[0].closed is True
