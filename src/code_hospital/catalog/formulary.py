"""内置名册：仪器分流、药柜匹配、scope_key、病症收拢。

数据在同目录的 yaml。phase 大于 CURRENT_PHASE 的药不参与匹配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

CURRENT_PHASE = 1
SPECIALTY_IDS = (
    "general", "python", "java", "structure", "architecture", "design", "performance",
)
REQUESTABLE_INSTRUMENTS = frozenset({"type-ruler"})
_FUNCTION_RULES = frozenset({"structure.function_lines", "structure.cyclomatic"})

# 前缀行优先于通配。安全覆盖（gitleaks / critical）在 route() 里先于这张表。
_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("ruff", "*", "python"),
    ("semgrep", "*", "general"),
    ("gitleaks", "*", "general"),
    ("structure-ruler", "structure.", "structure"),
    ("eslint", "*", "general"),
    ("jscpd", "*", "structure"),
    ("java-pack", "java.", "java"),
    ("layer-ruler", "*", "architecture"),
    ("hotspot-ruler", "hotspot.", "performance"),
    ("type-ruler", "*", "python"),
)


@dataclass
class Formula:
    id: str
    name: str
    method: str
    risk: str
    phase: int
    hold: bool
    scope: str
    acceptance_alias: str
    indications: list[dict] = field(default_factory=list)
    risk_overrides: list[dict] = field(default_factory=list)


@dataclass
class Match:
    formula: Formula | None
    specialty_id: str
    confidence: float
    matched: bool

    @property
    def method(self) -> str:
        if self.formula is None:
            return "manual"
        return self.formula.method


def _load_yaml(name: str) -> list:
    path = Path(__file__).with_name(name)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError(f"名册 {name} 必须是列表")
    return data


@lru_cache(maxsize=1)
def load() -> tuple[dict[str, Formula], dict[str, dict], dict[str, dict]]:
    formulas = {
        row["id"]: Formula(
            id=row["id"],
            name=row["name"],
            method=row["method"],
            risk=row["risk"],
            phase=int(row["phase"]),
            hold=bool(row.get("hold", False)),
            scope=row["scope"],
            acceptance_alias=row.get("acceptance_alias") or "",
            indications=list(row.get("indications") or []),
            risk_overrides=list(row.get("risk_overrides") or []),
        )
        for row in _load_yaml("formulas.yaml")
    }
    specialties = {row["id"]: row for row in _load_yaml("specialties.yaml")}
    instruments = {row["id"]: row for row in _load_yaml("instruments.yaml")}
    _validate(formulas, specialties)
    return formulas, specialties, instruments


def _validate(formulas: dict[str, Formula], specialties: dict[str, dict]) -> None:
    active = [f for f in formulas.values() if f.phase <= CURRENT_PHASE]
    covered: set[str] = set()
    for spec in specialties.values():
        for fid in spec["formulary"]:
            if fid not in formulas:
                raise RuntimeError(f"药柜引用了不存在的药 {fid}")
            if formulas[fid].phase <= CURRENT_PHASE:
                covered.add(fid)
    missing = [f.id for f in active if f.id not in covered]
    if missing:
        raise RuntimeError(f"当前阶段的药没有放进任何药柜：{missing}")


def formulas() -> dict[str, Formula]:
    return load()[0]


def specialties() -> dict[str, dict]:
    return load()[1]


def instruments() -> dict[str, dict]:
    return load()[2]


def route(tool: str, rule_id: str, severity: str) -> str:
    """一条发现的接诊专科。critical 与 gitleaks 先改到全科。"""
    if tool == "gitleaks" or severity == "critical":
        return "general"
    best = (-1, "general")
    for inst, pattern, specialty in _ROUTES:
        if inst != tool:
            continue
        if pattern == "*":
            rank = 0
        elif pattern.endswith(".") and rule_id.startswith(pattern):
            rank = 1
        else:
            continue
        if rank >= best[0]:
            best = (rank, specialty)
    return best[1]


def scope_key(finding) -> str:
    path = finding.file_path
    if finding.rule_id == "structure.file_lines":
        return f"file:{path}#file"
    if finding.tool == "layer-ruler":
        contract = getattr(finding, "contract_id", "") or "default"
        return f"contract:{contract}#rule:{finding.rule_id}"
    if finding.symbol:
        return f"file:{path}#sym:{finding.symbol}"
    return f"file:{path}#lines:{finding.line_start}-{finding.line_end}"


def _rule_hit(indication: dict, rule_id: str, tool: str) -> bool:
    tools = indication.get("tools") or []
    if tools and tool not in tools:
        return False
    ids = indication.get("rule_ids") or []
    prefixes = indication.get("prefixes") or []
    if not ids and not prefixes:
        return bool(tools)
    if rule_id in ids:
        return True
    return any(rule_id.startswith(prefix) for prefix in prefixes)


def _confidence(formula: Formula, tool: str) -> float:
    if formula.id == "seal":
        if tool == "gitleaks":
            return 0.92
        if tool == "ruff":
            return 0.70
        return 0.62
    if formula.id == "debride":
        return 0.86
    if formula.id == "repair":
        return 0.72 if tool == "ruff" else 0.58
    return 0.72


def match_finding(
    tool: str, rule_id: str, severity: str,
    specialty_id: str | None = None, fixable: bool = False,
) -> Match:
    """按 §4.3 的顺序从该专科药柜里选主药。对不上则待复核。"""
    books, specs, _instruments = load()
    specialty_id = specialty_id or route(tool, rule_id, severity)
    cabinet = [
        books[fid]
        for fid in specs[specialty_id]["formulary"]
        if books[fid].phase <= CURRENT_PHASE
    ]
    if tool == "gitleaks" or severity == "critical":
        seal = next(f for f in cabinet if f.id == "seal")
        return Match(seal, "general", _confidence(seal, tool), True)

    if tool == "eslint" and fixable and severity != "critical":
        debride = next((item for item in cabinet if item.id == "debride"), None)
        if debride is not None:
            return Match(debride, specialty_id, _confidence(debride, tool), True)

    for formula in cabinet:
        for indication in formula.indications:
            if indication.get("kind") == "rule" and _rule_hit(indication, rule_id, tool):
                return Match(formula, specialty_id, _confidence(formula, tool), True)

    for formula in cabinet:
        for indication in formula.indications:
            if indication.get("kind") != "fallback":
                continue
            tools = indication.get("tools") or []
            if tool in tools and severity != "critical":
                return Match(formula, specialty_id, _confidence(formula, tool), True)

    return Match(None, specialty_id, 0.40, False)


def effective_method(formula: Formula, acceptance_open: bool) -> str:
    """验收白名单为空时，削峰从煎药改为外购。"""
    if formula.id == "throttle" and not acceptance_open:
        return "manual"
    return formula.method


def companion_for(formula_id: str, method: str, specialty_id: str) -> Formula | None:
    if method != "llm_patch":
        return None
    books, specs, _ = load()
    if "contrast" not in specs[specialty_id]["formulary"]:
        return None
    contrast = books["contrast"]
    if contrast.phase > CURRENT_PHASE:
        return None
    parents: list[str] = []
    for indication in contrast.indications:
        if indication.get("kind") == "companion":
            parents = list(indication.get("parents") or [])
    if formula_id not in parents:
        return None
    return contrast


def line_span(finding) -> tuple[int, int]:
    start = finding.line_start or 1
    end = finding.line_end or start
    return start, max(start, end)


def build_conditions(clusters: list[tuple], requested: list[str]) -> list[dict]:
    """clusters 是 (diagnosis, members)。每个病症单独选主治。"""
    prepared = []
    for diagnosis, members in clusters:
        head = min(members, key=lambda f: (f.line_start, f.file_path))
        start, end = line_span(head)
        if head.rule_id in _FUNCTION_RULES and head.symbol:
            start = min(line_span(f)[0] for f in members)
            end = max(line_span(f)[1] for f in members)
        prepared.append({
            "diagnosis": diagnosis,
            "members": members,
            "head": head,
            "file": head.file_path,
            "start": start,
            "end": end,
            "function": head.rule_id in _FUNCTION_RULES and bool(head.symbol),
            "layer": head.tool == "layer-ruler",
            "scope_key": scope_key(head),
        })

    loci: list[dict] = []
    for item in prepared:
        if item["function"]:
            loci.append({
                "scope_key": item["scope_key"],
                "file": item["file"],
                "start": item["start"],
                "end": item["end"],
                "items": [item],
            })

    def _shortest(file: str, start: int, end: int) -> dict | None:
        hits = [
            locus for locus in loci
            if locus["file"] == file and locus["start"] <= start and end <= locus["end"]
        ]
        if not hits:
            return None
        return min(hits, key=lambda locus: (locus["end"] - locus["start"], locus["scope_key"]))

    loose: list[dict] = []
    layers: dict[str, dict] = {}
    for item in prepared:
        if item["function"]:
            continue
        if item["layer"]:
            bucket = layers.setdefault(item["scope_key"], {
                "scope_key": item["scope_key"],
                "file": "",
                "start": 0,
                "end": 0,
                "items": [],
            })
            bucket["items"].append(item)
            continue
        if not item["head"].symbol:
            host = _shortest(item["file"], item["start"], item["end"])
            if host is not None:
                host["items"].append(item)
                continue
        loose.append({
            "scope_key": item["scope_key"],
            "file": item["file"],
            "start": item["start"],
            "end": item["end"],
            "items": [item],
        })

    conditions = []
    for locus in [*loci, *loose, *layers.values()]:
        conditions.append(_condition(locus, requested))
    return conditions


def _condition(locus: dict, requested: list[str]) -> dict:
    from ..core.models import SEVERITY_ORDER

    items = locus["items"]
    ranked = sorted(
        items,
        key=lambda item: (
            SEVERITY_ORDER.get(item["diagnosis"].severity, 9),
            item["diagnosis"].category,
            item["diagnosis"].specialty_id,
        ),
    )
    specialties = list(dict.fromkeys(item["diagnosis"].specialty_id for item in ranked))
    named = [sid for sid in specialties if sid in requested]
    if named:
        attending = min(
            named,
            key=lambda sid: min(
                SEVERITY_ORDER.get(item["diagnosis"].severity, 9)
                for item in ranked if item["diagnosis"].specialty_id == sid
            ),
        )
    else:
        attending = ranked[0]["diagnosis"].specialty_id
    return {
        "scope_key": locus["scope_key"],
        "attending_specialty_id": attending,
        "consultant_specialty_ids": [sid for sid in specialties if sid != attending],
        "diagnosis_ids": [item["diagnosis"].id for item in items],
    }


def selected_instrument_ids(triage: str, files: list[str], requested_specialties: list[str], requested_instruments: list[str]) -> list[str]:
    """初检套餐。点名的 on_request 仪器不在这里，留给检查单。"""
    suffixes = {Path(path).suffix.lstrip(".").lower() for path in files}
    chosen: list[str] = []
    for inst in instruments().values():
        if inst["activation"] != "package":
            continue
        if triage == "emergency":
            if inst["id"] == "gitleaks":
                chosen.append(inst["id"])
            continue
        if triage == "checkup" and inst["id"] == "layer-ruler":
            continue
        if triage not in (inst.get("triage") or []):
            continue
        exts = set(inst.get("extensions") or [])
        if exts and suffixes and not (suffixes & exts):
            continue
        chosen.append(inst["id"])
    if triage == "checkup" and "architecture" in requested_specialties:
        chosen.append("layer-ruler")
    # requested_instruments 里的类型尺不进初检，避免和「点名专科不等于点名仪器」搅在一起。
    _ = requested_instruments
    return chosen


def scan_orders_for(requested_specialties: list[str], requested_instruments: list[str], notes: list[dict]) -> list[dict]:
    """还没跑过的点名仪器。outcome 为 ok 或 terminal 的不再开单。"""
    done = {row["id"] for row in notes if row.get("outcome") in {"ok", "terminal"}}
    orders = []
    if "performance" in requested_specialties and "hotspot-ruler" not in done:
        orders.append({"instrument_id": "hotspot-ruler", "reason": "requested:performance"})
    if "type-ruler" in requested_instruments and "type-ruler" not in done:
        orders.append({"instrument_id": "type-ruler", "reason": "requested:type-ruler"})
    for row in notes:
        if row.get("outcome") == "retry" and row["id"] not in {item["instrument_id"] for item in orders}:
            orders.append({"instrument_id": row["id"], "reason": f"retry:{row['id']}"})
    return orders
