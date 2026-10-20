import dataclasses
import json
import sys
import threading
from pathlib import Path

import pytest
from rich.console import Console

from audioshake import (
    Asset,
    AudioShakeError,
    AuthenticationError,
    ProgressEvent,
    RunResult,
    Task,
    TaskFailedError,
    UnexpectedResponseError,
)
from audioshake._run import is_url
from audioshake.cli._settings import EXIT_AUTH, EXIT_UNEXPECTED
from audioshake.cli.progress import ProgressTracker, Status
from audioshake.cli.run import BatchRunner, RunOptions, _DownloadTally, _label, output_dirs


def _task(task_id="task_abc123", statuses=("completed", "completed"), cost=1.5):
    return Task.from_api(
        {
            "id": task_id,
            "targets": [
                {"model": model, "status": status, "cost": cost, "duration": 12.0}
                if status == "completed"
                else {"model": model, "status": status, "error": "worker died"}
                for model, status in zip(("vocals", "drums"), statuses, strict=True)
            ],
        }
    )


class FakeClient:
    """Stands in for AudioShake: replays a scripted run() per source.

    A file source is "uploaded" as ``asset_<stem>`` before its outcome, unless
    ``uploads=False``, which plays a failure during the upload itself.
    """

    def __init__(self, outcomes, *, uploads=True):
        self.outcomes = outcomes  # {source name: RunResult | Exception}
        self.uploads = uploads
        self.calls = []

    def run(self, source, models, *, on_progress=None, **kwargs):
        self.calls.append((source, models, kwargs))
        name = Path(str(source)).name
        outcome = self.outcomes[name]
        asset = None
        if self.uploads and not is_url(source):
            asset = Asset(id=f"asset_{Path(name).stem}", name=name)
        if isinstance(outcome, Exception):
            if on_progress:
                on_progress(ProgressEvent("upload", name, 10, 10, asset=asset))
            raise outcome
        outcome = dataclasses.replace(outcome, asset=asset)
        task = outcome.task
        if on_progress:
            on_progress(ProgressEvent("upload", name, 5, 10))
            on_progress(ProgressEvent("upload", name, 10, 10, asset=asset))
            on_progress(ProgressEvent("queued", task.id, 0, len(task.targets), task))
            on_progress(ProgressEvent("processing", task.id, 1, len(task.targets), task))
            on_progress(ProgressEvent("download", "vocals", 50, 100, task))
            on_progress(ProgressEvent("done", task.id, 2, 2, task))
        return outcome

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def options(tmp_path):
    return RunOptions(models=["vocals", "drums"], output_dir=tmp_path / "out", max_concurrent=2)


def test_successful_batch_tallies_cost_and_marks_tree(options, tmp_path):
    ok = RunResult(task=_task(), files=(tmp_path / "a.wav",))
    client = FakeClient({"one.wav": ok, "two.wav": ok})
    tracker = ProgressTracker("t", live=False)
    runner = BatchRunner(client, options, tracker)

    result = runner.run([tmp_path / "one.wav", tmp_path / "two.wav"])

    assert result.ok and result.succeeded == 2 and result.total_cost == 6.0
    assert tracker.credits == 6.0
    root = tracker.roots[0]
    assert root.status is Status.DONE
    assert [c.label for c in root.children] == ["vocals", "drums"]
    assert all(c.status is Status.DONE for c in root.children)
    assert "credits" in root.children[0].detail
    # per-source output dir and options are forwarded to the library
    source, models, kwargs = client.calls[0]
    assert models == ["vocals", "drums"]
    assert kwargs["output_dir"] == options.output_dir / Path(str(source)).stem
    assert kwargs["download"] is True and kwargs["residual"] is False


