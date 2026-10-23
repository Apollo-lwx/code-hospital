# 代码医院 · 算法与建模设计

> 配套文档：[`code-hospital-design.md`](./code-hospital-design.md)（设计契约 v1.0-rc）、[`code-hospital-io-contracts.md`](./code-hospital-io-contracts.md)（IO 契约）
> 本文档定位：把设计契约里「一句话带过」的算法环节，落成**可实现的算法规格**——输入输出、数据结构、伪代码、复杂度、参数默认值、边界用例、验收方式，并标注与现有 P0 实现的差距。
> 当前代码基线：`src/code_hospital/`。算法核在 `code_hospital.core.algorithms`。挂号 / 检验 / 诊断 / 开方 / 成药施治 / 出院复诊与门禁 / 病例查询已接上双键指纹、诊断聚类、处方拓扑序、煎药 SEARCH 候选和出院差分。

---

## 0. 总览：九个算法环节

设计文档描述了「谁做什么」，但真正决定诊疗质量的，是下面九个可判定、可测、可调参的**算法核**。它们全部是**确定性算法**（除标注 LLM 的两处），是「主链路零 API Key」承诺的算法学基础。

| # | 算法环节 | 一句话问题 | 归口 | 优先级 | 现有实现 |
|---|---|---|---|---|---|
| A1 | Finding 指纹（FP） | 两次扫描的同一条问题，怎么认定是「同一个」 | 检验科 | P1 | ✅ 双键已接入检验 |
| A2 | 前后差分（findings diff） | 修完之后哪些好了、哪些还在、哪些是新添的 | 复诊 | P1 | ✅ 已接入出院复扫 |
| A3 | 医源性归因 | 新问题是不是我的改动造成的 | 复诊/门禁 | P1 | ✅ 已接入出院复扫 |
| A4 | 归因与剂型判定 | 这条问题该吃成药、煎药，还是转院 | 门诊 | P1 | ⚠️ 规则表，需数据驱动 |
| A5 | 诊断聚类 | 50 条 finding 怎样变成若干诊断簇 | 门诊 | P2 | 旧的 content_key 三级键已接入。名册要求的 `(rule_id, scope_key)` 尚未改代码 |
| A6 | 处方排序（拓扑） | 多味药为什么必须先清噪再修逻辑 | 药房前厅 | P2 | ✅ 拓扑序已接入开方 |
| A7 | 两步煎药协议 + 四道校验 + 3 次退避 | 模型写的补丁凭什么能落地 | 药房 | P2 | ✅ 取药令牌、提交校验与补验已接入；补验不改源码、不跑验收命令；冲突续行见 A8 |
| A8 | 漂移判定与最小逆应用回滚 | 越界了，怎么只撤越界的那部分 | 护理部 | P2 | ✅ 越界 hunk 已逆应用；冲突可 manual_resolved / retry / skip_item 续行 |
| A9 | 疗效判定与出院门禁决策树 | 能不能出院，四个维度怎么合成一个 verdict | 编排器 | P1 | ✅ 已接入 hosp_discharge |

另有三个**支撑模型**（不直接产出判定，但决定系统的可解释性）：§10 派生状态机与医疗不变量、§11 统计学习模型（处方-疗效先验、置信度校准）、§12 预算与规模估算。

---

## 1. A1 · Finding 指纹：从「哈希元组」到「分层匹配」

### 1.1 问题

指纹是整个系统的**主键**：差分、复发统计、门禁基线、调档全部建立在「两次扫描的 finding 是不是同一个」之上。哈希式指纹天生有两个失效模式：

1. **过度敏感**：修复在问题行上方插了一行 import，`context_anchor` 变了 → 同一条问题被算成「旧问题消失 + 新问题出现」，差分误报 `new_related`，直接触发假再入院。
2. **过度迟钝**：同一文件中同一规则命中两处完全相同代码（复制粘贴的 `except: pass`），两者指纹相同 → 「修好一个」会被判成「两个都好」，差分误报 `resolved`。

设计契约用 `context_anchor` + `possibly_resolved` 做二次匹配，但**没有给定二次匹配的算法**。下面补上。

### 1.2 分层匹配模型

把「同一发现」的关系定义成四级，逐级放宽，命中即停：

| 级别 | 名称 | 匹配条件 | 语义 | 产出状态 |
|---|---|---|---|---|
| L0 | exact | 5 元组哈希完全相等 | 确定性同一 | `unchanged` |
| L1 | fuzzy | `rule_id` 相等 ∧ `normalized_path` 相等 ∧ `symbol` 相等 ∧ 片段相似度 ≥ θ | 周围代码动了，问题本体没动 | `possibly_resolved` → 差分判定为 `resolved`（带 `match= fuzzy`） |
| L2 | displaced | `rule_id` 相等 ∧ `normalized_path` 相等 ∧ 片段相似度 ≥ θ | 问题在文件内挪了位置 | 同上，`match= displaced` |
| L3 | 不匹配 | 其余 | 旧问题真消失 + 新问题真出现 | `resolved` + `new_*` |

相似度用**归一化 Levenshtein 比**（`difflib.SequenceMatcher(None,a,b,autojunk=False).ratio()`，纯 Python；规模化后换 RapidFuzz 的 `ratio`，MIT）：

```
sim(a, b) = combined_similarity = w · snippet_sim + (1-w) · anchor_sim      # w = 0.5
snippet_sim = 1 - lev(canonical_snippet(a), canonical_snippet(b)) / max_len
```

