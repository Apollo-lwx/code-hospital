"""安全号和重症号的覆盖账。

查过只说明这台仪器跑完了，不等于没有问题。
没有仪器的攻击类保持未覆盖，不能写成正常。
源码哈希没变时，沿用上次已查的单元；变了就按这次重查。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_STATUS = {"checked": "已查", "partial": "只查了一部分", "uncovered": "未覆盖"}


def source_hash(project_root: str, files: list[str]) -> str:
    digest = hashlib.sha256()
    root = Path(project_root)
    for rel in sorted({item.replace("\\", "/") for item in files})[:400]:
        digest.update(rel.encode())
        path = root / rel
        try:
            payload = path.read_bytes()[:8192] if path.is_file() else b"missing"
        except OSError:
            payload = b"unreadable"
        digest.update(payload)
    return digest.hexdigest()[:16]


def build_ledger(
    visit_kind: str,
    devices: list[dict],
    project_root: str,
    files: list[str],
    prior: dict | None = None,
    full_scan: bool = True,
) -> tuple[list[str], dict]:
    """返回写进限制项的句子，以及要落在病例上的覆盖账。"""
    current_hash = source_hash(project_root, files)
    if not full_scan:
        kept = prior or {"source_hash": current_hash, "units": []}
        return ["补片不改写整份覆盖账。"], kept
    units = _units(visit_kind, devices)
    if not units:
        return [], {"source_hash": current_hash, "units": []}
    same_source = bool(prior and prior.get("source_hash") == current_hash and prior.get("units"))
    lines = ["覆盖账（查过不等于没有问题）："]
    if same_source:
        lines.append("源码未变，已查单元沿用上次覆盖账；未覆盖的仍然未覆盖。")
    elif prior and prior.get("units"):
        lines.append("源码变了，覆盖账按这次重查。")
    for unit in units:
        lines.append(f"{unit['title']}：{_STATUS[unit['status']]}。{unit['note']}")
    return lines, {"source_hash": current_hash, "units": units}


def _units(visit_kind: str, devices: list[dict]) -> list[dict]:
    present = {row.get("tool", "") for row in devices}
    units: list[dict] = []
    if visit_kind == "security" or "security-ruler" in present:
        units.extend(_security_units(devices))
    elif _state(devices, "gitleaks") == "ok":
        units.append(_secrets("absent", "ok"))
    if visit_kind == "severe" or "taint-ruler" in present or "impact-review" in present:
        units.extend(_severe_units(devices, present))
    return units


def _security_units(devices: list[dict]) -> list[dict]:
    ruler = _state(devices, "security-ruler")
    leaks = _state(devices, "gitleaks")
    semgrep = _state(devices, "semgrep")
    units = [
        _pattern("injection", "注入", ruler, "安全尺查过命令、SQL 和动态执行的写法"),
        _pattern("deserialization", "反序列化", ruler, "安全尺查过反序列化调用"),
        _secrets(ruler, leaks),
        _pattern("transport", "传输与摘要", ruler, "安全尺查过关闭证书校验和弱摘要"),
        _pattern("output-trust", "输出信任", ruler, "安全尺查过把字符串当成安全输出的写法"),
        {
            "id": "authn",
            "title": "认证边界",
            "status": "uncovered",
            "note": "这一号没有认证边界仪器，不能写成没有问题。",
        },
        {
            "id": "tenant",
            "title": "租户隔离",
            "status": "uncovered",
            "note": "这一号没有租户隔离仪器，不能写成没有问题。",
        },
        {
            "id": "sandbox",
            "title": "隔离执行",
            "status": "uncovered",
            "note": "没有隔离执行，未观察到越界结果。",
        },
    ]
    if semgrep != "absent":
        units.append(_external("semgrep", "外部规则", semgrep))
    return units


def _severe_units(devices: list[dict], present: set[str]) -> list[dict]:
    taint = _state(devices, "taint-ruler")
    impact = _state(devices, "impact-review")
    units = [
        _ran(
            "taint-intra", "函数内污点", taint,
            "只在单个函数里跟着赋值走。查过不等于没有漏洞。",
            "函数内污点没拍成，不能写成没有问题。",
        ),
        {
            "id": "taint-cross",
            "title": "跨函数污点",
            "status": "uncovered",
            "note": "跨函数没有追，不能写成没有漏洞。",
        },
        _ran(
            "impact", "改动影响", impact,
            "只列出了改过的函数和调用方，三句待复核没有自动改代码。",
            "改动影响没拍成。",
        ),
        {
            "id": "sandbox",
            "title": "隔离执行",
            "status": "uncovered",
            "note": "没有隔离执行，未观察到越界结果。",
        },
    ]
    if "spotbugs" in present:
        bugs = _state(devices, "spotbugs")
        units.append(_ran(
            "bytecode", "字节码缺陷", bugs,
            "SpotBugs 查过已有的 class。",
            "SpotBugs 没拍成，不能写成没有缺陷。",
        ))
    return units


def _pattern(unit_id: str, title: str, state: str, done: str) -> dict:
    if state == "ok":
        return {"id": unit_id, "title": title, "status": "checked", "note": done + "。查过不等于没有问题。"}
    if state == "absent":
        return {"id": unit_id, "title": title, "status": "uncovered", "note": "这台仪器没有进入本轮，不能写成正常。"}
    return {"id": unit_id, "title": title, "status": "uncovered", "note": "安全尺没拍成，不能写成没有问题。"}


def _secrets(ruler: str, leaks: str) -> dict:
    if ruler == "ok" and leaks == "ok":
        return {
            "id": "secret-in-source",
            "title": "源码里的密钥",
            "status": "checked",
            "note": "安全尺和 gitleaks 都跑过。查过不等于没有密钥。",
        }
    if ruler == "ok" and leaks == "terminal":
        return {
            "id": "secret-in-source",
            "title": "源码里的密钥",
            "status": "partial",
            "note": "字面量查过；gitleaks 未到岗，仓库级密钥规则未查，不能写成没有密钥。",
        }
    if ruler == "ok":
        return {
            "id": "secret-in-source",
            "title": "源码里的密钥",
            "status": "checked",
            "note": "安全尺查了源码字面量。这次没有跑 gitleaks。",
        }
    if leaks == "ok":
        return {
            "id": "secret-in-source",
            "title": "源码里的密钥",
            "status": "partial",
            "note": "gitleaks 跑过。安全尺没拍，字面量赋值未查。",
        }
    return {
        "id": "secret-in-source",
        "title": "源码里的密钥",
        "status": "uncovered",
        "note": "密钥仪器没拍成，不能写成没有密钥。",
    }


def _external(unit_id: str, title: str, state: str) -> dict:
    if state == "ok":
        return {
            "id": unit_id,
            "title": title,
            "status": "checked",
            "note": "外部规则跑过，但没有独立核实，不能写成已确认的边界失败。",
        }
    return {
        "id": unit_id,
        "title": title,
        "status": "uncovered",
        "note": f"{unit_id} 未到岗或没拍成，不能写成正常。",
    }


def _ran(unit_id: str, title: str, state: str, done: str, missed: str) -> dict:
    if state == "ok":
        return {"id": unit_id, "title": title, "status": "checked", "note": done}
    return {"id": unit_id, "title": title, "status": "uncovered", "note": missed}


def _state(devices: list[dict], tool: str) -> str:
    for row in devices:
        if row.get("tool") != tool:
            continue
        if row.get("available") and row.get("outcome") == "ok":
            return "ok"
        return "terminal"
    return "absent"