def test_every_source_that_did_not_succeed_is_reported_with_its_reason(options, tmp_path):
    """Failed and partly completed sources are counted apart, each with its task and reason."""
    failed_task = _task(statuses=("completed", "failed"))
    client = FakeClient(
        {
            "good.wav": RunResult(task=_task(), files=()),
            "bad.wav": TaskFailedError(failed_task),
            "gone.wav": AudioShakeError("Failed to upload asset: HTTP 415 unsupported"),
            "part.wav": RunResult(task=failed_task, files=(tmp_path / "v.wav",)),
        }
    )
    tracker = ProgressTracker("t", live=False)
    runner = BatchRunner(client, options, tracker)

    result = runner.run(
        [tmp_path / name for name in ("good.wav", "bad.wav", "gone.wav", "part.wav")]
    )

    assert not result.ok
    assert result.succeeded == 1 and len(result.partial) == 1
    reasons = {Path(f.source).name: f.summary for f in result.failed}
    assert "drums: worker died" in reasons["bad.wav"]
    assert "HTTP 415" in reasons["gone.wav"]
    assert [n.status for n in tracker.roots].count(Status.FAILED) == 2

    out = Console(record=True, width=200, highlight=False)
    runner.print_summary(out)
    text = out.export_text()
    assert "4 files" in text and "2 failed" in text and "1 partly completed" in text
    assert "worker died" in text and "HTTP 415" in text and "task task_abc123" in text


def test_a_row_names_its_task_while_it_runs_and_once_it_settles(options, tmp_path):
    """The id is what `tasks get` needs, so it stays on the row from creation onwards."""
    task = Task.from_api(
        {"id": "task_xyz789", "targets": [{"model": "vocals", "status": "processing"}]}
    )
    tracker = ProgressTracker("t", live=False)
    runner = BatchRunner(FakeClient({}), options, tracker)
    node = tracker.add_item("song.wav")

    # One target means no child rows, so the parent carries the id itself.
    runner._on_event(node, ProgressEvent("processing", task.id, 0, 1, task), _DownloadTally())
    assert "task_xyz789" in node.detail and "processing" in node.detail
    runner._on_event(node, ProgressEvent("download", "vocals", 50, 100, task), _DownloadTally())
    assert "task_xyz789" in node.detail and "downloading" in node.detail

    ok = RunResult(task=_task(), files=(tmp_path / "a.wav",))
    settled = ProgressTracker("t", live=False)
    BatchRunner(FakeClient({"one.wav": ok}), options, settled).run([tmp_path / "one.wav"])
    assert "task_abc123" in settled.roots[0].detail and "2/2" in settled.roots[0].detail


def test_zero_cost_is_left_out_of_the_tree_and_summary(options, tmp_path):
    """A free task should not advertise "0.00 credits" anywhere."""
    free = RunResult(task=_task(cost=0.0), files=(tmp_path / "a.wav",))
    client = FakeClient({"one.wav": free})
    tracker = ProgressTracker("t", live=False)
    runner = BatchRunner(client, options, tracker)

    result = runner.run([tmp_path / "one.wav"])

    assert result.ok and result.total_cost == 0.0
    root = tracker.roots[0]
    assert "credits" not in root.children[0].detail
    out = Console(record=True, width=100, highlight=False)
    runner.print_summary(out)
    text = out.export_text()
    assert "credits" not in text and "Total cost" not in text


@pytest.mark.parametrize(
    ("sources", "names"),
    [
        (
            [
                "a/take.wav",
                "b/take.wav",
                "https://x/1/sourcefile",
                "https://x/2/sourcefile",
                "o.wav",
            ],
            ["take", "take-2", "sourcefile", "sourcefile-2", "o"],
        ),
        # A generated name is reserved: the file really named take-2.wav gets its own.
        (["a/take.wav", "b/take.wav", "take-2.wav"], ["take", "take-2", "take-2-2"]),
        # One directory on macOS and Windows, so case alone does not keep sources apart.
        (["A/Take.wav", "b/take.wav", "c/TAKE.wav"], ["Take", "take-2", "TAKE-3"]),
    ],
    ids=["shared stems", "generated name taken", "case only"],
)
def test_sources_that_would_share_a_folder_get_separate_ones(sources, names):
    assert [d.name for d in output_dirs(Path("out"), sources)] == names


def _multi_voice_task(speakers=4):
    """The shape that motivated concurrent downloads: one target, many outputs."""
    return Task.from_api(
        {
            "id": "task_mv",
            "targets": [
                {
                    "model": "multi_voice",
                    "status": "completed",
                    "cost": 2.0,
                    "output": [
                        {
                            "name": f"speaker{i}",
                            "format": "wav",
                            "link": f"https://storage.test/s{i}.wav",
                        }
                        for i in range(speakers)
                    ],
                }
            ],
        }
    )


