"""Batch driver: run every source through the library and paint the progress tree."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import wait as wait_for_futures
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import click
from rich.console import Console
from rich.markup import escape

from audioshake import (
    SPEAKER_COUNT_MODELS,
    APIConnectionError,
    APIError,
    Asset,
    AudioShake,
    AudioShakeError,
    AuthenticationError,
    AuthError,
    Model,
    NotFoundError,
    ProgressEvent,
    RunResult,
    ServerError,
    Target,
    Task,
    TaskFailedError,
    UnexpectedResponseError,
    __version__,
    expand_models,
)
from audioshake._retry import DEFAULT_RETRIES
from audioshake._run import (
    asset_id_from_ref,
    build_targets,
    catalog_or_none,
    is_asset_ref,
    requested_formats,
    source_stem,
)
from audioshake.client import planned_outputs
from audioshake.exceptions import describe_task_failure

from . import _messages
from ._format import (
    credits_or_none,
    format_credits,
    format_duration,
    format_error,
    format_size,
    format_transfer,
)
from ._logging import logger
from ._output import emit_json, stdout
from ._settings import (
    EXIT_AUTH,
    EXIT_FAILED_ITEMS,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_UNEXPECTED,
    Settings,
)
from .progress import Node, ProgressTracker, Status

_log = logging.getLogger("audioshake.cli")

# A summary entry has a whole line to itself, so it can show more of the API's
# text than a tree row, which is squeezed into a column.
SUMMARY_ERROR_CHARS = 600

# Extensions picked up when a directory is given. The API's own list is the
# source of truth; this only decides which files to try.
INPUT_EXTENSIONS = ("wav", "mp3", "mp4", "m4a", "aac", "flac", "aiff", "aif", "pcm", "mov")


@dataclass
class RunOptions:
    """Everything a run needs besides the sources."""

    models: list[str]
    output_dir: Path
    format: str | None = None
    residual: bool = False
    language: str | None = None
    num_speakers: int | None = None
    download: bool = True
    max_concurrent: int = 10
    timeout: float = 3600.0
    json: bool = False


@dataclass(frozen=True)
class SourceFailure:
    """One source that did not finish, with everything still known about it."""

    source: str
    summary: str
    exit_code: int
    # Set once the task exists. Whatever completes on it is charged, so a
    # failure after that must still report the id to chase and the credits.
    task: Task | None = None
    # Set once the source was uploaded: the upload outlives the failure.
    asset_id: str | None = None
    # The reason with more room than a tree row has: for the summary, which is
    # not squeezed into a column. Still one line, still without upstream frames.
    reason: str = ""


@dataclass
class BatchResult:
    succeeded: int = 0
    failed: list[SourceFailure] = field(default_factory=list)
    # Sources whose completed models were downloaded while others failed. Not a
    # success: something the user asked for is missing, so the batch exits 1.
    partial: list[SourceFailure] = field(default_factory=list)
    total_cost: float = 0.0
    # Outputs written to disk, across every source.
    files_written: int = 0

    @property
    def total(self) -> int:
        return self.succeeded + len(self.failed) + len(self.partial)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.partial

    @property
    def exit_code(self) -> int:
        """The code for the batch as a whole.

        A batch can fail several ways at once. The order runs from the causes
        the user has to act on before anything will work, through the ones they
        can only report, to a plain failed item: credentials (3), then the
        network (4), then a bug in the CLI (5), then failed sources (1).
        """
        if self.ok:
            return EXIT_OK
        precedence = (EXIT_AUTH, EXIT_NETWORK, EXIT_UNEXPECTED, EXIT_FAILED_ITEMS)
        codes = {failure.exit_code for failure in (*self.failed, *self.partial)}
        return next(code for code in precedence if code in codes)


class _DownloadTally:
    """Folds one source's concurrent output transfers into a single progress line.

    Outputs are fetched in parallel, so progress callbacks arrive interleaved and
    from several threads. Summing them keeps the row stable instead of flicking
    between file names with byte counts that appear to jump backwards.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes: dict[str, tuple[int, int]] = {}
        self._expected = 0

    def expect(self, task: Task) -> None:
        """Record how many outputs the task will produce, for the ``done/total`` count."""
        with self._lock:
            if not self._expected:
                self._expected = len(planned_outputs(task))

    def record(self, name: str, written: int, total: int) -> str:
        with self._lock:
            self._bytes[name] = (written, total)
            finished = sum(1 for done, size in self._bytes.values() if size and done >= size)
            written_all = sum(done for done, _ in self._bytes.values())
            expected = max(self._expected, len(self._bytes))
            lone = expected == 1
            total_all = sum(size for _, size in self._bytes.values())
        if lone:
            # A single output keeps the name-and-percentage line it always had.
            return f"downloading {name} {format_transfer(written_all, total_all)}"
        # Outputs that have not started yet are missing from the byte totals, so a
        # percentage here would fall as they arrive. The count and the bytes moved
        # both only ever climb.
        return f"downloading {finished}/{expected} · {format_size(written_all)}"


