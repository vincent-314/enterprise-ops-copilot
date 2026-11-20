# Enterprise Ops Copilot：架构决策记录（ADR）与项目复盘

本文档沉淀从 Phase 0 到 Phase 6 的核心工程决策、实测陷阱及面试/汇报级表达脉络。

---

## 一、核心架构决策记录（ADR）

### ADR-001：原生手写 Agent Loop，而非直接套用重型黑盒编排框架
- **背景**：直接引入 LangChain 等重型抽象往往掩盖了上下文裁剪、Tool 解析容错、状态序列化和防死循环细节。
- **决策**：在 `runtime/engine.py` 用约 300 行清晰可审的原生异步代码实现 ReAct 循环，配合 Pydantic 工具注册表 `ToolRegistry`。
- **收益**：
  - 将工具 JSON 解析错误与参数校验错误转为结构化回喂信息（Model Self-Correction），避免循环崩溃；
  - 在引擎内部实现了 `max_steps`、Token 预算、重复调用指纹熔断，以及按原子工具组裁剪历史消息的 `prune_messages`；
  - 更容易植入企业级 HITL Checkpoint、Guardrails 与全链路 Tracing。

### ADR-002：面向 Ollama `/v1` 接口的上下文窗口陷阱（实测修正）
- **实测发现**：Phase 1 在本地 `qwen3:8b` 联调后通过 `ollama ps` 发现，即使请求中带了 `extra_body={"options": {"num_ctx": 16384}}`，Ollama 的 OpenAI 兼容端点 `/v1/chat/completions` 仍忽略该参数，上下文默认保持 `4096`（极易在多步 RAG 中静默截断系统提示）。
- **决策**：在服务端进程配置 `OLLAMA_CONTEXT_LENGTH=16384`，并在客户端代码中移除无效字段、加上显式注释。Phase 2 复测 `ollama ps` 确认 `qwen3:8b`（7.5 GB）与 `bge-m3`（689 MB）均以 `100% GPU` 运行且 `CONTEXT = 16384`。

### ADR-003：RAG 前置权限过滤（Pre-filter ACL）+ 运行时身份强绑定
- **背景**：若在检索后才用 Prompt 告诉模型“不要向员工泄露管理员资料”，一旦遭遇提示词注入或诱导就会失效。
- **决策**：
  - 在 `rag/store.py` 查询评分阶段之前，先按 `user_role` 对文档 `allowed_roles` 执行前置过滤；
  - 工具 Schema 不暴露 `role` 参数，由 `AgentEngine` 在运行时把不可变的 `UserContext` 注入给需要身份的 Tool Handler。
- **收益**：普通员工提问绝密哨兵与跳板机信息时，密级文档从物理检索层即不可见，实现零泄密。

### ADR-004：采用 MCP 协议解耦业务系统，并映射风险等级到 HITL 断点恢复
- **决策**：使用官方 `mcp==2.3.0` (`MCPServer`) 通过 `stdio` 暴露内部工单与运维操作，并利用 `ToolAnnotations`（`read_only_hint` / `destructive_hint`）自动映射 `READ` / `WRITE_LOW` / `HIGH_RISK`。
- **HITL 机制**：遇到 `HIGH_RISK` 工具时持久化 `ExecutionCheckpoint` 并返回 `AWAITING_APPROVAL`；人工批准后执行并继续 Loop，**人工驳回时则把审批人意见作为工具报错回喂给模型**，让模型改派建单或进一步排查。

### ADR-005：四层纵深安全护栏与全链路 AuditTracer
- **防线设计**：
  1. 入口护栏拦截直接注入（且必须在 LLM Router 之前执行，防止浪费算力）；
  2. 代码层确定性工具 RBAC（`employee` 无法触发 `HIGH_RISK` 工具）；
  3. 工具输出包裹 `<untrusted_tool_output>` 并扫描间接注入特征；
  4. 出口 DLP 对非管理员输出脱敏内网机密模式。
- **审计合规**：`AuditTracer` 以单一 `trace_id` 串联暂停与恢复前后的所有事件，支持按时间线回放。

### ADR-006：Eval 驱动迭代与复杂度模型路由（8B + 14B）
- **实测对比**（10 场景黄金评测集 + `qwen3:14b` LLM-as-a-Judge）：
  - 首轮基线：`fixed:qwen3:8b` 70.0%（P50 10.9s）、`fixed:qwen3:14b` 70.0%（P50 21.3s）、`router:8b+14b` 80.0%（P50 19.9s，总 Token 最低）。
  - 根因分析定位 3 个缺陷（Router 与入口护栏顺序颠倒、Judge 证据窗口 800 字符截断、系统提示词未约束保留工单/版本号），修复后 `router:8b+14b` 复测达 **90.0%（9/10）**、平均忠实度 **4.86/5**，直接注入拦截耗时从 10.0s 降至 **0.0 ms**。

---

## 二、本地开发与运维排坑备忘

1. **Windows + Python `sqlite3` 文件锁**：`with sqlite3.connect(...)` 只提交事务不会关闭连接句柄，在临时目录清理时会触发 `WinError 32`。本项目所有 Store 均采用 `@contextmanager` 在 `finally` 中显式 `conn.close()`。
2. **显存与并发权衡**：在 16GB 显存（RTX 5060 Ti）上，`qwen3:8b`（7.5GB @ 16K ctx）与 `bge-m3`（689MB）可长期共驻；批量评测切换 14B Judge 时，应将 Agent 执行阶段与 Judge 评分阶段分批执行，避免频繁换模抖动。
