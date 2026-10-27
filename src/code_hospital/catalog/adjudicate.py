"""安全科的独立核实。

拍片的仪器只提出候选。这里重读源码，尝试推翻它。
已确认要能指出低信任输入、越过的边界，以及有界结果。
证不上的写成待验证，不带「必须先处理」的严重度，也不入方。
证伪的写成已排除。没有隔离执行时，不把语法命中写成已经打穿。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SECURITY_TOOLS = frozenset({"security-ruler", "gitleaks", "taint-ruler", "impact-review"})
_LABEL = {"confirmed": "已确认", "needs_validation": "待验证", "rejected": "已排除"}
_SOURCE_HEADS = {"request", "req"}
_SOURCE_ATTRS = {
    "args", "form", "values", "json", "data", "GET", "POST", "query_params",
    "path_params", "cookies", "headers", "files",
}
_SECRET_NAME = re.compile(
    r"(password|passwd|secret|api_key|apikey|access_token|private_key)",
    re.I,
)
_PLACEHOLDER = re.compile(
    r"^(changeme|password|secret|todo|test|example|xxx+|your[-_a-z]*|placeholder|none|null|dummy)$",
    re.I,
)
_QUOTED = re.compile(r"""['\"]([^'\"]{0,200})['\"]""")
_FACTS = {
    "security.exec": "未证实传入的值来自低信任主体，也没有隔离执行观察到代码被执行",
    "security.shell": "未证实命令字符串含低信任输入，也没有观察到命令被执行",
    "security.sql-format": "未证实拼接进来的值来自低信任输入，也没有观察到语句结构被改变",
    "security.deserialize": "未证实反序列化的字节来自低信任主体，也没有隔离执行观察到对象被构造",
    "security.tls-verify": "未证实有低信任网络路径会用到这次关闭校验的请求",
    "security.weak-hash": "未证实这个摘要被用在口令或安全比较上",
    "security.debug": "未证实这个调试入口对低信任调用方开放",
    "security.trust-html": "未证实被标成安全的字符串来自低信任输入",
    "security.dom-write": "未证实写入页面的字符串来自低信任输入",
    "security.weak-random": "未观察到这个随机数被接受方当成口令或令牌",
}
_CLASS = {
    "security.exec": "injection",
    "security.shell": "injection",
    "security.sql-format": "injection",
    "security.deserialize": "deserialization",
    "security.hardcoded-secret": "secret-in-source",
    "security.tls-verify": "transport",
    "security.weak-hash": "transport",
    "security.debug": "authn",
    "security.trust-html": "output-trust",
    "security.dom-write": "output-trust",
    "security.weak-random": "secret-in-source",
    "severe.taint-sql": "injection",
    "severe.taint-command": "injection",
    "severe.taint-path": "injection",
    "severe.taint-request": "injection",
    "severe.review-impact": "impact",
}


def adjudicate_findings(project_root: str, findings: list) -> None:
    """就地写入裁决。其他仪器的发现保持原样。"""
    for finding in findings:
        if finding.tool not in _SECURITY_TOOLS:
            continue
        verdict, because, severity = _review(project_root, finding)
        _apply(finding, verdict, because, severity, _CLASS.get(finding.rule_id, ""))


def _apply(finding, verdict: str, because: str, severity: str, attack_class: str) -> None:
    finding.verdict = verdict
    finding.attack_class = attack_class
    finding.unresolved = because if verdict == "needs_validation" else ""
    if severity:
        finding.severity = severity
    label = _LABEL[verdict]
    if finding.message.startswith(label):
        return
    note = f"{label}：{because}" if because else f"{label}："
    finding.message = f"{note} {finding.message}"[:500]


def _review(project_root: str, finding) -> tuple[str, str, str]:
    if finding.tool == "impact-review":
        return (
            "needs_validation",
            "未核对这是谁的数据、失败时返回什么、状态有没有跳步",
            "info",
        )
    if finding.tool == "gitleaks":
        return _review_leak(project_root, finding)
    if finding.tool == "taint-ruler":
        return _review_taint(project_root, finding)
    return _review_ruler(project_root, finding)


