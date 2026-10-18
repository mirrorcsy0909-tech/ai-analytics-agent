# Phase 3：评测与可观测性

更新日期：2026-10-18（America/New_York）。**LangSmith 云端追踪与归档评测已验证**。此前 403 的原因是本机保存的值与原密钥不一致；从用户原先提供的截图恢复同一把 onboarding 密钥后，认证和原项目读取返回 200，没有轮换密钥。

一次真实模型合成 A/B 分析成功完成，云端读回确认主流程及工具节点，共 14 条记录，输入、输出、事件和序列化内容均未上传。固定十二题的归档重放实验也已上传并逐条读回运行及评分，`cloud_verified=true`；这次重放没有重新调用模型。记录见 [云端连接](../eval/results/phase3_cloud_connection.json)、[云端评测](../eval/results/phase3_cloud_evaluation.json) 和 [本次验证汇总](../eval/results/langsmith_reconnection_verification.json)。

此次还修复了真实服务中发现的两项兼容问题：上传后索引可能尚不可读，验证器现在仅对缺失记录/反馈最多重查六次，输出或评分不符仍立即失败；同步数据集时允许服务补充的默认 `dataset_split=["base"]`，其他内容变化仍拒绝覆盖。新增三项针对性回归检查后，本机离线测试为 **90/90**。原有阶段性验证数字保留在下文，Phase 4 本地工程化已完成，见 [Phase 4](phase4_engineering.md)。

本阶段保留原有单模型 LangGraph、SQL、Visualization、A/B Testing 和 Streamlit。

## 已实现

- `agent/observability.py` 提供统一调用入口，网页和命令行均已接入。每次调用分别记录总耗时、模型/工具调用次数与耗时、模型报告的 token、验证失败、工具失败及工作流累计重试次数。
- 模型中断时保留已返回的部分 token，用量缺失显示 unknown，不编成零。继续检查点时，只统计本次重新执行的调用，避免把全部对话历史再次计入。重试次数字段明确是当前问题的检查点累计值。
- Streamlit 的 **Run details** 展示本次指标；本地记录在 `outputs/telemetry/<invocation_id>.json`。这些指标没有问题原文、SQL、交易行、回答或凭据；当前不做自动清理，可按需要删除这些记录。原始评测报告另含合成问题、回答及工具证据，二者用途不同。
- 云端追踪默认关闭。显式启用后使用 LangSmith SDK 和 LangChainTracer，保留图、节点、模型、工具的父子关系、时间和状态。上传前隐藏输入、输出、元数据，额外白名单过滤错误详情、事件、序列化参数和附件。云端这类追踪不提供原始内容调试；完整 token 计数以本地记录为准，没有估算费用。
- 远程追踪设置失败或上传失败不应改变分析结果，也不触发额外模型重试。`submitted_unconfirmed` 只代表已交给 SDK，并不证明服务已保存；`upload_error` 表示在等待范围内观察到上传错误。未配置密钥会回到本地模式。

直接调用 `build_agent().invoke()` 不自动经过该入口；新增的应用入口应使用 `invoke_analysis`。第三方自行添加的追踪回调也不受此隐私过滤器管理。配置开关只管本项目入口。

## 固定题集与评分

`eval/agent_tasks_v1.json` 保持原样，仍为八行合成数据库上的十二题。`eval/phase3_evaluate.py` 用实际安装的 `langsmith==0.11.0` SDK 评估归档记录，默认 `upload_results=False`，且禁用环境中继承的追踪。测试还确认本地模式不进行 LangSmith 服务能力探测。

评分分别展示运行完成、数据库未变、所需工具执行、预期证据、回答来源验证，以及已有答案审阅。数值结果、排序、计数、显著性、零控制组和 PNG 记录均独立检查。允许额外排名/商品编号列，通过记录的 SQL 语法树识别直接列别名；未知列形态可能需要扩展评分器，不能据此直接判定 Agent 错误。

答案审阅与自动检查分开。改动答案后，旧的答案指纹不再匹配，审阅恢复为待完成；缺题或缺少审阅时，严格总通过率保持 null。当前归档审阅者是 **Codex**，并非独立人类评审。验证器不能全面判断自由文字含义、SQL 意图或所有因果断言，十二题也不是生产准确率。

本次真实模型使用原有 `gpt-5.6`、medium reasoning、Responses API，仅提交合成数据，关闭远程追踪。结果与源码、题集、运行器、评分器指纹保存在：

