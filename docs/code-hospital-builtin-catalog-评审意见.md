# 《内置名册》评审意见（code-hospital-builtin-catalog.md）

> 评审对象：`docs/code-hospital-builtin-catalog.md`（44715 字节，v 未标）
> 对照材料：`code-hospital-design.md`（原则 7、§4.5/§4.8）、`code-hospital-io-contracts.md`、`code-hospital-algorithms.md`（A5 聚类 / A6 排序）、现有源码 `diagnostician.py` / `prescriber.py` / `scanner.py` / `core/models.py` / `storage/db.py`
> 评审口径：文档自述为「设计已落盘、运行时不读取」，因此评审按「能否直接指导研发实现、是否与既有契约/代码冲突、边界是否闭合」三条打分。

---

## 一、总体结论

**方向合理，结构清晰，可作为落地基线；但有 6 处硬冲突、8 处边界未闭合，建议修订后再进入编码。**

**值得肯定的 8 点：**

1. **数据驱动而非代码分支**。三本名册随包携带，把「哪台仪器、接给谁、开什么药、什么剂型」收成 YAML，医生/药/仪器各自独立扩展。这比在 `_judge()` 里继续堆 `if tool == ...` 强一个量级——现在 `diagnostician._judge` 就是靠 tool 名硬编码，加一台仪器就要改核心判定函数。
2. **不新增工具、不动状态机**。守住设计原则 7「能力挂流程，不乱设科室」。工具面只加字段（`requested_specialties`、`scan_orders`、`formula_id`、`scope_key`），成本最低。
3. **`scan_orders` 落病例而不是返回值**。检查单必须穿过下一次工具调用，放病例上是正确选择；并且明确「补拍不新开 round、发现并入同一 `report_id`」，避免了报告链断裂。
4. **限制项分「终态」与「可补」**。二进制不在 PATH、无适配文件、无契约 → 终态；点名未拍、超时崩溃 → 检查单。这条设计直接决定用户会不会被无限循环打扰，把「宁可等不可猜」落到了具体规则上。
5. **「无契约不判架构违规」**。不替仓库发明分层，只认 `code-hospital.yaml` 自己声明的层，与 import-linter 同级。这是基础设施类工具最容易越界的地方，这里守住了。
6. **许可证分级清楚**。MegaLinter（AGPL）只学描述符、不链源码；Semgrep（LGPL）进程外调用。调研了 9 个开源件并逐一给出「参考/搬迁/复用」判断，边界写得很干净。
7. **药柜多对多 + 适应证单一真相**。清创适应证只留一份（§4.3 表格），并明确接入时删除 `diagnostician.py` 里的 `_AUTOFIX_EXACT` / `_AUTOFIX_PREFIX` 副本。这种「删旧副本」的写法比「新增一份」更负责。
8. **病症/簇/专科三层分得清**。§4.1.1 用 `scope_key` 把「一簇一接诊专科」和「一病症多专科会诊」拆开，并给出四步主治选择与并列裁决，可直接翻译为排序函数。

**必须修订的核心问题（详见第二节）：**