**为什么必须掺内容锚（实测踩出来的）**：裸片段相似度对短行极不可靠——`except: pass` → `except TypeError: pass` 只有 **0.71**，落在灰区 `[0.70, 0.85)` 会被判成「疑似治好」而不是「已治好」；掺入内容锚（前后各 2 行归一化签名）后升到 **0.81** 以上，稳定过阈值。反之锚缺失（文件读不到）时自动退化为纯片段相似度，此时同形态改动**只会落灰区、不会硬判治好**——这是刻意的安全侧设计。

- **默认 θ = 0.85**，灰区下界 `θ_low = 0.70`；
- 片段长度 < 12 字符时**禁用 fuzzy**（短片段相似度不可靠），避免 `pass` / `pass  ` 这类误配；
- `symbol` 为空时 L1 退化为要求 `line_start` 距离 ≤ 3 行的就近匹配，L2 不启用。

### 1.3 复合指纹：解决「同文件同规则多处命中」

原指纹对 `(rule, path, symbol, snippet, anchor)` 全哈希，导致复制粘贴代码撞键。修法：在元组中加入**出现序号** `occurrence`，得到稳定的「身份指纹」与「内容指纹」双键：

```
content_key = sha256(rule_id | path | symbol | canonical_snippet)      # 问题的"长相"
identity_fp = sha256(content_key | anchor | occurrence)                # 问题在这份文件里的"身份"
```

- `occurrence` = 同一 `content_key` 在本次扫描全部 finding 中按 `(path, line_start)` 排序后的 0-based 序号；
- **差分与基线用 `identity_fp`**（区分 A/B 两处复制代码）；
- **复发统计与调档用 `content_key`**（「这类问题历史上出现过」与具体位置无关）；
- 现状：`finding.fingerprint` 字段继续存 `identity_fp`，新增派生字段 `content_key`（不改表结构，落 `findings` 的 `content_key` 列则加一个 `ALTER TABLE`）。

### 1.4 伪代码

```
function MATCH_FINDINGS(prev[], curr[], ctx) -> DiffResult
    # prev：基线快照（初诊 scan 或上一轮末态），curr：本次复扫
    index ← 多级索引(prev)
    used  ← ∅
    pairs ← ∅

    # Pass 1：exact（O(n) 哈希查找）
    for c in curr:
        if p ← index.identity[c.identity_fp] and p ∉ used:
            pairs += (p, c, "exact"); used += p

    # Pass 2：fuzzy / displaced（同 symbol 桶内，按行号邻近优先）
    for c in curr where c ∉ matched:
        for p in index.by_symbol_path[(c.rule_id, c.path, c.symbol)] where p ∉ used:
            if len(c.snippet) < 12: continue
            if sim(p.snippet, c.snippet) ≥ θ:
                level ← "fuzzy" if |p.line_start - c.line_start| ≤ 3 else "displaced"
                pairs += (p, c, level); used += p; break

    # Pass 3：剩余 → 一消失一新增；按 §3 归因
    resolved   ← prev ∖ used
    added      ← curr ∖ matched
    return DiffResult(pairs, resolved, added)
```

**复杂度**：Pass 1 是 `O(n+m)` 哈希查找；Pass 2 只在**未匹配项**上做，且按 `(rule_id, path, symbol)` 分桶，桶内规模通常 ≤ 5，最坏（同文件同符号 200 条同类问题）`O(k²·L²)`，`L` 为片段长度（≤ 200 字符截断）。实践中 Pass 2 的候选量 < n 的 10%。

**Pass 2 的一条硬规则（踩坑得出）**：桶内只要存在「形态完全一致」的未匹配候选，就**必须**选它，不得退化为相似度匹配到另一个形态。否则「第 40 行那份复制代码还在」会被错配成「它治好了」，差分直接误报。实现见 `algorithms_reference.match_findings` 的 `locked` 分支。

### 1.5 参数与边界

| 参数 | 默认 | 说明 |
|---|---|---|
| `fingerprint.match_threshold` θ | 0.85 | 阈值下调 → 更多 fuzzy 命中（漏报新问题）；上调 → 更多误判为新增。建议 0.80–0.90 |
| `fingerprint.min_snippet_len` | 12 | 低于此长度禁用 fuzzy |
| `fingerprint.max_line_drift` | 3 | L1 的就近行距上限 |

边界用例（写成单测）：

1. 同文件两处 `except: pass` → 修好第一处，第二处仍在 → 必须输出 `resolved 1 / unchanged 1`（用 `occurrence` 区分）；
2. 修复在上方插入 5 行 import → 必须输出 `unchanged`（L0 命中，因指纹不含行号）；
3. 修复重写了问题行且改了函数名 → 输出 `resolved + new_related`（正确：这是两个不同问题）；
4. 文件被格式化（全文件缩进变化）→ 全部 `unchanged`（`canonical_snippet` 已做空白归一化）；
5. Semgrep 与 ruff 同时报同一处代码问题 → 两者 `rule_id` 不同，**本就不应合并**（工具产出不可互证），指纹层不处理，交 §4 聚类处理；
6. **锚缺失**（文件读不到 / 二进制）→ 同形态改动只能落 `possibly_resolved`，**不得硬判 resolved**；
7. **形态完全相同但身份不同**（复制粘贴）→ Pass 2 永远优先选择「形态完全一致」的候选，不得退化为匹配到另一个形态。

> 前 7 条已在 `docs/algorithms_reference.py` 的 `_demo()` 里跑通（`python docs/algorithms_reference.py` 自检）。

### 1.6 与现有实现的差距

`core/fingerprint.py` 提供 `content_key` / `identity` 双键。`scanner` 在批量归一化后调用 `algorithms.stamp_findings` 补 `occurrence`。`match_findings` 已在 `core/algorithms.py`，复诊差分接入时直接调用。

