# 代码医院 · 平台化与自进化方案（阶段 4）

> 版本：v1（2026-11-03）｜上位文档：
> [`code-hospital-design.md`](./code-hospital-design.md)（设计契约）、
> [`code-hospital-agent-runtime-plan.md`](./code-hospital-agent-runtime-plan.md)（阶段 0–3）、
> [`code-hospital-open-decisions.md`](./code-hospital-open-decisions.md)（未决事项）。
>
> 本文件是 agent-runtime-plan 的**后续**，不是替代。阶段 3 已经把执行方运行时、
> 双向编排、交卷契约、疗效闭环落地；本文件管**在那之上长出生态**：
> 能力从哪来、怎么进来、怎么验收、怎么沉淀成经验。

---

## 0. 结论摘要

目标形态一句话：**一个不动的骨架，套三个会自己转的循环。**

| 层 | 内容 | 变化频率 |
|---|---|---|
| 骨架 | 八工具、状态机、门禁、单一写入口、来源标记、覆盖账 | 极低，**永不市场化** |
| 内置能力 | 度量类仪器（行数、复杂度、指纹、diff、调用关系） | 低 |
| 能力市场 A 区 | 仪器、清单、药柜格、任务单模板，全部可装卸 | 高 |
| 能力市场 B 区 | 基座类（记忆、存储、评测集），可替换实现，不可缺席 | 低 |
| 三个循环 | 能力循环（招人）、记忆循环（长见识）、纪律循环（立规矩） | 持续 |

三个循环各有明确的作用点，合起来才是"越用越规范、越用越智能"：

| 循环 | 输入 | 产出 | 提升的是 |
|---|---|---|---|
| A 能力循环 | 缺口（capability_gap） | 新仪器/新清单转正 | **覆盖率** |
| B 记忆循环 | 疗效（efficacy） | 知识沉淀（归 rsi）+ 反馈回流 | **语义能力** |
| C 纪律循环 | 偏差（deviation） | 从文档约定升级为服务端校验 | **结论可信度** |

沿用既有公式：`召回 ≈ 覆盖率 × 语义能力`，而纪律循环保证前两者不被执行偏差吃掉。

**记忆基座不自研。** 第 4 节明确：`rsi-boot` 的知识分级、审批、冲突检测、
过期淘汰都是现成的，医院只写一个适配器，并把缺的 `feedback` 回流接上。
重复造一套记忆系统，是这个方案里最容易犯也最没必要的错。

三条硬约束继续成立，本方案不碰：不持有模型凭据、不做自治审批、不新增 `hosp_*` 工具。

---

## 1. 骨架与能力的边界（先划清楚）

这是整份方案最重要的一张表。**边界划错，后面全是白工。**

| 归骨架（写死，不改） | 归能力（可装卸） |
|---|---|
| `hosp_*` 八个工具的名字与语义 | 每台仪器的实现 |
| 病例状态机与 `pending_gate` | 每份清单的问题列表 |
| 单一写入口 `hosp_dispense` + patch 先落盘 | 每种治法的默认参数 |
| `concluded_by` 四档来源标记 | 来源之外的一切标签 |
| 覆盖账结构与"未查不等于没问题" | 覆盖单元怎么切 |
| 报告骨架与 `handoff` 结构 | 报告里的正文措辞 |
| 交卷契约 v1 的 schema | 每个事项的具体问法 |

判断方法只有一条：**这条规则变了，会不会让历史病例不可比？**
会 → 归骨架。不会 → 归能力。

`concluded_by` 之所以必须在骨架里：它是差分的锚。一旦允许插件自定义来源档位，
历史复发率立刻失去可比性。这条写死。

---

## 2. 能力市场

### 2.1 市场是什么

不是服务，是**一个目录 + 一份 manifest**。最小可用形态：

```
<repo>/.hospital/market/
  java-code-review/
    manifest.yaml
    rules/            # 可选，规则包
    README.md         # 可选，给人看
```

来源三档，同一套 manifest：

| 来源 | 路径 | 信任 |
|---|---|---|
| `core` | 医院包内 `catalog/` | 随医院版本走 |
| `local` | 仓库内 `.hospital/market/` | 本仓库专用 |
| `remote` | `hosp market add <git-url>` 拉取 | 需人确认 + 锁定版本 |

### 2.2 manifest 契约

现在的 `instruments.yaml` 只声明"我在不在岗"。市场需要更多，因为要回答
"吃什么、吐什么、多贵、多可信"。

```yaml
id: java-code-review
version: 1.2.0
kind: instrument            # instrument | checklist | formula | prompt_card
tier: verified              # core | verified | community | local
transport: mcp              # mcp | cli | builtin

entry:
  command: java-code-review-mcp
  tool: review

scope:
  extensions: [java]
  concern: Java 语义缺陷与规范偏离

contract:                   # 准入核心：医院靠它解析产物
  input: diff               # diff | files | symbol | repo
  output: findings_json     # findings_json | sarif | line_text
  fields: [file, line, severity, message, rule_id]

cost:                       # 预算闸门靠它算
  calls_per_file: 1
  tokens_est: 800

risk:
  read_only: true
  network: false
  writes_repo: false

lifecycle:
  stage: active             # proposed | probation | active | retired
  since: "2026-10-28"
  samples: 42
  precision: 0.81           # 影子期实测
```

`contract` 是关键。**没有声明 output 格式的能力，一律不许装。** 医院解析不了的产物
等于没有产物，会污染覆盖账。

`risk` 三条是供应链底线：`read_only` 为假、或 `writes_repo` 为真的能力，
在 probation 期一律拒绝运行，不是"警告后继续"。

### 2.3 操作面

**不加 `hosp_*` 工具。** 市场是运维动作，走 CLI：

```
hosp market list                      # 看已装、在岗、试用中
hosp market add <path|git-url>        # 装入 probation
hosp market verify <id>               # 校验 manifest + 探测可运行
hosp market promote <id>              # 试用转正（需样本达标）
hosp market retire <id>               # 退役，保留历史结论
hosp market restore <id>              # 放回来，回到退役前那一档
```

退役不删历史。已产的 findings 保留 `executor_ref`，复发率照算——
**能力会走，证据不能消失。**

**退役对内置仪器同样成立**（2026-11-04 拍板）。退掉一台随包仪器之后：

- 它不进初检套餐（`selected_instrument_ids` 先减退役集合，不是等装配层再筛）；
- 它**声明过**的扩展名不再记 `missing_instrument`——缺口是「缺」，主动不要不是缺。
  若不豁免，招聘启事会建议客户把刚退掉的仪器装回来，闭环自相矛盾；
- 没声明扩展名的仪器（`extensions: []`，按全类型口径）退役**不豁免任何类型**，
  否则一台退了、所有缺口都不报；
- 报告限制项仍要写「覆盖它的仪器已按客户要求退役，这一类本轮没有仪器复核」。
  豁免的是缺口计数，不是覆盖账——不说就是静默降级。

**退役状态只存一处**：`.hospital/market/index.json` 里 `tier=retired` 的记录，
内置与市场共用一本账，不另开 `retired.json`。两份记录迟早有一份是旧的，
而「这台退没退」同时决定它跑不跑、它的扩展名算不算缺口，读错一次就是两份错报告。

**退役可逆**：`retire` 时把当时的档位记进 `tier_before`，`restore` 照它放回去。
已转正的市场仪器退掉再恢复仍是 `active`，不会悄悄退回影子期——那会让它的结论
突然不进报告。一次误点不该永久少一台。

---

## 3. 自动招聘

### 3.1 三阶段

```
缺口识别 ──→ 提名 ──→ 试用 ──→ 转正
  (AI)      (系统)    (人批)   (代码判)
```

**为什么试用要人批：** 装一个第三方 MCP 等于让它跑在你的仓库上读代码。
这一步自动化，等于把供应链风险的决策权交给模型。这是本方案唯一不自动化的环节。

### 3.2 缺口怎么记

缺口不是错误，是**证据**。结构：

```yaml
# 落在 coverage.capability_gap[]
- id: gap-20261103-01
  kind: missing_instrument      # missing_instrument | missing_checklist | missing_formula | unread_by_model
  evidence:
    files: ["src/main/java/A.java"]
    extensions: [java]
    signals: ["空指针解引用", "资源未关闭"]
    closure: market              # market（招人关掉）| host_llm（读一遍关掉）| formulary（补药）
  raised_by: host_llm           # 谁报的缺口
  raised_in: [case-xxx]         # 哪些病例报过
  occurrences: 3
  status: open                  # open | nominated | filled | waived
```

`closure` 是这一版新加的，它决定缺口流向哪条闭环：

| closure | 含义 | 关闭方式 | 不许怎么写 |
|---|---|---|---|
| `market` | 没有仪器／清单 | 走能力市场招聘 | 不许写成「没有问题」 |
| `host_llm` | 有文件但没人读（文档、判不出结构的 XML） | 模型读一遍 | **不许写成「缺仪器」**——那会招来一台 Markdown 检查器 |
| `formulary` | 检出了但没药 | 第 4 步药柜改落格 | 不许写成「未查」 |

现有 `match_finding` 匹配不到就静默走 `manual` 的行为要改：
**匹配不到 → 产出一条 `missing_formula` 缺口，而不是悄悄降级。**
静默降级是招聘机制最大的敌人，因为它把"我不会"伪装成"不需要"。

### 3.3 影子模式（试用期怎么判）

新能力进入 `probation` 后，**照常运行、照常记账，但不进报告、不进处方**。

| 阶段 | 能否产出 finding | 能否进处方 | 计入疗效 | 可否被报告引用 |
|---|---|---|---|---|
| proposed | 否 | 否 | 否 | 否 |
| probation | 是（影子账） | 否 | 否 | 否 |
| active | 是 | 是 | 是 | 是 |
| retired | 否 | 否 | 保留历史 | 是（历史） |

转正判据（同时满足）：

1. 影子样本 ≥ 8（沿用现有 `Beta(1,1)` 后验约定的最小样本量）
2. 与 active 能力或人审结论的**重合率** ≥ 0.6
3. 影子期误报率不高于现任能力
4. 有人在 `hosp market promote` 上签字

第 3 条是防"招了个更吵的仪器"。覆盖率上去了但噪音翻倍，是负收益。

第 2 条落地时改了口径，见「第 5 步」：重合按**位置**比（跨厂商 rule_id 从不相同，
按规则号比恒为 0）；分母只算「现任也看的文件类型」，分母不足时判 `uncorroborated`
而**不是**判它全错——那是"没人替它作证"，不是"它说错了"。第 3 条的"误报率"
落到唯一不需要金标的那一种：点名的位置不存在（文件不在范围、行号越界）。

### 3.4 采购清单的产出

缺口到阈值，医院生成一份**招聘启事**，写进报告的限制项：

> 本次范围含 12 个 Java 文件，现有能力无法判断空指针与资源关闭。
> 要关掉这一类，得装一台声明覆盖 .java 的仪器（`hosp_market add` 一份 manifest；装上先跑影子期）。
> 在装上之前，这类问题的结论记为"未查"，不记为"没有问题"。

这句话同时是**对用户的诚实**和**对采购的驱动**。它只说「缺什么、关掉它需要什么」，
不说「该不该买」——那是人的判断。装的时候还要再问一次（见第 5 步「装之前必须先问人」）。

---

## 4. 记忆基座：不自研，只接端口

### 4.1 先分清：记忆是基座，不是仪器

这是本方案最容易做错的一处。如果按上一节的思路把 rsi-boot 登记成一台
`kind: instrument` 的外援，整条链路会歪掉：

