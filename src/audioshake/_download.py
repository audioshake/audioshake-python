"""Streamed downloads of task outputs with progress and whole-file retries."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import _messages, _retry
from ._http import classify_transport_error
from .exceptions import APIConnectionError, error_from_response

log = logging.getLogger("audioshake.http")

# Progress is reported with (bytes_written, total_bytes); total is 0 when unknown.
# ``object``: the return value is ignored, so any ``(sent, total)`` callable will do.
ProgressCallback: TypeAlias = Callable[[int, int], object]

_CHUNK = 64 * 1024
_REPORT_EVERY = 512 * 1024

# Presigned links are served from a CDN, so transfers parallelise almost linearly
# up to the client's own bandwidth. Eight keeps a fast pipe busy without opening
# more connections than a run can usefully use.
DEFAULT_CONCURRENCY = 8


@dataclass(frozen=True)
class TransferJob:
    """One file to fetch: where from, where to, and who to tell about progress."""

    url: str
    dest: Path
    on_progress: ProgressCallback | None = None


def download(
    client: httpx.Client,
    url: str,
    dest: Path,
    *,
    retries: int = _retry.DEFAULT_RETRIES,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Stream ``url`` to ``dest``, retrying the whole transfer on transient failure.

    The file is written to a temporary sibling and renamed into place only when
    complete, so a failed download never leaves a truncated file behind.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    last_error: APIConnectionError | None = None

    # Every exit that does not rename the file into place removes it: a
    # permanent status after a partial attempt, Ctrl-C, cancellation, or a
    # progress callback that raises.
    try:
        for attempt in range(1, retries + 2):
            response_for_delay: httpx.Response | None = None
            try:
                with client.stream("GET", url) as response:
                    if not response.is_success:
                        if _retry.is_retryable_status(response.status_code) and attempt <= retries:
                            response_for_delay = response
                            last_error = None
                        else:
                            response.read()
                            raise error_from_response(response, _messages.ACTION_DOWNLOAD_OUTPUT)
                    else:
                        _write_body(response, partial, on_progress)
                        partial.replace(dest)
                        return dest
            except httpx.TransportError as e:
                last_error = classify_transport_error(e, _messages.ACTION_DOWNLOAD_OUTPUT)

            if attempt > retries:
                break
            delay = _retry.delay_before_retry(attempt, response_for_delay)
            log.warning(_messages.DOWNLOAD_RETRY, _redact(url), attempt, retries, delay)
            _retry.sleep(delay)

        if last_error is not None:
            raise last_error
        raise APIConnectionError(_messages.download_retries_exhausted(retries, url))
    finally:
        partial.unlink(missing_ok=True)


async def async_download(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    retries: int = _retry.DEFAULT_RETRIES,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Async twin of :func:`download`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    last_error: APIConnectionError | None = None

    # Every exit that does not rename the file into place removes it: a
    # permanent status after a partial attempt, Ctrl-C, cancellation, or a
    # progress callback that raises.
    try:
        for attempt in range(1, retries + 2):
            response_for_delay: httpx.Response | None = None
            try:
                async with client.stream("GET", url) as response:
                    if not response.is_success:
                        if _retry.is_retryable_status(response.status_code) and attempt <= retries:
                            response_for_delay = response
                            last_error = None
                        else:
                            await response.aread()
                            raise error_from_response(response, _messages.ACTION_DOWNLOAD_OUTPUT)
                    else:
                        await _async_write_body(response, partial, on_progress)
                        partial.replace(dest)
                        return dest
            except httpx.TransportError as e:
                last_error = classify_transport_error(e, _messages.ACTION_DOWNLOAD_OUTPUT)

            if attempt > retries:
                break
            delay = _retry.delay_before_retry(attempt, response_for_delay)
            log.warning(_messages.DOWNLOAD_RETRY, _redact(url), attempt, retries, delay)
            await _retry.async_sleep(delay)

        if last_error is not None:
            raise last_error
        raise APIConnectionError(_messages.download_retries_exhausted(retries, url))
    finally:
        partial.unlink(missing_ok=True)


def _redact(url: str) -> str:
    """A presigned link is a credential; log it without its query string."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _content_length(response: httpx.Response) -> int:
    try:
        return int(response.headers.get("content-length", 0))
    except ValueError:
        return 0