> 纯函数在 `code_hospital.core.algorithms`。`python docs/algorithms_reference.py` 仍可跑 §1–§9 自检。

---

## 2. A2 · 前后差分与 new 细分

### 2.1 输出定义

```
DiffResult {
  resolved:        [Finding]   # 基线有、本次无（含 L1/L2 匹配成功，标 match 级别）
  unchanged:       [Finding]   # 两轮都在
  new_related:     [Finding]   # 本次新增 ∧ 医源性（§3）
  new_unrelated:   [Finding]   # 本次新增 ∧ 非医源性
  possibly_resolved: [Finding] # L1/L2 命中但片段相似度落在灰区 [θ_low, θ)
}
```

`possibly_resolved` 是设计契约里的关键缓冲带：**既不硬判 resolved（可能误判治好），也不判 new（会假报警）**，出院小结里单列一栏「疑似已解决，需人工确认」，`overrides` 可裁定。灰区默认 `[0.70, 0.85)`。

### 2.2 与状态字段的映射

| DiffResult 分区 | `findings.status` | `findings.origin` |
|---|---|---|
| resolved | `resolved` | 保持原值 |
| possibly_resolved | `possibly_resolved` | 保持原值 |
| unchanged | `unchanged` | `initial` |
| new_related | `new` | `new_related` |
| new_unrelated | `new` | `new_unrelated` |

差分结果**不覆盖历史记录**，而是为当前轮生成一批 `origin` 已标注的新 finding 行（`first_seen_round = 当前轮`），历史行仅改 `status` / `last_seen_round`。这样任意一轮的差分都可重放。

---

## 3. A3 · 医源性归因：新问题是不是我改出来的

这是整个「疗效判定」最难也最关键的一步。设计契约给的是**两条件的 OR**（文件在 dose 触及集合中 ∨ 符号区间相交），够用但不够精细——会导致「同文件不同函数的新问题」被判成医源性，误触发再入院。

### 3.1 三级归因模型

**改动足迹（change footprint）**：本轮全部 dose 的 patch 逆应用后得到的（文件 → 改动行区间集合），从 `.hospital/patches/*.diff` 的 hunk 头直接解析，无需额外建模：

```
footprint = { "src/a.py": [(12,18), (40,40)], "src/b.py": [(3,9)] }
```

对每条新 finding（文件 `f`，行区间 `[s, e]`，符号 `sym`）：

| 级别 | 判定 | 归类 | 理由 |
|---|---|---|---|
| R1 行内 | `f ∈ footprint` ∧ `[s,e] ∩ footprint[f] ≠ ∅` | `new_related` | 改动行本身引入了新问题，铁证 |
| R2 邻域 | `f ∈ footprint` ∧ `dist([s,e], footprint[f]) ≤ W` | `new_related` | 改动波及邻近行（如删了一个 `if` 块导致下方变量未定义） |
| R3 符号内 | `f ∈ footprint` ∧ `f.symbol ≠ ""` ∧ `f.symbol ∈ touched_symbols(本轮)` | `new_related` | 改的是同一个函数/方法 |
| R4 文件内 | `f ∈ footprint`，上述都不满足 | `new_unrelated`（advisory） | 只是同文件，归因不足 |
| R5 文件外 | `f ∉ footprint` | `new_unrelated` | 未触及 |

- **W 默认 5 行**（`attribution.neighbor_window`），可用配置调；
- `touched_symbols` 一期用**静态区间反查**：解析 patch 前文件，找包住 hunk 起始行的 `def`/`class`（用 `ast` 拿 `lineno/end_lineno`），不依赖 LSP；
- 二期外接 Serena 后，R3 可升级为 `find_referencing_symbols` 的**调用图传播**：改了 `parse()`，`parse()` 的所有调用点上出现的新问题也算 R3。

### 3.2 一个必须承认的局限

静态归因**无法证明因果**：改了 `a.py` 第 12 行，`b.py` 的测试挂了（行为耦合）——R5 会判 `new_unrelated`，但实际是医源性。缓解手段：

1. **验收项兜底**：虽然归因为 unrelated，但若**验收命令失败**，门禁的「验收项」hard check 会拦下出院（这是设计契约里两级瀑布之外的第二道保险）；
2. **报告显式声明**：出院小结中 `new_unrelated` 一律附注「本项归因为静态文件/行区间比对，跨文件行为耦合不在判定范围内」；
3. 二期可选：跑验收命令时记录失败测试与 patch 文件的关联（失败堆栈里出现的文件名 ∩ footprint），作为弱证据把 R5 升级为 R2。

---

## 4. A4 · 归因与剂型判定：从规则表到可学习打分

### 4.1 现状

`diagnostician._judge()` 是一张硬编码规则表（ruff + autofix 前缀白名单 → autofix；semgrep → llm_patch；其余 manual），返回 `(fixability, confidence)` 常量。问题：

- `confidence` 是**拍出来的常量**（0.86 / 0.72 / 0.58 / 0.4），不反映真实成功率，Q3 的 `confidence < 0.5` 阈值失去意义；
- 新增规则/新增设备必须改代码；
- 无法利用「这味药在某文件历史上失败过 3 次」这类病史（设计 §4.8 的核心卖点）。

### 4.2 打分模型（线性可解释版，P1 落地）

对每个诊断簇 `d`，按三个剂型各算一个**预期收益**，取代「if-else 定剂型」：

```
score(d, m) = w_rule[m, rule_id]
            + w_sev[m, severity]
            + w_hist[m] · history_success_rate(rule_id, path, m)
            + w_prior[m]                                       # 全局先验（Laplace 平滑的成功率）
            - w_cost[m] · cost(m)                              # autofix≈0.05  llm_patch≈0.6  manual≈1.0
            - w_risk[m] · risk_penalty(d)
```

