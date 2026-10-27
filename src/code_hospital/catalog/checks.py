"""检查事项：名册调度代码、外部工具和宿主 LLM。

医院不替 LLM 下判断。代码和外部工具先交事实，LLM 事项保持待交卷。
"""

from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path

import yaml

_EXECUTORS = {"code", "external", "llm"}
_SKIP = {"tests", "test", "__pycache__", "venv", ".venv", "node_modules", ".git", ".hospital"}
_ENTRY_LINE = re.compile(r"^\s*if\s+__name__\s*==", re.M)
_APP = re.compile(r"\bFastAPI\s*\(")
_CAP = 4000
_CODE = {}


def _load_builtin() -> list[dict]:
    path = Path(__file__).with_name("checks.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("检查事项名册必须是列表")
    return [_normalize(row) for row in data]


def _normalize(row: dict) -> dict:
    if not isinstance(row, dict) or not str(row.get("id") or "").strip():
        raise ValueError("检查事项缺少 id")
    executors = [str(item) for item in (row.get("executors") or [])]
    unknown = [item for item in executors if item not in _EXECUTORS]
    if unknown or not executors:
        raise ValueError(f"{row.get('id')} 的执行方只能是 code、external、llm")
    return {
        "id": str(row["id"]),
        "question": str(row.get("question") or row["id"]),
        "needs": [str(item) for item in (row.get("needs") or [])],
        "executors": executors,
        "code": str(row.get("code") or ""),
        "external": [str(item) for item in (row.get("external") or [])],
        "artifact": str(row.get("artifact") or ""),
    }


def _overlay(project_root: str) -> tuple[set[str], list[dict], str]:
    path = Path(project_root) / "code-hospital.yaml"
    if not path.is_file():
        return set(), [], ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return set(), [], "code-hospital.yaml 无法解析，这次只用内置检查事项"
    if not isinstance(data, dict):
        return set(), [], "code-hospital.yaml 不是映射，这次只用内置检查事项"
    block = (data.get("catalog") or {}).get("checks") or {}
    if not isinstance(block, dict):
        return set(), [], "catalog.checks 不是映射，这次只用内置检查事项"
    off = {str(item) for item in (block.get("disable") or [])}
    extra = []
    try:
        for row in block.get("items") or []:
            extra.append(_normalize(row))
    except ValueError as exc:
        return set(), [], f"catalog.checks 没有采用：{exc}"
    return off, extra, ""


def load_checks(project_root: str) -> tuple[list[dict], str]:
    builtin = _load_builtin()
    off, extra, note = _overlay(project_root)
    by_id = {row["id"]: row for row in builtin if row["id"] not in off}
    for row in extra:
        if row["id"] in off:
            continue
        by_id[row["id"]] = row
    ordered = [by_id[row["id"]] for row in builtin if row["id"] in by_id]
    seen = {row["id"] for row in ordered}
    for row in extra:
        if row["id"] not in seen and row["id"] not in off:
            ordered.append(by_id[row["id"]])
    return ordered, note


def run_checks(project_root: str, files: list[str]) -> tuple[list[dict], list[str]]:
    """按名册顺序执行。后一项能看见前一项已经交出的事实。"""
    items, note = load_checks(project_root)
    notes = [note] if note else []
    facts: dict[str, str] = {}
    results: list[dict] = []
    known = {row["id"] for row in items}
    for item in items:
        missing = [need for need in item["needs"] if need not in facts]
        unknown = [need for need in item["needs"] if need not in known]
        if unknown:
            results.append(_result(item, "blocked", [], f"前置事项不在名册里：{'、'.join(unknown)}"))
            continue
        if missing:
            results.append(_result(item, "blocked", [], f"先完成 {'、'.join(missing)}"))
            continue
        executors: list[dict] = []
        pieces: list[str] = []
        if "code" in item["executors"]:
            text = _run_code(item.get("code") or "", project_root, files)
            executors.append({"kind": "code", "status": "done", "text": text})
            pieces.append(text)
        if "external" in item["executors"]:
            external = _external(item.get("external") or [])
            executors.append(external)
            if external["text"]:
                pieces.append(external["text"])
        if "llm" in item["executors"]:
            prior = "\n\n".join(facts[need] for need in item["needs"])
            own = "\n\n".join(pieces)
            executors.append({
                "kind": "llm",
                "status": "pending",
                "text": _llm_task(item, prior, own),
            })
        facts[item["id"]] = "\n\n".join(pieces) or item["question"]
        status = "open" if any(row["status"] == "pending" for row in executors) else "done"
        results.append(_result(item, status, executors, ""))
    return results, notes


def summarize_checks(checks: list[dict]) -> str:
    pending = sum(1 for row in checks if row["status"] == "open")
    blocked = sum(1 for row in checks if row["status"] == "blocked")
    absent = sum(
        1
        for row in checks
        for executor in row["executors"]
        if executor["kind"] == "external" and executor["status"] == "absent"
    )
    return (
        f"检查事项 {len(checks)} 项：待宿主 LLM {pending} 项，被前置挡住 {blocked} 项，"
        f"外部工具未到岗 {absent} 处。未交卷的事项不能写成已经分析过。"
    )


def _result(item: dict, status: str, executors: list[dict], note: str) -> dict:
    return {
        "id": item["id"],
        "question": item["question"],
        "artifact": item["artifact"],
        "needs": list(item["needs"]),
        "status": status,
        "note": note,
        "executors": executors,
    }


def _llm_task(item: dict, prior: str, own: str) -> str:
    lines = [
        f"事项 {item['id']}：{item['question']}",
        f"要交出：{item['artifact']}",
        "只根据下面已经交出的事实来写。事实里没有的模块不要编。",
    ]
    if prior:
        lines += ["", "前置产物：", prior]
    if own:
        lines += ["", "本项事实：", own]
    return "\n".join(lines)[:_CAP]


def _run_code(name: str, project_root: str, files: list[str]) -> str:
    fn = _CODE.get(name)
    if fn is None:
        return f"没有名为 {name or '（空）'} 的代码执行方"
    return fn(project_root, files)[:_CAP]


def _external(names: list[str]) -> dict:
    if not names:
        return {"kind": "external", "status": "absent", "text": "这项没有点名外部工具"}
    present: list[str] = []
    absent: list[str] = []
    for name in names:
        if shutil.which(name):
            present.append(name)
        else:
            absent.append(f"{name} 未到岗")
    lines = []
    if present:
        lines.append("在岗：" + "、".join(present) + "。完整输出仍由对应仪器写入检验发现。")
    if absent:
        lines.append("未到岗：" + "、".join(absent) + "。这一方缺席，代码和宿主 LLM 仍要交自己的卷。")
    status = "done" if present and not absent else "absent" if not present else "partial"
    return {"kind": "external", "status": status, "text": "\n".join(lines)}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None


def _imports(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def _top(rel: str) -> str:
    parts = [part for part in Path(rel).parts if part not in {"src"}]
    if not parts:
        return "(根)"
    if len(parts) == 1 and parts[0].endswith(".py"):
        return "(根上的脚本)"
    return parts[0]


def _source_files(files: list[str]) -> list[str]:
    chosen = []
    for rel in files:
        path = Path(rel)
        if path.suffix.lower() != ".py":
            continue
        if any(part in _SKIP for part in path.parts):
            continue
        chosen.append(rel.replace("\\", "/"))
    return chosen[:300]


def _arch_map(project_root: str, files: list[str]) -> str:
    root = Path(project_root)
    modules: dict[str, int] = {}
    entries: list[str] = []
    imported: list[tuple[str, str, str]] = []
    for rel in _source_files(files):
        text = _read(root / rel)
        if text is None:
            continue
        top = _top(rel)
        modules[top] = modules.get(top, 0) + 1
        name = Path(rel).name
        if name in {"main.py", "app.py", "server.py", "cli.py", "__main__.py"} or _APP.search(text) or _ENTRY_LINE.search(text):
            entries.append(rel)
        for module in _imports(text):
            imported.append((top, module.split(".")[0], rel))
    known = set(modules)
    lines = ["模块（范围内的源码文件数）："]
    if modules:
        for name, count in sorted(modules.items(), key=lambda item: (-item[1], item[0]))[:30]:
            lines.append(f"- {name}：{count} 个文件")
    else:
        lines.append("- 范围内没有源码模块")
    lines.append("入口：")
    lines.extend(f"- {item}" for item in entries[:20]) if entries else lines.append("- 范围内没有认出入口")
    lines.append("仓库内部依赖：")
    seen: set[tuple[str, str]] = set()
    for source, target, rel in imported:
        if target not in known or source == target or (source, target) in seen:
            continue
        seen.add((source, target))
        lines.append(f"- {source} → {target}（{rel}）")
    if not seen:
        lines.append("- 没有认出跨模块 import")
    lines.append("这是从路径和 import 抽出的事实，结构名称留给宿主 LLM。")
    return "\n".join(lines)


def _arch_problems(project_root: str, files: list[str]) -> str:
    from .layers import _scan_code_hospital, resolve_contract

    contract = resolve_contract(project_root)
    if contract is None:
        return "未配置分层契约。代码这一方不判断依赖方向，结构问题留给宿主 LLM。"
    if contract["kind"] != "code-hospital":
        return f"分层契约来自 {contract['id']}。违例由分层尺执行，这里不重复跑外部命令。"
    layers = " → ".join(contract["layers"])
    raws = _scan_code_hospital(Path(project_root), files, contract["layers"])
    if not raws:
        return f"分层契约是 {layers}。范围内没有发现低层依赖高层。"
    lines = [f"分层契约是 {layers}。低层依赖了高层："]
    for row in raws[:20]:
        lines.append(f"- {row['file_path']}：{row['message']}")
    return "\n".join(lines)


def _complexity(project_root: str, files: list[str]) -> str:
    root = Path(project_root)
    lines = ["带循环的函数（写法估计，不证明渐近界）："]
    found = 0
    for rel in _source_files(files):
        text = _read(root / rel)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            loops, calls = _loops(node)
            if loops < 1:
                continue
            found += 1
            call_text = "、".join(calls[:4]) if calls else "没有直接调用"
            lines.append(f"- {rel} {node.name}：{loops} 层循环，循环内 {call_text}")
            if found >= 25:
                lines.append("- 其余函数这次没有列出")
                return "\n".join(lines)
    if found == 0:
        lines.append("- 范围内没有看见循环")
    return "\n".join(lines)


def _loops(fn: ast.AST) -> tuple[int, list[str]]:
    depth = 0
    max_depth = 0
    calls: list[str] = []

    class Walker(ast.NodeVisitor):
        def visit_For(self, node: ast.For) -> None:
            nonlocal depth, max_depth
            depth += 1
            max_depth = max(max_depth, depth)
            self.generic_visit(node)
            depth -= 1

        visit_AsyncFor = visit_For
        visit_While = visit_For

        def visit_Call(self, node: ast.Call) -> None:
            if depth:
                name = _call_name(node)
                if name and name not in calls:
                    calls.append(name)
            self.generic_visit(node)

    Walker().visit(fn)
    return max_depth, calls


def _resources(project_root: str, files: list[str]) -> str:
    root = Path(project_root)
    lines = ["可能没归还的资源（只看本函数，跨函数留给宿主 LLM）："]
    found = 0
    for rel in _source_files(files):
        text = _read(root / rel)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in _naked_resources(node):
                found += 1
                lines.append(f"- {rel} {node.name}：{call} 没有放进 with")
                if found >= 25:
                    lines.append("- 其余调用这次没有列出")
                    return "\n".join(lines)
    if found == 0:
        lines.append("- 范围内没有看见裸的 open、connect、create_engine 或 Session")
    return "\n".join(lines)


def _naked_resources(fn: ast.AST) -> list[str]:
    hits: list[str] = []

    class Walker(ast.NodeVisitor):
        def __init__(self) -> None:
            self.shield = 0

        def visit_With(self, node: ast.With) -> None:
            self.shield += 1
            self.generic_visit(node)
            self.shield -= 1

        visit_AsyncWith = visit_With

        def visit_Call(self, node: ast.Call) -> None:
            name = _call_name(node)
            leaf = name.rsplit(".", 1)[-1]
            if self.shield == 0 and leaf in {"open", "connect", "create_engine", "Session"}:
                if name not in hits:
                    hits.append(name or leaf)
            self.generic_visit(node)

    Walker().visit(fn)
    return hits


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            return f"{base.id}.{func.attr}"
        return func.attr
    return ""


_CODE["arch_map"] = _arch_map
_CODE["arch_problems"] = _arch_problems
_CODE["complexity"] = _complexity
_CODE["resources"] = _resources
