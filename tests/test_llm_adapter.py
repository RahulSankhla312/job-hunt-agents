"""LLM adapter: response parsing and CLI result extraction.

The JSON recovery matters more than it looks. A tailoring response that hits
the token cap partway through is otherwise thrown away entirely, costing a
whole second LLM call to redo work that was nearly complete.
"""

from __future__ import annotations

import json

import pytest

from jobhunt.llm.adapter import LLMError, _extract_cli_result, extract_json

# --- well-formed ----------------------------------------------------------


def test_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_wrapped_in_prose():
    assert extract_json('Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}


def test_array_response():
    assert extract_json('[{"id": 1}, {"id": 2}]') == [{"id": 1}, {"id": 2}]


# --- truncated ------------------------------------------------------------


def test_truncated_object_is_recovered():
    """Cut off mid-value: keep the complete keys, drop the partial one."""
    truncated = '{"summary": "ok", "experience": [{"index": 0, "bullets": ["one", "two'
    result = extract_json(truncated)
    assert result["summary"] == "ok"


def test_truncated_array_of_scores_is_recovered():
    """A scoring batch cut off after two of five postings still yields two."""
    truncated = '[{"id": 1, "scores": {"a": 5}}, {"id": 2, "scores": {"a": 4}}, {"id": 3, "sco'
    result = extract_json(truncated)
    assert isinstance(result, list)
    assert [r["id"] for r in result] == [1, 2]


def test_truncated_with_trailing_comma():
    result = extract_json('{"a": 1, "b": 2,')
    assert result == {"a": 1, "b": 2}


def test_braces_inside_strings_do_not_confuse_the_repair():
    payload = '{"note": "use {curly} braces [here]", "n": 1}'
    assert extract_json(payload) == {"note": "use {curly} braces [here]", "n": 1}


def test_unrecoverable_input_raises():
    with pytest.raises(LLMError):
        extract_json("there is no json here at all")


# --- CLI envelope ---------------------------------------------------------


def test_cli_result_field_is_unwrapped():
    payload = json.dumps({"type": "result", "result": '{"a": 1}', "is_error": False})
    assert _extract_cli_result(payload) == '{"a": 1}'


def test_cli_error_is_raised_not_returned():
    payload = json.dumps({"type": "result", "result": "rate limited", "is_error": True})
    with pytest.raises(LLMError):
        _extract_cli_result(payload)


def test_cli_non_json_output_passes_through():
    """A CLI schema change must degrade, not break outright."""
    assert _extract_cli_result("just some text") == "just some text"


def test_cli_empty_output_raises():
    with pytest.raises(LLMError):
        _extract_cli_result("   ")
