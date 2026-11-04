"""装第三方仪器必须先问人。

这条账的性质和第 6 步的 `gate_receipt` 一样：医院能保证的是「没有回执就不装」，
保证不了「人真的看过了」——后面这一半任何机制都做不到，所以只记账，不假装。

五件事：
1. 第一次 add 不写任何东西，只返回 pending_question；
2. 带回 question_id + 用户原话才装，原话落进仪器记录；
3. 票据一次性，且 24 小时过期；
4. 启事与安装接得上：装了就在报告里点名「已招 X，第几轮」；
5. 一条启事一个扩展名，装一台不会把别的类型一起算成关掉。
"""

import asyncio
import json
from pathlib import Path

import pytest

from code_hospital.api.mcp_server import _market
from code_hospital.catalog import ledger, market
from code_hospital.core.errors import HospitalError

MANIFEST = """
api: code-hospital/v1
kind: instrument
id: acme-kt
name: Acme Kotlin 检查
vendor: acme
version: "0.1.0"
concern: Kotlin 未使用变量
extensions: [kt]
triage: [outpatient, checkup]
specialty_id: kotlin
transport: mcp
command: acme
tool: lint
risk:
  reads_repo: true
  writes_repo: false
"""


def _manifest(root: str, ident: str = "acme-kt", ext: str = "kt") -> Path:
    path = Path(root) / f"{ident}.yaml"
    path.write_text(
        MANIFEST.replace("acme-kt", ident).replace("[kt]", f"[{ext}]"), encoding="utf-8"
    )
    return path


def _add(root: str, path: Path, **kwargs) -> dict:
    return asyncio.run(_market("add", str(path), "", False, root, **kwargs))


def _index(root: str) -> dict:
    path = Path(root) / market.MARKET_DIR / "index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def test_the_first_add_installs_nothing(tmp_path):
    """没有回执就不装。第一步只把设备摆出来问一句。"""
    root = str(tmp_path)
    got = _add(root, _manifest(root))

    assert got["status"] == "pending_question"
    assert got["question"]["kind"] == "install_instrument"
    assert got["question"]["blocking"] is True
    assert ".kt" in got["question"]["prompt"]
    # 什么都没落盘：manifest 没抄进来，索引没建。
    base = Path(root) / market.MARKET_DIR
    assert not (base / "manifests").is_dir()
    assert not (base / "index.json").is_file()


def test_the_answer_comes_back_and_gets_recorded(tmp_path):
    """带回用户原话才装；原话要留在仪器记录上，事后答得出「谁同意的」。"""
    root = str(tmp_path)
    path = _manifest(root)
    asked = _add(root, path)

    got = _add(root, path, question_id=asked["question"]["id"], answer="装吧，先看影子期", choice="install")

    assert got["ok"] is True
    assert got["tier"] == "probation"
    consent = _index(root)["acme-kt"]["consent"]
    assert consent["answer"] == "装吧，先看影子期"
    assert consent["via"] == "convention"


def test_a_ticket_cannot_be_used_twice(tmp_path):
    """一张回执装一台。复用别人的回执不算数。"""
    root = str(tmp_path)
    path = _manifest(root)
    qid = _add(root, path)["question"]["id"]
    _add(root, path, question_id=qid, answer="装", choice="install")

    with pytest.raises(HospitalError) as caught:
        _add(root, path, question_id=qid, answer="装", choice="install")
    assert str(caught.value).find("用过了") >= 0 or "没发过" in str(caught.value)


def test_a_question_without_an_answer_is_refused(tmp_path):
    """有回执但没原话，等于没问。不许宿主自己编一句。"""
    root = str(tmp_path)
    path = _manifest(root)
    qid = _add(root, path)["question"]["id"]

    with pytest.raises(HospitalError) as caught:
        _add(root, path, question_id=qid, answer="  ", choice="install")
    assert "answer" in str(caught.value) or "人点头" in str(caught.value)


def test_a_stale_ticket_expires(tmp_path):
    """24 小时前的回执作废，否则「上次问过」会永远有效。"""
    root = str(tmp_path)
    path = _manifest(root)
    qid = _add(root, path)["question"]["id"]

    consent_file = Path(root) / market.MARKET_DIR / "consent.json"
    data = json.loads(consent_file.read_text(encoding="utf-8"))
    data[qid]["at"] = 0
    consent_file.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(HospitalError) as caught:
        _add(root, path, question_id=qid, answer="装", choice="install")
    assert "作废" in str(caught.value)


