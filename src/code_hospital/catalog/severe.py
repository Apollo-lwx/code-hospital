"""重症号。只看这次改动。

污点在单个函数里跟着赋值走，出口是 SQL、命令、文件路径、对外请求。
调用影响只列出改过的函数被谁调用，并留下三句待复核，不自动改代码。
跨函数的数据流没有追。没有 diff 时结果是「重症未查」。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

_SKIP_DIRS = {
    ".git", ".hospital", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "target", ".rsi", "tmp", "vendor", "third_party", "site-packages",
}
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
_SOURCE_HEADS = {"request", "req"}
_SOURCE_ATTRS = {
    "args", "form", "values", "json", "data", "GET", "POST", "query_params",
    "path_params", "cookies", "headers", "files",
}
_SQL = {"execute", "executemany"}
_HTTP = {
    "requests.get", "requests.post", "requests.put", "requests.patch", "requests.delete",
    "httpx.get", "httpx.post", "httpx.put", "httpx.request",
    "urllib.request.urlopen", "urlopen",
}
_PROPAGATE = {"str", "format", "join", "get"}
_CAP_FUNCTIONS = 30
_CAP_CALLERS = 8


def resolve_change_set(project_root: str, files: list[str], scope_type: str) -> tuple[list[str], list[str], bool]:
    """重症只看本次 diff。指定路径或 diff 范围沿用这份文件清单。"""
    if scope_type != "repo":
        if not files:
            return [], ["重症未查：这次范围里没有文件。"], False
        return files, ["重症只看本次送进来的文件。"], True
    changed = _git_changed(project_root)
    if changed is None:
        return [], ["重症未查：读不到 git diff。"], False
    if not changed:
        return [], ["重症未查：这次没有 diff。重症只看本次改动，不扫全仓库。"], False
    existing = [item for item in changed if (Path(project_root) / item).is_file()]
    if not existing:
        return [], ["重症未查：diff 里的文件已经不在工作区。"], False
    return existing, ["重症只看本次 diff，未改到的文件没有拍。"], True


def scan_taint(project_root: str, files: list[str]) -> tuple[list[dict], list[str], str]:
    py_files = [item for item in files if item.endswith(".py") and not _is_test_path(item)]
    if not py_files:
        return [], ["这次 diff 没有要核对的 Python 文件，函数内污点未查。"], "terminal"
    raws: list[dict] = []
    limits = ["污点只在单个函数内跟着赋值走，跨函数没有追。跨函数这一段不能写成没有漏洞。"]
    root = Path(project_root)
    spans = _line_spans(project_root, py_files)
    if spans is None:
        spans = {item.replace("\\", "/"): None for item in py_files}
    else:
        limits.append("污点只核对这次 diff 改到的函数。没改到的函数没有追。")
    for rel in py_files:
        path = root / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            limits.append(f"跳过无法解析的 {rel}")
            continue
        key = rel.replace("\\", "/")
        raws.extend(_taint_file(tree, key, spans.get(key)))
    return raws, limits, "ok"


def scan_impact(project_root: str, files: list[str]) -> tuple[list[dict], list[str], str]:
    py_files = [item for item in files if item.endswith(".py") and not _is_test_path(item)]
    if not py_files:
        return [], ["这次 diff 没有要核对的 Python 文件，调用影响未查。"], "terminal"
    root = Path(project_root)
    callers = _caller_index(root)
    raws: list[dict] = []
    limits = [
        "调用方只认本文件里的使用，以及全仓库只有这一处定义时的调用。同名函数不会串到别的文件。",
    ]
    spans = _line_spans(project_root, py_files)
    if spans is None:
        spans = {item.replace("\\", "/"): None for item in py_files}
        limits.append("读不到行级 diff，调用影响按整个文件里的函数列。")
    else:
        limits.append("调用影响只列这次 diff 改到的函数，同一文件里没改到的函数没有单列。")
    seen = 0
    for rel in py_files:
        path = root / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            limits.append(f"跳过无法解析的 {rel}")
            continue
        key = rel.replace("\\", "/")
        for name, line in _touched_functions(tree, spans.get(key)):
            if seen >= _CAP_FUNCTIONS:
                limits.append(f"调用影响只列出前 {_CAP_FUNCTIONS} 个改过的函数")
                return raws, limits, "ok"
            seen += 1
            used = callers.callers(key, name)
            if used:
                who = "、".join(used)
                message = f"函数 {name} 被 {who} 使用。"
            else:
                message = f"函数 {name} 在已扫描的 Python 里没有找到调用方。"
            message += "待复核：这是谁的数据；失败时返回了什么；状态有没有跳步。跨函数影响不能写成逻辑正常。"
            raws.append(_raw(
                "impact-review", "severe.review-impact", message,
                key, line, "info", name,
            ))
    if not raws and not any(item.startswith("跳过无法解析") for item in limits):
        limits.append("这次 diff 没有改到 Python 函数，调用影响没有单列。")
    return raws, limits, "ok"


def _taint_file(tree: ast.AST, rel: str, lines: set[int] | None) -> list[dict]:
    raws: list[dict] = []

    def emit(rule_id: str, message: str, line: int, symbol: str) -> None:
        raws.append(_raw("taint-ruler", rule_id, message, rel, line, "major", symbol))

    tainted: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _overlaps(stmt, lines):
                _taint_function(stmt, emit)
        elif isinstance(stmt, ast.ClassDef):
            for sub in stmt.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and _overlaps(sub, lines):
                    _taint_function(sub, emit)
        elif _overlaps(stmt, lines):
            _walk_stmt(stmt, tainted, emit, "")
    return raws


def _taint_function(node: ast.FunctionDef | ast.AsyncFunctionDef, emit) -> None:
    tainted = {arg.arg for arg in [*node.args.args, *node.args.kwonlyargs] if arg.arg in _SOURCE_HEADS}
    for stmt in node.body:
        _walk_stmt(stmt, tainted, emit, node.name)


def _walk_stmt(stmt: ast.stmt, tainted: set[str], emit, symbol: str) -> None:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    if isinstance(stmt, ast.If):
        _walk_expr(stmt.test, tainted, emit, symbol)
        left, right = set(tainted), set(tainted)
        for item in stmt.body:
            _walk_stmt(item, left, emit, symbol)
        for item in stmt.orelse:
            _walk_stmt(item, right, emit, symbol)
        tainted.clear()
        tainted.update(left | right)
        return
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.expr):
                _walk_expr(child, tainted, emit, symbol)
        for item in stmt.body:
            _walk_stmt(item, tainted, emit, symbol)
        return
    if isinstance(stmt, ast.Try):
        for item in stmt.body:
            _walk_stmt(item, tainted, emit, symbol)
        for handler in stmt.handlers:
            for item in handler.body:
                _walk_stmt(item, tainted, emit, symbol)
        return
    if isinstance(stmt, ast.Assign):
        _walk_expr(stmt.value, tainted, emit, symbol)
        hot = _tainted_expr(stmt.value, tainted)
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                if hot:
                    tainted.add(target.id)
                else:
                    tainted.discard(target.id)
        return
    if isinstance(stmt, ast.AnnAssign):
        if stmt.value is not None:
            _walk_expr(stmt.value, tainted, emit, symbol)
            if isinstance(stmt.target, ast.Name):
                if _tainted_expr(stmt.value, tainted):
                    tainted.add(stmt.target.id)
                else:
                    tainted.discard(stmt.target.id)
        return
    if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
        _walk_expr(stmt.value, tainted, emit, symbol)
        if stmt.target.id in tainted or _tainted_expr(stmt.value, tainted):
            tainted.add(stmt.target.id)
        return
    if isinstance(stmt, ast.Expr):
        _walk_expr(stmt.value, tainted, emit, symbol)
        return
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, ast.stmt):
            _walk_stmt(child, tainted, emit, symbol)
        elif isinstance(child, ast.expr):
            _walk_expr(child, tainted, emit, symbol)


def _walk_expr(node: ast.AST, tainted: set[str], emit, symbol: str) -> None:
    if isinstance(node, ast.Call):
        _sink(node, tainted, emit, symbol)
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            _walk_expr(arg, tainted, emit, symbol)
        if isinstance(node.func, ast.Attribute):
            _walk_expr(node.func.value, tainted, emit, symbol)
        return
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            _walk_expr(child, tainted, emit, symbol)


def _sink(node: ast.Call, tainted: set[str], emit, symbol: str) -> None:
    name = _full_name(node.func)
    leaf = name.rsplit(".", 1)[-1]
    arg = node.args[0] if node.args else None
    line = node.lineno or 1
    if arg is None or not _tainted_expr(arg, tainted):
        if not (leaf in {"read_text", "read_bytes", "write_text", "write_bytes"} and _path_ctor_tainted(node, tainted)):
            return
    if leaf in _SQL:
        emit("severe.taint-sql", "外部输入流进了 SQL", line, symbol)
    elif name in {"os.system", "os.popen"} or (_shell(node) and (name.startswith("subprocess.") or leaf in {"run", "Popen", "call", "check_output", "check_call"})):
        emit("severe.taint-command", "外部输入流进了命令", line, symbol)
    elif name == "open" or leaf == "open" or leaf in {"read_text", "read_bytes", "write_text", "write_bytes"}:
        emit("severe.taint-path", "外部输入流进了文件路径", line, symbol)
    elif name in _HTTP or leaf == "urlopen":
        emit("severe.taint-request", "外部输入流进了对外请求", line, symbol)


def _path_ctor_tainted(node: ast.Call, tainted: set[str]) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    value = func.value
    if not isinstance(value, ast.Call):
        return False
    if _full_name(value.func) not in {"Path", "pathlib.Path"}:
        return False
    return any(_tainted_expr(arg, tainted) for arg in value.args)


def _tainted_expr(node: ast.AST, tainted: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in tainted
    if isinstance(node, ast.Attribute):
        return _attr_source(node) or _tainted_expr(node.value, tainted)
    if isinstance(node, ast.Subscript):
        return _tainted_expr(node.value, tainted)
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(item, ast.FormattedValue) and _tainted_expr(item.value, tainted) for item in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _tainted_expr(node.left, tainted) or _tainted_expr(node.right, tainted)
    if isinstance(node, ast.Call):
        if _call_source(node):
            return True
        leaf = _full_name(node.func).rsplit(".", 1)[-1]
        args = [*node.args, *(kw.value for kw in node.keywords)]
        if leaf in _PROPAGATE and any(_tainted_expr(arg, tainted) for arg in args):
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return _tainted_expr(node.func.value, tainted) or any(_tainted_expr(arg, tainted) for arg in args)
    return False


def _attr_source(node: ast.Attribute) -> bool:
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return False
    parts.append(cur.id)
    parts.reverse()
    return len(parts) >= 2 and parts[0] in _SOURCE_HEADS and parts[1] in _SOURCE_ATTRS


def _call_source(node: ast.Call) -> bool:
    name = _full_name(node.func)
    if name in {"input", "os.getenv"} or name.endswith("environ.get"):
        return True
    if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        base = node.func.value
        if isinstance(base, ast.Attribute) and _attr_source(base):
            return True
        if isinstance(base, ast.Name) and base.id in _SOURCE_HEADS:
            return True
    return False


def _shell(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


class _CallerIndex:
    """同名函数不跨文件乱认。本文件里的引用，以及全仓库唯一的定义，才算调用方。"""

    def __init__(self) -> None:
        self.defs: dict[str, list[str]] = {}
        self.uses: list[tuple[str, str, str, str]] = []

    def add_def(self, rel: str, name: str) -> None:
        bucket = self.defs.setdefault(name, [])
        if rel not in bucket:
            bucket.append(rel)

    def add_use(self, rel: str, fn: str, leaf: str, qualifier: str) -> None:
        if leaf:
            self.uses.append((rel, fn, leaf, qualifier))

    def callers(self, rel: str, name: str) -> list[str]:
        owners = self.defs.get(name, [])
        unique = owners == [rel]
        labels: list[str] = []
        for caller_rel, caller_fn, leaf, qualifier in self.uses:
            if leaf != name or (caller_rel == rel and caller_fn == name):
                continue
            if caller_rel != rel and not unique and not _qualifier_matches(qualifier, rel):
                continue
            where = caller_fn or "模块顶层"
            label = f"{caller_rel} 的 {where}"
            if label not in labels:
                labels.append(label)
            if len(labels) >= _CAP_CALLERS:
                break
        return labels


def _caller_index(root: Path) -> _CallerIndex:
    index = _CallerIndex()
    for rel, path in _python_corpus(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        _collect_uses(tree, rel, index)
    return index


def _python_corpus(root: Path) -> list[tuple[str, Path]]:
    listed = _git_python_files(root)
    if listed is None:
        return _walk_python_files(root)
    found: list[tuple[str, Path]] = []
    for rel in listed:
        path = root / rel
        if path.is_file():
            found.append((rel, path))
        if len(found) >= 2000:
            break
    return found


def _run_git(root: str | Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_python_files(root: Path) -> list[str] | None:
    tracked = _run_git(root, ["ls-files"])
    others = _run_git(root, ["ls-files", "--others", "--exclude-standard"])
    if tracked is None or others is None or tracked.returncode != 0:
        return None
    names: list[str] = []
    for blob in (tracked.stdout, others.stdout):
        for line in (blob or "").splitlines():
            item = line.strip().replace("\\", "/")
            if item.endswith(".py") and item not in names:
                names.append(item)
    return names


def _walk_python_files(root: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [item for item in dirnames if item not in _SKIP_DIRS and not item.startswith(".")]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = Path(dirpath) / name
            found.append((path.relative_to(root).as_posix(), path))
            if len(found) >= 2000:
                return found
    return found


def _collect_uses(tree: ast.AST, rel: str, index: _CallerIndex) -> None:
    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _here(self) -> str:
            return self.stack[-1] if self.stack else ""

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            index.add_def(rel, node.name)
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Call(self, node: ast.Call) -> None:
            full = _full_name(node.func)
            if full:
                leaf = full.rsplit(".", 1)[-1]
                qualifier = full[: -len(leaf) - 1] if "." in full else ""
                index.add_use(rel, self._here(), leaf, qualifier)
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                self.visit(arg)
            if isinstance(node.func, ast.Attribute):
                self.visit(node.func.value)

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load):
                index.add_use(rel, self._here(), node.id, "")

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.ctx, ast.Load):
                full = _full_name(node)
                if full and "." in full:
                    leaf = full.rsplit(".", 1)[-1]
                    qualifier = full[: -len(leaf) - 1]
                    index.add_use(rel, self._here(), leaf, qualifier)
            self.generic_visit(node)

    Visitor().visit(tree)


def _qualifier_matches(qualifier: str, rel: str) -> bool:
    if not qualifier:
        return False
    parts = [part for part in Path(rel).with_suffix("").as_posix().split("/") if part not in {"src", "lib", "tests"}]
    if not parts:
        return False
    dotted = ".".join(parts)
    return qualifier == dotted or qualifier == parts[-1] or dotted.endswith("." + qualifier) or qualifier.endswith("." + parts[-1])


def _touched_functions(tree: ast.AST, lines: set[int] | None) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _overlaps(stmt, lines):
                found.append((stmt.name, stmt.lineno or 1))
        elif isinstance(stmt, ast.ClassDef):
            for sub in stmt.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and _overlaps(sub, lines):
                    found.append((sub.name, sub.lineno or 1))
    return found


def _overlaps(node: ast.AST, lines: set[int] | None) -> bool:
    if lines is None:
        return True
    start = getattr(node, "lineno", None) or 1
    end = getattr(node, "end_lineno", None) or start
    return any(start <= line <= end for line in lines)


def _line_spans(project_root: str, files: list[str]) -> dict[str, set[int] | None] | None:
    """None 表示读不到 git。每个文件的 None 表示整文件都算改过，空集合表示这次没改到。"""
    rels = [item.replace("\\", "/") for item in files]
    diff = _run_git(project_root, ["diff", "-U0", "HEAD", "--", *rels])
    tracked = _run_git(project_root, ["ls-files", "--", *rels])
    if diff is None or tracked is None or diff.returncode not in {0, 1} or tracked.returncode != 0:
        return None
    known = {line.strip().replace("\\", "/") for line in (tracked.stdout or "").splitlines() if line.strip()}
    parsed = _parse_diff_lines(diff.stdout or "")
    return {rel: None if rel not in known else parsed.get(rel, set()) for rel in rels}


def _parse_diff_lines(stdout: str) -> dict[str, set[int]]:
    found: dict[str, set[int]] = {}
    for chunk in re.split(r"(?m)^diff --git ", stdout):
        if not chunk.strip():
            continue
        first = chunk.splitlines()[0].strip()
        match = re.match(r"a/(.+) b/(.+)$", first)
        if match is None:
            continue
        rel = match.group(2).replace("\\", "/")
        lines: set[int] = set()
        for hunk in _HUNK.finditer(chunk):
            start = int(hunk.group(1))
            count = int(hunk.group(2) if hunk.group(2) is not None else "1")
            if count <= 0:
                if start > 0:
                    lines.add(start)
            else:
                lines.update(range(start, start + count))
        found[rel] = lines
    return found


def _git_changed(project_root: str) -> list[str] | None:
    diff = _run_git(project_root, ["diff", "--name-only", "HEAD"])
    others = _run_git(project_root, ["ls-files", "--others", "--exclude-standard"])
    if diff is None or others is None or diff.returncode not in {0, 1}:
        return None
    names = []
    for blob in (diff.stdout, others.stdout):
        for line in (blob or "").splitlines():
            item = line.strip().replace("\\", "/")
            if item and item not in names:
                names.append(item)
    return names


def _is_test_path(path: str) -> bool:
    posix = path.replace("\\", "/")
    name = Path(posix).name
    return "/tests/" in f"/{posix}/" or name.startswith("test_") or name.endswith("_test.py")


def _full_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _full_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _raw(tool: str, rule_id: str, message: str, file_path: str, line: int, severity: str, symbol: str) -> dict:
    return {
        "tool": tool,
        "rule_id": rule_id,
        "message": message,
        "file_path": file_path.replace("\\", "/"),
        "line_start": line,
        "line_end": line,
        "severity": severity,
        "symbol": symbol,
    }
