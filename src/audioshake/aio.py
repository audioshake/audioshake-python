"""The asynchronous AudioShake client.

Same surface as :class:`audioshake.AudioShake`, every method awaitable::

    async with AsyncAudioShake() as client:
        asset = await client.assets.upload("song.wav")
        task = await client.tasks.create(asset, [Target("vocals")])
        task = await client.tasks.wait(task.id)
        await client.tasks.download(task, "out/")
"""

from __future__ import annotations

import builtins
import logging
import mimetypes
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from typing_extensions import Self

from . import _messages, _retry
from ._download import DEFAULT_CONCURRENCY, AsyncTransferPool, ProgressCallback, TransferJob
from ._http import (
    DEFAULT_TIMEOUT,
    TRANSFER_TIMEOUT,
    AsyncHttpSession,
    build_async_client,
    build_async_transfer_client,
    build_headers,
    resolve_base_url,
    resolve_credentials,
)
from ._run import (
    ProgressEvent,
    ProgressHandler,
    build_targets,
    is_url,
    safe_name,
)
from .client import (
    _CONTENT_TYPES,
    _bind_output,
    _collect_page,
    _completed_part,
    _page_size,
    _ProgressReader,
    _task_id,
    _task_list_bounds,
    _task_page,
    _task_payload,
    _warn_if_task_may_exist,
    planned_outputs,
)
from .exceptions import (
    APIConnectionError,
    AudioShakeError,
    ServerError,
    TaskFailedError,
    TaskTimeoutError,
    ValidationError,
    expect_object,
    expect_objects,
)
from .types import Asset, Model, Output, RunResult, StrPath, Target, Task

__all__ = ["AsyncAssets", "AsyncAudioShake", "AsyncModels", "AsyncTasks"]

_log = logging.getLogger("audioshake.tasks")