def test_a_manifest_that_fails_validation_never_reaches_the_question(tmp_path):
    """清单本身不合法就别拿去问用户——问他一个装不了的东西是浪费。"""
    root = str(tmp_path)
    path = Path(root) / "bad.yaml"
    path.write_text(MANIFEST.replace("mcp", "builtin"), encoding="utf-8")

    with pytest.raises(HospitalError) as caught:
        _add(root, path)
    assert "builtin" in str(caught.value)


def test_the_notice_names_the_instrument_once_it_is_hired(tmp_path):
    """启事与安装接上：装了以后报告里看得出「已招 X，影子期第几轮」。"""
    root = str(tmp_path)
    coverage: dict = {}
    ledger._record_instrument_gap(
        coverage, ["a.kt", "b.kt", "c.kt"], [], "case_1", root, set()
    )

    before = "\n".join(ledger.recruitment_lines(coverage, root))
    assert "招聘启事" in before and "要关掉这一类" in before

    assert market.install(root, _manifest(root))["ok"]
    after = "\n".join(ledger.recruitment_lines(coverage, root))
    assert "已招 acme-kt" in after
    assert "影子期第 0 轮" in after
    # 影子期的结论不进报告，所以「仍记为未查」这句必须在。
    assert "仍记为未查" in after


def test_one_extension_is_one_requisition(tmp_path):
    """一台仪器查不了一门语言以上：缺口按扩展名拆，启事也按扩展名发。"""
    root = str(tmp_path)
    coverage: dict = {}
    ledger._record_instrument_gap(
        coverage,
        ["a.kt", "b.kt", "c.kt", "x.rb", "y.rb"],
        [], "case_1", root, set(),
    )

    got = ledger.requisitions(coverage)
    # 缺口都记下来了，但只有够门槛的才发启事：.rb 只有 2 个文件，
    # 还不到「值得专门招一台」——一个 .rb 可能是误放进来的一颗钉子。
    assert [item["extensions"] for item in got] == [["kt"]]
    assert [item["file_count"] for item in got] == [3]
    gaps = {r["evidence"]["extensions"][0] for r in coverage["capability_gap"]}
    assert gaps == {"kt", "rb"}
    lines = "\n".join(ledger.recruitment_lines(coverage, root))
    assert ".kt" in lines
    assert ".rb" not in lines


def test_a_gap_keeps_its_identity_when_the_missing_set_changes(tmp_path):
    """补上一类之后，另一类的缺口 id 不能跟着变——否则这条启事追不住。"""
    root = str(tmp_path)
    first: dict = {}
    ledger._record_instrument_gap(
        first, ["a.kt", "b.kt", "c.kt", "x.rb"], [], "case_1", root, set()
    )
    second: dict = {}
    ledger._record_instrument_gap(second, ["a.kt", "b.kt", "c.kt"], [], "case_2", root, set())

    kt_first = next(r for r in first["capability_gap"] if "*.kt" in r["evidence"]["signals"])
    kt_second = next(r for r in second["capability_gap"] if "*.kt" in r["evidence"]["signals"])
    assert kt_first["id"] == kt_second["id"]


# --- 用户选了什么，必须由 choice 说，不能从原话里猜 -------------------------


def test_prose_alone_never_installs(tmp_path):
    """只带回原话、不说 choices，一律拒绝。

    「先别装，等等看」和「装吧，别等了」都含「装」字。医院读不出分别，
    也不该去读：猜错一次就是替用户签了字。
    """
    root = str(tmp_path)
    path = _manifest(root)
    qid = _add(root, path)["question"]["id"]

    with pytest.raises(HospitalError) as caught:
        _add(root, path, question_id=qid, answer="先别装，等等看")
    assert "choice" in str(caught.value)


def test_saying_skip_installs_nothing(tmp_path):
    """用户选「先不装」，医院就不装。票据核销掉，别留着当通行证。"""
    root = str(tmp_path)
    path = _manifest(root)
    qid = _add(root, path)["question"]["id"]

    got = _add(root, path, question_id=qid, choice="skip", answer="先不装")

    assert got["ok"] is False
    assert got["choice"] == "skip"
    assert not _index(root)
    # 票据一次性：跳过也是一次回答，用掉就没了。
    assert not (Path(root) / market.MARKET_DIR / "consent.json").read_text(encoding="utf-8").strip(
        "{} \n"
    )


