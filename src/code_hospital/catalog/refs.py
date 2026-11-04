"""文件引用链：这个文件，有没有别的文件指向它。

调用图只回答代码里的「谁调用谁」。文档、配置、SQL 映射没有调用，
但它们有引用：README 链过去、构建脚本点名、mapper 里写死文件名。
这条链以前根本没查，非 Python 文件一进范围就直接写「这条链未建」——
那是把「没查」写成「没有」，和「我治不了」写成「你没病」是同一类错。

只查两件事，都是数得出来的：
- 按约定生效：`.github/`、classpath 的 `resources/`、根目录的契约文件——
  这类文件不需要谁来提到，工具按目录或名字自己找；
- 被别的文件提到：文件名出现在其它文件的正文里，且不是它自己。
两件都不是，才记一条「没有任何引用」。

同名文件的局限：引用按文件名匹配，两个同名文件只要被提到一次就都算有引用。
宁可漏报孤儿，不可把被引用的记成孤儿——后者会让报告说错话。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..safety.repo_files import KIND_CODE, classify, is_denied

_MAX_CANDIDATES = 200
_MAX_READ_FILES = 4000
_MAX_READ_BYTES = 400 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_EXAMPLES = 5

_BINARY = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".ico", ".bmp", ".webp", ".pdf",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
    ".class", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp4", ".mov", ".avi",
    ".mp3", ".wav", ".parquet", ".avro", ".orc", ".lock",
})

# 这些目录里的文件由工具按约定自己找，不需要谁在正文里提到它。
# 这是事实，不是策略：GitHub 按目录读 workflow、JVM 按类路径读 resources。
_CONVENTION_DIRS = (
    ".github/", ".gitlab/", ".circleci/", "gradle/wrapper/", "META-INF/",
)

# 仓库根目录下的契约文件：按名字就是入口。
_CONVENTION_NAMES = frozenset({
    "readme", "readme.md", "readme.rst", "readme.txt", "readme.adoc",
    "license", "license.txt", "license.md", "notice", "notice.txt",
    "changelog", "changelog.md", "changelog.rst", "contributing.md",
    "code_of_conduct.md", "security.md", "authors", "makefile", "dockerfile",
    "docker-compose.yml", "docker-compose.yaml", ".gitignore", ".gitattributes",
    ".editorconfig", ".dockerignore", ".asf.yaml", ".npmrc", ".nvmrc",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "gradle.properties", "package.json", "tsconfig.json",
    "pyproject.toml", "setup.py", "setup.cfg", "go.mod", "cargo.toml",
    "composer.json", "requirements.txt", "tox.ini", "pytest.ini",
})

_TOKEN = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,12}")

# 子包里的 readme 是包元数据（npm / pub / pypi 按名字渲染），不需要谁来链接。
_CONVENTION_ANYWHERE = frozenset({
    "readme", "readme.md", "readme.rst", "readme.txt", "readme.adoc",
})

# 有这类文件的目录就是站点根：它下面的文档由生成器按目录结构渲染，
# 不需要谁在正文里逐个提到。Jekyll、MkDocs、Sphinx、mdBook、Docusaurus 都算。
_SITE_ROOTS = (
    "_config.yml", "_config.yaml", "mkdocs.yml", "hugo.toml", "book.toml",
    "docusaurus.config.js", "docusaurus.config.ts", "conf.py", "_toc.yml",
)


def _under_site_root(root: Path, rel: str) -> str:
    parts = rel.replace("\\", "/").split("/")[:-1]
    here = root
    for depth, part in enumerate(parts):
        here = here / part
        for marker in _SITE_ROOTS:
            try:
                if (here / marker).is_file():
                    where = "/".join(parts[: depth + 1]) or "."
                    return f"{where} 是站点根，内容按生成器约定生效"
            except OSError:
                continue
    return ""


def _by_convention(root: Path, rel: str) -> str:
    posix = rel.replace("\\", "/")
    low = posix.lower()
    name = posix.rsplit("/", 1)[-1]
    if name.startswith("."):
        # .eslintrc.json / .stylelintrc / .npmrc 这类是按文件名被工具读的，
        # 没有谁「提到」它们，也不该因此算孤儿。
        return "工具按文件名自动读取"
    if name.lower() in _CONVENTION_ANYWHERE:
        return "子包元数据，按名字渲染"
    if "/resources/" in f"/{low}/":
        return "类路径资源按名字加载"
    for folder in _CONVENTION_DIRS:
        if f"/{folder}" in f"/{posix}/" or low.startswith(folder):
            return f"{folder.rstrip('/')} 按目录约定生效"
    if "/" not in posix and low in _CONVENTION_NAMES:
        return "仓库根目录的契约文件"
    return _under_site_root(root, posix)


def _candidates(project_root: str, files: list[str]) -> list[str]:
    """非代码文件才走引用链；代码之间的链由调用图负责。"""
    out: list[str] = []
    for rel in files:
        key = rel.replace("\\", "/")
        if Path(key).suffix.lower() in _BINARY:
            continue
        if is_denied(key):
            continue
        kind, _why = classify(project_root, key)
        if kind == KIND_CODE:
            continue
        out.append(key)
    return out


def _scan_refs(root: Path, files: list[str], names: set[str]) -> dict[str, str]:
    """一次扫描把提到过的文件名记下来。返回 文件名 → 提到它的那个文件。"""
    found: dict[str, str] = {}
    budget_files = _MAX_READ_FILES
    budget_bytes = _MAX_TOTAL_BYTES
    for rel in files:
        if not names or budget_files <= 0 or budget_bytes <= 0:
            break
        key = rel.replace("\\", "/")
        if Path(key).suffix.lower() in _BINARY or is_denied(key):
            continue
        path = root / key
        try:
            if path.stat().st_size > _MAX_READ_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        budget_files -= 1
        budget_bytes -= len(text)
        here = key.rsplit("/", 1)[-1]
        for token in _TOKEN.findall(text):
            name = token.replace("\\", "/").rsplit("/", 1)[-1]
            if name in names and name != here and name not in found:
                found[name] = key
    return found


def check(project_root: str, files: list[str]) -> dict:
    """查一遍范围内的非代码文件有没有人引用。没有项目根就不查，记成没查。"""
    if not project_root or not files:
        return {}
    root = Path(project_root)
    candidates = _candidates(project_root, files)
    if not candidates:
        return {"candidates": 0, "by_convention": 0, "referenced": 0,
                "orphans": [], "orphan_count": 0, "truncated": False}

    truncated = len(candidates) > _MAX_CANDIDATES
    candidates = candidates[:_MAX_CANDIDATES]

    by_convention = 0
    unknown: list[str] = []
    for rel in candidates:
        if _by_convention(root, rel):
            by_convention += 1
        else:
            unknown.append(rel)

    referenced = _scan_refs(root, files, {Path(rel).name for rel in unknown})
    orphans = [rel for rel in unknown if Path(rel).name not in referenced]
    return {
        "candidates": len(candidates),
        "by_convention": by_convention,
        "referenced": len(unknown) - len(orphans),
        "orphans": orphans[:_MAX_EXAMPLES],
        "orphan_count": len(orphans),
        "truncated": truncated,
    }


def note(refs: dict | None) -> str:
    """一句话。没有孤儿时不写——和缺口账一样，没有就不占版面。"""
    if not refs:
        return ""
    total = int(refs.get("candidates") or 0)
    count = int(refs.get("orphan_count") or 0)
    if not total or not count:
        return ""
    examples = "、".join(refs.get("orphans") or [])
    tail = f"，如 {examples}" if examples else ""
    extra = f"（只查了前 {total} 个非代码文件）" if refs.get("truncated") else ""
    return (
        f"文件引用：{count} 个非代码文件在范围内没有任何其他文件提到过{tail}{extra}。"
        "没人引用不等于文件有问题，但这条链到此为止，不写成已经核过。"
    )


def lines(refs: dict | None) -> list[str]:
    text = note(refs)
    return [text] if text else []


__all__ = ["check", "lines", "note"]
