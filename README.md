# Code Hospital 代码医院

中文 | [English](README_EN.md)

**代码医院（Code Hospital）是一个开源、本地优先的 MCP（Model Context Protocol）服务器，面向 AI 代码评审（code review）与可验证的自动修复（verified auto-fix）。** 它把 ruff、semgrep、gitleaks 等确定性静态分析工具编排进一条带阶段门禁（human-in-the-loop stage gates）的「医院流程」：挂号 → 检验 → 诊断 → 开方 → 施治 → 出院。每次 AI 改动都有可审计的病历、人工审批闸门和可验证的差分。主链路零 API Key，支持 Cursor、Qoder、Codex 等任意 MCP 宿主。

把代码送到医院：先体检、再诊断、开处方、按方修码、护士全程盯防、复查确认才出院——每一步都有报告。AI 修代码不再是「赤脚医生模式」（看两眼就动手、改完不验证、越改越偏），而是按**病历、处方、护理记录**行医的正规医院：**有处方可查、有边界可控、有差分可证**，数据只落当前仓库的 `.hospital/`。

> 当前版本 v0.1：挂号 / 检验 / 诊断 / 开方 / 成药与煎药提交 / 出院门禁 / 病例查询已可用；补验与冲突续行仍按[路线图](#路线图)开放。
> 用户向术语手册见 [`docs/code-hospital-glossary.md`](docs/code-hospital-glossary.md)；设计契约见 [`docs/code-hospital-design.md`](docs/code-hospital-design.md)；IO 契约见 [`docs/code-hospital-io-contracts.md`](docs/code-hospital-io-contracts.md)；算法与建模设计见 [`docs/code-hospital-algorithms.md`](docs/code-hospital-algorithms.md)。运行时实现在 `code_hospital.core.algorithms`（`docs/algorithms_reference.py` 只是自检入口）。

## 目录

- [核心特性](#核心特性)
- [安装](#安装)
- [接到 Cursor](#接到-cursor)
- [接到其他宿主](#接到其他宿主)
- [在对话里就诊](#在对话里就诊)
- [终端冒烟](#终端冒烟)
- [MCP 工具](#mcp-工具)
- [路线图](#路线图)
- [配置](#配置)
- [开发](#开发)
- [常见问题（FAQ）](#常见问题faq)
- [相关项目](#相关项目)

## 核心特性

- **阶段门禁的 AI 工作流（human-in-the-loop）**——每个阶段产出产物后挂起等你确认，高危处方项必须人工批准，任何改动都不会静默落地。
- **确定性仪器，不靠玄学**——检验跑真实静态分析（Python 用 ruff，另有 semgrep、gitleaks）；缺设备自动降级并写进报告「覆盖与限制」，不阻塞流程。
- **可审计的处方与施治**——按已批准处方逐味施治；宿主模型补丁过四道校验才落盘；每处改动可逆应用回滚。
- **可直接贴 PR 的报告**——Markdown 检验报告带严重度分级与覆盖说明，直接贴进 Pull Request 评论。
- **复发追踪**——指纹关联历史病例，每条发现标注「初发」或「复发×N」（含首次病例与当时结局）。
- **本地优先、隐私可控**——主链路零 API Key、零云端调用；所有数据只落本仓库 `.hospital/`（自动加入 git exclude）。
- **口径归仓库所有**——被诊疗仓库的 `code-hospital.yaml` 决定什么算病：结构阈值、忽略规则与前缀、严重度下限。

## 安装

需要 Python 3.10+。在本仓库根目录：

```bash
python -m venv .venv
.venv\Scripts\pip install -e .     # Windows
# .venv/bin/pip install -e .       # macOS / Linux
```

扫描设备按需安装（未安装的设备自动降级，在报告「覆盖与限制」中注明，不阻塞流程）：

```bash
.venv\Scripts\pip install ruff     # Python 检验（推荐，P0 主力设备）
# semgrep / gitleaks 见各自官方安装方式
```

## 接到 Cursor

在**要诊疗的那个项目**的 `.cursor/mcp.json` 加（指向本仓库 venv 里的 hosp）：

```json
{
  "mcpServers": {
    "code-hospital": {
      "command": "D:/project/study/code-hospital/.venv/Scripts/hosp.exe",
      "args": ["serve"],
      "env": {
        "HOSP_PROJECT_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```

然后 Settings → MCP 确认 `code-hospital` 已启用。改过本仓库代码后要重载该 MCP。

工作区绑定顺序：`HOSP_PROJECT_ROOT`（项目级 mcp.json，推荐，显式契约）→ 进程 cwd。不要配用户级 `mcp.json`：`${workspaceFolder}` 在那里经常不展开，会把病历串到别的仓库。

## 接到其他宿主

手动接入（Qoder / Codex 等）使用根目录 [`mcp.json`](mcp.json) 同款配置：

```bash
# Qoder
qoder mcp add code-hospital -- hosp serve

# Codex（~/.codex/config.toml 或项目级 .codex/config.toml）
# [mcp_servers.code-hospital]
# command = "hosp"
# args = ["serve"]
```

从项目根目录启动即可——MCP 子进程继承 cwd，代码医院按 cwd 认领工作区。要显式钉死就加环境变量 `HOSP_PROJECT_ROOT=/绝对/路径`。

## 在对话里就诊

接好 MCP 后对当前项目窗口里的代理说即可：

| 你说 | 代理做的 |
|------|----------|
| 帮我看看这次改动有没有问题 | `hosp_register`（scope=staged）→ **给你挂号单确认** → 你确认后 `hosp_scan` → 给你检验报告 |
| 给这个仓库做个体检 | `hosp_register`（scope=repo）→ **确认挂号单** → `hosp_scan` |
| 这个 PR 能合吗 | scope=range（如 `main..HEAD`）→ 逐步确认 → 检验报告直接贴 PR 评论 |

**叫号确认（阶段门禁）**：和真实医院一样，每个阶段产出产物后流程会**挂起等你确认**——挂号单出来等你确认进诊室，检验报告出来等你确认再诊断，每味药服完等你确认再下一味。你不确认，医院绝不一步到位全弄完。想免叫号一口气跑完（CI 冒烟、一键体检），挂号时声明全权委托（`auto_advance=true`）即可。

医院在不确定时也会**停下来问你**（范围不明、撞单、高危项、疗程上限等十个节点），不会闷头执行；你不回答时一律按安全侧处理（不批准/不放行/转人工）。

## 终端冒烟

不经对话、只调 CLI 时用：

```bash
hosp check                          # 对暂存区（staged）挂号+检验
hosp check --scope-type repo        # 全仓体检
hosp check --scope-type range --range main..HEAD
hosp check --force                  # 已有活跃病例时先转诊再挂
hosp check --json                   # 完整 JSON 出参
hosp serve                          # 一般由宿主拉起，不必手跑
```

报告产物落 `.hospital/reports/<case_id>/`（检验报告可直接贴进 PR 评论），补丁存档落 `.hospital/patches/`；`.hospital/` 已自动加入 `.git/info/exclude`，不污染 git 状态。**二次入院不从头来**：检验时按指纹自动关联本仓库历史病例，检验报告「发现明细」的「病史」列直接标出每条发现是「初发」还是「复发×N」（含首次病例与当时结局），`.rsi` 项目记忆同页附注（未经证实档）。

## MCP 工具

| 工具 | 干什么 | 状态 |
|------|--------|------|
| `hosp_register` | 挂号建档：圈定范围（repo/paths/staged/commit/range）、轻量预检分诊、病例查重；一仓一活跃病例 | ✅ P0 |
| `hosp_scan` | 检验：调度确定性设备（ruff/semgrep/gitleaks），归一化指纹 findings，产出 PR 可贴报告 | ✅ P0 |
| `hosp_case` | 病例查询：列表/详情/检验发现/报告原文 | ✅ P0 |
| `hosp_diagnose` | 坐诊：指纹聚类、模板归因、病史调档；确认检验报告后才执行 | ✅ 模板归因 |
| `hosp_prescribe` | 开方：有序草稿 + 审批闸门（高危必须本人确认）。不改代码 | ✅ 草稿与审批 |
| `hosp_dispense` | 施治：成药（ruff --fix）逐味改代码；煎药取令牌后 `submit_patch` 过四道校验才落盘；外购跳过 | ✅ 成药与煎药提交 |
| `hosp_discharge` | 复诊差分 + 出院门禁会签 + 随访计划。有医源性新症且疗程未满则再入院 | ✅ 复诊与门禁 |
| `hosp_treat` | 一键全程（自动档）：低危自批，高危降级为外购；煎药停在 `need_patch` | ✅ 编排 |

## 路线图

- **P0 PR 门诊（当前）**：只读链路，检验报告贴 PR。
- **P1 门诊闭环**：诊断 + 处方审批 + 成药施治（autofix）+ 出院门禁 + 逆应用回滚。
- **P2 住院部**：煎药协议（宿主模型补丁，四道校验）、漂移控制、多轮疗程、`hosp_treat`、会诊转诊单、随访计划。
- **P3 医养结合**：rsi-boot 联动（用药禁忌召回、疗效经验蒸馏）、符号级 scope、worktree 高危隔离。

## 配置

主链路零配置可用。可选环境变量：

| 变量 | 用途 |
|------|------|
| `HOSP_PROJECT_ROOT` | 显式钉死工作区（推荐在项目级 mcp.json 里配） |

**扫描忽略（三层）**：① repo scope 枚举走 `git ls-files`，原生尊重 `.gitignore`；② 内置排除目录（`node_modules`/`.venv`/`__pycache__`/`dist` 等，全 scope 生效）；③ 项目级自定义——在**被诊疗仓库**根目录建 `code-hospital.yaml`：

```yaml
# 这些路径在任何 scope 下都不参与扫描（fnmatch，仓库相对路径）
scan_ignore:
  - "gen/**"           # 生成代码
  - "**/*_pb2.py"      # protobuf 产物
  - "migrations/**"
```

被忽略的文件数会写进检验报告「覆盖与限制」，跳过不等于看不见。

**什么算病**也写在同一份 `code-hospital.yaml`。结构尺默认函数 80 行、文件 500 行、圈复杂度 15，写成 `0` 就关掉这把尺。规则号或前缀放进 `catalog.rules.ignore` 后，初检直接丢掉。完整例子在 `code-hospital.yaml.example`。生效口径写在检验报告的「覆盖与限制」里。

病历、报告、补丁都在各仓库自己的 `.hospital/`，打开另一个仓库就是另一份，不会串。

## 开发

```bash
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

工程约定：适配器只产原始发现，归一化（指纹/净化/锚点）统一在检验科；设备执行失败进「覆盖与限制」不中断其他设备；测试用 `adapters.register()` 注入替身。

## 常见问题（FAQ）

**代码医院是什么？**
一个开源、本地优先的 MCP 服务器，把 AI 代码评审与自动修复变成带阶段门禁的医院流程——挂号、检验、诊断、开方、施治、出院——每个关口都有可审计记录与人工确认。

**和直接跑 ruff / semgrep 有什么区别？**
ruff、semgrep 是仪器，代码医院是医院。它负责调度仪器、把发现归一化成带指纹的档案、跨扫描追踪复发、产出可直接贴 PR 的报告；更重要的是，所有改代码的动作都必须有已批准的处方，改完还要复诊差分验证——裸 linter 不管这些。

**需要 API Key 吗？代码会上云吗？**
不需要，不上云。主链路完全本地、零 API Key。所有数据写在被诊疗仓库的 `.hospital/` 目录，并自动加入 git exclude。

**支持哪些语言和工具？**
P0 主力是 Python（ruff）；semgrep、gitleaks 作为可选设备接入。缺设备自动降级并在报告「覆盖与限制」中注明，流程绝不因缺工具而中断。

**它会不问我就改代码吗？**
不会。默认每个阶段门禁都挂起等你确认，高危处方项必须人工批准。CI 或一键体检可声明 `auto_advance=true`：此时只自批低危项，高危项一律降级为「外购」跳过。

**怎么接到 Cursor 或其他 MCP 宿主？**
在被诊疗项目的 `.cursor/mcp.json` 加一段指向 `hosp serve` 的配置（见[接到 Cursor](#接到-cursor)）；Qoder、Codex 等任意 MCP 宿主注册同一条 `hosp serve` 命令即可。

**报告、补丁和病历存在哪？**
各仓库自己的 `.hospital/` 下：报告在 `.hospital/reports/<case_id>/`，补丁存档在 `.hospital/patches/`。打开另一个仓库就是完全独立的另一份病历。

## 相关项目

[rsi-boot](../rsi-boot)：同一作者的本地记忆层 MCP。`.rsi/memory/` 里的项目记忆（约定/禁止项）会作为**既往史**出现在检验报告与 `hosp_scan` 出参的 `memory_context` 中——分 official（已经审批）/ pending（未审批）两档信任级，统一标注「可能过时或错误，仅供坐诊参考，不构成事实」。P3 起另有 MCP 级联动（开方前召回用药禁忌、出院后蒸馏疗效经验），不装 rsi-boot 也可独立使用。
