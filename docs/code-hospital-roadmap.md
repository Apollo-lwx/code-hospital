# 代码医院 · 长期演进路线（2026-10）

> 状态：规划文档，用于逐条推进。上位设计见 [`code-hospital-design.md`](./code-hospital-design.md)，未决事项见 [`code-hospital-open-decisions.md`](./code-hospital-open-decisions.md)，内置名册见 [`code-hospital-builtin-catalog.md`](./code-hospital-builtin-catalog.md)。
> 本文冻结三件事：评测低分的根因、链路可信的边界、内置与外聘的分层。后续改动按阶段推进，每阶段有验收。

---

## 1. 这份路线从哪来

最近几轮对话把同一件事从不同侧面敲了出来：

1. **评测低分不是医院流程错。** ReviewBench 25 PR 里 352 条人审金标，尺子行号重合只有 6 条；SWRBench 功能问题落在改动行上的召回 0.118。金标要的是「读懂这次改动在做什么」，尺子答的是「哪条规则响了」。
2. **宿主读 diff 的交卷精确率 17/17，但召回仍低。** 不是读不准，是大部分 PR 没读完。诊断现在收到「尚未交卷」仍会进入已诊断，这扇门是开着的。
3. **全仓不能只读有发现的文件。** 那样会把调用方截断，行为或安全结论必须挂在已读完的调用链上；没读的一跳、对不上的名字、动态派发，都是未闭合，不能写成正常。
4. **医生、仪器、清单可以外聘。** `aigate-dgp` 已经把「SKILL 排班、清单审稿、MCP 当仪器」跑通。医院处理流程与规范化，极致交给外聘。

一句话：**能机械判定的进尺子，需要读懂的进宿主清单，更强的工具进外聘登记。医院守住流程、边界和病历。**

## 2. 三档能力分层

| 档位 | 例子 | 放哪 | 谁执行 |
|---|---|---|---|
| 内置尺子 | 行长、函数/类长度、分支、参数数、硬编码密钥、空 catch、`System.out.print`、`new Thread`、`Executors.*`、TODO、行尾空白、SQL 拼接 | `catalog` 内置引擎 | 医院代码（AST/正则） |
| 宿主清单 | 功能对不对、SOLID、并发安全、空安全、N+1、事务范围、框架分层 | `catalog.checks` 提示包（executor=llm） | 宿主 LLM 交卷 |
| 外聘仪器 | `java-code-review-mcp`、`ai-code-review-mcp`、`time-complexity`、`sonarqube`、SpotBugs、Semgrep 远程规则 | 被诊疗仓库 `code-hospital.yaml` 登记 | 宿主调用，医院收产物 |

分层纪律：

- 内置尺子只做语法能判定的，不追求框架语义。
- 宿主清单是「审什么」，不是代码。内置通用包，仓库可覆盖成自己的清单。
- 外聘仪器不进医院源码，不改写 SKILL 原文。没装、没交产物，覆盖账写「外聘仪器未到岗」，不写成没有问题。
- `aigate-dgp` 这类项目专属清单、阈值、SKILL，留在被诊疗仓库，不搬进 code-hospital。

## 3. 阶段划分

阶段之间不跳步。每阶段先把「挡住『正常』这句话」做实，再谈召回。

### E0：先把「未读完不能结案」挡住（当前）

**目标**：合入门诊没有宿主交卷，就不能进入已诊断；全仓出院不能写成「没有功能问题」。

- 合入门诊 `hosp_diagnose`：无 `diff.issues` 或已读清单未盖范围内改动文件时，状态停在 `scanned`，返回待交卷，不产出诊断书，不指向开方。
- `hosp_treat`：同一位置停住。
- 出院：病历里未闭合链非空时，`outcome` 不得为 `cured`/`improved` 的「没有功能问题」口径；可以出院，正文是未闭合清单。
- 交卷格式沿用现有 `文件:行: 说明`，或「没有」+「已读：…」。分轮交卷累加进同一份病历。

**验收**：选一个 5–15 个文件的合入门诊，不交卷时工具停住；交卷后病历里同时有尺子发现和宿主 `diff.issue`。全仓体检出院正文不再出现「没有功能问题」。

已落地：合入未交卷返回 `pending_question(diff_filing)`，病例停在已检验，自动档同样停住。全仓（`scope_type=repo`）出院正文不写「没有功能问题」；未闭合链还在时，功能结论停在未闭合清单。疗效仍只统计仪器发现。分轮交卷累加进同一份病历：本轮写对的先入库，下一轮只追加，相同问题不重复插入。

