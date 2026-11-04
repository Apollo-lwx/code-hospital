"""仓库文件与 git 操作（只读）+ .git/info/exclude 隔离（设计 §5.2）。

scope 文件清单解析：repo / paths / staged / commit / range。
忽略三层（都不能全仓都扫）：
1. `.gitignore`：repo scope 枚举走 `git ls-files --exclude-standard`，git 原生尊重；
2. 内置排除目录 `_DENY_DIRS`：与 rsi-boot EXCLUDED_DIRS 同思路，全 scope 兜底；
3. 项目级 `code-hospital.yaml` 的 `scan_ignore` globs：用户自定义跳过（全 scope 生效）。
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
from pathlib import Path

import yaml

from ..core.errors import bad_argument

# 内置排除目录（任意路径段命中即跳过；与 rsi-boot signal_discovery.EXCLUDED_DIRS 同思路）
# `.rsi` 必须排除——它是医院自己的记忆库（yaml 知识条目 + identity + 日志）。
# 既往病史走 `storage/rsi_memory.py` 的 advisory 通道读取，那是**读**；
# 扫描范围是「哪些文件要检查」，把记忆库也算进去等于让医院检查自己的笔记本，
# 会污染覆盖账和调用链（yaml 找不到调用者，报一堆「这条链未建」）。
# `.hospital` 必须排除——医院绝不把自家病历当扫描输入（篡改病历属禁止级）。
_DENY_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".idea",
    ".vscode", ".hospital", ".rsi",
}

KIND_CODE = "code"
KIND_CONFIG = "config"
KIND_DOC = "doc"
# 判不出结构、又没有机械检查器的 XML（logback / web.xml / 安装器描述）：
# 它写的是运行时行为和装配关系，只能靠读。这类不报「缺仪器」，
# 报「没人读」——否则招聘信号会变成「招一台 XML 检查器」。
KIND_MARKUP = "markup"

# 扩展名分开列，因为它们回答的是不同的问题：
#   code   —— 仪器该不该扫（有规则、有行号、能开出方）
#   config —— 结构、供应链、清单要解析它，但规则仪器不该逐行审它
#   doc    —— 不交给规则，但必须进「模型该读的范围」：AGENTS.md / CONTRIBUTING.md
#             写的就是这个项目的规范，漏掉它们等于让医院自己不看院规
_CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".rb",
    ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".swift", ".m",
    ".scala", ".sh", ".bash", ".sql",
}
_CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".xml"}
_DOC_EXT = {".md", ".markdown", ".rst", ".adoc", ".txt"}

# .xml 不能按文件名判：叫 UserMapper.xml 的可能只是日志配置，
# 而 resources/mybatis/query.xml 里就装着 SQL。先读头部按内容判结构。
# SQL 的判据必须收紧到 MyBatis/iBatis 的 DTD 与 `mapper + namespace`——
# 用 `<sql` 这种宽标记，`<sql-maven-plugin>` 会被误判成 SQL 映射。
_XML_SNIFF_BYTES = 8192
_XML_SQL_DTD = ("mybatis-3-mapper", "sql-map-2.dtd", "ibatis.apache.org")
_XML_BUILD_MARKS = ("<project", "<modelversion", "<ivy-module", "maven.apache.org")
_XML_BUILD_NAMES = {"pom.xml", "build.xml", "ivy.xml", "settings.xml", "maven-metadata.xml"}
# 内容读不到时（无 project_root 或文件不可读）才退回路径判断，且只作兜底。
_XML_SQL_PATHS = ("mapper", "sqlmap", "mybatis")

_SOURCE_EXT = _CODE_EXT | _CONFIG_EXT | _DOC_EXT

PROJECT_CONFIG = "code-hospital.yaml"


async def run_git(repo_root: str, args: list[str], timeout: float = 30.0) -> str:
    """执行只读 git 命令，返回 stdout。失败抛 BadArgument 并附 stderr 摘要。"""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=repo_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise bad_argument(f"git {' '.join(args)} 超时", next_action="缩小 scope 或增大 timeout")
    if proc.returncode != 0:
        raise bad_argument(
            f"git {' '.join(args)} 失败：{err.decode(errors='replace')[:200]}",
            next_action="确认仓库状态与参数（如 range/commit 是否存在）",
        )
    return out.decode(errors="replace")


async def ensure_git_repo(project_root: str) -> None:
    if not (Path(project_root) / ".git").exists():
        raise bad_argument(
            f"{project_root} 不是 git 仓库",
            target="project_root",
            next_action="在 git 仓库根目录使用，或先 git init",
        )


def load_scan_ignores(project_root: str) -> list[str]:
    """读取项目级 `code-hospital.yaml` 的 `scan_ignore` globs（fnmatch，作用于仓库相对路径）。

    配置缺失/解析失败均返回 []（容忍，不阻塞扫描）。
    """
    cfg = Path(project_root) / PROJECT_CONFIG
    if not cfg.is_file():
        return []
    try:
        doc = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        patterns = doc.get("scan_ignore") or []
        return [str(p) for p in patterns if str(p).strip()]
    except Exception:
        return []


def _strip_dot_slash(path: str) -> str:
    """只去掉一个前导 './'——lstrip('./') 会把 '.hospital' 剥成 'hospital'（真 bug）。"""
    return path[2:] if path.startswith("./") else path


def is_denied(rel_path: str, custom_globs: list[str] | None = None) -> bool:
    """内置排除目录（任意路径段命中）+ 用户自定义 globs。"""
    posix = _strip_dot_slash(rel_path.replace("\\", "/"))
    parts = set(posix.split("/")[:-1])
    if parts & _DENY_DIRS:
        return True
    for pattern in custom_globs or []:
        if fnmatch.fnmatch(posix, pattern):
            return True
    return False


def _is_source(rel_path: str) -> bool:
    return Path(rel_path).suffix.lower() in _SOURCE_EXT


def _read_head(project_root: str, rel_path: str) -> str:
    """读文件头用于归类。只读前 8KB，二进制直接当读不到。"""
    if not project_root:
        return ""
    try:
        with (Path(project_root) / rel_path).open("rb") as fh:
            raw = fh.read(_XML_SNIFF_BYTES)
    except OSError:
        return ""
    if b"\x00" in raw:
        return ""
    return raw.decode("utf-8", errors="replace").lower()


def _xml_is_sql(head: str) -> bool:
    if any(mark in head for mark in _XML_SQL_DTD):
        return True
    return "<mapper" in head and "namespace" in head


def classify(project_root: str, rel_path: str) -> tuple[str, str]:
    """返回 (类别, 依据)。依据要能复盘：「凭什么是代码」不能只写一句我判的。

    依据取值：`ext:*` 按扩展名、`content:sql|build|unclear` 按内容、
    `name:*` / `path:*` 按名字兜底（内容读不到时才用）。
    """
    posix = _strip_dot_slash(rel_path.replace("\\", "/")).lower()
    suffix = Path(posix).suffix.lower()
    name = Path(posix).name
    if suffix in _DOC_EXT:
        return KIND_DOC, "ext:doc"
    if suffix == ".xml":
        head = _read_head(project_root, rel_path)
        if _xml_is_sql(head):
            return KIND_CODE, "content:sql"
        if any(mark in head for mark in _XML_BUILD_MARKS):
            return KIND_CONFIG, "content:build"
        if name in _XML_BUILD_NAMES:
            return KIND_CONFIG, "name:build"
        if any(mark in posix for mark in _XML_SQL_PATHS):
            return KIND_CODE, "path:sql"
        return KIND_MARKUP, ("content:unclear" if head else "name:unclear")
    if suffix in _CONFIG_EXT:
        return KIND_CONFIG, "ext:config"
    return KIND_CODE, "ext:code"


def kind_of(rel_path: str, project_root: str = "") -> str:
    """文件归到 code / config / markup / doc。给 project_root 时 .xml 按内容判。"""
    return classify(project_root, rel_path)[0]


async def _list_repo_files(project_root: str) -> tuple[list[str], int]:
    """repo scope 枚举：git ls-files（原生尊重 .gitignore），失败回退 os.walk。"""
    try:
        out = await run_git(
            project_root, ["ls-files", "--cached", "--others", "--exclude-standard"]
        )
        files = [ln.strip() for ln in out.splitlines() if ln.strip() and _is_source(ln.strip())]
        return sorted(set(files)), 0
    except Exception:
        root = Path(project_root).resolve()
        return _walk_sources(root, root), 0


async def scope_file_list(
    project_root: str, scope_type: str, scope: dict
) -> tuple[list[str], int]:
    """把 scope 解析为仓库相对 POSIX 路径清单。

    返回 (文件清单, 被忽略规则跳过的数量)；三层忽略全 scope 生效。
    """
    custom = load_scan_ignores(project_root)

    def apply(files: list[str]) -> tuple[list[str], int]:
        kept = [f for f in files if not is_denied(f, custom)]
        return kept, len(files) - len(kept)

    if scope_type == "staged":
        out = await run_git(project_root, ["diff", "--name-only", "--cached", "--diff-filter=ACMR"])
        return apply([ln.strip() for ln in out.splitlines() if ln.strip()])
    if scope_type == "commit":
        commit = scope.get("commit") or "HEAD"
        out = await run_git(
            project_root,
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "--diff-filter=ACMR", commit],
        )
        return apply([ln.strip() for ln in out.splitlines() if ln.strip()])
    if scope_type == "range":
        rng = scope.get("range") or ""
        if not rng:
            raise bad_argument("scope_type=range 需要提供 scope.range", target="scope.range")
        out = await run_git(project_root, ["diff", "--name-only", "--diff-filter=ACMR", rng])
        return apply([ln.strip() for ln in out.splitlines() if ln.strip()])
    if scope_type == "paths":
        paths = scope.get("paths") or []
        if not paths:
            raise bad_argument("scope_type=paths 需要提供非空 scope.paths", target="scope.paths")
        root = Path(project_root).resolve()
        result = []
        for p in paths:
            abs_p = (root / p).resolve()
            if root not in abs_p.parents and abs_p != root:
                raise bad_argument(f"路径越出仓库：{p}", target="scope.paths")
            if abs_p.is_file():
                result.append(abs_p.relative_to(root).as_posix())
            elif abs_p.is_dir():
                result.extend(_walk_sources(root, abs_p))
            else:
                raise bad_argument(f"路径不存在：{p}", target="scope.paths")
        return apply(sorted(set(result)))
    if scope_type == "repo":
        files, _ = await _list_repo_files(project_root)
        return apply(files)
    raise bad_argument(f"未知 scope_type：{scope_type}", target="scope_type")


def _walk_sources(root: Path, base: Path) -> list[str]:
    result = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _DENY_DIRS]
        for fn in filenames:
            if Path(fn).suffix.lower() in _SOURCE_EXT:
                result.append((Path(dirpath) / fn).relative_to(root).as_posix())
    return sorted(result)


def has_python_files(project_root: str) -> bool:
    """仓库里有没有 Python 文件（尊重三层忽略）。

    零配置探测用：本机装了 ruff 不等于这台仓库该建议 `ruff check`。
    纯 Java 仓库上它会扫 0 个文件、退出码 0，然后被写进验收白名单——
    一条永远绿的验收命令，疗效账跟着一起失真。
    """
    root = Path(project_root or ".").resolve()
    custom = load_scan_ignores(project_root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _DENY_DIRS]
        for fn in filenames:
            if Path(fn).suffix.lower() not in {".py", ".pyi"}:
                continue
            rel = (Path(dirpath) / fn).relative_to(root).as_posix()
            if not is_denied(rel, custom):
                return True
    return False


def ensure_git_info_exclude(project_root: str, entry: str = ".hospital/") -> bool:
    """把 .hospital/ 追加进 .git/info/exclude（本地排除，不动 tracked .gitignore）。

    返回是否发生了写入。幂等。
    """
    info = Path(project_root) / ".git" / "info"
    if not info.exists():
        return False
    exclude = info / "exclude"
    content = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if any(ln.strip() == entry for ln in content.splitlines()):
        return False
    with exclude.open("a", encoding="utf-8") as f:
        if content and not content.endswith("\n"):
            f.write("\n")
        f.write("# code-hospital 病历目录（本地排除，勿提交）\n" + entry + "\n")
    return True
