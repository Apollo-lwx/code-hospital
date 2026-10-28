"""ReviewBench 金标和医院发现的对应样本。

24 条金标来自本机已有的裁判包，没有重跑评测，也没有新编评语。
equivalent 只在读过金标说明和发现说明之后填写。行号碰上不算等价。
医院没有对应发现的金标标 false。金标里没有的医院发现单独记下，不进语义分母。
"""

from __future__ import annotations

from .hits import measure_coverage, render_eval_report, score_hits

_FINDINGS = {
    "apilix-1044": {
        "file": "server/index.js",
        "line": 1044,
        "rule_id": "security.path-prefix",
        "message": "前缀没有以分隔符结束，兄弟目录会通过",
        "concluded_by": "instrument",
    },
    "apilix-1083": {
        "file": "server/index.js",
        "line": 1083,
        "rule_id": "security.path-prefix",
        "message": "前缀没有以分隔符结束，兄弟目录会通过",
        "concluded_by": "instrument",
    },
    "apilix-1118": {
        "file": "server/index.js",
        "line": 1118,
        "rule_id": "security.path-prefix",
        "message": "前缀没有以分隔符结束，兄弟目录会通过",
        "concluded_by": "instrument",
    },
    "fortran-138": {
        "file": "fortran_to_rust/strategies/agentic.py",
        "line": 138,
        "rule_id": "defect.swallowed",
        "message": "异常被接住后没有再抛出或记录",
        "concluded_by": "instrument",
    },
    "fortran-118": {
        "file": "fortran_to_rust/strategies/hybrid.py",
        "line": 118,
        "rule_id": "F841",
        "message": "局部变量 result 赋值后没有使用",
        "concluded_by": "instrument",
    },
    "protrider-70": {
        "file": "src/protrider/model/model_helper.py",
        "line": 70,
        "rule_id": "F841",
        "message": "局部变量 presence_out 赋值后没有使用",
        "concluded_by": "instrument",
    },
    "protrider-74": {
        "file": "src/protrider/model/model_helper.py",
        "line": 74,
        "rule_id": "F841",
        "message": "局部变量 auc_prec_rec 赋值后没有使用",
        "concluded_by": "instrument",
    },
    "protrider-119": {
        "file": "src/protrider/model/model_helper.py",
        "line": 119,
        "rule_id": "F841",
        "message": "局部变量 tol 赋值后没有使用",
        "concluded_by": "instrument",
    },
    "protrider-121": {
        "file": "src/protrider/model/model_helper.py",
        "line": 121,
        "rule_id": "F841",
        "message": "局部变量 k_max 赋值后没有使用",
        "concluded_by": "instrument",
    },
}

