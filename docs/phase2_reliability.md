# Phase 2：Analytics 验证与可靠性

进度更新：Phase 3 本地评测与可观测性已实现，云端验证待配置密钥；见 [Phase 3](phase3_observability.md)。下文保留 Phase 2 当时的实测记录。

完成日期：2026-10-17（America/New_York）。改动已写入本地项目，沿用 SQL、Visualization、A/B Testing、Streamlit 和单模型 LangGraph 工作流。

## 实测结果

| 指标 | Phase 1 | Phase 2 |
| --- | ---: | ---: |
| 同一组工具基线 | 18/34 | **34/34** |
| 离线工作流、页面与可靠性测试 | 20/20 | **50/50** |
| 固定自然语言任务：严格审阅 | 9/12 | **12/12** |
| 平均整题耗时 | 4,027.44 ms | 6,900.53 ms |
| 全部模型轮次总 tokens | 20,101 | 39,185 |

三个阶段的任务文件与合成数据相同，模型仍为 `gpt-5.6`、medium reasoning、Responses API；本次每题未触发恢复重试。两项币种问题和零控制组除零问题均未再出现。原有四组评测也全部通过。

这是每个阶段各一次、仅 12 个合成任务的系统对照。不能将 12/12 当成生产准确率，也不能把变化单独归因于 LangGraph：工具、提示词与回答协议都随阶段改变。本次可靠性约束还增加了耗时和用量，需要后续用更大数据集评估取舍。

原始工具调用、最终答案、逐题评分理由、答案指纹、源码指纹和依赖版本均已保存：

- [34 项工具复测](../eval/results/phase2_tool_baseline.json)
- [12 题真实调用记录](../eval/results/phase2_agent_workflow.json)
- [跨阶段对照与逐题评分](../eval/results/phase2_comparison.json)

评分由 Codex 检查实际答案及工具记录，并用确定性断言复核结果数值、顺序、工具调用、图表数据与数据库未变。代码内的 claim 校验通过不直接等于任务评分通过。

## SQL 验证与执行边界

`tools/sql_tool.py` 改为 SQLGlot 的 SQLite 语法树策略检查：允许单条 SELECT、只读 CTE 和集合查询，关键词出现在字符串或别名中不会再被误拦；拒绝多语句、写操作、外部表与文件/扩展函数。

实际执行还使用 SQLite `mode=ro`、`query_only` 与 authorizer：只读取 `transactions` 和本地 CTE，函数按白名单开放；即使绕过 Python 的解析筛选，写入仍由数据库层阻止。解析通过不代表 SQL 正确，数据库依然负责语法、列名和执行检查。

默认最多返回 1,000 行，超出时明确报错，提示聚合或增加 LIMIT，不静默截断。查询文本上限 20,000 字符；SQLite VM 默认执行预算 3 秒；限制单个值长度，拒绝重复列名和非有限数值。连接在异常时同样关闭；缺失的数据库不会被自动创建。