class TestDownloadTally:
    def test_sums_interleaved_outputs_into_one_line(self):
        tally = _DownloadTally()
        tally.expect(_multi_voice_task(4))
        tally.record("speaker0", 500_000, 1_000_000)
        tally.record("speaker2", 1_000_000, 1_000_000)
        line = tally.record("speaker1", 250_000, 1_000_000)
        # One of four outputs finished, 1.75 MB moved between them.
        assert line == "downloading 1/4 \u00b7 1.7 MB"
        for name in ("speaker0", "speaker1", "speaker3"):
            line = tally.record(name, 1_000_000, 1_000_000)
        assert line.startswith("downloading 4/4 \u00b7 ")

    def test_bytes_never_fall_as_later_outputs_join(self):
        # A percentage over only the started outputs would drop here; bytes must not.
        tally = _DownloadTally()
        tally.expect(_multi_voice_task(4))
        first = tally.record("speaker0", 900_000, 1_000_000)
        second = tally.record("speaker1", 0, 1_000_000)
        assert first == "downloading 0/4 \u00b7 878.9 KB"
        assert second == first

    def test_a_lone_output_keeps_the_name_and_percentage(self):
        tally = _DownloadTally()
        assert tally.record("vocals", 30, 100) == "downloading vocals 30%"


class InterruptingClient:
    """Records every source that reaches run(), and interrupts on the first."""

    def __init__(self, interrupt_on):
        self.interrupt_on = interrupt_on
        self.started = []
        self._lock = threading.Lock()

    def run(self, source, models, *, on_progress=None, **kwargs):
        with self._lock:
            self.started.append(Path(str(source)).name)
        if Path(str(source)).name == self.interrupt_on:
            raise KeyboardInterrupt
        return RunResult(task=_task(), files=())


def test_interrupt_does_not_start_the_rest_of_the_batch(tmp_path):
    """Ctrl-C cancels every queued source: each would upload and create a billed task."""
    client = InterruptingClient(interrupt_on="a.wav")
    sources = [tmp_path / name for name in ("a.wav", "b.wav", "c.wav")]
    for source in sources:
        source.write_bytes(b"x")
    options = RunOptions(models=["vocals"], output_dir=tmp_path / "out", max_concurrent=1)
    runner = BatchRunner(client, options, ProgressTracker("t", live=False))

    with pytest.raises(KeyboardInterrupt):
        runner.run(sources)

    assert client.started == ["a.wav"]


def test_a_worker_picked_up_after_the_stop_touches_no_api(tmp_path):
    """The gap between cancelling a future and the pool running it is covered too."""
    client = InterruptingClient(interrupt_on="never")
    options = RunOptions(models=["vocals"], output_dir=tmp_path / "out")
    runner = BatchRunner(client, options, ProgressTracker("t", live=False))
    runner._stop.set()

    with pytest.raises(AudioShakeError):
        runner._run_one(tmp_path / "a.wav", runner.tracker.add_item("a.wav"), tmp_path / "out")

    assert client.started == []


class UploadingClient:
    """Uploads in steps, reporting progress, then creates a task: the billable step."""

    def __init__(self, on_step=None):
        self.created = []
        self.on_step = on_step

    def run(self, source, models, *, on_progress, **kwargs):
        for step in range(3):
            if self.on_step:
                self.on_step(step)
            on_progress(ProgressEvent("upload", Path(str(source)).name, step, 3))
        self.created.append(Path(str(source)).name)
        return RunResult(task=_task(), files=())


def test_an_upload_in_progress_stops_before_its_task_is_created(tmp_path):
    """A source still uploading at Ctrl-C would otherwise go on to create a billed task."""
    options = RunOptions(models=["vocals"], output_dir=tmp_path / "out")
    runner = BatchRunner(None, options, ProgressTracker("t", live=False))
    runner.client = UploadingClient(on_step=lambda step: step == 1 and runner._stop.set())

    with pytest.raises(AudioShakeError, match="before its task was created"):
        runner._run_one(tmp_path / "a.wav", runner.tracker.add_item("a.wav"), tmp_path / "out")

    assert runner.client.created == []


