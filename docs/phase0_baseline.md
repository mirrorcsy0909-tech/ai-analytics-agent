# AI Analytics Agent 2.0 — Phase 0 基线与升级路线

后续进展：模型接口兼容性已修复，旧版与 Phase 1 均已完成固定 12 题试跑。详见 [Phase 1 报告](phase1_workflow.md)。下文保留 Phase 0 当时的检查与失败记录。

评测日期：2026-10-17（America/New_York）。当前阶段只新增评测和文档，保留原有 Agent、SQL、Visualization、A/B Testing、Streamlit 实现。

## 实际结果

- 原有 `eval/eval_harness.py`：**4/4 通过**，使用本地数据库的临时副本。
- 新增边界检查：**14/30 通过，16/30 未满足要求**，使用 8 条虚构交易，包含退款、空 CustomerID 和跨年月份。
- 合计：**18/34 通过、16/34 失败、0 跳过**。这些是工具检查，包含为 Phase 2 明确的目标要求，不能作为 Agent 任务成功率。
- 本地数据库 `transactions` 有 **530,104 条交易**。没有重建或修改原数据库、原 CSV，也没有覆盖已有图表。
- 离线工具评测的 API 请求数：**0**，该脚本不读取 `.env`。另外进行了下文记录的模型兼容性试跑，只提交了合成任务问题。
- 自然语言端到端任务成功率、模型延迟、token 用量、错误恢复率：**尚未测量**。

机器可读的冻结结果：[`eval/results/phase0_baseline.json`](../eval/results/phase0_baseline.json)。其中包括每项断言、错误、耗时、运行环境和原实现的 SHA-256；耗时是本地检查耗时，不是模型延迟。

| 检查类别 | 通过 | 失败 | 解释 |
| --- | ---: | ---: | --- |
| 原有评测 | 4 | 0 | SQL 返回结构、已知写操作拦截、图表文件、A/B 输出结构 |
| SQL 精确结果 | 5 | 4 | 正常汇总与空结果通过；关键词、注释、CTE 的合法查询被误拦 |
| SQL 多语句入口校验 | 0 | 1 | `SELECT 1; SELECT 2;` 被校验器放行；不代表 SQLite 实际允许多语句执行 |
| A/B 数值正确性 | 5 | 0 | 与独立 pooled-z / `math.erfc` 参考计算对比，覆盖提升、下降、相等、双侧检验 |
| A/B 无效输入 | 1 | 7 | 不支持的检验方向能报 ValueError；其余缺少明确输入校验 |
| A/B 零控制组转化率 | 0 | 1 | 计算相对提升时除以零 |
| 图表 | 3 | 3 | 柱图、折线图的数据传递和 PNG 通过；空数据、非数值、路径边界未验证 |

## 如何复现

在项目根目录运行：

```bash
python3 eval/phase0_baseline.py
```

最新结果默认写入 `outputs/phase0_latest.json`，不会覆盖冻结基线。当前实现下预期退出码为 **1**，因为 16 项目标要求尚未满足；详细原因在 JSON 的 `cases` 中。若缺少本地数据库，原有四项明确记为 `skipped`，其余合成数据检查仍运行。

这台机器实测解释器是 Python **3.13.7**。项目自己的 `.venv` 存在，但未安装所需依赖；本次使用电脑上已经安装好依赖的 `python3`。不要把空 `.venv` 当成已经配好的环境。