| # | 严重度 | 问题 | 影响 |
|---|---|---|---|
| P1 | 🔴 高 | **成药无法定位到单行**：`autofix` 项沿用现行契约没有行号，但清创 / 补罩的范围是「发现的行区间」 | 一处 `F841` 会执行整文件 `ruff --fix`，误改同文件其他问题 |
| P2 | 🔴 高 | **聚类双索引并存**：名册按 `scope_key` 收病症，现行 `group_findings` 仍按 `content_key` 跨文件并簇 | 同一批发现可能进两个病症，`(formula_id, scope_key)` 去重键兜不住 |
| P3 | 🔴 高 | **剂型判定翻转**：现行 `_judge` 对 `semgrep + critical` 判 `manual`，名册的封存只认 gitleaks，`java-pack` 的 critical 会落 `llm_patch` | 安全类发现从「人工外购」降级为「AI 煎药」，是安全侧回退 |
| P4 | 🔴 高 | **合并后成药/煎药冲突**：成药簇与煎药簇被 `content_key` 跨文件合并，成药项的范围变成一个簇内文件集合 | 高危「先别改」与「要改」之外，又多一类未定义冲突 |
| P5 | 🟠 中 | **药柜双写**：专科→药的映射同时存在于 `Specialty.formulary` 和 `Formula` 的 `药柜` 列 | 两处不同步即出现「药在架上但没人能开」 |
| P6 | 🟠 中 | **`fixability` 口径自相矛盾**：§4.1.1 例子说结构科主治、处方里同时有清创+拆分，但 §5.2 写「取主药（严重度最高）的 `method`」 | 诊断书统计与处方实际项不一致 |
| P7 | 🟠 中 | **`contrast` 去重键不成形**：`(contrast, 主药 scope_key)` 与处方项去重键 `(formula_id, scope_key)` 是两个键 | 两个函数各开一味对照 → 同键两条，与「只留一条」冲突 |
| P8 | 🟠 中 | **jscpd 许可证未标注**，而同段其他项目都标了 | 与原则 7「许可证决定能不能进仓库」不一致 |
| P9-P22 | 🟡 低/待核 | `#file` 键的处方粒度上限丢失、`scan_orders` 无事件留痕、`catalog.layers` 与 import-linter 优先级、`scope: symbol_or_file` 语义、`report Markdown` 重写破坏留痕、Phase 2 药的空 `rule_ids`、`0.5` 阈值回归、`can_autofix` 与 `phase` 耦合等 | 见第二节 |

---

## 二、问题清单（按严重度排序，22 条）

### 2.1 高优先级（会直接导致错误行为）

**P1 · 成药无法定位到单行 🔴**

- 名册 §4.3 清创的「范围与验收」写「**发现的行区间**」，`scope_key` 也是 `file:…#lines:12-12` 这种行级键。
- 但现行 `prescriber.build_items` 生成的处方项只有 `target_file` / `scope_paths` / `scope_symbols`，**没有行区间字段**；`hosp_dispense` 执行 `autofix` 时也无法知道只清这一行。
- 后果：一处 `F841` 触发整文件的 `ruff --fix`。若同文件另有 `E722`（走补罩煎药），成药先跑会把行号全打乱，而 A6 §6 已明确指出「成药改变行号，后续煎药 SEARCH 候选全部对不上」。
- **建议**：处方项新增 `scope_lines: {start, end}`（或一个可复用的 `scope` 结构），`autofix` 执行改为 `ruff --fix --select <rule_id> --line-range`（ruff 支持 `--range`/`--line-range`，semgrep 侧用 noqa/幂等校验）。这属于第 6 节契约升版必须增补的一项，现在漏了。

**P2 · 聚类双索引并存 🔴**

- §3.3 和 §4.1.1 全部按 `scope_key`（文件+符号/行区间）收簇收病症；但 §4.8 接入顺序第 4 步只说「诊断按分流表写专科 id」，没有说要改 `group_findings`。而现有 `group_findings` 的第一级是 `content_key`（**跨文件**合并同规则同形态），第二级才是同文件同规则。
- 后果：`E501` 在 15 个文件出现会先被 `content_key` 合成一簇；这一簇里有 15 个不同 `scope_key`，收病症时又要按 `scope_key` 拆回 15 个病灶。此时「一簇一位接诊专科」与「一病症一位主治」的关系就断了——簇是跨 15 文件的，病症是单文件单符号的，簇不能映射到任一病症。
- **建议**：明确「`scope_key` 收簇替代 `content_key` 收簇」，并说清 A5 的三级键怎么改（如：`scope_key` 为主键，`content_key` 仅用于**同规则跨文件**的成药合并，且合并后必须仍满足「同一 `rule_id`、同一 `method`」）。否则 §4.1.1 的所有规则都是悬空的。

**P3 · 剂型判定安全侧回退 🔴**

