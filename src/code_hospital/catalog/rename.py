"""正骨（rename）的守门与规模控制（docs/code-hospital-rename-formula-draft.md 已定稿）。

一期范围是**单文件闭包**：改名符号 + 同一文件内的全部引用一起改。
跨文件引用不追——符号被其他文件 import 时该发现不入方，标待复核等 LSP001。
同规则同文件 ≥ RENAME_BATCH_MIN 处是批量迁移，不是诊疗：合成一味外购（manual）。
两类天然局部名字不做守门：G1 函数内名字；G5 import 别名（本文件绑定，
`__init__.py` 重导出例外——入待复核）。

守门是保守的文本扫描：宁可是误拦（留待复核），不能是漏拦（自动改断别的文件）。
三期算法升级（2026-11-23）：
1. 匹配前用 tokenize 剥掉注释与字符串字面量——docstring 里提一句符号名不算引用，
   这是文本守门误拦的主力来源；剥不动（语法已坏，医院常客）回退原文，方向仍保守。
2. 扫描命中 _SCAN_CAP 截断视为「没查完 = 不能证明单文件闭包」——截断点之后
   可能有引用，返回真值堵漏拦（截断是沉默的，不拦就是漏）。
"""

from __future__ import annotations

import ast
import fnmatch
import io
import re
import tokenize
from pathlib import Path

# G1 局部名字（函数内变量/参数）：天然单函数闭包，不可能有跨文件引用，不需要守门。
G1_LOCAL_RULES = frozenset({"N803", "N806"})

# G5 import 别名（N811–N814、N817）：别名是导入语句在**本文件**里起的局部绑定，
# 其他文件引用的是原模块/原名，不是这个别名——天然单文件闭包，同样不做跨文件守门。
# 唯一例外：`__init__.py` 里的别名即包的重导出名，改它会断包外 `from pkg import 别名`，
# 文本守门推不出包名，直接入待复核（等 LSP001）。
G5_ALIAS_RULES = frozenset({"N811", "N812", "N813", "N814", "N817"})

# 同规则同文件的簇数达到这个阈值，合并成一味「批量迁移」外购药。
RENAME_BATCH_MIN = 20

_SKIP_DIRS = {".git", ".hospital", ".venv", "venv", "node_modules", "target", "build", "__pycache__"}
_SCAN_CAP = 3000


def _module_tail(file_path: str) -> str:
    """模块路径的最后一段。src 布局下完整 dotted 名推不准，用尾段做文本匹配。"""
    name = Path(file_path).stem
    return "" if name == "__init__" else name


def _repo_python_files(root: Path, exclude: str) -> tuple[list[Path], bool]:
    """仓内全部 .py（排除自身与常见杂物目录）。返回 (文件清单, 是否被 _SCAN_CAP 截断)。"""
    found: list[str] = []
    excluded = str(Path(root) / exclude).replace("\\", "/")
    truncated = False
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if str(path).replace("\\", "/") == excluded:
            continue
        found.append(str(path))
        if len(found) >= _SCAN_CAP:
            truncated = True
            break
    return found, truncated


def _code_only(text: str) -> str:
    """剥掉注释与字符串字面量，只留代码骨架。剥不动（语法已坏）回退原文。

    注释 / docstring / 普通字符串里提到符号名都不算引用，剥掉再匹配是
    消灭文本守门误拦的主力手段。f-string 的表达式部分是真实代码
    （3.12+ 单独成 token），保留——f"{mod.symbol}" 是真引用。
    """
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    chars = list(text)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            begin = starts[tok.start[0] - 1] + tok.start[1]
            end = starts[tok.end[0] - 1] + tok.end[1]
            for idx in range(begin, min(end, len(chars))):
                if chars[idx] not in ("\n", "\r"):
                    chars[idx] = " "
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return text
    return "".join(chars)


def cross_file_reference(project_root: str, file_path: str, symbol: str) -> bool:
    """不能证明单文件闭包时为真：确认被本仓其他 .py 引用，或扫描没查完。

    文本扫描，误拦方向保守：注释/字符串先剥掉再匹配（三期），扫描被
    _SCAN_CAP 截断按有引用处理——截断点之后查不到了，放行就是漏拦。
    """
    symbol = str(symbol or "").strip()
    tail = _module_tail(file_path)
    if not symbol or not tail:
        return False
    root = Path(project_root)
    escaped_sym = re.escape(symbol)
    escaped_tail = re.escape(tail)
    from_import = re.compile(
        rf"\bfrom\s+[\w.]*{escaped_tail}\s+import\s+[^#\n]*(\b{escaped_sym}\b|\*)"
    )
    attr_use = re.compile(rf"\b{escaped_tail}\s*\.\s*{escaped_sym}\b")
    others, truncated = _repo_python_files(root, file_path)
    for other in others:
        try:
            text = Path(other).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code = _code_only(text)
        if from_import.search(code) or attr_use.search(code):
            return True
    return truncated


def exempted(symbol: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """符号命中仓库声明的命名约定（catalog.rename.exempt）时为真。

    机制内置、数据外置：豁免清单由被诊疗仓库的 code-hospital.yaml 声明，
    内置默认为空。命中不入方——这名不归医院管，不是「没问题」。
    """
    symbol = str(symbol or "").strip()
    if not symbol:
        return False
    return any(fnmatch.fnmatchcase(symbol, pattern) for pattern in patterns)


def touches_public_api(project_root: str, file_path: str, symbol: str) -> bool:
    """touches_public_api 的求值：__all__ 收录，或包顶层 __init__.py 里定义/再导出。"""
    symbol = str(symbol or "").strip()
    if not symbol:
        return False
    path = Path(project_root) / file_path
    if not path.is_file():
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return False
    exported = _dunder_all(tree)
    if symbol in exported:
        return True
    if path.name != "__init__.py":
        return False
    return _top_level_defines(tree, symbol)


def _dunder_all(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        value = None
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__":
            value = node.value
        if isinstance(value, (ast.List, ast.Tuple)):
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    names.add(elt.value)
    return names


def _top_level_defines(tree: ast.Module, symbol: str) -> bool:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == symbol for t in node.targets
        ):
            return True
        if isinstance(node, ast.ImportFrom) and any(
            (alias.asname or alias.name) == symbol for alias in node.names
        ):
            return True
    return False
