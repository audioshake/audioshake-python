"""Text helpers for messages people read."""

from __future__ import annotations

__all__ = ["one_line", "summarize_error"]

DEFAULT_LIMIT = 300

_TRACEBACK_HEAD = "Traceback (most recent call last)"


def one_line(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """Collapse ``text`` onto one line and cap its length.

    Error text from outside this process (the API, a proxy, a worker's stack
    trace) is shown to people and written to logs. Collapsing and capping it
    keeps one message on one line and keeps an upstream traceback out of the
    display; the full text stays available at debug level.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 1, 0)].rstrip() + "…"


def summarize_error(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """One capped line of error text, with a stack trace cut down to its message.

    Upstream sometimes answers with a whole Python traceback. Only its last line
    says what went wrong; the frames above are paths on a machine the caller has
    no access to, so they are dropped rather than shown or counted against the
    cap. The untouched text stays in ``--json`` and at debug level.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and lines[0].lstrip().startswith(_TRACEBACK_HEAD):
        text = lines[-1]
    return one_line(text, limit)
