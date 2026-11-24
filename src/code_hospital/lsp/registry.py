"""LSP 语言注册表（SPI）：语言 → 语言服务规格。

机制内置、数据外置：内置默认在 catalog/languages.yaml（python/typescript/java），
被诊仓库 code-hospital.yaml 的 `catalog.lsp.languages` 可追加新语言或按 id 覆盖。
命令名只认 PATH 上的裸名、参数过 safe_args——与 manifest 对外聘仪器的安全口径一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..catalog.external_run import safe_args

_YAML = Path(__file__).resolve().parents[1] / "catalog" / "languages.yaml"
_ID = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


@dataclass(frozen=True)
class LanguageSpec:
    """一门语言怎么接：什么文件、拉哪个命令、超时多少。"""

    id: str
    extensions: tuple[str, ...]           # 带点、小写：(".py",)
    language_id: str                      # LSP languageId
    command: str                          # PATH 裸名
    args: tuple[str, ...] = ()
    dialects: tuple[tuple[str, str], ...] = ()  # 扩展名 → languageId 的例外（.js → javascript）
    install_hint: str = ""        # 缺席时随降级提示展示的安装指引（只展示，永不执行）
    startup_timeout_s: float = 45.0
    request_timeout_s: float = 20.0

    def language_id_for(self, ext: str) -> str:
        return dict(self.dialects).get(ext.lower(), self.language_id)


def builtin() -> dict[str, LanguageSpec]:
    """内置三语言（python/typescript/java）。文件坏了等于没有内置——报错留痕。"""
    specs, notes = _parse_rows(_read_yaml(_YAML))
    return specs


def for_project(project_root: str) -> tuple[dict[str, LanguageSpec], list[str]]:
    """内置 + 被诊仓库 code-hospital.yaml 的 catalog.lsp.languages（按 id 覆盖/追加）。"""
    specs = builtin()
    notes: list[str] = []
    path = Path(project_root) / "code-hospital.yaml"
    if not path.is_file():
        return specs, notes
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return specs, ["code-hospital.yaml 无法解析，LSP 语言注册表只用内置默认"]
    if not isinstance(doc, dict):
        return specs, ["code-hospital.yaml 不是映射，LSP 语言注册表只用内置默认"]
    catalog = doc.get("catalog")
    lsp = (catalog or {}).get("lsp") if isinstance(catalog, dict) else None
    rows = (lsp or {}).get("languages") if isinstance(lsp, dict) else None
    if rows is None:
        return specs, notes
    custom, custom_notes = _parse_rows(rows)
    notes.extend(custom_notes)
    specs.update(custom)
    return specs, notes


def by_extension(specs: dict[str, LanguageSpec]) -> dict[str, LanguageSpec]:
    """扩展名 → 规格。后注册的覆盖先注册的（仓级覆盖内置就靠这个顺序）。"""
    out: dict[str, LanguageSpec] = {}
    for spec in specs.values():
        for ext in spec.extensions:
            out[ext] = spec
    return out


def _read_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []


def _parse_rows(raw) -> tuple[dict[str, LanguageSpec], list[str]]:
    specs: dict[str, LanguageSpec] = {}
    notes: list[str] = []
    if not isinstance(raw, list):
        return specs, ["LSP 语言注册表不是列表，已忽略"]
    for row in raw:
        spec, problem = _parse_row(row)
        if problem:
            notes.append(problem)
            continue
        specs[spec.id] = spec
    return specs, notes


def _parse_row(row) -> tuple[LanguageSpec | None, str]:
    if not isinstance(row, dict):
        return None, "LSP 语言注册行不是映射，已跳过"
    ident = str(row.get("id") or "").strip()
    if not _ID.fullmatch(ident):
        return None, f"LSP 语言 id「{ident or '空'}」不合法（小写字母开头的短名），已跳过"
    command = str(row.get("command") or "").strip()
    if not command or any(char in command for char in "\\/\n"):
        return None, f"LSP 语言 {ident} 的命令名不合法（只认 PATH 裸名），已跳过"
    args = safe_args(row.get("args"))
    if args is None:
        return None, f"LSP 语言 {ident} 的参数含禁止字符，已跳过"
    extensions = _extensions(row.get("extensions"))
    if not extensions:
        return None, f"LSP 语言 {ident} 没有声明扩展名，已跳过"
    language_id = str(row.get("language_id") or ident).strip()
    dialects = row.get("dialects")
    pairs: list[tuple[str, str]] = []
    if isinstance(dialects, dict):
        for ext, lang in dialects.items():
            pairs.append((_ext(ext), str(lang).strip()))
    return LanguageSpec(
        id=ident,
        extensions=extensions,
        language_id=language_id,
        command=command,
        args=tuple(args),
        dialects=tuple(pairs),
        install_hint=" ".join(str(row.get("install_hint") or "").split())[:120],
        startup_timeout_s=_timeout(row.get("startup_timeout_s"), 45.0),
        request_timeout_s=_timeout(row.get("request_timeout_s"), 20.0),
    ), ""


def _ext(value) -> str:
    return "." + str(value).lstrip(".").lower()


def _extensions(raw) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    kept = [_ext(item) for item in raw if str(item).strip()]
    return tuple(dict.fromkeys(kept))


def _timeout(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


__all__ = ["LanguageSpec", "builtin", "by_extension", "for_project"]