class BatchRunner:
    """Runs sources concurrently through :meth:`AudioShake.run`, reporting to a tree."""

    result: BatchResult

    def __init__(
        self,
        client: AudioShake,
        options: RunOptions,
        tracker: ProgressTracker,
        assets: Mapping[str, Asset] | None = None,
    ) -> None:
        self.client = client
        self.options = options
        self.tracker = tracker
        # ``asset:<id>`` sources, resolved before the batch starts, so each is
        # labelled and filed by the name of the file it was uploaded from.
        self.assets: Mapping[str, Asset] = assets or {}
        self.result = BatchResult()
        self._lock = threading.Lock()
        # Per node, the newest task seen and the source it came from. A tree row
        # is labelled with a bare file name, which two sources can share.
        self._tasks: dict[int, Task] = {}
        self._assets: dict[int, Asset] = {}
        self._sources: dict[int, str] = {}
        # Set when the batch must stop starting work. A worker that has not
        # begun yet checks this before it uploads anything, which closes the
        # gap between cancelling a future and the pool picking it up; one that
        # is still uploading checks it on every progress report, so it stops
        # before creating a task.
        self._stop = threading.Event()
        # Task ids of sources in flight, by node, so an interrupt can say which
        # tasks it is still waiting on.
        self._running_tasks: dict[int, str] = {}
        self._running_lock = threading.Lock()
        # Where each source's outputs go, in source order, for the summary.
        self._dirs: list[Path] = []

    def run(self, sources: Sequence[str | Path]) -> BatchResult:
        """Process every source, up to ``max_concurrent`` at a time.

        An interrupt stops the batch from starting anything else. A source that
        is already running is left to finish: its task cannot be cancelled and
        what completes is charged, so abandoning it would pay for outputs nobody
        collects.
        """
        self.tracker.set_total(len(sources))
        nodes = [self.tracker.add_item(label) for label in self._display_names(sources)]
        self._sources = {id(node): str(source) for node, source in zip(nodes, sources, strict=True)}
        # An asset source is filed under the file it was uploaded from, and it
        # is known before anything runs, so a failure can name it straight away.
        for node, source in zip(nodes, sources, strict=True):
            asset = self._asset_ref(source)
            if asset is not None:
                self._assets[id(node)] = asset
        dirs = output_dirs(self.options.output_dir, [self._naming_source(s) for s in sources])
        self._dirs = dirs

        executor = ThreadPoolExecutor(max_workers=self.options.max_concurrent)
        try:
            futures = {
                executor.submit(self._run_one, source, nodes[i], dirs[i]): i
                for i, source in enumerate(sources)
            }
            recorded: set[Future[RunResult]] = set()
            try:
                for future in as_completed(futures):
                    self._record(future, futures, sources, nodes, recorded)
            except BaseException:
                # Ctrl-C must not let the pool drain on the way out: every
                # source still queued would upload and create a task the user
                # just asked us not to start, and be billed for it.
                self._cancel_pending(futures)
                self._wait_for_running(futures)
                # The sources that were running are now settled, and each of
                # them was billed. They belong in --json, the summary and the
                # cost total just like the sources that finished before the
                # interrupt, so a script sees every task the batch reached.
                for future in futures:
                    if future.done() and not future.cancelled() and future not in recorded:
                        self._record(future, futures, sources, nodes, recorded)
                raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return self.result

    def _record(
        self,
        future: Future[RunResult],
        futures: Mapping[Future[RunResult], int],
        sources: Sequence[str | Path],
        nodes: Sequence[Node],
        recorded: set[Future[RunResult]],
    ) -> None:
        """Fold one settled source into the batch result, once."""
        recorded.add(future)
        index = futures[future]
        source = sources[index]
        node = nodes[index]
        try:
            run_result = future.result()
        except Exception as e:  # noqa: BLE001 - one bad item must not sink the batch
            self._record_failure(node, source, e)
            return
        self._record_success(node, run_result)

    def _cancel_pending(self, futures: Mapping[Future[RunResult], int]) -> None:
        """Stop the batch: drop what is queued, and say how much never started."""
        self._stop.set()
        skipped = sum(1 for future in futures if future.cancel())
        if skipped:
            _log.warning(_messages.INTERRUPT_SKIPPED, skipped)
        with self._running_lock:
            running = sorted(self._running_tasks.values())
        if running:
            _log.warning(_messages.INTERRUPT_FINISHING, len(running), ", ".join(running))

    def _wait_for_running(self, futures: Mapping[Future[RunResult], int]) -> None:
        """Let sources already in flight finish, unless the user interrupts again.

        Waiting here rather than in the interpreter's exit keeps a second Ctrl-C
        in our hands: left to thread shutdown it prints a Python traceback. The
        second one exits at once, since the workers cannot be stopped mid-call.
        """
        running = [future for future in futures if not future.done()]
        if not running:
            return
        try:
            wait_for_futures(running)
        except KeyboardInterrupt:
            _log.warning(_messages.INTERRUPT_ABANDONED, len(running))
            # os._exit skips every cleanup, so stop the live view first or the
            # terminal is left with its cursor hidden.
            self.tracker.__exit__(None, None, None)
            logging.shutdown()
            os._exit(130)  # the CLI's interrupted exit code, without joining workers

    # -- one source ---------------------------------------------------------

    def _asset_ref(self, source: str | Path) -> Asset | None:
        return self.assets.get(source) if isinstance(source, str) else None

    def _display_names(self, sources: Sequence[str | Path]) -> list[str]:
        """Row labels: the file name, plus its folder when another source shares the name."""
        names = [self._display_name(source) for source in sources]
        shared = Counter(name.casefold() for name in names)
        labels = []
        for name, source in zip(names, sources, strict=True):
            folder = Path(source).parent.name if isinstance(source, Path) else ""
            labels.append(f"{folder}/{name}" if shared[name.casefold()] > 1 and folder else name)
        return labels

    def _display_name(self, source: str | Path) -> str:
        asset = self._asset_ref(source)
        return asset.name if asset is not None and asset.name else _label(source)

    def _naming_source(self, source: str | Path) -> str | Path:
        """What the output folder is named after: the uploaded file for an asset."""
        asset = self._asset_ref(source)
        return asset.name if asset is not None and asset.name else source

    def _run_one(self, source: str | Path, node: Node, out_dir: Path) -> RunResult:
        if self._stop.is_set():
            # Cancelled between submit and pick-up. Nothing here is billable yet,
            # so leave without touching the API.
            raise AudioShakeError(_messages.NOT_STARTED)
        node.start("starting")
        tally = _DownloadTally()
        asset = self._asset_ref(source)
        try:
            return self.client.run(
                asset if asset is not None else source,
                self.options.models,
                output_dir=out_dir,
                format=self.options.format,
                residual=self.options.residual,
                language=self.options.language,
                num_speakers=self.options.num_speakers,
                download=self.options.download,
                timeout=self.options.timeout,
                on_progress=lambda event: self._on_event(node, event, tally),
            )
        except AudioShakeError:
            raise
        except OSError as e:  # unreadable source, full disk, and friends
            raise AudioShakeError(str(e)) from e
        finally:
            with self._running_lock:
                self._running_tasks.pop(id(node), None)

    def _on_event(self, node: Node, event: ProgressEvent, tally: _DownloadTally) -> None:
        if event.task is not None:
            # Keep the latest view of the task: whatever goes wrong from here,
            # the failure can still name the task and the credits it cost.
            with self._lock:
                self._tasks[id(node)] = event.task
        if event.asset is not None:
            # Likewise the upload: a failure after it can name the asset, which
            # outlives the run, instead of leaving the user to upload again.
            with self._lock:
                self._assets[id(node)] = event.asset
        if event.stage == "upload":
            if self._stop.is_set():
                # Raised through the upload, so run() never reaches the create.
                raise AudioShakeError(_messages.STOPPED_BEFORE_TASK)
            node.update(f"uploading {format_transfer(event.done, event.total)}")
        elif event.stage == "queued" and event.task is not None:
            with self._running_lock:
                self._running_tasks[id(node)] = event.task.id
            self._ensure_children(node, event.task)
            node.update(f"task {event.task.id} · queued")
        elif event.stage == "processing" and event.task is not None:
            self._mirror_targets(node, event.task)
        elif event.stage == "download":
            if event.task is not None:
                tally.expect(event.task)
            detail = tally.record(event.detail, event.done, event.total)
            node.update(f"task {event.task.id} · {detail}" if event.task else detail)
        elif event.stage == "done" and event.task is not None:
            self._mirror_targets(node, event.task)

    def _ensure_children(self, node: Node, task: Task) -> None:
        if len(task.targets) > 1 and not node.children:
            for target in task.targets:
                node.child(target.model).update("queued")

    def _mirror_targets(self, node: Node, task: Task) -> None:
        children = {child.label: child for child in node.children}
        running = 0
        for target in task.targets:
            child = children.get(target.model)
            if target.status == "failed":
                if child is not None:
                    # Tree rows are narrow; -vv has the API's full error text.
                    child.fail(
                        format_error(target.error, 80) if target.error else _messages.TARGET_FAILED
                    )
            elif target.status == "completed":
                # Same order as a settled file row: what it cost, then how long.
                credits = credits_or_none(target.cost)
                parts = [credits] if credits is not None else []
                if target.duration:
                    parts.append(format_duration(target.duration))
                if child is not None:
                    child.succeed(" · ".join(parts))
            else:
                running += 1
                if child is not None:
                    child.update(target.status)
        if children:
            node.update(f"task {task.id} · {running} running")
        elif task.targets:
            # One model, so there is no child row carrying the id: keep it here.
            node.update(f"task {task.id} · {task.targets[0].status}")

    # -- bookkeeping --------------------------------------------------------

    def _record_success(self, node: Node, run_result: RunResult) -> None:
        task = run_result.task
        if run_result.failed:
            self._record_partial(node, run_result)
            return
        cost = task.cost
        credits = credits_or_none(cost)
        self.result.succeeded += 1
        self.result.total_cost += cost
        self.result.files_written += len(run_result.files)
        self.tracker.add_credits(cost)
        # The row carried "task <short id>" while it was running; keep it once
        # the row settles, so a finished source can still be traced back. The
        # detail column is too narrow for the full id beside cost and elapsed,
        # which the one-line form, the log and --json all carry instead.
        parts = [f"task {task.id}"]
        if node.children:
            done = sum(1 for child in node.children if child.status is Status.DONE)
            parts.append(f"{done}/{len(node.children)} done")
        elif credits is not None:
            parts.append(credits)
        parts.append(format_duration(node.elapsed))
        node.succeed(" · ".join(parts))
        if credits is not None:
            _log.info("completed %s as task %s (%s)", node.label, task.id, credits)
        else:
            _log.info("completed %s as task %s", node.label, task.id)
        self._emit_result(
            node, task, list(run_result.files), None, asset=self._asset_for(node, run_result)
        )

    def _record_partial(self, node: Node, run_result: RunResult) -> None:
        """Some models completed and were downloaded; the rest failed.

        The failed targets' holds are released, the completed ones are charged,
        so the cost is the task's own and the files are the ones written.
        """
        task = run_result.task
        reason = describe_task_failure(task, full=True)
        summary = format_error(describe_task_failure(task))
        source = self._sources[id(node)]
        asset = self._asset_for(node, run_result)
        self.result.partial.append(
            SourceFailure(
                source,
                summary,
                EXIT_FAILED_ITEMS,
                task,
                asset.id if asset else None,
                reason=format_error(reason, SUMMARY_ERROR_CHARS),
            )
        )
        self.result.total_cost += task.cost
        self.result.files_written += len(run_result.files)
        self.tracker.add_credits(task.cost)
        done = sum(1 for target in task.targets if target.status == "completed")
        parts = [f"task {task.id}", f"{done}/{len(task.targets)} done"]
        credits = credits_or_none(task.cost)
        if credits is not None:
            parts.append(credits)
        node.partial(" · ".join(parts))
        _log.log(self._report_level(logging.WARNING), _messages.SOURCE_PARTIAL, node.label, summary)
        self._emit_result(node, task, list(run_result.files), reason, status="partial", asset=asset)

    def _record_failure(self, node: Node, source: str | Path, error: Exception) -> None:
        # The reason can be an API error carrying a worker's stack trace: one
        # capped line for the display, the full text at -vv and in --json.
        reason = _full_reason(error)
        summary = format_error(str(error))
        task = self._task_for(node, error)
        asset = self._asset_for(node)
        self.result.failed.append(
            SourceFailure(
                str(source),
                summary,
                _exit_code_for(error),
                task,
                asset.id if asset else None,
                reason=format_error(reason, SUMMARY_ERROR_CHARS),
            )
        )
        if task is not None:
            # Whatever completed on the task is charged; a failed target's hold
            # is released, and its cost is reported as such.
            self.result.total_cost += task.cost
        if node.status is not Status.FAILED:
            node.fail(summary)
        _log.log(self._report_level(logging.ERROR), _messages.SOURCE_FAILED, node.label, summary)
        if summary != reason:
            _log.debug(_messages.SOURCE_FAILED, node.label, reason)
        if not isinstance(error, AudioShakeError):
            # Not a failure the library describes, so it is a bug: -vv gets the
            # traceback, which is the only thing that makes it reportable.
            _log.debug(_messages.SOURCE_FAILED, node.label, reason, exc_info=error)
        self._emit_result(node, task, [], reason, asset=asset)

    def _report_level(self, level: int) -> int:
        """The log level for a source's failure.

        While the live view is up, the row and the summary already carry it;
        a log line as well would say the same thing three times above the
        tree. Outside the live view the log is the only record on stderr.
        """
        return logging.INFO if self.tracker.live_active else level

    def _asset_for(self, node: Node, run_result: RunResult | None = None) -> Asset | None:
        """The asset this source was uploaded as, from the result if it has one, else the last seen."""
        if run_result is not None and run_result.asset is not None:
            return run_result.asset
        with self._lock:
            return self._assets.get(id(node))

    def _task_for(self, node: Node, error: Exception) -> Task | None:
        """The task this source reached, from the error if it carries one, else the last seen."""
        if isinstance(error, TaskFailedError):
            return error.task
        with self._lock:
            return self._tasks.get(id(node))

    def _emit_result(
        self,
        node: Node,
        task: Task | None,
        files: list[Path],
        error: str | None,
        *,
        status: str | None = None,
        asset: Asset | None = None,
    ) -> None:
        """One line per source on stdout: NDJSON under --json, a short row otherwise.

        The machine-readable form carries the source as given, so two sources
        that share a file name stay apart in a log a script has to act on. The
        human row keeps the short label, which is what the tree above it used.
        """
        source = self._sources[id(node)]
        if self.options.json:
            emit_json(
                {
                    "source": source,
                    "task_id": task.id if task else None,
                    # The upload outlives the run: with the id, more models can
                    # be run on the same file without uploading it again.
                    "asset_id": asset.id if asset else None,
                    "status": status or ("completed" if error is None else "failed"),
                    # None, not 0.0: before the task exists, or while it is
                    # still running, the charge is unknown, and reporting it as
                    # zero understates it.
                    "cost": _final_cost(task),
                    "files": [str(f) for f in files],
                    "error": error,
                }
            )
        elif not self.tracker.live_active:
            # File names and API text are external: escape them, or a name like
            # "song [remix].wav" loses its brackets and "[/b]" fails the render.
            label = escape(node.label)
            if task is not None and (error is None or status == "partial"):
                credits = credits_or_none(task.cost)
                charge = f"  {credits}" if credits is not None else ""
                written = _count(len(files), "file")
                if error is None:
                    stdout.print(f"[green]✔[/green] {label}  {task.id}{charge}  {written}")
                else:
                    failed = escape(format_error(error))
                    stdout.print(
                        f"[yellow]◐[/yellow] {label}  {task.id}{charge}  {written}  {failed}"
                    )
            else:
                failed = escape(format_error(error or "failed"))
                if asset is not None:
                    failed = f"{failed}  ({_messages.uploaded_as_asset(asset.id)})"
                stdout.print(f"[red]✘[/red] {label}  {failed}")

    def print_summary(self, console: Console | None = None) -> None:
        """Print the final tally. Call after the progress view has stopped."""
        if self.options.json:
            return
        console = console or stdout
        result = self.result
        console.print()
        if result.total > 1:
            console.rule("[bold]Summary", style="dim")
            counts = [_count(result.total, "file")]
            if result.succeeded:
                counts.append(f"[green]{result.succeeded} succeeded[/green]")
            if result.partial:
                counts.append(f"[yellow]{len(result.partial)} partly completed[/yellow]")
            if result.failed:
                counts.append(f"[red]{len(result.failed)} failed[/red]")
            console.print("  ·  ".join(counts))
        if self.options.download and result.files_written:
            # One file goes in its own folder; a batch in the root the user gave.
            where = self._dirs[0] if result.total == 1 and self._dirs else self.options.output_dir
            console.print(
                f"Output: {escape(_shown_path(where))}/  ({_count(result.files_written, 'file')})"
            )
        if result.total > 1:
            tail = []
            if result.total_cost > 0:
                tail.append(f"Total cost: {format_credits(result.total_cost)}")
            tail.append(f"Elapsed: {format_duration(time.monotonic() - self.tracker.start_time)}")
            console.print("  ·  ".join(tail))
        if result.partial:
            # Their files are on disk; this names what is missing.
            console.print("\n[yellow bold]Partly completed:[/yellow bold]")
            self._print_items(console, "[yellow]◐[/yellow]", result.partial)
        if result.failed:
            console.print("\n[red bold]Failed:[/red bold]")
            self._print_items(console, "[red]✘[/red]", result.failed)

    @staticmethod
    def _print_items(console: Console, mark: str, items: Sequence[SourceFailure]) -> None:
        for item in items:
            console.print(f"  {mark} {escape(item.source)}")
            if item.task is not None:
                console.print(f"      task {escape(item.task.id)}")
            if item.reason or item.summary:
                console.print(f"      {escape(item.reason or item.summary)}")
            if item.asset_id:
                console.print(f"      {escape(_messages.uploaded_as_asset(item.asset_id))}")