- 现行 `_judge`：`gitleaks → manual(0.92)`；`ruff+critical → manual(0.7)`；`semgrep+critical → manual(0.62)`；其他 semgrep → `llm_patch(0.58)`。
- 名册：封存只命中 gitleaks 的全部规则；`java-pack`（semgrep 引擎）只声明了 `java.null-deref`、`java.resource-leak` → 补罩 = `llm_patch`；**没有「semgrep critical」这条兜底**。随包 semgrep 小规则集里若有 critical 级安全规则，会从「外购人工」变成「煎药自动补丁」。
- 另外，清创明确排除 `critical`（`severity=critical` 不命中），这个细节很好；但**封存没有对「非 gitleaks 的密钥类发现」开口**（如 semgrep 的硬编码密码规则）。
- **建议**：药名册增加一条通用适应证 `severity: critical` → `seal`（全科柜），或在仪器名册加 `critical_severity_manual: true`；并在 §4.1 写明「现有 `_judge` 的语义如何被名册完全复现」，列一张「现行规则 → 名册落点」对照表作为回归基线。

**P4 · 合并后成药/煎药冲突未定义 🔴**

- §3.2 节点 6 只处理了「同一 `scope_key` 上 `hold=true` 与 `autofix|llm_patch` 并存」。
- 但 `cluster_max_files=5` 会把一个跨文件 `E501` 簇按文件切块，成药项范围 = 文件块；若同簇内还有一处 `E722`（补罩，`llm_patch`），则同一簇里成药项与煎药项的范围是**父子关系而非相等**，`scope_key` 不同 → 不标 `formulary_conflict`，但实际 `ruff --fix` 与补罩 patch 会互相踩。
- **建议**：`formulary_conflict` 的判定条件从「`scope_key` 相等」扩为「`scope_key` 相等 **或** 行区间相交」；并明确成药优先执行、煎药备料前重读文件（A6 §6 已提出，但名册未引用）。

**P5 · 药柜双写 🟠**

- `Specialty.formulary: [debride, guard, split, ...]`（§5.1 类图、§5 示例 YAML）与 §4.3 处方药表的「药柜」列（`全科、Python`）是同一信息的**两个存放位置**。
- 这是典型的一致性隐患：改一处忘另一处，就出现「药在柜里但专科打不开」或「专科开了柜里没有的药」。
- **建议**：单一真相。以 `Specialty.formulary` 为准，删除 §4.3 表的「药柜」列（改为「见 §4.2 专科名册」）；或反之。并在接入时加一条校验：`set(all specialties.formulary) == set(formula.id)` 且无孤儿引用。

**P6 · `fixability` 口径矛盾 🟠**

- §4.1.1 例子：「最终一张处方里可以同时有清创、拆分、削峰」。§3.2 节点 6：「一簇多味且剂型不同时，以各处方项的 `method` 为准」。
- 但 §5.2 末段又写：「一簇多味且剂型不同时，展示用**主药（严重度最高的那味）**的 `method`」。而「主药」在 §4.3 中并未定义（只有 §4.1.1 的「主治专科」）。
- 后果：诊断书统计 `_stats()` 的 `autofix/llm_patch/manual` 计数会随「主药」定义漂移，用户看到的「本次 N 项成药」与实际可执行项不符。
- **建议**：明确 `fixability` 只作展示占位、统计口径改为「按处方项计数」，并删除「主药」说法（改为「主治专科开的药」或直接取 `method` 优先级 `autofix > llm_patch > manual`）。

**P7 · 对照去重键不成形 🟠**

