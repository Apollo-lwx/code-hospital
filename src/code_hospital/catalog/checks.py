"""检查事项：名册调度代码、外部工具和宿主 LLM。

医院不替 LLM 下判断。代码和外部工具先交事实，LLM 事项保持待交卷。
"""

from __future__ import annotations

import ast
import json
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


_PACK_DIR = Path(__file__).resolve().parent / "check_packs"


def _load_builtin() -> list[dict]:
    rows, problem = _read_list(_PACK_DIR / "base.yaml", "基础名册")
    if problem:
        raise RuntimeError(problem)
    return rows


def _read_list(path: Path, label: str) -> tuple[list[dict], str]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return [], f"{label} 无法解析"
    except OSError:
        return [], f"{label} 读不了"
    if not isinstance(data, list):
        return [], f"{label} 必须是列表"
    try:
        return [_normalize(row) for row in data], ""
    except ValueError as exc:
        return [], f"{label} 没有采用：{exc}"


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
        "group": str(row.get("group") or ""),
    }


def _overlay(project_root: str) -> tuple[set[str], list[dict], list, list[str], str]:
    path = Path(project_root) / "code-hospital.yaml"
    if not path.is_file():
        return set(), [], [], [], ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return set(), [], [], [], "code-hospital.yaml 无法解析，这次只用内置检查事项"
    if not isinstance(data, dict):
        return set(), [], [], [], "code-hospital.yaml 不是映射，这次只用内置检查事项"
    block = (data.get("catalog") or {}).get("checks") or {}
    if not isinstance(block, dict):
        return set(), [], [], [], "catalog.checks 不是映射，这次只用内置检查事项"
    off = {str(item) for item in (block.get("disable") or [])}
    packs = block.get("packs") or []
    if not isinstance(packs, list):
        packs = []
    dirs = [str(item) for item in (block.get("pack_dirs") or [])]
    extra = []
    try:
        for row in block.get("items") or []:
            extra.append(_normalize(row))
    except ValueError as exc:
        return set(), [], packs, dirs, f"catalog.checks 没有采用：{exc}"
    return off, extra, packs, dirs, ""


def _builtin_packs() -> dict[str, Path]:
    if not _PACK_DIR.is_dir():
        return {}
    return {
        path.stem: path
        for path in sorted(_PACK_DIR.glob("*.yaml"))
        if path.stem not in {"base", "visits"}
    }


def _visit_policy() -> tuple[dict[str, list[str]], list[dict], dict]:
    path = _PACK_DIR / "visits.yaml"
    if not path.is_file():
        return {}, [], {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}, [], {}
    if not isinstance(data, dict):
        return {}, [], {}
    visits_raw = data.get("visits") or {}
    visits: dict[str, list[str]] = {}
    if isinstance(visits_raw, dict):
        for key, names in visits_raw.items():
            if isinstance(names, list):
                visits[str(key)] = [str(item) for item in names]
    gates = [row for row in (data.get("when") or []) if isinstance(row, dict) and row.get("pack")]
    requires = data.get("requires") or {}
    if not isinstance(requires, dict):
        requires = {}
    return visits, gates, requires


def _inside(root: Path, raw: str) -> tuple[Path | None, str]:
    text = str(raw).strip()
    if not text:
        return None, "检查包路径是空的"
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None, f"检查包路径必须在仓库内：{text}"
    path = (root / candidate).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None, f"检查包路径越出仓库：{text}"
    return path, ""


def _pack_target(entry) -> tuple[str, str]:
    if isinstance(entry, dict):
        return "file", str(entry.get("file") or entry.get("path") or "")
    text = str(entry).strip()
    if text.endswith((".yaml", ".yml")) or "/" in text or "\\" in text:
        return "file", text
    return "name", text


def _gate_matches(gate: dict, visit_kind: str, files: list[str] | None) -> bool:
    if str(gate.get("visit") or "") != visit_kind:
        return False
    suffixes = [str(item).lower() for item in (gate.get("any_suffix") or [])]
    if not suffixes:
        return True
    return any(Path(item).suffix.lower() in suffixes for item in (files or []))


def _blocked_by(rule: dict, files: list[str] | None, name: str) -> str:
    if not isinstance(rule, dict):
        return ""
    suffixes = [str(item).lower() for item in (rule.get("any_suffix") or [])]
    if suffixes and not any(Path(item).suffix.lower() in suffixes for item in (files or [])):
        return str(rule.get("missing") or f"{name} 这次没排")
    return ""


