import pytest

from audioshake._text import one_line, summarize_error


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [("a\n  b\tc  ", 300, "a b c"), ("x" * 500, 10, "x" * 9 + "…")],
    ids=["whitespace and newlines collapse", "long text is capped with an ellipsis"],
)
def test_one_line(text, limit, expected):
    assert one_line(text, limit) == expected


def test_a_stack_trace_keeps_only_its_message():
    trace = (
        "Traceback (most recent call last):\n"
        '  File "/srv/worker/pipeline.py", line 88, in separate\n'
        "RuntimeError: cuda oom on node worker-1.test"
    )
    summary = summarize_error(trace)
    # The frames are paths on a machine the caller cannot reach.
    assert summary == "RuntimeError: cuda oom on node worker-1.test"
    assert "pipeline.py" not in summary
