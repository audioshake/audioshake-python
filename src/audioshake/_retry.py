"""Retry policy shared by the sync and async transports.

Only the decision logic lives here; the loops that apply it are in
:mod:`audioshake._http` and :mod:`audioshake._download`.
"""

from __future__ import annotations

import asyncio
import random
import time
from email.utils import parsedate_to_datetime

import httpx

# Retries after the first attempt, so the default is four tries in total.
DEFAULT_RETRIES = 3

# Exponential backoff bounds in seconds. Jitter spreads concurrent callers out.
BASE_DELAY = 0.5
MAX_DELAY = 30.0

# Statuses that mean "not right now" rather than "not ever".
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# The subset where the server says it declined to act, so re-sending a request
# that must not run twice is still safe. A 500, 502 or 504 is ambiguous: a
# gateway can time out after the backend has already done the work.
DECLINED_STATUSES = frozenset({408, 425, 429, 503})

# Kept as module attributes so tests can replace them without touching `time`/`asyncio`.
sleep = time.sleep
async_sleep = asyncio.sleep


def is_retryable_status(status: int, *, idempotent: bool = True) -> bool:
    """Return True for responses worth re-sending.

    Args:
        status: The response status.
        idempotent: Whether the request is safe to run twice. When False only
            :data:`DECLINED_STATUSES` qualify.
    """
    return status in (RETRY_STATUSES if idempotent else DECLINED_STATUSES)


def backoff_delay(attempt: int, *, base: float = BASE_DELAY, cap: float = MAX_DELAY) -> float:
    """Seconds to wait after a failed 1-based ``attempt``.

    Exponential and capped, jittered to 50-100% of the nominal delay so a batch
    of concurrent requests does not retry in lockstep.
    """
    delay = min(base * float(2 ** (attempt - 1)), cap)
    return float(delay * (0.5 + random.random() / 2))


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header as seconds, if the server sent one."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def delay_before_retry(attempt: int, response: httpx.Response | None) -> float:
    """How long to wait before retrying, honouring ``Retry-After`` when present."""
    if response is not None:
        hinted = retry_after_seconds(response)
        if hinted is not None:
            return min(hinted, MAX_DELAY)
    return backoff_delay(attempt)
