"""HTTP transport: client construction, authentication, and the retry loop."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import _messages, _retry
from ._version import __version__
from .exceptions import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    decode_json,
    error_from_response,
)

log = logging.getLogger("audioshake.http")

DEFAULT_BASE_URL = "https://api.audioshake.ai"
API_KEY_ENV = "AUDIOSHAKE_API_KEY"
BASE_URL_ENV = "AUDIOSHAKE_BASE_URL"

# Connect fast or fail; give responses a generous read window.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Uploads and downloads move whole files; the read timeout applies per chunk.
TRANSFER_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

USER_AGENT = f"audioshake-python/{__version__}"

# Hosts where plain http keeps the credential on the machine.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def resolve_credentials(api_key: str | None, bearer_token: str | None) -> dict[str, str]:
    """Return the auth header to send.

    An explicit API key wins, then an explicit bearer token (from
    :func:`audioshake.auth.login`), then ``AUDIOSHAKE_API_KEY``.
    """
    if api_key:
        return {"x-api-key": api_key}
    if bearer_token:
        return {"Authorization": f"Bearer {bearer_token}"}
    env_key = os.environ.get(API_KEY_ENV)
    if env_key:
        return {"x-api-key": env_key}
    raise AuthenticationError(_messages.no_credentials(API_KEY_ENV), status=401)


def resolve_base_url(base_url: str | None) -> str:
    """Return the API base URL, falling back to the environment, then the default.

    Warns when the URL is plain http to a host off this machine: the API key or
    bearer token would go out in the clear, readable by anything on the path.
    """
    url = (base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme == "http" and (parsed.hostname or "") not in LOOPBACK_HOSTS:
        log.warning(_messages.plain_http_warning(url))
    return url


def build_headers(credentials: dict[str, str]) -> dict[str, str]:
    return {**credentials, "Accept": "application/json", "User-Agent": USER_AGENT}


def build_client(
    credentials: dict[str, str],
    base_url: str,
    timeout: float | httpx.Timeout | None,
) -> httpx.Client:
    """Create the authenticated client used for API calls."""
    return httpx.Client(base_url=base_url, headers=build_headers(credentials), timeout=timeout)


def build_transfer_client() -> httpx.Client:
    """Create the unauthenticated client used to fetch presigned output links.

    Output links point at object storage, not the API, so the API key must not
    be sent along with them.
    """
    return httpx.Client(timeout=TRANSFER_TIMEOUT, follow_redirects=True)


def build_async_client(
    credentials: dict[str, str],
    base_url: str,
    timeout: float | httpx.Timeout | None,
) -> httpx.AsyncClient:
    """Async twin of :func:`build_client`."""
    return httpx.AsyncClient(base_url=base_url, headers=build_headers(credentials), timeout=timeout)


def build_async_transfer_client() -> httpx.AsyncClient:
    """Async twin of :func:`build_transfer_client`."""
    return httpx.AsyncClient(timeout=TRANSFER_TIMEOUT, follow_redirects=True)


def classify_transport_error(error: httpx.TransportError, action: str) -> APIConnectionError:
    """Map an httpx transport failure onto our exception types."""
    if isinstance(error, httpx.TimeoutException):
        return APITimeoutError(_messages.transport_timeout(action, error))
    return APIConnectionError(_messages.transport_failure(action, error))


class HttpSession:
    """A thin retrying wrapper over :class:`httpx.Client`.

    Connection failures, timeouts and retryable statuses (408, 425, 429, 5xx)
    are re-sent with exponential backoff up to ``retries`` times. Anything else
    that is not a 2xx raises the matching :class:`~audioshake.exceptions.APIError`.
    """

    def __init__(self, client: httpx.Client, *, retries: int = _retry.DEFAULT_RETRIES) -> None:
        if retries < 0:
            raise ValueError(_messages.NEGATIVE_RETRIES)
        self._client = client
        self.retries = retries

    @property
    def client(self) -> httpx.Client:
        return self._client

    def request(
        self,
        method: str,
        path: str,
        *,
        action: str,
        before_attempt: Callable[[], None] | None = None,
        idempotent: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a request, retrying transient failures, and return a 2xx response.

        Args:
            method: HTTP method.
            path: Path relative to the client's base URL.
            action: What this call does, in the infinitive, for error messages
                (for example ``"create task"``).
            before_attempt: Called before every attempt, so a streamed body can
                be rewound before it is re-sent.
            idempotent: Whether the request is safe to run twice. Set False
                for one that must not: a lost response or a 500/502/504 does
                not mean the server did nothing, so neither is retried. Only
                statuses where the server declined to act (408, 425, 429, 503)
                are.
            **kwargs: Passed through to :meth:`httpx.Client.request`.
        """
        last_error: APIConnectionError | None = None

        for attempt in range(1, self.retries + 2):
            response: httpx.Response | None = None
            if before_attempt is not None:
                before_attempt()
            try:
                log.debug("%s %s (attempt %d)", method, path, attempt)
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as e:
                last_error = classify_transport_error(e, action)
                if not idempotent:
                    raise last_error from e
            else:
                if response.is_success:
                    return response
                if not _retry.is_retryable_status(response.status_code, idempotent=idempotent):
                    raise error_from_response(response, action)
                last_error = None

            if attempt > self.retries:
                break

            delay = _retry.delay_before_retry(attempt, response)
            reason = str(last_error) if last_error else f"HTTP {response.status_code}"  # type: ignore[union-attr]
            log.warning(
                _messages.REQUEST_RETRY,
                method,
                path,
                reason,
                attempt,
                self.retries,
                delay,
            )
            _retry.sleep(delay)

        if last_error is not None:
            raise last_error
        assert response is not None  # the loop always ends with a response or an error
        raise error_from_response(response, action)

    def get_json(self, path: str, *, action: str, **kwargs: Any) -> Any:
        return decode_json(self.request("GET", path, action=action, **kwargs), action)

    def post_json(self, path: str, *, action: str, **kwargs: Any) -> Any:
        return decode_json(self.request("POST", path, action=action, **kwargs), action)

    def close(self) -> None:
        self._client.close()