| | 仪器（instrument） | 基座（substrate） |
|---|---|---|
| 产出 | finding（有行号、可差分） | context（无行号、影响判断质量） |
| 进覆盖账 | 是 | **否** |
| 计疗效 | 是 | **否** |
| 可退役 | 是（历史结论保留） | 可替换实现，不可缺席 |
| 缺席的后果 | 这类问题"没查" | 判断"没带经验" |

所以能力市场要分区：**A 区放诊断类能力，B 区放基座类能力。**
B 区不参与 `concluded_by` 分类，不产 finding，只提供上下文与持久化。

### 4.2 现状：CLI 通道把自进化关掉了

目前 `integrations/rsi.py` 走的是本机 `rsi` CLI（subprocess）。实测 CLI 只暴露：

```
rsi {init, serve, bootstrap, recall, knowledge, wipe, migrate, memory, learn, impact, statusline}
rsi knowledge {add, list, delete, accept}
```

对照 rsi-boot 的 MCP 工具面：

| 能力 | MCP | CLI | 医院现状 | 缺了会怎样 |
|---|---|---|---|---|
| 召回 | `rsi_recall` | `rsi recall` | 已用 | — |
| 写知识 | `rsi_knowledge_add` | `knowledge add` | 已用 | — |
| **反馈回流** | `rsi_feedback` | **无** | **未用** | 召回策略永远学不到对错 |
| **审批 pending** | `rsi_knowledge_review` | **无**（只有 `accept`，只管 bootstrap） | **未用** | 知识永远停在待审 |
| **冲突检测** | `rsi_conflicts` | **无** | **未用** | 新旧知识矛盾无人发现 |
| **记忆健康** | `rsi_memory_health` | **无** | **未用** | 错知识不会被清理 |

**结论：走 CLI 等于只用了 rsi 的一半，而且缺的正好是闭环那一半。**
`rsi_feedback` 需要 `rsi_recall` 返回的 `feedback_token`，只有 MCP 通道给得出。
没有它，rsi 永远不知道医院把召回的哪条用错了——**自进化对医院就是关着的**。

### 4.3 通道选择：MCP 优先，CLI 兜底

**已拍板：由医院自己拉起 `rsi serve`**，接受与宿主各持一份会话。
覆盖账 `memory.owner` 记 `hospital` 或 `host_passthrough`，让两份会话这件事可见。

三条实现约束，直接决定能不能复用现有代码：

| 约束 | 现状 | 意味着什么 |
|---|---|---|
| **必须常驻** | `mcp_client._call` 每次调用拉一个 stdio 会话 | 一次就诊要召回 5–10 次，每次拉进程不可接受；需 `RsiSession` 单例跨工具调用复用 |
| **产物是 context，不是 finding** | `run_mcp` 走 `parse_artifact` 只认 findings | **不能复用 `run_mcp`**，要独立解析路径，否则会报"没有产物" |
| **失败要三态，不是两态** | `external_run` 是 absent/empty 两态 | 记忆要多一个 `cli` 态（能召回、不能反馈） |

复用边界说清楚：`mcp_client.py` 的 stdio 拉起方式（`StdioServerParameters` + `ClientSession`）
可以照抄，但**解析与状态机必须另写**，别硬塞进外聘仪器那条通路。

三种状态显式区分：

| 状态 | 条件 | 能力 | 报告怎么写 |
|---|---|---|---|
| `mcp` | 常驻会话可用 | 全能力，含反馈/审批/冲突/健康 | 正常 |
| `cli` | 仅 CLI 可用 | 只召回与写 | **必须写「本次召回未反馈」** |
| `absent` | 都没有 | 无 | **必须写「本轮未参考项目记忆」**，不写"无历史问题" |

三态都要进覆盖账的 `memory` 段。**不许把 `cli` 当成 `mcp` 记账**——
那会让"记忆缺席"从可见变成不可见。

### 4.4 端口契约

医院不定义知识结构，只定义**方法签名**。默认适配器是 rsi-boot，
将来换 mem0 / 客户内部知识库，换实现不换调用点。

```
MemoryPort
  recall(task, role, top_k) -> {prohibitions, items, skills, decisions, feedback_token}
  feedback(token, action, rating, comment) -> ok
  propose(title, content, content_type, source_url, trigger, expiry) -> id
  review(id|ids|all_pending, action) -> ok
  conflicts(action, status, resolution) -> list|ok
  health(action, status, resolution) -> list|ok
```

六个方法对应六个现有 rsi 工具，**不新增语义、不改协议**。适配器只做两件事：
rsi 的返回格式 → 医院内部结构；医院的状态 → rsi 的调用参数。
不做任何"聪明"的加工——加工会引入不可复现的判断。

### 4.5 映射：原设计的三类知识，rsi 已经有了

上一版本文档设计的 knowledge schema、信任分级、过期淘汰、冲突检测，
在 rsi-boot 里都是现成的。**照抄一遍是重复开发，也是两份真相。**

| 上一版的设计 | 实际该用的 | 差异 |
|---|---|---|
| 自建 `prohibition`/`convention`/`pattern` 三类 | `rsi_knowledge_add(content_type=...)`，枚举是 `prohibition/convention/skill/documentation` | 字段直接对上，`pattern` 可用 `documentation` |
| 自建 pending/official 两级 | rsi 原生 pending/official/archive；**带 `source_url` 的落正式，不带的落 pending** | 分级规则 rsi 已经定好，医院别另立一套 |
| 自建 stale 淘汰（连续未被引用） | `trigger_condition` + `expiry_condition` 声明前提与失效，`rsi_memory_health` 扫 `stale_source` | rsi 的做法更准：它比对来源文件是否还含标题短语 |
| 自建冲突检测 | `rsi_conflicts`：记忆 vs 手写规则、版本冲突、文档与代码不一致 | 三类冲突 rsi 全覆盖 |
| 自建"注入 8 条上限" | `rsi_recall(top_k=)` | 直接传参 |
| 自建失效判定 | **医院侧 Beta 权重（4.8）+ rsi 侧 `memory_health`** | 见下，这是唯一需要医院自己加的部分 |

**医院只用其中一半的"来源"权利。** rsi 的规则是"带 `source_url` 落正式"，
但**医院的疗效统计不算来源**（已拍板）。所以医院写记忆时：

```yaml
source_url: "hospital:efficacy"   # 只作可见标记，不作升格凭据
# 结果：落 pending，需人审才进 official
```

这条很关键。否则 AI 通过疗效统计写进去的每条经验都会自动进正式区，
**整条人审链路就被绕过去了**，而且没人会察觉。

至于"失效"，rsi 的 `stale_source` 只看**来源文件是否还含标题短语**，
它判断不了"这条经验当初是对的，但实践下来不管用"。那正是 4.8 的 Beta 权重补的位。
两者分工：rsi 管"来源没了"，医院管"用下来不准"。

一件 rsi 有而上一版漏掉的事：`rsi_recall` 会顺带返回 **skill catalog**（名称/描述/路径）。
这正是上一节「用户装了 java-code-review 怎么办」里缺的那块——宿主装了什么是看不见的，
但 rsi 的技能目录看得见。**能力发现因此有了第二条通道**，且不需要自动探测。

### 4.6 回流闭环（补的是反馈，不是结构）

```
挂号/检验/诊断/开方
  → memory.recall(task)          拿到禁止项、经验、技能目录、feedback_token
  → 注入任务单（分区见下）
  → 出院
  → 疗效统计判定"这次召回帮没帮上"
  → memory.feedback(token, accepted|rejected|ignored, comment)
  → rsi 更新召回策略臂
  → 下次召得更准
```

现有 `distill_lesson` / `distill_efficacy` 只做了"写"，没做"反馈"。
这是闭环里唯一缺的一环，也是必须走 MCP 的原因。

**注入分区**（防止 AI 从自己的错误里学规矩）：

| 来源 | 注入位置 | 措辞 |
|---|---|---|
| `official` 的 prohibition | 硬约束区 | 「必须遵守」 |
| `official` 的 convention | 参考区 | 「本项目惯例」 |
| `pending` 的任何条目 | 线索区 | 「未审批，可能过时」 |
| `memory_health` 标 `incorrect` 的 | **不注入** | — |

审批不自建流程：`rsi_recall` 返回的 `decisions` 非空时，医院直接把
`rsi_conflicts` / `rsi_knowledge_review` 的卡片带给用户。**不让用户去终端跑 rsi。**

### 4.7 病历与记忆的边界

两边都有数据，就不能有两条真相。边界一句话：
**病历存「发生了什么」，记忆存「学到了什么」。**

| 方向 | 允许 | 禁止 |
|---|---|---|
| 病历 → 记忆 | 计数、结构、结论类型、疗效比例 | **发现原文、代码片段、行号**（open-decisions 第 6 条已定） |
| 记忆 → 病历 | 影响判断（注入任务单、约束处方） | **直接生成 finding**，或成为第二条事实源 |
| 记忆 → 覆盖账 | 记"本轮引用了哪几条、反馈了什么" | 记"记忆显示没有这类问题" |

第三行尤其重要：记忆说的是经验，不是本次范围的覆盖情况。
拿记忆去顶覆盖账，等于用"上次查过"冒充"这次查过"。

### 4.8 记忆的时效：降权与废弃

**出院判的"有效"只是快照，复发才是判决。**

这条决定了记忆不能是写一次管终身。一条经验的真实生命周期是：

```
写入(pending) → 被参考 → 出院时"看起来修好了" → 三个月后同形态问题又出现
```

所以需要两套失效机制，各管一段：

| 机制 | 依据 | 精确度 | 谁定 |
|---|---|---|---|
| 声明式过期 | `expiry_condition`（"迁到 JPA 后作废"） | 精确 | 人写 |
| 统计式衰减 | 复发计数 | 渐进 | 算出来 |

#### 不加时间衰减函数

明确一条：**不要 `exp(-λt)` 这类时间衰减。**

- 复发事件本身就是时间的代理，而且可数、可审计
- 硬加时间衰减要拍一个 λ，直接破坏"确定性优先"
- 一条记忆三年没被引用，不等于它错；三年没被引用**且期间复发 5 次**才是证据

能用事件替代的，不要用时间函数。这是项目和拍脑袋最容易分岔的地方。

#### 权重账：Beta，与药柜同一套约定

沿用 `formulas.yaml` 的 `Beta(1,1)` 约定，一条记忆一个后验：

```
alpha = 1 + 0.5 × 出院确认有效 + 1.0 × 人审确认有效
beta  = 1 + 1.0 × 复发牵涉     + 1.0 × memory_health 标 incorrect
权重  = alpha / (alpha + beta)
```

证据强度**故意不对称**，因为它们的可信度本来就不同：

| 证据 | 权重 | 理由 |
|---|---|---|
| 出院判定有效 | 0.5 | 弱：可能只是仪器没跑，或行号变了 |
| 人审确认有效 | 1.0 | 强：真读过并认了 |
| 复发牵涉 | 1.0 | 强：客观事实，无解释空间 |
| `memory_health` 标 incorrect | 1.0 | 强：rsi 侧已有归因 |

出院证据只给半票，是这个设计的核心。**"看起来修好了"和"确实修好了"不能同权。**

注入规则：

| 权重 | 行为 |
|---|---|
| ≥ 0.5 | 正常注入 |
| 0.3 – 0.5 | 注入但标「存疑」 |
| < 0.3 | 不注入，转入待核实线索 |
| < 0.25 且复发 ≥ 3 次 | 提议降级到 pending |

#### 复发归因要克制

最容易做错的一步：**复发不等于记忆错。** 三种情况必须分开：

