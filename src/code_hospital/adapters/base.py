"""扫描设备适配器协议：每台「检查设备」统一抽象（设计 §2.2）。

适配器只产出 RawFinding；归一化（指纹/净化/锚点）由扫描师（departments/scanner）完成。
"""

from __future__ import annotations

import asyncio
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class RawFinding:
    """设备原始发现（未归一化）。severity 取设备原值，由扫描师映射。"""

    rule_id: str
    message: str
    file_path: str  # 仓库相对 POSIX 路径
    line_start: int
    line_end: int = 0
    severity: str = "info"  # critical/major/minor/nit/info（适配器先粗映射）
    evidence: str = ""  # 允许为空，扫描师会从文件内容补
    extra: dict = field(default_factory=dict)


class ScannerAdapter(ABC):
    name: str = ""
    binary: str = ""
    # 适配器关心的扩展名；空集 = 对全部文件生效
    extensions: frozenset[str] = frozenset()

    def is_available(self) -> bool:
        return shutil.which(self.binary) is not None

    async def version(self) -> str:
        if not self.is_available():
            return ""
        try:
            out, _ = await _run([self.binary, "--version"], cwd=".", timeout=10)
            return out.strip().splitlines()[0][:60] if out.strip() else "unknown"
        except Exception:
            return "unknown"

    def filter_files(self, files: list[str]) -> list[str]:
        if not self.extensions:
            return files
        return [f for f in files if f.rsplit(".", 1)[-1].lower() in
                {e.lstrip(".") for e in self.extensions}]

    @abstractmethod
    async def scan(self, repo_root: str, files: list[str], timeout_ms: int) -> list[RawFinding]:
        """对文件清单执行扫描，返回原始发现。设备执行失败应抛 AdapterError。"""
        ...


class AdapterError(Exception):
    """设备执行失败（扫描师记入「覆盖与限制」，不中断其他设备）。"""


async def _run(
    args: list[str], cwd: str, timeout: float, input_text: str | None = None
) -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if input_text is not None else None,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input_text.encode() if input_text else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise AdapterError(f"{args[0]} 执行超时（{timeout}s）")
    return out.decode(errors="replace"), err.decode(errors="replace")
