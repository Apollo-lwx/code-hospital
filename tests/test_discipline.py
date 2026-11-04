"""院规执行器：判断在记忆层，医院只执行已批的。

这一层的验收有两句话，缺一条都算写错：

1. **批准之前什么都不会变，批准之后绕过就过不去**——所以既要断言
   规则库为空时布尔放行仍能过，也要断言批过之后同样的调用被拒、
   而且仓库配置关不掉。
2. **医院不产生判断**——所以还要反向断言：这里没有阈值、没有候选、
   没有「被拒过几次」的账。加了任何一条，就是在执行层又长了一个脑子，
   跟记忆层迟早分叉。

第 2 条是这一版的重点。上一版把「几次才该加严」写成了常量，
那是在执行层重写了一套进化逻辑。
"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_hospital.api import capabilities
from code_hospital.catalog import discipline
from code_hospital.core.errors import HospitalError
from code_hospital.departments import reception, scanner
from code_hospital.safety import admission
from code_hospital.storage.db import Database

pytestmark = [pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")]

REF = "mem-rule-1"


def _book(kind: str, count: int, stage: str = "scanned") -> dict:
    return {"kind": kind, "stage": stage, "count": count, "case_id": "c", "round": 1}


def _case(cid: str, *rows: dict, updated: int = 0) -> SimpleNamespace:
    return SimpleNamespace(id=cid, coverage={"discipline": list(rows)}, updated_at=updated)


def _approve(root: str, check_id: str = "gate_receipt", ref: str = REF) -> dict:
    return discipline.approve(root, check_id, ref=ref, channel="convention")


class _Port:
    """假装接上了记忆基座。channel 是 mcp，recall 给一条经验。"""

    channel = "mcp"

    def __init__(self, hits: list[dict] | None = None, channel: str = "mcp"):
        self.channel = channel
        self.hits = hits if hits is not None else [{
            "id": REF, "title": "放行必须带回执，不然门禁等于没有", "tier": "reference",
        }]
        self.tasks: list[str] = []

    async def recall(self, task: str, *, role: str = "", top_k: int | None = None) -> dict:
        self.tasks.append(task)
        return {"channel": self.channel, "task": task, "hits": self.hits, "note": ""}


@pytest.fixture
def wired(monkeypatch):
    """把记忆端口换成假的。不换的话临时仓库没有 .rsi，一律 absent。"""
    from code_hospital.integrations import memory_port

    port = _Port()

    async def fake_open(root: str, prefer: str = "") -> _Port:
        return port

    monkeypatch.setattr(memory_port, "open_port", fake_open)
    return port


@pytest.fixture
def unwired(monkeypatch):
    from code_hospital.integrations import memory_port

    port = _Port(hits=[], channel="absent")

    async def fake_open(root: str, prefer: str = "") -> _Port:
        return port

    monkeypatch.setattr(memory_port, "open_port", fake_open)
    return port


# ---------------------------------------------------------------- 医院不判断


def test_the_execution_layer_has_no_judgement_left():
    """阈值、候选、无法固化的清单、拒绝账——四样都是判断，一样都不许留。

    这条测试是这一版的设计意图本身。哪天有人想「顺手加个阈值」，
    它会先红。
    """
    for gone in ("MIN_EVIDENCE", "candidates", "unfixable", "reject"):
        assert not hasattr(discipline, gone), f"{gone} 是判断，属于记忆层"


def test_survey_sums_the_same_deviation_across_cases():
    rows = discipline.survey([
        _case("c1", _book("gate_bypass", 2)),
        _case("c2", _book("gate_bypass", 3, "diagnosed")),
    ])

    assert len(rows) == 1
    assert rows[0]["count"] == 5
    assert rows[0]["cases"] == ["c1", "c2"]
    assert set(rows[0]["stages"]) == {"scanned", "diagnosed"}


def test_survey_reports_numbers_not_recommendations():
    """数出来的东西只有个数。带「建议」「应该」的行就是把判断偷渡进来了。"""
    rows = discipline.survey([_case("c1", _book("gate_bypass", 9))])

    assert set(rows[0]) == {"kind", "count", "cases", "stages", "detail", "last_at"}


def test_a_deviation_with_no_check_stays_a_number():
    """医院执行不了的东西，写进报告也只是个数字，不编提案。"""
    rows = discipline.survey([_case("c1", _book("filing_invalid", 9))])

    assert rows[0]["kind"] == "filing_invalid"
    assert rows[0]["count"] == 9
    kinds = {kind for check in discipline.CHECKS for kind in check.deviates}
    assert "filing_invalid" not in kinds


def test_checks_are_a_closed_catalogue():
    """批不了一条不存在的规矩。清单是封闭的，id 就是开关名。"""
    assert {check.id for check in discipline.CHECKS} == {"gate_receipt", "location_floor"}
    for check in discipline.CHECKS:
        assert check.effect and check.cost


def test_the_catalogue_says_which_are_already_on(tmp_path: Path):
    root = str(tmp_path)
    _approve(root, "location_floor")

    catalog = {row["id"]: row for row in discipline.check_catalog(root)}
    assert catalog["location_floor"]["approved"] is True
    assert catalog["location_floor"]["ref"] == REF
    assert catalog["gate_receipt"]["approved"] is False


# ---------------------------------------------------------------- 规则库


def test_a_broken_rules_file_means_no_rules_not_a_crash(tmp_path: Path):
    """规则库坏了就当一条都没批。带着「已固化」的报告比没有规则更糟。"""
    hospital = tmp_path / ".hospital"
    hospital.mkdir()
    (hospital / discipline.RULES_FILE).write_text("{oops", encoding="utf-8")

    assert discipline.state(str(tmp_path))["approved"] == {}
    assert discipline.requires(str(tmp_path), "gate_receipt") is False


def test_an_unknown_check_cannot_be_approved(tmp_path: Path):
    with pytest.raises(ValueError):
        discipline.approve(str(tmp_path), "made-up", ref=REF)


def test_approval_without_a_ground_cannot_happen(tmp_path: Path):
    """关不掉的校验必须答得上「凭什么」。答不上来就不该批。"""
    with pytest.raises(ValueError) as caught:
        discipline.approve(str(tmp_path), "gate_receipt", ref="  ")

    assert "ref" in str(caught.value)
    assert discipline.requires(str(tmp_path), "gate_receipt") is False


def test_the_rules_file_holds_the_switch_and_the_ref_only(tmp_path: Path):
    """证据正文留在记忆层。这里再抄一份，就是第二个知识库。"""
    root = str(tmp_path)
    _approve(root)

    doc = json.loads((tmp_path / ".hospital" / discipline.RULES_FILE).read_text("utf-8"))
    entry = doc["approved"]["gate_receipt"]
    assert set(entry) == {"on", "ref", "channel", "since"}
    assert entry["ref"] == REF
    assert "evidence" not in entry and "cost" not in entry


def test_approval_is_idempotent_and_keeps_history(tmp_path: Path):
    root = str(tmp_path)
    _approve(root)
    _approve(root, "location_floor", ref="mem-rule-2")

    state = discipline.state(root)
    assert set(state["approved"]) == {"gate_receipt", "location_floor"}
    assert [row["check"] for row in state["history"]] == ["gate_receipt", "location_floor"]
    assert state["history"][-1]["ref"] == "mem-rule-2"


def test_the_report_line_cites_the_memory_entry(tmp_path: Path):
    """用户看到「怎么又多要一个参数」时，要能顺着这行追回一条经验。"""
    root = str(tmp_path)
    _approve(root)

    [line] = discipline.rule_lines(root)
    assert "阶段放行必须带回执" in line
    assert REF in line
    assert "关不掉" in line


def test_a_stale_lock_with_no_entry_is_ignored(tmp_path: Path):
    """规则库里出现清册外的 id，不能让它影响执行。"""
    hospital = tmp_path / ".hospital"
    hospital.mkdir()
    (hospital / discipline.RULES_FILE).write_text(json.dumps({
        "approved": {"ghost": {"on": True, "ref": "x"}, "gate_receipt": {"on": True, "ref": REF}},
        "history": [],
    }), encoding="utf-8")

    assert discipline.requires(str(tmp_path), "ghost") is False
    assert discipline.requires(str(tmp_path), "gate_receipt") is True


# ---------------------------------------------------------------- 记忆端口


async def test_listing_asks_memory_what_we_did_before(wired, tmp_path: Path):
    """list 要顺带问一句「这类事以前定过没有、管不管用」——判断从这里来。"""
    root = _repo(tmp_path)
    await _past_cases(root, 2)

    got = await discipline.collect(root)

    assert got["memory"]["channel"] == "mcp"
    assert [hit["id"] for hit in got["memory"]["hits"]] == [REF]
    assert wired.tasks and "gate_bypass" in wired.tasks[0]


async def test_listing_hands_over_facts_only(wired, tmp_path: Path):
    """返回里不许出现 candidates / unfixable / 阈值判断。"""
    got = await discipline.collect(str(tmp_path))

    assert "candidates" not in got
    assert "unfixable" not in got
    assert set(got) >= {"deviations", "checks", "approved", "memory", "note"}


async def test_the_recall_task_carries_no_source_code(wired, tmp_path: Path):
    """召回任务只带「哪类偏差几次」。带源码就是让记忆层替医院读代码了。"""
    cases = [_case("c1", _book("gate_bypass", 4))]
    task = discipline.survey_line(discipline.survey(cases))

    assert "gate_bypass 4 次" in task
    assert "/" not in task and "def " not in task


async def test_without_memory_the_hospital_will_not_harden(unwired, tmp_path: Path):
    """没有历史就没有资格说「该加严了」。这是这一版最硬的一条。"""
    from code_hospital.api import mcp_server

    root = str(tmp_path)
    (tmp_path / ".hospital").mkdir()

    with pytest.raises(HospitalError) as caught:
        await mcp_server._rule("approve", "gate_receipt", REF, "", "", True, root)

    assert caught.value.target == "ref"
    assert discipline.requires(root, "gate_receipt") is False


async def test_without_memory_listing_still_tells_the_truth(unwired, tmp_path: Path):
    """不装作在进化：照样报数，但把「不做判断」写在脸上。"""
    got = await discipline.collect(str(tmp_path))

    assert got["memory"]["channel"] == "absent"
    assert "不做判断" in got["note"] or "不出判断" in got["note"]
    assert got["checks"]


# ---------------------------------------------------------------- 硬校验：门禁回执


def test_boolean_confirmation_still_passes_while_the_rule_is_off(tmp_path: Path):
    event = capabilities.admit_stage(
        True, "scanned", expected="g-1", project_root=str(tmp_path),
    )

    assert event["kind"] == "gate_bypass"
    assert event["gate"] == "scanned"


def test_with_the_rule_on_a_bare_boolean_is_refused(tmp_path: Path):
    root = str(tmp_path)
    _approve(root)

    with pytest.raises(HospitalError) as caught:
        capabilities.admit_stage(True, "scanned", expected="g-1", project_root=root)

    assert caught.value.code == "Forbidden"
    assert caught.value.target == "ack"


def test_matching_receipt_releases_and_records_nothing(tmp_path: Path):
    root = str(tmp_path)
    _approve(root)

    assert capabilities.admit_stage(
        True, "scanned", ack="g-1", expected="g-1", project_root=root,
    ) is None


def test_a_receipt_for_another_artifact_is_refused(tmp_path: Path):
    root = str(tmp_path)
    _approve(root)

    with pytest.raises(HospitalError):
        capabilities.admit_stage(
            True, "scanned", ack="g-old", expected="g-new", project_root=root,
        )


def test_unreadable_artifact_degrades_instead_of_passing(tmp_path: Path):
    """说好要核却核不了，必须留账，不能悄悄当成核过了。"""
    root = str(tmp_path)
    _approve(root)

    event = capabilities.admit_stage(
        True, "scanned", ack="", expected="", project_root=root,
    )

    assert event["kind"] == "silent_degrade"
    assert "没法核" in event["detail"]


def test_an_empty_gate_is_not_a_bypass(tmp_path: Path):
    """门禁本来就是空的，这个参数没绕开任何东西；记成偏差会污染统计。"""
    assert capabilities.admit_stage(
        True, "", project_root=str(tmp_path),
    ) is None


def test_digest_tracks_content_not_path(tmp_path: Path):
    path = tmp_path / "a.md"
    path.write_text("第一版", encoding="utf-8")
    first = capabilities.digest_file(path)
    path.write_text("第二版", encoding="utf-8")

    assert first and first != capabilities.digest_file(path)
    assert capabilities.digest_file(tmp_path / "missing.md") == ""


# ---------------------------------------------------------------- 硬校验：准入下限


def _loosen(root: str) -> None:
    (Path(root) / "code-hospital.yaml").write_text(
        "admission:\n  report:\n    require_location: false\n    block_clearance: false\n",
        encoding="utf-8",
    )


def test_customer_config_can_loosen_until_a_rule_is_hardened(tmp_path: Path):
    root = str(tmp_path)
    _loosen(root)

    rules = admission.rules_for(root, "report")
    assert rules.require_location is False
    assert rules.block_clearance is False


def test_repo_config_cannot_turn_off_a_hardened_rule(tmp_path: Path):
    """被绕过三次才加的校验，用一行配置就能关掉的话，等于没加。"""
    root = str(tmp_path)
    _loosen(root)
    _approve(root, "location_floor")

    rules = admission.rules_for(root, "report")
    assert rules.require_location is True
    assert rules.block_clearance is True


def test_the_floor_does_not_tighten_flags_it_was_not_about(tmp_path: Path):
    root = str(tmp_path)
    _approve(root, "location_floor")

    rules = admission.rules_for(root, "report")
    assert rules.partition is True
    assert rules.clues_only is False


# ---------------------------------------------------------------- 端到端


def _repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


async def _past_cases(root: str, times: int) -> None:
    for _ in range(times):
        reg = await reception.register(
            title="旧病例", scope_type="repo", scope={}, project_root=root,
        )
        async with Database(root) as db:
            await db.update_case(
                reg["case_id"], status="discharged",
                coverage_json=json.dumps({
                    "discipline": [_book("gate_bypass", 1, "scanned")],
                }, ensure_ascii=False),
            )


class _Caps:
    elicitation = object()


class _Answer:
    def __init__(self, choice: str):
        self.choice = choice
        self.text = ""


class _Result:
    def __init__(self, action: str):
        self.action = action
        self.data = _Answer("approve")


class _Host:
    """声明了 elicitation 的宿主：当场弹卡片，用户那一下就是回执。"""

    def __init__(self, action: str = "accept"):
        self.client_capabilities = _Caps()
        self.protocol_version = "2026-07-28"
        self.action = action

    async def elicit(self, message, schema):
        return _Result(self.action)


async def test_bypasses_explained_by_memory_become_a_block(wired, tmp_path: Path):
    """从「记了三次」到「绕过就过不去」，中间隔一次人签字 + 一条经验。"""
    from code_hospital.api import mcp_server

    root = _repo(tmp_path)
    await _past_cases(root, 3)

    listed = await mcp_server._rule("list", "", "", "", "", False, root)
    assert listed["deviations"][0]["kind"] == "gate_bypass"
    assert listed["deviations"][0]["count"] == 3
    assert [hit["id"] for hit in listed["memory"]["hits"]] == [REF]

    asked = await mcp_server._rule("approve", "gate_receipt", REF, "", "", False, root)
    assert asked["status"] == "pending_question"
    assert asked["question"]["kind"] == "rule_approve"

    wrong = "ack-000000000000"
    with pytest.raises(HospitalError):
        await mcp_server._rule("approve", "gate_receipt", REF, wrong, "", True, root)

    good = discipline.digest("gate_receipt", REF)
    approved = await mcp_server._rule("approve", "gate_receipt", REF, good, "", True, root)
    assert approved["status"] == "approved"

    reg = await reception.register(
        title="新病例", scope_type="repo", scope={}, project_root=root,
    )
    digest = reg["handoff"]["artifact_digest"]
    assert digest

    with pytest.raises(HospitalError) as caught:
        await scanner.scan(case_id=reg["case_id"], stage_confirmed=True, project_root=root)
    assert caught.value.target == "ack"

    scanned = await scanner.scan(
        case_id=reg["case_id"], stage_confirmed=True, ack=digest, project_root=root,
    )
    assert Path(scanned["report_path"]).is_file()
    assert REF in Path(scanned["report_path"]).read_text(encoding="utf-8")


async def test_the_ack_binds_the_ground_not_just_the_check(wired, tmp_path: Path):
    """换一条依据就是另一件事，凭据跟着变。"""
    assert discipline.digest("gate_receipt", "mem-a") != discipline.digest("gate_receipt", "mem-b")


async def test_rejecting_is_just_not_approving(wired, tmp_path: Path):
    """拒绝不记账：记「被拒过几次」是记忆层的策略，医院抄一份必然分叉。"""
    from code_hospital.api import mcp_server

    root = _repo(tmp_path)
    await _past_cases(root, 3)

    with pytest.raises(HospitalError) as caught:
        await mcp_server._rule("reject", "gate_receipt", REF, "", "先不改", False, root)

    assert caught.value.target == "action"
    assert discipline.requires(root, "gate_receipt") is False


async def test_the_case_query_hands_back_a_lost_digest(tmp_path: Path):
    """宿主把摘要弄丢了不能就此卡死——这是它唯一的正路。"""
    from code_hospital.api import mcp_server

    root = _repo(tmp_path)
    reg = await reception.register(
        title="补摘要", scope_type="repo", scope={}, project_root=root,
    )

    got = await mcp_server._case_query(
        reg["case_id"], "", "summary", "", 1, 50, "created_at desc", root,
    )

    assert got["gate"]["artifact"] == "挂号单"
    assert got["gate"]["artifact_digest"] == reg["handoff"]["artifact_digest"]
    assert got["gate"]["requires_ack"] is False


async def test_in_call_approval_needs_no_ack(wired, tmp_path: Path):
    """已经证明「人看过了」，再要一个证明字段就是多余的摩擦。"""
    from code_hospital.api import mcp_server

    root = _repo(tmp_path)
    await _past_cases(root, 3)
    token = capabilities.bind_host(_Host())
    try:
        got = await mcp_server._rule("approve", "gate_receipt", REF, "", "", False, root)
    finally:
        capabilities.reset_host(token)

    assert got["status"] == "approved"
    assert got["rule"]["channel"] == "elicitation"


async def test_declining_in_call_changes_nothing(wired, tmp_path: Path):
    """用户在卡片上说不，就是没批。医院不为这次「不」留任何记录。"""
    from code_hospital.api import mcp_server

    root = _repo(tmp_path)
    await _past_cases(root, 3)
    token = capabilities.bind_host(_Host(action="decline"))
    try:
        got = await mcp_server._rule("approve", "gate_receipt", REF, "", "", False, root)
    finally:
        capabilities.reset_host(token)

    assert got["status"] == "declined"
    assert discipline.requires(root, "gate_receipt") is False
    assert discipline.state(root)["history"] == []