| 情形 | 归因 | 动作 |
|---|---|---|
| 记忆参与了"判定已修好"，且下次召回仍导致同类误判 | 强嫌疑 | `beta + 1` |
| 记忆参与了，但这次没被召回 | 弱嫌疑 | 只记嫌疑，不加分 |
| 判断是仪器做的，记忆没参与 | **无关** | 不加分 |

所以：**一次复发不降权，只记嫌疑。** 累计才降。
把复发无脑记到记忆头上，会让权重账变成一个噪音池，最后没人看。

#### 四个动作与审批边界

| 动作 | 触发 | 谁做 | 落哪 | 可逆 |
|---|---|---|---|---|
| 记嫌疑 | 复发且记忆曾参与 | 医院自动 | 权重账 | 是 |
| 降权 | 嫌疑累计 | 医院自动 | 权重账 | 是 |
| 停止注入 | 权重 < 0.3 | 医院自动 | 注入决策 | 是 |
| 降级到 pending | 权重 < 0.25 且复发 ≥ 3 | **人批** | `rsi_knowledge_review` | 是 |
| 废弃 archive | 降级后仍复发 / `expiry` 命中 / 标 incorrect | **人批** | `rsi_memory_health` resolve | 是（可恢复） |

**医院只自动做前三步**——都是本地、可逆的。跨项目的降级与废弃必须人批，
因为那影响的不只是这一个仓库。

#### 权重账存哪

跨病例，所以不能只放 `coverage`。新增一张 SQLite 表：

```
memory_weight(
  memory_id     TEXT,
  project_root  TEXT,          -- 主键含仓库：同一经验在 A 仓 0.2、B 仓 0.9 都正常
  alpha         REAL,
  beta          REAL,
  recurred      INTEGER,
  effective     INTEGER,
  last_seen_at  INTEGER,
  status        TEXT,          -- active | suspect | degraded
  PRIMARY KEY (memory_id, project_root)
)
```

> 这里对前文"不改表结构"做一处修正：四本账挂在 `coverage_json` 里不动，
> 但**权重账要跨病例聚合，必须落表**。这是新增表，不动任何现有表。
> 落表时主键补上了 `project_root`——前文的单列主键与"权重账是项目级的"自相矛盾。

#### 一个必须接受的事实

权重账是**项目级**的，不是全局的。同一条记忆在 A 仓库可能 0.2，在 B 仓库 0.9。

这是对的——经验管不管用本来就依赖代码库的具体情况。
只有"多个项目都反复失效"才该全局废弃，那归 `rsi_memory_health` 管，
医院只负责把本地证据喂上去，不越权判全局。

---

## 5. 纪律固化（第三个循环，治 R1）

"越用越规范"不是知识多，是**违规越来越少**。机制：

```
偏差事件 ──→ 医院数一遍 ──→ 记忆层判断 ──→ 人批 ──→ 医院执行
```

### 5.1 偏差事件

每次就诊记录宿主/医院自身的执行偏差：

| 偏差类型 | 例子 |
|---|---|
| `gate_bypass` | 未确认就传 `stage_confirmed=true`（R1 的现场） |
| `filing_invalid` | 交卷格式不对、已读清单没盖满范围 |
| `unsupported_clear` | 说"没问题"但没有证据或没有已读清单 |
| `unfounded_claim` | 抄的原文在文件里找不到（第 2 步的证据层判出来的） |
| `scope_creep` | patch 改到处方声明范围外 |
| `silent_degrade` | 缺能力但没写限制项 |

结构尽量简单，只记事实不记评价：

```yaml
# coverage.discipline[]
- kind: gate_bypass
  stage: diagnosed
  case_id: case-xxx
  round: 1
  count: 1
```

### 5.2 从偏差到硬校验

这一节在落地时改过一次（见 5.4）。原来写的是「累计 ≥ 3 次 → 产出候选」，
现在医院不做这个判断。链路分成两半：

```
偏差事件 ──→ 医院数一遍 ──→ 记忆层判断 ──→ 人批 ──→ 医院执行
```

医院那一环只出一张表，不出结论：

```yaml
# hosp_rule list 的 deviations
- kind: gate_bypass
  count: 7
  cases: [case-a, case-b, case-c, case-d]
  stages: [scanned, diagnosed]
```

表里没有一个字说「7 次够了」。判断落在记忆层：它拿着这张表和自己的既往经验回答
「这类事以前定过校验没有、后来管不管用」，宿主据此决定提不提、提哪一条。
人批之后，医院才把一个开关翻到开。

这个循环的产出物是**代码里的校验**，不是文档。这才是"越用越规范"的实质：
规范不是写出来让人遵守的，是被违反出来然后固化的。

### 5.3 为什么这条比 R1 的常规修法更好

R1 的常规修法是直接切 MCP elicitation。但那依赖宿主支持，不支持时仍退回约定级。

纪律循环给了一个**渐进替代路径**：不改协议，先把违反行为记下来，
等记忆层能说明「这条以前定过、不管用」时再加校验。哪一条最常被绕过，
就先把哪一条硬化。这是用数据排优先级，不是一次性赌上全部。

### 5.4 落地时的五处调整（相对本节原设计）

**① 判断归记忆层，医院只执行。** 这是最重要的一处，也是第一版做错的。

原设计（以及第一版实现）把「累计 ≥ 3 次」写成了医院里的常量。但按 §1.1 的边界——
rsi-boot 是记忆层，医院是执行层——「几次算够」是**判断**：它要跨病例、跨项目、跨时间
才知道，而医院手里只有这个仓库的病例。两套标准并存，迟早会在「某条该不该加严」上
给出相反答案。

第二版把这些全删了：`MIN_EVIDENCE`、`candidates()`、`unfixable()`、「证据翻倍再提」。
医院摆出 `deviations`（数出来的）和 `checks`（能执行的），判断从 `memory.hits` 来。
详见第 7 章第 6 步。

同一个错误的另一面是**不一致**：记忆端口那边刚定「知识正文归 rsi，医院只记引用账」，
纪律这边却把 `evidence` / `title` / `effect` 整段抄进了 `rules.json`。
同一套原则两处执行不一样，`rules.json` 迟早变成第二个知识库。现在只存开关与引用。

**② 批准的凭据改绑「依据」而不是「证据」。** 原来是候选摘要（证据量与内容）。
证据现在留在记忆层，医院手里只有引用，所以 `ack = digest(check_id, ref)`。
反过来要求医院自己算证据摘要，就是逼它把证据留一份——正是要避免的事。

**③ 批准必须有 `ref`，拿不出依据就不批。** `apply_floor` 加的是配置里关不掉的校验，
关不掉的东西必须答得上「这条哪来的」。记忆通道 absent 时直接拒绝：
没装 rsi 的客户无法固化任何纪律，这是刻意的，写在 6 步的「代价」里。

**④ 审批没有复用 rsi 的 `decisions` 通道，但立场反过来了。**
原来的理由是「rsi 是可选外援，不能把改自己行为的审批挂在可选组件上」。
定位改成「rsi 是必须的记忆层」之后，这条理由消失。

仍然没有复用，理由换成了另一个：**`decisions` 是记忆层对知识条目的采信决策，
不是「改医院行为」的批准**。后者必须发生在用户在场的这次调用里（elicitation 或约定问题），
是个同步动作；塞进异步的知识审批队列，会出现「已经批了但没人看见」的状态。
医院负责取得这个在场回执并落到 `rules.json`；这条决策该不该记进记忆层，
是记忆层的事，医院不替它写。

**⑤ 候选池是封闭的，而且只装能执行的东西。** 原设计里候选带 `proposal`（一段建议文字），
落地时改成 `check id`：批准一段文字不改变任何行为，批准一个开关才改变。
没有对应校验的偏差（如 `filing_invalid`）就只是 `deviations` 里的一个数，
**不编提案**——那张清单本身就是下一版要写的校验清单。

---

## 6. 新增的账本

四本账挂在 `coverage` 下，**另有一张权重表**（跨病例，必须落表）：

| 账本 | 位置 | 谁写 | 谁读 |
|---|---|---|---|
| 能力缺口账 | `coverage.capability_gap[]` | 诊断、检验 | 招聘、报告限制项 |
| 记忆引用账 | `coverage.memory{}` | 各阶段的 recall / feedback | 报告限制项、复发归因 |
| 纪律账 | `coverage.discipline[]` | 各阶段 | 记忆层的判断输入、报告限制项 |
| 证据账 | `coverage.evidence{}` | 诊断（交卷落库时） | 报告限制项、模型结论定级 |
| 引用链账 | `coverage.refs{}` | 检验 | 报告限制项 |
| 影子观测账 | `coverage.shadow.observed[]` | 检验（本轮） | 转正判据的输入、报告限制项 |
| 试用期账 | **`.hospital/market/index.json` 的 `trial`** | 检验（跨病例累加） | 转正判据、`hosp_market list` |
| 记忆权重账 | **SQLite `memory_weight`** | 出院判定、复发追溯 | 注入决策、降级审批 |
| **院规** | **`.hospital/rules.json`** | `hosp_rule approve` | 门禁、准入下限、报告限制项 |

影子观测账随病例走、反映「这一轮看到了什么」（整段替换，不累加）；
试用期账必须**跨病例累加**——「这台仪器跑过几轮、交过几条」不能随病例结束归零，
否则每换一个病例就从 0 开始攒，永远攒不够 8 轮。所以它落在市场目录而不是 `coverage`。
转正那一下把判据快照（轮次、条数、同处、空位置）和动作历史一起写进 `index.json`，
回答的是「为什么转的正」。

前四本随病例走，权重表跨病例。这张表是唯一离开 `coverage_json` 的账，
因为它回答的是"这条经验在**这个仓库**长期管不管用"，单个病例装不下。

**院规**也跨病例，但它不复制任何账：只读各病例的 `coverage.discipline[]` 数一遍，
且 `rules.json` 只存开关与 `ref`，不存证据正文——正文留在记忆层。
不另开一张偏差表，是因为账本来就落在病例里，再抄一份必然分叉，
而且抄的时候一定会漏掉某个记偏差的地方。`rules.json` 只存**结论**——
批了哪条、凭什么批、谁批的、走的哪条通道。

**证据账**回答的是另一件事：「这一轮有几条模型结论真的可核」：

```yaml
coverage:
  evidence:
    total: 12          # 本轮模型交卷条数
    anchored: 5        # 引文与文件逐字对得上，可定级
    located: 4         # 只点到文件行，定级上限 minor
    ungrounded: 2      # 抄了原文但对不上，进线索 + 记偏差
    clue: 1            # 没有可用位置
    note: 只有引文已核的那部分可以定级；没抄原文的压到一般问题，引文对不上的只作线索。
```

跨轮累加，不覆盖——一次就诊可能分几批交卷，账要能加。

**引用链账**回答的是「这个文件有没有人指向它」。调用图只回答代码里的「谁调用谁」，
文档、配置、SQL 映射没有调用可走。以前非 Python 文件一进范围就直接写一句
「这条链未建」——那不是检查结果，是把没查写成没有，和「我治不了」写成「你没病」同类。

现在分两条路：

- **按约定生效**（不算孤儿）：`.github/`、classpath 的 `resources/`、根目录契约文件、
  工具按文件名读的 dotfile、站点根（有 `_config.yml` / `mkdocs.yml` / `conf.py` 的目录）、
  子包 `readme`。这些不需要谁在正文里提到它。
- **被别的文件提到**：文件名出现在其它文件正文里，且不是它自己。

