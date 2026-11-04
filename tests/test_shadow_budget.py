"""影子期的成本参数：能配的是「快慢」，不能配的是「够不够格」。

两条边界：
- `every_nth` / `max_per_scan` 是成本，客户可以按机器和预算调；
- `MIN_RUNS` / `MIN_CORROBORATION` / `MIN_CHECKABLE` 是判据，写死。
  判据一改，两个仓库的重合率就不可比了，而成本和可比性无关。

第三个测试是回归：已转正的仪器**不该占**试用期的每轮名额。它先被放进 chosen，
再用 `len(chosen) >= max_per_scan` 判断，3 台 active 就会把试用的全挤掉——
影子期于是永远攒不到样本，招聘闭环静默停摆。
"""

from code_hospital.catalog import market, probation


def _fake_market(monkeypatch, rows):
    monkeypatch.setattr(market, "stage", lambda root: (rows, []))
    monkeypatch.setattr(market, "_read_index", lambda root: {})
    monkeypatch.setattr(market, "_write_index", lambda root, index: None)


def test_active_instruments_do_not_use_up_the_shadow_quota(tmp_path, monkeypatch):
    rows = {f"live{i}": {"id": f"live{i}", "tier": "active"} for i in range(3)}
    rows["newbie"] = {"id": "newbie", "tier": "probation"}
    _fake_market(monkeypatch, rows)

    chosen, _ = market.plan(str(tmp_path))

    assert "newbie" in chosen
    assert {"live0", "live1", "live2"} <= set(chosen)


def test_the_round_rhythm_is_configurable(tmp_path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  shadow:\n    every_nth: 1\n    max_per_scan: 5\n", encoding="utf-8"
    )

    caps = probation.limits(str(tmp_path))

    assert caps.every_nth == 1
    assert caps.max_per_scan == 5
    # 样本早就够了，但 every_nth=1 就是每轮都跑
    assert probation.should_run({"runs": 30}, every_nth=caps.every_nth)[0] is True


def test_zero_means_switch_the_shadow_runs_off(tmp_path, monkeypatch):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  shadow:\n    max_per_scan: 0\n", encoding="utf-8"
    )
    assert probation.limits(str(tmp_path)).max_per_scan == 0

    _fake_market(monkeypatch, {"newbie": {"id": "newbie", "tier": "probation"}})
    chosen, notes = market.plan(str(tmp_path))

    assert chosen == {}
    assert any("成本闸门" in line for line in notes), notes


def test_the_verdict_thresholds_are_not_configurable():
    """判据写死。文档写着「这是判断不是参数」，这里把它钉住。"""
    assert probation.MIN_RUNS == 8
    assert probation.MIN_CORROBORATION == 0.6
    assert probation.MIN_CHECKABLE == 5
    assert not hasattr(probation, "load_thresholds")
    assert not hasattr(probation, "set_thresholds")


def test_bad_values_fall_back_to_defaults(tmp_path):
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  shadow:\n    every_nth: -3\n    max_per_scan: abc\n", encoding="utf-8"
    )
    caps = probation.limits(str(tmp_path))
    assert caps.every_nth == probation.EVERY_NTH
    assert caps.max_per_scan == probation.MAX_PER_SCAN
