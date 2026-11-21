# Enterprise Ops Copilot（简体中文说明）

> 从零基于第一性原理构建的**企业级内部 IT 与运维 Agent**：包含 **ACL 前置权限隔离混合 RAG**、**标准 MCP + HITL 人工审批断点恢复**、**四层纵深安全护栏**、**API Bearer 签名鉴权**、**OpenTelemetry / OTLP 全链路追踪**、**8B/14B 复杂度路由** 与 **SQLite / PostgreSQL (`pgvector`) 双存储后端**。

[English](README.md) | [简体中文](README.zh-CN.md)

## 核心架构模块

- **API 网关与鉴权边界**（`src/enterprise_ops_agent/api/`）：FastAPI + SSE 实时事件流；基于 HMAC-SHA256 的无状态 Bearer Token 鉴权（`auth.py`），服务端强制解析不可篡改的 `UserContext`，普通员工访问高危审批 `/api/approvals/*` 或他人 Trace 返回 `403 Forbidden`。
- **原生 Agent Runtime**（`src/enterprise_ops_agent/runtime/`）：手写异步 ReAct 循环（`engine.py`）、Pydantic 参数校验自愈、步数/Token/重复调用指纹熔断、原子工具组上下文裁剪与 `ExecutionCheckpoint` 断点挂起恢复。
- **ACL 前置隔离混合 RAG**（`src/enterprise_ops_agent/rag/`）：Markdown 标题层级切片、检索评分前按 `allowed_roles` 硬过滤、`bge-m3` 向量 + 中英文 BM25 RRF 融合。
- **MCP 协议解耦**（`src/enterprise_ops_agent/mcp_bridge/`）：官方 `mcp==2.3.0` stdio 服务，利用 `ToolAnnotations` 自动映射 `READ` / `WRITE_LOW` / `HIGH_RISK`。
- **四层安全护栏与可观测性**（`src/enterprise_ops_agent/security/`、`observability/`）：入口直接注入拦截（0ms 拦截，不浪费路由算力）、代码级工具 RBAC、`<untrusted_tool_output>` 间接注入沙箱、出口 DLP 脱敏，以及 SQLite/Postgres `AuditTracer` + OTel/OTLP Span 导出。
- **双存储后端**（`src/enterprise_ops_agent/storage/`）：默认零配置 SQLite，支持 `STORAGE_BACKEND=postgres` 一键切换至 `PostgreSQL + pgvector`。
- **双语 Eval 与复杂度路由**（`src/enterprise_ops_agent/evals/`）：支持 `APP_LOCALE=en|zh` 双语 10 场景黄金评测集 + `qwen3:14b` LLM-as-a-Judge；最新路由策略达到 **10/10（100.0%）通过率、平均忠实度 5.00/5**。
- **生产级 GPU 推理压测与 Prefix Cache 基准**（`scripts/bench_serving.py`、`benchmarks/week3_vllm_sglang_colab.ipynb`）：实测对比本地 `RTX 5060 Ti (Ollama)` 与云端 `NVIDIA L4 (vLLM + Qwen3-8B-AWQ)`，vLLM 在 16 并发下达到 **`578.4 tok/s`（12.28x 扩展、`TTFT P95 = 282.5 ms`）**、长 Runbook 前缀缓存命中率 **`90.98%`**、并发工具调用与四项能力探针均保持 **100% 合格**。

## 快速运行

```bash
# 1. 安装依赖、代码检查与全部离线单元测试
uv sync
uv run ruff check .
uv run pytest -v

# 2. 中文模式本地启动（默认英文，通过 APP_LOCALE=zh 切换中文知识库与控制台）
cp .env.example .env
# 在 .env 中设置 APP_LOCALE=zh
uv run python scripts/ingest_knowledge.py
uv run python scripts/run_server.py

# 3. Docker Compose 启动（支持 SQLite 默认与 Postgres/pgvector Profile）
docker compose up --build
docker compose --profile postgres up --build
```

中文完整架构决策与踩坑复盘见：[docs/ADR_AND_RETROSPECTIVE.md](docs/ADR_AND_RETROSPECTIVE.md) 与 [docs/LLM_SERVING_BENCHMARK.zh-CN.md](docs/LLM_SERVING_BENCHMARK.zh-CN.md)；英文版见 [docs/ARCHITECTURE_AND_ADR.md](docs/ARCHITECTURE_AND_ADR.md) 与 [docs/LLM_SERVING_BENCHMARK.md](docs/LLM_SERVING_BENCHMARK.md)。