def test_saying_pause_installs_nothing(tmp_path):
    """「暂停，稍后再定」最容易被当成装——这正是要 choice 的理由。"""
    root = str(tmp_path)
    path = _manifest(root)
    qid = _add(root, path)["question"]["id"]

    got = _add(root, path, question_id=qid, choice="pause", answer="暂停一下，我再想想")

    assert got["ok"] is False
    assert got["choice"] == "pause"
    assert not _index(root)


def test_an_invented_choice_is_refused(tmp_path):
    """宿主自己归纳成「yes」这类词不算数。"""
    root = str(tmp_path)
    path = _manifest(root)
    qid = _add(root, path)["question"]["id"]

    with pytest.raises(HospitalError) as caught:
        _add(root, path, question_id=qid, choice="yes", answer="装机")
    assert "认不出来" in str(caught.value)


# --- 启事与安装：按启事 id 绑定，且在装的时候校验 ----------------------------


def _case_with_gap(root: str, files: list[str]) -> str:
    """建一个带招聘启事的病例，返回那条启事的 gap_id。"""
    from code_hospital.core.models import Case, Scope
    from code_hospital.storage.db import Database

    coverage: dict = {}
    ledger._record_instrument_gap(coverage, files, [], "case_1", root, set())
    case = Case(
        id="case_1", project_root=root, title="招聘", scope_type="repo",
        scope=Scope(), triage_level="checkup", status="scanned",
        created_at=1, updated_at=1, coverage=coverage,
    )

    async def _save() -> None:
        async with Database(root) as db:
            await db.insert_case(case)

    asyncio.run(_save())
    return ledger.requisitions(coverage)[0]["gap_id"]


def test_a_requisition_that_does_not_exist_is_refused(tmp_path):
    """编一个 gap id 不能过关。对不上就报错，不悄悄当成没提。"""
    root = str(tmp_path)
    path = _manifest(root)

    with pytest.raises(HospitalError) as caught:
        _add(root, path, requisition_id="gap-0000000000")
    assert "没有这条启事" in str(caught.value)


def test_an_instrument_that_cannot_cover_the_requisition_is_refused(tmp_path):
    """声明只查 .py 的仪器，不能记成把 .kt 那条启事关掉了。

    装上了却查不动，报告从此不再提这件事——文件仍然没人查，
    而账上写着「已招到」。这是整条闭环里最难看出来的一种错。
    """
    root = str(tmp_path)
    gap_id = _case_with_gap(root, ["a.kt", "b.kt", "c.kt"])
    path = _manifest(root, ident="acme-py", ext="py")

    with pytest.raises(HospitalError) as caught:
        _add(root, path, requisition_id=gap_id)
    assert "关不掉那条启事" in str(caught.value)
    assert not _index(root), "没装成就不该留下记录"


def test_a_hired_instrument_records_which_requisition_it_answered(tmp_path):
    """装成功就写下「回应的是哪条启事」，并只有那一条认它为已招。"""
    root = str(tmp_path)
    gap_id = _case_with_gap(root, ["a.kt", "b.kt", "c.kt"])
    path = _manifest(root)
    qid = _add(root, path, requisition_id=gap_id)["question"]["id"]

    got = _add(root, path, question_id=qid, choice="install",
               answer="装，把 .kt 补上", requisition_id=gap_id)

    assert got["ok"] is True
    bound = _index(root)["acme-kt"]["requisition"]
    assert bound["gap_id"] == gap_id
    assert bound["extensions"] == ["kt"]
    assert market.claimed_by(root, gap_id)["instrument"] == "acme-kt"
    # 别的启事不认这台：它没承诺过那一条。
    assert market.claimed_by(root, "gap-somethingelse") is None


def test_the_case_tool_hands_back_the_gap_id_to_install_against(tmp_path):
    """宿主从哪拿 requisition_id：只能从这儿原样抄，不能自己编一个。"""
    from code_hospital.api.mcp_server import _case_query

    root = str(tmp_path)
    gap_id = _case_with_gap(root, ["a.kt", "b.kt", "c.kt"])

    got = asyncio.run(
        _case_query("case_1", "", "requisitions", "", 1, 50, "created_at desc", root)
    )

    rows = got["requisitions"]
    assert [row["gap_id"] for row in rows] == [gap_id]
    assert rows[0]["extensions"] == ["kt"]
    assert rows[0]["file_count"] == 3
    assert rows[0]["claimed_by"] is None
    assert gap_id in rows[0]["install_hint"]