依据：[SQLGlot 官方文档](https://sqlglot.com/sqlglot.html)、[Python SQLite authorizer 文档](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.set_authorizer)。SQLGlot 本身不是 SQL 正确性证明工具，因此保留数据库侧强制约束。

## A/B 输入、稀疏数据与解释

`tools/ab_test_tool.py` 现在要求整数计数、正样本量、转化数在合法范围、有限且位于零与一之间的 alpha，以及明确支持的检验方向。布尔值不当作计数。

- 控制组转化率为零时，`relative_lift=None`，回答呈现为 **undefined**，不会除零，也不会当成零相对提升。
- 常规数据保留 pooled two-proportion z-test，便于与原有数值基线比较。
- 任一观察格或 pooled 期望格小于五时，使用 **Fisher exact**；全零、全一等退化组也返回有限 p 值。
- 稀疏数据的差值区间用 **Newcombe/Wilson**；常规数据保留原 Wald 区间。区间单独标注为双侧，不能与单侧 p 值当成同一个显著性判据。
- 返回检验方法、方向、alpha、区间置信水平、方法和假设说明。非默认 alpha 不再错误标成 `confidence_interval_95`。
- 支持 `larger`、`smaller`、`two-sided`。显著下降不会被描述为提升；方向应在看结果前确定。

零控制组的实测案例是 control 0/1,000、treatment 10/1,000：绝对提升 0.01，相对提升未定义，单侧 Fisher p≈0.000954733。测试与区间方法均被明确展示。

独立性、随机分组、样本比例失衡、重复查看、多重比较、检验功效和实际业务价值没有从这四个计数中得到验证。代码会保留这些假设说明，不据显著性自动批准上线。

方法依据：[SciPy Fisher exact](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.fisher_exact.html)、[statsmodels 两独立比例差区间](https://www.statsmodels.org/stable/generated/statsmodels.stats.proportion.confint_proportions_2indep.html)。

## 图表数据、文件与并发

`tools/visualization_tool.py` 验证数据非空、列名唯一、类别非空且不重复、数值有限并且不是字符串或布尔值。重复类别需要先聚合，避免图形覆盖而产生误读。

只接受输出目录内的 PNG 文件名，拒绝绝对路径、路径穿越和指向目录外的软链接。每次生成唯一文件名；整个 pyplot 创建、绘制、保存、关闭过程使用同一把锁，防止本进程内并行调用混用图形状态。保存失败时清理残留文件和 figure。

图表工具返回实际数据、轴字段、路径和 PNG 摘要。工作流校验文件存在且摘要匹配；显示仍使用本次分析的 `artifacts`。这验证了受控绘图管线的数据传递和文件完整性，不是通过视觉识别证明任意图表的语义。

## 数值和单位从哪里来

新增 `agent/evidence.py` 和 `agent/answer_validation.py`。分析工具返回两部分：供模型读取的 JSON，以及保留在 ToolMessage 中的结构化 evidence。表格给模型和渲染器展示同样的前 20 行，并标明返回行数；更大的结果需要缩小查询或分页。

模型最终提交内部的 `answer_template` 与来源引用。每个数字、数值 ID、月份、比例或区间端点引用一个成功工具的字段；代码读取实际字段值并填入，而不是使用模型提供的替代数字。引用只覆盖当前问题的成功证据；失败工具、虚构来源、越界行、未知列和额外自填值都会被拒绝。

允许的格式转换有原值、百分比、百分点、p 值，且受字段类型限制。收入不能被当成比例。没有声明币种时拒绝答案模板中的币种符号/名称；任意游离数字也会被拒绝。零相对提升分母产生的 None 显示为 undefined，微小非零比例不会被显示成零。

A/B 的声明结论与实际统计字段核对，最终附加由代码生成的检验方法、双侧区间标签与假设说明。用户界面只展示渲染后的自然语言和表格，不显示内部 JSON。验证失败后把具体原因交回同一个模型修正，与工具恢复共用最多两次的预算；耗尽后明确失败。

**边界：这不是完整语义验证器。** 它不证明用户问题被正确翻译成 SQL，不自动证明自由文字的全部含义、排名解释或因果关系，也不能阻止所有以文字写出的错误断言。来源绑定、数值渲染、单位约束与声明结论检查只是可复现的防护层；最终任务质量仍需独立评测。工作流保留 `semantic_accuracy_checked=False`，不会把这些检查冒称为全语义正确性。

## 验证与运行

在项目根目录、已安装依赖的 Python 环境中：

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 eval/phase0_baseline.py
python3 -m streamlit run app/streamlit_app.py
```

新增依赖 `sqlglot==30.23.0` 已在本次验证环境安装。若 VS Code 使用项目自己的独立 `.venv`，也需要在该环境安装 requirements。完整环境锁定与容器仍属于 Phase 4。

真实模型重跑，仅使用固定的合成数据库：

```bash
python3 eval/phase0_agent_baseline.py --run --implementation workflow
```

默认最新结果写入 `outputs/`，不会覆盖冻结的 `eval/results/`。`--implementation legacy` 现在是使用**当前工具**的原 `create_agent` 架构参考，并非冻结的旧工具实现；历史 Phase 0/1 实测以归档 JSON 及其源码指纹为准。

50 项离线测试包含只读防御、权限层绕过尝试、超时、行数、缺失数据库、非有限值、Fisher 独立参考计算、退化样本、检验方向、图表并行与文件清理、证据渲染和修正，以及真实工具到工作流的离线集成。没有通过删掉原有失败用例来改变通过率。

原始数据库、CSV 与 `.env` 未修改；API 评测未提交原始交易数据，且关闭远程 tracing。Git 的原有 `.gitignore` 修改保留，没有提交或推送。会话检查点仍限于当前进程。

下一阶段按原规划进入 Phase 3：LangSmith 数据集、评测与可观测性；之后是 Phase 4 FastAPI、Docker、CI 与环境复现。
