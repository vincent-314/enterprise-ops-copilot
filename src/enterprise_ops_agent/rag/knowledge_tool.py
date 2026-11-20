"""Context-bound knowledge search tool with strict citation and budget control."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from enterprise_ops_agent.rag.store import EmbeddingClient, KnowledgeStore
from enterprise_ops_agent.tools.registry import RiskLevel, ToolRegistry, UserContext


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(min_length=2, description="Search query in Chinese or English.")
    top_k: int = Field(default=4, ge=1, le=8, description="Max number of chunks to return.")


def register_knowledge_tool(
    registry: ToolRegistry,
    store: KnowledgeStore,
    embedder: EmbeddingClient,
    *,
    max_context_chars: int = 3500,
) -> None:
    """Register search_internal_knowledge bound to runtime UserContext ACL."""

    async def _search_internal_knowledge(
        args: SearchKnowledgeArgs,
        ctx: UserContext,
    ) -> str:
        hits = await store.search(
            args.query,
            user_role=ctx.role,
            embedder=embedder,
            top_k=args.top_k,
        )
        accessible_docs = store.count_accessible_documents(ctx.role)
        if not hits:
            return json.dumps(
                {
                    "ok": True,
                    "caller_role": ctx.role,
                    "accessible_doc_count": accessible_docs,
                    "hit_count": 0,
                    "message": (
                        "在当前角色权限范围内未检索到匹配文档（可能不存在相关内容，或相关文档仅限更高权限角色查看）。"
                    ),
                    "citations": [],
                },
                ensure_ascii=False,
            )

        selected: list[dict[str, object]] = []
        used_chars = 0
        for hit in hits:
            snippet = hit.content
            if used_chars + len(snippet) > max_context_chars and selected:
                break
            used_chars += len(snippet)
            selected.append(
                {
                    "citation": f"[{hit.doc_id} | {hit.section_path}]",
                    "doc_id": hit.doc_id,
                    "title": hit.title,
                    "section_path": hit.section_path,
                    "rrf_score": hit.rrf_score,
                    "content": snippet,
                }
            )

        return json.dumps(
            {
                "ok": True,
                "caller_role": ctx.role,
                "accessible_doc_count": accessible_docs,
                "hit_count": len(selected),
                "results": selected,
            },
            ensure_ascii=False,
        )

    registry.register(
        name="search_internal_knowledge",
        description=(
            "Search internal IT policies, troubleshooting manuals, and ops runbooks "
            "authorized for the current user's role. Always cite [doc_id | section_path]."
        ),
        args_model=SearchKnowledgeArgs,
        handler=_search_internal_knowledge,
        risk_level=RiskLevel.READ,
    )
