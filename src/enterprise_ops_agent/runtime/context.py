"""Context window estimation and tool-group-safe message pruning."""

from __future__ import annotations

import json
from typing import Any

_MESSAGE_OVERHEAD_TOKENS = 12


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Conservatively estimate tokens for a single chat message (CJK-safe)."""
    total_chars = len(str(message.get("content") or ""))
    if message.get("tool_calls"):
        total_chars += len(json.dumps(message["tool_calls"], ensure_ascii=False))
    return total_chars + _MESSAGE_OVERHEAD_TOKENS


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens across a message list."""
    return sum(estimate_message_tokens(msg) for msg in messages)


def _group_atomic_units(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group assistant(tool_calls) messages with their matching tool responses."""
    units: list[list[dict[str, Any]]] = []
    idx = 0
    n = len(messages)
    while idx < n:
        msg = messages[idx]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            group = [msg]
            idx += 1
            while idx < n and messages[idx].get("role") == "tool":
                group.append(messages[idx])
                idx += 1
            units.append(group)
        else:
            units.append([msg])
            idx += 1
    return units


def prune_messages(
    messages: list[dict[str, Any]],
    max_context_tokens: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Trim oldest middle turns while keeping system prompt, initial goal, and recent steps.

    Never separates an assistant(tool_calls) message from its subsequent tool results.
    """
    if estimate_messages_tokens(messages) <= max_context_tokens or len(messages) <= 2:
        return messages, False

    pinned_head: list[dict[str, Any]] = []
    tail_start = 0
    if messages and messages[0].get("role") == "system":
        pinned_head.append(messages[0])
        tail_start = 1
    if tail_start < len(messages) and messages[tail_start].get("role") == "user":
        pinned_head.append(messages[tail_start])
        tail_start += 1

    middle_and_tail = messages[tail_start:]
    if not middle_and_tail:
        return messages, False

    units = _group_atomic_units(middle_and_tail)
    marker_msg = {
        "role": "system",
        "content": "[Context notice: earlier intermediate steps were omitted to stay within context budget.]",
    }
    head_tokens = estimate_messages_tokens(pinned_head) + estimate_message_tokens(marker_msg)
    remaining_budget = max(0, max_context_tokens - head_tokens)

    kept_units: list[list[dict[str, Any]]] = []
    used = 0
    for unit in reversed(units):
        unit_tokens = estimate_messages_tokens(unit)
        if kept_units and used + unit_tokens > remaining_budget:
            break
        kept_units.append(unit)
        used += unit_tokens

    kept_units.reverse()
    pruned = len(kept_units) < len(units)
    if not pruned:
        return messages, False

    result = list(pinned_head)
    result.append(marker_msg)
    for unit in kept_units:
        result.extend(unit)
    return result, True
