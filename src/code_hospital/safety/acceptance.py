"""验收命令白名单（设计 §4.6）。

只执行 `code-hospital.yaml` 里 `acceptance.commands` 已确认的别名。
探测结果要等用户 confirm 才写入。命令不经 shell，秘密环境变量不透传。
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

import yaml

from ..core.errors import invalid_operation

_CONFIG = "code-hospital.yaml"
_KEEP_ENV = {
    "PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "LANG", "LC_ALL", "SYSTEMDRIVE",
}
_SECRET_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
_META = set("|;&<>`$\\\n\r")
_MAX_OUTPUT = 64 * 1024
_DEFAULT_TIMEOUT = 120.0


def _path(root: str) -> Path:
    return Path(root) / _CONFIG


def load_document(root: str) -> dict:
    path = _path(root)
    if not path.is_file():
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise invalid_operation("code-hospital.yaml 无法解析，拒绝改写验收白名单", target="acceptance") from exc
    if not isinstance(doc, dict):
        raise invalid_operation("code-hospital.yaml 不是映射，拒绝改写验收白名单", target="acceptance")
    return doc


def command_unsafe(command: str) -> bool:
    return (not command.strip()) or any(ch in command for ch in _META)


def load_commands(root: str) -> dict[str, str]:
    try:
        doc = load_document(root)
    except Exception:
        return {}
    acceptance = doc.get("acceptance") or {}
    if not isinstance(acceptance, dict):
        return {}
    raw = acceptance.get("commands") or {}
    if not isinstance(raw, dict):
        return {}
    commands: dict[str, str] = {}
    for key, value in raw.items():
        alias = str(key).strip()
        command = str(value).strip()
        if alias and not command_unsafe(command):
            commands[alias] = command
    return commands


def load_timeout(root: str) -> float:
    try:
        doc = load_document(root)
    except Exception:
        return _DEFAULT_TIMEOUT
    acceptance = doc.get("acceptance") or {}
    if not isinstance(acceptance, dict):
        return _DEFAULT_TIMEOUT
    try:
        timeout = float(acceptance.get("timeout_s") or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    return timeout if timeout > 0 else _DEFAULT_TIMEOUT


def save_detected(root: str, detected: list[dict]) -> dict[str, str]:
    """把探测到的固定命令补进白名单。已有别名不覆盖。"""
    doc = load_document(root)
    acceptance = doc.get("acceptance")
    if not isinstance(acceptance, dict):
        acceptance = {}
    commands = acceptance.get("commands")
    if not isinstance(commands, dict):
        commands = {}
    for item in detected:
        alias = str(item.get("alias") or "").strip()
        command = str(item.get("command") or "").strip()
        if alias and not command_unsafe(command):
            commands.setdefault(alias, command)
    acceptance["commands"] = commands
    doc["acceptance"] = acceptance
    _path(root).write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return {str(key): str(value) for key, value in commands.items()}


def scrub_env() -> dict[str, str]:
    kept: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper not in _KEEP_ENV:
            continue
        if any(part in upper for part in _SECRET_PARTS):
            continue
        kept[key] = value
    return kept


async def run_alias(root: str, alias: str) -> dict:
    """执行一个已确认别名。未入白名单或含元字符时不启动进程。"""
    command = load_commands(root).get(alias, "")
    if not command:
        return {"verdict": "skipped", "detail": f"{alias} 不在白名单，未执行"}
    if command_unsafe(command):
        return {"verdict": "fail", "detail": f"{alias} 含禁止字符，未执行"}
    argv = shlex.split(command, posix=(os.name != "nt"))
    if not argv:
        return {"verdict": "fail", "detail": f"{alias} 命令为空，未执行"}
    timeout = load_timeout(root)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=root,
            env=scrub_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return {"verdict": "fail", "detail": f"{alias} 无法启动：{exc}"[:300]}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"verdict": "fail", "detail": f"{alias} 超过 {timeout:.0f}s，未继续等待"}
    text = (out + b"\n" + err).decode("utf-8", errors="replace")[:_MAX_OUTPUT]
    if proc.returncode == 0:
        return {"verdict": "pass", "detail": f"{alias} 通过"}
    tail = " ".join(text.split())[:200]
    return {"verdict": "fail", "detail": f"{alias} 退出码 {proc.returncode}：{tail}"[:300]}


async def acceptance_check(root: str, aliases: list[str]) -> dict:
    """出院门禁用：全部跳过仍记 pass（与白名单为空时的门禁口径一致）；有失败则 fail。"""
    unique = list(dict.fromkeys(alias for alias in aliases if alias))
    if not unique:
        return {"verdict": "pass", "detail": "处方未要求命令验收"}
    results = [await run_alias(root, alias) for alias in unique]
    if all(item["verdict"] == "skipped" for item in results):
        return {"verdict": "pass", "detail": "验收命令白名单为空，本轮未执行命令"}
    failed = [item for item in results if item["verdict"] == "fail"]
    if failed:
        return {"verdict": "fail", "detail": "；".join(item["detail"] for item in failed)[:300]}
    return {"verdict": "pass", "detail": "验收命令已通过"}