- §4.3：对照 `kind: companion`，「去重键是 `(contrast, 主药的 scope_key)`」。
- §3.2：处方项去重键是 `(formula_id, scope_key)`。
- 两者不等价：同一个 `load_report` 上若拆分开了一味对照，削峰也开一味对照，前者的键是 `(contrast, file:…#sym:load_report)`，后者的键**也是** `(contrast, file:…#sym:load_report)` —— 按 `(formula_id, scope_key)` 看是**同一键**，应该只留一条；但按 `(contrast, 主药 scope_key)` 看是两条不同的主药，语义上又该各留一条。
- 这在实际中很常见：同一函数既超长又有循环 IO，拆分的对照测结构、削峰的对照测行为，二者不该合并。
- **建议**：把去重键统一为 `(formula_id, scope_key, parent_formula_id)`，或在 `scope_key` 上拼主药 id：`file:…#sym:load_report|contrast_of:split`。现写法会让「合成一张处方」的结果不可预测。

---

### 2.2 中优先级（实现期会卡住）

**P8 · jscpd 许可证未标注 🟠**

§4.6「复用」表与正文都提到 jscpd，但唯独它没写许可。而原则 7 明确「许可证决定能不能进仓库」。jscpd 是 MIT（需以官方仓库为准复核），但文档既然给其他 8 个项目都标了，漏这一项会让人怀疑是「没查」。

**P9 · `#file` 键让处方粒度上限失效 🟠**

A5 §5.3 有硬约束 `cluster_max_files=5`，防止「1 味药动 40 个文件」。名册把文件行数发现的 `scope_key` 定为 `file:{path}#file`，一个 500 行文件只开一味拆分 —— 这个粒度是对的。但**分层尺**的键是 `contract:{contract_id}#rule:{rule_id}`，一个契约规则的违例可能跨 40 个文件、合成**一个**病症、开**一味**归位。归位是 `manual`（`hold=true`），危害小；但如果以后有 `llm_patch` 的契约类药，就会绕过上限。建议明确：`contract:` 键的处方项在生成时仍按文件切块（`cluster_max_files` 生效），只是病症/主治按契约聚合。

**P10 · `scan_orders` 无事件留痕 🟠**

设计 §2 提到「轻量预检分诊可升级 triage_level（写事件留痕）」，但 `db.py` 里**没有 events 表**，只有 `nursing_logs`。名册 §3.3 第 2 步「写入 `scan_orders`、不插入诊断行、不写诊断报告、`status` 不变」是一次状态实质变化，却没有任何留痕手段。建议接入时在同批加 `nursing_logs` 记录（`action=scan_order_issued`），否则事后无法回答「为什么这一轮没出诊断书」。

**P11 · `catalog.layers` 与 import-linter 的优先级表述有歧义 🟠**

§4.4 配置示例注释写「仅当仓库没有 import-linter、ArchUnit、dependency-cruiser 时才作为契约」，但 §4.4 表格 `layer-ruler` 的「缺席时」列只写了「没有契约：终态」，§4.1 原则 4 又说「和 import-linter 同级」。三处表述粒度不同。建议在 §4.4 明确一张优先级表：`import-linter 配置 > ArchUnit 测试 > dependency-cruiser 配置 > code-hospital.yaml catalog.layers > 无契约(终态)`，并说明「多份契约同时存在时全跑还是只跑第一份」。

**P12 · `scope: symbol_or_file` 未定义 🟠**

§5 处方药示例里 `scope: symbol_or_file`、`scope: reference_closure` 是自由字符串，没有枚举，也没有说明药师的 `_path_chunks` / `scope_symbols` 如何消费它。建议定义 `scope` 枚举（`line_range | symbol | file | contract_rule | reference_closure | test_files`）并逐值给出「处方项字段怎么填」。

**P13 · 补拍后「报告 Markdown 按合并后的发现重写」破坏留痕 🟠**

§3.3 第 4 步：「新发现插入同一个 `report_id`，报告 Markdown 按合并后的发现重写」。但设计原则 3 是「全程留痕，病历可溯」。重写报告会让「初检那一刻看到了什么」永久丢失，而 A2 前后差分正依赖「本轮看到了什么」。建议：报告 Markdown 追加「补拍增量」小节，而不是重写；或保留 `reports` 表两行（初检 + 补拍），但共享同一 `report_id`。