class AsyncHttpSession:
    """Async twin of :class:`HttpSession`, over :class:`httpx.AsyncClient`."""

    def __init__(self, client: httpx.AsyncClient, *, retries: int = _retry.DEFAULT_RETRIES) -> None:
        if retries < 0:
            raise ValueError(_messages.NEGATIVE_RETRIES)
        self._client = client
        self.retries = retries

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        action: str,
        before_attempt: Callable[[], None] | None = None,
        idempotent: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """See :meth:`HttpSession.request`."""
        last_error: APIConnectionError | None = None

        for attempt in range(1, self.retries + 2):
            response: httpx.Response | None = None
            if before_attempt is not None:
                before_attempt()
            try:
                log.debug("%s %s (attempt %d)", method, path, attempt)
                response = await self._client.request(method, path, **kwargs)
            except httpx.TransportError as e:
                last_error = classify_transport_error(e, action)
                if not idempotent:
                    raise last_error from e
            else:
                if response.is_success:
                    return response
                if not _retry.is_retryable_status(response.status_code, idempotent=idempotent):
                    raise error_from_response(response, action)
                last_error = None

            if attempt > self.retries:
                break

            delay = _retry.delay_before_retry(attempt, response)
            reason = str(last_error) if last_error else f"HTTP {response.status_code}"  # type: ignore[union-attr]
            log.warning(
                _messages.REQUEST_RETRY,
                method,
                path,
                reason,
                attempt,
                self.retries,
                delay,
            )
            await _retry.async_sleep(delay)

        if last_error is not None:
            raise last_error
        assert response is not None
        raise error_from_response(response, action)

    async def get_json(self, path: str, *, action: str, **kwargs: Any) -> Any:
        return decode_json(await self.request("GET", path, action=action, **kwargs), action)

    async def post_json(self, path: str, *, action: str, **kwargs: Any) -> Any:
        return decode_json(await self.request("POST", path, action=action, **kwargs), action)

    async def close(self) -> None:
        await self._client.aclose()


def raise_for_status(response: httpx.Response, action: str) -> None:
    """Raise the matching :class:`APIError` if ``response`` is not a success."""
    if not response.is_success:
        raise error_from_response(response, action)


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "TRANSFER_TIMEOUT",
    "APIError",
    "AsyncHttpSession",
    "HttpSession",
    "build_async_client",
    "build_async_transfer_client",
    "build_client",
    "build_transfer_client",
    "raise_for_status",
    "resolve_base_url",
    "resolve_credentials",
]
