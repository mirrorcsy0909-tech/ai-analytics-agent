# Phase 4：FastAPI、Docker 与 CI

更新日期：2026-10-18（America/New_York）。保留原有单模型 LangGraph、SQL、Visualization、A/B Testing 和 Streamlit。后续已修正本机保存的原 LangSmith 密钥值，云端真实追踪和十二题归档重放评测均已读回验证，没有轮换密钥。详见 [Phase 3 更新](phase3_observability.md)；下文 87 项测试和容器记录保留为 2026-10-17 的 Phase 4 历史验证，本次连接修复后的本机测试为 90/90。

## 本地使用

项目自己的 `.venv` 已安装锁定依赖，VS Code 可选择 `.venv/bin/python` 作为解释器。在项目目录运行：

```bash
source .venv/bin/activate
python -m streamlit run app/streamlit_app.py
```

分析页面默认是 `http://127.0.0.1:8501`。打开另一个终端可启动 HTTP 接口：

```bash
source .venv/bin/activate
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

接口说明页面在 `http://127.0.0.1:8000/docs`。修改 `.env` 后应停止并重新启动进程，使凭据和追踪设置重新加载。实际密钥只留在本机 `.env`；本阶段没有修改已恢复的模型密钥，也没有轮换当前 LangSmith 密钥。

## 接口行为

| 接口 | 用途 |
| --- | --- |
| `GET /health/live` | 确认服务进程正常，不建立模型、不查询外部服务 |
| `GET /health/ready` | 检查模型密钥已配置且零售数据库可读；不证明外部认证或 LangSmith 可用 |
| `POST /v1/analyses` | 提交一个独立问题，返回答案、状态、本次指标及图表链接 |
| `GET /v1/analyses/{request_id}/artifacts/{artifact_id}` | 下载与该请求关联、尚未过期且完整性匹配的 PNG |

示例请求：

```bash
curl http://127.0.0.1:8000/v1/analyses \
  -H 'Content-Type: application/json' \
  -d '{"question":"Run an A/B test: control 100 conversions out of 1000 users, treatment 150 out of 1000."}'
```

接口使用现有工作流和验证器。问题必须是 1–5000 字符的非空字符串，禁止额外字段；请求不能指定已有会话或任意文件路径。每个 API 请求生成新 ID、从独立消息开始，不保存跨请求对话。Streamlit 原有会话与继续检查点功能保持原样。

默认单进程同时最多运行两项分析，容量满时立即返回 429，附 Retry-After。模型每次调用保留 30 秒超时、零 SDK 自动重试，工作流仍有最多六轮工具和两次恢复的限制。HTTP 接口是同步响应，不提供后台任务、断线后取消或整项任务总超时；多个模型轮次的总耗时可能超过 30 秒。生产任务队列与跨进程限流不在本次实现范围。

API 的 `status` 为 completed、responded 或 failed。HTTP 200 不代表结论的全部语义已获验证；返回 failed 时也必须按失败处理。凭据未配置返回 503，模型超时返回 504，模型限流/容量满返回 429，其余分析异常返回 502。错误响应不回显输入、密钥或提供商错误正文。

PNG 下载只使用服务生成的不透明 ID，校验目录范围、软链接、PNG 头和内容摘要，单文件上限 5 MB。答案中出现的对应服务器路径会替换为下载链接。索引默认保留一小时、最多一百个包含图表的请求；进程重启、索引到期或被淘汰后，旧链接返回 404。这个索引仅存在内存，实际 PNG 与本地指标文件需要单独清理，不会随索引淘汰自动删除。

默认只绑定本机地址。需要控制客户端访问时，在 `.env` 配置单独的 `ANALYTICS_API_TOKEN`，分析和下载请求带 `Authorization: Bearer <token>`；健康接口保持可用。它是本服务自己的可选访问令牌，与 OpenAI、LangSmith 的密钥各司其职。当前实现没有多用户账户、租户权限或分布式会话，不能直接当作完成生产隔离的公共服务。

## Docker 启动

```bash
docker compose build
python3 scripts/load_docker_database.py
docker compose --profile ui up -d
```

