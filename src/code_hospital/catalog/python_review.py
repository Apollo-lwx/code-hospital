"""Python 内置审查尺。函数行数仍由 structure-ruler 计，这里不重复。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SECRET_NAME = re.compile(r"(password|passwd|secret|api_key|apikey|token)", re.I)
_PLACEHOLDER = re.compile(r"^(x+|changeme|todo|placeholder|your[_-]?key)$", re.I)


def scan_python_review(project_root: str, files: list[str]) -> tuple[list[dict], list[str]]:
    from .policy import load_policy

    policy = load_policy(project_root)
    raws: list[dict] = []
    notes = ["函数行数由 structure-ruler 计，python-review 不重复报。"]
    root = Path(project_root)
    py_files = [rel for rel in files if Path(rel).suffix.lower() == ".py"]
    if not py_files:
        notes.append("python-review: 本 scope 无 Python 文件，跳过")
        return [], notes
    for rel in py_files:
        path = root / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            notes.append(f"python-review 跳过无法解析的 {rel}")
            continue
        key = rel.replace("\\", "/")
        _walk(tree, key, policy.parameters, policy.branches, raws)
    return raws, notes


def _walk(tree: ast.AST, file_path: str, parameters: int, branches: int, raws: list[dict]) -> None:
    test_file = _is_test(file_path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _function(node, file_path, parameters, branches, raws)
        elif isinstance(node, ast.ExceptHandler) and node.type is None:
            _add(raws, "python.bare-except", "裸 except 会把退出和键盘中断也接住", file_path, node, "major")
        elif isinstance(node, ast.Call) and _call_name(node) in {"eval", "exec"}:
            _add(raws, "python.exec", f"调用 {_call_name(node)}", file_path, node, "critical")
        elif not test_file:
            _secret(node, file_path, raws)


def _function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    file_path: str,
    parameters: int,
    branches: int,
    raws: list[dict],
) -> None:
    if parameters > 0:
        count = _param_count(node)
        if count > parameters:
            _add(
                raws, "python.too-many-params",
                f"函数 {node.name} 有 {count} 个参数，超过 {parameters}",
                file_path, node, "minor", node.name,
            )
    if branches > 0:
        count = _branch_count(node)
        if count > branches:
            _add(
                raws, "python.branch-count",
                f"函数 {node.name} 里有 {count} 个分支，超过 {branches}",
                file_path, node, "minor", node.name,
            )
    for default in list(node.args.defaults) + [item for item in node.args.kw_defaults if item is not None]:
        if isinstance(default, (ast.List, ast.Dict, ast.Set)):
            _add(
                raws, "python.mutable-default",
                f"函数 {node.name} 用可变对象做默认参数",
                file_path, default, "major", node.name,
            )


def _param_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    names = [arg.arg for arg in node.args.args] + [arg.arg for arg in node.args.kwonlyargs]
    if names and names[0] in {"self", "cls"}:
        names = names[1:]
    if node.args.vararg:
        names.append(node.args.vararg.arg)
    if node.args.kwarg:
        names.append(node.args.kwarg.arg)
    return len(names)


def _branch_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return _count_branches(node, nested=False)


def _count_branches(node: ast.AST, nested: bool) -> int:
    if nested and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return 0
    total = 0
    if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
        total += 1
    elif isinstance(node, ast.BoolOp):
        total += max(0, len(node.values) - 1)
    for child in ast.iter_child_nodes(node):
        child_nested = nested or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        total += _count_branches(child, child_nested)
    return total


def _secret(node: ast.AST, file_path: str, raws: list[dict]) -> None:
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets, value = [node.target], node.value
    else:
        return
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return
    literal = value.value.strip()
    if len(literal) < 8 or _PLACEHOLDER.match(literal) or "example" in literal.lower():
        return
    for target in targets:
        if isinstance(target, ast.Name) and _SECRET_NAME.search(target.id):
            _add(
                raws, "python.hardcoded-secret",
                f"{target.id} 被写成了源码里的字面量",
                file_path, node, "critical", target.id,
            )


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_test(file_path: str) -> bool:
    name = Path(file_path).name
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in file_path.replace("\\", "/")


def _add(
    raws: list[dict], rule_id: str, message: str, file_path: str, node: ast.AST,
    severity: str, symbol: str = "",
) -> None:
    line = getattr(node, "lineno", 1) or 1
    end = getattr(node, "end_lineno", None) or line
    raws.append({
        "rule_id": rule_id,
        "message": message,
        "file_path": file_path,
        "line_start": line,
        "line_end": end,
        "severity": severity,
        "symbol": symbol,
    })
