"""能力市场：把第三方仪器装进医院，但不让它们直接进报告。

装在哪：`<repo>/.hospital/market/`。选这里的三个理由——
1. 医院的全部状态已经在 `.hospital/`，不引入第二处；
2. 这个目录在扫描排除名单里，装进来的 manifest 不会被当成被诊代码；
3. 跨仓库共享的市场目录属于租户级问题，当前阶段不做，架构上留了口。

装进来一律 `probation`：跑得动、进覆盖账、**不进报告**。
转正判据在 `catalog/probation.py`（第 5 步），这里负责把试用期的账存下来、
把判据结果挂在 index 上、以及转正这个有后果的动作。

转正这件事**不自动做**：代码只判「够不够格」，签字的人另有其人。
装一台第三方 MCP 等于让它读你的仓库，这个决定的后果不该由判据自己承担。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import yaml

from . import manifest as mf

MARKET_DIR = ".hospital/market"
_INDEX = "index.json"
_MANIFEST_DIR = "manifests"


def market_root(project_root: str) -> Path:
    return Path(project_root) / MARKET_DIR


def installed(project_root: str) -> dict[str, dict]:
    """已装的（含已退役，调用方自己按 tier 筛）。读不出来就当没装。

    这里刻意不抛异常：市场坏掉不该让整个医院起不来。
    坏在哪由 verify() 说出来，装配层只按 tier 决定跑不跑。
    """
    base = market_root(project_root) / _MANIFEST_DIR
    if not base.is_dir():
        return {}
    index = _read_index(project_root)
    out: dict[str, dict] = {}
    for file in sorted(base.glob("*.yaml")):
        doc, problems = mf.load_file(file)
        ident = doc.get("id") or file.stem
        meta = index.get(ident) or {}
        if problems:
            # 装的时候是好的、后来文件被改了：档位压到 retired 且不再被 index 覆盖，
            # 否则列表里会显示成一台正常的 probation，实际一次也跑不起来。
            doc = {
                **doc, "id": ident, "problems": problems, "tier": "retired",
                "installed_at": int(meta.get("installed_at") or 0), "origin": "market",
                "trial": {}, "verdict": {},
            }
            out[ident] = doc
            continue
        doc["problems"] = []
        doc["tier"] = str(meta.get("tier") or doc.get("tier") or "probation")
        doc["installed_at"] = int(meta.get("installed_at") or 0)
        doc["origin"] = "market"
        doc["trial"] = meta.get("trial") if isinstance(meta.get("trial"), dict) else {}
        doc["verdict"] = meta.get("verdict") if isinstance(meta.get("verdict"), dict) else {}
        out[ident] = doc
    return out


def live(project_root: str) -> dict[str, dict]:
    """能参与本次检验的：probation 与 active。retired 不跑。"""
    return {
        ident: doc for ident, doc in installed(project_root).items()
        if doc.get("tier") in ("probation", "active") and not doc.get("problems")
    }


def runnable(project_root: str) -> dict[str, dict]:
    """live 里再筛掉被风险声明挡住的那几台。"""
    return {
        ident: doc for ident, doc in live(project_root).items()
        if not blocked_reason(doc)
    }


def blocked_reason(doc: dict) -> str:
    """影子期不许写仓库。

    这条不是不信任，是顺序：转正判据要拿这台仪器的产物和现任比，
    比对期间它改了仓库，比的就不是同一份源码了。
    """
    if doc.get("tier") == "active":
        return ""
    risk = doc.get("risk") or {}
    if risk.get("writes_repo"):
        return "声明了会写仓库，影子期不运行"
    return ""


def names(project_root: str = "") -> set[str]:
    """已占用的 id：内置 + 市场。第三方不许顶替其中任何一个。"""
    taken = set(mf.core_manifests())
    if project_root:
        taken |= set(installed(project_root))
        from .outside import overlay

        taken |= set(overlay(project_root)["instruments"])
    return taken


def install(project_root: str, source: str | Path) -> dict:
    """装一份 manifest。返回 {ok, id, tier, problems, note}。

    失败不落盘。成功只落 manifest 原文加一条 index 记录，
    不做任何"相信它"的动作——跑不跑由 tier 与 risk 决定。
    """
    doc, problems = mf.load_file(source)
    if problems:
        return {"ok": False, "id": doc.get("id", ""), "problems": problems, "note": ""}

    ident = doc["id"]
    taken = names(project_root)
    if ident in taken:
        return {
            "ok": False, "id": ident,
            "problems": [f"id「{ident}」已被占用；第三方仪器不能顶替已有仪器，换个 id 再装"],
            "note": "",
        }

    base = market_root(project_root)
    (base / _MANIFEST_DIR).mkdir(parents=True, exist_ok=True)
    target = base / _MANIFEST_DIR / f"{ident}.yaml"
    # 原样保存原文，不写回序列化结果——客户要能拿回去继续编辑。
    target.write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")

    index = _read_index(project_root)
    index[ident] = {
        "tier": doc.get("tier") or "probation",
        "kind": doc.get("kind") or "instrument",
        "origin": "market",
        "installed_at": int(time.time() * 1000),
        "verified_at": 0,
        "source": str(source)[:300],
        "blocked": blocked_reason(doc),
        "history": [{"at": int(time.time() * 1000), "action": "install", "source": str(source)[:200]}],
    }
    _write_index(project_root, index)
    return {
        "ok": True,
        "id": ident,
        "tier": index[ident]["tier"],
        "problems": [],
        "note": _install_note(doc, index[ident]),
    }


def _install_note(doc: dict, meta: dict) -> str:
    if doc.get("kind") == "substrate":
        return "基座已登记。基座供能力，不参与检验，也不产发现。"
    if meta.get("blocked"):
        return f"已装，但{meta['blocked']}。"
    return (
        "已装，按影子期处理：会跑，结果只进覆盖账，不进报告、不进处方、不进疗效。"
        "转正要看三个数（样本、与现任同处、空位置），调 review；数字过关还要人签字。"
    )


def retire(project_root: str, ident: str) -> dict:
    """退役：停止运行，保留文件与账。

    不删的理由和病历不销毁一样——「这台仪器曾经装过、跑过多少、为什么下架」
    是审计要回答的问题，删了文件就答不出来。
    """
    if not ident:
        return {"ok": False, "id": "", "note": "要指定仪器 id"}
    base = market_root(project_root)
    target = base / _MANIFEST_DIR / f"{ident}.yaml"
    index = _read_index(project_root)
    if not target.is_file() and ident not in index:
        return {"ok": False, "id": ident, "note": f"没有装过 {ident}"}
    index[ident] = {
        **index.get(ident, {}),
        "tier": "retired",
        "retired_at": int(time.time() * 1000),
        "history": _history(
            index.get(ident) or {},
            {"at": int(time.time() * 1000), "action": "retire"},
        ),
    }
    _write_index(project_root, index)
    return {"ok": True, "id": ident, "note": "已退役：不再运行，manifest 与账保留。"}


def verify(project_root: str, ids: list[str] | None = None) -> dict:
    """重新校验已装的 manifest，并检查命令是否到岗。

    「装在」不等于「跑得动」。这一步把两者分开：
    manifest 有问题 → 不可用；命令不在 PATH → 到岗失败（不是装错）。
    """
    base = market_root(project_root) / _MANIFEST_DIR
    index = _read_index(project_root)
    wanted = [ident for ident in (ids or []) if ident]
    if not wanted:
        wanted = sorted(file.stem for file in base.glob("*.yaml")) if base.is_dir() else []
    rows: list[dict] = []
    for ident in wanted:
        file = base / f"{ident}.yaml"
        doc, problems = mf.load_file(file)
        meta = index.get(ident) or {}
        tier = str(meta.get("tier") or doc.get("tier") or "probation")
        present, absent = _presence(doc) if not problems else (False, "")
        rows.append({
            "id": ident,
            "kind": doc.get("kind") or "",
            "tier": tier,
            "transport": doc.get("transport") or "",
            "manifest_ok": not problems,
            "problems": problems,
            "present": present,
            "absent": absent,
            "blocked": blocked_reason({**doc, "tier": tier}),
        })
        if ident in index:
            index[ident] = {**index[ident], "verified_at": int(time.time() * 1000)}
    if index:
        _write_index(project_root, index)
    return {"root": str(market_root(project_root)), "instruments": rows}


def _presence(doc: dict) -> tuple[bool, str]:
    """命令/引擎在不在本机。基座不查——它不是靠命令行跑的。"""
    if doc.get("kind") == "substrate":
        return True, ""
    transport = doc.get("transport") or ""
    if transport in ("external", "mcp"):
        name = doc.get("command") or doc.get("binary") or ""
        return (True, "") if name and shutil.which(name) else (False, f"{name or '命令'} 不在 PATH 上")
    if transport == "semgrep":
        return (True, "") if shutil.which("semgrep") else (False, "semgrep 不在 PATH 上")
    return True, ""  # builtin 由医院自己驱动，不依赖外部可执行文件


def listing(project_root: str) -> dict:
    """市场全貌：内置 core + 已装。给 hosp_market list 用。"""
    core = mf.core_manifests()
    own = installed(project_root)
    return {
        "root": str(market_root(project_root)),
        "core": [
            {"id": doc["id"], "transport": doc.get("transport", ""), "concern": doc.get("concern", "")[:60]}
            for doc in core.values()
        ],
        "installed": [
            {
                "id": ident,
                "kind": doc.get("kind", ""),
                "tier": doc.get("tier", ""),
                "transport": doc.get("transport", ""),
                "concern": doc.get("concern", "")[:60],
                "problems": doc.get("problems") or [],
                "trial": _trial_view(doc.get("trial")),
                "verdict": (doc.get("verdict") or {}).get("state", ""),
                "verdict_line": (doc.get("verdict") or {}).get("line", ""),
            }
            for ident, doc in sorted(own.items())
        ],
        "note": _listing_note(own),
    }


def _trial_view(trial) -> dict:
    """列表里只放三个数，不放整本账。"""
    from . import probation

    book = probation.load(trial)
    return {
        "runs": book["runs"],
        "failed": book["failed"],
        "skipped": book["skipped"],
        "claims": book["claims"],
        "corroborated": book["corroborated"],
    }


def _listing_note(own: dict[str, dict]) -> str:
    """按实际档位说话。装了没转正、转正了、退役了，说的不是同一句话。"""
    if not own:
        return "还没有装第三方仪器。core 那批是随包的，不用装。"
    counts: dict[str, int] = {}
    for doc in own.values():
        counts[str(doc.get("tier") or "probation")] = counts.get(str(doc.get("tier") or "probation"), 0) + 1
    parts = []
    if counts.get("probation"):
        total = sum(
            1 for doc in own.values() if str(doc.get("tier") or "probation") == "probation"
        )
        due = sum(
            1 for doc in own.values()
            if str(doc.get("tier") or "probation") == "probation"
            and (doc.get("verdict") or {}).get("promotable")
        )
        tail = f"，其中 {due} 台已够格等人签字" if due else ""
        parts.append(f"{total} 台影子期（跑，但结论只进覆盖账{tail}）")
    if counts.get("active"):
        parts.append(f"{counts['active']} 台已转正（结论进报告）")
    if counts.get("retired"):
        parts.append(f"{counts['retired']} 台已退役（不跑，账保留）")
    return "已装仪器：" + "；".join(parts) + "。"


def stage(project_root: str) -> tuple[dict[str, dict], list[str]]:
    """把市场仪器转成装配层认的行。返回 (rows, 限制项)。

    限制项写的是「装了但没跑」，不是「跑了没问题」——影子期最容易犯的错
    是把「没进报告」说成「没问题」。
    """
    rows: dict[str, dict] = {}
    notes: list[str] = []
    for ident, doc in installed(project_root).items():
        if doc.get("problems"):
            notes.append(f"{ident}：manifest 校验不过，没有装成，这次不跑")
            continue
        if doc.get("tier") == "retired":
            continue
        if doc.get("kind") == "substrate":
            continue  # 基座不是仪器，装配层不碰
        why = blocked_reason(doc)
        if why:
            notes.append(f"{ident}：{why}")
            continue
        rows[ident] = mf.to_row(doc)
    return rows, notes


def plan(project_root: str) -> tuple[dict[str, dict], list[str]]:
    """这一轮实际跑哪几台。返回 (要跑的行, 没跑的理由)。

    两个成本闸门，都只影响「这次跑不跑」，不影响「够不够格」：
    - 轮次节奏：样本够了改成每 `EVERY_NTH` 轮跑一次（`probation.should_run`）；
    - 每轮上限：最多 `MAX_PER_SCAN` 台，按样本少的优先——缺样本的先跑。
    被挡下的都要写一条账，否则「这轮没跑」会看起来像「这轮跑了没问题」。
    """
    from . import probation

    rows, notes = stage(project_root)
    index = _read_index(project_root)
    order = sorted(
        rows, key=lambda ident: (probation.load((index.get(ident) or {}).get("trial"))["runs"], ident)
    )
    chosen: dict[str, dict] = {}
    capped: list[str] = []
    touched = False
    for ident in order:
        if rows[ident].get("tier") == "active":
            # 已转正的不受试用期节奏管：它不是"还在观察"，它是现任。
            chosen[ident] = rows[ident]
            continue
        trial = (index.get(ident) or {}).get("trial")
        due, why = probation.should_run(trial)
        if not due:
            notes.append(f"{ident}：{why}")
            _bump(index, ident, "skipped")
            touched = True
            continue
        if len(chosen) >= probation.MAX_PER_SCAN:
            capped.append(ident)
            continue
        chosen[ident] = rows[ident]
    if capped:
        notes.append(
            f"{len(capped)} 台影子期仪器本轮没跑（每轮最多 {probation.MAX_PER_SCAN} 台，"
            f"按样本少的优先）：{'、'.join(capped)}。这是成本闸门，不是能力判断。"
        )
        for ident in capped:
            _bump(index, ident, "skipped")
        touched = True
    if touched:
        _write_index(project_root, index)
    return chosen, notes


def note_run(project_root: str, observed: list[dict], report_id: str = "") -> dict[str, dict]:
    """把这一轮的影子观测并进各台的试用期账，顺手重算判据。

    返回 {id: 账}，调用方拿它写「样本 x/8 轮」这类话——
    不写出来，客户会以为装了就等于生效了。
    """
    from . import probation

    index = _read_index(project_root)
    trials: dict[str, dict] = {}
    changed = False
    for row in observed or []:
        ident = str(row.get("instrument") or "")
        if not ident:
            continue
        trial = probation.observe(
            (index.get(ident) or {}).get("trial"),
            {
                "status": row.get("ran"),
                "claims": row.get("examined", row.get("findings")),
                "corroborated": row.get("corroborated"),
                "checkable": row.get("checkable"),
                "bad": row.get("bad"),
                "incumbent_claims": row.get("incumbent_claims"),
                "incumbent_bad": row.get("incumbent_bad"),
                "note": row.get("note"),
                "case_id": row.get("case_id"),
                "samples": row.get("samples"),
            },
            report_id,
        )
        trials[ident] = trial
        meta = index.get(ident) or {}
        if trial == probation.load(meta.get("trial")) and report_id and report_id in (meta.get("trial") or {}).get("seen", []):
            continue  # 同一份报告重跑，账不动
        verdict = probation.verdict(trial)
        index[ident] = {
            **meta,
            "trial": trial,
            "verdict": {
                "state": verdict["state"],
                "promotable": verdict["promotable"],
                "line": verdict["line"],
                "at": int(time.time() * 1000),
            },
        }
        changed = True
    if changed:
        _write_index(project_root, index)
    return trials


def review(project_root: str, ids: list[str] | None = None) -> dict:
    """重算一遍试用期判据，不改变任何档位。

    这一步是「判」不是「转」：把三个数和一句人话摆出来，
    签不签字由 `promote` 的调用方决定。
    """
    from . import probation

    index = _read_index(project_root)
    own = installed(project_root)
    wanted = [ident for ident in (ids or []) if ident] or sorted(own)
    rows: list[dict] = []
    for ident in wanted:
        doc = own.get(ident)
        if doc is None:
            rows.append({
                "id": ident, "state": "missing", "promotable": False,
                "line": f"没有装过 {ident}", "tier": "",
            })
            continue
        verdict = probation.verdict((index.get(ident) or {}).get("trial"))
        rows.append({
            "id": ident,
            "tier": doc.get("tier") or "",
            "state": verdict["state"],
            "promotable": verdict["promotable"],
            "line": verdict["line"],
            "runs": verdict["runs"],
            "claims": verdict["claims"],
            "corroborated": verdict["corroborated"],
            "checkable": verdict["checkable"],
            "bad": verdict["bad"],
            "rate": verdict["rate"],
            "bad_rate": verdict["bad_rate"],
            "blocked": blocked_reason(doc),
        })
    ready = [row for row in rows if row["promotable"] and row["tier"] == "probation"]
    return {
        "instruments": rows,
        "note": (
            f"{len(ready)} 台够格转正，等人签字（`promote` 要带 confirm）；判据只判格，不代签。"
            if ready else
            "还没有够格转正的。样本、重合、误报三个数都在 instruments 里，缺哪个看 line。"
        ),
    }


def promote(project_root: str, ident: str, confirm: bool = False) -> dict:
    """转正。代码判格，人签字，账上留快照。

    为什么判据不自己转：转正意味着这台仪器的结论从此进报告、进处方、进疗效。
    判据能看到"和现任同处 75%"，看不到"这家客户能不能接受它读源码"。
    后者不是数字能回答的问题。
    """
    from . import probation

    if not ident:
        return {"ok": False, "id": "", "note": "要指定仪器 id"}
    base = market_root(project_root) / _MANIFEST_DIR
    doc, problems = mf.load_file(base / f"{ident}.yaml")
    if problems:
        return {
            "ok": False, "id": ident,
            "note": "manifest 校验不过，不能转正：" + "；".join(problems[:3]),
        }
    index = _read_index(project_root)
    meta = index.get(ident) or {}
    tier = str(meta.get("tier") or doc.get("tier") or "probation")
    if tier == "active":
        return {"ok": False, "id": ident, "note": "已经转正了，不重复转。"}
    if tier == "retired":
        return {
            "ok": False, "id": ident,
            "note": "已退役的不能直接转正。要重装一份，重新攒样本——上一条命已经下架了。",
        }
    if doc.get("kind") == "substrate":
        return {"ok": False, "id": ident, "note": "基座不进报告，没有试用期，也没有转正。"}
    verdict = probation.verdict(meta.get("trial"))
    if not verdict["promotable"]:
        return {
            "ok": False,
            "id": ident,
            "state": verdict["state"],
            "note": "不够格转正：" + "；".join(verdict["reasons"] or [verdict["line"]]),
        }
    if not confirm:
        return {
            "ok": False,
            "id": ident,
            "state": verdict["state"],
            "needs_confirm": True,
            "note": (
                f"够格了，但转正要有后果地签一次：{verdict['line']}"
                "确认后带 confirm=true 再调一次。"
            ),
        }
    now = int(time.time() * 1000)
    approved = {
        "at": now,
        "state": verdict["state"],
        "runs": verdict["runs"],
        "claims": verdict["claims"],
        "corroborated": verdict["corroborated"],
        "checkable": verdict["checkable"],
        "bad": verdict["bad"],
    }
    index[ident] = {
        **meta,
        "tier": "active",
        "promoted_at": now,
        "promotion": approved,
        "history": _history(meta, {"at": now, "action": "promote", **approved}),
    }
    _write_index(project_root, index)
    return {
        "ok": True,
        "id": ident,
        "tier": "active",
        "state": verdict["state"],
        "note": (
            f"已转正，下一次初检起它的结论进报告、进处方、进疗效。判据快照已存档："
            f"{verdict['runs']} 轮 / {verdict['claims']} 条 / 同处 {verdict['corroborated']}。"
        ),
    }


def _bump(index: dict, ident: str, key: str) -> None:
    """给试用期账加一个计数。没有账就起一本。"""
    from . import probation

    meta = index.get(ident) or {}
    trial = probation.load(meta.get("trial"))
    trial[key] = int(trial.get(key) or 0) + 1
    index[ident] = {**meta, "trial": trial}


def _history(meta: dict, entry: dict) -> list[dict]:
    """动作历史。审计要回答「为什么上架、为什么下架」，两句话都得留着。"""
    rows = [row for row in (meta.get("history") or []) if isinstance(row, dict)]
    rows.append(entry)
    return rows[-12:]


def _read_index(project_root: str) -> dict:
    path = market_root(project_root) / _INDEX
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_index(project_root: str, index: dict) -> None:
    base = market_root(project_root)
    base.mkdir(parents=True, exist_ok=True)
    (base / _INDEX).write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )


__all__ = [
    "MARKET_DIR",
    "blocked_reason",
    "install",
    "installed",
    "listing",
    "live",
    "market_root",
    "names",
    "note_run",
    "plan",
    "promote",
    "retire",
    "review",
    "runnable",
    "stage",
    "verify",
]
