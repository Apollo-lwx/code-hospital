"""分层尺：只采用优先级最高的一份契约，不把多份违例叠在一起。"""

from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path

_IMPORT_LINTER_FILES = (".importlinter",)
_CRUISER_FILES = (
    ".dependency-cruiser.js",
    ".dependency-cruiser.cjs",
    "dependency-cruiser.config.js",
)
_SKIP = {".git", ".hospital", "node_modules", "target", "build", ".venv", "venv"}
_LAYER_RULE = "layer.forbidden-import"


def resolve_contract(project_root: str) -> dict | None:
    """返回第一份契约。id 写入 scope_key 的 contract: 段。"""
    root = Path(project_root)
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and "[tool.importlinter]" in pyproject.read_text(encoding="utf-8", errors="replace"):
        return {"id": "import-linter", "kind": "import-linter"}
    if any((root / name).is_file() for name in _IMPORT_LINTER_FILES):
        return {"id": "import-linter", "kind": "import-linter"}
    if _has_archunit(root):
        return {"id": "archunit", "kind": "archunit"}
    if any((root / name).is_file() for name in _CRUISER_FILES):
        return {"id": "dependency-cruiser", "kind": "dependency-cruiser"}
    layers = _catalog_layers(root)
    if layers:
        return {"id": "code-hospital", "kind": "code-hospital", "layers": layers}
    return None


def _catalog_layers(root: Path) -> list[str]:
    path = root / "code-hospital.yaml"
    if not path.is_file():
        return []
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    layers = ((data.get("catalog") or {}).get("layers") or [])
    return [str(item) for item in layers if str(item).strip()]


def _has_archunit(root: Path) -> bool:
    seen = 0
    for path in root.rglob("*.java"):
        if any(part in _SKIP for part in path.parts):
            continue
        seen += 1
        if seen > 2000:
            break
        text = path.read_text(encoding="utf-8", errors="replace")
        if "com.tngtech.archunit" in text or "ArchRule" in text:
            return True
    return False


def _layer_index(path: str, layers: list[str]) -> int | None:
    parts = set(Path(path).parts)
    for index, name in enumerate(layers):
        if name in parts:
            return index
    return None


def _imported_modules(text: str) -> list[str]:
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


def _scan_code_hospital(root: Path, files: list[str], layers: list[str]) -> list[dict]:
    raws = []
    for rel in files:
        if Path(rel).suffix.lower() != ".py":
            continue
        source_index = _layer_index(rel, layers)
        if source_index is None:
            continue
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for module in _imported_modules(text):
            target = _layer_index(module.replace(".", "/"), layers)
            if target is None or target >= source_index:
                continue
            raws.append({
                "rule_id": _LAYER_RULE,
                "message": f"{layers[source_index]} 不允许依赖更上层的 {layers[target]}（{module}）",
                "file_path": rel.replace("\\", "/"),
                "line_start": 1,
                "line_end": 1,
                "severity": "major",
                "symbol": "",
                "contract_id": "code-hospital",
            })
            break
    return raws


def _archunit_reports(root: Path) -> list[Path]:
    found = []
    for folder in (root / "target" / "surefire-reports", root / "build" / "test-results" / "test"):
        if folder.is_dir():
            found.extend(folder.glob("*"))
    return found


def _scan_archunit(root: Path) -> tuple[list[dict], str]:
    reports = _archunit_reports(root)
    if not reports:
        return [], "ArchUnit 测试结果不在仓库里，分层尺未拍"
    raws = []
    for path in reports:
        if path.suffix.lower() not in {".txt", ".xml"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "Architecture Violation" not in text and "ArchitectureViolation" not in text:
            continue
        raws.append({
            "rule_id": _LAYER_RULE,
            "message": "ArchUnit 报告了分层违例",
            "file_path": path.relative_to(root).as_posix(),
            "line_start": 1,
            "line_end": 1,
            "severity": "major",
            "symbol": "",
            "contract_id": "archunit",
        })
    return raws, ""


def _scan_import_linter_text(text: str) -> list[dict]:
    raws = []
    for line in text.splitlines():
        matched = re.search(r"(\S+)\s+->\s+(\S+)\s+\(l\.(\d+)\)", line)
        if not matched:
            continue
        raws.append({
            "rule_id": _LAYER_RULE,
            "message": f"{matched.group(1)} 不允许依赖 {matched.group(2)}",
            "file_path": matched.group(1).replace(".", "/") + ".py",
            "line_start": int(matched.group(3)),
            "line_end": int(matched.group(3)),
            "severity": "major",
            "symbol": "",
            "contract_id": "import-linter",
        })
    return raws


async def scan_layers(project_root: str, files: list[str], timeout_ms: int = 120_000) -> tuple[list[dict], list[str]]:
    """返回 (raw dicts, limitations)。没有契约时只有终态限制。"""
    from ..adapters.base import AdapterError, _run

    contract = resolve_contract(project_root)
    if contract is None:
        return [], ["未配置分层，分层尺未拍"]
    root = Path(project_root)
    kind = contract["kind"]
    if kind == "code-hospital":
        return _scan_code_hospital(root, files, contract["layers"]), []
    if kind == "archunit":
        raws, limitation = _scan_archunit(root)
        return raws, [limitation] if limitation else []
    if kind == "import-linter":
        if shutil.which("lint-imports") is None:
            return [], ["lint-imports 未到岗，分层尺未拍"]
        try:
            out, err = await _run(
                ["lint-imports"], cwd=project_root, timeout=max(timeout_ms / 1000, 1),
            )
        except (AdapterError, OSError):
            return [], ["lint-imports 本轮失败，可补拍一次"]
        return _scan_import_linter_text(out or err), []
    if shutil.which("depcruise") is None:
        return [], ["dependency-cruiser 未到岗，分层尺未拍"]
    try:
        out, _err = await _run(
            ["depcruise", "--output-type", "json", "."],
            cwd=project_root,
            timeout=max(timeout_ms / 1000, 1),
        )
    except (AdapterError, OSError):
        return [], ["dependency-cruiser 本轮失败，可补拍一次"]
    return _scan_cruiser_json(out), []


def _scan_cruiser_json(payload: str) -> list[dict]:
    import json
    try:
        doc = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return []
    raws = []
    for row in doc.get("violations") or []:
        rule = row.get("rule") or {}
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(rule.get("name") or "forbidden-import")).strip("-")
        source = str(row.get("from") or "")
        raws.append({
            "rule_id": f"layer.{name or 'forbidden-import'}",
            "message": str(row.get("comment") or rule.get("name") or "依赖契约违例"),
            "file_path": source.replace("\\", "/"),
            "line_start": 1,
            "line_end": 1,
            "severity": "major",
            "symbol": "",
            "contract_id": "dependency-cruiser",
        })
    return raws
