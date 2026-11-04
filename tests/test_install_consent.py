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

    got = _add(root, path, question_id=asked["question"]["id"], answer="装吧，先看影子期")

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
    _add(root, path, question_id=qid, answer="装")

    with pytest.raises(HospitalError) as caught:
        _add(root, path, question_id=qid, answer="装")
    assert str(caught.value).find("用过了") >= 0 or "没发过" in str(caught.value)


def test_a_question_without_an_answer_is_refused(tmp_path):
    """有回执但没原话，等于没问。不许宿主自己编一句。"""
    root = str(tmp_path)
    path = _manifest(root)
    qid = _add(root, path)["question"]["id"]

    with pytest.raises(HospitalError) as caught:
        _add(root, path, question_id=qid, answer="  ")
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
        _add(root, path, question_id=qid, answer="装")
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
