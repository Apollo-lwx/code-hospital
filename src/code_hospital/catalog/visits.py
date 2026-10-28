"""号别：这场要查什么。和分诊（急不急）、范围（查哪里）分开。"""

from __future__ import annotations

VISIT_KINDS = (
    "checkup",
    "admission",
    "security",
    "performance",
    "defect",
    "algorithm",
    "types",
    "architecture",
    "test",
    "supply",
    "business",
    "severe",
    "general",
)

VISIT_LABELS = {
    "checkup": "体检",
    "admission": "合入门诊",
    "security": "安全",
    "performance": "性能",
    "defect": "缺陷",
    "algorithm": "算法",
    "types": "类型契约",
    "architecture": "架构",
    "test": "测试",
    "supply": "供应链",
    "business": "业务",
    "severe": "重症",
    "general": "全科",
}

# plus：在分诊套餐上加做。only：只拍这一号的仪器。
_PANELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "checkup": ("plus", ()),
    "admission": ("plus", ("security-ruler", "defect-ruler", "test-gap", "taint-ruler")),
    "security": ("only", ("security-ruler", "java-review", "ts-review", "gitleaks", "semgrep", "java-pack")),
    "performance": ("only", ("perf-ruler", "hotspot-ruler")),
    "defect": ("only", ("defect-ruler", "python-review", "java-review", "ts-review", "java-pack", "ruff", "spotbugs")),
    "algorithm": ("only", ("algo-ruler",)),
    "types": ("only", ("type-ruler",)),
    "architecture": ("only", ("layer-ruler",)),
    "test": ("only", ("test-gap",)),
    "supply": ("only", ("supply-ruler",)),
    "business": ("only", ("business-ruler",)),
    "severe": ("only", ("taint-ruler", "impact-review", "spotbugs")),
    "general": ("plus", (
        "security-ruler", "defect-ruler", "perf-ruler", "algo-ruler",
        "supply-ruler", "test-gap", "type-ruler", "layer-ruler", "hotspot-ruler",
        "business-ruler", "spotbugs",
    )),
}


def normalize_visit_kind(value: str, project_root: str = "") -> str:
    kind = (value or "checkup").strip() or "checkup"
    if kind in _PANELS:
        return kind
    from .outside import overlay

    if kind in overlay(project_root)["visits"]:
        return kind
    allowed = list(VISIT_KINDS) + list(overlay(project_root)["visits"])
    raise ValueError("、".join(allowed))


def panel(kind: str, project_root: str = "") -> tuple[str, tuple[str, ...]]:
    kind = normalize_visit_kind(kind, project_root)
    if kind in _PANELS:
        return _PANELS[kind]
    from .outside import overlay

    visit = overlay(project_root)["visits"][kind]
    return visit["mode"], tuple(visit["instruments"])


def label_for(kind: str, project_root: str = "") -> str:
    if kind in VISIT_LABELS:
        return VISIT_LABELS[kind]
    if not project_root:
        return kind
    from .outside import overlay

    visit = overlay(project_root)["visits"].get(kind)
    if visit:
        return visit["label"]
    return kind


def coverage_line(
    visit_kind: str,
    selected: list[str],
    devices: list[dict],
    project_root: str = "",
) -> str:
    """计划内仪器有没有拍成。没拍成的维度不能当成正常。"""
    label = label_for(visit_kind, project_root)
    by_tool: dict[str, dict] = {}
    for row in devices:
        by_tool.setdefault(row.get("tool", ""), row)
    if not selected:
        text = f"号别{label}：范围内没有这号要拍的文件。"
        if visit_kind == "general":
            text += "重症未查：全科不跑函数内污点和改动影响。要查本次 diff，挂号别用 severe。"
        return text
    done: list[str] = []
    missed: list[str] = []
    for inst in selected:
        row = by_tool.get(inst)
        if row and row.get("available") and row.get("outcome") == "ok":
            done.append(inst)
        elif row is None:
            missed.append(f"{inst}（未进入本轮调度）")
        else:
            missed.append(f"{inst}（{row.get('outcome') or '未完成'}）")
    done_text = "、".join(done) if done else "无"
    if missed:
        text = (
            f"号别{label}：已拍 {done_text}；没拍成 {'、'.join(missed)}。"
            "没拍成的维度不能写成正常。"
        )
    else:
        text = f"号别{label}：计划内仪器都已拍完（{done_text}）。"
    if visit_kind == "general":
        text += "重症未查：全科不跑函数内污点和改动影响。要查本次 diff，挂号别用 severe。"
    return text
