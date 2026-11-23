"""会诊转诊单：R1–R4 生成、可及判定、referred_from 链路（转诊单草案验收断言）。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_hospital.catalog.formulary import match_finding
from code_hospital.catalog.layers import scan_layers
from code_hospital.catalog.referrals import attach_referrals, resolve_target
from code_hospital.core.errors import HospitalError
from code_hospital.core.models import Diagnosis, Finding
from code_hospital.departments import diagnostician, reception, scanner
from code_hospital.storage.db import Database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def _finding(rule_id: str, message: str, file_path: str = "src/a.py", **kw) -> Finding:
    return Finding(
        id=kw.pop("id", f"fnd-{rule_id}-{abs(hash(message)) % 10000}"),
        case_id="case-x", report_id="rpt-x", tool=kw.pop("tool", "layer-ruler"),
        rule_id=rule_id, severity=kw.pop("severity", "info"),
        file_path=file_path, line_start=1, line_end=1,
        message=message, fingerprint=f"fp-{rule_id}", **kw,
    )


def _diagnosis(members: list[Finding]) -> tuple:
    return (
        Diagnosis(
            id="dia-x", case_id="case-x", report_id="rpt-x",
            fingerprint=members[0].fingerprint, category=members[0].rule_id,
            root_cause="", severity=members[0].severity, confidence=0.7,
            fixability="manual", member_ids=[m.id for m in members],
        ),
        members,
    )


# ---------- 可及判定 ----------


def test_resolve_target_local_dir(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (tmp_path / "shared").mkdir()
    resolved, local = resolve_target("../shared", str(root))
    assert local is True
    assert Path(resolved).name == "shared"


def test_resolve_target_rejects_url_and_missing(tmp_path: Path):
    assert resolve_target("https://example.com/repo", str(tmp_path)) == ("", False)
    assert resolve_target("git@github.com:x/y.git", str(tmp_path)) == ("", False)
    assert resolve_target("../not-there", str(tmp_path)) == ("", False)
    assert resolve_target("", str(tmp_path)) == ("", False)


# ---------- R1 信号：layer.external-import ----------


async def test_external_import_flagged_when_sibling_repo_exists(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "shared").mkdir()  # 兄弟仓目录
    (proj / "code-hospital.yaml").write_text(
        "catalog:\n  layers:\n    - api\n    - storage\n", encoding="utf-8",
    )
    (proj / "storage").mkdir()
    (proj / "storage" / "db.py").write_text(
        "import shared.utils\nfrom .local import thing\nimport os\n", encoding="utf-8",
    )
    raws, limits = await scan_layers(str(proj), ["storage/db.py"])
    assert limits == []
    external = [r for r in raws if r["rule_id"] == "layer.external-import"]
    assert len(external) == 1
    assert external[0]["symbol"] == "shared.utils"
    assert external[0]["severity"] == "info"
    assert "shared" in external[0]["message"]


async def test_external_import_not_flagged_without_sibling(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "code-hospital.yaml").write_text(
        "catalog:\n  layers:\n    - api\n    - storage\n", encoding="utf-8",
    )
    (proj / "storage").mkdir()
    (proj / "storage" / "db.py").write_text("import requests\nimport os\n", encoding="utf-8")
    raws, _limits = await scan_layers(str(proj), ["storage/db.py"])
    assert [r for r in raws if r["rule_id"] == "layer.external-import"] == []


def test_external_import_is_review_only_not_relocate():
    matched = match_finding("layer-ruler", "layer.external-import", "info")
    assert matched.formula is None
    assert matched.matched is False
    assert matched.confidence < 0.5
    assert matched.specialty_id == "architecture"


# ---------- R1–R3 生成 ----------


def test_r1_referral_generated_and_local(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (tmp_path / "shared").mkdir()
    finding = _finding(
        "layer.external-import", "import 目标 shared.utils 落在仓外兄弟目录 shared",
        symbol="shared.utils",
    )
    diagnosis, members = _diagnosis([finding])
    attach_referrals([(diagnosis, members)], str(root))
    assert len(diagnosis.referrals) == 1
    slip = diagnosis.referrals[0]
    assert slip["rule"] == "R1"
    assert slip["target_repo"] == "../shared"
    assert slip["local"] is True
    assert Path(slip["resolved_path"]).name == "shared"
    assert slip["finding_ids"] == [finding.id]
    assert slip["suggested_triage"] == "outpatient"


def test_r2_supply_dependency_points_upstream(tmp_path: Path):
    finding = _finding(
        "supply.unpinned", "依赖 requests 没有钉死版本",
        file_path="requirements.txt", tool="supply-ruler", severity="minor",
    )
    diagnosis, members = _diagnosis([finding])
    attach_referrals([(diagnosis, members)], str(tmp_path))
    assert len(diagnosis.referrals) == 1
    slip = diagnosis.referrals[0]
    assert slip["rule"] == "R2"
    assert slip["target_repo"] == "requests"
    assert slip["local"] is False
    assert slip["resolved_path"] == ""


def test_r2_skips_findings_without_dep_name(tmp_path: Path):
    finding = _finding(
        "supply.missing-lock", "有 package.json，没有锁文件",
        file_path="package.json", tool="supply-ruler",
    )
    diagnosis, members = _diagnosis([finding])
    attach_referrals([(diagnosis, members)], str(tmp_path))
    assert diagnosis.referrals == []


def test_r3_host_filed_referral(tmp_path: Path):
    (tmp_path / "other-repo").mkdir()
    finding = _finding(
        "diff.issue", "这个问题在别的仓", tool="host", severity="major",
        referrals=[{
            "target_repo": "other-repo",
            "hypothesis": "真正的根因在 other-repo",
            "evidence": "",
            "suggested_triage": "checkup",
        }],
    )
    diagnosis, members = _diagnosis([finding])
    attach_referrals([(diagnosis, members)], str(tmp_path))
    assert len(diagnosis.referrals) == 1
    slip = diagnosis.referrals[0]
    assert slip["rule"] == "R3"
    assert slip["local"] is True
    assert Path(slip["resolved_path"]).name == "other-repo"


def test_r3_does_not_guess(tmp_path: Path):
    finding = _finding("diff.issue", "普通交卷", tool="host", severity="major")
    diagnosis, members = _diagnosis([finding])
    attach_referrals([(diagnosis, members)], str(tmp_path))
    assert diagnosis.referrals == []


def test_one_finding_enters_at_most_one_slip(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (tmp_path / "shared").mkdir()
    finding = _finding(
        "layer.external-import", "import 目标 shared 落在仓外",
        symbol="shared",
        referrals=[{"target_repo": "../shared", "hypothesis": "宿主也点名了",
                    "evidence": "", "suggested_triage": "checkup"}],
    )
    diagnosis, members = _diagnosis([finding])
    attach_referrals([(diagnosis, members)], str(root))
    assert len(diagnosis.referrals) == 1  # R1 先占用，R3 不再重复开单


# ---------- R4：记忆层（替身端口） ----------


async def test_r4_needs_recurrence_and_memory(tmp_path: Path, monkeypatch):
    from code_hospital.catalog import referrals
    from code_hospital.integrations import memory_port

    (tmp_path / "outside-lib").mkdir()
    inside = tmp_path / "proj"
    inside.mkdir()

    finding = _finding("F841", "unused", tool="ruff", severity="major")
    diagnosis, members = _diagnosis([finding])
    diagnosis.history_refs = [{"case_id": "a"}, {"case_id": "b"}, {"case_id": "c"}]

    class _Port:
        channel = "cli"

        async def recall(self, task, top_k=None):
            return {"hits": [{
                "id": "m1", "title": "上次复发", 
                "content": f"根因在 ../other 之外的 {tmp_path / 'outside-lib'} 仓",
            }]}

    monkeypatch.setattr(memory_port, "open_port", lambda root: _async(_Port()))
    pairs = await referrals.r4_slips([(diagnosis, members)], str(inside), set())
    assert len(pairs) == 1
    diag, slip = pairs[0]
    assert diag is diagnosis
    assert slip["rule"] == "R4"
    assert slip["local"] is True
    assert "outside-lib" in slip["resolved_path"]


async def test_r4_silent_when_memory_absent(tmp_path: Path, monkeypatch):
    from code_hospital.catalog import referrals
    from code_hospital.integrations import memory_port

    finding = _finding("F841", "unused", tool="ruff", severity="major")
    diagnosis, members = _diagnosis([finding])
    diagnosis.history_refs = [{"case_id": "a"}, {"case_id": "b"}, {"case_id": "c"}]

    monkeypatch.setattr(
        memory_port, "open_port", lambda root: _async(memory_port.NullPort(str(tmp_path))),
    )
    assert await referrals.r4_slips([(diagnosis, members)], str(tmp_path), set()) == []


async def test_r4_skips_fresh_clusters(tmp_path: Path):
    from code_hospital.catalog import referrals

    finding = _finding("F841", "unused", tool="ruff", severity="major")
    diagnosis, members = _diagnosis([finding])
    assert await referrals.r4_slips([(diagnosis, members)], str(tmp_path), set()) == []


async def _async(value):
    return value


# ---------- 直接转诊：referred_from 链路 ----------


def _mk_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    return str(path)


async def test_referred_from_links_both_cases(tmp_path: Path):
    source = _mk_repo(tmp_path / "source")
    target = _mk_repo(tmp_path / "target")

    first = await reception.register(
        title="源病例", scope_type="staged", scope={},
        auto_advance=True, project_root=source,
    )
    second = await reception.register(
        title="转诊接收", scope_type="staged", scope={},
        auto_advance=True, project_root=target,
        referred_from={"case_id": first["case_id"], "project_root": source},
    )
    assert second["referral"]["from_case"] == first["case_id"]

    async with Database(target) as db:
        links = await db.list_case_links(second["case_id"])
    assert len(links) == 1
    assert links[0]["related_case_id"] == first["case_id"]
    assert links[0]["relation"] == "consultation"

    async with Database(source) as db:
        back = await db.list_case_links(first["case_id"])
    assert len(back) == 1
    assert back[0]["related_case_id"] == second["case_id"]


async def test_referred_from_rejects_unknown_source(tmp_path: Path):
    source = _mk_repo(tmp_path / "source")
    target = _mk_repo(tmp_path / "target")
    first = await reception.register(
        title="源病例", scope_type="staged", scope={},
        auto_advance=True, project_root=source,
    )
    with pytest.raises(HospitalError):
        await reception.register(
            title="坏转诊", scope_type="staged", scope={},
            auto_advance=True, project_root=target,
            referred_from={"case_id": "case-不存在", "project_root": source},
        )
    assert first["case_id"]


async def test_referred_from_rejects_repo_without_hospital(tmp_path: Path):
    bare = _mk_repo(tmp_path / "bare")
    target = _mk_repo(tmp_path / "target")
    with pytest.raises(HospitalError):
        await reception.register(
            title="坏转诊", scope_type="staged", scope={},
            auto_advance=True, project_root=target,
            referred_from={"case_id": "case-x", "project_root": bare},
        )


async def test_case_query_include_links(tmp_path: Path):
    from code_hospital.api.mcp_server import _case_query

    source = _mk_repo(tmp_path / "source")
    target = _mk_repo(tmp_path / "target")
    first = await reception.register(
        title="源病例", scope_type="staged", scope={},
        auto_advance=True, project_root=source,
    )
    second = await reception.register(
        title="转诊接收", scope_type="staged", scope={},
        auto_advance=True, project_root=target,
        referred_from={"case_id": first["case_id"], "project_root": source},
    )
    result = await _case_query(second["case_id"], "", "links", "", 1, 50, "", target)
    assert result["links"][0]["related_case_id"] == first["case_id"]


# ---------- R1 端到端：architecture 号别 → 诊断书带转诊单 ----------


async def test_r1_end_to_end_diagnosis(tmp_path: Path):
    from code_hospital import adapters
    from code_hospital.adapters import RawFinding, ScannerAdapter

    class _Quiet(ScannerAdapter):
        """占位设备：registry 空时装的是真设备，这里要的是「没有额外发现」。"""

        name = "quiet"
        binary = "git"
        extensions = frozenset({".py"})

        async def scan(self, repo_root, files, timeout_ms):
            return []

    adapters.clear()
    adapters.register(_Quiet())
    proj = _mk_repo(tmp_path / "proj")
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "utils.py").write_text("X = 1\n", encoding="utf-8")
    Path(proj, "code-hospital.yaml").write_text(
        "catalog:\n  layers:\n    - api\n    - storage\n", encoding="utf-8",
    )
    Path(proj, "storage").mkdir()
    Path(proj, "storage", "db.py").write_text("import shared.utils\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=proj, check=True)

    reg = await reception.register(
        title="架构号", scope_type="repo", scope={},
        auto_advance=True, requested_specialties=["architecture"], project_root=proj,
    )
    await scanner.scan(case_id=reg["case_id"], project_root=proj)
    res = await diagnostician.diagnose(case_id=reg["case_id"], project_root=proj)
    referrals = [
        ref for d in res["diagnoses"] for ref in d.get("referrals") or []
    ]
    r1 = [ref for ref in referrals if ref["rule"] == "R1"]
    assert len(r1) == 1
    assert r1[0]["local"] is True
    assert r1[0]["finding_ids"]  # 单必有据
    report = Path(res["report_path"]).read_text(encoding="utf-8")
    assert "会诊转诊" in report
    assert "referred_from" in report


# ---------- 会诊链聚合视图：hosp_case(include="chain")（design §4.8 二期·轻编排） ----------


async def test_chain_walks_across_three_hospitals(tmp_path: Path):
    from code_hospital.storage.chain import consultation_chain

    repo_a = _mk_repo(tmp_path / "a")
    repo_b = _mk_repo(tmp_path / "b")
    repo_c = _mk_repo(tmp_path / "c")
    case_a = await reception.register(
        title="甲仓病例", scope_type="staged", scope={},
        auto_advance=True, project_root=repo_a,
    )
    case_b = await reception.register(
        title="乙仓病例", scope_type="staged", scope={},
        auto_advance=True, project_root=repo_b,
        referred_from={"case_id": case_a["case_id"], "project_root": repo_a},
    )
    case_c = await reception.register(
        title="丙仓病例", scope_type="staged", scope={},
        auto_advance=True, project_root=repo_c,
        referred_from={"case_id": case_b["case_id"], "project_root": repo_b},
    )

    chain = await consultation_chain(case_a["case_id"], repo_a)
    by_id = {n["case_id"]: n for n in chain["nodes"]}
    assert set(by_id) == {case_a["case_id"], case_b["case_id"], case_c["case_id"]}
    assert by_id[case_a["case_id"]]["depth"] == 0
    assert by_id[case_a["case_id"]]["is_seed"] is True
    assert by_id[case_b["case_id"]]["depth"] == 1
    assert by_id[case_c["case_id"]]["depth"] == 2
    assert all(n["reachable"] for n in chain["nodes"])
    assert by_id[case_c["case_id"]]["title"] == "丙仓病例"
    # 卡片只带元数据，病历内容不过链
    assert "findings" not in by_id[case_c["case_id"]]
    assert len(chain["edges"]) == 2
    assert chain["truncated"] is False


async def test_chain_cycle_safe_and_edges_deduped(tmp_path: Path):
    from code_hospital.storage.chain import consultation_chain

    repo_a = _mk_repo(tmp_path / "a")
    repo_b = _mk_repo(tmp_path / "b")
    case_a1 = await reception.register(
        title="首诊", scope_type="staged", scope={},
        auto_advance=True, project_root=repo_a,
    )
    case_b = await reception.register(
        title="转诊出去", scope_type="staged", scope={},
        auto_advance=True, project_root=repo_b,
        referred_from={"case_id": case_a1["case_id"], "project_root": repo_a},
    )
    case_a2 = await reception.register(
        title="又转回来", scope_type="staged", scope={},
        auto_advance=True, project_root=repo_a, force=True,
        referred_from={"case_id": case_b["case_id"], "project_root": repo_b},
    )

    chain = await consultation_chain(case_a1["case_id"], repo_a)
    ids = [n["case_id"] for n in chain["nodes"]]
    # A→B→A 成环也不绕圈：每个病例只出现一次，同一次转诊两端各写的一行只算一条边
    assert sorted(ids) == sorted([case_a1["case_id"], case_b["case_id"], case_a2["case_id"]])
    assert len(chain["edges"]) == 2


async def test_chain_unreachable_peer_still_listed_without_side_effect(tmp_path: Path):
    from code_hospital.storage.chain import consultation_chain

    repo_a = _mk_repo(tmp_path / "a")
    repo_b = _mk_repo(tmp_path / "b")
    case_a = await reception.register(
        title="源病例", scope_type="staged", scope={},
        auto_advance=True, project_root=repo_a,
    )
    case_b = await reception.register(
        title="要搬走的", scope_type="staged", scope={},
        auto_advance=True, project_root=repo_b,
        referred_from={"case_id": case_a["case_id"], "project_root": repo_a},
    )
    shutil.rmtree(Path(repo_b) / ".hospital")

    chain = await consultation_chain(case_a["case_id"], repo_a)
    by_id = {n["case_id"]: n for n in chain["nodes"]}
    assert by_id[case_b["case_id"]]["reachable"] is False
    assert "病案室不存在" in by_id[case_b["case_id"]]["reason"]
    assert len(chain["edges"]) == 1  # 链还在，端点状态如实上报
    # 只读走查绝不能在对端仓库重建病案室
    assert not (Path(repo_b) / ".hospital").exists()


async def test_chain_orphan_peer_without_root(tmp_path: Path):
    from code_hospital.storage.chain import consultation_chain

    repo = _mk_repo(tmp_path / "a")
    case = await reception.register(
        title="源病例", scope_type="staged", scope={},
        auto_advance=True, project_root=repo,
    )
    async with Database(repo) as db:
        await db.insert_case_link(case["case_id"], "case-幽灵", "consultation", note="")

    chain = await consultation_chain(case["case_id"], repo)
    ghost = next(n for n in chain["nodes"] if n["case_id"] == "case-幽灵")
    assert ghost["reachable"] is False
    assert "对端仓库根" in ghost["reason"]


async def test_chain_single_case_without_links(tmp_path: Path):
    from code_hospital.storage.chain import consultation_chain

    repo = _mk_repo(tmp_path / "solo")
    case = await reception.register(
        title="没转过诊", scope_type="staged", scope={},
        auto_advance=True, project_root=repo,
    )
    chain = await consultation_chain(case["case_id"], repo)
    assert [n["case_id"] for n in chain["nodes"]] == [case["case_id"]]
    assert chain["nodes"][0]["reachable"] is True
    assert chain["edges"] == []


async def test_case_query_include_chain(tmp_path: Path):
    from code_hospital.api.mcp_server import _case_query

    source = _mk_repo(tmp_path / "source")
    target = _mk_repo(tmp_path / "target")
    first = await reception.register(
        title="源病例", scope_type="staged", scope={},
        auto_advance=True, project_root=source,
    )
    second = await reception.register(
        title="转诊接收", scope_type="staged", scope={},
        auto_advance=True, project_root=target,
        referred_from={"case_id": first["case_id"], "project_root": source},
    )
    result = await _case_query(second["case_id"], "", "chain", "", 1, 50, "", target)
    chain = result["chain"]
    assert {n["case_id"] for n in chain["nodes"]} == {first["case_id"], second["case_id"]}
    assert chain["seed"] == second["case_id"]
    assert "病历内容" in chain["note"]
