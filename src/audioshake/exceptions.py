"""Exception hierarchy for the AudioShake client.

Every error raised by this library derives from :class:`AudioShakeError`.
HTTP failures are :class:`APIError` subclasses chosen by status code, so callers
can catch precisely what they can handle::

    try:
        client.tasks.get(task_id)
    except NotFoundError:
        ...
    except RateLimitError:
        ...
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx

from . import _messages
from ._text import summarize_error

if TYPE_CHECKING:
    from .types import Task

__all__ = [
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "AudioShakeError",
    "AuthError",
    "AuthenticationError",
    "NotFoundError",
    "RateLimitError",
    "ServerError",
    "TaskFailedError",
    "TaskTimeoutError",
    "UnexpectedResponseError",
    "ValidationError",
]

_log = logging.getLogger("audioshake.http")


class AudioShakeError(Exception):
    """Base class for every error raised by the AudioShake client."""


class APIError(AudioShakeError):
    """The API answered with an error status.

    The message carries the API's own error text collapsed onto one capped
    line, for display. :attr:`full_message` has it untouched.

    Attributes:
        status: HTTP status code.
        body: Decoded JSON body when the response was JSON, else the raw text.
        request_id: The server's request id header when present, for support.
        full_message: The message with the API's error text uncapped.
    """

    status: int
    body: Any
    request_id: str | None
    full_message: str

    def __init__(
        self,
        message: str,
        *,
        status: int,
        body: Any = None,
        request_id: str | None = None,
        full_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.request_id = request_id
        self.full_message = full_message if full_message is not None else message


class AuthenticationError(APIError):
    """The API key is missing, invalid, or not allowed to do this (401/403)."""


class NotFoundError(APIError):
    """The resource does not exist (404)."""


class RateLimitError(APIError):
    """Too many requests (429). Retried automatically before being raised."""


class ServerError(APIError):
    """The API failed on its side (5xx). Retried automatically before being raised."""


class UnexpectedResponseError(AudioShakeError):
    """The API answered with a success status, but not with a body the library can read.

    Attributes:
        body: What the API sent: the decoded JSON, or the raw text when it was
            not JSON.
    """

    def __init__(self, message: str, *, body: Any = None) -> None:
        super().__init__(message)
        self.body = body


class AuthError(AudioShakeError):
    """Browser sign-in or token refresh could not be completed (see :mod:`audioshake.auth`)."""


class APIConnectionError(AudioShakeError):
    """The request never got an answer: DNS, connection, or protocol failure."""


class APITimeoutError(APIConnectionError):
    """The request ran out of time waiting to connect, send, or read."""


class TaskFailedError(AudioShakeError):
    """A task finished with one or more failed targets.

    Attributes:
        task: The final task, so the caller can still read the targets that succeeded.
    """

    def __init__(self, task: Task) -> None:
        super().__init__(describe_task_failure(task))
        self.task = task


def describe_task_failure(task: Task, *, full: bool = False) -> str:
    """Name each failed target of ``task`` and why.

    Shared by :class:`TaskFailedError` and by callers reporting a task that
    partly completed, so both say it the same way. Each reason is one capped
    line unless ``full``, which keeps the API's text untouched for
    machine-readable output.
    """
    reasons = [
        f"{t.model}: {_target_reason(t.error, full)}"
        if t.error
        else f"{t.model}: {_messages.NO_TARGET_ERROR}"
        for t in task.failed
    ]
    return _messages.task_failed(task.id, reasons)


def _target_reason(error: str, full: bool) -> str:
    return error if full else summarize_error(error, 200)


class TaskTimeoutError(AudioShakeError):
    """Waiting for a task exceeded the caller's timeout."""

    def __init__(self, task_id: str, timeout: float) -> None:
        super().__init__(_messages.task_timeout(task_id, timeout))
        self.task_id = task_id
        self.timeout = timeout


class ValidationError(AudioShakeError):
    """The request is wrong before it is sent: bad model, format, or argument."""


def error_from_response(response: httpx.Response, action: str) -> APIError:
    """Build the most specific :class:`APIError` for a non-2xx response.

    Args:
        response: The failed response.
        action: What was being attempted, used to prefix the message
            (for example ``"upload asset"``).
    """
    body = _decode_body(response)
    # The body may carry upstream infrastructure detail (a proxy's HTML error
    # page, a worker's stack trace). Keep it off the message and out of the
    # default output, but available at debug level and on the exception.
    _log.debug("%s failed: HTTP %s body=%r", action, response.status_code, body)
    detail = _message_from_body(body) or response.reason_phrase or ""
    message = _messages.api_error(action, response.status_code, summarize_error(detail))
    full_message = _messages.api_error(action, response.status_code, detail)
    request_id = response.headers.get("x-request-id")

    status = response.status_code
    cls: type[APIError]
    if status in (401, 403):
        cls = AuthenticationError
    elif status == 404:
        cls = NotFoundError
    elif status == 429:
        cls = RateLimitError
    elif status >= 500:
        cls = ServerError
    else:
        cls = APIError
    return cls(message, status=status, body=body, request_id=request_id, full_message=full_message)


def decode_json(response: httpx.Response, action: str) -> Any:
    """The JSON body of a success response.

    Raises:
        UnexpectedResponseError: The body is not JSON. The body is logged at
            debug level and kept on the exception, not put in the message: an
            HTML page from a proxy can carry upstream infrastructure detail.
    """
    try:
        return response.json()
    except ValueError:
        _log.debug("%s: body is not JSON: %r", action, response.text)
        raise UnexpectedResponseError(
            _messages.response_not_json(action, response.status_code), body=response.text
        ) from None


def expect_object(data: Any, action: str, expected: str) -> Mapping[str, Any]:
    """``data`` as a JSON object carrying a non-empty ``id``.

    Raises:
        UnexpectedResponseError: It is not one.
    """
    if isinstance(data, Mapping) and isinstance(data.get("id"), str) and data["id"]:
        return data
    raise UnexpectedResponseError(_messages.response_wrong_shape(action, expected), body=data)


def expect_objects(data: Any, key: str, action: str, expected: str) -> list[Mapping[str, Any]]:
    """The list of JSON objects in ``data``: a bare list, or one under ``key``.

    Raises:
        UnexpectedResponseError: It is neither.
    """
    items = data.get(key) if isinstance(data, Mapping) else data
    if isinstance(items, list) and all(isinstance(item, Mapping) for item in items):
        return items
    raise UnexpectedResponseError(_messages.response_wrong_shape(action, expected), body=data)


def _decode_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _message_from_body(body: Any) -> str:
    """The API's own error text, untouched, when the body is JSON that carries one.

    A body that is not JSON is upstream infrastructure detail rather than
    something the caller can act on, so it is never put in the message: the
    status line speaks for it. It stays on :attr:`APIError.body`.
    """
    if not isinstance(body, dict):
        return ""
    for key in ("message", "error", "detail"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
