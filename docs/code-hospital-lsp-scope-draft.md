# 符号级 scope（LSP001）草案 · 一二期已落地（2026-11-23）

> 状态：**一期（references 销账）+ 二期（rename 写操作）已落地，回归通过**。
> G6（N999 模块文件名）评估后留待「移动剂型」专项（见 §4）；三期（算法层升级）未做。
>
> 二期落地内容：`textDocument/rename`（prepareRename 先探）→ WorkspaceEdit 翻译为
> unified diff（`lsp/edits.py`）→ 药房新 mode `lsp_rename` 走**同一条**四道校验管线
> （executor 记 `lsp_rename`）；正骨守门从「跨文件引用入待复核」改为「LSP 确认引用
> 集合后扩 scope 入方」——引用文件全部进 scope_paths，scope 关按清单放行。
>
> 二期真实观察（2026-11-23，真 pyright，dry 语义不落盘）：
> `MemoryUnavailable → MemoryUnavailableError` 的机械改名 diff 精确覆盖 3 个文件
> （定义 + memory_port.py + test_memory_port.py），+13/-13，工作区零改动。
> TS 观察（同日，typescript-language-server 对 doms/frontend_dbaudit_aigate）：
> `resolveDbTypeIconKey` references 命中 2 个文件（index.ts 重导出 + 测试），与事实相符。
> Java 不实测（2026-11-23 定论）：jdtls 过重——本体约 500MB，且自身运行需 JDK 21+
>（1.63 起要 25；分析 1.8+ 项目不受影响）。轻量替代盘点无一生产品质：
> georgewfraser/java-language-server 半停更、多模块 Maven 不可信；Apache NetBeans LS
> 同重量级。Java 命名规范强、文本守门误报率低，references 边际收益三语言里最低。
> 注册行保留标注「重型可选」，缺席零成本；远期观察 JDK 23+ 孵化的自带 langserver
> 模块（成熟后「JDK 即仪器」零安装）。
>
> 顺带修复两个存量缺陷：① `hosp_dispense` 的 MCP 路径一直带 `assist` 参数而药房
> 签名没有（83f76fb 起该路径必 TypeError）——药房收下不消费；② `_parse_unified`
> 对末尾换行切出幻影上下文行，锚点校验必炸（生成式 diff 全部带尾换行）。
>
> 本仓真实观察（2026-11-23，pyright 1.1.414，工作区本地安装 .tmp-lsp/）：
> `MemoryUnavailable`（N818）跨文件引用 2 个文件（memory_port.py、
> test_memory_port.py）与事实相符；`_NOTE_CAP` 正确销账（空集）。
> **观察暴露一个方向性缺陷并已修复**：pyright 索引未完成时 references 抢答空结果，
> 而空集语义是销账（最危险方向）。防线两道：didOpen 后等该文件首条
> publishDiagnostics（分析管线跑过的证据，软屏障有宽限）；空集延迟复核一次
> （该语言回过一次非空结果后跳过复核）。另：服务器主动请求必须自动应答
> （workspace/configuration 回全 null），否则 pyright 会吊住。
>
> 拍板记录（2026-11-23）：①内置客户端 + SPI 方向照收，Serena 方案归档；
> ②语言注册表数据外置 `code-hospital.yaml`、内置默认语言照收；
> ③分期照收，一期语言范围修正为 **Python / Java / TS**（Go 较少，走仓级配置）。
>
> 历史：初版草案（2026-11-20）建议外聘 Serena MCP。讨论中被否——用户真实项目是
> TS/Java/Python/Go 多语言仓，Serena 整机太重且 GPL 隔离带始终是隐患；
> 2026-11-23 拍板改为内置方案。Serena 方案归档为不采用。
>
> 问题来源不变：正骨跨文件守门待复核、G5 `__init__.py` 重导出待复核、
> G6（N999）三处积压在等符号级 scope；algorithms.md 两处二期标注
> （touched_symbols 静态区间反查、SUSPECT 判定一期 ast）。

---

## 1. 定型：内置客户端 + SPI，不外聘不 vendor

- **内置的是医院自己写的 LSP 客户端**（stdio 上 JSON-RPC，纯标准库），
  不是 vendor Serena/SolidLSP。LSP 协议开放，零许可风险。
- **语言服务是外部二进制，医院不打包**：pyright（MIT）、jdtls（EPL）、
  typescript-language-server（Apache）由用户自装，开发者机器上本来就有。
  在岗判定复用仪器体系语义：`shutil.which` 查 PATH，不在岗写覆盖账、
  降级文本兜底——「设备未到岗写进覆盖账」。
- Serena MCP 方案归档不采用：多语言需求下整机太重（要 uv、项目索引预热、
  工具面远超需求），GPL 隔离带虽可合规但无必要承担。

## 2. 分层与 SPI

