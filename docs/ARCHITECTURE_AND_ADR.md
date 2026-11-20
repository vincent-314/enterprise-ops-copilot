# Architecture Decision Records (ADR) & Engineering Retrospective

This document captures the key architectural trade-offs, production pitfalls, and verification results from building **Enterprise Ops Copilot** from first principles.

---

## ADR-001: Native Async ReAct Loop Over Heavyweight Black-Box Orchestration

- **Context**: Wrapping an enterprise ops agent inside heavyweight frameworks often obscures message-history pruning, malformed tool-call JSON recovery, loop circuit breaking, and state serialization for human-in-the-loop (HITL) pauses.
- **Decision**: Implemented an explicit, inspectable async ReAct loop in `src/enterprise_ops_agent/runtime/engine.py` paired with a Pydantic-first `ToolRegistry` (`tools/registry.py`).
- **Trade-offs & Payoff**:
  - Tool JSON decode failures and Pydantic validation errors are converted into structured `tool` error messages so the model self-corrects rather than crashing the loop.
  - Enforces deterministic safety bounds: `max_steps`, `max_total_tokens`, consecutive duplicate call-fingerprint circuit breaking, and atomic tool-group context pruning (`prune_messages`).
  - Cleanly embeds HITL checkpoints, 4-layer guardrails, and step-level audit tracing with zero framework magic.

## ADR-002: Diagnosing Ollama `/v1` Context Window Truncation

- **Empirical Finding**: During local `qwen3:8b` integration, `ollama ps` revealed that Ollama's OpenAI-compatible `/v1/chat/completions` endpoint silently ignored `extra_body={"options": {"num_ctx": 16384}}` and defaulted to `4096` tokens—silently truncating long system prompts and multi-hop RAG context.
- **Decision**: Enforce `OLLAMA_CONTEXT_LENGTH=16384` at the inference server process level and remove misleading client-side `extra_body` overrides with explicit documentation.

## ADR-003: Pre-Filter RBAC + Runtime Identity Injection in Hybrid RAG

- **Context**: Post-retrieval filtering via system prompts (*"do not reveal admin secrets to employees"*) fails immediately under prompt injection or model noncompliance.
- **Decision**:
  - Enforce ACL filtering **before** similarity/BM25 scoring in `rag/store.py` (`allowed_roles`) and `storage/postgres.py` (`WHERE '*' = ANY(d.allowed_roles) OR %s = ANY(d.allowed_roles)`).
  - Omit `role` from the LLM tool JSON schema completely; `AgentEngine` injects the immutable server-side `UserContext` directly into context-bound tool handlers.
- **Payoff**: Classified SRE runbooks never enter the retrieval candidate set or LLM context for unauthorized roles.

## ADR-004: Decoupling Operations Tools via MCP + Risk-Gated HITL Checkpoints

- **Decision**: Expose ticket, service metrics, deployment history, and remediation actions over an official `mcp==2.3.0` stdio server (`mcp_bridge/ops_server.py`), mapping MCP `ToolAnnotations` (`readOnlyHint`, `destructiveHint`) to `READ` / `WRITE_LOW` / `HIGH_RISK`.
- **HITL Flow**: Invoking a `HIGH_RISK` tool persists an `ExecutionCheckpoint` and returns `AWAITING_APPROVAL`. Approvals execute the gated tool and resume the loop; **rejections feed the reviewer's comment back to the model as a tool error** so the agent adapts cleanly (e.g., files an investigative ticket instead).

## ADR-005: 4-Layer Defense-in-Depth Guardrails & OTel-Compatible Audit Tracing

1. **Layer 1 (Input Guardrail)**: Deterministic direct-injection & prompt-leak detection executed **before** the LLM complexity router (cutting jailbreak latency from `10.0s` to `0.0ms` and `0` tokens).
2. **Layer 2 (Deterministic Tool RBAC)**: Code-level check blocking `employee` callers from invoking `HIGH_RISK` tools before HITL or execution.
3. **Layer 3 (Indirect Injection Sandboxing)**: External tool payloads are scanned and wrapped in `<untrusted_tool_output injection_suspected="...">` with explicit data-vs-instruction boundaries.
4. **Layer 4 (Output DLP Redaction)**: Strips restricted infrastructure secrets (bastion hosts, internal management subnets, break-glass prefixes) for non-admin callers.
- **Observability**: `AuditTracer` links pause and resume spans under one `trace_id`, with optional `OtelSpanExporter` emitting W3C/OTLP JSON spans (`gen_ai.*`, `enterprise.*`) to JSONL or any OTLP/HTTP collector (Jaeger/Tempo/Langfuse).

## ADR-006: Eval-Driven Iteration & Complexity Model Routing (`8B` + `14B`)

- Evaluated a 10-case golden suite with deterministic trajectory/security assertions plus `qwen3:14b` LLM-as-a-Judge:
  - Baseline `fixed:qwen3:8b`: 70.0% pass rate (P50 `10.9s`); `fixed:qwen3:14b`: 70.0% (P50 `21.3s`).
  - Root-cause fixes (guardrail-before-router ordering, expanding judge tool-evidence window, explicit ticket/version/permission-denial grounding rules) pushed `router:8b+14b` to **90.0% (9/10)** with **4.86/5** average faithfulness.

## ADR-007: Closing the HTTP Identity Boundary & Dual SQLite/PostgreSQL Backends

- **HTTP Auth Hardening**: While `AgentEngine` never trusts the LLM for `UserContext`, trusting raw `role` fields in HTTP JSON requests would leave an API privilege-escalation gap. `api/auth.py` introduces stateless HMAC-SHA256 Bearer tokens (`AUTH_MODE=token` by default): `POST /api/chat` and `/api/chat/stream` derive identity strictly from the verified token, `POST /api/approvals/{id}` returns `403` unless the caller holds `ops_admin` or `sec_admin`, and `GET /api/traces/{id}` enforces per-user isolation for standard employees.
- **Dual Storage Backends**: `storage/__init__.py` keeps zero-config SQLite as the local/CI default while supporting `STORAGE_BACKEND=postgres` (`PostgresKnowledgeStore` with `pgvector`, `PostgresCheckpointStore`, `PostgresAuditTracer`, `PostgresOpsStore`) via `docker compose --profile postgres up`.
