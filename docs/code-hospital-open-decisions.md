# 代码医院 · 未决事项

> 这份文档记录已经拍板的事，以及放行之后仍然缺可执行规则、这一轮没有编造的项。
> 不新增 `hosp_*` 工具。

## 已经拍板

1. **自动档继续跳过验收。** `hosp_treat` 仍传 `acceptance_decision=skip`。
2. **验收失败只退回这一味的快照，药味记失败。** 不自动改签（不作废旧项、不插入新项、不写 `amended_from`）。
3. **不做子进程禁网。** 设计里提过，是因为 `pytest` / `npm test` 会执行仓库里的代码，理论上能把环境里的密钥送出去；Windows 上又没有可移植的办法只掐掉这一个子进程的网络。白名单、不经 shell、环境变量裁剪和超时继续保留。不增加 `acceptance.offline`。
4. **Q1、Q2、Q6、Q7、Q8 维持现在的返回。** 不改成 `pending_question` 卡片。
5. **第二期名册放行。** 见下方「放行后仍缺规则」。本轮只把已经有药名的那一条接上：类型尺发现走续治（`repair`，`llm_patch`）。
6. **rsi-boot 作为基础能力。** 医院进程调用本机 `rsi` 命令，不新开 MCP 工具。开方前若仓库已有 `.rsi/identity.json` 且 `rsi` 在 PATH 上，草稿增加 `rsi_prohibitions`。`distill_to_rsi=true` 时把结局和计数写成 `documentation`，不写发现原文。命令不在时出院说明为「未找到 rsi 命令」，就诊不中断。真机观察补记（2026-11-23）：documentation 落 rsi 正式区即写即可召回（相关性门禁照常）；疗效账 `experience` 被 rsi 归一为 `convention`，按 rsi 治理落**待审区**，审批（rsi 大屏/MCP review）后才参与召回——医院写后查 `knowledge list` 按真实状态出话术，待审时明说「待 rsi 侧审批后参与召回」，不冒充已生效。

## 放行后仍缺规则

这些已经允许做，但名册没有给出可执行的规则、二进制或「什么算仓外」。这一轮不编造：

| 项 | 卡在哪里 |
|---|---|
| 正骨 `rename` | ~~`rule_ids` 为空~~ → 已按动作分组枚举 G1–G4 入册（2026-11-19），**phase 已降 1（2026-11-20）**，G5 import 别名（N811–N814、N817）第二批同日入册：别名是本文件局部绑定不做跨文件守门，`__init__.py` 重导出例外入待复核。「框架约定名」豁免机制内置、数据外置（被诊疗仓库的 `catalog.rename.exempt` 声明符号 glob，命中入待复核）；ruff 适配器从 N8xx 消息反引号提取符号名（G5 取第二对）。降期依据：本仓真实观察 8 处 `visit_*`（N815）全部按豁免入待复核、`MemoryUnavailable`（N818）按跨文件守门入待复核，零误开方 |
| ast-grep | ~~名册写了下一台仪器，没有随包规则。缺席时若塞进默认初检，会让每份报告多一条「未到岗」~~ → 已定（2026-11-24）：降为**候选仪器（无排期）**。接入前提两条：① 不进默认初检，只显式声明启用；② 有 ≥1 个带 `fix` 的随包规则（真成药需求）才写适配器。理由：机械改名已被 lsp_rename 覆盖，Java 热点走 semgrep，当前没有处方在等结构替换 |
| Java 成药执行器 | ~~名册没有指定格式化二进制~~ → 已落地（2026-11-24）：不引格式化二进制，走 `semgrep --autofix`（java-pack 随包规则带 `fix` 才有成药资格，首批 `java.boxing-constructor` / `java.literal-equals-first`，规范类告知级）；执行后按 `scope_lines` 裁剪，与 eslint 同一口径。顺带修存量缺陷：`_rule_id_from_item` 正则此前匹配不到带点/横线的规则号 |
| 设计科仪器 | 没有仪器 id 和规则 |
| LSP 符号范围 | ~~设计写的是外部 LSP；现有范围仍是文件加行区间~~ → 一二期已落地（2026-11-23）：references 销账 + rename 写操作（lsp_rename），见 lsp-scope 草案 |
| LSP 仪器引导安装 | ~~语言服务全靠用户自装；`hosp lsp install` 类引导命令未做，原则未定~~ → 已定（2026-11-24）：**医院永不代下外部二进制，`install` 类命令不做**；安装指引数据外置——`languages.yaml` 每语言带 `install_hint`（仓级 yaml 可加/可覆盖），仪器缺席时随降级提示展示，只展示不执行 |
| Java 轻量仪器 | jdtls 过重（500MB + JDK 21/25 运行）故不实测；JDK 23+ 孵化的自带 langserver 模块成熟后可能「JDK 即仪器」零安装，持续观察 |
| 剂量栈回滚 | 验收失败仍只恢复这一味自己的快照，不按后味先退的栈回滚 |
| 处方疗效统计 | 要有足够出院样本才有聚合意义 |
| MCP elicitation | 人审通道仍是约定级 `convention` |
## 已落地（从本表移出）

- **git worktree 高危隔离（2026-11-23 一期）**：高危药味进 `.hospital/worktrees/` 隔离间预演，验收过后 drift 检查合回主仓并复验；隔离间内任何失败主仓零接触。`safety.worktree`（never/high/always，默认 high）+ `hosp_dispense(isolation=...)` 入参覆盖；依赖目录 junction/symlink 接入清单可配。草案 code-hospital-worktree-draft.md（定稿），测试 tests/test_worktree.py。

- **转诊单（2026-11-19）**：R1 仓外 import（分层尺发 `layer.external-import`，架构科待复核不开归位药）/ R2 供应链发现点名依赖 / R3 宿主交卷自带 / R4 复发 ≥3 且记忆层指向仓外。单子带 `rule`/`finding_ids`/`resolved_path`/`local`；`local=true` 可 `hosp_register(referred_from=...)` 直接转诊，建档写 `case_links(consultation)` 双向可查；出院小结单列「转诊」一节。契约见 io-contracts §2.2/§3.1/§3.7。
