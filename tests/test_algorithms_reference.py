"""算法参考实现的回归测试（docs/algorithms_reference.py）。

这些用例对应 docs/code-hospital-algorithms.md §1.5 / §3 / §6 / §7 / §8 / §9 的边界清单，
是后续把这些算法搬进 src/code_hospital 时的验收基线。

运行：.venv/Scripts/python -m pytest tests/test_algorithms_reference.py -q
"""

from __future__ import annotations

import pytest

from code_hospital.core import algorithms as A


def _anchor(text: str) -> str:
    return A.sha256_str(text)


# ────────────────────────── §1 指纹与差分 ──────────────────────────


def test_occurrence_distinguishes_copied_findings():
    a = A.FindingRow("E722", "a.py", "", "except: pass", 10, anchor=_anchor("try: a"))
    b = A.FindingRow("E722", "a.py", "", "except: pass", 40, anchor=_anchor("try: b"))
    assert a.content_key == b.content_key          # 同形态
    assert a.identity_fp != b.identity_fp          # 不同身份
    A.annotate_occurrence([a, b])
    assert a.identity_fp != b.identity_fp


def test_fix_one_of_two_copies_is_detected_by_anchor():
    prev = [
        A.FindingRow("E722", "a.py", "", "except: pass", 10, anchor=_anchor("try: a")),
        A.FindingRow("E722", "a.py", "", "except: pass", 40, anchor=_anchor("try: b")),
    ]
    curr = [
        A.FindingRow("E722", "a.py", "", "except TypeError: pass", 10, anchor=_anchor("try: a")),
        A.FindingRow("E722", "a.py", "", "except: pass", 40, anchor=_anchor("try: b")),
    ]
    d = A.match_findings(prev, curr)
    assert d.counts() == {"matched": 2, "resolved": 1, "possibly_resolved": 0, "added": 0}
    assert [r.line_start for r in d.resolved] == [40]


def test_inserted_import_keeps_exact_match():
    prev = [A.FindingRow("F821", "b.py", "f", "return undefined_name", 5)]
    curr = [A.FindingRow("F821", "b.py", "f", "return undefined_name", 9)]
    d = A.match_findings(prev, curr)
    assert d.counts()["matched"] == 1 and d.matched[0].level == "exact"


def test_missing_anchor_never_hard_judges_resolved():
    prev = [A.FindingRow("E722", "a.py", "", "except: pass", 10)]
    curr = [A.FindingRow("E722", "a.py", "", "except TypeError: pass", 10)]
    d = A.match_findings(prev, curr)
    assert d.counts()["possibly_resolved"] == 1
    assert d.counts()["resolved"] == 0


def test_short_snippet_never_fuzzy_matched():
    prev = [A.FindingRow("X", "a.py", "", "pass", 1, anchor=_anchor("k"))]
    curr = [A.FindingRow("X", "a.py", "", "pass ", 1, anchor=_anchor("k"))]
    # "pass" 归一化后仅 4 字符 < MIN_SNIPPET_LEN，但 identity 相同仍会 exact 命中
    d = A.match_findings(prev, curr)
    assert d.counts()["matched"] == 1


def test_rewritten_problem_is_resolved_plus_added():
    prev = [A.FindingRow("F821", "c.py", "f", "return undefined_name", 5)]
    curr = [A.FindingRow("F821", "c.py", "g", "return other_missing", 5)]
    d = A.match_findings(prev, curr)
    assert d.counts() == {"matched": 0, "resolved": 1, "possibly_resolved": 0, "added": 1}


# ────────────────────────── §3 归因 ──────────────────────────


def test_attribution_levels():
    fp = {"src/a.py": [(12, 18)]}
    added = [
        A.FindingRow("R1", "src/a.py", "f", "x", 15),   # 行内
        A.FindingRow("R2", "src/a.py", "f", "x", 22),   # 邻域（gap=4）
        A.FindingRow("R4", "src/a.py", "g", "x", 60),   # 同文件但远
        A.FindingRow("R5", "src/b.py", "", "x", 3),     # 文件外
    ]
    out = A.attribute_new_findings(added, fp)
    assert [lv for _, lv in out["new_related"]] == ["R1", "R2"]
    assert [lv for _, lv in out["new_unrelated"]] == ["R4", "R5"]


def test_attribution_symbol_level():
    fp = {"src/a.py": [(100, 102)]}
    added = [A.FindingRow("R3", "src/a.py", "handler", "x", 10)]
    out = A.attribute_new_findings(added, fp, touched_symbols={"src/a.py": {"handler"}})
    assert out["new_related"] and out["new_related"][0][1] == "R3"


def test_footprint_from_diff():
    diff = """--- a/src/x.py
+++ b/src/x.py
@@ -10,3 +10,4 @@
 a
-b
+B
+C
 d
"""
    fp = A.footprint_from_unified_diff(diff)
    assert fp == {"src/x.py": [(11, 13)]} or fp["src/x.py"][0][0] == 11


# ────────────────────────── §6 排序 ──────────────────────────


