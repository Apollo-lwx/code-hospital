"""结论来源：仪器与宿主交卷分开计数，疗效和复发不收宿主交卷。"""

import sqlite3

import pytest

from code_hospital.adapters.base import RawFinding
from code_hospital.catalog.coverage import stamp_source_counts
from code_hospital.catalog.formulary import concluded_by_for
from code_hospital.core.models import (
    Case,
    Diagnosis,
    Finding,
    Scope,
    classify_source,
    source_summary,
)
from code_hospital.departments import discharge
from code_hospital.departments.diagnostician import cluster_findings
from code_hospital.departments.scanner import normalize
from code_hospital.storage.db import Database
from code_hospital.storage.reports import render_diagnose_report, render_scan_report


def _case(cid: str = "case_x", root: str = ".") -> Case:
    return Case(
        id=cid, project_root=root, title="示例", scope_type="repo",
        scope=Scope(), triage_level="checkup", status="scanned",
        created_at=1_700_000_000_000, updated_at=1_700_000_000_000,
    )


def _finding(**kwargs) -> Finding:
    base = dict(
        id="fnd_1", case_id="case_x", report_id="rpt_1", tool="ruff", rule_id="F401",
        severity="nit", file_path="a.py", line_start=1, line_end=1,
        message="unused", evidence="import os", fingerprint="sha256:" + "a" * 64,
        content_key="sha256:" + "b" * 64, concluded_by="instrument",
    )
    base.update(kwargs)
    return Finding(**base)


def test_source_labels():
    assert classify_source("ruff", "F401", "ruff") == "instrument"
    assert classify_source("host", "diff.issue") == "host_llm"
    assert classify_source("java-code-review", "sql", "external") == "external"
    assert concluded_by_for("ruff", "F401") == "instrument"
    assert concluded_by_for("host", "diff.issue") == "host_llm"


