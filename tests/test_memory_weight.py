"""记忆权重账：Beta 后验、注入档位、复发三重门槛。

这里守的是「别把复发无脑记到记忆头上」——权重账一旦变成噪音池，
半年后没人再看它，等于没做。
"""

import pytest

from code_hospital.catalog import memory_weight as mw
from code_hospital.core.models import Case, Finding, Scope
from code_hospital.storage.db import Database, now_ms


def _row(alpha=1.0, beta=1.0, recurred=0, effective=0, status="active"):
    return {
        "memory_id": "kn-1", "alpha": alpha, "beta": beta,
        "recurred": recurred, "effective": effective, "status": status,
    }


def test_prior_is_a_coin_flip():
    assert mw.weight(None) == 0.5
    assert mw.weight(_row()) == 0.5


def test_evidence_is_asymmetric():
    """出院判有效只给半票：看起来修好了和确实修好了不能同权。"""
    assert mw.ALPHA_EFFECTIVE < mw.ALPHA_CONFIRMED
    # 三次出院确认（1.5 票）不如两次复发（2.0 票）压得多
    after_effective = mw.weight(_row(alpha=1 + 3 * mw.ALPHA_EFFECTIVE))
    after_two_recur = mw.weight(_row(beta=1 + 2 * mw.BETA_RECUR))
    assert after_effective == pytest.approx(2.5 / 3.5)
    assert after_two_recur == pytest.approx(1 / 4)
    assert after_two_recur < after_effective


def test_policy_bands():
    assert mw.policy(None) == "normal"
    assert mw.policy(_row(alpha=3.0)) == "normal"
    assert mw.policy(_row(alpha=1.0, beta=2.0)) == "suspect"   # 0.33
    assert mw.policy(_row(alpha=1.0, beta=4.0)) == "skip"      # 0.2
    assert mw.policy(_row(alpha=9.0, status="degraded")) == "skip"


def test_apply_policy_withholds_and_marks_suspect():
    hits = [
        {"id": "kn-good", "tier": "hard", "title": "a"},
        {"id": "kn-mid", "tier": "reference", "title": "b"},
        {"id": "kn-bad", "tier": "reference", "title": "c"},
    ]
    rows = {
        "kn-good": _row(alpha=5.0),
        "kn-mid": _row(alpha=1.0, beta=2.0),
        "kn-bad": _row(alpha=1.0, beta=9.0),
    }
    kept, withheld = mw.apply_policy(hits, rows)
    assert withheld == 1
    assert [hit["id"] for hit in kept] == ["kn-good", "kn-mid"]
    # 存疑的降到线索区，不给它 reference 的措辞
    assert kept[1]["tier"] == "clue"
    assert kept[1]["weighed"] == 0.33


def test_downgrade_needs_both_floor_and_recurrence():
    """权重低但只复发一次 → 只是嫌疑，不是降级。"""
    assert mw.should_propose_downgrade(_row(alpha=1.0, beta=5.0, recurred=1)) is False
    assert mw.should_propose_downgrade(_row(alpha=1.0, beta=5.0, recurred=3)) is True
    # 复发够多但权重还高 → 不动
    assert mw.should_propose_downgrade(_row(alpha=8.0, beta=1.0, recurred=3)) is False
    # 已经降级的，不再重复建议
    assert mw.should_propose_downgrade(
        _row(alpha=1.0, beta=9.0, recurred=5, status="degraded")
    ) is False


def test_downgrade_line_says_propose_not_act():
    lines = mw.downgrade_lines([_row(alpha=1.0, beta=9.0, recurred=4)])
    assert len(lines) == 1
    assert "建议降级" in lines[0]
    assert "不自动降级" in lines[0]
    assert mw.downgrade_lines([_row(alpha=5.0)]) == []


# ---------- 落表 ----------


