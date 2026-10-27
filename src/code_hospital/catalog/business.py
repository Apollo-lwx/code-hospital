"""业务尺。只核对已经写下来的规则。

没有状态机、金额字段、权限约定，也没有主诉时，结果是「业务未查」，不是「业务正常」。
写下来但这一期对不上代码的规则列为待复核，不进自动修改。
状态机和权限只看 Python。金额字段同时看 Python 注解和 Java 的 float/double 声明。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_CONFIG = "code-hospital.yaml"
_FLOAT_NAMES = {"float", "double"}
_JAVA_FLOAT = re.compile(r"\b(float|double)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_SKIP_NEST = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


@dataclass(frozen=True)
class Machine:
    id: str
    field: str
    states: frozenset[str]
    transitions: frozenset[tuple[str, str]]


@dataclass
class BusinessRules:
    machines: tuple[Machine, ...] = ()
    money_fields: frozenset[str] = frozenset()
    checks: frozenset[str] = frozenset()
    writes: tuple[str, ...] = ()
    invariants: tuple[tuple[str, str], ...] = ()
    notes: list[str] = field(default_factory=list)

    @property
    def auth_ready(self) -> bool:
        return bool(self.checks and self.writes)

    @property
    def mechanical(self) -> bool:
        return bool(self.machines or self.money_fields or self.auth_ready)

    def configured(self, chief_complaint: str = "") -> bool:
        return self.mechanical or bool(self.invariants) or bool(chief_complaint.strip())


def load_business(project_root: str) -> BusinessRules:
    path = Path(project_root) / _CONFIG
    rules = BusinessRules()
    if not path.is_file():
        return rules
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        rules.notes.append("code-hospital.yaml 无法解析，业务规则没有读到")
        return rules
    if not isinstance(doc, dict):
        rules.notes.append("code-hospital.yaml 不是映射，业务规则没有读到")
        return rules
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    raw = catalog.get("business") if isinstance(catalog, dict) else None
    if raw is None:
        return rules
    if not isinstance(raw, dict):
        rules.notes.append("catalog.business 不是映射，业务规则没有读到")
        return rules
    rules.machines = tuple(_machines(raw.get("machines"), rules.notes))
    rules.money_fields = frozenset(_names(raw.get("money_fields"), rules.notes, "money_fields"))
    auth = raw.get("auth") if isinstance(raw.get("auth"), dict) else {}
    if raw.get("auth") is not None and not isinstance(raw.get("auth"), dict):
        rules.notes.append("catalog.business.auth 不是映射，权限未核对")
        auth = {}
    rules.checks = frozenset(_names(auth.get("checks"), rules.notes, "auth.checks"))
    rules.writes = tuple(_names(auth.get("writes"), rules.notes, "auth.writes"))
    rules.invariants = tuple(_invariants(raw.get("invariants"), rules.notes))
    if rules.writes and not rules.checks:
        rules.notes.append("权限写了入口但没有 checks，权限未核对")
    if rules.checks and not rules.writes:
        rules.notes.append("权限写了校验函数但没有 writes，权限未核对")
    return rules


def scan_business(
    project_root: str,
    files: list[str],
    chief_complaint: str = "",
) -> tuple[list[dict], list[str], str]:
    """返回 (发现, 限制项, outcome)。outcome 为 ok 或 terminal。"""
    rules = load_business(project_root)
    complaint = chief_complaint.strip()[:500]
    limits = list(rules.notes)
    if not rules.configured(complaint):
        limits.append("业务未查：没有写下来的状态机、金额字段或权限校验。")
        return [], limits, "terminal"

    raws: list[dict] = []
    py_files = [rel for rel in files if Path(rel).suffix.lower() == ".py" and not _is_test_path(rel)]
    java_files = [rel for rel in files if Path(rel).suffix.lower() == ".java" and not _is_test_path(rel)]
    if rules.machines or rules.auth_ready:
        if not py_files:
            limits.append("状态机和权限这一期只核对 Python，本 scope 没有可核对的 Python 文件")
        else:
            raws.extend(_scan_python(project_root, py_files, rules, limits))
    if rules.money_fields:
        raws.extend(_scan_python_money(project_root, py_files, rules, limits))
        raws.extend(_scan_java_money(project_root, java_files, rules))
        if not py_files and not java_files:
            limits.append("金额字段这一期看 Python 和 Java，本 scope 没有这两类文件")
    for invariant_id, text in rules.invariants:
        raws.append(_invariant(invariant_id, text))
    if complaint:
        raws.append(_invariant("chief-complaint", complaint))

    checked = _checked_text(rules)
    if rules.mechanical:
        limits.append(checked + "。没有写成「当前是某一状态」再赋值的迁移不会核对。未写入契约的规则没有查。")
    if rules.invariants or complaint:
        limits.append("写下来但这一期不能机械核对的规则已列入待复核，不能写成业务正常。")
    return raws, limits, "ok"


def _checked_text(rules: BusinessRules) -> str:
    parts = []
    if rules.machines:
        parts.append("状态机 " + "、".join(item.id for item in rules.machines))
    if rules.money_fields:
        parts.append("金额字段 " + "、".join(sorted(rules.money_fields)))
    if rules.auth_ready:
        parts.append("权限校验 " + "、".join(sorted(rules.checks)))
    return "业务号已核对：" + "；".join(parts)


def _scan_python(project_root: str, files: list[str], rules: BusinessRules, limits: list[str]) -> list[dict]:
    raws: list[dict] = []
    root = Path(project_root)
    for rel in files:
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            limits.append(f"跳过无法解析的 {rel}")
            continue
        walker = _Walker(rel.replace("\\", "/"), rules)
        walker.visit(tree)
        raws.extend(walker.findings)
    return raws


def _scan_python_money(project_root: str, files: list[str], rules: BusinessRules, limits: list[str]) -> list[dict]:
    raws: list[dict] = []
    root = Path(project_root)
    for rel in files:
        path = root / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            note = f"跳过无法解析的 {rel}"
            if note not in limits:
                limits.append(note)
            continue
        walker = _MoneyWalker(rel.replace("\\", "/"), rules.money_fields)
        walker.visit(tree)
        raws.extend(walker.findings)
    return raws


def _scan_java_money(project_root: str, files: list[str], rules: BusinessRules) -> list[dict]:
    raws: list[dict] = []
    root = Path(project_root)
    names = {item.casefold() for item in rules.money_fields}
    for rel in files:
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        posix = rel.replace("\\", "/")
        for index, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*")):
                continue
            matched = _JAVA_FLOAT.search(line)
            if matched and matched.group(2).casefold() in names:
                raws.append(_raw(
                    "business.money-float",
                    f"{matched.group(2)} 用 {matched.group(1)} 记账，二进制小数不能当金额",
                    posix, index, "major",
                ))
    return raws


class _Walker(ast.NodeVisitor):
    def __init__(self, file_path: str, rules: BusinessRules) -> None:
        self.file_path = file_path
        self.rules = rules
        self.fn = ""
        self.findings: list[dict] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._auth(node)
        prev = self.fn
        self.fn = node.name
        self.generic_visit(node)
        self.fn = prev

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_If(self, node: ast.If) -> None:
        for machine in self.rules.machines:
            source = _source_state(node.test, machine.field)
            if source is None or source not in machine.states:
                continue
            for stmt, target in _guarded_assignments(node.body, machine.field):
                if target not in machine.states or target == source:
                    continue
                if (source, target) not in machine.transitions:
                    self.findings.append(_raw(
                        "business.illegal-transition",
                        f"状态机 {machine.id} 不允许从 {source} 到 {target}",
                        self.file_path, getattr(stmt, "lineno", node.lineno) or node.lineno,
                        "major", self.fn,
                    ))
        self.generic_visit(node)

    def _auth(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if not self.rules.auth_ready or not _is_write(node.name, self.rules.writes):
            return
        if node.name in self.rules.checks:
            return
        seen = {_decorator_leaf(item) for item in node.decorator_list}
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                seen.add(_call_leaf(child))
        if seen & set(self.rules.checks):
            return
        self.findings.append(_raw(
            "business.missing-auth",
            f"{node.name} 会写数据，函数里没有调用约定的权限校验",
            self.file_path, node.lineno or 1, "major", node.name,
        ))


class _MoneyWalker(ast.NodeVisitor):
    def __init__(self, file_path: str, money_fields: frozenset[str]) -> None:
        self.file_path = file_path
        self.money = {item.casefold() for item in money_fields}
        self.fn = ""
        self.findings: list[dict] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for arg in [*node.args.args, *node.args.kwonlyargs]:
            if arg.annotation is not None and _is_float(arg.annotation) and arg.arg.casefold() in self.money:
                self._emit(arg.arg, arg.lineno or node.lineno)
        prev = self.fn
        self.fn = node.name
        self.generic_visit(node)
        self.fn = prev

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        name = _target_name(node.target)
        if name and name.casefold() in self.money and (
            _is_float(node.annotation) or (node.value is not None and _is_float_value(node.value))
        ):
            self._emit(name, node.lineno or 1)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if _is_float_value(node.value):
            for target in node.targets:
                name = _target_name(target)
                if name and name.casefold() in self.money:
                    self._emit(name, node.lineno or 1)
        self.generic_visit(node)

    def _emit(self, name: str, line: int) -> None:
        self.findings.append(_raw(
            "business.money-float",
            f"{name} 用浮点数记账，二进制小数不能当金额",
            self.file_path, line, "major", self.fn,
        ))


def _machines(raw: object, notes: list[str]) -> list[Machine]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        notes.append("状态机不是列表，状态机未核对")
        return []
    machines: list[Machine] = []
    for item in raw:
        if not isinstance(item, dict):
            notes.append("有一条状态机不是映射，已跳过")
            continue
        ident = str(item.get("id") or item.get("field") or "").strip()
        field_name = str(item.get("field") or "").strip()
        states = {str(state).strip().casefold() for state in (item.get("states") or []) if str(state).strip()}
        if not ident or not field_name or not states:
            notes.append("有一条状态机缺 id、field 或 states，已跳过")
            continue
        transitions: set[tuple[str, str]] = set()
        for edge in item.get("transitions") or []:
            transitions.update(_edges(edge, notes))
        machines.append(Machine(ident, field_name, frozenset(states), frozenset(transitions)))
    return machines


def _edges(edge: object, notes: list[str]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    if isinstance(edge, dict):
        source = str(edge.get("from") or "").strip().casefold()
        target = edge.get("to")
        targets = target if isinstance(target, list) else [target]
    elif isinstance(edge, (list, tuple)) and len(edge) == 2:
        source = str(edge[0]).strip().casefold()
        targets = edge[1] if isinstance(edge[1], list) else [edge[1]]
    else:
        notes.append("有一条迁移格式不对，已跳过")
        return found
    for item in targets:
        dest = str(item or "").strip().casefold()
        if source and dest:
            found.add((source, dest))
    return found


def _names(raw: object, notes: list[str], label: str) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        notes.append(f"{label} 不是列表，已跳过")
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _invariants(raw: object, notes: list[str]) -> list[tuple[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        notes.append("invariants 不是列表，待复核规则没有读到")
        return []
    found = []
    for index, item in enumerate(raw, 1):
        if isinstance(item, str) and item.strip():
            found.append((f"invariant-{index}", item.strip()[:500]))
            continue
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                ident = str(item.get("id") or f"invariant-{index}").strip() or f"invariant-{index}"
                found.append((ident[:80], text[:500]))
                continue
        notes.append("有一条 invariant 没有 text，已跳过")
    return found


def _source_state(test: ast.AST, field_name: str) -> str | None:
    nodes = test.values if isinstance(test, ast.BoolOp) else [test]
    for node in nodes:
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], (ast.Eq, ast.Is)):
            continue
        left, right = node.left, node.comparators[0]
        if _is_field(left, field_name):
            return _state_name(right)
        if _is_field(right, field_name):
            return _state_name(left)
    return None


def _guarded_assignments(stmts: list[ast.stmt], field_name: str) -> list[tuple[ast.stmt, str]]:
    found: list[tuple[ast.stmt, str]] = []
    for stmt in stmts:
        if isinstance(stmt, _SKIP_NEST):
            continue
        target = _assigned_state(stmt, field_name)
        if target is not None:
            found.append((stmt, target))
            continue
        if isinstance(stmt, ast.If) and _source_state(stmt.test, field_name) is not None:
            continue
        if isinstance(stmt, ast.If):
            found.extend(_guarded_assignments(stmt.body, field_name))
            found.extend(_guarded_assignments(stmt.orelse, field_name))
            continue
        for block in _nested_bodies(stmt):
            found.extend(_guarded_assignments(block, field_name))
    return found


def _nested_bodies(stmt: ast.stmt) -> list[list[ast.stmt]]:
    blocks: list[list[ast.stmt]] = []
    for name in ("body", "orelse", "finalbody"):
        block = getattr(stmt, name, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            blocks.append(block)
    if isinstance(stmt, ast.Try):
        blocks.extend(handler.body for handler in stmt.handlers)
    return blocks


def _assigned_state(stmt: ast.stmt, field_name: str) -> str | None:
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            if _is_field(target, field_name):
                return _state_name(stmt.value)
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None and _is_field(stmt.target, field_name):
        return _state_name(stmt.value)
    return None


def _is_field(node: ast.AST, field_name: str) -> bool:
    if isinstance(node, ast.Name):
        return node.id == field_name
    if isinstance(node, ast.Attribute):
        return node.attr == field_name
    return False


def _state_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
        return node.value.strip().casefold()
    if isinstance(node, ast.Attribute) and node.attr.strip():
        return node.attr.strip().casefold()
    return None


def _target_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_float(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id.casefold() in _FLOAT_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr.casefold() in _FLOAT_NAMES
    return False


def _is_float_value(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, float):
        return True
    if isinstance(node, ast.Call):
        return _call_leaf(node).casefold() == "float"
    return False


def _is_write(name: str, writes: tuple[str, ...]) -> bool:
    for item in writes:
        if item.endswith("_") and name.startswith(item):
            return True
        if name == item:
            return True
    return False


def _decorator_leaf(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _decorator_leaf(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _call_leaf(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_test_path(path: str) -> bool:
    posix = path.replace("\\", "/")
    name = Path(posix).name
    return (
        "/tests/" in f"/{posix}/"
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def _invariant(invariant_id: str, text: str) -> dict:
    return _raw(
        "business.unverified-invariant",
        f"规则「{text}」已记录，这一期不能机械核对，待复核",
        "code-hospital.yaml", 1, "info", invariant_id,
    )


def _raw(rule_id: str, message: str, file_path: str, line: int, severity: str, symbol: str = "") -> dict:
    return {
        "tool": "business-ruler",
        "rule_id": rule_id,
        "message": message,
        "file_path": file_path.replace("\\", "/"),
        "line_start": line,
        "line_end": line,
        "severity": severity,
        "symbol": symbol,
    }