### E1：内置语言尺子（Python 先行）

**目标**：把 `ai-code-review-mcp` / `java-code-review-mcp` 里已在用的确定性规则，收成医院可调度、可配置的内置尺子。

- `python-review`（内置）：参数个数、AST 分支数、裸 except、可变默认参数、`eval/exec`、硬编码密钥。阈值走 `catalog.structure` 的 `parameters` / `branches`。函数行数仍由 `structure-ruler` 计，不在这把尺上再报。
- `java-review`（内置，先做正则层）：行长、空 catch、`System.out.print`、硬编码字面量、`new Thread(`、`Executors.*`、SQL 拼接。行长阈值走 `catalog.java.line_length`。方法行数仍由 `structure-ruler` 计。Spring/JBCT 不进尺子。同一场里若缺陷尺也在，空 catch 不再报第二遍。
- `ts-review`（内置，先做文本层）：行长（`catalog.ts.line_length`，默认 200）、函数长度（按 `function`/`=>` 估算，阈值用 `catalog.structure.function_lines`）、`any`、`console.log`、TODO/FIXME/HACK/XXX、行尾空白、硬编码字面量、`innerHTML` / `dangerouslySetInnerHTML`。覆盖账写明「TS 解析未建，函数长度按 function 和 => 估算」。同一场里若安全尺也在，`.ts`/`.tsx` 的 `innerHTML` 不再报第二遍。`.js` 的 `innerHTML` 仍由安全尺报。
- 三台仪器登记进 `instruments.yaml`，挂进对应号别。用户仓库装了更强工具，可在 `code-hospital.yaml` 登记为外聘仪器并优先使用。

**验收**：`hosp_scan` 在 Python 仓库产出 `python-review` 的 `Finding.tool`；`code-hospital.yaml` 能改阈值；覆盖账写清「估算」与「未建」。

已落地：`python-review`、`java-review`、`ts-review` 都已上台。`ts-review` 报行长、函数长度估算、`any`、`console.log`、待办标记、行尾空白、硬编码字面量、`innerHTML` 和 `dangerouslySetInnerHTML`。覆盖账写明 TS 解析未建，函数长度是估算。方法行数与 Python 函数行数仍由 `structure-ruler` 计。

### E2：宿主审查提示包

**目标**：把「功能/SOLID/并发/安全」这类需要读懂的清单，从 `java-code-review` 的 `get_checklist` 思想，转成医院 catalog 的提示包。

- 内置通用包：`java-checklist`、`python-checklist`、`ts-checklist`，executor 为 `llm`，内容为审查问题（不是代码）。
- `code-hospital.yaml` 可追加或替换这些包（现有 `catalog.checks.items` / `packs` / `pack_dirs`）。
- 合入门诊把「按这份清单读 diff 并交卷」写进交给宿主的说明。未勾的项算未闭合。

**验收**：合入门诊的 handoff 文本里出现清单项；宿主交卷能按清单逐条交 `diff.issue`；仓库能用自己的清单替换内置包。

已落地：合入门诊按文件后缀排上 `python-checklist`、`java-checklist`、`ts-checklist`。每项是交给宿主的问题。未逐项交卷时停在已检验，说明里列出未闭合的项。`catalog.checks.disable` 关掉内置项后，用 `items`、`packs` 或 `pack_dirs` 写上 `group: checklist` 即换成仓库自己的清单。清单里的问题行同样收成 `diff.issue`，发现记下对应的清单 id，清单项也能指回这些发现。

### E3：调用链闭合

**目标**：行为或安全结论只挂在已读完的调用链上。

- 扩展 `severe.py` 的 `_CallerIndex`：在现有「调用方」基础上补「被调用方」，从种子（入口 + diff 改到的函数 + 必须先处理的发现）走出闭包。
- 动态调用（`getattr`、注册表、反射）、对不上的名字、语法解析失败的文件，标未闭合。
- 覆盖账从固定的「跨函数没有追」改成这份病例的链清单：已闭合、未闭合、断点文件与原因。
- Python 以外语言先标「这条链未建」，不写成已经看过。

**验收**：全仓出院正文列出已闭合/未闭合链；未闭合非空时不得写「没有漏洞」。

