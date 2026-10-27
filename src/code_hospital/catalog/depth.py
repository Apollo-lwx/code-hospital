"""院内深度尺。不依赖外部二进制：安全、缺陷、性能、算法、测试缺口、依赖钉死。

已知漏洞只在本机有 pip-audit 或 osv-scanner 时才查。没有扫描器时写限制项，不当成「没有漏洞」。
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

_SECRET_NAME = re.compile(
    r"(password|passwd|secret|api_key|apikey|access_token|private_key)",
    re.I,
)
_PLACEHOLDER = re.compile(
    r"^(changeme|password|secret|todo|test|example|xxx+|your[-_a-z]*|placeholder|none|null)$",
    re.I,
)
_CONCAT_NAME = re.compile(
    r"^(s|text|buf|out|result|html|sql|query|msg|payload|body|content|script)$",
    re.I,
)
_COLLECTION = re.compile(r"(items|rows|users|files|lines|records|values|entries|list|array|data)s?$", re.I)
_LOOP_IO = {
    "open", "urlopen", "time.sleep",
    "requests.get", "requests.post", "requests.put", "requests.patch", "requests.delete",
    "httpx.get", "httpx.post", "httpx.put", "httpx.request",
}
_VULN_PREFIXES = ("CVE-", "GHSA-", "PYSEC-", "GO-", "RUSTSEC-", "OSV-")
_TEST_CAP = 40

_TEXT_RULES: tuple[tuple[set[str], re.Pattern[str], str, str, str, str], ...] = (
    ({".js", ".jsx", ".ts", ".tsx"}, re.compile(r"\beval\s*\("), "security-ruler", "security.exec", "调用 eval", "critical"),
    ({".js", ".jsx", ".ts", ".tsx"}, re.compile(r"\binnerHTML\s*="), "security-ruler", "security.dom-write", "把字符串写入 innerHTML", "major"),
    ({".js", ".jsx", ".ts", ".tsx"}, re.compile(r"child_process\.(exec|execSync)\s*\("), "security-ruler", "security.shell", "通过 shell 执行命令", "critical"),
    ({".java"}, re.compile(r"Runtime\.getRuntime\(\)\.exec\s*\("), "security-ruler", "security.shell", "执行外部命令", "critical"),
    ({".java"}, re.compile(r"new\s+ObjectInputStream\s*\("), "security-ruler", "security.deserialize", "反序列化不可信字节", "critical"),
    ({".java"}, re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}"), "defect-ruler", "defect.empty-catch", "空的 catch 吃掉了异常", "major"),
    ({".go"}, re.compile(r"""exec\.Command\(\s*["'](sh|bash|cmd)["']"""), "security-ruler", "security.shell", "通过 shell 执行命令", "critical"),
)


def scan_depth(
    project_root: str,
    files: list[str],
    instruments: list[str],
    scope_type: str = "repo",
    visit_kind: str = "checkup",
) -> tuple[dict[str, list[dict]], list[str]]:
    """按仪器 id 分组返回 raw dict。限制项单独列出。"""
    wanted = set(instruments)
    grouped = {item: [] for item in instruments}
    limits: list[str] = []
    root = Path(project_root)
    py_tools = wanted & {"security-ruler", "defect-ruler", "perf-ruler", "algo-ruler"}
    py_files = [rel for rel in files if Path(rel).suffix.lower() == ".py"]
    if py_tools and not py_files:
        covered = _text_tools(files, wanted)
        for inst in instruments:
            if inst in py_tools and inst not in covered:
                limits.append(f"{inst}: 本 scope 无适配文件，跳过")
    for rel in py_files:
        text = _read(root, rel)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            limits.append(f"跳过无法解析的 {rel}")
            continue
        walker = _Walker(rel.replace("\\", "/"), wanted)
        walker.visit(tree)
        for raw in walker.findings:
            grouped.setdefault(raw["tool"], []).append(raw)
    if "security-ruler" in wanted or "defect-ruler" in wanted:
        _scan_text(root, files, wanted, grouped)
    if "supply-ruler" in wanted:
        grouped["supply-ruler"].extend(_scan_supply(root, files))
    if "test-gap" in wanted:
        rows, notes = _scan_test_gap(root, files, scope_type, visit_kind)
        grouped["test-gap"].extend(rows)
        limits.extend(notes)
    return grouped, limits


def run_supply_cve(project_root: str, timeout_s: float) -> tuple[list[dict], list[str]]:
    """pip-audit / osv-scanner 在岗才查已知漏洞。失败写成限制项。"""
    root = Path(project_root)
    raws: list[dict] = []
    notes: list[str] = []
    pip = shutil.which("pip-audit")
    osv = shutil.which("osv-scanner")
    req = root / "requirements.txt"
    locks = [
        name for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.sum")
        if (root / name).is_file()
    ]
    if pip is None and osv is None:
        return [], ["已知漏洞未查：本机没有 pip-audit 或 osv-scanner。这一号只核对了锁文件和版本是否钉死。"]
    if pip and req.is_file():
        found, note = _run_json(
            ["pip-audit", "-r", "requirements.txt", "--format", "json", "--progress-spinner", "off"],
            project_root, timeout_s, "pip-audit",
        )
        raws.extend(_vulns_from_json(found, "requirements.txt"))
        if note:
            notes.append(note)
    if osv and locks:
        for lock in locks:
            found, note = _run_json(
                ["osv-scanner", f"--lockfile={lock}", "--format", "json"],
                project_root, timeout_s, "osv-scanner",
            )
            raws.extend(_vulns_from_json(found, lock))
            if note:
                notes.append(note)
    if not raws and not notes and not ((pip and req.is_file()) or (osv and locks)):
        notes.append("已知漏洞未查：没有 requirements.txt 或锁文件可供 pip-audit / osv-scanner 使用。")
    return raws, notes


def _run_json(cmd: list[str], cwd: str, timeout_s: float, label: str) -> tuple[object, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            timeout=max(5.0, timeout_s), encoding="utf-8", errors="replace", check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, f"{label} 未完成，已知漏洞这次没查完"
    if not (proc.stdout or "").strip():
        if proc.returncode not in (0, 1):
            return None, f"{label} 未完成，已知漏洞这次没查完"
        return None, ""
    try:
        return json.loads(proc.stdout), ""
    except json.JSONDecodeError:
        return None, f"{label} 输出无法解析，已知漏洞这次没查完"


def _vulns_from_json(payload: object, file_path: str) -> list[dict]:
    found: list[dict] = []
    _collect_vulns(payload, found)
    seen: set[str] = set()
    raws: list[dict] = []
    for item in found:
        vid = item["id"]
        if vid in seen:
            continue
        seen.add(vid)
        summary = str(item.get("summary") or item.get("details") or item.get("description") or "")
        summary = re.sub(r"\s+", " ", summary)[:120]
        message = f"依赖存在已知漏洞 {vid}"
        if summary:
            message = f"{message}：{summary}"
        raws.append(_raw("supply-ruler", "supply.known-vuln", message, file_path, 1, "major"))
    return raws


def _collect_vulns(node: object, found: list[dict]) -> None:
    if isinstance(node, dict):
        vid = node.get("id")
        if isinstance(vid, str) and vid.startswith(_VULN_PREFIXES):
            found.append(node)
        for value in node.values():
            _collect_vulns(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_vulns(item, found)


class _Walker(ast.NodeVisitor):
    def __init__(self, file_path: str, wanted: set[str]) -> None:
        self.file_path = file_path
        self.wanted = wanted
        self.fn = ""
        self.loop = 0
        self.findings: list[dict] = []
        self._test_file = _is_test_path(file_path)
        self._import_probe = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._defaults(node)
        self._exits(node.body)
        prev = self.fn
        self.fn = node.name
        self.generic_visit(node)
        self.fn = prev

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_If(self, node: ast.If) -> None:
        self._identical(node)
        self._exits(node.body)
        self._exits(node.orelse)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.target)
        self.visit(node.iter)
        self.loop += 1
        if self.loop >= 3 and not _bounded_range(node):
            self._emit(
                "algo-ruler", "algo.nested-loop",
                f"函数 {self.fn or '（模块）'} 里有 {self.loop} 层循环，开销可能随输入连乘。这是写法估计",
                node, "minor",
            )
        self._exits(node.body)
        for stmt in node.body:
            self.visit(stmt)
        self.loop -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    visit_AsyncFor = visit_For  # type: ignore[assignment]

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self.loop += 1
        if self.loop >= 3:
            self._emit(
                "algo-ruler", "algo.nested-loop",
                f"函数 {self.fn or '（模块）'} 里有 {self.loop} 层循环，开销可能随输入连乘。这是写法估计",
                node, "minor",
            )
        for stmt in node.body:
            self.visit(stmt)
        self.loop -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Try(self, node: ast.Try) -> None:
        prev = self._import_probe
        self._import_probe = _import_probe(node)
        self.generic_visit(node)
        self._import_probe = prev

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if _swallowed(node) and not self._import_probe:
            self._emit("defect-ruler", "defect.swallowed", "异常被接住后没有再抛出或记录", node, "major")
        elif node.type is None:
            self._emit("defect-ruler", "defect.bare-except", "裸 except 会把退出和键盘中断也接住", node, "major")
        self._exits(node.body)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        seen: list[object] = []
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, (str, int)):
                if key.value in seen:
                    self._emit(
                        "defect-ruler", "defect.duplicate-key",
                        f"字典键 {key.value!r} 写了两次，前面的值会被盖掉",
                        key, "major",
                    )
                seen.append(key.value)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op, comp in zip(node.ops, node.comparators):
            if len(node.ops) == 1 and isinstance(op, ast.Eq) and ast.dump(node.left) == ast.dump(comp):
                self._emit("defect-ruler", "defect.self-compare", "左右是同一个表达式，比较结果恒为真", node, "major")
            if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(comp, ast.Constant) and comp.value is not None:
                self._emit("defect-ruler", "defect.is-literal", "用 is 比较字面量，比的是身份而不是值", node, "major")
            if self.loop and isinstance(op, ast.In) and isinstance(comp, ast.Name) and _COLLECTION.search(comp.id):
                self._emit(
                    "algo-ruler", "algo.linear-membership",
                    f"循环里判断成员是否在 {comp.id} 中；若它是列表，整体接近平方复杂度",
                    node, "minor",
                )
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if (
            self.loop and isinstance(node.op, ast.Add)
            and isinstance(node.target, ast.Name) and _CONCAT_NAME.match(node.target.id)
            and not isinstance(node.value, ast.Constant)
        ):
            self._emit(
                "algo-ruler", "algo.string-concat-loop",
                f"循环里用 += 拼接 {node.target.id}，字符串会反复复制",
                node, "major",
            )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._secret(node.targets, node.value, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._secret([node.target], node.value, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        self._security_call(node, name)
        self._perf_call(node, name)
        self._algo_call(node, name)
        self.generic_visit(node)

    def _defaults(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        defaults = list(node.args.defaults) + [item for item in node.args.kw_defaults if item is not None]
        for default in defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self._emit(
                    "defect-ruler", "defect.mutable-default",
                    f"函数 {node.name} 用可变对象做默认参数，调用之间会共享同一份",
                    default, "major",
                )

    def _identical(self, node: ast.If) -> None:
        if not node.orelse:
            return
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            return
        if [ast.dump(stmt) for stmt in node.body] == [ast.dump(stmt) for stmt in node.orelse]:
            self._emit("defect-ruler", "defect.identical-branches", "if 和 else 的函数体相同，分支没有区别", node, "major")

    def _exits(self, stmts: list[ast.stmt]) -> None:
        exited = False
        for stmt in stmts:
            if exited:
                self._emit("defect-ruler", "defect.unreachable", "return 或 raise 之后还有语句，后面不会执行", stmt, "major")
                return
            if isinstance(stmt, (ast.Return, ast.Raise)):
                exited = True

    def _secret(self, targets: list[ast.expr], value: ast.expr, node: ast.AST) -> None:
        if "security-ruler" not in self.wanted or self._test_file:
            return
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return
        literal = value.value.strip()
        if len(literal) < 8 or _PLACEHOLDER.match(literal) or "example" in literal.lower():
            return
        for target in targets:
            if isinstance(target, ast.Name) and _SECRET_NAME.search(target.id):
                self._emit(
                    "security-ruler", "security.hardcoded-secret",
                    f"{target.id} 被写成了源码里的字面量",
                    node, "critical",
                )

    def _security_call(self, node: ast.Call, name: str) -> None:
        if "security-ruler" not in self.wanted:
            return
        if name in {"eval", "exec"}:
            self._emit("security-ruler", "security.exec", f"调用 {name}，参数若来自外部就能执行任意代码", node, "critical")
        if name in {"os.system", "os.popen"} or _shell_true(node):
            self._emit("security-ruler", "security.shell", "通过 shell 执行命令，拼接进来的参数会被当成命令", node, "critical")
        if name in {"pickle.loads", "pickle.load", "marshal.loads", "dill.loads", "yaml.unsafe_load"}:
            self._emit("security-ruler", "security.deserialize", f"{name} 会执行不可信数据里的对象", node, "critical")
        if name == "yaml.load" and not _safe_loader(node):
            self._emit("security-ruler", "security.deserialize", "yaml.load 没有使用 SafeLoader", node, "critical")
        if name in {"hashlib.md5", "hashlib.sha1"} and not _kw_false(node, "usedforsecurity"):
            self._emit("security-ruler", "security.weak-hash", f"{name} 不适合做安全摘要", node, "major")
        if _kw_false(node, "verify"):
            self._emit("security-ruler", "security.tls-verify", "关闭了 TLS 证书校验", node, "major")
        if name.endswith(".run") and _kw_true(node, "debug"):
            self._emit("security-ruler", "security.debug", "调试模式开在源码里，会暴露堆栈和重载器", node, "major")
        if name in {"mark_safe", "Markup"}:
            self._emit("security-ruler", "security.trust-html", "把字符串标成安全 HTML，来自用户时会变成 XSS", node, "major")
        if self._weak_random(name):
            self._emit("security-ruler", "security.weak-random", "用 random 生成密钥或令牌，结果可以预测", node, "major")
        if name.endswith(".execute") or name.endswith(".executemany") or name in {"execute", "executemany"}:
            arg = node.args[0] if node.args else None
            if arg is not None and _sql_dynamic(arg):
                self._emit("security-ruler", "security.sql-format", "SQL 用拼接或格式化组成，输入会改变语句结构", node, "critical")

    def _perf_call(self, node: ast.Call, name: str) -> None:
        if "perf-ruler" not in self.wanted or not self.loop:
            return
        leaf = name.rsplit(".", 1)[-1]
        arg = node.args[0] if node.args else None
        if leaf in {"execute", "executemany"} and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            sql = arg.value.lower()
            if "select" in sql and "limit" not in sql:
                self._emit("perf-ruler", "perf.unbounded-query", "循环里的查询没有 LIMIT", node, "major")
                return
        if name in _LOOP_IO or leaf in {"execute", "executemany", "query"}:
            self._emit(
                "perf-ruler", "perf.loop-io",
                f"循环里调用 {name or leaf}，可能每次都访问外部资源。这是写法估计",
                node, "minor",
            )

    def _algo_call(self, node: ast.Call, name: str) -> None:
        if "algo-ruler" not in self.wanted or not self.loop:
            return
        leaf = name.rsplit(".", 1)[-1]
        if name == "sorted" or leaf == "sort":
            self._emit(
                "algo-ruler", "algo.sort-in-loop",
                "循环里排序，可能比循环外排一次更贵。这是写法估计",
                node, "minor",
            )

    def _weak_random(self, name: str) -> bool:
        if not name.startswith("random."):
            return False
        return bool(_SECRET_NAME.search(self.fn))

    def _emit(self, tool: str, rule_id: str, message: str, node: ast.AST, severity: str) -> None:
        if tool not in self.wanted:
            return
        self.findings.append(_raw(
            tool, rule_id, message, self.file_path,
            getattr(node, "lineno", 1) or 1, severity,
            getattr(node, "end_lineno", None) or getattr(node, "lineno", 1) or 1,
            self.fn,
        ))


def _text_tools(files: list[str], wanted: set[str]) -> set[str]:
    covered: set[str] = set()
    for rel in files:
        suffix = Path(rel).suffix.lower()
        for exts, _pattern, tool, _rule, _message, _severity in _TEXT_RULES:
            if tool in wanted and suffix in exts:
                covered.add(tool)
    return covered


def _scan_text(root: Path, files: list[str], wanted: set[str], grouped: dict[str, list[dict]]) -> None:
    for rel in files:
        suffix = Path(rel).suffix.lower()
        if suffix == ".py":
            continue
        text = _read(root, rel)
        if text is None:
            continue
        for exts, pattern, tool, rule_id, message, severity in _TEXT_RULES:
            if tool not in wanted or suffix not in exts:
                continue
            for index, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(("//", "#", "*", "/*")):
                    continue
                if pattern.search(line):
                    grouped.setdefault(tool, []).append(
                        _raw(tool, rule_id, message, rel.replace("\\", "/"), index, severity)
                    )


def _scan_supply(root: Path, files: list[str]) -> list[dict]:
    raws: list[dict] = []
    seen: set[Path] = set()
    manifests: list[Path] = []
    for rel in files:
        path = root / rel
        name = path.name
        if name.startswith("requirements") and name.endswith(".txt") and not name.endswith(".in"):
            manifests.append(path)
        elif name == "package.json":
            manifests.append(path)
    for path in root.glob("requirements*.txt"):
        if path.is_file() and not path.name.endswith(".in"):
            manifests.append(path)
    package_json = root / "package.json"
    if package_json.is_file():
        manifests.append(package_json)
    for path in manifests:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        rel = _rel(root, resolved)
        if path.name == "package.json":
            raws.extend(_scan_package_json(root, resolved, rel))
        elif path.name.startswith("requirements"):
            raws.extend(_scan_requirements(resolved, rel))
    return raws


def _scan_requirements(path: Path, rel: str) -> list[dict]:
    raws: list[dict] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for index, line in enumerate(text.splitlines(), 1):
        raw = line.split("#", 1)[0].strip()
        if not raw or raw.startswith("-"):
            continue
        if raw.startswith("http://"):
            raws.append(_raw("supply-ruler", "supply.http-source", "依赖地址是明文 HTTP", rel, index, "major"))
            continue
        if raw.startswith("git+") or "git+" in raw:
            if "@" not in raw.split("git+", 1)[-1]:
                raws.append(_raw("supply-ruler", "supply.unpinned-vcs", "Git 依赖没有钉到提交", rel, index, "major"))
            continue
        if any(op in raw for op in ("==", "~=", "===")):
            continue
        raws.append(_raw("supply-ruler", "supply.unpinned", f"依赖 {raw.split('[', 1)[0]} 没有钉死版本", rel, index, "minor"))
    return raws


def _scan_package_json(root: Path, path: Path, rel: str) -> list[dict]:
    raws: list[dict] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return raws
    if not isinstance(data, dict):
        return raws
    locks = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "npm-shrinkwrap.json")
    if not any((root / name).is_file() for name in locks):
        raws.append(_raw("supply-ruler", "supply.missing-lock", "有 package.json，没有锁文件", rel, 1, "major"))
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        deps = data.get(section) or {}
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            text = str(spec).strip()
            if text.startswith("http://"):
                raws.append(_raw("supply-ruler", "supply.http-source", f"{name} 使用明文 HTTP", rel, 1, "major"))
            elif text in {"*", "latest", ""}:
                raws.append(_raw("supply-ruler", "supply.wildcard", f"{name} 的版本是通配", rel, 1, "major"))
            elif text.startswith(("git:", "git+", "github:")) and "#" not in text:
                raws.append(_raw("supply-ruler", "supply.unpinned-vcs", f"{name} 的 Git 依赖没有钉到提交", rel, 1, "major"))
    return raws


def _scan_test_gap(
    root: Path, files: list[str], scope_type: str, visit_kind: str,
) -> tuple[list[dict], list[str]]:
    if scope_type == "repo" and visit_kind != "test":
        return [], ["全仓库号别不逐文件开测试缺口。要逐文件查，挂号别用 test。"]
    raws: list[dict] = []
    for rel in files:
        path = Path(rel)
        if path.suffix.lower() != ".py" or path.name in {"__init__.py", "conftest.py"}:
            continue
        if _is_test_path(rel):
            continue
        stem = path.stem
        candidates = [
            root / path.parent / f"test_{stem}.py",
            root / "tests" / f"test_{stem}.py",
            root / path.parent / "tests" / f"test_{stem}.py",
            root / path.parent / f"{stem}_test.py",
        ]
        if any(item.is_file() for item in candidates):
            continue
        raws.append(_raw(
            "test-gap", "test.uncovered-file",
            f"{rel} 没有对应的 test_{stem}.py，这次改动缺少直接测试",
            rel.replace("\\", "/"), 1, "major",
        ))
        if len(raws) >= _TEST_CAP:
            return raws, [f"测试缺口只列出前 {_TEST_CAP} 个源文件"]
    return raws, []


def _raw(
    tool: str, rule_id: str, message: str, file_path: str,
    line: int, severity: str, line_end: int | None = None, symbol: str = "",
) -> dict:
    return {
        "tool": tool,
        "rule_id": rule_id,
        "message": message,
        "file_path": file_path.replace("\\", "/"),
        "line_start": line,
        "line_end": line_end or line,
        "severity": severity,
        "symbol": symbol,
    }


def _read(root: Path, rel: str) -> str | None:
    path = root / rel
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


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


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            return f"{base.id}.{func.attr}"
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            return f"{base.value.id}.{base.attr}.{func.attr}"
        return func.attr
    return ""


def _shell_true(node: ast.Call) -> bool:
    name = _call_name(node)
    if not (name.startswith("subprocess.") or name in {"run", "Popen", "call", "check_output", "check_call"}):
        return False
    return _kw_true(node, "shell")


def _kw_true(node: ast.Call, arg: str) -> bool:
    for kw in node.keywords:
        if kw.arg == arg and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def _kw_false(node: ast.Call, arg: str) -> bool:
    for kw in node.keywords:
        if kw.arg == arg and isinstance(kw.value, ast.Constant) and kw.value.value is False:
            return True
    return False


def _safe_loader(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg != "Loader":
            continue
        value = kw.value
        if isinstance(value, ast.Attribute) and value.attr in {"SafeLoader", "CSafeLoader"}:
            return True
        if isinstance(value, ast.Name) and value.id in {"SafeLoader", "CSafeLoader"}:
            return True
    return False


def _sql_dynamic(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return True
    return False


def _bounded_range(node: ast.For) -> bool:
    it = node.iter
    if not isinstance(it, ast.Call) or not isinstance(it.func, ast.Name) or it.func.id != "range":
        return False
    if not it.args or not isinstance(it.args[0], ast.Constant):
        return False
    return isinstance(it.args[0].value, int) and it.args[0].value <= 8


def _import_probe(node: ast.Try) -> bool:
    """只为了探测可选模块：失败后继续往下走，不算把异常吃掉。"""
    if not node.handlers or not node.body:
        return False
    saw_import = False
    for stmt in node.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            saw_import = True
            continue
        if isinstance(stmt, (ast.Return, ast.Pass)):
            continue
        return False
    return saw_import and all(_swallowed(handler) for handler in node.handlers)


def _swallowed(node: ast.ExceptHandler) -> bool:
    meaningful = []
    for stmt in node.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        meaningful.append(stmt)
    return not meaningful