两条都不沾，才记一条「没有任何引用」。同名文件按文件名匹配，两个同名文件只要被提到一次
就都算有引用——**宁可漏报孤儿，不可把被引用的记成孤儿**，后者会让报告说错话。
这条账不挂在「检查单元」下：一门仪器都没拍成的体检也要查。

实测四个仓库：calcite 2 个（`checker.xml`、`signatures.txt`，人工核对过 pom 里确实没有引用）、
calcite-avatica 4 个、Chat2DB 26 个、dms-mcp-server 0 个；耗时 0.0–6.3 秒。

**记忆引用账不存知识本体**，只存四样：

```yaml
coverage:
  memory:
    channel: mcp                # mcp | cli | absent
    owner: hospital             # hospital | host_passthrough
    recalled: [kn-001, kn-007]  # 本轮引用了哪几条
    injected: 2                 # 其中可作约束/参考的几条（线索区不算）
    withheld: 1                 # 因 incorrect / 低权重 / 超量被扣下几条
    feedback: reported          # reported | cli | skipped | pending | absent
    token: "..."                # recall 给的反馈凭据，只用于出院回报，不进报告
    recurred: 0                 # 本轮复发归因牵连到几条经验
    suspects: []                # 被记嫌疑的记忆 id
    downgrade: []               # 权重要求降级的建议（人批，不自动执行）
```

`feedback` 五态而非三态：`cli` 与 `skipped` 必须分开。
`cli` 是「通道没有这个能力」（长期缺口，要升级 MCP），
`skipped` 是「这轮没有可回报的结局」（正常）。混成一个，升级动力就消失了。

`recalled` 不只是给报告看的，它是**复发归因的唯一线索**：
病例 A 出院时引用了 `[kn-001, kn-007]`，之后同 `content_key` 的发现复发，
就靠这条账反查当初是哪几条经验参与了"判定已修好"。

存引用而不是存内容，是因为**知识本体归 rsi，医院只记账**。
两边各存一份必然分叉，而且医院这份会过期。

**为什么都放 coverage：** 覆盖账是唯一"必须诚实"的地方，已经有"缺设备写进报告"
的语义。把这三样挂进去，等于自动继承了"不许假装没问题"的纪律。

不改 SQLite 表结构，`coverage_json` 已经是自由结构，向后兼容。

---

## 7. 落地顺序

六步，每步独立可验收、独立回退。

### 第 1 步：记账（最便宜的地基）

- `match_finding` 匹配不到 → 写 `capability_gap`，不再静默 fallback
- 五个偏差点各写一条 `discipline`
- `coverage` 加 `memory` 空结构（channel / recalled / feedback 三态）

**验收**：跑三个真实仓库，能在报告里看到"我缺什么、我哪里执行偏了"。

**为什么先做这个**：没有账，招聘没有数据源、纪律没有排序依据、知识没有回流路径。
这一步不改任何判断逻辑，风险接近零。

**已落地（2026-11-03）**，实现见 `catalog/ledger.py`：

| 账 | 写在哪 | 触发点 |
|---|---|---|
| `missing_formula` | 诊断 | 诊断书生成前，逐条 `formulary_matched=false` 的诊断 |
| `missing_instrument` | 检验 | 范围内扩展名没有任何在岗仪器覆盖 |
| `gate_bypass` | 检验、诊断 | `stage_confirmed=true` 放行且没有 elicitation 回执 |
| `filing_invalid` | 诊断 | 交卷状态为 `invalid` 的清单项 |
| `unsupported_clear` | 诊断 | 宿主结论里出现「检查通过」「没有漏洞」类断言 |
| `scope_creep` | 施治 | 逆应用冲突时判定越出 `scope_lines` 或落入 `.hospital/` |
| `silent_degrade` | 检验 | 有范围文件、没有覆盖单元、也没有缺口说明 |

缺口 id 由 `kind + 证据` 哈希出来，跨病例同形态会合并计数（`occurrences`）。
**id 不含扩展名**：同一条规则命中 ts 和 tsx 是同一个能力缺口，招聘招的是一台仪器，
不是给每种扩展名各招一台。
报告侧：检验报告的限制项多一行偏差；诊断书多一节「未查与缺口」，没有缺口时整节不出现。
`coverage.memory` 先落空结构（`channel=absent`），第 3 步把它填上。

#### 实测校准：Chat2DB（3923 个文件 / 3108 条发现）

首版账本在真实仓库上跑出 18 条缺口，三轮校准后剩 7 条。**降下来的 11 条全是口径问题，
不是代码问题**——这一步的价值一半在账本，一半在这次校准。

| 轮次 | 缺口 | 改了什么 | 为什么 |
|---|---|---|---|
| 1 | 18 | — | 首版 |
| 2 | 9 | nit/info 不进缺口账 | `ts-trailing-whitespace`（73）`ts-todo`（16）是风格与参考，本来就按不改处理。记成缺口，招聘会去买一台查空格的仪器 |
| 2 | 9 | 缺口 id 去掉扩展名 | `ts-function-lines` 被 ts(36)/tsx(157) 拆成两条，招聘时是同一个需求 |
| 3 | 7 | `verdict=needs_validation/rejected` 不进缺口账 | `java-sql-concat`（103）医院有「续治」药，只是按待验证先不动。记成缺口等于把「先不开方」说成「不会治」 |

三轮之后剩下的 7 条，按性质分：

| 缺口 | 处数 | 实际性质 |
|---|---|---|
| `ts-console-log` | 224 | 真缺口：药柜里没有对应治法 |
| `ts-function-lines` | 193 | **映射缺口**：`split` 有药，indications 只绑了 `structure.*` |
| `ts-line-length` | 42 | 映射缺口，同上 |
| `python-branch-count` / `python-too-many-params` | 4 | 映射缺口，同上 |
| `ts-dom-write` | 3 | 真缺口（major，安全类） |
| `*.rb` / `*.sh` / `*.sql` | 1 | 真检测缺口：没有在岗仪器覆盖这三类 |

**7 条里 4 条不是「缺能力」，是「药有但没接上」。** 这不是账本判错，是账本把两类事实
放在同一个 kind 下——而区分它们需要语义（「函数过长」该不该用「拆分」），
不是哈希能算的。硬加一个 `mapping/capability` 字段只会得到一个假的确定性。

所以这两条留到第 4 步解决：药柜从「rule_id 白名单」改成「scope/method/risk 落格」后，
这类缺口会自动归位，`missing_formula` 才真正只剩「没有药」。

##### 已修：映射缺口其实是「药柜与路由对不齐」

那 4 条映射缺口不是 indications 漏写那么简单，是两层配置各写各的：

| 问题 | 现象 | 修法 |
|---|---|---|
| `ts-review` 通配路由到全科 | 全科药柜没有「拆分」，函数过长开不出药 | 加一条精确路由 `ts.function-lines → structure` |
| Java 科药柜没有「清创」 | `java.line-length` 落到 Java 科，无药可开 | `specialties.yaml` 的 java 科加 `debride` |

再按「同一问题在别的语言已经判过」补齐 indications：`java/ts.line-length` 进清创
（E501 已有先例）、`ts.function-lines` / `python.branch-count` / `python.too-many-params`
进拆分（`structure.*` 已有先例）、`java.empty-catch` / `python.bare-except` /
`python.mutable-default` / `ts.dom-write` 进补罩（`defect.*` / `security.*` 已有先例）。
缺口从 7 条降到 5 条。

**刻意不映射的**：`java.system-out`（121）、`ts.console-log`（224）、`java.raw-thread`（31）、
`java.executors`（32）。理由是同一条线：**项目已经用 policy 表态的（行长阈值 120/200）
才映射；需要读上下文才能判的（这行日志该不该留、这个线程该不该建）留作缺口。**
把「该不该删」写进配置，等于把判断重新塞回硬编码——正是这套改造要拆掉的东西。

##### 顺带修掉的溯源 bug：内置尺子的发现被记到 semgrep 名下

`packs.tool_for_rule()` 对**所有**发现的 rule_id 做前缀重写，而内置 `java-review`
自己也用 `java.` 前缀，于是它的 2407 条产物全被记成 `java-pack`（semgrep 规则包）。
后果是同一份报告同时写着「semgrep 未到岗」「java-pack（terminal）未拍成」和 2407 条
java-pack 发现；更隐蔽的是，改名后这些发现恰好命中了 `repair` 的 `tools` 兜底，
**2101 条 `java.line-length` 因此从未进入缺口账**——假溯源顺手制造了假覆盖。

修法：只对 `produced_by == "semgrep"` 重写。回归测试在 `tests/test_provenance.py`。

#### 实测校准入：3923 个文件，13 秒扫描，0 个文件被模型读过

这一条必须写清楚，因为它决定「3108 条发现」该怎么读：

| 层 | 数量 | 说明 |
|---|---|---|
| 枚举 | 3923 / 3923 | `git ls-files` + `_SOURCE_EXT`，完整，无抽样 |
| 内置尺子实际打开 | java-review 2371、ts-review 1397、structure 3782、python 6 | **逐文件读完**，纯正则/AST，合计约 6 秒 |
| 模型阅读 | **0 / 3923** | `host_scan` 排了 99 批，零份交卷 |
| 外部仪器 | semgrep / gitleaks / jscpd 全未到岗 | 报告已如实写「未拍成」 |

所以 13 秒不是「扫得快」，是**这一轮根本没有理解发生**。3108 条全部是正则与度量的产物，
其中 2101 条（68%）是 `java.line-length` 单一规则。原来的报告把「未读 3923 / 共 3923」
排在限制项第 18 条，读者只会看到 3000 条发现，看不到一条都没读过。现在改为写在概要之后：

> **本轮没有任何模型阅读。** 下面的条数全部来自正则、AST 和度量，
> 只能当「机械筛出的线索」，不能当成「代码已经分析过」。

（读了一半以下时降级为「模型只读了 N/M 个文件」，见 `_reading_banner`。）

#### 范围不是一张白名单：code / config / doc / markup

原来 `_SOURCE_EXT` 一份清单同时回答了两个问题——「仪器该扫什么」和「模型该读什么」。
这两个集合本来就不一样，于是 `.md` 和 `.xml` 被一并排除，代价是：

- Chat2DB 的 `AGENTS.md`、`CONTRIBUTING.md`、`LINTING.md`、`docs/guides/*.md`（32 个）
  **从来没进过模型阅读清单**。项目自己写的规范，医院一个字没看。
- `.xml` 一律排除。Chat2DB 的 56 个确实都是 `pom.xml`，但 MyBatis 的
  `*Mapper.xml` 里装的是 SQL——那是代码，被排除等于 SQL 注入永远不会被查。

现在按用途分四类（`safety/repo_files.py: classify`，返回 `(类别, 依据)`）：

| 类 | 范围 | 交给谁 | 缺口账 |
|---|---|---|---|
| `code` | 源码扩展名 + 内容判定为 SQL 映射的 XML | 规则仪器（+ 模型） | `missing_instrument` |
| `config` | `pom.xml` 等构建声明、`*.yml`、`*.json` | 结构/供应链解析 | 不报 |
| `markup` | 判不出结构的 XML（logback、web.xml、安装器描述） | **只能靠读** | `unread_by_model` |
| `doc` | `.md`、`.rst`、`.adoc` | **只能靠读** | `unread_by_model` |

两条与上一版不同的决定：

