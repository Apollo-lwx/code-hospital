# 设计科专项设计草案

> 状态：草案待拍板（2026-11-24）。
> 背景：放行清单里「设计科仪器」一条不能销项——设计规范审查是医院的原始承诺
> （7 大设计原则 + 23 种设计模式），且提示包内容已存在，缺的是接诊链路。

## 1. 现状证据链

| # | 事实 | 位置 |
|---|---|---|
| 1 | 原则提示包已存在：SRP/OCP/LSP/ISP/DIP/迪米特/组合优于继承 7 问，配 `design.signals` 代码执行器先抽结构事实再交 LLM 判断 | `catalog/check_packs/principles.yaml` |
| 2 | 模式识别包已存在：23 种 GoF 候选，纪律写明「只认已存在且压住复杂度的模式，不因为没用某模式就算病」 | `catalog/check_packs/patterns.yaml` |
| 3 | 两个包挂在**架构号别**下排程（同号还有并发/序列化/命名/访问包） | `check_packs/visits.yaml` |
| 4 | 交卷发现被压平：`rule_id` 统一写 `diff.issue`、`tool=host`，清单项 id 只留在 `check_id` 侧记；分流表没有 `host` 的行 → 落全科 → 配不上药 → 待复核，设计科从不出现在诊断书 | `departments/diagnostician.py`、`catalog/filing.py`（`stamped["check_id"]`）、`catalog/formulary.py` `_ROUTES` |
| 5 | 设计科专科在册、药柜 `[rename, dedupe, split, contrast]` 齐备但空转 | `catalog/specialties.yaml` |

结论：设计科的内容（原则/模式清单）已在，链路（发现身份 → 分流 → 药柜）断了三截。

## 2. 设计方案

### D1 发现身份：路由用 `check_id`，存储身份不动

- 发现入库时 `rule_id` 维持 `diff.issue`（查重、疗效账、台账兼容面不动）；
- 分流与配药改用侧记的 `check_id`（`principle.srp` 等）：`route()` 与开方匹配看到 `tool=host` 且带 `check_id` 的发现时，按 `check_id` 查表。
- 实现面：`formulary.route` 加一个可选的 `check_id` 入参；诊断聚类与开方各传一次。

### D2 分流表加两行

```python
("host", "principle.", "design"),
("host", "pattern.", "design"),
```

`diff.issue` 无 `check_id` 前缀命中时维持落全科（现状不变）。

### D3 号别：两个取向（**待拍板**）

- **取向甲（推荐）**：新设 `design` 号别，排 `principles` + `patterns` 两个包；架构号别把这两包摘下（它还留着并发/序列化/命名/访问，语义反而更纯）。代价：架构号的排程变少是行为变更，旧病例的架构号覆盖账口径要在迁移说明里写一句。
- **取向乙**：维持架构号排程不动，只改分流。零行为变更，但「看设计」这件事永远寄居在架构号里，与设计科坐诊的语义不符。

### D4 药柜映射（一期只接有把握的）

| 清单发现 | 药 | 理由 |
|---|---|---|
| `principle.srp`（一类多变化原因） | 拆分 | 修法是语义重构，煎药（llm_patch）正合适 |
| `principle.demeter`（链式深入） | 拆分 | 同上 |
| `pattern.recognize`（已识别模式） | 对照 | 信息级陈述，不是病 |
| 其余原则项（ocp/lsp/isp/dip/composition） | 暂不开药，待复核 | 没有机械判据也没有安全的煎药模板，不硬凑 |

### D5 处置矩阵：全族告知级

`principle.*`、`pattern.*` 收入默认表 `cls="convention"`——设计规范违例不造成功能错误，默认只告知，`include_advisory` 点名才施治（与用户拍板的「规范不自动改」铁律一致）。

## 3. 分期

- **一期**：D1 + D2 + D4 + D5——设计科开始坐诊，架构号排程不动（取向乙先行）。验收：架构号交卷写出 SRP 违例 → 诊断书出现设计科 → 药柜开拆分（告知级）→ 点名后煎药走通。
- **二期**：D3 取向甲（design 号别独立），视一期使用反馈再定。
- **三期**：设计重构规则包（机检 god class / feature envy 等），等真实需求信号，候选方向 PMD design 规则集或 LSP 度量聚合。

## 4. 验收（一期）

1. 架构号交卷 `principle.srp` 有问题项 → 发现分流到设计科，诊断书科室列出现「设计科」；
2. 拆分药落告知级，不随处方自动执行；`include_advisory` 点名后煎药流程走通；
3. 无 `check_id` 的 `diff.issue` 发现路由不变（全科），存量病例兼容；
4. 疗效账/查重对压平 rule_id 的行为逐字节不变。