```
src/code_hospital/lsp/
├── client.py      # LSP 会话：initialize / didOpen / references / shutdown
├── registry.py    # 语言注册表（SPI）：语言 → 服务器规格，内置三行 + 仓级扩展
└── pool.py        # 门诊级会话池：按语言懒拉起、复用、诊完统一退
```

语言注册表是**数据不是代码**。内置默认（`catalog/languages.yaml`）：

| 语言 | 扩展名 | 服务器命令 | 根标记 | 备注 |
|---|---|---|---|---|
| python | .py | `pyright-langserver --stdio` | pyproject.toml / setup.py | |
| typescript | .ts/.tsx/.js/.jsx | `typescript-language-server --stdio` | package.json / tsconfig.json | |
| java | .java | `jdtls` | pom.xml / build.gradle | 重型可选：本体约 500MB、自身需 JDK 21+ 运行，缺席零成本 |

用户自定义走被诊仓库 `code-hospital.yaml` 的 `catalog.lsp.languages` 段
（与 `catalog.rename.exempt` 同一「机制内置、数据外置」先例）；命令名只认
PATH 裸名，沿用外聘仪器安全口径。

## 3. 三个关键机制

**生命周期**：门诊级会话池。一次门诊内按语言懒拉起、复用、诊完统一
shutdown/exit；拉起失败或超时标记该语言「本次门诊未到岗」并降级，不炸门诊。
一期不做跨门诊常驻。jdtls 类重设备的启动超时按注册表 `startup_timeout_s` 放宽。

**能力分档**：一期只接 `textDocument/references`（只读）→ 正骨守门销账；
二期 `textDocument/rename` 返回的 WorkspaceEdit **不直接落盘**，翻译成医院
diff 格式进煎药管线——验收、对照、high risk 真人确认纪律不变。LSP 是更准的
取数，煎与验还是医院的。

**降级闭环（三层，每层留痕）**：语言未注册 → 该扩展名不查；已注册但命令不在
PATH → 覆盖账写「<cmd> 未到岗」；拉起失败/超时 → 本轮降级文本守门。
所有层级最终行为与现状一致。

## 4. 分期

| 期 | 内容 |
|---|---|
| ①（已落地） | client + registry + pool，references 三语言；正骨守门销账（跨文件守门 + G5 `__init__.py` 重导出） |
| ②（已落地） | rename 方法 + WorkspaceEdit 翻译 + 药房 mode `lsp_rename`；跨文件引用发现扩 scope 入方。**G6 除外**（见下） |
| ③ | G6 模块文件名（移动剂型专项）；Go 等语言按需入默认注册表；algorithms.md 两处二期标注 |
| ④ | jdtls 重设备策略细化（跨门诊缓存等），按真实观察再议 |

**G6（N999 模块文件名）评估（2026-11-23）：暂不接，留专项。** 文件移动与煎药管线
有三个结构性不兼容：① 锚点关要求目标文件存在，而移动产生的新文件不存在；
② scope 关要求处方列全路径，而 willRenameFiles 的准确编辑清单施治时才返回；
③ 回滚是字节级逆应用，文件移动不在 patch 表达内。三个都指向同一个东西——
药房需要「移动剂型」（move + 编辑 + 快照含移动的一等公民语义），这是独立的
设计草案，不该夹带进符号改名。

## 5. 验收断言

| 断言 | 含义 |
|---|---|
| 缺席不变 | 语言服务未到岗时，守门行为与现状一致（待复核照常） |
| 到岗可查 | 覆盖账能报出各语言服务在岗/未到岗及原因 |
| references 销账 | 在岗且确认无跨文件引用的 rename 发现照常入方 |
| 引用即入方 | LSP 确认跨文件引用的发现入方且 scope 覆盖全部引用文件（二期起，不再待复核） |
| 机械改名同管 | lsp_rename 生成的 patch 过同一条四道校验，executor 记 `lsp_rename` |
| 降级留痕 | 拉起失败/超时进覆盖账，守门回退文本扫描 |
| 会话不泄漏 | 门诊结束后无残留语言服务进程 |
| 索引期不销账 | didOpen 后等首条 publishDiagnostics；空 references 延迟复核一次才信 |

## 6. 明确不做

- 不 import / vendor Serena、SolidLSP；
- 不把 LSP 做成门诊前提——没装语言服务的仓库一切照旧；
- 不接 G6（移动剂型专项）、不接 Go 默认注册、不动 algorithms.md 标注项；
- 不做跨门诊进程常驻。

---

> 拍板记录（2026-11-23）：①内置客户端 + SPI 方向照收，Serena 方案归档；
> ②语言注册表数据外置 `code-hospital.yaml`、内置默认语言照收；
> ③分期照收，一期语言范围修正为 **Python / Java / TS**（Go 较少，走仓级配置）。
