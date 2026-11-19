# 会诊转诊单（referrals）定义草案 · 已定稿落地（2026-11-19）

> 状态：**已落地**。R1–R4 生成、可及判定、`hosp_register(referred_from=...)` 直接转诊、
> `case_links(consultation)` 双向链、出院小结「转诊」一节均已实现；
> 契约升版见 io-contracts §2.2（referrals 八字段）、§3.1（referred_from）、§3.7（include=links）。
> 测试：tests/test_referrals.py（19 例）。本文件留档备查，不再作为待办。
>
> 原状态说明：解决的是 open-decisions「放行后仍缺规则」表里的这一行：
> 「转诊单：『什么发现算指向仓外』还没定。`referrals` 仍是空列表」。
> 上位承诺：design §4.8（P2 住院部「会诊转诊单」，一期只做转诊）。
> 本文件只定义触发规则与数据结构，拍板后才动代码；不动病例状态机，不新增工具。
>
> 修订（2026-11-19）：**目标仓在当前工作区可及时，允许直接转诊并开始诊治**——
> 宿主拿单子去目标仓挂号开第二个病例，不再停在纸面。

---

## 1. 转诊单是什么

诊断书上的一条声明：**这个问题的主战场不在这个仓库**。它把「本院治不了」从
「没查出来」里分出来——前者是诚实的能力边界，后者是漏诊。

三条纪律：

1. **单子分两种去向**：
   - **目标仓在工作区可及**（`target_repo` 解析为本地存在的仓库目录）→ 单子可执行：
     宿主拿它去目标仓 `hosp_register` 开第二个病例，直接开始诊治；
     `case_links(relation="consultation")` 把两个病例串起来，链路可溯（design §4.8 一期口径）。
   - **目标仓不可及**（路径不存在、或是 URL/远程服务）→ 单子只出纸面，
     拿着它去那个仓库挂号是人的动作。
2. **医院不自动开病例。** 直接转诊由宿主/用户发起（调 `hosp_register`），
   医院不自己建档——不叫号不进诊室，对目标仓同样成立。
3. **每条转诊单必须挂到具体发现。** 没有 `finding_ids` 的转诊单等于
   「我觉得你有病但说不出哪疼」，不写。
4. **转诊不是失败。** 有转诊单的病例可正常出院，`outcome=referred` 是既有枚举值。

「可及」的判定（刻意保守）：

- `target_repo` 只接受**本地文件系统路径**（相对 `project_root` 或绝对）；URL 一律不可及；
- 解析后目录存在，且可作为独立 `project_root` 建档（目标仓自己的一仓一病例约束照常适用）；
- 解析结果原样写进单子的 `resolved_path`，医院不做进一步规范化——它不认识那个仓库的内部。

## 2. 触发规则（什么发现算指向仓外）

满足其一即生成一条转诊单。四条都可机械判定，不需要读懂语义：

| # | 规则 | 信号来源 | 判定方式 |
|---|---|---|---|
| R1 | **仓外依赖违反**：分层尺/依赖分析发现 import 目标解析到仓库根之外的本地路径 | layer-ruler / 依赖解析 | import 目标路径不在 `project_root` 内且不是已安装包 |
| R2 | **主战场在上游的供应链发现**：已知漏洞或废弃依赖，修复动作是升级/替换依赖而非改本仓代码 | supply-ruler | 发现的修复面是依赖清单条目，且 `hold=true`（进货药） |
| R3 | **宿主交卷点名仓外**：合入门诊的 `diff.issues` 或诊断问答里，宿主明确写出仓外文件/服务路径 | 宿主交卷 | 交卷条目含 `project_root` 之外的路径，或含明示的外部服务名 |
| R4 | **反复复发的仓外根因**：同一 content_key 复发 ≥3 次，且记忆层（rsi）有记录指向仓外根因 | 疗效账 + 记忆端口 | 复发计数由疗效账出；根因指向由记忆层召回，医院不自己判断 |

**明确不触发**的：怀疑但没证据（「这个函数可能被别的仓调用」）、
远程服务不可达、HTTP API 的行为问题。这些留在限制项或待复核，不升级成转诊单。

## 3. 数据结构

诊断行上既有字段 `referrals`（design §5.2 已定 `{target_repo, evidence, hypothesis, suggested_triage}`，默认 `[]`）从恒空变为：

```json
{
  "target_repo": "../shared-core",
  "evidence": "分层契约 api→departments 的违例 import 指向仓外路径",
  "hypothesis": "共享层改动的主战场在 shared-core",
  "suggested_triage": "outpatient",
  "rule": "R1",
  "finding_ids": ["fnd_..."],
  "resolved_path": "D:/work/shared-core",
  "local": true
}
```

- 前四个字段沿用 design §5.2 既有 schema，不改契约形状；
- `rule`：R1–R4，转诊原因可倒查；`finding_ids`：挂载的具体发现；
- `resolved_path` 与 `local`：§1 的可及判定结果。`local=false` 时 `resolved_path=""`
  （URL 或不存在），单子只能纸面流转；
- 一条发现最多进一条转诊单；同一 `target_repo` 的多条单在出院小结里合并展示。

## 4. 落地位置

| 位置 | 动作 |
|---|---|
| `hosp_diagnose` | 诊断时按 R1–R4 现算，写进诊断行的 `referrals`；可及判定在这一步做 |
| 直接转诊 | 宿主对 `local=true` 的单子调 `hosp_register(project_root=resolved_path, referred_from=<源 case_id>)`；建档即写 `case_links(relation="consultation")` 双向可查。**契约升版项**：`hosp_register` 增加可选入参 `referred_from`（仅此一个字段，不开新工具） |
| 出院小结 | 单列「转诊」一节；`outcome` 不受影响，除非整案无一本院可治（既有 `referred` 口径） |
| `hosp_case` | `include="links"` 已能查会诊链，不新增查询面 |
| 契约 | io-contracts §2.2 Diagnosis 的 `referrals` 字段从「恒空」改为本结构（向后兼容的字段填充），§3.1 增加 `referred_from`，同批升版 |

## 5. 验收断言

| 断言 | 含义 |
|---|---|
| 无发现则无单 | 四条规则都不命中时 `referrals=[]`，与现状一致 |
| 单必有据 | 每条转诊单的 `finding_ids` 非空，且都能在病例里查到 |
| R3 不猜 | 宿主交卷没点名仓外时，不生成 R3 单 |
| 可及才算 local | `target_repo` 解析不到存在目录时 `local=false`、`resolved_path=""` |
| 直接转诊留链 | 目标仓挂号带 `referred_from` 后，两个病例在 `hosp_case(include="links")` 互见 |
| 出院不阻塞 | 有转诊单的病例可正常出院，小结如实列出 |

## 6. 明确不做

- 不解析、不访问 URL 或远程目标；`target_repo` 是 URL 的一律 `local=false`；
- 不自动开病例——直接转诊由宿主/用户发起；
- 不做多仓 scope 联合会诊、跨仓差分（design §8，远期不写入承诺）；
- 不把转诊单当疗效统计的分母或分子——它不是疗效事件；
- 跨 case 调档仍限定本 `project_root` 内（design §4.8 权限约束不变）：
  直接转诊只共享「这条链存在」，不共享病历内容。

---

> 待拍板项：①四条触发规则是否都要，R4 依赖记忆层、复杂度最高，可以砍掉或放到第二批；
> ②`evidence` 上限建议 ≤200 字符（与 findings 证据同口径）；
> ③`hosp_register` 加 `referred_from` 这一个入参是否接受（不加的话，链路只能靠出院归档时补写，
> 目标病例中途就查不到来源）。