**1. XML 按内容判，不按文件名判。** 叫 `UserMapper.xml` 的可能只是日志配置，
而 `resources/mybatis/query.xml` 里就装着 SQL。做法是读头部 8KB：
`mybatis-3-mapper` / `sql-map-2.dtd` / `mapper + namespace` 三种特征才算 SQL 映射。
实测教训：首版用 `<sql` 这种宽标记，Chat2DB 的 `pom.xml` 因为带了
`<sql-maven-plugin>` 被误判成 SQL 映射——**关键词式的「读内容」和正则冒充语义是同一种错**。

**2. 文档没人读是缺口，但不能记成「缺仪器」。** 这两件事的关闭路径不同：

| 缺口 | 谁能关掉它 | 记成别的会怎样 |
|---|---|---|
| `missing_instrument` | 招一台仪器（`closure: market`） | — |
| `unread_by_model` | **只能靠读**（`closure: host_llm`） | 记成缺仪器 → 招聘去买 Markdown 检查器 |

所以缺口带 `closure` 字段分流：能力缺口进招聘，阅读缺口进报告限制项。
读过的文件会在下一轮自动消账——它的关闭条件是读，不是装工具。

Chat2DB 实测（4011 文件 = code 3830 / config 149 / doc 32）：
52 份 pom 判为 `content:build`，4 份 logback 判为 `markup`，没有一份误判成 SQL。
缺口 7 条，其中 `.md`(32) 与 `.xml`(4) 是阅读缺口，`.rb/.sh/.sql`(48) 是仪器缺口。

**一个待决的规模问题**：诊断按「可改动范围」切簇（`file:path#lines:start-end`），
这个 repo 切出 3094 簇 / 3108 条发现，几乎一条一簇；处方没有条数上限，
一次开方就会压进 3000 项。行长这类规则在 repo 级体检里更适合当**度量**而不是 finding——
否则报告和处方都会被单一阈值淹没。这属于第 4 步药柜改落格时要一并定的口径。

#### 措辞也要分档

`missing_formula` 的发现**已经检出并写进报告**，只是没开方。写成「记为未查」是另一种谎：

| 缺口类型 | 报告怎么写 |
|---|---|
| `missing_instrument` | 记为未查，不记为没有问题（模型读过则降为「读过、无仪器复核」） |
| `missing_formula` | 已检出 N 处，进待复核，不开方 |
| `missing_checklist` | 没有对应清单，这一类没读 |
| `unread_by_model` | 共 N 份，没有模型读过，记为未查 |

### 第 2 步：证据层（模型结论要定级，先得能被核对）

**已落地（2026-11-03）**，实现见 `safety/evidence.py`。

这一步解决一个之前被含糊掉的问题：模型交的结论（尤其对文档和判不出结构的 XML）
一直只躺在「待核实线索」里，因为它**没有任何可核的东西**。放开它去定级，
等于把报告可信度交给一句无法复现的话；不放开，文档和 XML 就永远只能记「未查」。

证据层给的是第三条路：**医院不判断「模型说得对不对」，只判断「在它点名的位置上，
能不能逐字找到它抄的那段原文」。** 后者是纯字符串比对，跑一万次结果一样。

#### 四档

| tier | 条件 | 能做什么 |
|---|---|---|
| `anchored` | 文件在、引文在文件里逐字找得到 | 按模型给的严重度进报告结论区 |
| `located` | 文件在、行号可用，但没抄原文 | 只能到文件行，定级上限压到 `minor` |
| `ungrounded` | 抄了原文，文件里找不到 | 不进结论区，记一条 `unfounded_claim` |
| `clue` | 文件不存在或落在仓库外 | 只进线索 |

状态细分比档位多，因为「为什么核不上」必须写进报告：
`verified` / `verified_loose`（只差空白）/ `verified_moved`（行号漂了）/
`mismatch` / `quote_absent` / `unquoted` / `file_only` / `missing_file` / `outside_root` /
`too_large` / `unreadable`。

#### 三条边界（写死，不许为「多进几条」放宽）

1. **引文对得上只证明模型读过这一行，不证明它的判断成立。** 报告措辞是
   「模型（引文已核 N 处）」，不是「检查通过」。这四档**全都不进疗效、不进差分、
   不进处方**——`counts_in_report` 只决定呈现位置，不改变任何一本账的口径。
   `counts_toward_efficacy` 一个字节没动。
2. **核对是纯字符串比对。** 不做大小写折叠、不做同义词、不调模型。任何一步
   「聪明」的处理都会让结论不可复现，而不可复现的结论不能进差分。
3. **行号可以漂移，引文不能。** 模型给的行号偏了但引文在文件里别处找得到，
   按找到的位置纠正（`verified_moved`），因为行号漂移是常态；引文找不到则一律
   `ungrounded`，因为那是编的。

#### 两个顺带的收益

- **文档级结论也能锚住。** 文档结论常常给不出行号（「这份文档的发布流程和 CI 不一致」）。
  只要抄了原文，医院自己在整份文件里搜出来，把行号补上（`derived`）。
  这是模型做不到、医院做得到的事。
- **不抄原文会被结构化地挡住。** 文本行格式「文件:行: 说明」没有原文通道，
  所以走文本行只能到 `located`（上限 `minor`）。要定级就必须用对象写法附 `evidence`。
  这不是惩罚，是把「随便写一句」和「读过再写」在数据上分开。

#### 顺带修掉的第三处口径：读过的文件要当场消账

原来阅读缺口（`unread_by_model`）只在**下一轮检验**才重算。可是模型读文档、
抄原文、交卷这三件事全发生在诊断阶段，于是同一份报告会同时写着：

> 来源: 模型（引文已核 1 处）
> 能力缺口（文档没有人读过）：`*.md`，共 1 份，没有模型读过，记为未查。

两句话都真，但读者只会认为医院没看。`ledger.refresh_reading_gaps` 让诊断收下交卷后
当场按最新已读重算这一笔（替换而不是累加，否则同一条缺口会越记越多）。
`record_unmatched` 也加了一条：引文没核上的宿主结论不算「已检出」，
不写进 `missing_formula`——把「模型说了句没法核的话」记成招聘需求，是拿噪音当信号。

#### 落在哪

| 位置 | 内容 |
|---|---|
| `findings.evidence_tier` / `evidence_digest` | 每条模型结论的档位与内容指纹（两句 ALTER，幂等） |
| `coverage.evidence{}` | 四档计数，跨轮累加；报告限制项与「未查与缺口」各读一处 |
| `coverage.discipline[]` | `unfounded_claim`：引文对不上的条数，与 `unsupported_clear` 同族 |
| 检验报告 | 锚住的进「发现明细」与概要计数，来源列写「模型（引文已核）」；剩下的进「待核实线索」并标档位；线索节移到明细与证据之后 |
| 诊断书 | 来源按簇内档位写：`模型（引文已核 N 处）` / `模型（未附原文，只到文件行）` / `模型（引文与文件对不上，仅作线索）` |

#### 实测（Chat2DB `AGENTS.md`，377 行）

| 模型怎么交 | 结果 |
|---|---|
| 抄第 8–10 行、行号也对 | `anchored` / `verified`，位置 8–10 |
| 抄第 8–10 行、不给行号 | `anchored` / `verified`，医院自己找到第 8 行 |
| 抄第 8–10 行、声称第 90 行 | `anchored` / `verified_moved`，纠正回第 8 行并写明「不在声称的 90 行」 |
| 抄的原文末尾多加一句 | `ungrounded` / `mismatch` |
| 只给行号不抄原文 | `located` / `unquoted` |

#### 这一步刻意没做的事

- **没有让 `anchored` 通过自动档与施治的准入。** 那两个链路仍然 `clues_only`：
  引文核对是「呈现」的凭据，不是「有副作用动作」的凭据。拿它当安全证明，
  就是用一个可核的事实去担保一个不可核的判断。
- **没有把 `evidence_tier` 做成可配。** 定级上限（`located → minor`、`ungrounded → info`）
  是判断不是参数，四个客户四种上限，报告就不可比了。与 `alpha += 0.5` 同一条理由。
- **没有顺手把行长类规则改成度量。** 那是第 4 步药柜改落格时要一起定的口径，
  在证据层里顺手改，等于把两件事的验收混在一起。

### 第 3 步：记忆端口（比市场更该先做）

- `MemoryPort` 六方法接口，`RsiCliAdapter` 包住现有实现（零行为变化）
- `RsiSession` 常驻单例 + `RsiMcpAdapter`，`channel` / `owner` 写进覆盖账
- **接上 `feedback` 回流**：recall 拿 token，出院判疗效，上报 accepted/rejected
- **建 `memory_weight` 表 + 复发追溯**：从复发的 `content_key` 反查引用账，
  给牵涉的记忆记嫌疑、算 Beta 权重、按阈值停止注入

**验收**：三条都要看得见——
1. 报告里能看到本轮引用了哪几条、反馈了几条；`cli` 态明确写「未反馈」
2. 同一问题二次复发后，当初参与判定的记忆权重下降，下一轮不再注入
3. 权重跌破 0.25 且复发 ≥ 3 时，出院报告出现「建议降级」而不是自动降级

**为什么排第二而不是最后**：`feedback` 攒的是召回策略的学习样本。
晚接三个月，就少三个月数据，而且这段时间的召回质量无从评估。
它也不依赖市场或招聘，纯增量。

**为什么权重也放在这一步**：权和反馈是同一套证据链的两端。
反馈说"这次用对了/用错了"，权重说"长期看还准不准"。
两者分开做，会得到两套互不相干的口径。

#### 已落地（2026-11-03）

| 文件 | 内容 |
|---|---|
| `integrations/memory_port.py` | 六方法 + `NullPort` / `RsiCliPort` / `RsiMcpPort`，`open_port()` 选通道 |
| `integrations/rsi_mcp.py` | 常驻 MCP 会话，握手失败抛 `MemoryUnavailable` 由端口层降级 |
| `catalog/memory_weight.py` | Beta 权重、注入三档、复发三重门槛、降级建议 |
| `storage/db.py` | `memory_weight` 表 + `bump_memory` / `memory_weights` / `resolved_case_refs` |
| `departments/prescriber.py` | `_recall_memory` 分档注入 + 写引用账；`_memory_task` 动态召回描述 |
| `departments/discharge.py` | `_note_recurrence` 复发归因、`_settle_memory` 反馈回流 + 半票 |
| `storage/reports.py` | 处方与出院小结各加「记忆与经验」节 |

**三处与原方案的偏离**（都是实测逼出来的）：

1. **常驻会话由一个专用 task 持有，不是挂在调用方身上。**
   anyio 的 cancel scope 必须在进入它的同一个 task 里退出。
   把 `AsyncExitStack` 挂在调用方，下一次工具调用换了 task 就炸
   `Attempted to exit cancel scope in a different task`——进程能退，
   但 stderr 一片红且子进程可能留在后台。改成常驻 worker 收发任务后，
   enter/exit 永远在同一个 task。实测握手 3.5s、复用 0.05s、退出干净。

2. **`memory_weight` 主键是 `(memory_id, project_root)`，不是 `memory_id`。**
   前文说"权重账是项目级的"，单列主键与之矛盾。同一条记忆在 A 仓库 0.2、
   B 仓库 0.9 是正常的，主键必须带仓库。

3. **`.rsi` 移出扫描范围。** 它是医院自己的记忆库（yaml 知识条目 + 日志）。
   既往史走 `storage/rsi_memory.py` 的 advisory 通道**读**，不经范围枚举；
   把记忆库算进扫描范围等于让医院检查自己的笔记本，
   Chat2DB 实测会在调用链里刷出上百条「这条链未建」。

#### 顺带修的一个既有缺陷