- `w_*` 初值用**人工先验**（见下表），随病历累积改为从 §11.1 的处方-疗效统计里在线更新（简单在线梯度/指数加权即可，不必上 LR 训练管线）；
- 决策：`m* = argmax score(d, m)`，但**硬约束优先于打分**——高危规则、scope 过宽、manifest 改动一律落 `manual`（安全侧不可被分数推翻）；
- `confidence(d) = softmax(score)[m*]`，即**最大剂型的相对优势**，这才是「我有多确定这样治」的合理度量；三剂型打平时 softmax 趋近 1/3，自然落在 Q3 的复核区间。

**先验参数表（初版手填，标注为可调）**：

| 因子 | autofix | llm_patch | manual |
|---|---|---|---|
| `w_prior`（全局成功率先验） | 0.95 | 0.55 | —（不产出修复，固定 0） |
| `w_sev[critical]` | — （critical 禁 autofix） | +0.05 | +0.30（critical 倾向人工） |
| `cost` | 0.05 | 0.6 | 1.0 |
| `risk_penalty`（高危 scope / manifest） | 5.0（直接压死） | 0.3 | 0 |

### 4.3 为什么不用分类模型

考虑过用 GBDT 直接学「finding → 该选哪种剂型」。结论：**一期不做**。原因：

1. 冷启动无标签（一个仓库前 50 个病例的样本量撑不起多分类）；
2. 不可解释——医生（用户）要能在处方上看到「为什么选煎药」，线性打分能逐项列出来；
3. 特征工程成本远高于收益：真正的区分信号就是 `rule_id` + 病史成功率，线性模型已能吃满。

P3 可选：样本量 > 500 且带疗效标签后，用 LightGBM 学残差，仅作**建议**（不直接改处方，作为 `risk_level` 之外的 advisory 字段）。

---

## 5. A5 · 诊断聚类：50 条 finding → 8 张诊断书

### 5.1 现状与问题

诊断的 `group_findings` 已经按名册改成 `(rule_id, scope_key)`。`content_key` 只留在复发调档。本节以下是这套键的定义。

### 5.2 聚类键

簇的主键只有一个：

```
cluster_key = (rule_id, scope_key)
```

`scope_key` 的四种写法以名册 §4.1.1 为准（符号、行区间、`#file`、契约规则）。一个簇只有一个 `scope_key`，所以总能落进一个病症。

`content_key` 退出分簇，只留在复发调档（A1）。同一规则、同一长相、`method=autofix` 的多个簇，可以在开方时收成一批执行，再按下面的 `cluster_max_files` 切开。诊断书上它们仍是多簇。`method` 不一致的发现禁止合成一批，避免成药和煎药被装进同一个范围。

病症收拢在分簇之后做，规则也在名册 §4.1.1：函数级行区间可以包住其中的无符号发现；分层尺的病症按契约聚合，处方项仍按文件切块。

### 5.3 处方粒度上限

簇合并或成药批次仍可能碰到很多文件。硬约束不变：

- `prescription.cluster_max_files = 5`：超过则按文件切分（同簇或同一成药批次拆成多味，`seq` 相邻）。`contract:` 病症也按这个上限切处方项，不能因为病症是一条契约就开一味药动完全部违例文件。
- `prescription.cluster_max_hunks = 20`：超过则按位置二分（保持区间连续，便于 LIFO 回滚）。
- 任一味药 `scope_paths > 10` → 自动升 `high` 风险（与设计契约 §4.6 一致）。
- 成药项必须带 `scope_lines`。执行时只保留这些行里的 diff，见名册 §4.3。跨文件的 `E501` 不再靠一次整文件 `ruff --fix` 收成一簇。

### 5.4 复杂度

聚类是哈希分组，`O(n)`，n 为 finding 数（实际 < 2000）。切分是 `O(k log k)`，k 为簇成员数。无性能风险。

---

## 6. A6 · 处方排序：带约束的拓扑序

设计契约只说「成药先行 → 煎药随后 → 外购只记录，同文件多味药强制串行」。这是三条互相冲突的约束的粗暴叠加，落地会踩两个坑：

1. **成药改变行号**：成药若对整文件 `ruff --fix`，后面煎药项的行号和锚点都会漂。名册要求成药只提交 `scope_lines` 内的 diff，区间外回滚。煎药备料时仍要重读文件（A7），不能沿用开方时的旧行号。
2. **同文件串行 + 全局成药先行** 会死锁：文件 A 的成药要排在文件 A 的煎药之前，同时文件 B 的成药要排在文件 B 的煎药之前——两个"全局成药先行"约束叠加时若实现为全局计数排序，A 的煎药可能被 B 的成药挡在中间，白白浪费一次备料。

### 6.1 建模为 DAG + 拓扑排序

**节点**：处方项 `item`。**边**（`u → v` 表示 u 必须先于 v）：

| 边类型 | 条件 | 理由 |
|---|---|---|
| E1 文件串行 | `u.file ∩ v.file ≠ ∅` ∧ `u.seq_hint < v.seq_hint` | 同文件多味药不能并行改，LIFO 回滚也要求顺序确定 |
| E2 剂型序 | `method(u)=autofix` ∧ `method(v)=llm_patch` ∧ 同文件 | 先清噪再修逻辑，避免 noise 干扰锚点 |
| E3 依赖序 | `v.instructions` 引用了 `u` 改动的符号（可检测：u 的 target_symbol 出现在 v 的 scope_symbols 中） | 被改符号的修复要先去重命名/清理，否则锚点会漂 |
| E4 手工序 | 用户在改签时显式指定 `after_item_id` | 人审干预优先 |

