# Code Hospital 代码医院

把代码送到医院：先体检、再诊断、开处方、按方修码、护士全程盯防、复查确认才出院——每一步都有报告。

代码医院是一个编排型代码诊疗 MCP。AI 修代码不再是「赤脚医生模式」（看两眼就动手、改完不验证、越改越偏），而是按**病历、处方、护理记录**行医的正规医院：**有处方可查、有边界可控、有差分可证**。本地优先，主链路零 API Key，数据只落当前仓库的 `.hospital/`。

> 当前版本 v0.1（P0 · PR 门诊）：挂号 / 检验 / 病例查询三个只读工具已可用；诊断、处方、施治、出院门禁按[路线图](#路线图)陆续开放。
> 用户向术语手册见 [`docs/code-hospital-glossary.md`](docs/code-hospital-glossary.md)；设计契约见 [`docs/code-hospital-design.md`](docs/code-hospital-design.md)；IO 契约见 [`docs/code-hospital-io-contracts.md`](docs/code-hospital-io-contracts.md)。

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
| 帮我看看这次改动有没有问题 | `hosp_register`（scope=staged）→ `hosp_scan` → 给你检验报告 |
| 给这个仓库做个体检 | `hosp_register`（scope=repo）→ `hosp_scan` |
| 这个 PR 能合吗 | scope=range（如 `main..HEAD`）→ 检验报告直接贴 PR 评论 |

医院在不确定时会**停下来问你**（范围不明、撞单、高危项、疗程上限等十个节点），不会闷头执行；你不回答时一律按安全侧处理（不批准/不放行/转人工）。

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
| `hosp_diagnose` | 坐诊：聚类归因、病史调档、会诊转诊单 | P1 |
| `hosp_prescribe` | 开方：有序修复计划 + 审批闸门（高危必须真人） | P1 |
| `hosp_dispense` | 施治：唯一改码工具，护理核查内嵌（漂移即逆应用回滚） | P1 |
| `hosp_discharge` | 复诊差分 + 出院门禁会签 + 随访计划 | P1 |
| `hosp_treat` | 一键全程（自动档） | P2 |

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

病历、报告、补丁都在各仓库自己的 `.hospital/`，打开另一个仓库就是另一份，不会串。

## 开发

```bash
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

工程约定：适配器只产原始发现，归一化（指纹/净化/锚点）统一在检验科；设备执行失败进「覆盖与限制」不中断其他设备；测试用 `adapters.register()` 注入替身。

## 相关项目

[rsi-boot](../rsi-boot)：同一作者的本地记忆层 MCP。`.rsi/memory/` 里的项目记忆（约定/禁止项）会作为**既往史**出现在检验报告与 `hosp_scan` 出参的 `memory_context` 中——分 official（已经审批）/ pending（未审批）两档信任级，统一标注「可能过时或错误，仅供坐诊参考，不构成事实」。P3 起另有 MCP 级联动（开方前召回用药禁忌、出院后蒸馏疗效经验），不装 rsi-boot 也可独立使用。
