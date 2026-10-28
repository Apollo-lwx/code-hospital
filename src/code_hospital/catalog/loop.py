"""疗效闭环。出院时把已消除的仪器发现写入评测集，下次检验对照复发率。

没有样本时不算复发率。宿主交卷不进入这本账。这本账不给药物排序。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.models import counts_toward_efficacy
from ..storage.db import hospital_root

_MAX_SAMPLES = 200
_NO_KEYS = "出院样本里没有可对照的仪器发现，复发率不算。"
_NO_COVER = "没有已读文件盖满范围的病例，复发率不算。"


def record_discharge(
    root: str, case_id: str, outcome: str, resolved: list, scope: dict | None = None,
) -> tuple[dict, str]:
    """把这次出院消除的仪器发现并进 `.hospital/eval/set.json`。同一病例只留最新一份。"""
    rows = []
    seen: set[str] = set()
    for finding in resolved:
        if not counts_toward_efficacy(getattr(finding, "concluded_by", "") or "instrument"):
            continue
        key = str(getattr(finding, "content_key", "") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({
            "content_key": key,
            "rule_id": str(getattr(finding, "rule_id", "") or ""),
            "file_path": str(getattr(finding, "file_path", "") or "").replace("\\", "/"),
        })
    sample = {
        "case_id": case_id,
        "outcome": outcome,
        "resolved": rows,
        "files": _paths((scope or {}).get("files")),
        "read": _paths((scope or {}).get("read")),
    }
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = _load(path)
    samples = [item for item in document["samples"] if item.get("case_id") != case_id]
    samples.append(sample)
    document["samples"] = samples[-_MAX_SAMPLES:]
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return sample, str(path)


def scope_account(coverage: dict | None, files: list[str]) -> dict:
    """清单每一项都是已通读时，已读文件就是这次范围。否则没有可读账。"""
    filing = coverage.get("filing") if isinstance(coverage, dict) else None
    items = filing.get("items") if isinstance(filing, dict) else None
    read: list[str] = []
    if isinstance(items, list) and items and all(
        isinstance(row, dict) and row.get("status") == "clear" for row in items
    ):
        read = list(files)
    return {"files": list(files), "read": read}


def note_recurrence(root: str, findings: list, limitations: list[str], ledger: dict) -> dict:
    """有出院样本时把复发率写进覆盖账和限制项。没有样本则原样返回。"""
    row = measure(root, findings)
    if row is None:
        return ledger
    updated = dict(ledger)
    updated["recurrence"] = row
    limitations.append(row["note"])
    return updated


def measure(root: str, findings: list) -> dict | None:
    document = _load(_path(root))
    samples = document["samples"]
    if not samples:
        return None
    eligible = [sample for sample in samples if _covered(sample)]
    skipped = len(samples) - len(eligible)
    if not eligible:
        return {"resolved": 0, "recurred": 0, "rate": None, "skipped": skipped, "note": _NO_COVER}
    keys: list[str] = []
    seen: set[str] = set()
    for sample in eligible:
        for row in sample.get("resolved") or []:
            key = str(row.get("content_key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    if not keys:
        return {
            "resolved": 0, "recurred": 0, "rate": None, "skipped": skipped,
            "note": _with_skipped(_NO_KEYS, skipped),
        }
    current = {
        str(getattr(finding, "content_key", "") or "")
        for finding in findings
        if counts_toward_efficacy(getattr(finding, "concluded_by", "") or "instrument")
    }
    current.discard("")
    recurred = [key for key in keys if key in current]
    rate = round(len(recurred) / len(keys), 4)
    if recurred:
        note = (
            f"出院时消除的仪器发现 {len(keys)} 条，这次复扫又出现 {len(recurred)} 条，"
            f"复发率 {round(rate * 100)}%。"
        )
    else:
        note = f"出院时消除的仪器发现 {len(keys)} 条，这次复扫没有再出现。"
    return {
        "resolved": len(keys),
        "recurred": len(recurred),
        "rate": rate,
        "skipped": skipped,
        "note": _with_skipped(note, skipped),
    }


def _covered(sample: dict) -> bool:
    from .hits import measure_coverage

    row = measure_coverage([{"files": sample.get("files"), "read": sample.get("read")}])
    return row["scopes"] == 1 and row["covered"] == 1


def _with_skipped(note: str, skipped: int) -> str:
    if not skipped:
        return note
    return f"{note}另有 {skipped} 份未盖满范围，不计入。"


def _paths(raw) -> list[str]:
    from .hits import _file_set

    return sorted(_file_set(raw if isinstance(raw, list) else []))


def _path(root: str) -> Path:
    return hospital_root(root) / "eval" / "set.json"


def _load(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "samples": []}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "samples": []}
    if not isinstance(loaded, dict):
        return {"version": 1, "samples": []}
    samples = loaded.get("samples")
    if not isinstance(samples, list):
        samples = []
    return {"version": 1, "samples": [item for item in samples if isinstance(item, dict)]}