排序 = **拓扑排序，同层按 `(method_rank, severity_rank, file, line)` 稳定排序**（Kahn 算法 + 优先队列）。若检测到环（E1/E2/E3 组合理论上可能成环），降级策略：**丢弃 E3**，只保留 E1/E2（二者天然无环，因为都是"同文件内按 seq_hint 单向"），并在处方报告里标注「存在潜在顺序冲突，已降级为按文件分组执行」。

**复杂度**：`O(V + E log V)`，V ≤ 数百，无压力。

### 6.2 一个反直觉的结论（值得写进设计文档）

「成药先行」**不应该**是全局优先级，而是**同文件局部优先级**。跨文件时按「scope 小 → 大」「风险低 → 高」排更合理——先把小范围低风险项做完，用户能早一步看到 diff、早一步发现方向不对，符合「宁可等，不可猜」的产品原则。§12 的 `hosp_treat` 也受益：前面的项早早 pass，进度条才有东西可滚。

---

## 7. A7 · 两步煎药协议：锚点生成、四道校验、失败退避

这是设计中**工程价值最高**的一段，也是最容易写成"能跑但很脆"的一段。

### 7.1 SEARCH 候选生成算法（药师备料）

目标：给定 `(target_file, instructions, 诊断归属的行区间)`，产出 1–3 个**已验证唯一匹配**的 SEARCH 块。

```
function GEN_SEARCH_CANDIDATES(file_lines, focus_line) -> candidates
    L ← file_lines
    for w in [0, 1, 2, 4, 8]:                        # 窗口扩张序：从精确行开始
        s ← max(0, focus_line - w); e ← min(len(L), focus_line + w + 1)
        block ← L[s:e]
        if block 全为空行: continue
        # 唯一性检查：block 在全文中的出现次数（连续行序列匹配，忽略尾随空白）
        if count_occurrences(L, block, ignore_trailing_ws=True) == 1:
            candidates.append({text: block, window: w, lines: [s+1, e]})
            if len(candidates) == 3: break
    # 退化：全文无唯一块 → 用"锚点扩展法"（向上/向下各扩 1 行直到唯一，最多扩 20 行）
    return candidates or expand_until_unique(file_lines, focus_line, max_expand=20)
```

**三个细节决定成败**：

1. **窗口扩张序不是线性 1,2,3**：多数情况下 `w=0`（单行）就已唯一；遇到 `}`、`return None` 这类高频行才需要扩张。用几何序（0,1,2,4,8）能保证 90% 场景只做一次全文匹配。
2. **`count_occurrences` 必须按行序列匹配**，不能按子串——`if x:` 是 `if x: return` 的子串，会让唯一性检查假阴性。
3. **明确拒绝「不唯一就放弃」**：宁可给宿主 3 个候选让模型挑，也不要返回 0 个候选让流程断掉（这正是 Agentless 论文里失败率的头号来源）。

**复杂度**：`w` 最多 5 次，每次 `count_occurrences` 是 `O(len(L) · w)`（可用 KMP 优化到 `O(len(L) + w)` 但没必要，文件通常 < 3000 行）。

### 7.2 四道校验的判定顺序与短路

顺序**必须**是：格式 → 锚点 → scope → 内容。理由不是随机：

- 格式关最便宜（纯语法，`O(patch 长度)`），先挡掉 60% 的坏 patch；
- 锚点关次之（一次全文匹配），挡掉「模型凭空编造上下文」；
- scope 关（集合运算）极便宜但**放在锚点后**是有意的：让模型先知道"锚点错了"，而不是被 scope 报错误导去改路径；
- 内容关最贵（正则 + 模式库），放最后。

**每条失败必须返回可指导的重试信息**（这是 3 次额度能用满而不是白烧的前提）：

| 失败关 | `error.code` | `nextAction` 具体化 |
|---|---|---|
| 格式 | `BadArgument` | 「SEARCH 块未闭合，第 N 行起缺少 `>>>>>>> REPLACE`」 |
| 锚点（0 处） | `InvalidOperation` | 「SEARCH 块未在目标文件中找到。当前文件该区域内容为：<窗口>」（带**实时内容**而非旧内容） |
| 锚点（≥2 处） | `InvalidOperation` | 「SEARCH 块匹配 N 处，需扩大到唯一。建议包含上下文行：<前后 2 行>」 |
| scope | `Forbidden` | 「hunk 目标 `src/x.py` 不在 scope `["src/y.py"]` 内，本味药不可跨文件」 |
| 内容 | `Forbidden` | 「命中禁止项 `hardcoded_secret`。本味药不得引入密钥，请改为从环境变量读取」 |

### 7.3 3 次退避：不是重复同一件事

设计契约写「≤3 次，token 不失效可重试」。**朴素重试是浪费**——模型在第 2 次大概率犯同样的错。给出退避策略：

| 次 | 策略 | 上下文增量 |
|---|---|---|
| 1 | 直提 | 原 `patch_context` |
| 2 | **强化锚点**：只给**第一个候选块**，并要求「原样复制该块作为 SEARCH」 | 附上次失败的 `error.nextAction` + 实时窗口 |
| 3 | **降级为最小替换**：药师把 SEARCH 块自己填好，宿主**只填 REPLACE 体**（`patch_context.mode = "replace_only"`） | 附「只输出替换后的代码，不要输出 SEARCH」 |

若第 3 次仍失败 → Q6 触发（转外购 / 改签 / 终止）。**这条退避曲线的意义**：把「模型找锚点」这个高熵任务逐级降级为零熵任务，实测应能把煎药成功率从 Agentless 原始的 50–60% 拉到 85%+（此处为设计预期，需在 P2 用真实仓库标定后回填实测值）。