def load_checks(
    project_root: str,
    visit_kind: str = "checkup",
    files: list[str] | None = None,
) -> tuple[list[dict], str]:
    builtin = _load_builtin()
    off, extra, configured, pack_dirs, note = _overlay(project_root)
    notes = [note] if note else []
    from .outside import overlay as outside_overlay

    user_visit = outside_overlay(project_root)["visits"].get(visit_kind)
    if user_visit:
        configured = list(configured) + list(user_visit["packs"])
    known = _builtin_packs()
    visits, gates, requires = _visit_policy()
    names: list[str] = []
    user_files: list[tuple[str, Path]] = []
    seen_files: set[Path] = set()

    def add_name(name: str) -> None:
        if name and name not in names:
            names.append(name)

    def add_file(label: str, path: Path) -> None:
        if path in seen_files:
            return
        seen_files.add(path)
        user_files.append((label, path))

    root = Path(project_root)
    for entry in configured:
        kind, value = _pack_target(entry)
        if kind == "name":
            add_name(value)
            continue
        path, problem = _inside(root, value)
        if problem:
            notes.append(problem)
            continue
        if path is None or not path.is_file():
            notes.append(f"检查包文件不存在：{value}")
            continue
        add_file(value, path)
    for name in visits.get(visit_kind, []):
        add_name(name)
    for gate in gates:
        if _gate_matches(gate, visit_kind, files):
            add_name(str(gate.get("pack")))
    for raw_dir in pack_dirs:
        folder, problem = _inside(root, raw_dir)
        if problem:
            notes.append(problem)
            continue
        if folder is None or not folder.is_dir():
            notes.append(f"检查包目录不存在：{raw_dir}")
            continue
        found = sorted(folder.glob("*.yaml")) + sorted(folder.glob("*.yml"))
        for path in found:
            add_file(path.name, path)
    pack_rows: list[dict] = []
    for name in names:
        if name not in known:
            notes.append(f"未知检查包 {name}，这次没排")
            continue
        blocked = _blocked_by(requires.get(name) or {}, files, name)
        if blocked:
            notes.append(blocked)
            continue
        rows, problem = _read_list(known[name], name)
        if problem:
            notes.append(problem)
            continue
        pack_rows.extend(rows)
    for label, path in user_files:
        rows, problem = _read_list(path, label)
        if problem:
            notes.append(problem)
            continue
        pack_rows.extend(rows)
    by_id = {row["id"]: row for row in builtin if row["id"] not in off}
    for row in pack_rows + extra:
        if row["id"] in off:
            continue
        by_id[row["id"]] = row
    ordered = [by_id[row["id"]] for row in builtin if row["id"] in by_id]
    seen = {row["id"] for row in ordered}
    for row in pack_rows + extra:
        if row["id"] not in seen and row["id"] not in off:
            ordered.append(by_id[row["id"]])
            seen.add(row["id"])
    return ordered, "；".join(notes)


def run_checks(
    project_root: str,
    files: list[str],
    visit_kind: str = "checkup",
) -> tuple[list[dict], list[str]]:
    """按名册顺序执行。后一项能看见前一项已经交出的事实。"""
    items, note = load_checks(project_root, visit_kind, files)
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
            from .filing import CONTRACT_LINE, contract_for

            prior = "\n\n".join(facts[need] for need in item["needs"])
            own = "\n\n".join(pieces)
            executors.append({
                "kind": "llm",
                "status": "pending",
                "text": _llm_task(item, prior, own),
                "contract": contract_for(item["id"]),
            })
        facts[item["id"]] = "\n\n".join(pieces) or item["question"]
        status = "open" if any(row["status"] == "pending" for row in executors) else "done"
        results.append(_result(item, status, executors, ""))
    return results, notes


def summarize_checks(checks: list[dict]) -> str:
    pending = sum(1 for row in checks if row["status"] == "open")
    budget = sum(1 for row in checks if row["status"] == "budget")
    blocked = sum(1 for row in checks if row["status"] == "blocked")
    absent = sum(
        1
        for row in checks
        for executor in row["executors"]
        if executor["kind"] == "external" and executor["status"] == "absent"
    )
    head = f"检查事项 {len(checks)} 项：待宿主 LLM {pending} 项"
    if budget:
        head += f"，因预算未做 {budget} 项"
    return (
        f"{head}，被前置挡住 {blocked} 项，外部工具未到岗 {absent} 处。"
        "未交卷的事项不能写成已经分析过。"
    )


