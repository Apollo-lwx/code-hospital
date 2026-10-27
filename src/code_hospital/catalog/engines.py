"""外部仪器登记表。

扫描师只问这张表：选中的仪器 id 有没有对应引擎。
加一台同类工具时，在这里登记引擎，并在 instruments.yaml、号别里写上 id。
不要把新仪器的命令写进扫描流程。
"""

from __future__ import annotations

from .spotbugs import SpotBugsEngine

ENGINES = {
    "spotbugs": SpotBugsEngine(),
}


def engines_for(selected: set[str] | list[str]) -> list:
    wanted = set(selected)
    return [engine for instrument_id, engine in ENGINES.items() if instrument_id in wanted]