### 7.4 patch 解析：unified diff 与 SEARCH/REPLACE 双格式

```
parse_patch(text) -> list[Hunk] | ParseError
  若 text 含 "<<<<<<< SEARCH" → 解析 SEARCH/REPLACE 块，每个块转成 Hunk{search, replace, path?}
  否则若 text 含 "--- a/" 或 "@@" → 按 unified diff 解析，提取 (path, old_start, old_lines, new_lines)
  否则 → ParseError("未识别的补丁格式")
```

**格式统一到 Hunk 之后，一切校验与逆应用都只对 Hunk 操作**——这是让「两种格式、四道校验、逆应用回滚」不要写成四份代码的关键抽象。`Hunk = (path, search_lines, replace_lines, line_hint)`。

---

## 8. A8 · 漂移判定与最小逆应用回滚

### 8.1 漂移判定（三值，不是二值）

```
drift(item, actual_changed_files, hunks) =
    ✗ VIOLATION   若 actual_changed_files ⊄ item.scope_paths
    ⚠ SUSPECT     若 ∃hunk: hunk 落在 scope 内但 hunk 行区间 ∉ item.scope_symbols 声明的区间
    ✓ OK          其余
```

成药施治先逆应用完全落在 `scope_lines` 之外的 hunk，再按行裁掉同一个 hunk 里越界的行。范围内改动保留，并记下 `drift_files`。对不上逆替换锚点时剂量记 `rollback_conflict`，未决文本留在工作区，快照写入 `.hospital/patches/<case>/<item>-r<round>.snapshot.json`。`resolve_conflict` 已开放：`manual_resolved` 以现状为基线重验，越界则挂起且不改文件；`retry` 再跑一次逆应用；`skip_item` 把快照写回并作废本味。绝不 `git checkout`。`.hospital/` 仍整味退回工作区。P2 还要补的：

- **VIOLATION 的最小化**：不是回滚整味药，而是**只逆应用越界 hunk**，范围内的改动保留（设计契约 §4.4 明确要求）；
- **SUSPECT 的判定需要符号区间**，一期用 `ast` 静态解析（`def/class` 的 `lineno..end_lineno`），二期外接 LSP。

### 8.2 逆应用算法

逆应用 = 对正向 patch 的每个 hunk，执行 `search ⟶ replace` 的反向替换（`replace ⟶ search`），**按 hunk 逆序（文件内自下而上）**执行——否则上方 hunk 的行号变动会让下方 hunk 的定位失效。

```
function REVERSE_APPLY(file_text, hunks) -> (new_text, Result)
    # hunks 已按 (path, old_start) 逆序排好
    for h in hunks:
        if count_occurrences(file_text, h.replace_lines) == 1:
            file_text ← replace_once(file_text, h.replace_lines, h.search_lines)
        else if count_occurrences(file_text, h.replace_lines) == 0 ∧ count_occurrences(file_text, h.search_lines) == 1:
            skip  # 该 hunk 已被用户手改覆盖，视为已回滚
        else:
            return (原文本, RollbackConflict(path=h.path, line=h.line_hint,
                       reason="replace 区块被修改，无法定位逆替换锚点"))
    return (file_text, Success)
```

**关键行为**：
- **绝不 fallback 到 `git checkout`**（设计契约明文禁止：会连带用户改动一起丢）；
- **冲突即挂起**：`RollbackConflict` → `dose.result = rollback_conflict` + 病例 `blocked_on` → 用户 `resolve_conflict` 三选一（`manual_resolved` 以现状为基线重验 / `retry` 重试逆应用 / `skip_item` 作废改签）；
- **写盘前先落 patch 文件**（`.hospital/patches/<case>/<item>-r<round>.revert.diff`），保证「逆应用本身也可追溯」。

**LIFO**：同一文件多味药的回滚按 **dose 的 `created_at` 逆序**执行，与栈语义一致；实现上在 `doses` 表加 `rollback_seq`，或直接按 `(round desc, created_at desc)` 排序。

### 8.3 复杂度

`k` 个 hunk，文件长 `L`：`O(k · L)`（每次一次全文匹配）。k ≤ 20，L ≤ 5000 行，可忽略。

---

## 9. A9 · 疗效判定与出院门禁决策树

### 9.1 结构：两级瀑布（设计契约已定），本文补的是**门禁内部的合成规则**

疗效项（hard）与四维门禁的关系用一个决策树画清楚，避免实现时把两者写成互相打架的 if：

```
DISCHARGE(case):
    diff ← RECHECK(case)                        # §2 + §3
    new_related ← diff.new_related

    # ── 第一级：再入院判定（此时不产出 gate）──
    if |new_related| > 0 and case.round < case.max_rounds:
        return READMIT(round+1, list=new_related)      # 无 gate

    # ── 第二级：出院门禁会签 ──
    checks ← []
    checks += EFFECT_CHECK(diff)                       # 疗效项：全档 hard
    checks += HEALTH_CHECK(diff, baseline=round1)      # 全仓健康项：按 level
    checks += ACCEPTANCE_CHECK(case)                   # 验收项：按 level
    checks += ARCHIVE_CHECK(case)                      # 档案项：按 level

    verdict ← PASS if all(hard checks pass) else FAIL
    if verdict == FAIL and waive_requested:
        if not (approver == "user" and user_confirmed and reason):
            raise Forbidden("豁免须真人确认")
        verdict ← WAIVED

    outcome ← derive_outcome(diff, verdict)            # 见 9.2
    return DISCHARGE(gate={verdict, checks}, outcome, follow_up=FOLLOWUP(case, diff))
```