_ISSUE_LINE = re.compile(
    r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?:\s*(?P<msg>\S.*)$"
)
_NONE_LINE = re.compile(r"^(没有|none)[。.]?$", re.IGNORECASE)
_READ_LINE = re.compile(r"^(已读|read)\s*[:：]\s*(?P<body>.+)$", re.IGNORECASE)


def filing_accepted(note: str) -> bool:
    """合入交卷只有这两种说明算完成。其余说明都表示还不能诊断。"""
    text = (note or "").strip()
    return text.startswith("宿主已交卷") or text.startswith("宿主已通读")


def file_host_diff(
    checks: list[dict], answers: dict | None, scope_files: list[str],
    previous: dict | None = None,
) -> tuple[list[dict], str]:
    """把本轮交卷收成新条目。上一轮已经写入的留在病历里，相同问题不重复返回。"""
    from .filing import settle_filing

    specs, note, ledger = settle_filing(checks, answers, scope_files, previous)
    by_id = {row["id"]: row for row in (ledger or {}).get("items") or []}
    for item in checks:
        row = by_id.get(item.get("id"))
        if row is None or row["status"] not in {"filed", "clear"}:
            continue
        raw = (answers or {}).get(item["id"])
        if raw is None or (isinstance(raw, str) and not str(raw).strip()):
            continue
        text = str(raw).strip() if isinstance(raw, str) else _stored(raw)
        for executor in item.get("executors") or []:
            if executor.get("kind") == "llm":
                executor["status"] = "done"
                executor["text"] = text
        if item.get("executors") and not any(
            executor.get("status") == "pending" for executor in item["executors"]
        ):
            item["status"] = "done"
    return specs, note


def _stored(raw) -> str:
    return json.dumps(raw, ensure_ascii=False)


def _parse_diff_issues(text: str, scope_files: list[str]) -> tuple[list[dict], str]:
    known = _scope_index(scope_files)
    specs: list[dict] = []
    clean = False
    read: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _NONE_LINE.match(line):
            clean = True
            continue
        read_match = _READ_LINE.match(line)
        if read_match:
            read.update(_read_paths(read_match.group("body"), known))
            continue
        issue = _ISSUE_LINE.match(line)
        if issue is None:
            return [], "交卷要按「文件:行: 说明」写，或者写「没有」并另起一行「已读：」列出文件。"
        path = _resolve_path(issue.group("path").strip(), known)
        if not path:
            return [], f"「{issue.group('path').strip()}」不在这次合入范围里，不能写入病历。"
        start = int(issue.group("start"))
        end = int(issue.group("end") or start)
        if start < 1 or end < start:
            return [], f"{path} 的行号无效。"
        specs.append({
            "file_path": path,
            "line_start": start,
            "line_end": end,
            "message": issue.group("msg").strip(),
        })
    if specs and clean:
        return [], "同一份交卷里不能既写问题又写没有。"
    if specs:
        return specs, ""
    if not clean:
        return [], "交卷要按「文件:行: 说明」写，或者写「没有」并另起一行「已读：」列出文件。"
    missing = [path for path in known["paths"] if path not in read]
    if missing:
        shown = "、".join(missing[:8])
        return [], f"还有文件没列入已读（{shown}），不能写成没有功能问题。"
    return [], ""


def _scope_index(scope_files: list[str]) -> dict:
    paths = [rel.replace("\\", "/").lstrip("./") for rel in scope_files]
    names: dict[str, list[str]] = {}
    for path in paths:
        names.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    return {"paths": paths, "by_path": set(paths), "names": names}


def _resolve_path(token: str, known: dict) -> str:
    path = token.replace("\\", "/").lstrip("./")
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        return ""
    if path in known["by_path"]:
        return path
    if "/" in path:
        return ""
    hits = known["names"].get(path) or []
    if len(hits) == 1:
        return hits[0]
    return ""


def _read_paths(body: str, known: dict) -> set[str]:
    found: set[str] = set()
    for token in re.split(r"[\s,，、;；]+", body.strip()):
        if not token:
            continue
        path = _resolve_path(token, known)
        if path:
            found.add(path)
    return found


def _result(item: dict, status: str, executors: list[dict], note: str) -> dict:
    return {
        "id": item["id"],
        "question": item["question"],
        "artifact": item["artifact"],
        "group": item.get("group") or "",
        "needs": list(item["needs"]),
        "status": status,
        "note": note,
        "executors": executors,
    }