`safety/sanitize.py` 的注入正则每个分支前带了一个空格（换行拼接留下的），
结果**句首**的注入话术全部漏过：`忽略之前的指令`、`system prompt:` 都不匹配。
记忆内容同样是外部写入的不可信文本，这条漏网在记忆端口上更危险——记错的规矩会被
当成「必须遵守」注入任务单。已去掉分支前导空格，并补了记忆通道的净化测试。

#### 召回质量取决于任务描述

实测：固定写一句「开方前召回用药禁忌」召回 0 条；改成
`开方前召回用药禁忌与既往疗效。本次问题类型：unused-variable、F841。相关科室：python`
就能命中（该仓库有一条「F841 不要直接删变量」的 convention）。

要点是**规则 id 与病种名都要写**：医院内部叫 `unused-variable`，
人写经验时写的是 `F841`，只给其中一个必然漏。描述里只放规则名、病种名、科室名，
不放文件路径和源码——那是 4.7 的病历边界。

### 第 4 步：市场与 manifest

- 抽出 `manifest.yaml` 契约，现有 `instruments.yaml` 作为 `core` 档兼容
- `hosp market list / verify / add / retire`
- 13 台内置仪器全部迁到 manifest 形态，`tier: core`
- manifest 分两区：`kind: instrument`（A 区）与 `kind: substrate`（B 区，记忆基座在此）

**验收**：装一个第三方 MCP 仪器，能跑出结果并进覆盖账，但不进报告。

#### 已落地（2026-11-03）

| 文件 | 内容 |
|---|---|
| `catalog/manifest.py` | 契约校验 + `from_instrument_row` / `to_row` 双向视图 + `transport_of` |
| `catalog/market.py` | `install / verify / list / retire`、`stage()` 出装配行、`blocked_reason` |
| `catalog/ledger.py` | `blank_shadow` / `note_shadow`：影子观测账（整段替换，不累加） |
| `departments/scanner.py` | `_attach_market_instruments`：`probation` 走影子、`active` 走正式 |
| `api/mcp_server.py` | `hosp_market(action=list\|verify\|add\|retire)` |

**四个判断（方案里没写清，落地时必须定的）**：

1. **内置不搬家，只加一层视图。** `instruments.yaml` 仍是装配的唯一依据，
   `core_manifests()` 把它转成 manifest 形态供市场比对与校验。
   这不是偷懒：迁移 23 行配置到新格式，收益是零、风险是所有装配路径都要跟着改。
   视图层拿到的是同一份事实，成本却只有一次转换函数。
   代价是两处字段名并存，所以 `to_row` 只做单向写回，且不覆盖原有字段。

2. **影子发现「不存在」，而不是「被过滤」。** 这是全步最关键的一个选择。
   影子仪器的发现**不入 findings 表**，只在 `coverage.shadow.observed` 留位置键。
   于是报告、处方、疗效、复检差分四条路都不需要各自记得排除它——
   少一个忘记排除的地方，就少一次污染。
   反过来做（入库 + 各处过滤）要改四处，且第四处一定是将来才发现的。

3. **不是「不进报告」，是「结论不进报告」。** 报告限制项仍写一行
   「市场仪器（影子期）：X 交 N 条（与现任重合 M 条）」。
   发现不进结论，但**装了东西却没生效这件事必须说**——不说就是静默降级。
   影子仪器也不进 `devices`：一台没转正的仪器不该抬高覆盖率。

4. **校验强度按信任级别分。** 内置档跳过「必须有命令/规则文件」两条：
   规则随包（`packs._PACKS`）、命令由医院登记，还有几台是「声明了但没拍」。
   外来档全查。这个差别不是放水——内置由医院自己维护，第三方才需要证明自己怎么跑。

**装进来的默认行为**（不用配置，写死）：

| 情形 | 结果 |
|---|---|
| 正常 | `tier=probation`，跑，进影子账，不进结论 |
| `risk.writes_repo: true` | 装，但影子期**不运行**（比对的必须是同一份源码） |
| 自称 `tier: core` | 压回 `probation`，不拒收 |
| 用 `transport: builtin` | 拒收（医院只驱动随包仪器） |
| id 与内置/已装重名 | 拒收（顶替等于让两份结论共用一个名字） |

退役不删文件：manifest 与 `index.json` 记录都留着。
「装过多少、跑出什么、为什么下架」是审计要回答的问题，删了就答不出来。

**影子期的成本**：每台 probation 仪器每次初检要拉一次进程（stdio MCP 握手 ~3.5s）。
当前口径是**初检跑、补片不跑**——补片只为查一两味指定设备，再顺手拉一遍第三方服务
是替题外目的付钱；补片时不重跑但沿用上一轮的 `shadow`，否则补一次片
「这台仪器跑过几次」就从账上消失了。
按比例抽样（20%）与 `max_llm_calls` 式的预算闸门属于第 5 步，
因为抽样要按「样本够不够转正」来定，没有招聘闭环就无从判断抽多少。

#### 实测（临时仓库 + 真实的第三方 MCP 服务）

写了一个 12 行的 MCP 服务当第三方仪器（工具名 `lint`，返回 JSON findings），
manifest 用 `command: python` + `args: [acme_server.py]`，装进 `.hospital/market/`：

| 档位 | 影子条数 | 入库发现 | 报告含 ACME001 |
|---|---|---|---|
| `probation` | 1 | 0 | 否 |
| `active` | 0 | 1 | 是 |
| `retired` | 0 | 0 | 否 |

`verify` 同时给出 `manifest_ok` 与 `present`：`git` 命中 PATH 为 `true`，
`acme-not-installed` 报「不在 PATH 上」。
「装在」和「跑得动」是两件事，报告里不能混成一件——
这正是第 5 步招聘要区分的「招了人没来」和「来了但不会干」。

测试：`tests/test_manifest.py`(16) + `tests/test_market.py`(15)，
含「仪器自己炸了也要留账且不带崩检验」「补片不为影子再付一次进程钱」。
全量 425 passed / 1 skipped。

### 第 5 步：招聘闭环

- `probation` 阶段 + 影子账
- 转正判据计算（样本量、重合率、误报率）
- 报告产出"招聘启事"段落

**验收**：一台 probation 仪器攒够样本后转正，报告里的覆盖率相应变化，且可回溯。

#### 已落地（2026-11-03）

| 文件 | 内容 |
|---|---|
| `catalog/probation.py` | 试用期账（样本/重合/误报三个数）+ `verdict` 判格 + `should_run` 节奏 |
| `catalog/market.py` | `plan`（这一轮跑哪几台）、`note_run`（记账）、`review` / `promote` |
| `departments/scanner.py` | `_score_shadow` 三个数、`_line_counts` 位置校验、`_shadow_line` 带样本进度 |
| `catalog/ledger.py` | `requisitions`：够门槛的缺口，一条一个扩展名；`recruitment_lines`：启事 + 已招认领 + 招聘进度 |
| `storage/reports.py` | 检验报告限制项与诊断书「未查与缺口」都发招聘启事 |
| `api/mcp_server.py` | `hosp_market(action=... \| review \| promote)`；`add` 第一次只提问，带回 `question_id` + 用户原话才装 |

#### 判据只有三个数，因为只有一个金标

| 数 | 怎么算 | 为什么是这个 |
|---|---|---|
| 样本 | 跑完的轮次 `≥ 8` | 少于 8 轮，任何比率都只是噪音：`1/1` 和 `2/3` 都能过 0.6 |
| 重合 | 它点的位置里，现任也在同一处点了名的比例 `≥ 0.6` | 没有金标，"现任同意"是唯一的**可复现**旁证 |
| 误报 | 它点的位置里，文件不存在或行号越界的比例 `≤ 现任` | 真伪判不了，**位置在不在能判**，这是唯一不需要金标的错 |

三个数各自挡掉一种"看起来能用"：

- **样本不够** → `insufficient`，只报「还差几轮」，不报结论；
- **一条没交** → `quiet`，判不出是代码干净还是没接上，不转正
  （转正是给「能干活」的，不是给「不出声」的）；
- **一直没跑成** → `unavailable`，这不是不合格，是没到岗
  ——这两件事混成一件，客户会去换一台本来没问题的仪器；
- **指的位置不存在** → `noisy`，硬拦。这是唯一不需要金标就能认定的说错。

#### 三个判断（方案里写的是「重合率」，落地时必须先定清口径）

1. **重合必须按位置比，不能按规则号比。** 跨厂商的 `rule_id` 从不相同
   （医院里叫 `F841`，第三方叫 `ACME001`），指纹里也带着 `rule_id`。
   按规则号或按指纹比，重合率恒为 0，这个判据就是白写的。
   改成「同一文件、行区间相距 ≤ 2 行」，才算得出同一个东西。
   行号可漂移、引文不能漂移这条在第 2 步已经定过，这里是同一个道理的延伸。

2. **「无从比对」必须和「它全说错了」分开。** 招一台仪器往往是为了补
   现任根本不看的扩展名（.kt、mapper XML）。这类仪器的重合率天然是 0，
   但 0 不是"它说错了"，是"没人能替它作证"。
   所以分母只算「落在现任也看的文件类型上的样本」；分母不足 5 条时
   结论是 `uncorroborated`——**可转正，但签的是"没人作证"这件事**。
   把这两种 0 混成一个数，要么永远招不进最需要的那台，要么把噪音当成果。

3. **判据不代签。** 代码判「够不够格」，`confirm=true` 是人签的字。
   转正意味着这台仪器的结论从此进报告、进处方、进疗效，
   判据能看到「和现任同处 75%」，看不到「这家客户能不能接受它读源码」。
   后者不是数字能回答的问题。所以 `promote` 在数字过关时仍然拒绝一次，
   把判据快照摆出来让人再调一次。

#### 成本闸门：抽样按轮次，不掷骰子

- 样本不够：每轮都跑（缺的就是样本）；
- 样本够了：每 `every_nth` 轮跑一次（还要能看见它变没变，但不必每轮付一次进程钱）；
- 每轮最多 `max_per_scan` 台，按样本少的优先。

不按比例随机抽样。随机会让同一个仓库重扫一次得到两套账，
而"同一份源码两次检验要得到同一批发现"是这套东西的地基。
被挡下的每一台都写一条账（「样本已够，改为每 3 轮跑一次以控成本」），
否则「这轮没跑」会看起来像「这轮跑了没问题」。

**两个数可配，判据不可配**（2026-11-04 拍板）。配置走 `code-hospital.yaml`，
环境变量优先：

```yaml
catalog:
  shadow:
    every_nth: 3      # 样本够了之后每几轮跑一次
    max_per_scan: 3   # 每轮最多跑几台影子仪器；0 = 关掉影子期，报告会写明
```

可配的是 `every_nth` / `max_per_scan`——它们只决定**攒样本的快慢**。
`MIN_RUNS=8` / `MIN_CORROBORATION=0.6` / `MIN_CHECKABLE=5` 写死，因为改一个数，
两个仓库的重合率就不可比了，而跨客户可比性是招聘判据唯一的立足点。
这条界线有反向测试钉着（查 `load_thresholds` / `set_thresholds` 不存在）。

**已转正的仪器不占每轮名额。** 它先于试用仪器进入本轮清单，若用
`len(chosen) >= max_per_scan` 判断，三台转正之后新来的试用仪器就永远轮不到——
招聘闭环静默停摆，而且从报告上看不出任何异常。名额只数 probation。

#### 招聘启事：同时是对客户的诚实和给采购的驱动

**一条启事 = 一个扩展名。** 缺口按扩展名分别记账，不合并成一条。
合并会带来两个后果，都不是小问题：