**P14 · Phase 2 药的空 `rule_ids` 会静默永不命中 🟠**

正骨（rename）、去重（dedupe）在第一期写 `rule_ids: []`，且 `phase: 2`。但 §4.3 只说了「不入方」，没有说**为什么**不入方、以及 `phase` 字段谁在读。若运行时没有「按 phase 过滤」的实现，空列表只是「恰好不命中」，一旦有人补了 rule_id 就会意外入方。建议：药名册加载时断言 `phase <= 当前阶段` 才允许参与匹配，并把 `phase` 写进 §6 的读取说明。

**P15 · 置信度 `0.5` 阈值与现行 `_judge` 的映射未回归 🟠**

名册规定「对不上任何 `rule` 适应证的簇，`confidence < 0.5`，`formulary_matched=false`」。但现行 `_judge` 会对 `semgrep` 非 critical 给 **0.58** —— 意味着今天「对不上规则表也能入方」的 semgrep 发现，名册下会变成「待复核不入方」。这是行为变更，会减少出方数量。文档没有把它写成「行为变更」并给出回归对比（哪些规则的入方率会变）。建议补一张「现行 `_judge` 输出 → 名册输出」的对照表，作为接入验收基线。

---

### 2.3 低优先级 / 待核（打磨项）

16. **`Finding.tool` 与 `Adapter.name` 的错位**：`java-pack` 的 `engine=semgrep`，走 `semgrep_adapter`，`normalize()` 当前用 `adapter.name` 写 `Finding.tool`，所以现在的实现会写 `semgrep` 而不是 `java-pack`。名册说「`tool` 写仪器 id」，那第 5 步接入时 `semgrep_adapter` 必须支持「一次调用产出两个 tool 值」或按 rule_id 前缀回写。这是实现细节，但文档 §4.8 没说清，容易被误估为「零改动」。
17. **`hotspot.loop-io` 的 `symbol` 怎么来**：§4.1.1 规则 1 说「带符号的发现各自打开病灶」，但 semgrep YAML 要产出 `symbol` 需要元变量捕获；`scanner.normalize()` 目前用 `_symbol_hint(line_text)` 从行文本猜。文档应说明随包规则必须带 `metavariable` 或在名册声明 `symbol_strategy`。
18. **`throttle` 削峰的验收**：`method` 在「没有验收命令时改为 `manual`」，但 §4.4 又说「验收命令留在护理和出院门禁」。谁判定「没有验收命令」？是 `code-hospital.yaml` 的 `acceptance.commands` 为空，还是探测失败？建议沿用设计的「探测≠执行」口径并写明。
19. **`contrast` 只许动测试文件**：范围写「只许动测试文件」，但 pytest 项目里测试文件未必都在 `tests/`（如 `test_*.py` 同目录）。建议给出可配置的测试文件 glob。
20. **`type-ruler` 与 Python 语言科**：`activation=on_request`，只有点名 Python 科才上台；但 python 语言科在分流表里是 ruff 的默认接收方。要点名才做类型检查，用户预期可能错位（点名「Python」只想让 ruff 更专，却顺带跑 mypy，慢）。建议拆分「点名专科」与「点名仪器」两个入口，或给 `type-ruler` 单独 id。
21. **§4.4 仪器表缺 `limitation` 列**：字段清单里有 `limitation`，示例 YAML 也有，但两张仪器表只有「缺席时」列。建议表头直接用 `limitation`，或说明「缺席时」就是 `limitation` 的填充内容。
22. **§2.1 第二条 mermaid 图未标 `graph LR` 的文件间依赖序号**：`megalinter → instruments` 这类箭头容易被读成「依赖」，而正文说的是「学形状」。建议箭头标签改为「学描述符形状」等动词。

---

## 三、改进建议（按优先级给方案）

### 建议 1 · 先补「现行行为 → 名册行为」的等价性对照表（最高优先）

这是整份文档最缺的一块。名册本质是**把代码里的隐式规则显式化**，那必须先证明「显式化之后默认行为不变」，再谈新增。

