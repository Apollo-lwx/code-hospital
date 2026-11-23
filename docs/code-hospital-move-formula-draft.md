# code-hospital 移动剂型专项设计草案（G6 模块文件名 / N999）

> 定位：正骨 G6（模块文件改名）的前置专项。2026-11-23 评估（lsp-scope 草案 §4）
> 认定文件移动与煎药管线有三个结构性不兼容，当时的结论是「留专项，不夹带进
> 符号改名」。本草案就是这个专项：回答药房怎么长出「移动剂型」——
> **move + 编辑 + 含移动语义的快照，三者都是一等公民**。
> 本草案只定设计，拍板后才动名册 YAML 与代码。

---

## 0. 三个结构性不兼容 → 三个设计决定

| 不兼容（现状机制） | 设计决定 |
|---|---|
| ① 锚点关要求目标文件存在（`validate_patch` 的 anchor：`files.get(path)` 为 None 即 NotFound），而移动产生的新文件尚不存在 | 锚点关开**移动特例**：MOVE 操作的旧路径必须存在、新路径必须**不存在**（撞名即拒）；编辑 hunk 照旧唯一匹配 |
| ② scope 关要求处方开列全部受影响路径，而 `willRenameFiles` 的准确编辑清单施治当下才返回 | scope 推导**前移到开方**：LSP references 先定引用集合，`scope_paths = {旧路径, 新路径} ∪ 引用文件全集`；施治时 LSP 实际触及的文件 ⊆ scope 才放行，越出即拒（next_action 改签处方）——与 lsp_rename 的「引用集合扩 scope」同一条裤子 |
| ③ 回滚是 patch 字节级逆应用，文件移动不在 unified diff 的表达力内 | 快照升 **schema v2**：`{path: content \| null}`，null = 施治前该文件不存在。回滚 = 有内容的写回 + null 的删除 + 空目录修剪，恢复后逐文件字节级比对断言 |

三个决定互相咬合：①②让移动能过校验，③让移动可后悔。

## 1. 剂型定义

- **payload 是结构化的，不走文本 patch**：`{move: {from, to}, edits: [hunk...]}`。
  lsp_rename 走「WorkspaceEdit → unified diff → parse 回来」，是因为符号改名的
  编辑天然是文本；移动不是。把 move 硬翻成文本再解析回来是有损的绕路，
  本剂型直接持结构化操作。
- **新 mode**：`hosp_dispense(mode="lsp_move")`，executor 记 `lsp_move`。
  与煎药（llm_patch）、机械改名（lsp_rename）共用同一条四道校验管线
  （`_apply_patch_pipeline` 的移动变体），验收 / 剂量记录 / 护理核查 /
  处方报告全部复用，不重开一条管。
- **适用面**：只接正骨 G6（`N999`）入方项，其余规则用不了这个 mode——
  沿用 lsp_rename 的适用面检查惯例（「机械移动只适用于正骨 G6 煎药味」）。
- 单味药一次移动：`from → to` 一对一。一个文件一味药，不并体；
  批量（同规则 ≥20 个文件）按正骨惯例合并为一味外购批量迁移（manual）。

## 2. 开方侧（N999 怎么入方）

- **入方前提**：LSP 到岗，**且** 语言服务声明支持
  `workspace.fileOperations.willRenameFiles`（LSP 客户端新增能力探测，
  探测结果进覆盖账）。缺席或不支持 → 维持现状：N999 不入方、留待复核写明
  LSP001 / 能力缺失。**不手搓文本推导的 import 重写**——那正是 LSP 一期
  立项时拒绝过的路，移动比改名更错不起。
- **新名推导**：N999 消息给出非法模块名，按 snake_case 机械映射
  （与 G1–G5 同一条「新名字有机械判据」原则，宿主模型只做现名 → 合规名映射）。
  用户可在施治时 override（沿用 lsp_rename 的 `new_name` 入参惯例，这里换成
  `new_path`，只允许改文件名部分——目录归属不在本剂型内，见 §8）。
- **scope 推导**：references（模块符号 + import 路径）→ 引用文件集。
  无引用 → `scope_paths` 只列新旧两路径；有引用 → 新旧路径 + 引用文件全列。
  与 G1–G5 的守门销账口径一致，LSP 缺席无降级——直接不入方。
- **处置矩阵联动**：N999 是命名规范（不合规不造成功能错误），入方时应同步
  把 `N999` 加进内置默认表 convention——与 N8 族同一判据。默认 advisory、
  `include_advisory` 点名才治；仓库可配置放宽，放宽风险写进药味说明。
  （这是一期实施时的配套拍板项，先记在这里。）

## 3. 药房侧（mode="lsp_move"）流程

1. **生成**：不调 need_patch（移动不是模型写 patch）。客户端向语言服务发
   `willRenameFiles(old_uri → new_uri)`，收回 WorkspaceEdit，规格化为
   §1 的结构化 payload。
2. **规格化的坑（到岗验证第一件事）**：各语言服务对返回编辑的键法不同——
   被移动文件自身的编辑可能键旧 URI 也可能键新 URI。规格化统一成
   「对被移动文件**内容**的编辑」，与路径键法解耦；每个语言服务的实测键法
   留台账（进覆盖账），键法意外时拒绝施治而不是猜。
