"""git worktree 高危隔离（worktree 草案一期）：隔离间的生命周期机械层。

只做四件事：判定要不要隔离（wants_isolation）、建间（prepare）、拆间（remove）、
清扫（cleanup_all）。合回编排（drift 检查、复制、主仓复验）在药房——那里才有
剂量、护理、挂起语义。

三条铁律：
- 医院对主仓的 git 操作只有 worktree add / remove / prune，不碰历史与分支；
- 隔离间只是文件操作目标，病历/快照/剂量永远写主仓 .hospital（双 root）；
- 建间失败宁可这味药不治，也不悄悄降级直写主仓——降级是用户的决定
  （safety.worktree=never 或 hosp_dispense(isolation=false)）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ..catalog.policy import load_policy
from ..core.errors import invalid_operation
from ..storage.db import hospital_root

_BASE = "HEAD"


def wants_isolation(root: str, item: dict, override: bool | None) -> bool:
    """入参覆盖 > 仓级配置（never/high/always，默认 high：risk=high 的药味进隔离间）。"""
    if override is not None:
        return bool(override)
    mode = load_policy(root).worktree
    if mode == "always":
        return True
    if mode == "never":
        return False
    return item.get("risk") == "high"


def _git(root: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", root, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:240]
        raise invalid_operation(
            f"建/拆隔离间失败（git {' '.join(args[:2])}）：{detail}",
            target="isolation",
            next_action="要直写主仓：safety.worktree=never 或 hosp_dispense(isolation=false)",
        )
    return proc.stdout


def worktree_dir(root: str, item_id: str) -> Path:
    return hospital_root(root) / "worktrees" / item_id


def prepare(root: str, item_id: str, scope_paths: list[str]) -> str:
    """建隔离间：detached 检出 HEAD + 本味 scope 文件与主仓工作区对齐 + 接入依赖目录。

    对齐这步不能省：检出的是 HEAD，用户未提交的改动不在里面，不同步的话
    锚点对的是主仓内容、落盘对的却是检出内容，patch 必挂。同步后隔离间状态
    = HEAD + 主仓当前工作区（就本味文件而言），合回时的 drift 检查依然兜底。
    """
    wt = worktree_dir(root, item_id)
    if wt.exists():
        _remove_path(root, wt)
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "--detach", str(wt), _BASE)
    base = Path(root)
    for rel in scope_paths:
        src = base / rel
        if src.is_file():
            dst = wt / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    _link_dep_dirs(root, wt)
    return str(wt)


def _link_dep_dirs(root: str, wt: Path) -> None:
    """把主仓的依赖目录接进隔离间（junction/symlink），验收命令才有环境可跑。

    链接失败不拦：验收命令自己会报「跑不起来」，比在这里猜原因诚实。
    """
    for rel in load_policy(root).worktree_link_dirs:
        src = Path(root) / rel
        dst = wt / rel
        if not src.is_dir() or dst.exists():
            continue
        try:
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                    capture_output=True,
                )
            else:
                os.symlink(src, dst, target_is_directory=True)
        except OSError:
            continue


def _remove_path(root: str, wt: Path) -> None:
    try:
        _git(root, "worktree", "remove", "--force", str(wt))
    except Exception:  # noqa: BLE001 - git 拆不掉就物理拆，prune 收尾
        shutil.rmtree(wt, ignore_errors=True)
    _prune(root)


def _prune(root: str) -> None:
    try:
        _git(root, "worktree", "prune")
    except Exception:  # noqa: BLE001 - 清扫是尽力而为，不拦诊疗
        pass


def remove(root: str, item_id: str) -> None:
    """拆一味药的隔离间。不在就不动作（成功/失败路径都会调到，幂等）。"""
    wt = worktree_dir(root, item_id)
    if wt.exists():
        _remove_path(root, wt)


def cleanup_all(root: str) -> list[str]:
    """出院/转院清扫：拆掉本仓全部隔离间，返回拆掉的 item_id 清单（台账用）。"""
    parent = hospital_root(root) / "worktrees"
    removed: list[str] = []
    if parent.is_dir():
        for child in sorted(parent.iterdir()):
            if child.is_dir():
                _remove_path(root, child)
                removed.append(child.name)
    _prune(root)
    return removed