def _final_cost(task: Task | None) -> float | None:
    """What the task was charged, or None while that is not known yet.

    A task that has not finished, as after a --timeout, bills its targets as
    they complete, so the sum so far is not the charge.
    """
    return task.cost if task is not None and task.done else None


def _full_reason(error: Exception) -> str:
    """The failure's text with the API's own words uncapped, for --json and -vv."""
    if isinstance(error, TaskFailedError):
        return describe_task_failure(error.task, full=True)
    if isinstance(error, APIError):
        return error.full_message
    return str(error)


def _exit_code_for(error: Exception) -> int:
    """The documented exit code a failure would have had on its own.

    Keeping the cause lets an expired key, a dead network and a bug in the CLI
    reach the codes the docs promise instead of all reading as a failed item (1).
    """
    if isinstance(error, AuthenticationError | AuthError):
        return EXIT_AUTH
    if isinstance(error, APIConnectionError | ServerError):
        return EXIT_NETWORK
    if isinstance(error, UnexpectedResponseError):
        return EXIT_UNEXPECTED
    if isinstance(error, AudioShakeError):
        return EXIT_FAILED_ITEMS
    return EXIT_UNEXPECTED


def output_dirs(root: Path, sources: Sequence[str | Path]) -> list[Path]:
    """One output directory per source, named after the source's stem.

    Files inside are named by output only (``vocals.wav``), the layout stem tools
    such as Demucs and Spleeter use.

    Two sources with the same stem (``a/take.wav`` and ``b/take.wav``, or two URLs
    ending in ``sourcefile``) would otherwise write into the same directory and
    overwrite each other silently. Repeats get ``-2``, ``-3``, ... in input order,
    skipping any name already handed out: ``take-2.wav`` must not land in the
    ``take-2`` made for a second ``take.wav``. Names are compared ignoring case,
    since ``Take`` and ``take`` are one directory on macOS and Windows.
    """
    taken: set[str] = set()
    dirs: list[Path] = []
    for source in sources:
        stem = source_stem(source)
        name, count = stem, 1
        while name.casefold() in taken:
            count += 1
            name = f"{stem}-{count}"
        taken.add(name.casefold())
        dirs.append(root / name)
    return dirs


