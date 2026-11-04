"""试用期账与转正判据（第 5 步）。

这里测的是「一台第三方仪器攒够什么才够格转正」，不是「它好不好用」——
好坏没有金标，判不了；够不够格能从三个数上判，所以只判这个。
"""

from __future__ import annotations

import pytest

from code_hospital.catalog import probation


def _run(status="ran", claims=10, corroborated=8, checkable=10, bad=0,
         inc_claims=100, inc_bad=0, note="", samples=None):
    return {
        "status": status,
        "claims": claims,
        "corroborated": corroborated,
        "checkable": checkable,
        "bad": bad,
        "incumbent_claims": inc_claims,
        "incumbent_bad": inc_bad,
        "note": note,
        "case_id": "case-1",
        "samples": samples if samples is not None else [
            {"file": "src/a.py", "line": 2, "rule_id": "ACME001"},
        ],
    }


def _many(trial, rounds, **kw):
    for _ in range(rounds):
        trial = probation.observe(trial, _run(**kw), _next_report())
    return trial


_REPORTS = iter(range(10_000))


def _next_report() -> str:
    return f"rpt-{next(_REPORTS)}"


# —— 三个数怎么算 ——

def test_corroboration_matches_by_position_not_by_rule_id():
    """跨厂商的 rule_id 从不相同，按规则号比等于永远比不出重合。"""
    samples = [
        {"file": "src/a.py", "line": 10, "rule_id": "ACME001"},
        {"file": "src/a.py", "line": 50, "rule_id": "ACME002"},
    ]
    indexed = {"src/a.py": [(10, 10)]}  # 现任的规则号叫 F841，位置对得上

    assert probation.corroborated(samples, indexed) == 1


def test_corroboration_tolerates_adjacent_lines():
    """同一处缺陷，一台标在声明行、一台标在语句行，是常见差异，不是分歧。"""
    samples = [{"file": "src/a.py", "line": 12}]

    assert probation.corroborated(samples, {"src/a.py": [(10, 10)]}) == 1
    assert probation.corroborated(samples, {"src/a.py": [(20, 20)]}) == 0


def test_corroboration_uses_the_incumbent_range():
    """现任给的是区间时，区间内任意一行都算同一处。"""
    samples = [{"file": "src/a.py", "line": 6}]

    assert probation.corroborated(samples, {"src/a.py": [(4, 9)]}) == 1


def test_bad_location_covers_missing_file_and_out_of_range():
    samples = [
        {"file": "src/a.py", "line": 3},
        {"file": "src/a.py", "line": 99},
        {"file": "src/gone.py", "line": 1},
        {"file": "src/a.py", "line": 0},
    ]
    counts = {"src/a.py": 20, "src/gone.py": None}

    assert probation.count_bad(samples, counts) == 3


def test_covered_extensions_distinguishes_all_types_from_none():
    """`None` = 有一台按全类型干活的（semgrep），空集 = 一台能比的都没有。

    这两种在判据里意思相反，不能都返回空集。
    """
    devices = [{"tool": "semgrep", "available": True, "outcome": "ok"}]
    assert probation.covered_extensions("", devices) is None

    assert probation.covered_extensions("", []) == set()

    devices = [{"tool": "ruff", "available": True, "outcome": "ok"}]
    assert probation.covered_extensions("", devices) == {"py"}


def test_checkable_count_uses_all_types_when_covered_is_none():
    samples = [{"file": "a.py"}, {"file": "b.xml"}]

    assert probation.checkable_count(samples, None) == 2
    assert probation.checkable_count(samples, {"py"}) == 1
    assert probation.checkable_count(samples, set()) == 0


# —— 判据 ——

def test_few_samples_is_not_a_verdict():
    trial = _many({}, 3)
    got = probation.verdict(trial)

    assert got["state"] == "insufficient"
    assert not got["promotable"]
    assert "3/8" in got["line"]


def test_enough_samples_with_corroboration_is_ready():
    trial = _many({}, probation.MIN_RUNS)
    got = probation.verdict(trial)

    assert got["state"] == "ready"
    assert got["promotable"]
    assert got["rate"] >= probation.MIN_CORROBORATION
    assert "等人签字" in got["line"]


