"""内置仪器退役：退了就不跑，不算缺口，退错了能放回来。

判据四条：
1. 退役的内置仪器不进初检套餐；
2. 它**声明过**的扩展名不再算缺口（不写进招聘启事），但报告仍要说
   「已按客户要求退役」——不说就是静默降级；
3. 恢复回到退役前的档位，不是一律退回影子期；
4. 反例：没声明扩展名的仪器退役，不许把「全类型」一起豁免掉，
   否则一台退了、所有缺口都不报了。

第五件事是位置：退役状态只存一处（市场索引）。两份记录迟早有一份是旧的。
"""

import json

from code_hospital.catalog import formulary, ledger, market


def test_retiring_a_shipped_instrument_takes_it_out_of_the_panel(tmp_path):
    root = str(tmp_path)
    before = formulary.selected_instrument_ids("checkup", ["a.py"], [], [], "checkup", root)
    assert "ruff" in before

    got = market.retire(root, "ruff")
    assert got["ok"], got

    after = formulary.selected_instrument_ids("checkup", ["a.py"], [], [], "checkup", root)
    assert "ruff" not in after


def test_a_retired_instrument_stops_counting_as_a_gap(tmp_path):
    """退掉覆盖 .go 的三台，缺口里就不该再提 .go，但限制项要说明原因。"""
    root = str(tmp_path)
    for ident in ("structure-ruler", "jscpd", "security-ruler"):
        assert market.retire(root, ident)["ok"]

    coverage: dict = {}
    notes = ledger.record_scope_gaps(coverage, ["main.go"], [], "c1", root)

    assert not any("没有在岗仪器" in line for line in notes), notes
    assert any("已按客户要求退役" in line for line in notes), notes
    assert "missing_instrument" not in json.dumps(coverage, ensure_ascii=False)


def test_an_instrument_without_extensions_excuses_nothing(tmp_path):
    """semgrep 声明的是全类型口径（extensions: []）。它退役不该让所有类型都免检。"""
    root = str(tmp_path)
    assert market.retire(root, "semgrep")["ok"]

    notes = ledger.record_scope_gaps({}, ["main.go"], [], "c1", root)
    assert any("没有在岗仪器" in line for line in notes), notes


def test_restore_returns_it_to_the_tier_it_had(tmp_path):
    root = str(tmp_path)
    assert market.retire(root, "ruff")["ok"]
    assert "ruff" in market.retired_ids(root)

    got = market.restore(root, "ruff")
    assert got["ok"], got
    assert got["tier"] == "active"
    assert "ruff" not in market.retired_ids(root)


def test_restore_does_not_silently_demote_a_promoted_instrument(tmp_path):
    """已转正的市场仪器退掉再恢复，仍是 active。退回影子期会让它的结论突然不进报告。"""
    root = str(tmp_path)
    base = tmp_path / ".hospital" / "market"
    base.mkdir(parents=True)
    (base / "index.json").write_text(
        json.dumps({"acme": {"tier": "active", "origin": "market"}}), encoding="utf-8"
    )

    assert market.retire(root, "acme")["ok"]
    assert market.restore(root, "acme")["tier"] == "active"


def test_retirement_state_lives_in_one_place(tmp_path):
    root = str(tmp_path)
    assert market.retire(root, "ruff")["ok"]

    index = json.loads(
        (tmp_path / ".hospital" / "market" / "index.json").read_text(encoding="utf-8")
    )
    assert index["ruff"]["tier"] == "retired"
    assert index["ruff"]["origin"] == "builtin"
    assert not (tmp_path / ".hospital" / "retired.json").exists()


def test_the_listing_shows_a_retired_shipped_instrument(tmp_path):
    root = str(tmp_path)
    assert market.retire(root, "ruff")["ok"]

    got = market.listing(root)
    core = {row["id"]: row for row in got["core"]}
    assert core["ruff"]["tier"] == "retired"
    assert "ruff" in got["note"]
    assert "已按客户要求退役" in got["note"]


def test_retiring_something_that_never_existed_is_refused(tmp_path):
    got = market.retire(str(tmp_path), "no-such-instrument")
    assert not got["ok"]
    assert "没有这台仪器" in got["note"]