def _review_leak(project_root: str, finding) -> tuple[str, str, str]:
    line = _read_line(project_root, finding.file_path, finding.line_start)
    if line is None:
        return "needs_validation", "这一行现在读不到，不能排除密钥还在", ""
    literals = [item.strip() for item in _QUOTED.findall(line)]
    if literals and all(len(item) < 8 or _PLACEHOLDER.match(item) for item in literals):
        return "rejected", "这一行只有占位符，不是可用的密钥材料", "info"
    return "confirmed", "这一行还能读到，先不要自动改。复核不再展开密钥原文", ""


def _review_ruler(project_root: str, finding) -> tuple[str, str, str]:
    line = _read_line(project_root, finding.file_path, finding.line_start)
    if line is None:
        return "rejected", "这一行已经不在，主张无法复核", "info"
    stripped = line.strip()
    if stripped.startswith("#") or stripped.startswith("//"):
        return "rejected", "命中落在注释里", "info"
    if finding.rule_id == "security.hardcoded-secret":
        return _review_literal_secret(project_root, finding)
    fact = _FACTS.get(finding.rule_id, "未证实低信任输入越过了边界，也没有隔离执行观察到结果")
    if not str(finding.file_path).endswith(".py"):
        return "needs_validation", fact, "minor"
    try:
        tree = ast.parse(_read_text(project_root, finding.file_path))
    except (SyntaxError, OSError):
        return "needs_validation", "源码无法解析，独立复核没有重走这条路径", "minor"
    calls = _calls_on_line(tree, finding.line_start)
    if finding.rule_id == "security.exec":
        matched = [node for node in calls if _call_name(node) in {"eval", "exec"}]
        if matched and all(_constant_call(node) for node in matched):
            return "rejected", "参数是源码字面量，没有低信任输入", "info"
        return "needs_validation", fact, "minor"
    if finding.rule_id == "security.shell":
        if calls and all(_constant_call(node) for node in calls):
            return "rejected", "命令是源码字面量，没有低信任输入", "info"
        return "needs_validation", fact, "minor"
    if finding.rule_id == "security.sql-format":
        dynamic = [node for node in calls if not _constant_call(node)]
        if calls and not dynamic:
            return "rejected", "SQL 参数是源码字面量，没有拼接", "info"
        return "needs_validation", fact, "minor"
    if finding.rule_id == "security.deserialize":
        if not calls and "load" not in line:
            return "rejected", "这一行已经没有反序列化调用", "info"
        return "needs_validation", fact, "minor"
    return "needs_validation", fact, "minor"


def _review_literal_secret(project_root: str, finding) -> tuple[str, str, str]:
    try:
        tree = ast.parse(_read_text(project_root, finding.file_path))
    except (SyntaxError, OSError):
        return "needs_validation", "源码无法解析，独立复核没有重看到密钥字面量", "minor"
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != finding.line_start or not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        literal = value.value.strip()
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if not any(_SECRET_NAME.search(name) for name in names):
            continue
        if len(literal) < 8 or _PLACEHOLDER.match(literal):
            return "rejected", "赋上的是占位符，不是可用的密钥材料", "info"
        return "confirmed", "源码里能读到密钥字面量，先不要自动改", ""
    return "needs_validation", "独立复核没有重看到密钥字面量赋值", "minor"


def _review_taint(project_root: str, finding) -> tuple[str, str, str]:
    if not str(finding.file_path).endswith(".py"):
        return "needs_validation", "污点复核这一期只重走 Python", "minor"
    try:
        tree = ast.parse(_read_text(project_root, finding.file_path))
    except (SyntaxError, OSError):
        return "needs_validation", "源码无法解析，独立复核没有重走函数内数据流", "minor"
    host = _enclosing(tree, finding.line_start)
    body = host.body if host is not None else tree.body
    tainted = set()
    if host is not None:
        tainted = {
            arg.arg for arg in [*host.args.args, *host.args.kwonlyargs]
            if arg.arg in _SOURCE_HEADS
        }
    calls: list[tuple[ast.Call, set[str]]] = []
    _replay(body, tainted, calls)
    sinks = [
        (node, names) for node, names in calls
        if node.lineno == finding.line_start and _sink_kind(node) == finding.rule_id
    ]
    if not sinks:
        return "rejected", "独立复核在这一行没有看到对应出口", "info"
    if any(node.args and _tainted_expr(node.args[0], names) for node, names in sinks):
        return "confirmed", "有界结果只到函数内数据流，没有执行目标代码", "major"
    if all(node.args and _constant_expr(node.args[0]) for node, _names in sinks):
        return "rejected", "出口参数是源码字面量，没有低信任输入", "info"
    return "needs_validation", "拍片称外部输入到了出口，独立复核没有重走出数据流", "minor"