def test_an_interrupt_names_the_created_tasks_it_is_waiting_for(tmp_path, caplog):
    """Billed tasks are left to finish; the user is told which, so the wait is not a hang."""
    queued = threading.Event()
    release = threading.Event()

    class Client:
        def run(self, source, models, *, on_progress, **kwargs):
            if Path(str(source)).name == "a.wav":
                on_progress(ProgressEvent("queued", "task_abc123", 0, 2, _task()))
                queued.set()
                release.wait(5)
                return RunResult(task=_task(), files=())
            queued.wait(5)
            raise KeyboardInterrupt

    options = RunOptions(models=["vocals"], output_dir=tmp_path / "out", max_concurrent=2)
    runner = BatchRunner(Client(), options, ProgressTracker("t", live=False))
    try:
        with pytest.raises(KeyboardInterrupt), caplog.at_level("WARNING", logger="audioshake"):
            runner.run([tmp_path / "a.wav", tmp_path / "b.wav"])
    finally:
        release.set()

    waiting = [r.getMessage() for r in caplog.records if "already created" in r.getMessage()]
    assert waiting and "task_abc123" in waiting[0]


class _SettlesDuringTheWait:
    """b.wav interrupts once a.wav's task exists; a.wav then settles during the wait."""

    def __init__(self, outcome):
        self.outcome = outcome  # RunResult | Exception for a.wav
        self.queued = threading.Event()
        self.interrupted = threading.Event()

    def run(self, source, models, *, on_progress, **kwargs):
        if Path(str(source)).name == "a.wav":
            on_progress(ProgressEvent("queued", "task_abc123", 0, 2, _task()))
            self.queued.set()
            self.interrupted.wait(5)
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome
        self.queued.wait(5)
        self.interrupted.set()
        raise KeyboardInterrupt


@pytest.mark.parametrize("settles_as", ["completed", "failed"])
def test_a_source_that_settles_during_the_interrupt_wait_is_reported(tmp_path, capsys, settles_as):
    """Its task was billed either way, so it belongs in --json and the totals."""
    if settles_as == "completed":
        outcome = RunResult(task=_task(cost=2.0), files=(tmp_path / "v.wav",))
    else:
        outcome = TaskFailedError(_task(statuses=("failed", "failed"), cost=0.0))
    client = _SettlesDuringTheWait(outcome)
    options = RunOptions(
        models=["vocals"], output_dir=tmp_path / "out", max_concurrent=2, json=True
    )
    runner = BatchRunner(client, options, ProgressTracker("t", live=False))

    with pytest.raises(KeyboardInterrupt):
        runner.run([tmp_path / "a.wav", tmp_path / "b.wav"])

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    settled = [line for line in lines if line["task_id"] == "task_abc123"]
    assert [line["status"] for line in settled] == [settles_as]
    if settles_as == "completed":
        assert runner.result.succeeded == 1
        assert runner.result.total_cost == 4.0  # both targets of the task
        assert str(tmp_path / "v.wav") in settled[0]["files"]
    else:
        assert [f.task.id for f in runner.result.failed if f.task] == ["task_abc123"]


def test_a_second_interrupt_leaves_running_tasks_without_a_traceback(tmp_path, monkeypatch):
    """A second Ctrl-C exits 130 at once, through the CLI's own exit, with no traceback."""
    # The package re-exports the `run` command under the module's name.
    run_module = sys.modules[BatchRunner.__module__]

    class Exited(BaseException):
        pass

    def fake_exit(code):
        raise Exited(code)

    def interrupted_wait(futures):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_module.os, "_exit", fake_exit)
    monkeypatch.setattr(run_module, "wait_for_futures", interrupted_wait)
    release = threading.Event()
    queued = threading.Event()

    class Client:
        def run(self, source, models, *, on_progress, **kwargs):
            if Path(str(source)).name == "a.wav":
                on_progress(ProgressEvent("queued", "task_abc123", 0, 2, _task()))
                queued.set()
                release.wait(5)
                return RunResult(task=_task(), files=())
            queued.wait(5)
            raise KeyboardInterrupt

    options = RunOptions(models=["vocals"], output_dir=tmp_path / "out", max_concurrent=2)
    runner = BatchRunner(Client(), options, ProgressTracker("t", live=False))
    try:
        with pytest.raises(Exited) as exited:
            runner.run([tmp_path / "a.wav", tmp_path / "b.wav"])
    finally:
        release.set()
    assert exited.value.args == (130,)