def _label(source: str | Path) -> str:
    """A short display name for a source.

    A URL is reduced to the last segment of its *path*. Splitting the whole URL
    kept the query string, so a presigned source put its signature on screen,
    into scrollback and into every log line naming the source.
    """
    if isinstance(source, Path):
        return source.name
    name = Path(urlsplit(source).path).name
    return name or source


def collect_files(path: Path, *, recursive: bool = False) -> list[Path]:
    """Supported media files at ``path``: the file itself, or a directory's contents."""
    if path.is_file():
        return [path]
    files: set[Path] = set()
    glob = path.rglob if recursive else path.glob
    for ext in INPUT_EXTENSIONS:
        files.update(glob(f"*.{ext}"))
        files.update(glob(f"*.{ext.upper()}"))
    return sorted(files)


def collect_sources(inputs: Sequence[str], *, recursive: bool) -> list[str | Path]:
    """Expand the SOURCE arguments: URLs pass through, directories are listed."""
    sources: list[str | Path] = []
    for value in inputs:
        if value.startswith(("http://", "https://")):
            sources.append(value)
            continue
        if is_asset_ref(value):
            if not asset_id_from_ref(value):
                raise click.BadParameter(_messages.empty_asset_ref(value), param_hint="SOURCE")
            sources.append(value)
            continue
        path = Path(value)
        if not path.exists():
            raise click.BadParameter(_messages.source_does_not_exist(value), param_hint="SOURCE")
        found = collect_files(path, recursive=recursive)
        if not found:
            logger.warning(_messages.NO_SUPPORTED_AUDIO_FILES, path)
        sources.extend(found)
    if not sources:
        raise click.UsageError(_messages.NO_AUDIO_FILES)
    return sources