建议在 §4.3 后加一节：

| 现行代码 | 现行输出 | 名册落点 | 是否等价 |
|---|---|---|---|
| `gitleaks` 任意规则 | `manual` / 0.92 | `seal`（全科柜，`manual`，`hold`） | ✅ 等价 |
| `ruff` + `critical` | `manual` / 0.70 | 清创排除 critical；**无药命中** → 待复核 | ❌ **变更**：从外购变待复核，不入方 |
| `ruff` + 可修规则 | `autofix` / 0.86 | `debride`（`autofix`） | ✅ 等价 |
| `ruff` + 其他 | `llm_patch` / 0.72 | **无药命中**（清创不覆盖，补罩只认 BLE001） | ❌ **变更**：从煎药变待复核，不入方 |
| `semgrep` + `critical` | `manual` / 0.62 | **无药命中** | ❌ **变更（且方向不安全）** |
| `semgrep` + 其他 | `llm_patch` / 0.58 | **无药命中** | ❌ **变更** |

这张表会立刻暴露两个真问题：**（a）`ruff` 非可修非 critical 的规则（如 `F821` undefined-name）今天走煎药，名册下无药可开，会掉出方子；（b）`semgrep` 的绝大多数发现今天走煎药，名册下全落待复核。** 如果不补药，接入后处方数量会断崖式下降。建议至少补两味药：`repair`（通用煎药兜底，适应证 = 未被其他药命中的 `llm_patch` 类，`risk: medium`）、以及 semgrep critical → `seal`。

### 建议 2 · `scope_key` 与聚类算法一次性对齐（A5 改造）

- 把 `group_findings` 的三级键改为：`scope_key`（主）→ 同 `(rule_id, scope_key)` → 同 `(rule_id, content_key)` 且 `method` 相同（仅用于成药跨文件合并）。
- `content_key` 的跨文件合并必须加一个闸门：**合并后所有成员的 `method` 必须一致**，否则不合并。这一条能同时解决 P2 和 P4。
- 在 `code-hospital-algorithms.md` A5 同步更新，避免两份文档的聚类定义分叉。

### 建议 3 · 处方项补 `scope_lines`，成药执行改行级

- 契约升版：`prescription_items` 加 `scope_start` / `scope_end`（或 `scope` TEXT 存 JSON）。
- `pharmacist` 对 `autofix` 执行改为「按 `rule_id` + 行区间」的精准修复，禁止整文件 `--fix`。
- 清创的「范围与验收」从「发现的行区间」改为「`scope_lines` 指定的行区间，未指定则整文件但标 `risk: medium`」。

### 建议 4 · 药柜单一真相 + 加载期校验

- 删除 §4.3 的「药柜」列，以 `Specialty.formulary` 为唯一来源（或反之，二选一）。
- 名册加载时跑三条断言：① 每个 `formulary` 元素都存在对应 `Formula.id`；② 每味 `phase<=1` 的药至少属于一个专科柜；③ `set(所有柜子) ⊇ set(所有 phase<=1 的药)`。
- 这三条断言建议写成单测，接入前就有了回归防线。

### 建议 5 · `formulary_conflict` 判定扩为「相交」

```
conflict(a, b) = a.scope_key == b.scope_key
              or a.file == b.file and ranges_overlap(a.lines, b.lines)
              or a.parent_scope == b.scope_key (父子范围)
```

并在处方草稿上给出「冲突原因」字段（`same_scope` / `overlap` / `parent_child`），方便用户判断要不要保留。

### 建议 6 · 补一节「名册与三份现有文档的同步责任」

名册引用了设计 §4.5/§4.8、契约（`additionalProperties: false`）、算法 A5/A6，但没说**改动时谁先动**。建议补：

