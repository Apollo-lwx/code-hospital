"""SpotBugs / FindBugs 仪器。

分析的是已编译的 class，不是源码。没有命令或没有 class 目录时，这台仪器未拍成。
新增同类外部仪器时不要改扫描流程：实现 Engine，登记进 catalog.engines.ENGINES，
再在 instruments.yaml 和号别里点名。
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

_CLASS_DIRS = (
    "target/classes",
    "build/classes/java/main",
    "build/classes/main",
    "build/classes",
)
_PRIORITY = {"1": "major", "2": "major", "3": "minor"}


def parse_spotbugs_xml(payload: str, files: list[str] | None = None) -> list[dict]:
    """BugCollection XML。files 给出时，只留源码路径对得上的条目。"""
    try:
        root = ET.fromstring(payload or "")
    except ET.ParseError:
        return []
    wanted = {item.replace("\\", "/") for item in files or []}
    raws: list[dict] = []
    for bug in root.iter("BugInstance"):
        bug_type = bug.attrib.get("type") or "unknown"
        message = ""
        for tag in ("LongMessage", "ShortMessage"):
            node = bug.find(tag)
            if node is not None and (node.text or "").strip():
                message = node.text.strip()
                break
        line_node = bug.find("SourceLine")
        if line_node is None:
            line_node = bug.find(".//SourceLine")
        source = ""
        line = 1
        if line_node is not None:
            source = (line_node.attrib.get("sourcepath") or line_node.attrib.get("sourcefile") or "").replace("\\", "/")
            start = line_node.attrib.get("start") or line_node.attrib.get("startLine") or "1"
            line = int(start) if str(start).isdigit() else 1
        file_path = _match_source(source, wanted)
        if wanted and file_path is None:
            continue
        raws.append({
            "tool": "spotbugs",
            "rule_id": bug_type,
            "message": message or bug_type,
            "file_path": file_path or source or "spotbugs",
            "line_start": line,
            "line_end": line,
            "severity": _PRIORITY.get(bug.attrib.get("priority") or "3", "minor"),
            "symbol": "",
        })
    return raws


class SpotBugsEngine:
    """spotbugs 优先，找不到再试 findbugs。"""

    instrument_id = "spotbugs"

    def run(self, project_root: str, files: list[str], timeout_s: float) -> tuple[list[dict], list[str], str]:
        java = [item for item in files if item.endswith(".java")]
        if not java:
            return [], ["这次范围没有 Java 源文件，spotbugs 未拍。"], "terminal"
        binary = shutil.which("spotbugs") or shutil.which("findbugs")
        if not binary:
            return [], ["spotbugs 未到岗，findbugs 也未到岗。"], "terminal"
        classes = _class_dirs(project_root)
        if not classes:
            return [], ["spotbugs 需要已编译的 class 或 jar，没有找到 target/classes 或 build/classes。"], "terminal"
        cmd = _command(binary, classes)
        try:
            proc = subprocess.run(
                cmd, cwd=project_root, capture_output=True, text=True,
                timeout=max(5.0, min(timeout_s, 90.0)), encoding="utf-8", errors="replace", check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return [], ["spotbugs 未完成，这台仪器这次没拍成。"], "terminal"
        payload = proc.stdout or ""
        if "<BugCollection" not in payload and "<BugInstance" not in payload:
            if proc.returncode not in {0, 1}:
                return [], ["spotbugs 未完成，这台仪器这次没拍成。"], "terminal"
            return [], [], "ok"
        return parse_spotbugs_xml(payload, java), [], "ok"


def _class_dirs(project_root: str) -> list[Path]:
    root = Path(project_root)
    found = []
    for rel in _CLASS_DIRS:
        path = root / rel
        if path.is_dir() and any(path.rglob("*.class")):
            found.append(path)
    return found


def _command(binary: str, classes: list[Path]) -> list[str]:
    name = Path(binary).name.lower()
    if "spotbugs" in name:
        cmd = [binary, "-textui", "-xml:withMessages", "-effort:less", "-low", "-nested:false", "-quiet"]
    else:
        cmd = [binary, "-textui", "-xml", "-effort:min", "-low", "-nested:false"]
    cmd.extend(str(path) for path in classes)
    return cmd


def _match_source(source: str, wanted: set[str]) -> str | None:
    if not source:
        return None if wanted else ""
    if not wanted:
        return source
    for item in wanted:
        if item == source or item.endswith("/" + source) or item.endswith(source):
            return item
    return None