def resolve_assets(client: AudioShake, sources: Sequence[str | Path]) -> dict[str, Asset]:
    """Look up every ``asset:<id>`` source, so the batch files it by the uploaded file's name.

    Done before anything runs: an id the API no longer has, typically an upload
    that has expired, is a usage error like a missing file, not a failed item.
    """
    resolved: dict[str, Asset] = {}
    for source in sources:
        if not isinstance(source, str) or not is_asset_ref(source) or source in resolved:
            continue
        asset_id = asset_id_from_ref(source)
        try:
            resolved[source] = client.assets.get(asset_id)
        except NotFoundError:
            raise click.BadParameter(
                _messages.asset_not_found(asset_id), param_hint="SOURCE"
            ) from None
    return resolved


def _log_format_fallbacks(
    targets: Sequence[Target],
    output_format: str | None,
    catalog: Mapping[str, Model] | None,
) -> None:
    """Say which models kept their own default because --format is not one of theirs."""
    wanted = requested_formats(output_format)
    if not wanted or catalog is None:
        return
    for target in targets:
        model = catalog.get(target.model)
        if model is None or any(f in wanted for f in target.formats):
            continue
        missing = [f for f in wanted if f not in model.output_formats]
        logger.info(
            "%s: %s (does not output %s; offers %s)",
            target.model,
            ", ".join(target.formats),
            ", ".join(missing),
            ", ".join(model.output_formats),
        )