- [本次真实模型与逐题证据](../eval/results/phase3_agent_workflow.json)
- [LangSmith SDK 本地评分与分类结果](../eval/results/phase3_evaluation.json)
- [固定工具复测记录](../eval/results/phase3_tool_baseline.json)
- [Phase 2/3 对照](../eval/results/phase3_comparison.json)

本次复跑通过 **12/12 确定性检查与 Codex 逐题审阅**。固定工具检查仍为 **34/34**；离线测试覆盖正常分析、中断恢复、用量缺失、云端不可用、敏感信息过滤、SDK 本地评分和数据集冲突处理。具体测试数和耗时见对照记录。网页测试验证分析结果、会话清空和继续执行仍可用。

| 指标 | Phase 2 | Phase 3 |
| --- | ---: | ---: |
| 离线测试 | 50/50 | 69/69 |
| 固定工具检查 | 34/34 | 34/34 |
| 确定性检查与 Codex 答案审阅 | 12/12 | 12/12 |
| 平均整题耗时 | 6,900.53 ms | 7,328.78 ms |
| 全部模型轮次总 tokens | 39,185 | 41,225 |

本次共 28 次模型调用、16 次工具调用，各题没有触发恢复重试。12 题的回调 token 计数与返回消息记录全部一致。模型生成的查询及回答会变化，单次重跑的时间和用量差不能当成观测功能本身的开销测量。

## 本地运行

在项目根目录、装好 requirements 的 Python 环境中：

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 -m streamlit run app/streamlit_app.py
```

只重新计算归档记录的评分，不调用模型、也不上传：

```bash
python3 eval/phase3_evaluate.py \
  --report eval/results/phase3_agent_workflow.json
```

需要新一轮真实模型调用时：

```bash
python3 eval/phase0_agent_baseline.py --run --implementation workflow --observe \
  --output outputs/phase3_agent_latest.json
python3 eval/phase3_evaluate.py --report outputs/phase3_agent_latest.json
```

新记录的答案仍需审阅，评分器不会自动补上审阅结果。真实模型评测的 `--observe` 只记录本地指标，即使环境开启 tracing 也不会上传。不要把新一轮结果覆盖到冻结归档路径。项目独立 `.venv` 仍需安装 requirements；本次验证使用已安装依赖的 Python 3.13.7。

## 配置 LangSmith

在本机 `.env` 填写下列配置；**不要把密钥发到聊天或提交到 Git**。示例也已加入 `.env.example`。本机当前沿用原密钥，并使用已有项目 `analytics-agent-local`；示例项目名应按实际创建的项目填写。

```dotenv
ANALYTICS_LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your_langsmith_key
LANGSMITH_PROJECT=analytics-agent
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

多工作区密钥可另填 `LANGSMITH_WORKSPACE_ID`。区域或自托管服务使用自己的 endpoint。重启 Streamlit 后再运行分析；默认不启用 `LANGSMITH_TRACING`，由本项目开关控制。关闭 `ANALYTICS_LANGSMITH_TRACING` 即恢复仅本地记录。尚未配置密钥时，分析仍可运行。

把固定的合成参考答案、检查分数、答案指纹、原始耗时和数字用量上传为一个**归档结果重放实验**：

```bash
python3 eval/phase3_evaluate.py \
  --report eval/results/phase3_agent_workflow.json \
  --output outputs/phase3_cloud_evaluation.json --publish
```

该命令会写入你的 LangSmith 账户。数据集名包含冻结题集指纹，示例 ID 固定，重复同步只补缺少的示例；若已有内容冲突或包含额外示例则拒绝覆盖。只上传固定合成问题与预期结果、分数和数字指标，不上传报告里的 SQL、回答、完整消息、交易行或本地路径。

这个实验不重新调用模型，LangSmith 实验自身耗时是重放耗时；模型性能应查看输出的 `original_duration_ms`，不能混为一谈。脚本读回云端运行与反馈后才标记 `cloud_verified=true`。2026-10-18 已执行上传并确认十二题运行与评分实际保存；结果保存在上方云端评测记录中。本地指标仍可能显示 `submitted_unconfirmed`，因为日常调用入口不会自动增加远程读取；本次云端确认来自独立的读回验证。

参考：[LangSmith 本地配置追踪](https://docs.langchain.com/langsmith/trace-without-env-vars)、[输入/输出及其他字段隐藏](https://docs.langchain.com/langsmith/mask-inputs-outputs)、[SDK evaluate 参数](https://reference.langchain.com/python/langsmith/evaluation/_runner/evaluate)。

Phase 4 的 FastAPI、Docker、CI 配置与本地验证已完成；GitHub Actions 的远程执行仍需后续提交和推送才会触发。
