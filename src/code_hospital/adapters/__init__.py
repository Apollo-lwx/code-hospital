"""扫描设备适配器注册表（设计 §2 适配器层）。

测试可通过 register()/clear() 注入替身适配器。
"""

from __future__ import annotations

from .base import RawFinding, ScannerAdapter

_REGISTRY: list[ScannerAdapter] = []


def register(adapter: ScannerAdapter) -> None:
    _REGISTRY.append(adapter)


def all_adapters() -> list[ScannerAdapter]:
    return list(_REGISTRY)


def clear() -> None:
    _REGISTRY.clear()


def register_defaults() -> None:
    """注册三个内置适配器（幂等）。"""
    from .gitleaks_adapter import GitleaksAdapter
    from .ruff_adapter import RuffAdapter
    from .semgrep_adapter import SemgrepAdapter

    names = {a.name for a in _REGISTRY}
    for cls in (RuffAdapter, SemgrepAdapter, GitleaksAdapter):
        inst = cls()
        if inst.name not in names:
            _REGISTRY.append(inst)


__all__ = ["RawFinding", "ScannerAdapter", "register", "all_adapters", "clear", "register_defaults"]
