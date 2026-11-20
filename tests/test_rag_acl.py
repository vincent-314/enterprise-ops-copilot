"""Offline unit tests for Markdown chunking, pre-filter RBAC, hybrid RRF, and UserContext binding."""

from __future__ import annotations

import json
from pathlib import Path

from enterprise_ops_agent.rag import (
    KnowledgeStore,
    parse_markdown_document,
    register_knowledge_tool,
)
from enterprise_ops_agent.tools import ToolRegistry, UserContext


class DeterministicFakeEmbedder:
    """Produces simple keyword-based unit vectors so vector ranking works deterministically offline."""

    _VOCAB = ("redis", "504", "vpn", "bastion", "10.88.24.19", "security", "mfa")

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]:
        del model
        vectors: list[list[float]] = []
        for text in texts:
            lower = text.lower()
            vec = [1.0 if kw in lower else 0.05 for kw in self._VOCAB]
            vectors.append(vec)
        return vectors


def _write_doc(directory: Path, filename: str, content: str) -> Path:
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


def test_markdown_chunker_preserves_frontmatter_and_headings(tmp_path: Path) -> None:
    doc_path = _write_doc(
        tmp_path,
        "runbook.md",
        (
            "---\n"
            "title: Redis 故障手册\n"
            "department: sre_ops\n"
            "allowed_roles: ops_admin\n"
            "---\n\n"
            "# 总览\n"
            "基础说明。\n\n"
            "## 主从切换\n"
            "登录跳板机执行切换。\n"
        ),
    )
    parsed = parse_markdown_document(doc_path)
    assert parsed.title == "Redis 故障手册"
    assert parsed.department == "sre_ops"
    assert parsed.allowed_roles == ["ops_admin"]
    assert len(parsed.chunks) == 2
    assert parsed.chunks[1].section_path == "Redis 故障手册 > 总览 > 主从切换"
    assert "[Redis 故障手册 > 总览 > 主从切换]" in parsed.chunks[1].content


async def test_prefilter_rbac_blocks_unauthorized_roles(tmp_path: Path) -> None:
    public_doc = _write_doc(
        tmp_path,
        "public_guide.md",
        (
            "---\n"
            "title: 通用排查指南\n"
            "department: engineering\n"
            "allowed_roles: employee, ops_admin\n"
            "---\n\n"
            "# Redis 超时\n"
            "出现 ERR_REDIS_POOL_200 请检查最近发布记录。\n"
        ),
    )
    secret_doc = _write_doc(
        tmp_path,
        "secret_ops.md",
        (
            "---\n"
            "title: 生产应急口令\n"
            "department: sre_ops\n"
            "allowed_roles: ops_admin\n"
            "---\n\n"
            "# 跳板机与哨兵\n"
            "生产 Redis 哨兵地址 10.88.24.19:26379，口令 OPS-BREAKGLASS-772。\n"
        ),
    )

    store = KnowledgeStore(tmp_path / "knowledge.db")
    embedder = DeterministicFakeEmbedder()
    await store.index_documents(
        [parse_markdown_document(public_doc), parse_markdown_document(secret_doc)],
        embedder,
    )

    assert store.count_accessible_documents("employee") == 1
    assert store.count_accessible_documents("ops_admin") == 2

    # Even when employee queries exact secret keywords, pre-filter ACL returns ZERO secret chunks.
    emp_hits = await store.search(
        "10.88.24.19 哨兵 OPS-BREAKGLASS-772",
        user_role="employee",
        embedder=embedder,
        top_k=4,
    )
    assert all(hit.doc_id != "secret_ops" for hit in emp_hits)

    # Admin retrieves the restricted document cleanly.
    admin_hits = await store.search(
        "10.88.24.19 哨兵 OPS-BREAKGLASS-772",
        user_role="ops_admin",
        embedder=embedder,
        top_k=4,
    )
    assert any(hit.doc_id == "secret_ops" for hit in admin_hits)


async def test_knowledge_tool_requires_and_binds_user_context(tmp_path: Path) -> None:
    doc = _write_doc(
        tmp_path,
        "secret_ops.md",
        (
            "---\n"
            "title: 生产应急\n"
            "allowed_roles: ops_admin\n"
            "---\n\n"
            "# 切换\n"
            "使用 bastion-sre-01 切换。\n"
        ),
    )
    store = KnowledgeStore(tmp_path / "knowledge.db")
    embedder = DeterministicFakeEmbedder()
    await store.index_documents([parse_markdown_document(doc)], embedder)

    registry = ToolRegistry()
    register_knowledge_tool(registry, store, embedder)

    # Schema exposed to LLM must NOT contain any role parameter to prevent spoofing.
    schema_props = registry.to_openai_tools()[0]["function"]["parameters"]["properties"]
    assert "role" not in schema_props
    assert "user_role" not in schema_props

    # Calling without UserContext fails safely.
    missing_ctx = await registry.execute("search_internal_knowledge", {"query": "bastion-sre-01"})
    assert not missing_ctx.ok
    assert missing_ctx.error_code == "MISSING_USER_CONTEXT"

    # Calling as employee returns 0 hits; calling as ops_admin returns 1 hit.
    emp_res = await registry.execute(
        "search_internal_knowledge",
        {"query": "bastion-sre-01"},
        user_context=UserContext(user_id="alice", role="employee"),
    )
    assert emp_res.ok
    assert json.loads(emp_res.output)["hit_count"] == 0

    admin_res = await registry.execute(
        "search_internal_knowledge",
        {"query": "bastion-sre-01"},
        user_context=UserContext(user_id="bob", role="ops_admin"),
    )
    assert admin_res.ok
    payload = json.loads(admin_res.output)
    assert payload["hit_count"] == 1
    assert payload["results"][0]["doc_id"] == "secret_ops"
