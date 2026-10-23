"""药房 · 药师（成药施治，设计 §3.2 节点 5 的 P1 子集）。

只对已批准处方逐味执行。成药调用 ruff --fix，且只碰 scope_paths；
补丁先落 .hospital/patches/。外购不改代码。煎药先取药发令牌，
submit_patch 过四道校验后才改文件。逆应用冲突可 mode=resolve_conflict
续行（manual_resolved / retry / skip_item）。补验 mode=verify_only 只重放
护理核查，不改源码，也不跑验收命令。
每一味完成后，若还有下一味且未全权委托，则挂起等用户确认护理记录。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from ..adapters.base import AdapterError, _run
from ..core.algorithms import (
    CheckResult,
    PatchParseError,
    apply_hunks,
    gen_search_candidates,
    normalize_path,
    forbidden_hit,
    parse_patch,
    reverse_apply,
    validate_patch,
)
from ..core.errors import (
    HospitalError,
    bad_argument,
    conflict,
    forbidden,
    invalid_operation,
    not_found,
    overlimit,
)
from ..core.ids import new_id
from ..core.questions import ask
from ..safety.acceptance import run_alias
from ..storage.db import Database, hospital_root, now_ms

_MODES = {"apply", "submit_patch", "verify_only", "resolve_conflict"}
_RESOLUTIONS = {"manual_resolved", "retry", "skip_item"}
_TOKEN_TTL_MS = 30 * 60 * 1000
_MAX_ATTEMPTS = 3


def _ruff_binary() -> str:
    found = shutil.which("ruff")
    if found:
        return found
    name = "ruff.exe" if os.name == "nt" else "ruff"
    candidate = Path(sys.executable).parent / name
    return str(candidate) if candidate.is_file() else ""


def _safe_paths(root: str, paths: list[str]) -> list[str]:
    base = Path(root).resolve()
    safe: list[str] = []
    for raw in paths:
        rel = raw.replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts or ".hospital" in Path(rel).parts:
            continue
        full = (base / rel).resolve()
        if base not in full.parents and full != base:
            continue
        if full.is_file():
            safe.append(full.relative_to(base).as_posix())
    return list(dict.fromkeys(safe))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _unified(rel: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    ))


def _diff_stat(diff: str) -> str:
    plus = minus = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            plus += 1
        elif line.startswith("-"):
            minus += 1
    return f"+{plus}/-{minus}"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _handoff(case_id: str, item_id: str, next_item_id: str, auto_advance: bool) -> dict:
    waiting = bool(next_item_id) and not auto_advance
    return {
        "stage": f"dose:{item_id}",
        "artifact": "护理记录",
        "awaiting_confirmation": waiting,
        "next_tool": "hosp_dispense" if next_item_id else "hosp_discharge",
        "confirm_hint": (
            f'请用户确认本味护理记录。确认后 hosp_dispense(prescription_id=…, '
            f'stage_confirmed=true) 继续下一味 {next_item_id}。'
        )[:300] if waiting else "",
    }


async def _record_nursing(db, case_id: str, item_id: str, nursing: dict) -> dict:
    log_id = await db.insert_nursing_log(
        case_id, item_id, "acceptance_check",
        {"verdict": nursing["verdict"], "drift_files": nursing["drift_files"]},
        nursing["verdict"],
    )
    nursing["logs"] = [log_id]
    return nursing


def _next_pending(items: list[dict], after_seq: int) -> str:
    pending = [it for it in items if it["status"] == "pending" and it["seq"] > after_seq]
    return pending[0]["id"] if pending else ""


async def dispense(
    prescription_id: str,
    item_id: str = "",
    mode: str = "apply",
    patch_token: str = "",
    snapshot_hash: str = "",
    patch: str = "",
    resolution: str = "",
    dry_run: bool = False,
    stage_confirmed: bool = False,
    project_root: str = "",
) -> dict:
    """hosp_dispense。一次只处理一味药。"""
    from .reception import resolve_project_root

    if mode not in _MODES:
        raise bad_argument("mode 须为 apply / submit_patch / verify_only / resolve_conflict", target="mode")
    if mode == "verify_only" and resolution:
        raise bad_argument("verify_only 不接收 resolution", target="resolution")

    root = resolve_project_root(project_root)
    async with Database(root) as db:
        rx = await db.get_prescription(prescription_id)
        if rx is None:
            raise not_found(f"处方不存在：{prescription_id}", target="prescription_id")
        if rx["status"] != "approved":
            raise invalid_operation(
                f"处方状态为 {rx['status']}，未批准不能施治",
                target="prescription_id",
                next_action="先 hosp_prescribe(approve=true) 完成审批",
            )
        case = await db.get_case(rx["case_id"])
        if case is None:
            raise not_found("处方所属病例不存在", target="case_id")
        if case.status != "dispensing":
            raise invalid_operation(
                f"病例当前状态为 {case.status}，不能施治",
                target="case_id",
                next_action="处方批准后才会进入 dispensing",
            )
        if mode == "verify_only":
            return await _verify_only(db, root, case, rx, item_id)
        if mode == "resolve_conflict":
            return await _resolve_conflict(
                db, root, case, rx, item_id, resolution, stage_confirmed,
            )
        if case.pending_gate and not stage_confirmed:
            return ask(
                "stage_confirm",
                f"上一味护理记录待确认（{case.pending_gate}）。确认后才抓下一味。",
                [("confirm", "确认护理记录，抓下一味"), ("pause", "暂停施治")],
                "挂起不执行（不改代码）",
                f'hosp_dispense(prescription_id="{prescription_id}", stage_confirmed=true)',
            )
        if case.pending_gate and not case.pending_gate.startswith("dose:"):
            raise invalid_operation(
                f"当前待确认的是 {case.pending_gate}，不能施治",
                target="case_id",
                next_action="按 handoff 确认当前阶段产物",
            )
        if case.pending_gate:
            await db.update_case(case.id, pending_gate="")
            case.pending_gate = ""

        pending = [it for it in rx["items"] if it["status"] == "pending"]
        if item_id:
            item = next((it for it in rx["items"] if it["id"] == item_id), None)
            if item is None:
                raise not_found(f"药味不存在：{item_id}", target="item_id")
            if item["status"] != "pending":
                raise invalid_operation(f"药味状态为 {item['status']}，不能再次抓药", target="item_id")
        elif pending:
            item = pending[0]
        else:
            raise invalid_operation("没有待执行的药味", target="item_id", next_action="可请求 hosp_discharge 出院")

        if mode == "submit_patch":
            return await _submit_patch(
                db, root, case, rx, item, patch_token, snapshot_hash, patch, dry_run,
            )
        if item["method"] == "manual":
            return await _skip_manual(db, case, rx, item)
        if item["method"] == "llm_patch":
            return await _need_patch(db, case, item, await _focus_line(db, case.id, item))
        if item["method"] != "autofix":
            raise invalid_operation(f"未知剂型 {item['method']}", target="item_id")
        return await _apply_autofix(db, root, case, rx, item, dry_run)


async def _focus_line(db, case_id: str, item: dict) -> int:
    diagnoses = await db.list_diagnoses(case_id)
    diagnosis = next((d for d in diagnoses if d.id == item["diagnosis_id"]), None)
    if diagnosis is None:
        return 1
    findings = await db.all_findings(case_id)
    member_ids = set(diagnosis.member_ids)
    for finding in findings:
        same_member = finding.id in member_ids or finding.fingerprint == diagnosis.fingerprint
        if same_member and finding.file_path == item["target_file"] and finding.line_start:
            return finding.line_start
    return 1


async def _need_patch(db, case, item: dict, focus_line: int = 1) -> dict:
    attempts = int(item.get("patch_attempts") or 0)
    left = max(0, _MAX_ATTEMPTS - attempts)
    if left == 0:
        raise overlimit("这味煎药已用完 3 次提交", target="patch_token", next_action="改签或转外购")
    now = now_ms()
    token = item.get("patch_token") or ""
    expires = int(item.get("token_expires_at") or 0)
    if not token.startswith("ptk_") or expires < now:
        token = new_id("ptk")
        expires = now + _TOKEN_TTL_MS
        await db.update_prescription_item(item["id"], patch_token=token, token_expires_at=expires)
    target = Path(case.project_root) / item["target_file"] if item["target_file"] else None
    text = _read(target) if target is not None else ""
    lines = text.splitlines()
    candidates = gen_search_candidates(lines, focus_line) if lines else []
    texts = [c["text"] for c in candidates][:3] or ["1|"]
    start = max(0, focus_line - 8)
    window = "\n".join(f"{i + 1}|{line}" for i, line in enumerate(lines[start:start + 16], start=start))
    return {
        "status": "need_patch",
        "item_id": item["id"],
        "patch_token": token,
        "token_expires_at": expires,
        "snapshot_hash": _sha(text),
        "patch_context": {
            "target_file": item["target_file"],
            "current_content_window": window[:4000],
            "search_candidates": texts,
            "instructions": item["instructions"][:500],
            "format": "unified_diff",
        },
        "constraints": {
            "scope_paths": item["scope_paths"],
            "max_patch_lines": 500,
            "forbidden": [".hospital/", "依赖清单文件", "shell heredoc", "硬编码密钥"],
        },
        "attempts_left": left,
        "note": "用 submit_patch 回传 patch_token、snapshot_hash 和补丁。未提交前不要改文件。",
    }


async def _burn_attempt(db, item: dict) -> int:
    used = int(item.get("patch_attempts") or 0) + 1
    await db.update_prescription_item(item["id"], patch_attempts=used)
    item["patch_attempts"] = used
    return _MAX_ATTEMPTS - used


def _fail_check(result, left: int) -> None:
    code = result.code if result.code in {
        "BadArgument", "NotFound", "InvalidOperation", "Conflict", "Overlimit", "Forbidden",
    } else "InvalidOperation"
    if left <= 0:
        raise overlimit(
            f"校验失败且重试已用完：{result.message[:240]}",
            target=result.stage,
            next_action="改签或转外购",
        )
    raise HospitalError(code, result.message[:500], result.stage, result.next_action[:300])


async def _submit_patch(db, root, case, rx, item, token: str, snapshot_hash: str, patch: str, dry_run: bool) -> dict:
    if item["method"] != "llm_patch":
        raise invalid_operation("只有煎药可以提交补丁", target="mode")
    stored = item.get("patch_token") or ""
    if not token or token != stored:
        raise invalid_operation("补丁令牌无效，请先取药", target="patch_token", next_action="mode=apply 重新取药")
    if int(item.get("token_expires_at") or 0) < now_ms():
        raise invalid_operation("补丁令牌已过期，请重新取药", target="patch_token")
    scope = item["scope_paths"] or ([item["target_file"]] if item["target_file"] else [])
    safe = _safe_paths(root, scope)
    if any(".hospital" in p.split("/") for p in scope):
        raise forbidden("处方范围含 .hospital/，拒绝施治", target="scope_paths")
    base = Path(root)
    before = {normalize_path(rel): _read(base / rel) for rel in safe}
    target = normalize_path(item["target_file"])
    current = before.get(target, "")
    if snapshot_hash != _sha(current):
        raise conflict("文件与取药时的快照不一致，请重新取药", target="snapshot_hash", next_action="mode=apply 重新取药")
    try:
        hunks = parse_patch(patch, default_path=target)
    except PatchParseError as exc:
        await _burn_attempt(db, item)
        raise invalid_operation(str(exc)[:300], target="patch", next_action="按 unified diff 或 SEARCH/REPLACE 重写") from exc
    checked = validate_patch(
        hunks, before, safe, allow_manifest=bool(item.get("touches_manifest")),
    )
    if not checked.ok:
        _fail_check(checked, await _burn_attempt(db, item))
    try:
        after = apply_hunks(before, hunks)
    except PatchParseError as exc:
        await _burn_attempt(db, item)
        raise invalid_operation(str(exc)[:300], target="patch") from exc
    chunks = []
    for rel, old in before.items():
        new = after.get(rel, old)
        if new != old:
            chunks.append(_unified(rel, old, new))
    diff = "".join(chunks)
    if not diff:
        _fail_check(
            CheckResult(False, "format", "BadArgument", "补丁没有改变文件", "提交有实际改动的补丁"),
            await _burn_attempt(db, item),
        )
    if not dry_run:
        for rel, new in after.items():
            if new != before.get(rel):
                dest = base / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new, encoding="utf-8")
    patch_path = ""
    if diff and not dry_run:
        stored_diff = hospital_root(root) / "patches" / case.id / f"{item['id']}-r{case.round}.diff"
        stored_diff.parent.mkdir(parents=True, exist_ok=True)
        stored_diff.write_text(diff, encoding="utf-8")
        patch_path = str(stored_diff)
    dose = {
        "id": new_id("dose"),
        "item_id": item["id"],
        "round": case.round,
        "executor": "llm_patch",
        "patch_path": patch_path,
        "snapshot_hash": snapshot_hash,
        "diff_stat": _diff_stat(diff),
        "result": "success",
        "error": "",
        "created_at": now_ms(),
    }
    nursing = {
        "env_precheck": "skipped",
        "scope_check": "pass",
        "content_check": "pass",
        "acceptance": "skipped",
        "verdict": "pass",
        "drift_files": [],
        "logs": [],
    }
    if dry_run:
        return {
            "status": "dry_run",
            "item_id": item["id"],
            "dose": {**dose, "patch_path": ""},
            "nursing": nursing,
            "case_status": case.status,
            "next_item_id": item["id"],
            "diff": diff[:4000],
            "handoff": _handoff(case.id, item["id"], item["id"], True),
        }
    rejected = await _reject_if_acceptance_failed(db, root, case, rx, item, before, nursing)
    if rejected is not None:
        return rejected
    _write_snapshot(root, case.id, item["id"], case.round, before)
    await db.insert_dose(dose)
    await db.update_prescription_item(item["id"], status="applied", patch_token="", token_expires_at=0)
    item["status"] = "applied"
    nursing = await _record_nursing(db, case.id, item["id"], nursing)
    _append_report(root, case, item, dose, nursing)
    return await _finish(db, case, rx, item, dose, nursing, applied=True)


async def _skip_manual(db, case, rx, item: dict) -> dict:
    await db.update_prescription_item(item["id"], status="skipped")
    item["status"] = "skipped"
    nursing = await _record_nursing(db, case.id, item["id"], {
        "env_precheck": "skipped",
        "scope_check": "skipped",
        "content_check": "skipped",
        "acceptance": "skipped",
        "verdict": "pass",
        "drift_files": [],
        "logs": [],
    })
    return await _finish(db, case, rx, item, None, nursing, applied=False)


def _hunk_outside(hunk, spans: list) -> bool:
    start = hunk.line_hint or 1
    end = start + max(len(hunk.search), 1) - 1
    return not any(a <= end and start <= b for a, b in spans)


def _settle_drift(rel: str, old: str, new: str, spans: list) -> tuple[str, bool, str]:
    """撤掉落在 scope_lines 外的 hunk，范围内的改动留下。

    返回 (留下的文本, 是否发生漂移, 冲突原因)。冲突时返回的文本是快照，
    调用方把未决文本留在工作区，另存快照，不把这份返回值写回去。
    """
    if old == new:
        return new, False, ""
    raw = new
    try:
        hunks = parse_patch(_unified(rel, old, new), rel)
    except PatchParseError as exc:
        return old, True, str(exc)
    outside = [hunk for hunk in hunks if _hunk_outside(hunk, spans)]
    if outside:
        key = normalize_path(rel)
        rolled = reverse_apply({key: new}, outside)
        if not rolled.ok:
            return old, True, rolled.reason
        new = rolled.files[key]
    clipped = _clip_to_spans(old, new, spans)
    return clipped, clipped != raw, ""


def _clip_to_spans(old: str, new: str, spans: list) -> str:
    """只保留与 scope_lines 相交的行改动，其余行回到施治前。"""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
    kept: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            kept.extend(new_lines[j1:j2])
            continue
        start = i1 + 1
        end = i2 if i2 >= start else start
        if any(start <= b and a <= end for a, b in spans):
            kept.extend(new_lines[j1:j2])
        else:
            kept.extend(old_lines[i1:i2])
    return "".join(kept)


def _autofix_engine(item: dict) -> str:
    sample = (item.get("scope_paths") or [item.get("target_file") or ""])[0]
    if Path(sample).suffix.lower() in {".js", ".jsx", ".ts", ".tsx"}:
        return "eslint"
    return "ruff"


async def _apply_autofix(db, root: str, case, rx, item: dict, dry_run: bool) -> dict:
    paths = _safe_paths(root, item["scope_paths"] or ([item["target_file"]] if item["target_file"] else []))
    if any(".hospital" in p.split("/") for p in item["scope_paths"]):
        raise forbidden("处方范围含 .hospital/，拒绝施治", target="scope_paths")
    if not paths:
        return await _fail(db, case, rx, item, "scope 内没有可写的文件，未改代码")

    engine = _autofix_engine(item)
    if engine == "eslint":
        from ..catalog.formulary import instruments
        from ..catalog.runners import eslint_binary, eslint_configured

        if not instruments()["eslint"].get("can_autofix"):
            return await _fail(db, case, rx, item, "eslint 成药执行器未开启，未改代码", "eslint --fix")
        if not eslint_configured(root):
            return await _fail(db, case, rx, item, "项目未配置 eslint，未改代码", "eslint --fix")
        binary = eslint_binary()
        if not binary:
            return await _fail(db, case, rx, item, "未找到 eslint，成药未执行，文件未改", "eslint --fix")
        command = [binary, "--fix", *paths]
        executor = "eslint --fix"
    else:
        binary = _ruff_binary()
        if not binary:
            return await _fail(db, case, rx, item, "未找到 ruff，成药未执行，文件未改")
        command = [binary, "check", "--fix", "--unsafe-fixes", "--no-cache", "--quiet"]
        selected = re.search(r"规则\s+([A-Za-z][A-Za-z0-9]+)", item["instructions"])
        if selected:
            command += ["--select", selected.group(1)]
        command += paths
        executor = "ruff --fix"

    base = Path(root)
    spans = item.get("scope_lines") or []
    if not spans:
        return await _fail(db, case, rx, item, "成药缺少 scope_lines，未执行", executor)
    before = {rel: _read(base / rel) for rel in paths}
    try:
        await _run(command, cwd=root, timeout=60)
    except (AdapterError, OSError) as exc:
        for rel, text in before.items():
            (base / rel).write_text(text, encoding="utf-8")
        return await _fail(db, case, rx, item, str(exc)[:500], executor)

    raw_after = {rel: _read(base / rel) for rel in before}
    kept: dict[str, str] = {}
    drift_files: list[str] = []
    conflict = ""
    for rel, old in before.items():
        text, drifted, reason = _settle_drift(rel, old, raw_after[rel], spans)
        if reason:
            kept[rel] = raw_after[rel]
            conflict = conflict or reason
            drift_files.append(rel)
        else:
            kept[rel] = text
            if drifted:
                drift_files.append(rel)
    for rel, text in kept.items():
        (base / rel).write_text(text, encoding="utf-8")

    chunks = []
    for rel, old in before.items():
        new = kept[rel]
        if new != old:
            chunks.append(_unified(rel, old, new))
    diff = "".join(chunks)
    hospital_leak = ".hospital/" in diff
    if hospital_leak:
        for rel, text in before.items():
            (base / rel).write_text(text, encoding="utf-8")
        conflict = conflict or "改动落入 .hospital/"
        drift_files = list(before)
    if dry_run:
        for rel, text in before.items():
            (base / rel).write_text(text, encoding="utf-8")
    if conflict:
        if dry_run:
            return _blocked(item, executor, drift_files, conflict, case.status, persisted=False)
        return await _rollback_conflict(
            db, root, case, rx, item, executor, drift_files, conflict, before,
        )

    verdict = "warn" if drift_files or not diff else "pass"
    nursing = {
        "env_precheck": "skipped",
        "scope_check": "warn" if drift_files else "pass",
        "content_check": "pass",
        "acceptance": "skipped",
        "verdict": verdict,
        "drift_files": drift_files,
        "logs": [],
    }

    patch_path = ""
    if drift_files and not dry_run:
        revert = "".join(
            _unified(rel, raw_after[rel], kept[rel])
            for rel in drift_files
            if raw_after[rel] != kept[rel]
        )
        if revert:
            revert_path = hospital_root(root) / "patches" / case.id / f"{item['id']}-r{case.round}.revert.diff"
            revert_path.parent.mkdir(parents=True, exist_ok=True)
            revert_path.write_text(revert, encoding="utf-8")
    if diff and not dry_run:
        dest = hospital_root(root) / "patches" / case.id / f"{item['id']}-r{case.round}.diff"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(diff, encoding="utf-8")
        patch_path = str(dest)
    dose = {
        "id": new_id("dose"),
        "item_id": item["id"],
        "round": case.round,
        "executor": executor,
        "patch_path": patch_path,
        "snapshot_hash": _sha("\n".join(before.values())),
        "diff_stat": _diff_stat(diff),
        "result": "success",
        "error": "" if diff else "设备未改动文件",
        "created_at": now_ms(),
    }
    if not dry_run:
        rejected = await _reject_if_acceptance_failed(db, root, case, rx, item, before, nursing)
        if rejected is not None:
            return rejected
        _write_snapshot(root, case.id, item["id"], case.round, before)
        await db.insert_dose(dose)
        await db.update_prescription_item(item["id"], status="applied")
        item["status"] = "applied"
        nursing = await _record_nursing(db, case.id, item["id"], nursing)
        _append_report(root, case, item, dose, nursing)
        return await _finish(db, case, rx, item, dose, nursing, applied=True)
    return {
        "status": "dry_run",
        "item_id": item["id"],
        "dose": {**dose, "patch_path": ""},
        "nursing": nursing,
        "case_status": case.status,
        "next_item_id": item["id"],
        "diff": diff[:4000],
        "handoff": _handoff(case.id, item["id"], item["id"], True),
    }


def _snapshot_path(root: str, case_id: str, item_id: str, round_: int) -> Path:
    return hospital_root(root) / "patches" / case_id / f"{item_id}-r{round_}.snapshot.json"


def _write_snapshot(root: str, case_id: str, item_id: str, round_: int, before: dict) -> tuple[str, str]:
    path = _snapshot_path(root, case_id, item_id, round_)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
    return str(path), _sha("\n".join(before.values()))


def _load_snapshot(root: str, case_id: str, item_id: str, round_: int) -> dict:
    path = _snapshot_path(root, case_id, item_id, round_)
    if not path.is_file():
        raise invalid_operation("本味快照不在，无法续行", target="item_id", next_action="skip_item 需要快照，请人工处理工作区")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise invalid_operation("本味快照无法读取", target="item_id") from exc
    if not isinstance(loaded, dict):
        raise invalid_operation("本味快照无法读取", target="item_id")
    return {str(key): str(value) for key, value in loaded.items()}


def _manual_block(before: dict, current: dict, spans: list) -> str:
    """现状相对快照仍越界时返回原因。范围内的人工改动可以收下，越界行不悄悄裁掉。"""
    for rel, old in before.items():
        new = current.get(rel, "")
        if ".hospital" in Path(rel).parts:
            return "改动落入 .hospital/"
        if old == new:
            continue
        if ".hospital/" in _unified(rel, old, new):
            return "改动落入 .hospital/"
        if _clip_to_spans(old, new, spans) != new:
            return "现状仍有越出 scope_lines 的改动"
    return ""


def _blocked(
    item: dict, executor: str, drift_files: list[str], reason: str, case_status: str, *, persisted: bool = True,
) -> dict:
    if persisted:
        action = (
            f'未决改动留在工作区，快照已落盘。请 hosp_dispense(mode="resolve_conflict", '
            f'item_id="{item["id"]}", resolution="manual_resolved"|"retry"|"skip_item") 续行，'
            "或 hosp_discharge 转院。不要用 git checkout。"
        )
    else:
        action = "dry_run 已把文件还原到本味快照，未记剂量。"
    return {
        "status": "blocked",
        "blocked_on": "rollback_conflict",
        "item_id": item["id"],
        "dose": {
            "item_id": item["id"],
            "executor": executor,
            "result": "rollback_conflict",
            "error": reason[:500],
        },
        "nursing": {
            "env_precheck": "skipped",
            "scope_check": "fail",
            "content_check": "skipped",
            "acceptance": "skipped",
            "verdict": "fail",
            "drift_files": drift_files,
            "logs": [],
        },
        "case_status": case_status,
        "nextAction": action,
    }


async def _rollback_conflict(
    db, root, case, rx, item, executor: str, drift_files: list[str], reason: str, before: dict,
) -> dict:
    _path, snapshot_hash = _write_snapshot(root, case.id, item["id"], case.round, before)
    dose = {
        "id": new_id("dose"),
        "item_id": item["id"],
        "round": case.round,
        "executor": executor,
        "patch_path": "",
        "snapshot_hash": snapshot_hash,
        "diff_stat": "+0/-0",
        "result": "rollback_conflict",
        "error": reason[:500],
        "created_at": now_ms(),
    }
    await db.insert_dose(dose)
    await db.update_prescription_item(item["id"], status="failed")
    item["status"] = "failed"
    payload = _blocked(item, executor, drift_files, reason, case.status)
    payload["dose"] = dose
    nursing = await _record_nursing(db, case.id, item["id"], payload["nursing"])
    payload["nursing"] = nursing
    _append_report(root=case.project_root, case=case, item=item, dose=dose, nursing=nursing)
    finished = await _finish(db, case, rx, item, dose, nursing, applied=False)
    payload["next_item_id"] = finished["next_item_id"]
    payload["handoff"] = finished["handoff"]
    return payload


def _added_lines(diff: str) -> str:
    return "\n".join(
        line[1:] for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def _content_verdict(diff: str) -> str:
    if ".hospital/" in diff or forbidden_hit(_added_lines(diff)):
        return "fail"
    return "pass"


def _review_texts(rel: str, old: str, new: str, spans: list, scope_paths: list) -> tuple[str, str, bool]:
    if old == new:
        return "pass", "pass", False
    diff = _unified(rel, old, new)
    content = _content_verdict(diff)
    allowed = {normalize_path(path) for path in scope_paths}
    if normalize_path(rel) not in allowed:
        return "fail", content, True
    if spans and _clip_to_spans(old, new, spans) != new:
        return "fail", content, True
    if not spans:
        return "warn", content, True
    return "pass", content, False


def _fold(values: list[str]) -> str:
    rank = {"fail": 3, "warn": 2, "pass": 1, "skipped": 0}
    if not values:
        return "skipped"
    return max(values, key=lambda value: rank.get(value, 0))


async def _verify_only(db, root, case, rx, item_id: str) -> dict:
    """重放护理核查。不改源码，不推进药味，不跑验收命令。"""
    if item_id:
        item = next((it for it in rx["items"] if it["id"] == item_id), None)
        if item is None:
            raise not_found(f"药味不存在：{item_id}", target="item_id")
        targets = [item]
    else:
        targets = list(rx["items"])
    parts: list[dict] = []
    for item in targets:
        latest = await db.latest_dose(item["id"])
        if latest is None:
            if item_id:
                raise invalid_operation(
                    "这味药还没有服药记录，不能补验",
                    target="item_id",
                    next_action="先 mode=apply 施治，再 mode=verify_only",
                )
            continue
        parts.append(await _verify_item(db, root, case, item, latest))
    if not parts:
        raise invalid_operation(
            "没有可补验的服药记录",
            target="prescription_id",
            next_action="先施治一味，再 mode=verify_only",
        )
    nursing = {
        "env_precheck": "skipped",
        "scope_check": _fold([part["scope_check"] for part in parts]),
        "content_check": _fold([part["content_check"] for part in parts]),
        "acceptance": _fold([part["acceptance"] for part in parts]),
        "verdict": _fold([part["verdict"] for part in parts]),
        "drift_files": list(dict.fromkeys(path for part in parts for path in part["drift_files"])),
        "logs": [log_id for part in parts for log_id in part["logs"]],
    }
    if nursing["verdict"] == "skipped":
        nursing["verdict"] = "pass"
    return {
        "status": "verified",
        "item_id": item_id,
        "nursing": nursing,
        "case_status": case.status,
    }


async def _verify_item(db, root, case, item: dict, latest: dict) -> dict:
    scope = "skipped"
    content = "skipped"
    drift: list[str] = []
    snap = _snapshot_path(root, case.id, item["id"], case.round)
    if snap.is_file():
        before = _load_snapshot(root, case.id, item["id"], case.round)
        scope = "pass"
        content = "pass"
        for rel, old in before.items():
            new = _read(Path(root) / rel)
            one_scope, one_content, drifted = _review_texts(
                rel, old, new, item.get("scope_lines") or [], item.get("scope_paths") or [],
            )
            if one_scope == "fail" or (one_scope == "warn" and scope != "fail"):
                scope = one_scope
            if one_content == "fail":
                content = "fail"
            if drifted:
                drift.append(rel)
    elif latest.get("patch_path"):
        patch_path = Path(latest["patch_path"])
        patch = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
        if patch:
            content = _content_verdict(patch)
            try:
                hunks = parse_patch(patch, default_path=item.get("target_file") or "")
            except PatchParseError:
                scope = "fail"
                hunks = []
            allowed = {normalize_path(path) for path in (item.get("scope_paths") or [])}
            spans = item.get("scope_lines") or []
            if scope != "fail":
                scope = "pass"
            for hunk in hunks:
                if normalize_path(hunk.path) not in allowed or (spans and _hunk_outside(hunk, spans)):
                    scope = "fail"
                    drift.append(normalize_path(hunk.path))
    verdict = "fail" if scope == "fail" or content == "fail" else "warn" if scope == "warn" or content == "warn" else "pass"
    nursing = _nursing(verdict, scope if scope != "skipped" else "pass", drift)
    nursing["scope_check"] = scope
    nursing["content_check"] = content
    alias = (item.get("acceptance") or {}).get("alias") or ""
    if alias:
        ran = await run_alias(root, alias)
        nursing["acceptance"] = ran["verdict"]
        nursing["env_precheck"] = "pass" if ran["verdict"] != "skipped" else "skipped"
        if ran["verdict"] == "fail":
            nursing["verdict"] = "fail"
    else:
        nursing["acceptance"] = "skipped"
        nursing["env_precheck"] = "skipped"
    log_id = await db.insert_nursing_log(
        case.id, item["id"], "verify_replay",
        {
            "dose_id": latest["id"],
            "dose_result": latest["result"],
            "scope_check": scope,
            "content_check": content,
            "drift_files": drift,
        },
        verdict,
    )
    nursing["logs"] = [log_id]
    return nursing


async def _resolve_conflict(db, root, case, rx, item_id: str, resolution: str, stage_confirmed: bool) -> dict:
    if resolution not in _RESOLUTIONS:
        raise bad_argument("resolution 须为 manual_resolved / retry / skip_item", target="resolution")
    if not item_id:
        raise bad_argument("resolve_conflict 必须指定 item_id", target="item_id")
    item = next((it for it in rx["items"] if it["id"] == item_id), None)
    if item is None:
        raise not_found(f"药味不存在：{item_id}", target="item_id")
    gate = case.pending_gate or ""
    if gate and gate != f"dose:{item['id']}":
        if not stage_confirmed:
            return ask(
                "stage_confirm",
                f"上一味护理记录待确认（{gate}）。确认后才处理这一味。",
                [("confirm", "确认护理记录，再续行"), ("pause", "暂停施治")],
                "挂起不执行（不改代码）",
                f'hosp_dispense(prescription_id="{rx["id"]}", stage_confirmed=true)',
            )
        if not gate.startswith("dose:"):
            raise invalid_operation(
                f"当前待确认的是 {gate}，不能施治",
                target="case_id",
                next_action="按 handoff 确认当前阶段产物",
            )
        await db.update_case(case.id, pending_gate="")
        case.pending_gate = ""
    latest = await db.latest_dose(item["id"])
    if item["status"] != "failed" or latest is None or latest["result"] != "rollback_conflict":
        raise invalid_operation(
            "这味药没有未决的 rollback_conflict",
            target="item_id",
            next_action="只有逆应用冲突的药味可以续行",
        )
    before = _load_snapshot(root, case.id, item["id"], case.round)
    current = {rel: _read(Path(root) / rel) for rel in before}
    spans = item.get("scope_lines") or []
    if resolution == "skip_item":
        return await _skip_conflict(db, root, case, rx, item, before)
    if resolution == "retry":
        return await _retry_conflict(db, root, case, rx, item, before, current, spans)
    return await _accept_manual(db, root, case, rx, item, before, current, spans)


async def _skip_conflict(db, root, case, rx, item, before: dict) -> dict:
    base = Path(root)
    for rel, text in before.items():
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    dose = _resolution_dose(item, case.round, "resolve_conflict:skip_item", before, "", "reverted")
    await db.insert_dose(dose)
    await db.update_prescription_item(item["id"], status="skipped")
    item["status"] = "skipped"
    await db.insert_nursing_log(
        case.id, item["id"], "rollback", {"resolution": "skip_item"}, "pass",
    )
    nursing = await _record_nursing(db, case.id, item["id"], _nursing("pass", "pass", []))
    _append_report(root, case, item, dose, nursing)
    return await _finish(db, case, rx, item, dose, nursing, applied=False)


async def _retry_conflict(db, root, case, rx, item, before: dict, current: dict, spans: list) -> dict:
    if all(before[rel] == current.get(rel, "") for rel in before):
        return _blocked(
            item, "resolve_conflict:retry", [], "工作区与快照一致，没有可重试的差异", case.status,
        )
    kept: dict[str, str] = {}
    drift_files: list[str] = []
    conflict = ""
    conflict_files: list[str] = []
    for rel, old in before.items():
        text, drifted, reason = _settle_drift(rel, old, current[rel], spans)
        if reason:
            conflict = conflict or reason
            conflict_files.append(rel)
            kept[rel] = current[rel]
        else:
            kept[rel] = text
            if drifted:
                drift_files.append(rel)
    if conflict:
        return await _rollback_conflict(
            db, root, case, rx, item, "resolve_conflict:retry",
            conflict_files, conflict, before,
        )
    if drift_files:
        revert = "".join(
            _unified(rel, current[rel], kept[rel])
            for rel in drift_files
            if current[rel] != kept[rel]
        )
        if revert:
            path = hospital_root(root) / "patches" / case.id / f"{item['id']}-r{case.round}.revert.diff"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(revert, encoding="utf-8")
    return await _accept_resolution(
        db, root, case, rx, item, before, kept, drift_files, "retry",
    )


async def _accept_manual(db, root, case, rx, item, before: dict, current: dict, spans: list) -> dict:
    reason = _manual_block(before, current, spans)
    if reason:
        return _blocked(item, "resolve_conflict:manual_resolved", list(before), reason, case.status)
    return await _accept_resolution(
        db, root, case, rx, item, before, current, [], "manual_resolved",
    )


async def _accept_resolution(
    db, root, case, rx, item, before: dict, kept: dict, drift_files: list[str], resolution: str,
) -> dict:
    base = Path(root)
    for rel, text in kept.items():
        dest = base / rel
        if _read(dest) != text:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
    diff = "".join(_unified(rel, old, kept[rel]) for rel, old in before.items() if kept[rel] != old)
    patch_path = ""
    if diff:
        dest = hospital_root(root) / "patches" / case.id / f"{item['id']}-r{case.round}.diff"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(diff, encoding="utf-8")
        patch_path = str(dest)
    dose = _resolution_dose(
        item, case.round, f"resolve_conflict:{resolution}", before, patch_path, "success",
    )
    dose["diff_stat"] = _diff_stat(diff)
    dose["error"] = "" if diff else "现状与快照一致"
    verdict = "warn" if drift_files else "pass"
    nursing = _nursing(verdict, "warn" if drift_files else "pass", drift_files)
    rejected = await _reject_if_acceptance_failed(db, root, case, rx, item, before, nursing)
    if rejected is not None:
        return rejected
    await db.insert_dose(dose)
    await db.update_prescription_item(item["id"], status="applied")
    item["status"] = "applied"
    if drift_files:
        await db.insert_nursing_log(
            case.id, item["id"], "rollback",
            {"resolution": resolution, "drift_files": drift_files}, "warn",
        )
    nursing = await _record_nursing(db, case.id, item["id"], nursing)
    _append_report(root, case, item, dose, nursing)
    return await _finish(db, case, rx, item, dose, nursing, applied=True)


def _resolution_dose(item, round_: int, executor: str, before: dict, patch_path: str, result: str) -> dict:
    return {
        "id": new_id("dose"),
        "item_id": item["id"],
        "round": round_,
        "executor": executor,
        "patch_path": patch_path,
        "snapshot_hash": _sha("\n".join(before.values())),
        "diff_stat": "+0/-0",
        "result": result,
        "error": "",
        "created_at": now_ms(),
    }


def _nursing(verdict: str, scope_check: str, drift_files: list[str]) -> dict:
    return {
        "env_precheck": "skipped",
        "scope_check": scope_check,
        "content_check": "pass",
        "acceptance": "skipped",
        "verdict": verdict,
        "drift_files": drift_files,
        "logs": [],
    }


async def _reject_if_acceptance_failed(db, root, case, rx, item, before: dict, nursing: dict) -> dict | None:
    alias = (item.get("acceptance") or {}).get("alias") or ""
    if not alias:
        return None
    result = await run_alias(root, alias)
    nursing["env_precheck"] = "pass"
    nursing["acceptance"] = result["verdict"]
    if result["verdict"] == "warn" and nursing.get("verdict") == "pass":
        nursing["verdict"] = "warn"
    if result["verdict"] != "fail":
        return None
    base = Path(root)
    for rel, text in before.items():
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return await _fail(
        db, case, rx, item,
        f"验收未通过，本味已回到快照：{result['detail']}",
        executor="acceptance",
        acceptance="fail",
    )


async def _fail(db, case, rx, item, message: str, executor: str = "ruff --fix", acceptance: str = "skipped") -> dict:
    dose = {
        "id": new_id("dose"),
        "item_id": item["id"],
        "round": case.round,
        "executor": executor,
        "patch_path": "",
        "snapshot_hash": "",
        "diff_stat": "+0/-0",
        "result": "failed",
        "error": message[:500],
        "created_at": now_ms(),
    }
    await db.insert_dose(dose)
    await db.update_prescription_item(item["id"], status="failed")
    item["status"] = "failed"
    nursing = await _record_nursing(db, case.id, item["id"], {
        "env_precheck": "skipped",
        "scope_check": "skipped",
        "content_check": "skipped",
        "acceptance": acceptance,
        "verdict": "fail",
        "drift_files": [],
        "logs": [],
    })
    _append_report(root=case.project_root, case=case, item=item, dose=dose, nursing=nursing)
    return await _finish(db, case, rx, item, dose, nursing, applied=False)


async def _finish(db, case, rx, item, dose, nursing, applied: bool) -> dict:
    nxt = _next_pending(rx["items"], item["seq"])
    waiting = bool(nxt) and not case.auto_advance
    await db.update_case(case.id, pending_gate=f"dose:{item['id']}" if waiting else "")
    status = "applied" if applied else ("skipped" if item["status"] == "skipped" else item["status"])
    if item["status"] == "failed":
        status = "failed"
    return {
        "status": status,
        "item_id": item["id"],
        "dose": dose,
        "nursing": nursing,
        "case_status": "dispensing",
        "next_item_id": nxt,
        "handoff": _handoff(case.id, item["id"], nxt, case.auto_advance),
    }


def _append_report(root: str, case, item: dict, dose: dict, nursing: dict) -> None:
    path = hospital_root(root) / "reports" / case.id / f"dispense-r{case.round}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    block = (
        f"\n## {item['id']}\n\n"
        f"- 剂型: {item['method']} ｜ 结果: {dose['result']} ｜ diff: {dose['diff_stat']}\n"
        f"- 护理: {nursing['verdict']}（scope {nursing['scope_check']} / 内容 {nursing['content_check']}）\n"
        f"- patch: `{dose['patch_path'] or '无'}`\n"
    )
    if path.is_file():
        path.write_text(path.read_text(encoding="utf-8") + block, encoding="utf-8")
    else:
        path.write_text(f"# 施治护理记录 · {case.title}\n\n> case: `{case.id}`\n{block}", encoding="utf-8")