async def _async_write_body(
    response: httpx.Response, path: Path, on_progress: ProgressCallback | None
) -> None:
    total = _content_length(response)
    written = 0
    next_report = _REPORT_EVERY
    # The network side streams asynchronously; each local write is a short,
    # bounded disk write, which an executor round-trip would only slow down.
    with path.open("wb") as fh:
        async for chunk in response.aiter_bytes(_CHUNK):
            fh.write(chunk)
            written += len(chunk)
            if on_progress and written >= next_report:
                on_progress(written, total)
                next_report = written + _REPORT_EVERY
    if on_progress:
        on_progress(written, total or written)


def _write_body(response: httpx.Response, path: Path, on_progress: ProgressCallback | None) -> None:
    total = _content_length(response)
    written = 0
    next_report = _REPORT_EVERY
    with path.open("wb") as fh:
        for chunk in response.iter_bytes(_CHUNK):
            fh.write(chunk)
            written += len(chunk)
            if on_progress and written >= next_report:
                on_progress(written, total)
                next_report = written + _REPORT_EVERY
    if on_progress:
        on_progress(written, total or written)


class TransferPool:
    """Bounds how many output transfers run at once, across one client.

    The pool is held by the client rather than created per call, so a batch that
    already runs several sources at a time cannot multiply its way into hundreds
    of open connections: every transfer the client makes shares this budget.
    Each transfer takes a slot, whether it runs on the pool's threads or, for a
    single output, on the caller's own thread.
    """

    def __init__(self, max_concurrent: int = DEFAULT_CONCURRENCY) -> None:
        if max_concurrent < 1:
            raise ValueError(_messages.NON_POSITIVE_CONCURRENCY)
        self.max_concurrent = max_concurrent
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_concurrent)

    def _get_executor(self) -> ThreadPoolExecutor:
        """Created on first use, so a client that never downloads starts no threads."""
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.max_concurrent, thread_name_prefix="audioshake-transfer"
                )
            return self._executor

    def run(
        self,
        client: httpx.Client,
        jobs: Sequence[TransferJob],
        *,
        retries: int = _retry.DEFAULT_RETRIES,
    ) -> list[Path]:
        """Fetch every job, at most ``max_concurrent`` at a time, in job order.

        Every transfer is allowed to settle before the first failure is raised,
        so a failing job never leaves another one's ``.part`` file behind.
        """
        if not jobs:
            return []
        if len(jobs) == 1 or self.max_concurrent == 1:
            # No thread hop for these; _one still waits for a slot like any other.
            return [self._one(client, job, retries) for job in jobs]

        executor = self._get_executor()
        futures: dict[Future[Path], int] = {
            executor.submit(self._one, client, job, retries): i for i, job in enumerate(jobs)
        }
        done: list[Path | None] = [None] * len(jobs)
        errors: list[BaseException | None] = [None] * len(jobs)
        for future, index in futures.items():
            try:
                done[index] = future.result()
            except BaseException as e:  # noqa: BLE001 - re-raised in job order below
                errors[index] = e

        for error in errors:
            if error is not None:
                raise error
        return [path for path in done if path is not None]

    def _one(self, client: httpx.Client, job: TransferJob, retries: int) -> Path:
        with self._slots:
            return download(client, job.url, job.dest, retries=retries, on_progress=job.on_progress)

    def close(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)


class AsyncTransferPool:
    """Async twin of :class:`TransferPool`."""

    def __init__(self, max_concurrent: int = DEFAULT_CONCURRENCY) -> None:
        if max_concurrent < 1:
            raise ValueError(_messages.NON_POSITIVE_CONCURRENCY)
        self.max_concurrent = max_concurrent
        self._semaphore: asyncio.Semaphore | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Created on first use, inside the loop the transfers will run on."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        return self._semaphore

    async def run(
        self,
        client: httpx.AsyncClient,
        jobs: Sequence[TransferJob],
        *,
        retries: int = _retry.DEFAULT_RETRIES,
    ) -> list[Path]:
        """See :meth:`TransferPool.run`."""
        if not jobs:
            return []
        if len(jobs) == 1 or self.max_concurrent == 1:
            return [await self._one(client, job, retries) for job in jobs]

        settled = await asyncio.gather(
            *(self._one(client, job, retries) for job in jobs), return_exceptions=True
        )
        for result in settled:
            if isinstance(result, BaseException):
                raise result
        return [result for result in settled if isinstance(result, Path)]

    async def _one(self, client: httpx.AsyncClient, job: TransferJob, retries: int) -> Path:
        # Every transfer takes a slot, a lone one included, so concurrent calls
        # with one output each still share the client's budget.
        async with self._get_semaphore():
            return await async_download(
                client, job.url, job.dest, retries=retries, on_progress=job.on_progress
            )