已落地：静态调用从范围内的函数和必须先处理的发现往外走，出院小结列出已闭合和未闭合。`getattr`、对不上的名字、语法解析失败，以及 Python 以外的文件，标成未闭合。未闭合还在时，这一节写「漏洞结论先停在这里」。函数内污点仍只在单个函数里跟着赋值走。

### E4：外聘仪器登记

**目标**：`java-code-review-mcp`、`sonarqube`、`time-complexity` 这类进程外工具，医院只登记、收产物、记到岗/未到岗。

- `code-hospital.yaml` 的 `catalog.instruments` 增加 `engine: external` 的完整登记：命令、到岗检查、产物格式（SARIF/JSON/文本行）。
- 医院不代跑 MCP；宿主按号别或清单点名调用，把产物交回 `hosp_scan`/`hosp_diagnose` 病历。
- 覆盖账区分「外聘仪器未到岗」与「已到岗无命中」。

**验收**：在 `aigate-dgp` 这类已装外聘工具的仓库，医院病历能收进 `java-code-review` 的 SARIF/JSON 产物；未装时写清未到岗。

已落地：名册里的 external 进程和 stdio MCP 跑完后，JSON findings、SARIF、`文件:行: 说明` 收进病历，来源是 external。命令不在 PATH 上写「外聘仪器未到岗」。认得出产物但范围内没有命中，写「已到岗，范围内没有命中」。空白或认不出的输出仍是没有交产物，不能写成没问题。这一项用探针验证格式，没有在 `aigate-dgp` 仓库上实跑。

### E5：疗效与名册演进

**目标**：出院样本足够后，再让名册自己调权。

- 继续落 E1/E2 的疗效后验（Beta），但不用于决定开哪味药，只写进处方草稿说明。
- 出院样本达到约定阈值后，再讨论是否让疗效影响药柜排序或号别套餐。
- 评测只统计「已读文件盖满范围」的病例，分数旁写覆盖率；不重跑 SWRBench/ReviewBench 直到 E0–E2 落地。

**验收**：处方草稿里出现疗效后验说明；评测报告带覆盖率。

评测报告这一半已落地：分数旁写已读文件是否盖满范围。没有范围账时覆盖率不算。复发率只统计已读文件盖满范围的出院样本，没盖满的留在评测集里但不进分母。处方草稿写 Beta(1, 1) 后验：有成败记录时给出估计成功率，样本不到 8 次或已经到了，顺序都仍按名册。

## 4. 不做的事（本周期）

- 不新增 `hosp_*` 工具。
- 不把 `aigate-dgp` 的 SKILL、清单、阈值复制进 code-hospital。
- 不把 Spring/JBCT 语义写进内置尺子。
- 不在出院样本不足时用疗效后验决定开方。
- 不用更多 YAML 规则去追 ReviewBench 的 350 条逻辑金标。

## 5. 每阶段怎么改代码

通用落点，按阶段取用：

| 阶段 | 主要落点 | 备注 |
|---|---|---|
| E0 | `departments/diagnostician.py`、`departments/treat.py`、`departments/discharge.py` | 未交卷/未闭合时停住 |
| E1 | `catalog/engines.py`、`catalog/instruments.yaml`、`catalog/depth.py` | 内置尺子引擎 |
| E2 | `catalog/check_packs/`、`catalog/checks.py`、`catalog/outside.py` | 提示包与覆盖 |
| E3 | `catalog/severe.py`（`_CallerIndex`）、`catalog/coverage.py` | 闭包与链清单 |
| E4 | `catalog/outside.py`、`adapters/`（external 适配） | 外聘登记与产物归一 |
| E5 | `core/efficacy.py`、`departments/prescriber.py` | 后验说明，不改排序 |

## 6. 验收清单（每阶段结束打勾）

- [x] E0：合入未交卷停住；全仓出院无「没有功能问题」口径。
- [x] E1：Python/Java/TS 内置尺子产出带 `Finding.tool` 的发现；阈值可配置。
- [x] E2：宿主按内置提示包交卷；仓库可替换清单。
- [x] E3：全仓出院列出已闭合/未闭合链；未闭合非空时结论受限。
- [x] E4：外聘仪器产物进病历；未到岗写清。
- [x] E5：处方草稿带疗效后验；评测带覆盖率。

---

> 本路线冻结后，后续 PR/改动按阶段引用条目编号（如「按 E1-python-review」）。阶段内再拆小步，不跨阶段混做。