3. **四道校验**：见 §4，全部过完才落盘。
4. **落盘顺序（崩溃安全）**：① 编辑后的旧内容写入新路径 →
   ② 引用文件编辑落盘 → ③ 删旧路径。顺序保证「旧文件活到最后」：
   前两步任何一步失败，仓库还是原样；第 ③ 步后失败有快照兜底。
5. **验收 → 快照 → 剂量**：acceptance alias 照旧；快照按 §5 的 v2 写入；
   剂量记录的 patch_path 存结构化 payload 的 JSON + 给人看的渲染 diff
   （`rename from / rename to` 风格的 git 头部 + 编辑 diff）。
6. **dry_run**：全量推演不落盘，返回移动 + 编辑的 diff 预览，口径同现有。

## 4. 四道校验的移动特例

| 关 | 现状 | 移动特例 |
|---|---|---|
| format | 补丁非空、≤500 行 | 编辑 hunk 照旧；MOVE 操作本身不计行数 |
| anchor | search 唯一匹配、目标文件必须存在 | 编辑 hunk 照旧；MOVE：旧路径必须存在、新路径必须不存在（撞名即拒，next_action 改名或改签）、新路径父目录在 root 内可建 |
| scope | hunk 路径 ⊆ scope_paths | 编辑 hunk 照旧；MOVE 的 from 与 to **都**必须在 scope_paths 内（新路径开方时已列，施治时不得冒出 scope 外的新文件） |
| content | 禁止项、未声明依赖清单 | 编辑 hunk 的 replace 体照旧；新增：to 路径扩展名必须等于 from（防「改名顺手改类型」）、to 不落 .hospital |

四道关一道不少，只是每道对 MOVE 开一个特例——这是专项的全部本质，
也是当初不把 G6 夹带进符号改名的原因：特例开在通用管线里会污染 G1–G5
的判定，开在自己的剂型变体里各自干净。

## 5. 快照与回滚（schema v2）

- **格式**：`{path: content | null}`。null = 施治前不存在（移动产生的新文件）。
  旧快照（全 content）向后兼容，读取侧兼容两种。
- **恢复**：content → 写回；null → 文件存在即删，随后修剪空父目录
  （逐级向上，收到 project_root 为止，绝不动 root 本身）。
- **恢复断言**：恢复后逐文件与快照字节级比对（现有「回滚必须字节级可验证」
  的延伸），且所有 null 路径在恢复后必须不存在——缺一即 rollback_conflict，
  走现有的挂起 + resolve_conflict 续行流程。
- **续行兼容**：resolve_conflict / manual_resolved 的现状比对
  （`_manual_block`）同样要学会 null 语义——范围内的人工改动可收下，
  越界行不悄悄裁掉的口径不变。

## 6. 验收与复扫

- acceptance alias 照旧跑：移动重写全仓 import，验收测试是第一道网。
- 复扫口径：该规则（N999）发现数应为 0（沿用正骨批量迁移的验收口径惯例）。
- **已知盲区（必须写进药味 instructions）**：字符串 import、`importlib`
  动态加载、模板 / 配置文件里的模块路径——LSP references 覆盖不到，
  药方 instructions 点名提醒人工核。这是「机械移动」与「真重构」的分界线，
  越线的部分永远归人。
- 可选增强（一期不做）：施治后用 references(新模块) 与事前引用集合对账，
  数量不符即告警。

## 7. 分期与验收断言

- **一期**：python（pyright）单文件移动；`N999` 同步入默认表 convention。
- **二期**：ts / java 按各语言服务 `willRenameFiles` 实测到岗情况逐个开；
  实测矩阵（pyright / typescript-language-server / jdtls）是一期到岗验证的
  第一项任务，结果回填本节。
- jdtls 重设备策略沿用 lsp-scope 草案 §4 的既有记录，不在这里重开。

| 断言 | 含义 |
|---|---|
| 缺席不变 | LSP 未在岗或不支持 willRenameFiles 时，N999 行为与现状逐字节一致（不入方、待复核写明原因） |
| 到岗可查 | 覆盖账能报出各语言服务的 fileOperations 能力与实测键法 |
| 锚点特例 | 新路径撞名即拒；旧路径不存在即拒；编辑 hunk 照旧唯一匹配 |
| scope 特例 | 移动与编辑全部落在开方 scope_paths 内；LSP 实际触及越出即拒并指路改签 |
| 落盘有序 | 旧文件活到最后；前两步失败仓库保持原样 |
| 回滚字节级 | 恢复后逐文件与快照一致、新路径不存在；缺一即 rollback_conflict |
| 降级留痕 | 拉起失败 / 键法意外 / 超时进覆盖账，发现维持待复核 |
| 会话不泄漏 | 门诊结束后无残留语言服务进程（沿用 LSP 基座既有断言） |

## 8. 明确不做

- 目录移动（整包改名）、批量移动（≥20 合并外购 manual）、跨仓移动；
- 无 LSP 的文本推导移动（import 重写不手搓）；
- 移动顺手做别的：改目录归属、改文件类型、顺手格式化——单味单效；
- git mv / 历史保留：医院落盘是普通文件操作，git 历史跟着 rename
  detection 走，不专门保；要保历史请人工 git mv 后绕开医院。

## 9. 开放问题

- 各语言服务 `willRenameFiles` 能力实测矩阵（pyright / tsls / jdtls）——
  一期到岗验证第一件事，结果回填 §7；
- `N999` 入默认表 convention 的拍板与实施同步进行（§2 已记判据）；
- 移动后 references 对账（§6 可选增强）按真实观察再议。