def test_a_failed_task_still_reports_its_id_and_cost(options, tmp_path):
    """A task that fails was still created and billed, so the failure carries its id and cost."""
    failed_task = _task(statuses=("completed", "failed"), cost=5.0)
    client = FakeClient({"bad.wav": TaskFailedError(failed_task)})
    runner = BatchRunner(client, options, ProgressTracker("t", live=False))

    result = runner.run([tmp_path / "bad.wav"])

    assert [f.task.id for f in result.failed] == ["task_abc123"]
    assert result.total_cost == failed_task.cost > 0


@pytest.mark.parametrize("stage", ["before the task exists", "while the task still runs"])
def test_an_unknown_cost_is_null_not_zero(options, tmp_path, capsys, stage):
    """Nothing has been charged yet, or the charge is still growing: 0.0 would understate it."""
    from audioshake import TaskTimeoutError

    options.json = True
    running = _task(statuses=("completed", "processing"))

    class TimingOut(FakeClient):
        def run(self, source, models, *, on_progress=None, **kwargs):
            on_progress(ProgressEvent("processing", running.id, 1, 2, running))
            raise TaskTimeoutError(running.id, 1.0)

    if stage == "before the task exists":
        client, expected_task = FakeClient({"slow.wav": AudioShakeError("upload refused")}), None
    else:
        client, expected_task = TimingOut({}), running.id
    runner = BatchRunner(client, options, ProgressTracker("t", live=False))

    runner.run([tmp_path / "slow.wav"])

    line = json.loads(capsys.readouterr().out.strip())
    assert line["task_id"] == expected_task and line["cost"] is None


def test_the_batch_exit_code_follows_the_worst_cause(options, tmp_path):
    """The exit code reflects the worst cause, not just "a source failed" (1).

    Credentials come first: nothing in the batch will work until they are fixed.
    """
    client = FakeClient(
        {
            "a.wav": AudioShakeError("one bad file"),
            "b.wav": AuthenticationError("key expired", status=401),
        }
    )
    runner = BatchRunner(client, options, ProgressTracker("t", live=False))

    result = runner.run([tmp_path / "a.wav", tmp_path / "b.wav"])

    assert result.exit_code == EXIT_AUTH


@pytest.mark.parametrize(
    "error",
    [RuntimeError("off by one"), UnexpectedResponseError("unreadable")],
    ids=["a bug", "an answer the library could not read"],
)
def test_neither_a_bug_nor_an_unreadable_answer_is_a_failed_item(options, tmp_path, error):
    """Exit 5 exists so these are distinguishable from a source the API rejected."""
    runner = BatchRunner(FakeClient({"a.wav": error}), options, ProgressTracker("t", live=False))

    assert runner.run([tmp_path / "a.wav"]).exit_code == EXIT_UNEXPECTED


def test_a_url_label_does_not_carry_the_query_string(options, tmp_path):
    """A presigned source must not put its signature on screen or in the log."""
    assert _label("https://cdn.test/song.wav?X-Amz-Signature=SOURCE_SECRET") == "song.wav"
    assert _label("https://cdn.test/media/track.mp3") == "track.mp3"
    assert _label(tmp_path / "song.wav") == "song.wav"


def test_two_sources_with_one_name_stay_apart_in_json(options, tmp_path, capsys):
    """--json reports the source as given, so two files that share a name stay apart."""
    options.json = True
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "song.wav").write_bytes(b"x")
    client = FakeClient({"song.wav": AudioShakeError("nope")})
    runner = BatchRunner(client, options, ProgressTracker("t", live=False))

    runner.run([tmp_path / "a" / "song.wav", tmp_path / "b" / "song.wav"])

    sources = {json.loads(line)["source"] for line in capsys.readouterr().out.splitlines()}
    assert sources == {str(tmp_path / "a" / "song.wav"), str(tmp_path / "b" / "song.wav")}