def test_normalize_marks_host_and_instrument(tmp_path):
    (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")
    host = normalize(
        RawFinding(rule_id="diff.issue", message="这里要改", file_path="a.py", line_start=1, severity="major"),
        "host", "case_x", "rpt_x", str(tmp_path),
    )
    instrument = normalize(
        RawFinding(rule_id="F401", message="unused", file_path="a.py", line_start=1, severity="major"),
        "ruff", "case_x", "rpt_x", str(tmp_path),
    )
    assert host.concluded_by == "host_llm"
    assert instrument.concluded_by == "instrument"


def test_scan_report_counts_sources_and_ranks_host_last():
    case = _case()
    host = _finding(
        id="fnd_host", tool="host", rule_id="diff.issue", severity="critical",
        file_path="z.py", concluded_by="host_llm", fingerprint="sha256:" + "c" * 64,
        message="宿主看到的问题",
    )
    instrument = _finding(id="fnd_ruff", severity="nit", file_path="a.py")
    md = render_scan_report(case, "rpt_1", [], [host, instrument], [])
    assert "仪器 1 条 / 模型 1 条" in md
    assert "待核实线索" in md
    detail = md.split("## 发现明细", 1)[1]
    assert detail.find("a.py") < detail.find("z.py")
    summary = md.split("## 概要", 1)[1].split("##", 1)[0]
    assert "| 全仓库 | 2 | 0 |" in summary


def test_diagnose_report_splits_pending_leads():
    case = _case()
    instrument = _finding()
    host = _finding(
        id="fnd_host", tool="host", rule_id="diff.issue", severity="critical",
        file_path="z.py", concluded_by="host_llm", message="宿主看到的问题",
    )
    instrument_dx = Diagnosis(
        id="dia_1", case_id=case.id, report_id="rpt_1", fingerprint=instrument.fingerprint,
        category="F401", root_cause="没用到", severity="nit", confidence=0.9,
        fixability="manual", member_ids=[instrument.id], concluded_by="code",
    )
    host_dx = Diagnosis(
        id="dia_2", case_id=case.id, report_id="rpt_1", fingerprint=host.fingerprint,
        category="diff.issue", root_cause="宿主交卷", severity="critical", confidence=0.4,
        fixability="manual", member_ids=[host.id], concluded_by="host_llm",
    )
    md = render_diagnose_report(
        case, "rpt_1", [(host_dx, [host]), (instrument_dx, [instrument])],
        {"total_findings": 2, "clusters": 2, "autofix": 0, "llm_patch": 0, "manual": 2},
    )
    assert "仪器 1 条 / 模型 1 条" in md
    body = md.split("## 问题分组", 1)[1]
    pending = body.split("## 待核实线索", 1)
    assert "F401" in pending[0]
    assert "diff.issue" in pending[1]
    assert "这些是宿主交卷。" in pending[1]


def test_semantic_conclusion_does_not_share_an_instrument_diagnosis():
    host = _finding(
        id="fnd_host", tool="host", rule_id="F841", concluded_by="host_llm",
        file_path="src/a.py", line_start=2, line_end=2, symbol="pay",
        fingerprint="sha256:" + "c" * 64,
    )
    instrument = _finding(
        id="fnd_ruff", tool="ruff", rule_id="F841", concluded_by="instrument",
        file_path="src/a.py", line_start=2, line_end=2, symbol="pay",
    )
    clusters = cluster_findings([instrument, host], "case_x", "rpt_1", {})
    by_source = {diagnosis.concluded_by: members for diagnosis, members in clusters}
    assert [item.id for item in by_source["host_llm"]] == ["fnd_host"]
    assert [item.id for item in by_source["code"]] == ["fnd_ruff"]


def test_cluster_puts_host_llm_after_instruments(tmp_path):
    instrument = _finding(case_id="case_x", severity="nit", file_path="a.py", line_start=2)
    host = _finding(
        id="fnd_host", case_id="case_x", tool="host", rule_id="diff.issue",
        severity="critical", file_path="z.py", concluded_by="host_llm",
        fingerprint="sha256:" + "c" * 64, content_key="sha256:" + "d" * 64,
    )
    clusters = cluster_findings([host, instrument], "case_x", "rpt_1", {}, str(tmp_path))
    assert [pair[0].concluded_by for pair in clusters] == ["code", "host_llm"]
    assert clusters[-1][1][0].rule_id == "diff.issue"


def test_stamp_source_counts_lands_on_the_ledger():
    ledger = stamp_source_counts({"units": []}, [
        _finding(),
        _finding(id="fnd_host", concluded_by="host_llm", tool="host", rule_id="diff.issue"),
    ])
    assert ledger["concluded_by_counts"] == {
        "instrument": 1, "host_llm": 1, "external": 0, "code": 0,
    }
    assert source_summary([_finding()]) == "仪器 1 条 / 模型 0 条"


@pytest.mark.asyncio
async def test_history_and_discharge_skip_host_filings(tmp_path):
    root = str(tmp_path)
    old = _case("case_old", root)
    old.status = "discharged"
    new = _case("case_new", root)
    shared = "sha256:" + "a" * 64
    host_fp = "sha256:" + "e" * 64
    past_instrument = _finding(
        id="fnd_past", case_id=old.id, fingerprint=shared, content_key=shared,
    )
    past_host = _finding(
        id="fnd_past_host", case_id=old.id, tool="host", rule_id="diff.issue",
        concluded_by="host_llm", fingerprint=host_fp, content_key=host_fp,
        severity="critical", file_path="z.py",
    )
    instrument = _finding(
        id="fnd_now", case_id=new.id, fingerprint=shared, content_key=shared,
    )
    host = _finding(
        id="fnd_host", case_id=new.id, tool="host", rule_id="diff.issue",
        concluded_by="host_llm", fingerprint=host_fp, content_key=host_fp,
        severity="critical", file_path="z.py",
    )
    async with Database(root) as db:
        await db.insert_case(old)
        await db.insert_case(new)
        await db.insert_findings([past_instrument, past_host, instrument, host])
        history = await db.find_fingerprint_history(
            root, [shared, host_fp], exclude_case_id=new.id, content_keys=[shared, host_fp],
        )
        page, _total = await db.list_findings(new.id, order_by="severity desc")
        buckets, _devices = await discharge._compare(db, new, [instrument])
    assert shared in history
    assert host_fp not in history
    assert [item.id for item in page] == ["fnd_now", "fnd_host"]
    seen = {item.fingerprint for group in buckets.values() for item in group}
    assert host_fp not in seen
    assert shared in seen


@pytest.mark.asyncio
async def test_migrate_backfills_host_rows(tmp_path):
    hospital = tmp_path / ".hospital"
    hospital.mkdir()
    db_path = hospital / "hospital.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE findings (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            report_id TEXT NOT NULL,
            tool TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            severity TEXT NOT NULL,
            file_path TEXT NOT NULL,
            line_start INTEGER NOT NULL DEFAULT 0,
            line_end INTEGER NOT NULL DEFAULT 0,
            symbol TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL,
            content_key TEXT NOT NULL DEFAULT '',
            context_anchor TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            origin TEXT NOT NULL DEFAULT 'initial',
            first_seen_round INTEGER NOT NULL DEFAULT 1,
            last_seen_round INTEGER NOT NULL DEFAULT 1,
            contract_id TEXT NOT NULL DEFAULT '',
            fixable INTEGER NOT NULL DEFAULT 0,
            verdict TEXT NOT NULL DEFAULT '',
            unresolved TEXT NOT NULL DEFAULT '',
            attack_class TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        "INSERT INTO findings (id, case_id, report_id, tool, rule_id, severity, file_path,"
        " fingerprint, message) VALUES (?,?,?,?,?,?,?,?,?)",
        ("fnd_old", "case_old", "rpt_1", "host", "diff.issue", "major", "a.py", "sha256:" + "a" * 64, "交卷"),
    )
    conn.commit()
    conn.close()
    async with Database(str(tmp_path)) as db:
        rows = await db.all_findings("case_old")
    assert rows[0].concluded_by == "host_llm"
    assert rows[0].origin == "initial"