### 9.2 `outcome` 派生表（把"治愈/好转/未愈/转院"变成可计算的）

| 条件 | `outcome` |
|---|---|
| `resolved > 0` ∧ `new_related == 0` ∧ 全部初始 finding 都 resolved | `cured` |
| `resolved > 0` ∧ `new_related == 0` | `improved` |
| `gate.verdict == fail` 未豁免 | `unchanged`（可治但未达标）；若含 manual 项且未处理 → `referred` |
| `round == max_rounds` ∧ `new_related > 0` | `referred` |
| `gate.verdict == waived` | 按 `resolved/new_related` 走上面三行，不因豁免升档 |

**注意**：`cured` 要求「全部初始 finding resolved」，其中 `possibly_resolved` **不计入 resolved**（宁严勿宽——出院小结标「治愈」被用户打脸的成本远高于标「好转」）。

### 9.3 基线对齐（设计契约 §4.5 的「按设备对齐」落到算法）

```
baseline(round1) = { (device, rule_id): count }  ← 第 1 轮初诊 scan 的产出
current          = { (device, rule_id): count }  ← 本轮复扫产出
comparable_keys  = baseline.keys() ∩ current.keys()          # 只比设备交集
regression(k)    = current[k] > baseline[k]                   # 同类问题变多
```

三个容易写错的地方：

1. **不能只比总数**：总数不变但 critical 从 1 涨到 3 是老病换新病，必须按 `(device, rule_id)` 分组比；
2. **新启用设备不算恶化**：`current.keys() - baseline.keys()` 中的增量一律忽略，只在报告「覆盖与限制」里注明「本轮新增设备 X，其产出不参与健康项对比」；
3. **`new_unrelated` 的 critical 是 hard check**（设计契约 v0.2→v0.3 第 2 条）：即使设备对齐后 regression 为 0，只要存在 critical 级 `new_unrelated`，standard 档也判 fail。

### 9.4 门禁三档的合成逻辑（避免 3×4 = 12 个 if）

```python
HARD = {"strict": {"effect","health_all","acceptance","archive"},
        "standard": {"effect","health_critical","acceptance","archive"},
        "lax": {"effect"}}

verdict = "pass" if all(c.verdict == "pass" for c in checks if c.name in HARD[level]) else "fail"
```

三档的差异只在**哪些项算 hard**，其余项无论 pass/fail 都只进 `checks` 明细供人看。这样新增维度只改表，不改逻辑。

---

## 10. 派生状态模型：把「医疗不变量」写成可测断言

状态机已经画好了，但**真正的 bug 都长在"状态对了、关系错了"的缝里**。下面四条不变量建议直接写成 property-based 测试（Hypothesis）：

| # | 不变量 | 违反时的典型后果 |
|---|---|---|
| I1 | 任一 `project_root` 至多 1 个 `status ∉ {discharged, referred}` 的 case | 并发施治互相踩踏（部分唯一索引已兜底，但 `force` 转诊路径要测） |
| I2 | `prescription.status = approved` ⟹ 该 case 存在 `human_in_loop=1` 或 `risk_level=low` | 高危自批后门 |
| I3 | `dose.result = success` ⟹ 存在对应 `patch_path` 且该 patch 文件内容 == 实际 diff | 「改了什么」不可追溯（设计契约「病例可溯」的底线） |
| I4 | `discharge.outcome ∈ {cured, improved}` ⟹ `gate.verdict ∈ {pass, waived}` 且疗效项 pass | 门禁被绕过 |
| I5 | `case.pending_gate ≠ ""` ⟹ 该阶段产物（report 或 dose）已存在 | 用户确认了一个不存在的产物 |

**时序不变量**（并发相关，用模型检查或压测）：

- I6：`hosp_dispense` 对同一 case 的两次并发调用，**至多一次**产生 `dose` 记录（服务端互斥锁 + 处方项状态 CAS）；
- I7：`patch_token` 的消费是**原子的一次性**（`UPDATE ... WHERE token=? AND used=0` 影响行数 = 1 才继续），防同一 token 重放两次应用同一补丁。

建议把 I1–I7 写成 `tests/test_invariants.py`，用 Hypothesis 随机生成状态序列（`register/scan/diagnose/prescribe/approve/dispense/discharge` 的随机合法/非法序列），断言：非法序列全部抛 `InvalidOperation` 且**不改库**（事务回滚）。

---

## 11. 统计学习：把病历变成先验

### 11.1 处方-疗效统计（设计契约 §4.8 第 3 点）的算法化

按 `(rule_id, method)` 维护一个**成功率估计**，用 Beta-Bernoulli 共轭先验（天然带小样本保护，无需调参）：

```
prior:     Beta(α₀, β₀) = Beta(1, 1)           # 无信息先验
observed:  s = Σ success(dose), f = Σ failure(dose)   # 仅统计 result ∈ {success, failed}
posterior: Beta(α₀ + s, β₀ + f)
estimate:  p̂ = (α₀ + s) / (α₀ + β₀ + s + f)
```

- 「`E501` 用 autofix 治了 40 次全成功」→ `p̂ = 41/42 ≈ 0.976`，autofix 打分自然抬高；
- 「`F821` 用 llm_patch 治了 3 次失败 2 次」→ `p̂ = 2/5 = 0.4`，下次开方自动倾向 manual 或先补充上下文；
- **不落库冗余字段**：每次开方时 `GROUP BY (rule_id, method)` 现算，行数可控（规则数 × 3）；若表大，加物化视图按天刷新。

实现成本：一个 SQL 聚合 + 一次算术，`hosp_prescribe` 里增加一次查询。**这是整个设计里性价比最高的一个算法**——它让「病案室」从装饰变成真正的决策输入。

