import httpx
import pytest

from audioshake import _retry


def test_backoff_is_exponential_capped_and_jittered():
    for attempt in range(1, 12):
        nominal = min(_retry.BASE_DELAY * 2 ** (attempt - 1), _retry.MAX_DELAY)
        delay = _retry.backoff_delay(attempt)
        assert nominal * 0.5 <= delay <= nominal


def test_retryable_statuses():
    assert all(_retry.is_retryable_status(s) for s in (408, 425, 429, 500, 502, 503, 504))
    assert not any(_retry.is_retryable_status(s) for s in (200, 400, 401, 403, 404, 422))


def test_non_idempotent_requests_retry_only_declined_statuses():
    retried = {s for s in _retry.RETRY_STATUSES if _retry.is_retryable_status(s, idempotent=False)}
    assert retried == {408, 425, 429, 503}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7.0),
        ("9999", _retry.MAX_DELAY),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),  # a date in the past means "now"
        ("soon", None),  # unreadable: back off as if there were no header
    ],
    ids=["seconds", "capped", "http date", "garbage"],
)
def test_retry_after_header_decides_the_delay(header, expected):
    response = httpx.Response(503, headers={"Retry-After": header})
    delay = _retry.delay_before_retry(1, response)
    if expected is None:
        assert 0 < delay <= _retry.BASE_DELAY
    else:
        assert delay == expected