API 与网页分别映射到本机 8000 和 8501 端口。只需要 API 时，最后一行改为 `docker compose up -d api`。

本地进程与 Docker 默认使用相同端口，启动 Docker 前应先停止对应的本地进程，或自行调整端口。

容器使用 Python 3.13.7 的指定镜像摘要、非 root 用户、只读根文件系统和只读数据库卷。`.dockerignore` 采用运行源码白名单，真实 `.env`、交易 CSV/数据库、Git 历史、编辑器历史、评测结果和虚拟环境不会进入镜像。凭据由 Compose 在启动时从本机 `.env` 注入，不参与镜像构建。

数据库放在本地 Docker 数据卷 `ai-analytics-agent-data`，避免依赖 macOS Desktop 目录共享。加载脚本先用 SQLite 只读连接建立一致性快照，验证 transactions 表，再通过本机 Docker 输入流写入数据卷；不会将数据库发往云端，也不修改原数据库。已有容器使用该卷时脚本拒绝替换，应先停止服务：

```bash
docker compose down
python3 scripts/load_docker_database.py
docker compose --profile ui up -d
```

数据库卷保持只读挂载，API 与网页共享一个持久输出卷。`docker compose down` 保留数据卷；更新数据库需要重新加载快照，修改原文件不会自动反映到 Docker 中。首次未加载快照时，服务可以启动，但完整零售 readiness 为 503，SQL 查询不可用。

## 依赖与自动检查

`requirements.txt` 固定直接依赖，`requirements.lock` 固定所有依赖及发行包哈希，包含跨平台条件。新环境安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip check
python -m unittest discover -s tests -v
```

需要更新依赖时，先修改直接版本，再重新生成锁文件和验证：

```bash
uv pip compile requirements.txt --universal --python-version 3.13 \
  --generate-hashes --output-file requirements.lock
```

`.github/workflows/ci.yml` 配置两个 GitHub Actions 作业：Python 安装与测试，以及 Docker 构建与无凭据启动。权限为 contents:read，不使用真实密钥或数据，不做部署或镜像推送。工具基线使用临时八行合成库，原十二题归档评分调用 LangSmith SDK 的本地模式，不调用模型或上传。

当前只有本地与 Docker 中的验证记录；**尚未提交/推送代码，GitHub Actions 未实际触发**。远程流水线结果应在用户后续推送后查看。

## 本阶段验证

本机与 Linux 容器均通过 **87/87 离线测试、34/34 工具检查**。原十二题归档结果在 Linux 的 LangSmith SDK 本地评分中仍为 12/12；这次归档重评分没有重新调用模型。Docker API 的三项真实模型合成任务另外保存，不混入原十二题基准。

- macOS 项目虚拟环境测试，以及 Linux 容器中的测试和工具检查。
- API 请求隔离、输入限制、并发准入、失败后释放容量、状态和错误处理。
- 可选令牌验证、图表链接绑定、路径/软链接拒绝、文件篡改、索引超时和容量淘汰。
- 真实 SQL 工具、LangGraph 和回答验证器经 HTTP 的离线集成。
- 数据库一致性快照、原文件不变、缺失/无效数据库和使用中的数据卷保护。
- 构建镜像实际导入所需库，确认非 root 用户和私密文件排除。
- 真实模型通过 Docker API 完成合成 SQL、月度图表下载和零控制组 A/B 三项检查，追踪在这轮测试中关闭。
- 另用当前密钥启用追踪后做一次合成 A/B 调用，分析成功返回，指标正确记录 upload_error；未把上传失败误报成分析失败。

详见 [验证汇总](../eval/results/phase4_verification.json)、[API 真实调用记录](../eval/results/phase4_api_smoke.json) 和 [固定工具复测](../eval/results/phase4_tool_baseline.json)。这些是受控验证，不是生产准确率或云端 LangSmith 认证的证明。

参考：[FastAPI 同步函数与线程池](https://fastapi.tiangolo.com/async/)、[Docker 构建建议](https://docs.docker.com/build/building/best-practices/)、[GitHub Python CI](https://docs.github.com/en/actions/tutorials/build-and-test-code/python)。
