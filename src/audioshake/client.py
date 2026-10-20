"""The synchronous AudioShake client."""

from __future__ import annotations

import builtins
import logging
import mimetypes
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    # typing.Self arrived in 3.11; the stub package has it for 3.10 and the
    # import is never executed at runtime.
    from typing_extensions import Self

from . import _messages, _retry
from ._download import DEFAULT_CONCURRENCY, ProgressCallback, TransferJob, TransferPool
from ._http import (
    DEFAULT_TIMEOUT,
    TRANSFER_TIMEOUT,
    HttpSession,
    build_client,
    build_headers,
    build_transfer_client,
    resolve_base_url,
    resolve_credentials,
)
from ._run import (
    ProgressEvent,
    ProgressHandler,
    build_targets,
    catalog_or_none,
    is_url,
    safe_name,
)
from .exceptions import (
    APIConnectionError,
    ServerError,
    TaskFailedError,
    TaskTimeoutError,
    ValidationError,
    expect_object,
    expect_objects,
)
from .types import Asset, Model, Output, RunResult, StrPath, Target, TargetResult, Task

__all__ = ["Assets", "AudioShake", "Models", "Tasks"]

_log = logging.getLogger("audioshake.tasks")

# mimetypes is inconsistent across platforms for audio; pin the ones the API takes.
_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".mov": "video/quicktime",
}