def _llm_task(item: dict, prior: str, own: str) -> str:
    from .filing import CONTRACT_LINE

    lines = [
        f"事项 {item['id']}：{item['question']}",
        f"要交出：{item['artifact']}",
        "只根据下面已经交出的事实来写。事实里没有的模块不要编。",
        CONTRACT_LINE,
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


_BASES_SKIP = {"object", "ABC", "Enum", "Exception", "BaseException", "Protocol"}
_CTOR_SKIP = {
    "Exception", "ValueError", "TypeError", "RuntimeError", "KeyError", "HTTPException",
}


def _design_signals(project_root: str, files: list[str]) -> str:
    root = Path(project_root)
    lines = ["设计信号（写法事实，原理判断留给宿主 LLM）："]
    found = 0
    for rel in _source_files(files):
        text = _read(root / rel)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            methods = [item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if len(methods) >= 12:
                found += 1
                lines.append(f"- {rel} {node.name}：{len(methods)} 个方法")
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id not in _BASES_SKIP:
                    found += 1
                    lines.append(f"- {rel} {node.name} 继承 {base.id}")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "__init__":
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                name = call.func.id
                if not name[:1].isupper() or name in _CTOR_SKIP:
                    continue
                found += 1
                lines.append(f"- {rel} {node.name} 直接构造 {name}")
                break
        if found >= 20:
            lines.append("- 其余信号这次没有列出")
            return "\n".join(lines)
    if found == 0:
        lines.append("- 范围内没有看见大类、具体类继承或直接构造具体类")
    return "\n".join(lines)


_JAVA_EXTENDS = re.compile(r"\bclass\s+(\w+)\s+extends\s+(\w+)")
_JAVA_SKIP = {"Exception", "RuntimeException", "Enum", "Record", "Throwable"}


def _java_signals(project_root: str, files: list[str]) -> str:
    root = Path(project_root)
    lines = ["Java 写法（只记录能对上的文件，判断留给宿主 LLM）："]
    found = 0
    java_files = [
        rel.replace("\\", "/")
        for rel in files
        if Path(rel).suffix.lower() == ".java" and not any(part in _SKIP for part in Path(rel).parts)
    ]
    for rel in java_files[:200]:
        text = _read(root / rel)
        if text is None:
            continue
        has_equals = "equals(" in text
        has_hash = "hashCode(" in text
        if has_equals != has_hash:
            found += 1
            side = "有 equals 没有 hashCode" if has_equals else "有 hashCode 没有 equals"
            lines.append(f"- {rel}：{side}")
        for match in _JAVA_EXTENDS.finditer(text):
            parent = match.group(2)
            if parent in _JAVA_SKIP:
                continue
            found += 1
            lines.append(f"- {rel}：{match.group(1)} 继承 {parent}")
        if found >= 20:
            lines.append("- 其余信号这次没有列出")
            return "\n".join(lines)
    if found == 0:
        lines.append("- 范围内没有看见不成对的 equals/hashCode，也没有继承具体类")
    return "\n".join(lines)


_CODE["arch_map"] = _arch_map
_CODE["arch_problems"] = _arch_problems
_CODE["complexity"] = _complexity
_CODE["resources"] = _resources
_CODE["design_signals"] = _design_signals
_CODE["java_signals"] = _java_signals


_WRITE_PREFIX = ("create_", "update_", "delete_", "remove_", "save_")
_AUTH_MARK = ("require_", "authorize", "permission", "current_user", "login_required", "get_current")
_CACHE_NAMES = ("cache", "redis", "lru")


def _function_calls(fn: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name and name not in found:
                found.append(name)
    return found


def _access_signals(project_root: str, files: list[str]) -> str:
    root = Path(project_root)
    lines = ["鉴权与缓存信号（只记写法，越权和击穿留给宿主 LLM）："]
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
            calls = _function_calls(node)
            joined = " ".join(calls)
            if node.name.startswith(_WRITE_PREFIX) and not any(mark in joined for mark in _AUTH_MARK):
                found += 1
                lines.append(f"- {rel} {node.name}：写操作附近没看见鉴权调用")
            cached: set[str] = set()
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Assign) or not isinstance(sub.value, ast.Call):
                    continue
                call_name = _call_name(sub.value).lower()
                if not any(token in call_name for token in _CACHE_NAMES) or not call_name.endswith(".get"):
                    continue
                for target in sub.targets:
                    if isinstance(target, ast.Name):
                        cached.add(target.id)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Name) and sub.value.id in cached:
                    found += 1
                    lines.append(f"- {rel} {node.name}：把缓存里取出的对象交出去")
                    break
            if found >= 20:
                lines.append("- 其余信号这次没有列出")
                return "\n".join(lines)
    if found == 0:
        lines.append("- 范围内没有看见缺少鉴权调用的写操作，也没有把缓存对象交出去")
    return "\n".join(lines)


def _concurrency_signals(project_root: str, files: list[str]) -> str:
    root = Path(project_root)
    lines = ["并发信号："]
    found = 0
    for rel in _source_files(files):
        text = _read(root / rel)
        if text is None:
            continue
        started = "Thread(" in text or "threading.Thread" in text or "asyncio.create_task" in text
        locked = "Lock(" in text or "synchronized" in text
        if started and not locked:
            found += 1
            lines.append(f"- {rel}：启动了线程或任务，文件里没看见 Lock")
        if "self." in text and ("Thread(" in text or "create_task(" in text):
            found += 1
            lines.append(f"- {rel}：有线程或任务，同时也在写 self")
        if found >= 20:
            break
    for rel in files:
        if Path(rel).suffix.lower() != ".java":
            continue
        text = _read(root / rel)
        if text and ("synchronized" in text or "volatile " in text):
            found += 1
            lines.append(f"- {rel.replace(chr(92), '/')}：使用了 synchronized 或 volatile")
    if found == 0:
        lines.append("- 范围内没有看见未加锁的线程，也没有 synchronized / volatile")
    return "\n".join(lines)


def _serialization_signals(project_root: str, files: list[str]) -> str:
    root = Path(project_root)
    lines = ["序列化入口（安全尺已覆盖的危险调用这里只点名，不另判漏洞）："]
    needles = ("pickle.loads", "pickle.load", "ObjectInputStream", "readObject", "implements Serializable")
    found = 0
    candidates = _source_files(files) + [
        rel.replace("\\", "/") for rel in files if Path(rel).suffix.lower() == ".java"
    ]
    for rel in candidates:
        text = _read(root / rel)
        if text is None:
            continue
        for needle in needles:
            if needle in text:
                found += 1
                lines.append(f"- {rel}：出现 {needle}")
                break
        if found >= 20:
            break
    if found == 0:
        lines.append("- 范围内没有看见 pickle、ObjectInputStream、readObject 或 Serializable")
    return "\n".join(lines)


def _naming_signals(project_root: str, files: list[str]) -> str:
    root = Path(project_root)
    lines = ["命名和说明信号（不评文风）："]
    found = 0
    for rel in _source_files(files):
        text = _read(root / rel)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if node.name.startswith("_"):
                continue
            short = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.name) <= 2
            missing = ast.get_docstring(node) is None
            if not short and not missing:
                continue
            found += 1
            if short and missing:
                lines.append(f"- {rel} {node.name}：名字过短，且没有说明")
            elif short:
                lines.append(f"- {rel} {node.name}：名字过短")
            else:
                lines.append(f"- {rel} {node.name}：对外入口没有说明")
            if found >= 20:
                lines.append("- 其余名字这次没有列出")
                return "\n".join(lines)
    if found == 0:
        lines.append("- 范围内没有看见过短的对外名字，也没有缺少说明的顶层入口")
    return "\n".join(lines)


_CODE["arch_map"] = _arch_map
_CODE["arch_problems"] = _arch_problems
_CODE["complexity"] = _complexity
_CODE["resources"] = _resources
_CODE["design_signals"] = _design_signals
_CODE["java_signals"] = _java_signals
_CODE["access_signals"] = _access_signals
_CODE["concurrency_signals"] = _concurrency_signals
_CODE["serialization_signals"] = _serialization_signals
_CODE["naming_signals"] = _naming_signals


def _diff_map(project_root: str, files: list[str]) -> str:
    """合入范围的文件清单。通读由宿主完成，这里不代替读代码。"""
    del project_root
    if not files:
        return "这次范围里没有源文件。没有文件可通读，不能写成没有功能问题。"
    lines = ["合入范围里要打开通读的文件（仪器命中只是证据）："]
    shown = files[:40]
    lines.extend(f"- {rel}" for rel in shown)
    if len(files) > len(shown):
        lines.append(f"- 其余 {len(files) - len(shown)} 个文件同样要读")
    lines.append("没读完的文件不能把这次合入写成没有功能问题。")
    return "\n".join(lines)


_CODE["diff_map"] = _diff_map