def _case(root, cid: str, recalled: list[str], status="discharged") -> Case:
    return Case(
        id=cid, project_root=str(root), title="t", scope_type="repo",
        scope=Scope(), triage_level="outpatient", status=status,
        coverage={"memory": {"channel": "mcp", "recalled": recalled, "feedback": "reported"}},
        created_at=now_ms(), updated_at=now_ms(),
    )


def _finding(case_id: str, fp: str) -> Finding:
    return Finding(
        id=f"f-{case_id}-{fp}", case_id=case_id, report_id="rpt-1", tool="ruler",
        rule_id="F841", severity="major", file_path="src/a.py", line_start=2,
        line_end=2, message="unused", fingerprint=fp, status="resolved",
        content_key=fp, first_seen_round=1, last_seen_round=1,
    )


@pytest.fixture()
async def db(tmp_path):
    database = Database(str(tmp_path))
    await database.connect()
    yield database
    await database.close()


async def test_bump_memory_adds_prior_once(db, tmp_path):
    root = str(tmp_path)
    first = await db.bump_memory("kn-1", root, alpha=0.5)
    assert (first["alpha"], first["beta"]) == (1.5, 1.0)
    second = await db.bump_memory("kn-1", root, alpha=0.5)
    assert (second["alpha"], second["beta"]) == (2.0, 1.0)


async def test_weights_are_per_project(db, tmp_path):
    """同一条记忆在 A 仓库 0.2、B 仓库 0.9 是正常的。"""
    root = str(tmp_path)
    await db.bump_memory("kn-1", root, beta=4.0)
    await db.bump_memory("kn-1", "/other/repo", alpha=4.0)
    here = await db.memory_weights(["kn-1"], root)
    there = await db.memory_weights(["kn-1"], "/other/repo")
    assert mw.weight(here["kn-1"]) < 0.3
    assert mw.weight(there["kn-1"]) > 0.8


async def test_note_effective_gives_half_vote(db, tmp_path):
    rows = await mw.note_effective(db, str(tmp_path), ["kn-1", "kn-2"])
    assert len(rows) == 2
    assert all(row["alpha"] == pytest.approx(1.5) for row in rows)
    assert all(row["effective"] == 1 for row in rows)


async def test_recurrence_blames_only_memory_that_participated(db, tmp_path):
    root = str(tmp_path)
    # 病例 A：当初引用了 kn-1，判定已修好
    await db.insert_case(_case(root, "case-a", ["kn-1"]))
    await db.insert_findings([_finding("case-a", "sha256:aaa")])
    # 病例 B：同一个问题回来了，但那轮没有记忆参与
    await db.insert_case(_case(root, "case-b", []))
    await db.insert_findings([_finding("case-b", "sha256:bbb")])

    got = await mw.note_recurrence(db, root, ["sha256:aaa", "sha256:bbb"])
    assert got["recurred"] == 1          # 只有 aaa 那条能归到记忆头上
    assert got["unblamed"] == 1          # bbb 没有记忆参与，不牵连
    assert [row["memory_id"] for row in got["suspects"]] == ["kn-1"]

    weights = await db.memory_weights(["kn-1"], root)
    assert weights["kn-1"]["recurred"] == 1
    assert weights["kn-1"]["beta"] == pytest.approx(2.0)


async def test_recurrence_without_history_is_silent(db, tmp_path):
    got = await mw.note_recurrence(db, str(tmp_path), ["sha256:never-seen"])
    assert got == {"suspects": [], "unblamed": 0, "recurred": 0}


async def test_resolved_refs_ignore_open_cases(db, tmp_path):
    """还在治的病例不算「当初判它好了」，不能拿它做复发归因。"""
    root = str(tmp_path)
    await db.insert_case(_case(root, "case-live", ["kn-1"], status="dispensing"))
    await db.insert_findings([_finding("case-live", "sha256:aaa")])
    assert await db.resolved_case_refs(["sha256:aaa"], root) == []