class AudioShake:
    """Client for the AudioShake API.

    Args:
        api_key: API key. Defaults to the ``AUDIOSHAKE_API_KEY`` environment variable.
        bearer_token: A token from :func:`audioshake.auth.login` (browser sign-in),
            used when no API key is given.
        base_url: API root. Defaults to ``AUDIOSHAKE_BASE_URL`` or the public API.
        timeout: Request timeout in seconds, or an :class:`httpx.Timeout`.
        retries: Retries after the first attempt for transient failures
            (connection errors, timeouts, 408/425/429/5xx).
        download_concurrency: How many outputs to fetch at once. The budget is
            shared by every download this client makes, so callers that already
            run several sources in parallel keep a fixed ceiling on connections.
        http_client: Bring your own :class:`httpx.Client`, for example to add a
            proxy or a custom transport. Auth headers and the base URL are
            applied to it.

    Example::

        with AudioShake() as client:
            asset = client.assets.upload("song.wav")
            task = client.tasks.create(asset, [Target("vocals"), Target("drums")])
            task = client.tasks.wait(task.id)
            client.tasks.download(task, "out/")
    """

    assets: Assets
    tasks: Tasks
    models: Models

    def __init__(
        self,
        api_key: str | None = None,
        *,
        bearer_token: str | None = None,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
        retries: int = _retry.DEFAULT_RETRIES,
        download_concurrency: int = DEFAULT_CONCURRENCY,
        http_client: httpx.Client | None = None,
    ) -> None:
        credentials = resolve_credentials(api_key, bearer_token)
        url = resolve_base_url(base_url)

        if http_client is None:
            http_client = build_client(credentials, url, timeout)
        else:
            http_client.headers.update(build_headers(credentials))
            if not str(http_client.base_url):
                http_client.base_url = httpx.URL(url)

        self._http = HttpSession(http_client, retries=retries)
        self._transfer = build_transfer_client()
        self._transfers = TransferPool(download_concurrency)
        self.assets = Assets(self._http)
        self.tasks = Tasks(self._http, self._transfer, self._transfers)
        self.models = Models(self._http)

    @property
    def base_url(self) -> str:
        """The API host this client talks to."""
        return str(self._http.client.base_url).rstrip("/")

    @property
    def retries(self) -> int:
        """How many times a failed request is retried."""
        return self._http.retries

    def run(
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
        """Process one source end to end: upload, create a task, wait, download.

        Args:
            source: A local file, a public ``http(s)`` URL, or an :class:`Asset`
                from an earlier upload (``assets.upload()``, or a previous
                result's ``asset``), which is used as it is.
            models: Model ids, comma lists, or :class:`Target`
                objects for full control.
            output_dir: Where to write results, as ``{output}.{format}`` (for
                example ``vocals.wav``). Defaults to the current directory; give
                each source its own directory when processing several.
            format: Output formats: one, a comma list, or a sequence. Each model
                produces the requested formats it offers, so ``"srt,mp3"`` gives
                ``alignment`` its SRT and the stems their MP3, and ``"json,srt"``
                gives ``alignment`` both files from one billed target. A model
                offering none of them keeps its default; a format no selected
                model offers is an error. ``None`` means each model's default
                (WAV for audio, JSON for text).
            residual: Also return everything the models did not capture.
            language: For models that accept one (transcription, alignment).
            num_speakers: For models that accept one (multi_voice).
            download: Set False to leave results in the API's storage.
            poll_interval: Seconds between status checks.
            timeout: Give up waiting after this many seconds.
            on_progress: Receives a :class:`ProgressEvent` at each step.

        Returns:
            The finished task and the files written (empty when ``download`` is False).
            When some targets failed and others completed, the completed ones
            are still downloaded and the result is returned; see
            :attr:`RunResult.failed`.

        Raises:
            TaskFailedError: Every target failed, so there is nothing to keep.
            TaskTimeoutError: The task did not finish in time.
            ValidationError: The model list is empty or repeats a model.
        """
        emit = on_progress or (lambda event: None)
        catalog = catalog_or_none(lambda: self.models.list(enabled_only=False))
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
            asset = self.assets.upload(
                path,
                on_progress=lambda sent, total: emit(
                    ProgressEvent("upload", path.name, sent, total)
                ),
            )
            # One last upload event carries the asset, so a caller learns its
            # id even when creating the task fails a moment later.
            emit(ProgressEvent("upload", path.name, size, size, asset=asset))
            task_source = asset

        task = self.tasks.create(task_source, targets)
        emit(ProgressEvent("queued", task.id, 0, len(targets), task))

        def on_poll(polled: Task) -> None:
            finished = sum(1 for t in polled.targets if t.done)
            emit(ProgressEvent("processing", polled.id, finished, len(polled.targets), polled))

        try:
            task = self.tasks.wait(
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
            files = self.tasks.download(
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

    def close(self) -> None:
        """Release HTTP connections. Also happens when used as a context manager."""
        self._transfers.close()
        self._http.close()
        self._transfer.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Assets:
    """Upload source files."""

    def __init__(self, http: HttpSession) -> None:
        self._http = http

    def upload(
        self,
        path: StrPath,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Asset:
        """Upload a local audio or video file and return the created asset.

        Args:
            path: File to upload.
            on_progress: Called with ``(bytes_sent, total_bytes)`` as the upload proceeds.

        Raises:
            FileNotFoundError: ``path`` does not exist.
            APIError: The API rejected the upload.
        """
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(_messages.file_not_found(file_path))

        content_type = _CONTENT_TYPES.get(file_path.suffix.lower()) or (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        total = file_path.stat().st_size

        with file_path.open("rb") as fh:
            body = _ProgressReader(fh, total, on_progress)

            def rewind() -> None:
                body.reset()

            data = self._http.post_json(
                "/assets",
                action=_messages.ACTION_UPLOAD_ASSET,
                files={"file": (file_path.name, body, content_type)},
                timeout=TRANSFER_TIMEOUT,
                before_attempt=rewind,
            )
        asset = Asset.from_api(
            expect_object(data, _messages.ACTION_UPLOAD_ASSET, _messages.SHAPE_ASSET)
        )
        _log.info("uploaded %s as asset %s", file_path.name, asset.id)
        return asset

    def get(self, asset_id: str) -> Asset:
        """Fetch an asset's metadata (name, format) by id."""
        data = self._http.get_json(f"/assets/{asset_id}", action=_messages.ACTION_GET_ASSET)
        return Asset.from_api(
            expect_object(data, _messages.ACTION_GET_ASSET, _messages.SHAPE_ASSET)
        )


class Tasks:
    """Create, inspect, wait for and download tasks."""

    def __init__(self, http: HttpSession, transfer: httpx.Client, pool: TransferPool) -> None:
        self._http = http
        self._transfer = transfer
        self._pool = pool

    def create(
        self,
        source: str | Asset,
        targets: Iterable[Target],
    ) -> Task:
        """Create a task.

        Args:
            source: An :class:`Asset`, an asset id, or a public ``http(s)`` URL.
            targets: Models to run. At least one.
        """
        # Read twice: once for the payload, once for the log line.
        targets = list(targets)
        payload = _task_payload(source, targets)
        # A task is billed once created. If the response is lost after the
        # server acted, a blind retry would create and charge a second task, so
        # only statuses where the server declined to act are retried here.
        try:
            data = self._http.post_json(
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

    def get(self, task_id: str | Task) -> Task:
        """Fetch the current state of a task."""
        task_id = _task_id(task_id)
        data = self._http.get_json(f"/tasks/{task_id}", action=_messages.ACTION_GET_TASK)
        return Task.from_api(expect_object(data, _messages.ACTION_GET_TASK, _messages.SHAPE_TASK))

    def list(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> builtins.list[Task]:
        """List this account's tasks, newest first.

        Pages through ``GET /tasks`` until ``limit`` tasks are found, a task
        older than ``since`` appears, or the list runs out.

        Args:
            limit: Return at most this many tasks. None returns every task.
            since: Only tasks created at or after this time. A naive datetime is UTC.

        Raises:
            ValidationError: ``limit`` is less than 1.
        """
        since = _task_list_bounds(limit, since)
        found: builtins.list[Task] = []
        while True:
            take = _page_size(limit, len(found))
            data = self._http.get_json(
                "/tasks",
                action=_messages.ACTION_LIST_TASKS,
                params={"skip": len(found), "take": take},
            )
            if not _collect_page(found, _task_page(data), take=take, limit=limit, since=since):
                return found

    def wait(
        self,
        task_id: str | Task,
        *,
        poll_interval: float = 10.0,
        timeout: float = 3600.0,
        on_poll: Callable[[Task], object] | None = None,
    ) -> Task:
        """Poll until every target has finished.

        Args:
            task_id: Task to wait for.
            poll_interval: Seconds between status checks.
            timeout: Give up after this many seconds.
            on_poll: Called with each polled :class:`Task`, for progress display.

        Returns:
            The finished task, with every target completed.

        Raises:
            TaskFailedError: One or more targets failed. The task is on the error.
            TaskTimeoutError: ``timeout`` elapsed first.
        """
        task_id = _task_id(task_id)
        deadline = time.monotonic() + timeout
        while True:
            task = self.get(task_id)
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
            _retry.sleep(min(poll_interval, remaining))

    def download(
        self,
        task: Task,
        output_dir: StrPath,
        *,
        name_prefix: str = "",
        on_progress: Callable[[Output, int, int], object] | None = None,
    ) -> builtins.list[Path]:
        """Download every output of the task's completed targets.

        Outputs are fetched concurrently, up to the client's
        ``download_concurrency``, which matters for models such as
        ``multi_voice`` that produce one output per speaker. The paths come back
        in API order however the transfers interleave.

        Files are named ``{name_prefix}{output}.{format}``, e.g. ``vocals.wav``.
        When two targets produce an output with the same name (every stem model
        also produces ``residual``), the model id is added:
        ``{name_prefix}{model}_{output}.{format}``.

        Args:
            task: A finished task, as returned by :meth:`wait`.
            output_dir: Directory to write into; created if needed.
            name_prefix: Prepended to every file name, typically the source's stem.
            on_progress: Called with ``(output, bytes_written, total_bytes)``.

        Returns:
            The paths written, in API order.
        """
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
        return self._pool.run(self._transfer, jobs, retries=self._http.retries)


def _completed_part(error: TaskFailedError) -> Task:
    """The task behind ``error``, when some of it completed and is worth keeping.

    A failed target's credit hold is released, but completed targets are
    charged, so their outputs must be downloaded rather than lost with the
    error. Shared by the sync and async ``run()``. Re-raises when nothing
    completed.
    """
    task = error.task
    completed = [target for target in task.targets if target.status == "completed"]
    if not completed:
        raise error
    failed = ", ".join(target.model for target in task.failed)
    _log.warning(_messages.PARTIAL_TASK, task.id, failed, len(completed))
    return task


def _warn_if_task_may_exist(error: ServerError | APIConnectionError) -> None:
    """Warn that a failed ``POST /tasks`` may still have created a billed task.

    A declined status (503 after its retries) means the server did nothing, so
    it stays quiet; a lost response or a 500/502/504 does not.
    """
    if isinstance(error, ServerError) and error.status in _retry.DECLINED_STATUSES:
        return
    _log.warning(_messages.TASK_MAY_EXIST)


def _task_id(task_id: str | Task) -> str:
    """The id behind either form ``get`` and ``wait`` accept, as ``create`` does with an Asset."""
    return task_id.id if isinstance(task_id, Task) else task_id


def _task_payload(
    source: str | Asset,
    targets: Sequence[Target],
) -> dict[str, Any]:
    """Build the ``POST /tasks`` body shared by the sync and async clients."""
    if not targets:
        raise ValidationError(_messages.NO_TARGETS)

    payload: dict[str, Any] = {"targets": [target.to_api() for target in targets]}
    source_id = source.id if isinstance(source, Asset) else source
    if source_id.startswith(("http://", "https://")):
        payload["url"] = source_id
    else:
        payload["assetId"] = source_id
    return payload


def planned_outputs(task: Task) -> builtins.list[tuple[Output, TargetResult, str]]:
    """Decide the file stem for every downloadable output of a task.

    Outputs of targets that did not complete are skipped. When two targets share
    an output name and format (every stem model also produces ``residual``) the
    model id is prefixed so the files do not overwrite each other. One name in
    two formats (``alignment.json`` and ``alignment.srt``) already differs by
    extension, so it keeps the plain name.

    Names are compared as they will be written: after sanitising, and ignoring
    case. Should two outputs still end up with one file name, the later one gets
    ``-2``, ``-3``, ... rather than overwriting the first.
    """

    def file_key(stem: str, output: Output) -> tuple[str, str]:
        return stem.casefold(), safe_name(output.format, "bin").casefold()

    # Names and formats come from the API response; never let them carry a
    # path out of the output directory.
    name_counts: dict[tuple[str, str], int] = {}
    for target in task.targets:
        for output in target.outputs:
            key = file_key(safe_name(output.name, "output"), output)
            name_counts[key] = name_counts.get(key, 0) + 1

    planned: builtins.list[tuple[Output, TargetResult, str]] = []
    taken: set[tuple[str, str]] = set()
    for target in task.targets:
        if target.status != "completed":
            continue
        for output in target.outputs:
            base = safe_name(output.name, "output")
            if name_counts[file_key(base, output)] > 1:
                base = f"{safe_name(target.model, 'model')}_{base}"
            stem, count = base, 1
            while file_key(stem, output) in taken:
                count += 1
                stem = f"{base}-{count}"
            taken.add(file_key(stem, output))
            planned.append((output, target, stem))
    return planned


TASK_PAGE_SIZE = 100
"""The most tasks ``GET /tasks`` returns in one page."""


def _task_list_bounds(limit: int | None, since: datetime | None) -> datetime | None:
    """Validate ``tasks.list()`` arguments; return ``since`` as an aware datetime."""
    if limit is not None and limit < 1:
        raise ValidationError(_messages.invalid_task_limit(limit))
    if since is not None and since.tzinfo is None:
        return since.replace(tzinfo=timezone.utc)
    return since


def _page_size(limit: int | None, found: int) -> int:
    return TASK_PAGE_SIZE if limit is None else min(TASK_PAGE_SIZE, limit - found)


def _task_page(data: Any) -> builtins.list[Task]:
    items = expect_objects(data, "tasks", _messages.ACTION_LIST_TASKS, _messages.SHAPE_TASK_LIST)
    return [Task.from_api(item) for item in items]


def _collect_page(
    found: builtins.list[Task],
    page: builtins.list[Task],
    *,
    take: int,
    limit: int | None,
    since: datetime | None,
) -> bool:
    """Add a page of tasks to ``found``; return True when the next page is needed.

    The API lists newest first, so the first task older than ``since`` ends the
    listing. A short page means there is nothing after it.
    """
    for task in page:
        created = task.created_at
        if since is not None and created is not None and created < since:
            return False
        found.append(task)
        if limit is not None and len(found) >= limit:
            return False
    return len(page) == take


def _bind_output(
    on_progress: Callable[[Output, int, int], object] | None, output: Output
) -> ProgressCallback | None:
    """Turn a per-output progress callback into the (written, total) form download() wants."""
    if on_progress is None:
        return None

    def progress(written: int, total: int) -> None:
        on_progress(output, written, total)

    return progress


class Models:
    """Discover the models this account can run. Results are cached per client."""

    def __init__(self, http: HttpSession) -> None:
        self._http = http
        self._cache: builtins.list[Model] | None = None

    def list(self, *, enabled_only: bool = True) -> builtins.list[Model]:
        """List models, from ``GET /models``.

        Args:
            enabled_only: Hide models the account must request access to.
        """
        if self._cache is None:
            data = self._http.get_json("/models", action=_messages.ACTION_LIST_MODELS)
            items = expect_objects(
                data, "models", _messages.ACTION_LIST_MODELS, _messages.SHAPE_MODEL_LIST
            )
            self._cache = [Model.from_api(item) for item in items]
        if enabled_only:
            return [model for model in self._cache if model.enabled]
        return list(self._cache)

    def get(self, model_id: str) -> Model:
        """Look up one model by id.

        Raises:
            ValidationError: No model with that id is visible to this account.
        """
        for model in self.list(enabled_only=False):
            if model.id == model_id:
                return model
        raise ValidationError(_messages.unknown_model(model_id))

    def refresh(self) -> None:
        """Drop the cache so the next call fetches again."""
        self._cache = None


class _ProgressReader:
    """File wrapper that reports bytes read, for upload progress.

    Exposes ``read``/``seek``/``tell`` (so httpx can size the body) but not
    ``fileno``, which keeps httpx on the code path that goes through ``read``.
    """

    def __init__(self, fh: IO[bytes], total: int, on_progress: ProgressCallback | None) -> None:
        self._fh = fh
        self._total = total
        self._on_progress = on_progress
        self._sent = 0

    def reset(self) -> None:
        self._fh.seek(0)
        self._sent = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk and self._on_progress is not None:
            self._sent += len(chunk)
            self._on_progress(self._sent, self._total)
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._fh.seek(offset, whence)

    def tell(self) -> int:
        return self._fh.tell()