# relation: same 同一缺陷；near 对着一条发现但不是同一缺陷；miss 没有医院发现；extra 医院发现没有金标。
_ROWS = [
    {
        "gold_id": "rb-apilix-prefix",
        "pr": "cmik/apilix",
        "relation": "same",
        "gold": {
            "file": "server/index.js",
            "line": 1044,
            "end": 1044,
            "message": "repoDir.startsWith(path.resolve(repoBase)) 少了结尾分隔符，workspaceId 写成 ../workspaces_evil 就能通过",
        },
        "finding": "apilix-1044",
        "equivalent": True,
        "note": "同一处前缀检查少了路径分隔符，兄弟目录会通过。",
    },
    {
        "gold_id": "rb-apilix-datadir",
        "pr": "cmik/apilix",
        "relation": "near",
        "gold": {
            "file": "server/index.js",
            "line": 1037,
            "end": 1044,
            "message": "dataDir 直接来自请求体，调用方可以传 / 让服务往任意目录写仓库",
        },
        "finding": "apilix-1044",
        "equivalent": False,
        "note": "行号碰上了。金标说的是 dataDir 本身不受约束，医院说的是前缀少分隔符。",
    },
    {
        "gold_id": "rb-apilix-bcrypt",
        "pr": "cmik/apilix",
        "relation": "miss",
        "gold": {
            "file": "server/package.json",
            "line": 1,
            "end": 23,
            "message": "锁文件根依赖有 bcryptjs 和 jsonwebtoken，server/package.json 里没有，本目录 npm install 装不上",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-apilix-lastsynced",
        "pr": "cmik/apilix",
        "relation": "miss",
        "gold": {
            "file": "client/src/utils/syncEngine.ts",
            "line": 49,
            "end": 51,
            "message": "lastSynced 从未写入，冲突检测里的 localTs 一直是空",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-apilix-rfc2822",
        "pr": "cmik/apilix",
        "relation": "miss",
        "gold": {
            "file": "client/src/utils/syncEngine.ts",
            "line": 50,
            "end": 51,
            "message": "冲突检测用字符串比较 Last-Modified，RFC 2822 不能按字典序比先后",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-apilix-savetimer",
        "pr": "cmik/apilix",
        "relation": "miss",
        "gold": {
            "file": "client/src/store.tsx",
            "line": 573,
            "end": 573,
            "message": "saveTimer 放在 useState 里，每次设防抖定时器都会多渲染一次，清理用的是旧值",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-apilix-switch-lost",
        "pr": "cmik/apilix",
        "relation": "miss",
        "gold": {
            "file": "client/src/store.tsx",
            "line": 648,
            "end": 668,
            "message": "切换工作区会清掉上一个工作区还没触发的 300 毫秒保存，刚改的内容丢掉",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-apilix-syncconfig-wipe",
        "pr": "cmik/apilix",
        "relation": "miss",
        "gold": {
            "file": "client/src/utils/storageDriver.ts",
            "line": 179,
            "end": 191,
            "message": "writeSyncConfig 读失败后用空对象继续写，其他工作区的同步凭据被清掉",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-apilix-electron-syncfs",
        "pr": "cmik/apilix",
        "relation": "miss",
        "gold": {
            "file": "electron/main.js",
            "line": 140,
            "end": 176,
            "message": "读写 JSON 和列目录用同步文件接口，堵在 Electron 主进程上",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-apilix-delete-manifest",
        "pr": "cmik/apilix",
        "relation": "miss",
        "gold": {
            "file": "client/src/components/WorkspaceManagerModal.tsx",
            "line": 87,
            "end": 101,
            "message": "删除工作区先改内存再写清单，写失败后磁盘上的清单仍留着已删项",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-apilix-s3-electron",
        "pr": "cmik/apilix",
        "relation": "miss",
        "gold": {
            "file": "client/src/utils/sync/s3Adapter.ts",
            "line": 23,
            "end": 26,
            "message": "S3 适配器依赖 electronAPI.getPresignedUrl，浏览器里会抛错，界面仍可选 S3",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-apilix-simplegit-health",
        "pr": "cmik/apilix",
        "relation": "miss",
        "gold": {
            "file": "server/index.js",
            "line": 1009,
            "end": 1009,
            "message": "simple-git 加载失败后服务照跑，健康检查仍返回 200",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。行号离三条前缀发现都很远。",
    },
    {
        "gold_id": "rb-fortran-env-quotes",
        "pr": "DisciplinedSoftware/Codebase-Conversion",
        "relation": "miss",
        "gold": {
            "file": "convert.py",
            "line": 37,
            "end": 43,
            "message": ".env 只做了 strip，值两边的引号还在，密钥会带上引号",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-fortran-partial-download",
        "pr": "DisciplinedSoftware/Codebase-Conversion",
        "relation": "miss",
        "gold": {
            "file": "fortran_to_rust/fetcher.py",
            "line": 63,
            "end": 66,
            "message": "下载中断后超过 100 字节的残文件被当成成功，没有删掉",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-fortran-hybrid-filter",
        "pr": "DisciplinedSoftware/Codebase-Conversion",
        "relation": "near",
        "gold": {
            "file": "fortran_to_rust/strategies/hybrid.py",
            "line": 161,
            "end": 172,
            "message": "过滤以 /* 开头的行时，把 f2c 的函数原型 /* Subroutine */ int dgemm_ 也删了，函数体不会进入翻译",
        },
        "finding": "fortran-118",
        "equivalent": False,
        "note": "同一文件。金标说注释过滤误删原型，医院说 result 赋值后没用。",
    },
    {
        "gold_id": "rb-fortran-accuracy-true",
        "pr": "DisciplinedSoftware/Codebase-Conversion",
        "relation": "miss",
        "gold": {
            "file": "fortran_to_rust/test_harness.py",
            "line": 672,
            "end": 691,
            "message": "找不到例程或没给源文件时，精度检查仍返回 passed=True",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-fortran-last-exc",
        "pr": "DisciplinedSoftware/Codebase-Conversion",
        "relation": "miss",
        "gold": {
            "file": "fortran_to_rust/llm_client.py",
            "line": 233,
            "end": 253,
            "message": "重试都耗在 429 上时 last_exc 仍是空，最后抛出的原因是 None",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-fortran-agentic-success",
        "pr": "DisciplinedSoftware/Codebase-Conversion",
        "relation": "near",
        "gold": {
            "file": "fortran_to_rust/strategies/agentic.py",
            "line": 141,
            "end": 144,
            "message": "修复循环结束后一律 success=True，quick_check 全失败也报成功",
        },
        "finding": "fortran-138",
        "equivalent": False,
        "note": "行号相邻。金标说失败仍报成功，医院说异常被接住后没有再抛出或记录。",
    },
    {
        "gold_id": "rb-fortran-llm-first-success",
        "pr": "DisciplinedSoftware/Codebase-Conversion",
        "relation": "miss",
        "gold": {
            "file": "fortran_to_rust/strategies/llm_first.py",
            "line": 100,
            "end": 103,
            "message": "quick_check 每次都失败时仍把转换标成成功",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "rb-fortran-hybrid-nocheck",
        "pr": "DisciplinedSoftware/Codebase-Conversion",
        "relation": "miss",
        "gold": {
            "file": "fortran_to_rust/strategies/hybrid.py",
            "line": 97,
            "end": 100,
            "message": "混合策略不跑文档里写的编译检查就报成功",
        },
        "finding": None,
        "equivalent": False,
        "note": "同文件有一条未使用变量，说的不是编译检查被跳过。",
    },
    {
        "gold_id": "rb-protrider-auprc",
        "pr": "gagneurlab/PROTRIDER",
        "relation": "near",
        "gold": {
            "file": "src/protrider/model/model_helper.py",
            "line": 75,
            "end": 86,
            "message": "损失不是有限值时给 auc_prec_rec 赋了 nan，下面却 return auprc，auprc 没有定义",
        },
        "finding": "protrider-74",
        "equivalent": False,
        "note": "名字挨着。金标说的是返回了未定义的 auprc，医院说的是 auc_prec_rec 没用到。",
    },
    {
        "gold_id": "rb-protrider-kmax",
        "pr": "gagneurlab/PROTRIDER",
        "relation": "same",
        "gold": {
            "file": "src/protrider/model/model_helper.py",
            "line": 122,
            "end": 122,
            "message": "k_max 取了特征维数之后再也没有引用，本应拿它卡住搜索上界",
        },
        "finding": "protrider-121",
        "equivalent": True,
        "note": "两边都指出 k_max 赋值后没有再用。金标多写了本应做上界。行号差一行，不算行级命中。",
    },
    {
        "gold_id": "rb-protrider-binary-search",
        "pr": "gagneurlab/PROTRIDER",
        "relation": "miss",
        "gold": {
            "file": "src/protrider/model/model_helper.py",
            "line": 148,
            "end": 163,
            "message": "收缩区间之后没有 elif，同一次迭代又按 fR > fM 把区间挪了一次",
        },
        "finding": None,
        "equivalent": False,
        "note": "同文件的未使用变量没有描述这个分支错误。",
    },
    {
        "gold_id": "rb-protrider-checkpoint",
        "pr": "gagneurlab/PROTRIDER",
        "relation": "miss",
        "gold": {
            "file": "src/protrider/pipeline.py",
            "line": 539,
            "end": 540,
            "message": "checkpoint_path 总会落到默认文件，每次训练都写盘并在下次跳过重训",
        },
        "finding": None,
        "equivalent": False,
        "note": "这批医院发现里没有这条。",
    },
    {
        "gold_id": "",
        "pr": "cmik/apilix",
        "relation": "extra",
        "gold": None,
        "finding": "apilix-1083",
        "equivalent": False,
        "note": "另一处同样少了分隔符。裁判包里没有单独一条金标对着这一行。",
    },
    {
        "gold_id": "",
        "pr": "cmik/apilix",
        "relation": "extra",
        "gold": None,
        "finding": "apilix-1118",
        "equivalent": False,
        "note": "第三处同样少了分隔符。裁判包里没有单独一条金标对着这一行。",
    },
    {
        "gold_id": "",
        "pr": "gagneurlab/PROTRIDER",
        "relation": "extra",
        "gold": None,
        "finding": "protrider-70",
        "equivalent": False,
        "note": "presence_out 未使用。读过的金标没有把这件事当成缺陷。",
    },
    {
        "gold_id": "",
        "pr": "gagneurlab/PROTRIDER",
        "relation": "extra",
        "gold": None,
        "finding": "protrider-119",
        "equivalent": False,
        "note": "tol 未使用。读过的金标没有把这件事当成缺陷。",
    },
]


def load_sample() -> dict:
    """展开成 score_hits 能吃的金标、发现和标注。"""
    items = []
    gold = []
    findings = []
    mappings = []
    seen: set[str] = set()
    for row in _ROWS:
        finding = None
        if row["finding"]:
            finding = dict(_FINDINGS[row["finding"]])
            if row["finding"] not in seen:
                seen.add(row["finding"])
                findings.append(finding)
        item = {
            "gold_id": row["gold_id"],
            "pr": row["pr"],
            "relation": row["relation"],
            "gold": dict(row["gold"]) if row["gold"] else None,
            "finding": finding,
            "equivalent": row["equivalent"],
            "note": row["note"],
        }
        items.append(item)
        if not row["gold_id"]:
            continue
        located = dict(row["gold"])
        located["id"] = row["gold_id"]
        gold.append(located)
        mappings.append({"gold_id": row["gold_id"], "equivalent": row["equivalent"]})
    return {"items": items, "gold": gold, "findings": findings, "mappings": mappings}


def score_sample() -> dict:
    sample = load_sample()
    return score_hits(sample["gold"], sample["findings"], sample["mappings"])


def sample_report() -> str:
    """映射样本的评测报告。三档都写，不合成一个数字。"""
    sample = load_sample()
    score = score_hits(sample["gold"], sample["findings"], sample["mappings"])
    line_ids = [
        row["id"]
        for row in sample["gold"]
        if any(_overlaps(row, found) for found in sample["findings"])
    ]
    semantic_ids = [row["gold_id"] for row in sample["mappings"] if row["equivalent"]]
    extra = sum(1 for row in sample["items"] if row["relation"] == "extra")
    source = (
        f"材料是映射样本里 {len(sample['gold'])} 条已标注金标，不是全量重跑。"
        f"另有 {extra} 条医院发现没有金标，不进这三档。"
        "这份样本没有已读文件账。"
    )
    return render_eval_report(
        score,
        source=source,
        line_ids=line_ids,
        semantic_ids=semantic_ids,
        coverage=measure_coverage(None),
    )


def _overlaps(gold: dict, finding: dict) -> bool:
    end = finding["line"] if finding.get("end") in (None, "") else finding["end"]
    return gold["file"] == finding["file"] and gold["line"] <= end and finding["line"] <= gold["end"]
