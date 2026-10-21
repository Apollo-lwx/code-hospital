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
_DENY_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".idea",
    ".vscode", ".hospital", ".rsi",
}

_SOURCE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".rb",
    ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".swift", ".m",
    ".scala", ".sh", ".bash", ".sql", ".yaml", ".yml", ".json", ".toml",
}

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