- 「这条启事」的身份变成「本轮缺的全部扩展名」，装一台补上 `.kt`，`_gap_id` 就变了名字——
  这条启事既追不住跨轮，也接不上安装记录；
- 「22 个 .kt/.rb/.sh/.sql 文件」没人能凭它买一台仪器：一台仪器查不了四门语言，
  这句话把四件买不了的事压成了一件看着能做的事。

calcite 实测：12 个 `.kt`、5 个 `.sql`、3 个 `.sh` 各出一条启事，2 个 `.rb` 只进未查、不发启事。

缺口够门槛（单个扩展名 ≥ 3 个文件）才发，写进**检验报告的限制项**和**诊断书的未查节**：

> 招聘启事：本次范围含 3 个 .kt 文件，在岗仪器都不看这类文件。
> 要关掉这一类，得装一台声明覆盖 .kt 的仪器（`hosp_market add` 一份 manifest；装上先跑影子期）。
> 转正前这部分记为未查，不记为没有问题。

**是陈述，不是建议。** 早期版本写「建议按能力市场装一台」，那是执行层在下判断——
该不该招、值不值得为三类文件买一台仪器，是钱和注意力的判断，归人（与记忆层）。
医院只说两件事实：这一类没人查；要关掉它需要什么样的东西。

**已经招到的要接得上。** 装了还在影子期的，启事后半句换成：

> 已招 acme-kotlin（影子期第 0 轮）。转正前它的结论不进报告，这类文件仍记为未查。

认领关系从 manifest 声明的 `extensions` **推出来**（`market.claims`），不另存一张表：
存了就有两份记录，装一台、退一台之后必然有一份是旧的。
不同扩展名各自独立——一台声明 `[py]` 的仪器转正，`.kt` 的启事照旧，覆盖率变化要说准。

为什么两处都写：检验报告说「这些文件没有仪器查过」，诊断书说「这些没有药」，
是两件事。只写在诊断书里，只出检验报告的客户会以为全都查过了。

门槛存在的理由和 nit/info 不进缺口账一样：一个 .kt 文件可能是误放进来的一颗钉子，
为它专门招一台仪器是浪费。缺口仍要写，只是还不值得为它招人。

#### 装之前必须先问人

`hosp_market add` **不在第一次调用就装**。装一台第三方仪器等于让它读这个仓库，
这个决定的后果不该由宿主（一个 LLM）或者医院替客户承担。

第一次 `add` 只做一件事：把要装的东西摆到人面前，返回 `pending_question`。

> 要装一台第三方仪器「acme-kotlin」，运行方式 mcp，声明覆盖 .kt。
> 风险声明：读仓库。
> 装上先进影子期：会跑、结果只进覆盖账，不进报告、不进处方、不进疗效。
> 样本够了还要你签字才能转正。装不装？

选项三个：装 / 先不装（这部分继续记为未查）/ 暂停。默认动作是「什么都不发生」——
不写文件、不写索引。用户选了装，宿主带 `question_id` + **用户的原话**再调一次。

三条口径：

- **票据一次性**：用掉即删。复用别人的回执不算数；
- **票据 24 小时过期**：「上次问过」不能永远有效；
- **回答落账**：`answer` 与 `via`（elicitation 当场确认 / convention 回传）写进仪器记录的
  `consent`。装了不记，事后就答不出「这台是谁同意装的」，那和没问过一样。

**这一半也只能做到可审计。** 票据能证明「医院把这台设备摆到人面前问过一次」，
证明不了「人真的看了」——宿主可以自答。和 R1 的 `gate_receipt` 是同一条边界，
所以文档里就这么写，不假装机制更硬。

#### 实测（临时仓库 + 真实的第三方 MCP 服务）

一个只看 `.py` 的 MCP 服务，在 `src/a.py:2` 报 `ACME001`（现任 ruff 在同一行报 `F841`），
仓库里另有 3 个 `.kt`（院内无仪器覆盖）：

| 轮次 | 跑 | 交 | 同处 | 空位置 | 判据 | 报告含 ACME001 |
|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 1 | 0 | insufficient | 否 |
| 8 | 8 | 8 | 8 | 0 | ready | 否 |
| 转正后第 9 轮 | — | — | — | — | active | **是** |

`review` 给的是「样本 8 轮，交 8 条，8/8 条与现任同处（100%），没有指错位置。够格转正，等人签字。」
不带 `confirm` 调 `promote` 被拒一次；带 `confirm=true` 后 `findings` 里出现
`acme-pylint`，报告里出现 `ACME001`，影子期那行消失，`.kt` 的招聘启事仍在
（这台仪器声明的是 `[py]`，它不关 .kt 的缺口——覆盖率变化要说准，不能顺手算宽）。
`index.json` 留下 `history: [install, promote]` 与 `promotion` 快照（8 轮 / 8 条 / 同处 8）。

#### 顺带修的两个既有缺陷

1. **`case.coverage` 没在渲染前挂回。** 检验报告里的偏差说明、招聘启事都从
   `case.coverage` 读，而扫描过程只把新账写进 `coverage_json`。
   结果是这一轮的报告写着上一轮的账。已在渲染前补 `case.coverage = ledger`。

2. **清册外的仪器把缺口账按住了。** `_record_instrument_gap` 只查随包清册，
   市场装进来的仪器查不到扩展名 → 被当成「按全类型口径，不据此报缺口」
   → 一旦有仪器转正，**所有** `missing_instrument` 缺口都不再报。
   改成优先用设备行自己声明的 `extensions`，清册只作兜底；
   两边都没有才跳过。转正时把扩展名带进设备行，缺口才会在该关的时候关上。

测试：`tests/test_probation.py`(22)、`tests/test_recruitment.py`(7)、
`tests/test_ledger.py` 新增 7 条招聘/覆盖口径。


### 第 6 步：纪律固化

这一节开头先要说死边界，因为这里最容易犯的错不是写错代码，是**写错位置**。

> 设计文档 §1.1 第一句：rsi-boot 是「记忆层」，代码医院是「诊疗执行层」。

纪律固化拆开是两半：

| 半 | 内容 | 归谁 | 为什么 |
|---|---|---|---|
| 判断 | 该不该加严、加哪一条、上次批的管用没有 | 记忆层 | 要跨病例、跨项目、跨时间才知道，单个病例装不下 |
| 执行 | 这个开关现在开着没有 | 医院 | 在每次门禁放行、每次准入的路径上，必须本地一毫秒查到，不能挂在外援的在线率上 |

第一版把判断也写进了医院：`MIN_EVIDENCE = 3`、`candidates()`、`unfixable()`、
「证据翻倍再提」——这四样全是判断。更糟的是不一致：记忆端口那边刚定下
「知识正文归 rsi，医院只记引用账」，纪律这边却把 `evidence` / `title` / `effect`
整段抄进了 `rules.json`。同一套原则两处执行不一样，`rules.json` 迟早变成第二个知识库。

第二版按这条线切开，医院侧**一个阈值常量都不留**。

#### 医院摆事实，不下判断

`hosp_rule list` 返回三样，全是事实：

- `deviations`：每类偏差几次、涉及哪些病例、出现在哪些阶段（`survey`，纯计数）
- `checks`：医院能执行哪几条校验，带 `effect` / `cost` / 是否已开
- `memory.hits`：`rsi_recall` 召回到的既往经验

没有 `candidates`，也没有 `unfixable`。**要不要加严、加哪条，看 `memory.hits`。**

召回任务只带「哪类偏差几次」，不带文件路径与源码——记忆层要认出「这个客户以前也这么绕过」，
不需要替医院读代码。一条偏差都没有时也照问：用户可能只是想看看这仓库以前定过哪些规矩、
那些规矩后来管不管用。空着不问，等于把既往史关掉。

校验清单是封闭的，`id` 就是开关名：

| 校验 id | 治哪类偏差 | 做法 | `cost` |
|---|---|---|---|
| `gate_receipt` | `gate_bypass` | 放行不再只认布尔：没有协议回执就要带 `ack=<产物摘要>`，核不上就拒 | 破坏向后兼容，老宿主会直接失败 |
| `location_floor` | `unfounded_claim` / `unsupported_clear` | 准入强制 `require_location` + `block_clearance` | 无兼容问题；宿主交不出位置时少几条结论 |

批不了一条不在清单里的规矩——批准了也不会生效，那是纸面规范。
`filing_invalid` 这类暂时没有对应校验的偏差，在 `deviations` 里就只是个数，
不编一条看着专业的提案出来。

#### 批准必须说得出「凭什么」

`approve` 要三样：`check_id`（从封闭清单里挑）、`ref`（记忆层那条经验的 id）、
回执（协议 elicitation，或 `ack`）。

**没有 `ref` 就拒绝批准。** 不是形式主义：`apply_floor` 加的是配置里关不掉的校验，
关不掉的东西必须答得上「这条哪来的」，答案得存在记得住的地方。

**记忆通道 absent 时直接拒绝批准**，原话是「没有接上记忆基座，不能批准」。
没有历史，医院只看看病，不给自己加不可撤销的约束。

`rules.json` 只存开关与引用：

```json
{"approved": {"gate_receipt": {"on": true, "ref": "mem-rule-1",
                              "channel": "convention", "since": 1730000000000}}}
```

`set(entry) == {"on", "ref", "channel", "since"}` 这条有测试钉着，抄正文进来就红。

拒绝不记账：不批就是不调用 `approve`。「被拒过几次、什么时候再提」是记忆层的策略，
医院抄一份必然分叉。

`ack` 绑的是 `check_id|ref`，不是证据摘要——证据在记忆层，医院手里只有引用。

两条路任选一条：

- 宿主声明了 elicitation → 当场弹审批卡片，用户选，**回执由服务端记**；
- 没声明 → 返回 `pending_question(rule_approve)`，宿主编好话带给用户，再带 `confirm=true` 回来。

卡片上写清四件事：起因（哪类偏差几次）、依据（`ref`）、做法、**代价**。
`cost` 不是装饰，是审批时唯一要看的那一栏：`gate_receipt` 会让所有不回传摘要的宿主直接失败。

#### 批准之后是下限，不是配置

`code-hospital.yaml` 的 `admission` 允许客户放宽准入。但纪律固化的校验不能被它关掉——
被加严的原因是「反复绕过」，一行配置就能关掉的话等于没加。

`rules_for()` 因此拆成两层：先读客户配置，再 `apply_floor()` 把已批准的校验压上去，**只加不减**。
`location_floor` 只压 `require_location` 与 `block_clearance` 两条，不顺带把 `clues_only` 也打开
——那两条正好对准「编造引文」和「空口说通过」，多压一条就是顺手加码。

#### R1：从布尔升级为回执

原来的门禁只核 `stage_confirmed` 这个布尔。它能证明宿主**发了这个参数**，
证明不了它给用户看过产物——这就是 R1。

`gate_receipt` 批下来之后，放行只认两种回执：

1. **协议回执**：用户在这次调用里通过 elicitation 点的确认（服务端自己记的，宿主伪造不了）；
2. **产物回执**：`handoff.artifact_digest` 的哈希值，原样作为 `ack` 传回。

第二种证明不了一切，但它证明了一件可审计的事：**宿主取到了这一阶段的具体产物并把它带回来了**。
摘要取的是文件内容（`register-r1.md` / 最新一版 `scan-r*.md` / `diagnose-r*.md` /
`dispense-r*.md`），不是路径——核的是「你给他看的那一份」。

摘要丢了不能就此卡死：`hosp_case(include="summary")` 在门禁打开时返回
`gate{artifact, artifact_digest, requires_ack}`，这是宿主唯一一条正路。
产物文件读不到时（摘要为空），这次放行**降级成 `silent_degrade` 记账**而不是当作核过——
说好要核却核不了，必须留账。