def _replay(stmts, tainted: set[str], calls: list[tuple[ast.Call, set[str]]]) -> None:
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
            _replay(getattr(stmt, "body", []), tainted, calls)
            _replay(getattr(stmt, "orelse", []), tainted, calls)
            for handler in getattr(stmt, "handlers", []):
                _replay(handler.body, tainted, calls)
            continue
        if isinstance(stmt, ast.Assign):
            hot = _tainted_expr(stmt.value, tainted)
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    if hot:
                        tainted.add(target.id)
                    else:
                        tainted.discard(target.id)
            continue
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            if _tainted_expr(stmt.value, tainted):
                tainted.add(stmt.target.id)
            else:
                tainted.discard(stmt.target.id)
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            calls.append((stmt.value, set(tainted)))


def _sink_kind(node: ast.Call) -> str:
    name = _call_name(node)
    leaf = name.rsplit(".", 1)[-1]
    if leaf in {"execute", "executemany"}:
        return "severe.taint-sql"
    if name in {"os.system", "os.popen"} or _shell(node):
        return "severe.taint-command"
    if name == "open" or leaf == "open" or leaf in {"read_text", "read_bytes", "write_text", "write_bytes"}:
        return "severe.taint-path"
    if name in {"requests.get", "requests.post", "requests.put", "requests.patch", "requests.delete", "urlopen"} or name.endswith(".urlopen"):
        return "severe.taint-request"
    return ""


def _tainted_expr(node: ast.AST, tainted: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in tainted
    if isinstance(node, ast.Attribute):
        return _attr_source(node) or _tainted_expr(node.value, tainted)
    if isinstance(node, ast.Subscript):
        return _tainted_expr(node.value, tainted)
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(item, ast.FormattedValue) and _tainted_expr(item.value, tainted)
            for item in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _tainted_expr(node.left, tainted) or _tainted_expr(node.right, tainted)
    if isinstance(node, ast.Call):
        if _direct_source(node):
            return True
        leaf = _call_name(node).rsplit(".", 1)[-1]
        args = [*node.args, *(kw.value for kw in node.keywords)]
        if leaf in {"str", "format", "join", "get"} and any(_tainted_expr(arg, tainted) for arg in args):
            return True
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


def _direct_source(node: ast.Call) -> bool:
    name = _call_name(node)
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


def _constant_call(node: ast.Call) -> bool:
    values = [*node.args, *(kw.value for kw in node.keywords)]
    return bool(values) and all(_constant_expr(item) for item in values)


def _constant_expr(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _call_name(ast.Call(func=func.value, args=[], keywords=[])) if isinstance(func.value, ast.Call) else ""
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        if isinstance(func.value, ast.Attribute) and isinstance(func.value.value, ast.Name):
            return f"{func.value.value.id}.{func.value.attr}.{func.attr}"
        return base or func.attr
    return ""


def _calls_on_line(tree: ast.AST, line: int) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node, "lineno", None) == line
    ]


def _enclosing(tree: ast.AST, line: int):
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        if node.lineno <= line <= end and (best is None or node.lineno >= best.lineno):
            best = node
    return best


def _read_text(project_root: str, file_path: str) -> str:
    path = Path(project_root) / file_path
    return path.read_text(encoding="utf-8", errors="replace")


def _read_line(project_root: str, file_path: str, line: int) -> str | None:
    if line <= 0:
        return None
    try:
        text = _read_text(project_root, file_path)
    except OSError:
        return None
    rows = text.splitlines()
    if line > len(rows):
        return None
    return rows[line - 1]