### 11.2 置信度校准（可选，P3）

`confidence` 若要与「实际成功率」语义对齐，需要校准。最简做法：按 `fixability` 分桶，画 reliability curve（预测置信度 vs 实际成功频率），再拟合一个**保序回归**（isotonic regression，`sklearn.isotonic.IsotonicRegression`，MIT）得到单调校准函数。样本 < 100 时不校准，直接用手填先验。

---

## 12. 复杂度与预算总表

给 DevOps 一个「这个服务跑在笔记本上会不会卡」的答案。

| 环节 | 复杂度 | 实测/估算规模 | 备注 |
|---|---|---|---|
| 扫描（ruff） | 外部进程，`O(文件数 × 文件大小)` | 千行仓库 ≈ 0.5–2s | 主力耗时，非本系统算法开销 |
| 归一化 + 指纹 | `O(F · L)`，F 为 finding 数，L 为片段长 | 1000 finding × 200B ≈ 10ms | |
| 差分匹配 | `O(n+m)` + 少量 fuzzy | < 20ms | |
| 归因 | `O(F · k)`，k 为 footprint 区间数 | < 5ms | |
| 聚类 + 排序 | `O(n log n)` | < 5ms | |
| 四道校验 | `O(patch_len + L)` | < 10ms/次 | 最坏 3 次重试 |
| 逆应用 | `O(k · L)` | < 20ms | |
| 门禁会签 | `O(F)` + 一次全量验收命令 | 验收命令为主 | 预算见下 |
| SQLite 查询 | 单仓库病例数 C，`O(log C)` 索引查找 | C < 1000 无感 | 调档批查询已按 500 分批 |

**一次完整门诊（5 finding、2 味药）的算法开销约 100–200ms**，其余全在外部设备与验收命令，符合「本地优先秒级体验」。

---

## 13. 落地顺序与验收指标

按 ROI 排序（不是按设计文档章节顺序）：

| 序 | 算法 | 落地阶段 | 验收指标（可测） |
|---|---|---|---|
| 1 | A9 门禁决策树 + 基线对齐 | P1 | 构造 3 个 case：治好手上病添新病（必须 fail）、切了设备（必须 pass）、critical new_unrelated（必须 fail） |
| 2 | A2 差分 + A3 归因 | P1 | 5 个真实修复用例，`new_related` 判定准确率 ≥ 80% |
| 3 | A1 指纹 occurrence 双键 | P1 | §1.5 的 5 个边界用例全绿 |
| 4 | A4 打分 + 11.1 成功率统计 | P1→P2 | 冷启动与有病史两种情形下，剂型选择与人工判断一致率 ≥ 75% |
| 5 | A7 SEARCH 候选 + 退避曲线 | P2 | 同一批 20 个煎药 case，一次成功率 ≥ 60%，三次内成功率 ≥ 85% |
| 6 | A8 最小逆应用 | P2 | 越界 hunk 回滚后，范围内改动必须保留（diff 断言） |
| 7 | A5 聚类 + A6 拓扑排序 | P2 | 50 finding 的仓库，处方项数 ≤ 8 |
| 8 | §10 不变量测试 | 贯穿 | Hypothesis 1000 例无违反 |

---

## 14. 与现有实现的差距清单（可直接开工）

| 文件 | 现状 | 需补 | 工作量 |
|---|---|---|---|
| `core/fingerprint.py` + `core/algorithms.py` | 双键、匹配、归因、拓扑序、SEARCH 候选、逆应用、门禁函数 | 复诊差分、出院门禁、煎药提交尚未挂到工具 | 中 |
| `departments/scanner.py` | 批量 `stamp_findings` | — | 已接入 |
| `departments/diagnostician.py` | §5.2 三级聚类；`_judge()` 仍是规则表 | §4.2 打分（权重表未给全，先不改判定） | 中 |
| `departments/prescriber.py` | §6 拓扑排序；同簇超过 5 个文件切开 | — | 已接入 |
| `departments/pharmacist.py` | 煎药备料走 `gen_search_candidates`；成药照旧 | §7 四道校验、退避、token、提交 | 大 |
| `departments/nurse.py` | **不存在**（逆应用函数已在 algorithms） | §8 挂到施治回滚 | 大 |
| `departments/discharge.py` | 复扫差分、再入院、门禁、豁免、随访；`distill_to_rsi` 在 `rsi` 在岗时写入 documentation | 疗效统计、`case_links` | 中 |
| `storage/db.py` | `findings.content_key`、`diagnoses.member_ids_json` | §11.1 疗效聚合、`case_links` | 小 |
| `tests/` | 无 property-based | §10 I1–I7 | 中 |

---

## 15. 待定问题（需要产品/技术决策）

1. **θ 与 W 是否需要暴露给用户**：暴露到 `code-hospital.yaml` 会让「同一份代码在不同配置下诊断结果不同」，影响报告可复现性。建议：可配，但报告头部记录生效参数快照（进 patch 同类思路）。
2. **`possibly_resolved` 在门禁里算什么**：当前按「不算 resolved」（宁严），但这会让"改了 30 处、29 处 L1 命中"的 case 强判 `improved` 而非 `cured`。是否给一个 `possibly_resolved` 占比 > 80% 时允许人工确认升 `cured` 的通道？
3. **跨文件行为耦合的归因**（§3.2）：是否值得在验收命令失败时做堆栈关联？成本不高，但需要统一验收输出的解析格式。
4. **A4 的 `risk_penalty` 是否允许分数压倒硬约束**：当前设计是硬约束优先（安全侧），会导致 autofix 几乎永远处理不了 manifest 文件——这是对的，但要确认产品接受。
