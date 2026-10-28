"""被诊疗仓库的外援名册。只读 code-hospital.yaml，不执行用户命令。"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ID = re.compile(r"^[a-z][a-z0-9._-]{0,48}$")
_METHODS = {"llm_patch", "manual"}
_ENGINES = {"semgrep", "external", "mcp"}
_MODES = {"only", "plus"}
_SCOPES = {"line_range", "symbol", "file", "reference_closure"}
_RISKS = {"low", "medium", "high"}


def overlay(project_root: str) -> dict:
    """返回已经筛过的外援。和内置重名的条目不覆盖内置。"""
    empty = {
        "visits": {},
        "specialties": {},
        "formulas": {},
        "instruments": {},
        "routes": [],
        "diagnose": [],
        "discharge": [],
        "notes": [],
    }
    if not project_root:
        return empty
    block, notes = _block(project_root)
    if not block and notes:
        empty["notes"] = notes
        return empty
    if not block:
        return empty
    from .formulary import load

    builtin_formulas, builtin_specs, builtin_instruments = load()
    known_formulas = set(builtin_formulas)
    known_specs = set(builtin_specs)
    known_instruments = set(builtin_instruments)
    root = Path(project_root)

    formulas: dict[str, dict] = {}
    for row in _rows(block.get("formulas"), notes, "药方"):
        item = _formula(row, notes)
        if item is None:
            continue
        if item["id"] in known_formulas or item["id"] in formulas:
            notes.append(f"药 {item['id']} 与已有药重名，这次没用用户定义")
            continue
        formulas[item["id"]] = item

    specialties: dict[str, dict] = {}
    for row in _rows(block.get("specialties"), notes, "医生"):
        item = _specialty(row, notes, known_formulas | set(formulas))
        if item is None:
            continue
        if item["id"] in known_specs or item["id"] in specialties:
            notes.append(f"医生 {item['id']} 与已有专科重名，这次没用用户定义")
            continue
        specialties[item["id"]] = item

    referenced = {fid for spec in specialties.values() for fid in spec["formulary"]}
    for fid in list(formulas):
        if fid not in referenced:
            notes.append(f"药 {fid} 没有放进任何药柜，这次不参与匹配")
            formulas.pop(fid)

    instruments: dict[str, dict] = {}
    for row in _rows(block.get("instruments"), notes, "仪器"):
        item = _instrument(row, notes, root, known_specs | set(specialties))
        if item is None:
            continue
        if item["id"] in known_instruments or item["id"] in instruments:
            notes.append(f"仪器 {item['id']} 与已有仪器重名，这次没用用户定义")
            continue
        instruments[item["id"]] = item

    known_instruments |= set(instruments)
    visits: dict[str, dict] = {}
    for row in _rows(block.get("visits"), notes, "号别"):
        item = _visit(row, notes, known_instruments)
        if item is None:
            continue
        if item["id"] in visits:
            notes.append(f"号别 {item['id']} 写了多次，这次用先写出的")
            continue
        from .visits import VISIT_KINDS

        if item["id"] in VISIT_KINDS:
            notes.append(f"号别 {item['id']} 已内置，这次没用用户定义")
            continue
        visits[item["id"]] = item

    routes = []
    for row in _rows(block.get("routes"), notes, "分流"):
        item = _route(row, notes, known_specs | set(specialties))
        if item is not None:
            routes.append(item)

    return {
        "visits": visits,
        "specialties": specialties,
        "formulas": formulas,
        "instruments": instruments,
        "routes": routes,
        "diagnose": _questions(block.get("diagnose"), notes, "诊断"),
        "discharge": _questions(block.get("discharge"), notes, "出院"),
        "notes": notes,
    }


def _block(project_root: str) -> tuple[dict, list[str]]:
    path = Path(project_root) / "code-hospital.yaml"
    if not path.is_file():
        return {}, []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}, ["code-hospital.yaml 无法解析，这次不用外援名册"]
    if not isinstance(data, dict):
        return {}, ["code-hospital.yaml 不是映射，这次不用外援名册"]
    block = data.get("catalog") or {}
    if not isinstance(block, dict):
        return {}, ["catalog 不是映射，这次不用外援名册"]
    return block, []


def _rows(value, notes: list[str], label: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        notes.append(f"{label} 不是列表，这次没采用")
        return []
    return value


def _identity(row, notes: list[str], label: str) -> str:
    if not isinstance(row, dict):
        notes.append(f"{label} 有一条不是映射，这次跳过")
        return ""
    ident = str(row.get("id") or "").strip()
    if not _ID.match(ident):
        notes.append(f"{label} id 必须是小写字母开头的短名：{ident or '（空）'}")
        return ""
    return ident


def _formula(row, notes: list[str]) -> dict | None:
    ident = _identity(row, notes, "药方")
    if not ident:
        return None
    method = str(row.get("method") or "").strip()
    if method not in _METHODS:
        notes.append(f"药 {ident} 的剂型只能是 llm_patch 或 manual，这次没采用")
        return None
    risk = str(row.get("risk") or "medium")
    if risk not in _RISKS:
        notes.append(f"药 {ident} 的风险只能是 low、medium、high")
        return None
    scope = str(row.get("scope") or "symbol")
    if scope not in _SCOPES:
        notes.append(f"药 {ident} 的范围不认识")
        return None
    indications = []
    for item in row.get("indications") or []:
        if not isinstance(item, dict) or item.get("kind") != "rule":
            notes.append(f"药 {ident} 的适应症只接受 kind: rule")
            continue
        indications.append({
            "kind": "rule",
            "rule_ids": [str(rule) for rule in (item.get("rule_ids") or [])],
            "prefixes": [str(prefix) for prefix in (item.get("prefixes") or [])],
            "tools": [str(tool) for tool in (item.get("tools") or [])],
        })
    if not indications:
        notes.append(f"药 {ident} 没有可用的适应症，这次没采用")
        return None
    return {
        "id": ident,
        "name": str(row.get("name") or ident)[:40],
        "method": method,
        "risk": risk,
        "phase": 1,
        "hold": False,
        "scope": scope,
        "acceptance_alias": "",
        "indications": indications,
        "risk_overrides": [],
    }


def _specialty(row, notes: list[str], formula_ids: set[str]) -> dict | None:
    ident = _identity(row, notes, "医生")
    if not ident:
        return None
    kept = []
    for fid in row.get("formulary") or []:
        text = str(fid)
        if text not in formula_ids:
            notes.append(f"医生 {ident} 的药柜里没有 {text}，这一味跳过")
            continue
        kept.append(text)
    if not kept:
        notes.append(f"医生 {ident} 没有可用的药，这次没采用")
        return None
    return {"id": ident, "name": str(row.get("name") or ident)[:40], "formulary": kept}


def _instrument(row, notes: list[str], root: Path, specialty_ids: set[str]) -> dict | None:
    ident = _identity(row, notes, "仪器")
    if not ident:
        return None
    engine = str(row.get("engine") or "").strip()
    if engine not in _ENGINES:
        notes.append(f"仪器 {ident} 的引擎只能是 semgrep、external 或 mcp，这次没采用")
        return None
    rules = ""
    binary = str(row.get("binary") or "").strip()
    command = ""
    tool = ""
    args: list[str] = []
    if engine == "semgrep":
        rules, problem = _inside(root, str(row.get("rules") or ""))
        if problem or rules is None or not rules.is_file():
            notes.append(problem or f"仪器 {ident} 的规则文件不存在")
            return None
        binary = binary or "semgrep"
    elif engine == "external":
        if not binary or any(char in binary for char in "\\/\n"):
            notes.append(f"仪器 {ident} 的 external 要写一个命令名，这次没采用")
            return None
        from .external_run import safe_args

        args = safe_args(row.get("args"))
        if args is None:
            notes.append(f"仪器 {ident} 的参数含禁止字符，这次没采用")
            return None
    else:
        command = str(row.get("command") or "").strip()
        tool = str(row.get("tool") or "").strip()
        if not command or any(char in command for char in "\\/\n"):
            notes.append(f"仪器 {ident} 的 mcp 要写一个命令名，这次没采用")
            return None
        from .mcp_client import tool_name

        if tool_name(tool) is None:
            notes.append(f"仪器 {ident} 的 mcp 要写工具名，这次没采用")
            return None
        from .external_run import safe_args

        args = safe_args(row.get("args"))
        if args is None:
            notes.append(f"仪器 {ident} 的参数含禁止字符，这次没采用")
            return None
        binary = command
    specialty = str(row.get("specialty_id") or "general")
    if specialty not in specialty_ids:
        notes.append(f"仪器 {ident} 的专科 {specialty} 不存在，改记到全科")
        specialty = "general"
    triage = [str(item) for item in (row.get("triage") or []) if str(item) in {"emergency", "outpatient", "checkup"}]
    return {
        "id": ident,
        "concern": str(row.get("concern") or ident)[:80],
        "extensions": [str(item).lstrip(".").lower() for item in (row.get("extensions") or [])],
        "triage": triage,
        "activation": "package" if triage else "visit",
        "engine": engine,
        "binary": binary,
        "command": command,
        "tool": tool,
        "args": args if engine in {"external", "mcp"} else [],
        "sandbox": bool(row.get("sandbox")) if engine == "external" else False,
        "builtin": False,
        "can_autofix": False,
        "specialty_id": specialty,
        "requires_contract": False,
        "limitation": f"{ident} 未到岗",
        "rules": str(rules) if rules else "",
        "user": True,
    }


def _visit(row, notes: list[str], instrument_ids: set[str]) -> dict | None:
    ident = _identity(row, notes, "号别")
    if not ident:
        return None
    mode = str(row.get("mode") or "only")
    if mode not in _MODES:
        notes.append(f"号别 {ident} 的 mode 只能是 only 或 plus")
        return None
    instruments = []
    for item in row.get("instruments") or []:
        text = str(item)
        if text not in instrument_ids:
            notes.append(f"号别 {ident} 点了不存在的仪器 {text}，这次没排")
            continue
        instruments.append(text)
    if not instruments:
        notes.append(f"号别 {ident} 没有可用仪器，这次没采用")
        return None
    packs = [str(item) for item in (row.get("packs") or [])]
    return {
        "id": ident,
        "label": str(row.get("label") or ident)[:40],
        "mode": mode,
        "instruments": instruments,
        "packs": packs,
    }


def _route(row, notes: list[str], specialty_ids: set[str]) -> tuple[str, str, str] | None:
    if not isinstance(row, dict):
        notes.append("分流有一条不是映射，这次跳过")
        return None
    tool = str(row.get("tool") or "").strip()
    specialty = str(row.get("specialty") or "").strip()
    rule = str(row.get("rule") or "").strip()
    prefix = str(row.get("prefix") or "").strip()
    if not tool or specialty not in specialty_ids:
        notes.append("分流要写已有的 tool 和 specialty，这条跳过")
        return None
    if rule:
        return tool, rule, specialty
    if prefix and prefix not in {"*", "."}:
        return tool, prefix, specialty
    notes.append("分流要写 rule 或 prefix，不能用通配把一整台仪器接走")
    return None


def _questions(block, notes: list[str], label: str) -> list[dict]:
    if block is None:
        return []
    if not isinstance(block, dict):
        notes.append(f"{label}外援不是映射，这次没采用")
        return []
    found = []
    for row in _rows(block.get("items"), notes, f"{label}问题"):
        ident = _identity(row, notes, f"{label}问题")
        if not ident:
            continue
        question = str(row.get("question") or ident)
        artifact = str(row.get("artifact") or "一句结论")
        found.append({
            "id": ident,
            "question": question,
            "artifact": artifact,
            "needs": [],
            "status": "open",
            "note": "",
            "executors": [{
                "kind": "llm",
                "status": "pending",
                "text": (
                    f"事项 {ident}：{question}\n"
                    f"要交出：{artifact}\n"
                    "只根据这一阶段报告里已经写出的内容来写。没有的文件不要编。"
                ),
            }],
        })
    return found


def _inside(root: Path, raw: str) -> tuple[Path | None, str]:
    text = str(raw).strip()
    if not text:
        return None, "规则路径是空的"
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None, f"规则路径必须在仓库内：{text}"
    path = (root / candidate).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None, f"规则路径越出仓库：{text}"
    return path, ""
