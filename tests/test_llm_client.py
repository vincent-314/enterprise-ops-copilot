"""Unit tests for LLM client parsing and normalization."""

from enterprise_ops_agent.llm.client import _parse_tool_arguments, _strip_code_fence


def test_parse_tool_arguments_valid() -> None:
    parsed, err = _parse_tool_arguments('{"service_name": "redis-prod-01", "limit": 5}')
    assert err is None
    assert parsed == {"service_name": "redis-prod-01", "limit": 5}


def test_parse_tool_arguments_invalid_json_captures_error() -> None:
    parsed, err = _parse_tool_arguments('{"service_name": "redis-prod-01"')
    assert parsed == {}
    assert err is not None
    assert "Invalid JSON arguments" in err


def test_parse_tool_arguments_non_dict_captures_error() -> None:
    parsed, err = _parse_tool_arguments('["not", "an", "object"]')
    assert parsed == {}
    assert err is not None
    assert "must be a JSON object" in err


def test_strip_code_fence() -> None:
    raw = '```json\n{"category": "database"}\n```'
    assert _strip_code_fence(raw) == '{"category": "database"}'
