"""Small display helpers shared by the CLI."""

from __future__ import annotations

from audioshake._text import summarize_error

ERROR_DISPLAY_CHARS = 200


def format_credits(credits: float) -> str:
    return f"{credits:.2f} credits"


def credits_or_none(credits: float) -> str | None:
    """``None`` for a zero charge, so callers can leave it out of the display."""
    return format_credits(credits) if credits > 0 else None


def format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} GB"


def format_error(text: str, limit: int = ERROR_DISPLAY_CHARS) -> str:
    """One capped line of error text, for a tree row, a table cell or a summary.

    The API's error strings can carry a worker's multi-line stack trace. The
    display shows one line, with the frames dropped; the full text is logged at
    debug level and, for `--json`, passed through untouched.
    """
    return summarize_error(text, limit)


def format_transfer(written: int, total: int) -> str:
    if total > 0:
        return f"{written * 100 // total}%"
    return format_size(written)