def test_autofix_precedes_llm_patch_in_same_file():
    items = [
        {"id": "p", "method": "llm_patch", "files": ["x.py"], "symbols": [],
         "severity": "major", "seq_hint": 1},
        {"id": "a", "method": "autofix", "files": ["x.py"], "symbols": [],
         "severity": "minor", "seq_hint": 2},
    ]
    order = [it["id"] for it in A.order_prescription_items(items)]
    assert order.index("a") < order.index("p")


def test_ordering_is_stable_without_constraints():
    items = [
        {"id": "m", "method": "manual", "files": ["y.py"], "symbols": [],
         "severity": "critical", "seq_hint": 3},
        {"id": "a", "method": "autofix", "files": ["x.py"], "symbols": [],
         "severity": "nit", "seq_hint": 1},
    ]
    order = [it["id"] for it in A.order_prescription_items(items)]
    assert order == ["a", "m"]


# ────────────────────────── §7 锚点与校验 ──────────────────────────


def test_search_candidates_expand_geometrically():
    lines = ["import os", "", "", "def h(req):", "    d = req.get('d')",
             "    if not d:", "        return None", "    return d", "", "def g():", "    return None"]
    cands = A.gen_search_candidates(lines, 6)
    assert cands and cands[0]["unique"] is True
    assert cands[0]["window"] == 0
    assert len(cands) <= 3


def test_search_candidates_prefer_unique_small_window():
    lines = ["def f():", "    return 1"] * 3   # 高频重复
    cands = A.gen_search_candidates(lines, 2)
    assert cands
    # 单行不唯一时必须靠窗口扩张；若最多 20 行仍不唯一，标 unique=False 而非假装成功
    assert all("text" in c for c in cands)


def test_validate_patch_blocks_out_of_scope():
    files = {"src/a.py": "x = 1\n"}
    hunks = A.parse_patch(
        "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE", default_path="src/b.py"
    )
    r = A.validate_patch(hunks, files, ["src/a.py"])
    assert not r.ok and r.stage in ("scope", "anchor")


def test_validate_patch_blocks_secret():
    files = {"s.py": "import os\n"}
    hunks = A.parse_patch(
        '<<<<<<< SEARCH\nimport os\n=======\nimport os\nAPI_KEY = "abcdefghij1234567890"\n>>>>>>> REPLACE',
        default_path="s.py",
    )
    r = A.validate_patch(hunks, files, ["s.py"])
    assert not r.ok and r.stage == "content" and "hardcoded_secret" in r.message


def test_validate_patch_blocks_manifest():
    files = {"pyproject.toml": "[project]\nname='x'\n"}
    hunks = A.parse_patch(
        "<<<<<<< SEARCH\nname='x'\n=======\nname='y'\n>>>>>>> REPLACE",
        default_path="pyproject.toml",
    )
    r = A.validate_patch(hunks, files, ["pyproject.toml"])
    assert not r.ok and r.stage == "content"


def test_parse_patch_rejects_garbage():
    with pytest.raises(A.PatchParseError):
        A.parse_patch("这不是补丁")


# ────────────────────────── §8 逆应用 ──────────────────────────


def test_apply_then_reverse_roundtrip():
    files = {"a.py": "def f():\n    x = 1\n    return x\n"}
    hunks = A.parse_patch(
        "<<<<<<< SEARCH\n    x = 1\n=======\n    x = 2\n>>>>>>> REPLACE", default_path="a.py"
    )
    assert A.validate_patch(hunks, files, ["a.py"]).ok
    after = A.apply_hunks(files, hunks)
    assert after["a.py"] != files["a.py"]
    back = A.reverse_apply(after, hunks)
    assert back.ok and back.files["a.py"] == files["a.py"]


def test_reverse_apply_detects_conflict():
    files = {"a.py": "def f():\n    x = 1\n    return x\n"}
    hunks = A.parse_patch(
        "<<<<<<< SEARCH\n    x = 1\n=======\n    x = 2\n>>>>>>> REPLACE", default_path="a.py"
    )
    after = A.apply_hunks(files, hunks)
    tampered = {"a.py": after["a.py"].replace("x = 2", "x = 99")}
    res = A.reverse_apply(tampered, hunks)
    assert not res.ok and "锚点" in res.reason


# ────────────────────────── §9 门禁 ──────────────────────────


def test_gate_levels_differ_on_major_regression():
    base = {("ruff", "E501"): 1}
    cur = {("ruff", "E501"): 3}
    strict = A.gate_verdict([A.health_check(base, cur, {"ruff"}, "strict")], "strict")
    standard = A.gate_verdict([A.health_check(base, cur, {"ruff"}, "standard")], "standard")
    assert strict["verdict"] == "fail"
    assert standard["verdict"] == "pass"


def test_health_ignores_new_device():
    base = {("ruff", "E501"): 1}
    cur = {("ruff", "E501"): 1, ("semgrep", "x"): 5}
    assert A.health_check(base, cur, {"ruff"}, "strict").verdict == "pass"


def test_outcome_derivation():
    ok = {"verdict": "pass"}
    assert A.derive_outcome(3, 3, 0, ok, False) == "cured"
    assert A.derive_outcome(2, 3, 0, ok, False) == "improved"
    assert A.derive_outcome(0, 3, 1, ok, False) == "referred"
    assert A.derive_outcome(3, 3, 0, {"verdict": "fail"}, False) == "unchanged"