| 变更点 | 名册 | 设计 | 契约 | 算法 | 序号 |
|---|---|---|---|---|---|
| 新增仪器 | ✔ | — | — | — | 仅名册 |
| 新增药 | ✔ | — | — | — | 仅名册 |
| 新增专科 | ✔ | §2 架构图 | — | — | 名册 → 设计 |
| 新增处方项字段 | — | — | ✔（升版） | — | 契约 → 名册 §6 |
| 改聚类键 | ✔（§4.1.1） | — | — | ✔（A5） | 算法 → 名册 |
| 改分诊套餐 | ✔ | §4.5 | — | — | 设计 → 名册 |

没有这张表，半年后这四份文档一定会互相矛盾——现在 P2/P6 已经是两处矛盾了。

### 建议 7 · 补「失败模式与用户可见文案」清单

名册把限制项写得很细（终态 vs 可补），但**用户看到的中文文案**只在零散几处（「未配置分层，分层尺未拍」「设计科第一期无检验仪器」）。建议单独列一张「仪器 → 缺席原因 → 文案 → 是否写检查单」表，作为 UI/报告渲染的输入，避免报告里出现「设备不可用: xxx」这种机器腔（现行 `scanner.py` 就是这么写的）。

### 建议 8 · 把 7 条设计原则转成可测断言

文档的 10 条原则写得很好，但只能靠人读。建议每条配一个测试断言，作为接入验收：

| 原则 | 断言 |
|---|---|
| 仪器只拍片 | `Finding` 上不存在 `method` / `specialty` 字段 |
| 缺设备写限制项 | 任一仪器缺席，报告 `limitations` 长度 +1，且不含 `scan_orders` 项 |
| 无契约不判架构违规 | 无 `catalog.layers` 且无 `[tool.importlinter]` 时，`architecture` 专科不出现在诊断书 |
| 分诊决定上台 | `checkup` 且未点名时，`type-ruler` / `hotspot-ruler` / `layer-ruler` 均未执行 |
| 主链路离线 | 断网环境下 `hosp_scan` 仍产出报告（mock 网络异常） |
| 剂型只由药名册决定 | 删掉 `_AUTOFIX_EXACT` / `_AUTOFIX_PREFIX` 后全量测试仍通过 |
| 一病症多专科 | 构造「结构超长 + F841 同函数」，断言 1 个病症、2 位专科、主治 = 结构科 |

---

## 四、建议的修订顺序（可直接当排期用）

| 步骤 | 内容 | 产出 |
|---|---|---|
| 1 | 补「现行 → 名册」等价性对照表（建议 1），决定是否补 `repair` 兜底药与 semgrep critical → seal | 名册 §4.3 增补 2 味药 + 1 张对照表 |
| 2 | 统一 `scope_key` 与聚类算法（建议 2），同步改 `code-hospital-algorithms.md` A5 | 名册 §4.1.1 修订 + 算法 A5 修订 |
| 3 | 契约升版补 `scope_lines`（建议 3），成药执行改行级 | 契约 §prescription_items + `pharmacist` 实现 |
| 4 | 药柜单一真相 + 三条加载断言（建议 4） | 专科 YAML 定稿 + 单测 |
| 5 | `formulary_conflict` 扩为相交（建议 5）+ 对照去重键（P7） | 名册 §3.2 / §4.3 修订 |
| 6 | 补同步责任表（建议 6）、文案表（建议 7）、原则断言（建议 8） | 名册新增 3 节 |
| 7 | 其余 2.3 节 7 条打磨项 | 名册措辞微调 |

---

## 五、一句话总结

**这份名册的设计方向是对的（数据驱动、零新增工具、许可证边界清晰），但「把现有代码里的隐式规则搬进名册」这一动作只做了一半：剂型判定、聚类索引、成药范围这三处仍以旧代码为准，导致名册与实现是两个口径。** 建议先补等价性对照表与 `scope_key` 聚类改造，再补契约里的 `scope_lines`，然后才进入编码——否则接入时会发现「名册写了、代码跑出来不一样」，而这正是这类文档最容易失效的地方。

---

> 本评审为产品/设计评审参考，具体实现请结合仓库当前测试基线与团队排期确认。