class AsyncAudioShake:
    """Async client for the AudioShake API. See :class:`audioshake.AudioShake` for arguments."""

    assets: AsyncAssets
    tasks: AsyncTasks
    models: AsyncModels

    def __init__(
        self,
        api_key: str | None = None,
        *,
        bearer_token: str | None = None,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
        retries: int = _retry.DEFAULT_RETRIES,
        download_concurrency: int = DEFAULT_CONCURRENCY,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        credentials = resolve_credentials(api_key, bearer_token)
        url = resolve_base_url(base_url)

        if http_client is None:
            http_client = build_async_client(credentials, url, timeout)
        else:
            http_client.headers.update(build_headers(credentials))
            if not str(http_client.base_url):
                http_client.base_url = httpx.URL(url)

        self._http = AsyncHttpSession(http_client, retries=retries)
        self._transfer = build_async_transfer_client()
        self._transfers = AsyncTransferPool(download_concurrency)
        self.assets = AsyncAssets(self._http)
        self.tasks = AsyncTasks(self._http, self._transfer, self._transfers)
        self.models = AsyncModels(self._http)

    @property
    def base_url(self) -> str:
        """The API host this client talks to."""
        return str(self._http.client.base_url).rstrip("/")

    @property
    def retries(self) -> int:
        """How many times a failed request is retried."""
        return self._http.retries

    async def run(
        self,
        source: StrPath | Asset,
        models: str | Target | Iterable[str | Target],
        *,
        output_dir: StrPath | None = None,
        format: str | Iterable[str] | None = None,
        residual: bool = False,
        language: str | None = None,
        num_speakers: int | None = None,
        download: bool = True,
        poll_interval: float = 10.0,
        timeout: float = 3600.0,
        on_progress: ProgressHandler | None = None,
    ) -> RunResult:
        """See :meth:`audioshake.AudioShake.run`."""
        emit = on_progress or (lambda event: None)
        try:
            catalog: dict[str, Model] | None = {
                m.id: m for m in await self.models.list(enabled_only=False)
            }
        except AudioShakeError as e:
            _log.warning(_messages.CATALOG_UNAVAILABLE, e)
            catalog = None
        targets = build_targets(
            models,
            catalog=catalog,
            format=format,
            residual=residual,
            language=language,
            num_speakers=num_speakers,
        )

        asset: Asset | None = None
        if isinstance(source, Asset):
            # Uploaded by an earlier call; there is nothing to send again.
            asset = source
            task_source: str | Asset = source
        elif is_url(source):
            task_source = str(source)
        else:
            path = Path(source)
            size = path.stat().st_size if path.exists() else 0
            emit(ProgressEvent("upload", path.name, 0, size))
            asset = await self.assets.upload(
                path,
                on_progress=lambda sent, total: emit(
                    ProgressEvent("upload", path.name, sent, total)
                ),
            )
            # One last upload event carries the asset, so a caller learns its
            # id even when creating the task fails a moment later.
            emit(ProgressEvent("upload", path.name, size, size, asset=asset))
            task_source = asset

        task = await self.tasks.create(task_source, targets)
        emit(ProgressEvent("queued", task.id, 0, len(targets), task))

        def on_poll(polled: Task) -> None:
            finished = sum(1 for t in polled.targets if t.done)
            emit(ProgressEvent("processing", polled.id, finished, len(polled.targets), polled))

        try:
            task = await self.tasks.wait(
                task.id, poll_interval=poll_interval, timeout=timeout, on_poll=on_poll
            )
        except TaskFailedError as e:
            task = _completed_part(e)

        files: builtins.list[Path] = []
        if download:
            # Name each transfer by the file it becomes, not by the output name:
            # two targets can both produce "residual", and a progress display
            # keyed on the bare name folds their two transfers into one row.
            named = {output.link: stem for output, _target, stem in planned_outputs(task)}
            files = await self.tasks.download(
                task,
                output_dir if output_dir is not None else Path.cwd(),
                on_progress=lambda output, written, total: emit(
                    ProgressEvent(
                        "download", named.get(output.link, output.name), written, total, task
                    )
                ),
            )
        emit(ProgressEvent("done", task.id, len(files), len(files), task))
        return RunResult(task=task, files=tuple(files), asset=asset)

    async def close(self) -> None:
        """Close the client's connections. ``async with`` does this for you."""
        await self._http.close()
        await self._transfer.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class AsyncAssets:
    """Async twin of :class:`audioshake.Assets`: uploaded source files."""

    def __init__(self, http: AsyncHttpSession) -> None:
        self._http = http

    async def upload(
        self,
        path: StrPath,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Asset:
        """See :meth:`audioshake.Assets.upload`."""
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(_messages.file_not_found(file_path))

        content_type = _CONTENT_TYPES.get(file_path.suffix.lower()) or (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        total = file_path.stat().st_size

        # httpx reads the file as it sends it; opening it is a local, bounded call.
        with file_path.open("rb") as fh:  # noqa: ASYNC230
            body = _ProgressReader(fh, total, on_progress)
            data = await self._http.post_json(
                "/assets",
                action=_messages.ACTION_UPLOAD_ASSET,
                files={"file": (file_path.name, body, content_type)},
                timeout=TRANSFER_TIMEOUT,
                before_attempt=body.reset,
            )
        asset = Asset.from_api(
            expect_object(data, _messages.ACTION_UPLOAD_ASSET, _messages.SHAPE_ASSET)
        )
        _log.info("uploaded %s as asset %s", file_path.name, asset.id)
        return asset

    async def get(self, asset_id: str) -> Asset:
        """See :meth:`audioshake.Assets.get`."""
        data = await self._http.get_json(f"/assets/{asset_id}", action=_messages.ACTION_GET_ASSET)
        return Asset.from_api(
            expect_object(data, _messages.ACTION_GET_ASSET, _messages.SHAPE_ASSET)
        )


class AsyncTasks:
    """Async twin of :class:`audioshake.Tasks`: create, wait for and download tasks."""

    def __init__(
        self, http: AsyncHttpSession, transfer: httpx.AsyncClient, pool: AsyncTransferPool
    ) -> None:
        self._http = http
        self._transfer = transfer
        self._pool = pool

    async def create(
        self,
        source: str | Asset,
        targets: Iterable[Target],
    ) -> Task:
        """See :meth:`audioshake.Tasks.create`."""
        # Read twice: once for the payload, once for the log line.
        targets = list(targets)
        payload = _task_payload(source, targets)
        # A task is billed once created. If the response is lost after the
        # server acted, a blind retry would create and charge a second task, so
        # only statuses where the server declined to act are retried here.
        try:
            data = await self._http.post_json(
                "/tasks", action=_messages.ACTION_CREATE_TASK, json=payload, idempotent=False
            )
        except (ServerError, APIConnectionError) as e:
            _warn_if_task_may_exist(e)
            raise
        task = Task.from_api(
            expect_object(data, _messages.ACTION_CREATE_TASK, _messages.SHAPE_TASK)
        )
        _log.info("created task %s for %s", task.id, ", ".join(t.model for t in targets))
        return task

    async def get(self, task_id: str | Task) -> Task:
        """Async twin of :meth:`audioshake.Tasks.get`."""
        task_id = _task_id(task_id)
        data = await self._http.get_json(f"/tasks/{task_id}", action=_messages.ACTION_GET_TASK)
        return Task.from_api(expect_object(data, _messages.ACTION_GET_TASK, _messages.SHAPE_TASK))

    async def list(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> builtins.list[Task]:
        """Async twin of :meth:`audioshake.client.Tasks.list`."""
        since = _task_list_bounds(limit, since)
        found: builtins.list[Task] = []
        scanned = 0
        while True:
            take = _page_size(limit, len(found))
            data = await self._http.get_json(
                "/tasks",
                action=_messages.ACTION_LIST_TASKS,
                params={"skip": scanned, "take": take},
            )
            page = _task_page(data)
            scanned += len(page)
            if not _collect_page(found, page, take=take, limit=limit, since=since):
                return found

    async def wait(
        self,
        task_id: str | Task,
        *,
        poll_interval: float = 10.0,
        timeout: float = 3600.0,
        on_poll: Callable[[Task], object] | None = None,
    ) -> Task:
        """See :meth:`audioshake.Tasks.wait`."""
        task_id = _task_id(task_id)
        deadline = time.monotonic() + timeout
        while True:
            task = await self.get(task_id)
            if on_poll is not None:
                on_poll(task)
            if task.done:
                if task.failed:
                    raise TaskFailedError(task)
                if task.cost > 0:
                    _log.info("task %s completed (%.2f credits)", task.id, task.cost)
                else:
                    _log.info("task %s completed", task.id)
                return task

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TaskTimeoutError(task_id, timeout)
            await _retry.async_sleep(min(poll_interval, remaining))

    async def download(
        self,
        task: Task,
        output_dir: StrPath,
        *,
        name_prefix: str = "",
        on_progress: Callable[[Output, int, int], object] | None = None,
    ) -> builtins.list[Path]:
        """See :meth:`audioshake.Tasks.download`."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        jobs = [
            TransferJob(
                output.link,
                out / f"{name_prefix}{stem}.{safe_name(output.format, 'bin')}",
                _bind_output(on_progress, output),
            )
            for output, _target, stem in planned_outputs(task)
        ]
        return await self._pool.run(self._transfer, jobs, retries=self._http.retries)


class AsyncModels:
    """Async twin of :class:`audioshake.Models`: the model catalog."""

    def __init__(self, http: AsyncHttpSession) -> None:
        self._http = http
        self._cache: builtins.list[Model] | None = None

    async def list(self, *, enabled_only: bool = True) -> builtins.list[Model]:
        """See :meth:`audioshake.Models.list`."""
        if self._cache is None:
            data = await self._http.get_json("/models", action=_messages.ACTION_LIST_MODELS)
            items = expect_objects(
                data, "models", _messages.ACTION_LIST_MODELS, _messages.SHAPE_MODEL_LIST
            )
            self._cache = [Model.from_api(item) for item in items]
        if enabled_only:
            return [model for model in self._cache if model.enabled]
        return list(self._cache)

    async def get(self, model_id: str) -> Model:
        """Async twin of :meth:`audioshake.Models.get`."""
        for model in await self.list(enabled_only=False):
            if model.id == model_id:
                return model
        raise ValidationError(_messages.unknown_model(model_id))

    def refresh(self) -> None:
        """Forget the cached catalog, so the next call fetches it again."""
        self._cache = None