def _subtitle(options: RunOptions, targets: Sequence[Target], source_count: int) -> str:
    """One header line: what runs, how many at a time for a batch, and where it lands.

    The footer counts the files, so the header does not repeat that.
    """
    by_model = {target.model: ", ".join(target.formats) for target in targets}
    if len(set(by_model.values())) <= 1:
        # Every model writes the same formats: name them once.
        parts = [", ".join(by_model), next(iter(by_model.values()), "")]
    else:
        # A mixed run, say vocals as mp3 beside alignment as srt: say which is which.
        parts = [", ".join(f"{model} ({fmts})" for model, fmts in by_model.items())]
    if options.language:
        parts.append(f"lang {options.language}")
    if options.num_speakers is not None:
        parts.append(f"{options.num_speakers} speakers")
    if options.residual:
        parts.append("residual")
    if source_count > 1:
        # The pool is never wider than the batch, so say what will really run.
        parts.append(f"{min(options.max_concurrent, source_count)} at a time")
    line = "  ·  ".join(part for part in parts if part)
    if options.download:
        return f"{line}  →  {_shown_path(options.output_dir)}/"
    return f"{line}  ·  no download"


def _shown_path(path: Path) -> str:
    """A path as the user would type it: relative to the working directory when under it."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


@click.command()
@click.argument("sources", nargs=-1, required=True, metavar="SOURCE...")
@click.option(
    "-m",
    "--model",
    "model_values",
    multiple=True,
    required=True,
    metavar="MODEL[,MODEL...]",
    help="Models to run; repeat or comma-separate. See `audioshake models`.",
)
@click.option(
    "-o",
    "--output",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("audioshake-output"),
    show_default=True,
    help="Directory for results; one sub-directory per source",
)
@click.option(
    "-f",
    "--format",
    "output_format",
    metavar="FMT[,FMT...]",
    help="Output formats; each model produces the ones it offers "
    "(default: wav for audio, json for text)",
)
@click.option("--residual", is_flag=True, help="Also return everything each model did not capture")
@click.option(
    "--language", metavar="CODE", help="Language for transcription/alignment (en, es, ...)"
)
@click.option(
    "--num-speakers",
    type=click.IntRange(min=1),
    help=f"Speaker count, for {', '.join(SPEAKER_COUNT_MODELS)}",
)
@click.option(
    "-c",
    "--concurrency",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="Sources processed at the same time",
)
@click.option("-r", "--recursive", is_flag=True, help="Descend into sub-directories")
@click.option("--no-download", is_flag=True, help="Leave results in the API's storage")
@click.option(
    "--timeout",
    type=click.FloatRange(min=1),
    default=3600.0,
    show_default=True,
    help="Seconds to wait for each task",
)
@click.option(
    "--retries",
    type=click.IntRange(min=0),
    default=DEFAULT_RETRIES,
    show_default=True,
    help="Retries for a failed request; 0 fails on the first error",
)
@click.pass_obj
def run(
    settings: Settings,
    sources: tuple[str, ...],
    model_values: tuple[str, ...],
    output_dir: Path,
    output_format: str | None,
    residual: bool,
    language: str | None,
    num_speakers: int | None,
    concurrency: int,
    recursive: bool,
    no_download: bool,
    timeout: float,
    retries: int,
) -> None:
    """Run models on files, directories or URLs.

    \b
      audioshake run song.wav -m vocals,drums
      audioshake run ./album -m effects,dialogue,music_fx -o stems/ -f mp3
      audioshake run https://example.com/a.mp3 -m transcription --language en
      audioshake run song.wav -m alignment --language en -f srt
      audioshake run song.wav -m vocals,alignment -f srt,mp3
      audioshake run panel.wav -m multi_voice --num-speakers 3
    """
    model_list = expand_models(model_values)
    if num_speakers is not None and not any(m in SPEAKER_COUNT_MODELS for m in model_list):
        logger.warning(_messages.num_speakers_ignored(SPEAKER_COUNT_MODELS))

    items = collect_sources(sources, recursive=recursive)
    logger.info("%d source(s), models: %s", len(items), ", ".join(model_list))
    if not no_download:
        # Created up front so an unwritable -o fails before anything is uploaded.
        output_dir.mkdir(parents=True, exist_ok=True)

    options = RunOptions(
        models=model_list,
        output_dir=output_dir,
        format=output_format,
        residual=residual,
        language=language,
        num_speakers=num_speakers,
        download=not no_download,
        max_concurrent=concurrency,
        timeout=timeout,
        json=settings.json,
    )
    live = not settings.plain and not settings.json and settings.verbosity == 0
    client = settings.client(retries=retries)
    with client:
        # Check the options against the model catalog once, before any upload is
        # billed. run() repeats this per source from the client's cache.
        catalog = catalog_or_none(lambda: client.models.list(enabled_only=False))
        targets = build_targets(
            model_list,
            catalog=catalog,
            format=output_format,
            residual=residual,
            language=language,
            num_speakers=num_speakers,
        )
        _log_format_fallbacks(targets, output_format, catalog)
        tracker = ProgressTracker(
            title=f"AudioShake v{__version__}",
            subtitle=_subtitle(options, targets, len(items)),
            live=live,
        )
        runner = BatchRunner(client, options, tracker, assets=resolve_assets(client, items))
        with tracker:
            result = runner.run(items)

    runner.print_summary()
    if not result.ok:
        sys.exit(result.exit_code)