def test_quiet_instrument_is_not_promoted():
    """跑了十轮一条没交，判不出是干净还是没接上——转正是给能干活的。"""
    trial = _many({}, probation.MIN_RUNS, claims=0, corroborated=0, checkable=0, samples=[])

    got = probation.verdict(trial)
    assert got["state"] == "quiet"
    assert not got["promotable"]


def test_bad_locations_block_promotion():
    """指的位置都不存在，是最硬的一种不合格：不需要金标就能判。"""
    trial = _many({}, probation.MIN_RUNS, claims=10, bad=4, corroborated=6)

    got = probation.verdict(trial)
    assert got["state"] == "noisy"
    assert not got["promotable"]
    assert "不存在" in got["line"]


def test_never_ran_is_not_a_quality_problem():
    """装上了没到岗，不是不合格。这两件事混成一件，客户会去换一台本来没问题的仪器。"""
    trial = _many({}, probation.GIVE_UP_ROUNDS, status="failed", note="命令不在 PATH 上")

    got = probation.verdict(trial)
    assert got["state"] == "unavailable"
    assert "没到岗" in got["line"]
    assert not got["promotable"]


def test_nothing_comparable_is_honest_about_it():
    """招来的仪器只看现任不看的东西时，重合率 0 说明不了任何事——不是"它全错了"。"""
    trial = _many({}, probation.MIN_RUNS, checkable=0, corroborated=0)

    got = probation.verdict(trial)
    assert got["state"] == "uncorroborated"
    assert got["promotable"], "没有人能替它作证，不等于不能转正；要人签字"
    assert "无从比对" in got["line"]


def test_disagreement_is_promotable_but_flagged():
    trial = _many({}, probation.MIN_RUNS, corroborated=1, checkable=10)

    got = probation.verdict(trial)
    assert got["state"] == "contentious"
    assert got["promotable"]
    assert "要人签字" in got["line"]


# —— 记账 ——

def test_same_report_is_not_counted_twice():
    """补一次片、重扫一次不该把样本凭空喂高。"""
    once = probation.observe({}, _run(), "rpt-1")
    twice = probation.observe(once, _run(), "rpt-1")

    assert twice["runs"] == 1
    assert twice["claims"] == 10


def test_samples_are_deduped_across_rounds():
    """同一处反复报，是"它一直在说这一处"，不是"它说了很多处"。"""
    trial = probation.observe({}, _run(samples=[{"file": "a.py", "line": 1}]), "r1")
    trial = probation.observe(trial, _run(samples=[{"file": "a.py", "line": 1}]), "r2")

    assert len(trial["samples"]) == 1


def test_failed_rounds_do_not_pollute_the_rates():
    trial = probation.observe({}, _run(), "r1")
    trial = probation.observe(trial, _run(status="failed", note="炸了"), "r2")

    assert trial["runs"] == 1 and trial["failed"] == 1
    assert trial["claims"] == 10
    assert trial["last_error"] == "炸了"


# —— 成本闸门 ——

def test_runs_every_round_until_samples_are_enough():
    assert probation.should_run({}) == (True, "")

    almost = _many({}, probation.MIN_RUNS - 1)
    assert probation.should_run(almost)[0] is True


def test_cadence_thins_out_after_enough_samples():
    """样本够了改成每 N 轮跑一次，但账上要写清楚这次为什么不跑。"""
    enough = _many({}, probation.MIN_RUNS)
    assert enough["runs"] % probation.EVERY_NTH != 0

    due, why = probation.should_run(enough)
    assert due is False
    assert "控成本" in why

    later = _many(enough, 1, claims=1, corroborated=1, checkable=1, samples=[])
    assert probation.should_run(later)[0] is True


@pytest.mark.parametrize("raw", [None, {}, {"runs": "3"}, {"samples": "坏"}])
def test_broken_ledger_does_not_raise(raw):
    got = probation.load(raw)

    assert got["runs"] in (0, 3)
    assert isinstance(got["samples"], list)
