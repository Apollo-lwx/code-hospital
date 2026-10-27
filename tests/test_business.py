"""业务号：只核对写下来的规则。没写规则就是未查。"""

import subprocess
from pathlib import Path

import pytest

from code_hospital.catalog.business import scan_business
from code_hospital.catalog.formulary import match_finding
from code_hospital.departments import reception, scanner

_RULES = """
catalog:
  business:
    machines:
      - id: order
        field: status
        states: [draft, paid, shipped, cancelled]
        transitions:
          - {from: draft, to: paid}
          - {from: paid, to: [shipped, cancelled]}
    money_fields: [amount, price]
    auth:
      checks: [require_user]
      writes: [create_, update_]
    invariants:
      - id: refund-balance
        text: 退款必须把余额加回去
"""

_CODE = """
def create_order(amount: float):
    if status == "draft":
        status = "shipped"
    price = 1.25

def update_paid(order):
    require_user()
    if order.status == "paid":
        order.status = "shipped"

@require_user
def update_note():
    return 1

def create_ok():
    require_user()
    if status == "draft":
        status = "paid"
"""


def _ids(raws: list[dict]) -> set[str]:
    return {row["rule_id"] for row in raws}


def test_mechanical_rules_and_pending_invariant(tmp_path: Path):
    (tmp_path / "code-hospital.yaml").write_text(_RULES, encoding="utf-8")
    (tmp_path / "order.py").write_text(_CODE, encoding="utf-8")
    (tmp_path / "Bill.java").write_text("class Bill { double amount; }\n", encoding="utf-8")
    raws, limits, outcome = scan_business(
        str(tmp_path), ["order.py", "Bill.java"], chief_complaint="优惠不能算两次",
    )
    assert outcome == "ok"
    rules = _ids(raws)
    assert "business.illegal-transition" in rules
    assert "business.money-float" in rules
    assert "business.missing-auth" in rules
    assert "business.unverified-invariant" in rules
    assert any("优惠不能算两次" in row["message"] for row in raws)
    assert any("业务号已核对" in item for item in limits)
    assert any("待复核" in item for item in limits)
    legal = [row for row in raws if row["rule_id"] == "business.illegal-transition"]
    assert len(legal) == 1
    assert "draft" in legal[0]["message"] and "shipped" in legal[0]["message"]
    missing = {row["symbol"] for row in raws if row["rule_id"] == "business.missing-auth"}
    assert missing == {"create_order"}


def test_missing_rules_are_not_a_clean_bill(tmp_path: Path):
    (tmp_path / "order.py").write_text("status = 'shipped'\n", encoding="utf-8")
    raws, limits, outcome = scan_business(str(tmp_path), ["order.py"])
    assert raws == []
    assert outcome == "terminal"
    assert any(item.startswith("业务未查") for item in limits)


def test_business_rules_enter_review_not_an_automatic_fix():
    assert match_finding("business-ruler", "business.illegal-transition", "major").formula.id == "repair"
    assert match_finding("business-ruler", "business.money-float", "major").formula.id == "repair"
    assert match_finding("business-ruler", "business.missing-auth", "major").formula.id == "repair"
    pending = match_finding("business-ruler", "business.unverified-invariant", "info")
    assert pending.matched is False
    assert pending.confidence < 0.5


@pytest.mark.skipif(subprocess.run(["git", "--version"], check=False).returncode != 0, reason="需要 git")
async def test_business_visit_records_the_complaint(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("def create_order():\n    return 1\n", encoding="utf-8")
    (tmp_path / "code-hospital.yaml").write_text(
        "catalog:\n  business:\n    auth:\n      checks: [require_user]\n      writes: [create_]\n",
        encoding="utf-8",
    )
    opened = await reception.register(
        title="业务", scope_type="paths", scope={"paths": ["a.py"]},
        visit_kind="business", chief_complaint="退款必须回余额",
        project_root=str(tmp_path),
    )
    scanned = await scanner.scan(
        case_id=opened["case_id"], stage_confirmed=True, project_root=str(tmp_path),
    )
    rules = {row["rule_id"] for row in scanned["findings"]["data"]}
    assert "business.missing-auth" in rules
    assert "business.unverified-invariant" in rules
    assert any(row["tool"] == "business-ruler" and row["outcome"] == "ok" for row in scanned["devices"])