def test_names_and_errors_with_brackets_are_printed_literally(options, tmp_path, capsys):
    """Names and API text are external: brackets print literally, never as markup."""
    ok = RunResult(task=_task(), files=(tmp_path / "a.wav",))
    client = FakeClient(
        {"song [remix].wav": ok, "demo.wav": AudioShakeError("rejected [/b] by the API")}
    )
    runner = BatchRunner(client, options, ProgressTracker("t", live=False))

    runner.run([tmp_path / "song [remix].wav", tmp_path / "demo.wav"])

    out = capsys.readouterr().out
    assert "song [remix].wav" in out and "rejected [/b] by the API" in out


LONG_REASON = "Unknown model 'vocalz'. Valid models: " + ", ".join(f"model_{i}" for i in range(80))


def _api_rejection():
    import httpx

    from audioshake.exceptions import error_from_response

    error = error_from_response(httpx.Response(400, json={"message": LONG_REASON}), "create task")
    assert len(str(error)) < len(LONG_REASON)  # the exception's own message is capped for display
    return error


def _model_failure(tmp_path):
    task = Task.from_api(
        {
            "id": "task_x",
            "targets": [
                {"model": "vocals", "status": "completed", "cost": 1.0},
                {"model": "drums", "status": "failed", "error": LONG_REASON},
            ],
        }
    )
    return RunResult(task=task, files=(tmp_path / "v.wav",))


@pytest.mark.parametrize("failure", ["the API rejected the task", "one model failed"])
def test_json_carries_the_apis_error_text_in_full(options, tmp_path, capsys, failure):
    """The display caps the API's text; --json must not, or a list of valid ids never finishes."""
    options.json = True
    outcome = _api_rejection() if failure.startswith("the API") else _model_failure(tmp_path)
    runner = BatchRunner(FakeClient({"s.wav": outcome}), options, ProgressTracker("t", live=False))

    result = runner.run([tmp_path / "s.wav"])

    line = json.loads(capsys.readouterr().out.strip())
    assert LONG_REASON in line["error"]
    assert all(len(f.summary) < len(LONG_REASON) for f in (*result.failed, *result.partial))


def test_json_carries_the_asset_id_so_a_file_need_not_be_uploaded_again(options, tmp_path, capsys):
    """The upload outlives the run: name it on success and on a failure after it."""
    options.json = True
    ok = RunResult(task=_task(), files=(tmp_path / "v.wav",))
    client = FakeClient(
        {
            "one.wav": ok,
            "two.wav": AudioShakeError("Unknown model 'vocalz'"),
            "clip.mp3": ok,
        }
    )
    runner = BatchRunner(client, options, ProgressTracker("t", live=False))

    runner.run([tmp_path / "one.wav", tmp_path / "two.wav", "https://cdn.test/clip.mp3"])

    lines = {
        json.loads(line)["source"]: json.loads(line)
        for line in capsys.readouterr().out.splitlines()
    }
    assert lines[str(tmp_path / "one.wav")]["asset_id"] == "asset_one"
    failed = lines[str(tmp_path / "two.wav")]
    assert failed["status"] == "failed" and failed["asset_id"] == "asset_two"
    assert "asset_two" not in failed["error"]  # the id has its own field
    assert lines["https://cdn.test/clip.mp3"]["asset_id"] is None  # never uploaded
    assert [f.asset_id for f in runner.result.failed] == ["asset_two"]

    # A failure during the upload itself has no asset to name either.
    client = FakeClient({"gone.wav": AudioShakeError("upload refused")}, uploads=False)
    BatchRunner(client, options, ProgressTracker("t", live=False)).run([tmp_path / "gone.wav"])
    line = json.loads(capsys.readouterr().out.strip())
    assert line["asset_id"] is None and line["task_id"] is None


def test_a_failure_after_the_upload_names_the_asset_on_screen(options, tmp_path, capsys):
    client = FakeClient({"two.wav": AudioShakeError("Unknown model 'vocalz'")})
    runner = BatchRunner(client, options, ProgressTracker("t", live=False))

    runner.run([tmp_path / "two.wav"])
    runner.print_summary(Console(file=sys.stdout, width=200))

    out = capsys.readouterr().out
    assert "uploaded as asset asset_two" in out  # the row outside the live view, and the summary
    assert runner.result.failed[0].asset_id == "asset_two"
    assert "asset_two" not in runner.result.failed[0].summary  # the row keeps to the error