如需在另一台机器准备独立环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python eval/phase0_baseline.py
```

`requirements.txt` 尚未固定版本，因此新安装环境可能与本次不同。冻结 JSON 已记录本次实际版本；完整依赖锁定属于 Phase 4。

所有原评测在数据库临时副本上运行，新增用例只操作合成 SQLite 数据库，所有生成图写入临时目录并清理。离线脚本 `phase0_baseline.py` 不导入项目 Agent，避免加载 `.env` 或调用模型。

## 代码检查与复用决定

| 文件 | 当前作用 | 后续处理 |
| --- | --- | --- |
| `tools/sql_tool.py` | SQLite 查询和关键词筛选 | 保留接口；Phase 2 添加结构化 SQL 校验、数据库只读连接、结果限制和资源清理 |
| `tools/visualization_tool.py` | 柱图、折线图与 PNG 输出 | 保留；Phase 2 添加数据校验与路径限制；按请求区分图表产物 |
| `tools/ab_test_tool.py` | 两比例 z 检验和建议 | 保留；Phase 2 增加计数、样本量、alpha 校验与零控制组处理 |
| `agent/analytics_agent.py` | 工具包装、系统提示词、LangChain Agent、CLI | 复用工具定义与提示词；Phase 1 建立可观察的分析状态及验证节点 |
| `app/streamlit_app.py` | 用户提问、回答和图表展示 | 保留；修复重复图表识别；Phase 4 接入服务层 |
| `app/basic_analytics_assistant.py` | 较早的规则驱动演示 | 保留作参考，升级主入口继续使用真正的 Agent |
| `scripts/create_database.py` | CSV 导入 SQLite | 保留；后续增加目录、输入和非破坏性重建选项 |
| `eval/eval_harness.py` | 原有四组工具检查 | 原样保留，作为历史评测参照 |

当前安装的 LangChain **1.3.15** 中，`create_agent` 本身已返回 LangGraph 图，执行模型与工具循环。Phase 1 的价值在于把分析计划、工具结果、验证结果和重试状态显式建模，而非把现有实现描述为完全没有图或没有状态。现有项目代码没有配置持久化检查点或自己的分析验证节点。

## 已确认的问题与边界

1. **SQL 校验误拦合法分析。** `Update guide` 字符串、`total_updates` 别名、前导注释和只读 CTE 都被拒绝。多个 SELECT 被校验器放行，但当前 SQLite 执行层还有单语句限制；本次没有证明可绕过执行任意写操作。
2. **A/B 无效输入没有稳定的错误契约。** 总人数为零触发 ZeroDivisionError；负数、非整数、转化数大于样本数、无效 alpha 可能继续计算。控制组转化率为零时，相对提升未定义，当前会直接除零。基线要求返回明确的 ValueError 或显式处理未定义值，原实现尚未满足。
3. **统计解释需要明确。** 默认检验是单侧 `treatment > control`，区间是双侧 Wald 区间；这不是同一个显著性判据。小样本、检验前提、样本比例失衡等没有检查。本次只确认列明样例的数值计算，不代表所有统计情境都已验证。
4. **图表输入与输出边界不足。** 空数据也会生成图；字符串可能被当作分类轴而非数值；`../escaped.png` 能越出配置的图表目录。路径用例只在评测的临时目录中运行。
5. **Streamlit 会漏掉同名图表更新。** `get_new_charts` 仅比较路径集合，而 Agent 使用固定文件名。对实际辅助函数的独立复现中，更新前后同一路径返回空列表。此项为补充代码检查，未计入 34 项工具检查。
6. **入口与可复现性。** Streamlit 在提交问题前就创建模型客户端，缺少统一错误展示；数据库创建脚本会替换表，缺少目录创建和输入校验；依赖未固定。Git 已跟踪部分 `.pyc` / `.DS_Store`。原 `.gitignore` 已有 staged / unstaged 修改，本次保留，没有重置或代为提交。
7. **README 的 Markdown 问题。** 评测命令后的代码围栏没有及时结束，后文正文会显示在代码块中；文件没有完整环境准备步骤。可随 Phase 1 更新使用文档。

## 固定的端到端任务集

[`eval/agent_tasks_v1.json`](../eval/agent_tasks_v1.json) 冻结了 **12 个尚未完成端到端执行**的自然语言任务及预期行为：SQL 汇总/空结果、图表、A/B 提升、缺失输入、拒绝写入和零控制组转化率。

它们仅绑定这里的合成数据。后续端到端评测需要记录真实工具调用和最终答案，不能凭问题列表生成成功率，也不能把固定的合成数据答案用于真实零售数据库。先在当前 Agent 上实测，再用同一集合评价升级版本，才能得到可靠的前后对比。

### 当前模型配置阻断了端到端评测

已使用 `phase0_agent_baseline.py` 试跑第 1 题。实际配置模型为 `openai:gpt-5.6`。服务在执行分析工具前返回 **HTTP 400 / BadRequestError**，指出 `reasoning_effort` 与 Chat Completions 的 function tools 不兼容。停止剩余 11 题后，用同一题做了 1 次诊断确认；两次均未完成模型任务，不能记作 0% 的分析正确率，也没有可用的 token 用量。

原始试跑和诊断结果保存在 [`eval/results/phase0_agent_baseline.json`](../eval/results/phase0_agent_baseline.json)，密钥不写入报告。当前阶段保留原始实现和 `.env` 配置，未为了得到成功结果而切换模型或修改请求语义。

已核对本机 SDK：`ChatOpenAI` 没有显式设置 reasoning effort 或 Responses API；服务根据模型默认值处理请求。OpenAI 官方的 [Responses 迁移文档](https://developers.openai.com/api/docs/guides/migrate-to-responses) 说明，GPT-5.4 起 Chat Completions 的工具调用仅支持 `reasoning_effort=none`。修复方向是显式使用 Responses API，或在兼容的 Chat Completions 工作流中禁用 reasoning。优先在下一步保留同一模型、修复接口配置，再完成 12 题旧版基线，之后才改 Agent 编排。

接口修复后，可用以下命令重跑；它会使用已有 API 凭据和真实模型调用，但数据库及图表全部隔离到临时目录，并限制请求超时、输出长度和图执行步数：

```bash
python3 eval/phase0_agent_baseline.py --run
```

最新结果默认写入 `outputs/phase0_agent_latest.json`。原始观察需要结合实际工具调用和最终回答评分，脚本不会自行生成未经核验的成功率。当前配置下预期退出码为 2，表示尚未跑完。

## 既定五阶段路线

| 阶段 | 实现范围 | 退出标准 |
| --- | --- | --- |
| Phase 0 | 结构检查、历史评测、工具边界基线、冻结任务集 | 本文与 JSON 形成可复现的工具基线；端到端指标明确标为未测，需在修改 Agent 前补测 |
| Phase 1 | 显式 LangGraph 状态；计划、执行、验证节点；受控重试与会话状态 | 保留现有工具和 Streamlit；真实任务可追踪到工具证据与最终结果；与旧版使用相同任务对比 |
| Phase 2 | SQL、结果、统计和图表验证；可靠性与错误恢复 | 固定用例中的已确认问题得到修复；增加由真实失败驱动的验证，复测相同基线 |
| Phase 3 | LangSmith 数据集、追踪、评测与可观测性 | 记录任务结果、模型/工具错误、延迟和用量；使用同一版本的任务集比较 |
| Phase 4 | FastAPI、Docker、CI、依赖版本与运行文档 | Streamlit 调用独立服务；容器可复现；CI 运行离线回归；发布凭据单独配置 |

沿用单 Agent 的显式工作流。每个新组件都应解决已观察到的问题，避免无必要的多 Agent。
