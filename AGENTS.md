<!-- rsi-boot:begin -->

> 本块由 RSI Boot 自动维护，请勿手改（改动会被下次重写覆盖）。

## 记忆目录（详情用 rsi_recall 召回）
- visit_kind 为 admission 且交卷说明不是宿主已交卷或宿主已通读时，返回 pending_question(diff_filing)，状态保持 scanned，不写诊断书。出院小结写明疗效只统计仪器发现。
- visit_kind 为 admission 且交卷说明不是宿主已交卷或宿主已通读时，返回 pending_question(diff_filing)，状态保持 scanned，不写诊断书。出院小结写明疗效只统计仪器发现。
- 在本机跑 SWRBench（ZZR0/SWRench）和 ReviewBench 25 PR 测试子集，用 code-hospital 对 PR diff 做挂号和检验，对照金标算重合、多报和未覆盖。不改医院业务代码，评测脚本放仓外。
- startsWith(path.resolve(目录)) 或 startswith(os.path.abspath(目录))、后面没有再接分隔符，记为 security.path-prefix，已确认并保持明确缺陷。带上 path.sep 
- startsWith(path.resolve(目录)) 或 startswith(os.path.abspath(目录))、后面没有再接分隔符，记为 security.path-prefix，已确认并保持明确缺陷。带上 path.sep 
- 合入门诊把 F541 降为风格。点名的异常、成功路径已经返回且后面还有语句、条件分支里只有一次调用，不算异常被吃掉；笼统的 Exception 接住后仍使用 try 里赋的名字，仍算。semgrep 的 pickle 未核实低信任输入时降为
- 合入门诊把 F541 降为风格。点名的异常、成功路径已经返回且后面还有语句、条件分支里只有一次调用，不算异常被吃掉；笼统的 Exception 接住后仍使用 try 里赋的名字，仍算。semgrep 的 pickle 未核实低信任输入时降为
- 合入交卷写入病历，semgrep 改跑登记处规则包
- 合入交卷写入病历，semgrep 改跑登记处规则包
- 合入门诊把未使用导入和星号导入下的未定义名字降为风格，并让宿主通读 diff
- 合入门诊把未使用导入和星号导入下的未定义名字降为风格，并让宿主通读 diff
- 为 code-hospital 选择合适的开源代码审查 benchmark 仓库，评估当前能力覆盖、缺陷和改进点。候选包括 ReviewBench、Code Review Bench、c-CRAB、AACR-Bench、ContextCRB
- 检查事项写在内置名册或 code-hospital.yaml。每项声明要交出的产物，以及由代码、宿主 LLM、外部工具里哪些执行方配合完成。医院负责调度事项、把上一份产物交给下一项、记下没到岗的执行方。架构示例按顺序交出当前架构、优缺点、重
- 检查事项写在内置名册或 code-hospital.yaml。每项声明要交出的产物，以及由代码、宿主 LLM、外部工具里哪些执行方配合完成。医院负责调度事项、把上一份产物交给下一项、记下没到岗的执行方。架构示例按顺序交出当前架构、优缺点、重
- 医院规范并串起 LLM 本来就会做的审查：在就诊范围内通读代码、写出业务理解、再报问题。仪器结果是证据和覆盖账，与 LLM 发现一起进病历，供下一阶段开方。范围内未读完的代码不能结案。

## 知识树（板块计数；下钻用 rsi_explore，召回用 rsi_recall）
- general (40)  ·  rsi_explore "general"

## 收工纪律

teach_catch：修失败多次、低置信、改公共规则、或用户纠正执行方式时先记现场；写好 lesson 后 teach_record。
修了用户可见的 bug（成功也算）必须 teach_record，lesson 含 wrong_action 与 correct_fix。
未 audit_finish 前不得改业务代码。
最终总结照抄 closeout 路径，或写跳过原因。
用了 rsi_recall 命中项，收工前必须 rsi_feedback（带 feedback_token）上报 accepted 或 rejected。
Catch then record. Do not edit product code before the audit is finished. Copy closeout paths into the final summary.

<!-- rsi-boot:end -->
