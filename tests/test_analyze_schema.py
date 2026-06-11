"""Unit tests for the pure Analyze helpers. No AWS."""

from __future__ import annotations

from agg.analyze.schema import (
    ContentBlock,
    ExecResult,
    extract_code,
    parse_invoke_result,
    result_to_events,
)

# --- extract_code -----------------------------------------------------------


def test_extract_code_from_python_fence():
    raw = "Here is the script:\n```python\nimport numpy as np\nprint(np.mean([1,2,3]))\n```\nDone."
    assert extract_code(raw) == "import numpy as np\nprint(np.mean([1,2,3]))"


def test_extract_code_bare_fence():
    assert extract_code("```\nprint(1)\n```") == "print(1)"


def test_extract_code_unfenced_is_treated_as_code():
    assert extract_code("print('hi')") == "print('hi')"


def test_extract_code_first_block_when_multiple():
    raw = "```python\nprint(1)\n```\nand\n```python\nprint(2)\n```"
    assert extract_code(raw) == "print(1)"


# --- parse_invoke_result ----------------------------------------------------


def test_parse_invoke_result_from_stream_wrapper():
    raw = {
        "stream": {
            "result": {
                "content": [
                    {"type": "text", "text": "mean = 2.0"},
                    {"type": "image", "data": "QUJD", "mimeType": "image/png"},
                ],
                "isError": False,
            }
        }
    }
    res = parse_invoke_result(raw, elapsed_s=1.5)
    assert res.stdout == "mean = 2.0"
    assert len(res.images) == 1
    assert res.images[0].mime_type == "image/png"
    assert res.elapsed_s == 1.5
    assert res.is_error is False


def test_parse_invoke_result_from_bare_result():
    res = parse_invoke_result(
        {"result": {"content": [{"type": "text", "text": "x"}], "isError": True}}
    )
    assert res.stdout == "x"
    assert res.is_error is True


def test_parse_invoke_result_empty():
    res = parse_invoke_result({"result": {}})
    assert res.content == []
    assert res.stdout == ""


# --- result_to_events -------------------------------------------------------


def test_result_to_events_text_and_chart():
    res = ExecResult(
        content=[
            ContentBlock(type="text", text="answer = 42"),
            ContentBlock(type="image", data="QUJD", mime_type="image/png"),
        ]
    )
    events = result_to_events(res)
    assert events[0] == {"type": "answer", "text": "answer = 42"}
    assert events[1] == {"type": "chart", "mime": "image/png", "data": "QUJD"}


def test_result_to_events_error_titles_the_answer():
    res = ExecResult(content=[ContentBlock(type="text", text="Traceback ...")], is_error=True)
    events = result_to_events(res)
    assert events[0]["title"] == "Analyze — error"
    assert "Traceback" in events[0]["text"]


def test_result_to_events_empty_when_no_output():
    assert result_to_events(ExecResult(content=[])) == []


def test_result_to_events_carries_pane():
    res = ExecResult(content=[ContentBlock(type="text", text="x")])
    assert result_to_events(res, pane="frontier")[0]["pane"] == "frontier"
