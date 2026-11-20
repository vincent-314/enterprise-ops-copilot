"""Markdown frontmatter parser and heading-aware chunker for enterprise docs."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")


class DocumentChunk(BaseModel):
    """Single searchable chunk carrying inherited document ACL metadata."""

    chunk_id: str
    doc_id: str
    title: str
    section_path: str
    chunk_index: int
    content: str
    allowed_roles: list[str]
    department: str


class ParsedDocument(BaseModel):
    """Parsed Markdown document with ACL metadata and ordered chunks."""

    doc_id: str
    title: str
    source_path: str
    department: str
    allowed_roles: list[str]
    chunks: list[DocumentChunk] = Field(default_factory=list)


def _parse_frontmatter(raw_text: str, fallback_title: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(raw_text.lstrip("\ufeff"))
    if not match:
        return {"title": fallback_title}, raw_text
    meta_block, body = match.group(1), match.group(2)
    meta: dict[str, str] = {}
    for line in meta_block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip()
    meta.setdefault("title", fallback_title)
    return meta, body


def _split_sections(body: str, doc_title: str) -> list[tuple[str, str]]:
    """Split Markdown body into (section_path, text) segments preserving headings."""
    heading_stack: list[tuple[int, str]] = []
    sections: list[tuple[str, str]] = []
    current_lines: list[str] = []
    current_path = doc_title

    for line in body.splitlines():
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            content = "\n".join(current_lines).strip()
            if content:
                sections.append((current_path, content))
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            heading_stack = [(lvl, t) for lvl, t in heading_stack if lvl < level]
            heading_stack.append((level, title))
            current_path = " > ".join([doc_title, *(t for _, t in heading_stack)])
            current_lines = []
        else:
            current_lines.append(line)

    tail = "\n".join(current_lines).strip()
    if tail:
        sections.append((current_path, tail))
    return sections


def _window_text(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    step = max(1, max_chars - overlap)
    while start < len(text):
        end = min(len(text), start + max_chars)
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start += step
    return chunks


def parse_markdown_document(
    path: Path,
    *,
    max_chunk_chars: int = 550,
    chunk_overlap: int = 80,
) -> ParsedDocument:
    """Parse a Markdown knowledge document into ACL-tagged contextual chunks."""
    raw = path.read_text(encoding="utf-8")
    doc_id = path.stem
    meta, body = _parse_frontmatter(raw, fallback_title=doc_id)

    title = meta.get("title", doc_id)
    department = meta.get("department", "general")
    raw_roles = meta.get("allowed_roles", "employee, ops_admin")
    allowed_roles = [r.strip() for r in raw_roles.split(",") if r.strip()]

    sections = _split_sections(body, doc_title=title)
    chunks: list[DocumentChunk] = []
    idx = 0
    for section_path, section_text in sections:
        for piece in _window_text(section_text, max_chunk_chars, chunk_overlap):
            contextual_text = f"[{section_path}]\n{piece}"
            chunks.append(
                DocumentChunk(
                    chunk_id=f"{doc_id}::c{idx}",
                    doc_id=doc_id,
                    title=title,
                    section_path=section_path,
                    chunk_index=idx,
                    content=contextual_text,
                    allowed_roles=allowed_roles,
                    department=department,
                )
            )
            idx += 1

    return ParsedDocument(
        doc_id=doc_id,
        title=title,
        source_path=str(path),
        department=department,
        allowed_roles=allowed_roles,
        chunks=chunks,
    )