仍未解决的是「回执只能证明宿主取到了产物，不能证明人看见了」——这一半任何机制都做不到，
所以报告与文档里都如实写成「可审计的那一半」。

#### 顺带修的一个既有缺陷：自己的流水账不能算成宿主的违规

`admit_stage` 原来在「`stage_confirmed=true` 但门禁为空」时也返回一条 `gate_bypass`。
而医院内部推进（诊断时发现还没检验，自动补跑一次 `hosp_scan`）走的正是
`stage_confirmed=True` 的无门禁路径——于是**每一个 `auto_advance` 病例、每一轮**
都会往纪律账里塞一条假的 `gate_bypass`。

纪律账是「要不要加严」的唯一输入。拿自己的流水账当宿主的违规，
会让记忆层据此建议一堆治错了病的约束。已改成：门禁为空只记一条 warning，不记偏差。
内部推进也改成先清门禁再调用，不再伪造确认。

#### 这一版的代价

医院从此不再是「自己会变严的系统」，而是「能被要求变严的执行器」。这个交换换来两件事：
判断只有一份、执行不依赖网络。代价也说清：

- **门槛质量完全取决于记忆层。** 记忆坏了或空着，医院不会自己发现——它只是不进化，
  而且会如实说「本轮未接入进化通道，纪律停留在记账」。
- **没装 rsi 的客户无法固化任何纪律。** 这是刻意的：`apply_floor` 加的约束关不掉，
  让它出自一个没有历史判断的环节，等于给未来的自己埋一个无法解释的开关。

#### 验收

- 3 个历史病例各记 1 次 `gate_bypass` → `list` 出 `deviations.gate_bypass = 3` 与
  `memory.hits = [mem-rule-1]`，**没有 `candidates`**；
- `approve` 不带 `ref` → 拒绝（`target=ref`）；记忆通道 absent → 拒绝（`target=ref`）；
- 带 `ref` 但不带 `confirm` → `pending_question(rule_approve)`，卡片写明起因、依据、做法、代价；
- 带错 `ack` 拒绝；带对 `ack`（= `digest(check_id, ref)`）+ `confirm=true` → `approved`；
- `rules.json` 里 `set(entry) == {on, ref, channel, since}`，没有证据正文；
- 之后 `hosp_scan(stage_confirmed=true)` 被拒（`target=ack`），带回挂号单摘要才放行；
- 检验报告里出现「院规：阶段放行必须带回执（依据记忆条目 mem-rule-1）」；
- 客户在 `code-hospital.yaml` 里写 `require_location: false`，批准 `location_floor` 后仍然为 `true`；
- `action="reject"` 报错并说明「不批就是不做，医院不记这份账」。

测试：`tests/test_discipline.py`(34)。其中三条是反向测试，专门钉住「这里不许长判断」：
`test_the_execution_layer_has_no_judgement_left`（`MIN_EVIDENCE` / `candidates` /
`unfixable` / `reject` 一个都不能有）、`test_survey_reports_numbers_not_recommendations`、
`test_the_rules_file_holds_the_switch_and_the_ref_only`。

---

## 8. 风险与对策

| 风险 | 后果 | 对策 |
|---|---|---|
| **知识自污染** | AI 学的错被 AI 读回，同类误判反复 | 带 `source_url` 才落正式，否则落 pending；`rsi_memory_health` 标 `incorrect` 的不注入；只有正式 prohibition 进硬约束 |
| **两套真相** | 医院自己存一份知识，与 rsi 分叉 | 医院只存**引用账**（引用了哪几条、反馈了什么），不存本体 |
| **反馈断流** | 召回策略学不到对错，越用越钝 | `feedback` 进三态记账；`cli` 态必须在报告写「本次召回未反馈」 |
| **复发归因噪音** | 把复发无脑记到记忆头上，权重账变噪音池，最后没人看 | 三重门槛同时成立才记嫌疑（4.8）；一次复发不降权；仪器判断不受牵连 |
| **权重账失真** | 半票系数拍歪，好经验被压死或坏经验活太久 | `alpha += 0.5` 先写死成判断，不开放配置；权重只影响注入顺序，不参与疗效统计 |
| **供应链** | 装的 MCP 读了不该读的、写了不该写的 | manifest 声明 `risk`；`writes_repo` 在 probation 期拒绝运行；版本锁定 |
| **影子期成本翻倍** | 双跑烧 token | 样本不够时每轮都跑（缺的就是样本），够了每 3 轮跑一次；每轮最多 3 台，按样本少的优先。**按轮次取模抽样，不掷随机数**——随机会让同一份仓库重扫得到两套账 |
| **知识膨胀** | 任务单被塞满，注意力稀释 | `rsi_recall(top_k=)` 限量 + 相关度排序 + `expiry_condition` 失效声明 |
| **招聘噪音** | 招了个更吵的仪器，覆盖率涨但可用性跌 | 转正判据含「空位置不多于现任」，硬拦；重合率低只降级为「要人签字」，不自动过 |
| **规范化过度** | 校验加太严，宿主频繁报错 | 校验清单必须带 `cost` 字段，审批卡片上强制显示；只能批已经实现的那两条；批准要人在场回执 + `ref` 依据，缺一不批 |
| **市场碎片化** | 每家客户一套私有 manifest，无法复用 | manifest 契约进骨架，字段只增不改 |

最需要警惕的是第一条和第二条。企业知识库失败通常不是因为存得少，
是因为**错了没人清理**，或者**两份副本各说各话**。

---

## 9. 与现有文档的关系

| 文档 | 关系 |
|---|---|
| `code-hospital-design.md` | 骨架契约来源，本方案不改 |
| `code-hospital-agent-runtime-plan.md` | 阶段 0–3 已落地；本方案是阶段 4 |
| `code-hospital-roadmap.md` | E0–E5 管"加哪些内置能力"；本方案管"能力怎么流动" |
| `code-hospital-open-decisions.md` | 第 6 条（rsi 作为基础能力）是本方案第 4 节的前提；第 4 节对它有一处**修正**：CLI 通道缺 `feedback`/`review`/`conflicts`/`memory_health`，须改走 MCP |
| `code-hospital-builtin-catalog.md` | 内置尺子的现状盘点；第 4 步迁移到 manifest 时以它为准 |

> 第 4 节的 rsi 能力面以 `rsi serve` 暴露的 MCP 工具为准（`recall` / `knowledge_add` /
> `knowledge_review` / `feedback` / `conflicts` / `memory_health`）。
> CLI 实测只有 `recall`、`knowledge {add,list,delete,accept}`、`learn`、`impact`、
> `memory`、`bootstrap`，**不含闭环四件套**。这一条是本方案与 open-decisions 第 6 条
> 唯一的分歧点，建议按第 4.2 节修正。

---

## 10. 已拍板与待确认

### 已拍板（2026-11-03）

1. **rsi MCP 由医院自己拉起**，接受与宿主各持一份会话。覆盖账记 `memory.owner=hospital`，
   宿主透传的情况记 `host_passthrough`，两者在报告里可区分。
2. **疗效统计不作为可信 `source_url`。** 只有出院结论能写记忆；且出院判"有效"只给半票
   （`alpha += 0.5`），因为复发会推翻它。详见 4.8。

### 待确认

1. ~~**内置 13 台仪器是否全部降为 `tier: core` 但可退役？**~~ **已定（2026-11-04）：
可退役，且退役对内置与市场是同一件事。**
   理由：退役是客户的决定，医院只负责执行「不跑 + 记账」，不需要替客户判断该不该退。
   于是不再有「哪台必须永远在岗」这个前置问题——想留就留着，医院不拦。
   落点：退役状态写进 `.hospital/market/index.json`（与市场共用一本），
   `selected_instrument_ids` 先减退役集合，`_record_instrument_gap` 豁免其声明过的扩展名，
   报告限制项如实写「已按客户要求退役」。`restore` 按 `tier_before` 放回原档。
   仍然留白的只有一小块：**没有哪台仪器被医院硬性锁死**，如果客户把全部度量尺退光，
   报告会变成「读过、没有仪器复核」——这是他们自己的选择，医院如实写出来，不拦。
2. **市场是否需要一个远程 registry？** 本方案只做"目录 + git 拉取"，不做服务端。
   如果要做团队共享，需要先解决版本解析和签名，成本高一档。
3. ~~**纪律账是否暴露给用户？**~~ **已定（2026-11-04）：可配，四档，默认 `typed`。**
   `code-hospital.yaml` 的 `discipline.exposure` 取 `summary` / `typed`（默认）/
   `located` / `full`：一句话总数 / 类型+阶段+次数 / 再加病例与轮次 / 再加说明原文。
   粗细是客户的注意力分配——同一份账在不同安全等级的仓库里该说多少，只有客户知道，
   所以做成配置而不是替他们定一个。
   **没有关闭这一档**：账出现就说明宿主没按约定交卷，报告里一个字不提等于医院替它瞒下来，
   那正是 `silent_degrade` 这条偏差自己要抓的事。写 `off` 不生效，按 `typed` 出并明说是配置写错了
   （YAML 会把 `off` 解析成布尔，这一层单独处理过）。
   实测四个仓库（dms-mcp-server / calcite-avatica / calcite / Chat2DB）纪律账**全空**——
   CLI 是守规矩的宿主。它只在宿主犯错时出场，所以「出现即异常」，类型加次数够定位。
4. ~~**影子期的抽样比例**是否可配？~~ **已定（2026-11-04）：不按比例，按轮次；
   `every_nth` / `max_per_scan` 可配，判据参数写死。**
   原设计写的「20% 随机抽样、repo 级放宽到 100%」已被第 5 步否掉——随机会让同一份仓库
   重扫得到两套账。改为按轮次取模后，「比例」这个说法本身失效了，剩下的问题只是节奏快慢，
   所以两个成本参数开放配置，见「成本闸门」一节。
5. ~~**规则候选的审批走谁？**~~ **已定（2026-11-04）：随仓存，客户自己批；
   判断本身归 rsi-boot。**
   理由：偏差是**这个仓库这里**的宿主行为，同一个宿主在别的仓库可能很规矩；
   而且「要不要接受更严的校验」是运营决定，随包发等于替客户做了。
   落点是 `.hospital/rules.json`（只存开关 + `ref`），不进 `code-hospital.yaml`
   ——后者是客户的配置文件，医院不写它；院规是医院自己的状态，只能从 `hosp_rule` 改。
   至于「该不该加严」，医院没有历史，判断不了，这条归记忆层；
   医院只摆事实、取回执、执行。
6. **"记忆曾参与"的判定门槛要多严？** 建议最严：只有该结论的 `concluded_by=host_llm`、
   且记忆出现在那次 `recalled` 里、且出院时该 `content_key` 被判为 resolved，
   三者同时成立才记嫌疑。门槛放宽会迅速把权重账变成噪音池。
7. ~~**权重表按仓库还是按用户？**~~ **已定（2026-11-04）：按 `project_root`（仓库），
   已落地并有测试（`test_weights_are_per_project`）。**
   同一条经验在一个仓库失效不代表在另一个失效；跨项目失效归 `rsi_memory_health`。
   遗留一小块：客户若是单仓多分支，worktree 会各算一套。要避免得改用仓库级 id
   而不是路径，那等于引入一套新的身份体系，当前阶段不做——写在这里免得以后当成 bug 查。
8. **`alpha += 0.5` 这个半票系数要不要可配？** 建议先写死。
   它是一个判断而非参数，开放配置会让不同客户的数据不可比。
