import json
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
import respx
from click.testing import CliRunner

from audioshake import Asset, NotFoundError, RunResult, Task, cli

BASE = "https://api.test"


def _task(task_id="task_1", status="completed", cost=1.0):
    return Task.from_api(
        {
            "id": task_id,
            "targets": [
                {
                    "model": "vocals",
                    "status": status,
                    "cost": cost,
                    "output": [{"name": "vocals", "format": "wav", "link": "https://s/x"}],
                }
            ],
        }
    )


class _Catalog:
    """``models.list``: the models the CLI validates against, by id with their formats."""

    def __init__(self, formats_by_id=None):
        from audioshake import Model

        self.models = [
            Model.from_api({"id": model_id, "outputFormats": formats})
            for model_id, formats in (formats_by_id or {}).items()
        ]

    def list(self, enabled_only=True):
        return self.models


class _FakeAssets:
    """``assets.get``: every id resolves to the same uploaded file, except ``missing``."""

    def __init__(self):
        self.lookups = []

    def get(self, asset_id):
        self.lookups.append(asset_id)
        if asset_id == "missing":
            raise NotFoundError("Failed to get asset: HTTP 404 Not Found", status=404)
        return Asset(id=asset_id, name="Live Take.m4a")


class FakeClient:
    instances: ClassVar[list] = []
    models = _Catalog()

    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key
        self.kwargs = kwargs
        self.runs = []
        self.assets = _FakeAssets()
        FakeClient.instances.append(self)

    def run(self, source, models, **kwargs):
        self.runs.append((source, models, kwargs))
        return RunResult(task=_task(), files=(Path("out/a_vocals.wav"),))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake(monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr("audioshake.cli._settings.AudioShake", FakeClient)
    monkeypatch.setenv("AUDIOSHAKE_API_KEY", "env-key")
    return FakeClient


def invoke(*args):
    return CliRunner().invoke(cli.main, list(args))


class TestRun:
    def test_forwards_every_option(self, fake, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x")
        result = invoke(
            "--plain",
            "run",
            str(src),
            "-m",
            "effects,dialogue,music_fx",
            "-m",
            "drums",
            "-o",
            str(tmp_path / "out"),
            "-f",
            "mp3",
            "--residual",
            "--language",
            "en",
            "--num-speakers",
            "2",
            "-c",
            "3",
            "--timeout",
            "60",
            "--retries",
            "1",
        )
        assert result.exit_code == 0, result.stderr
        client = fake.instances[0]
        assert client.api_key is None and client.kwargs == {
            "retries": 1
        }  # env key read by the library
        source, models, kwargs = client.runs[0]
        assert source == src
        assert models == ["effects", "dialogue", "music_fx", "drums"]
        assert kwargs["format"] == "mp3" and kwargs["residual"] is True
        assert kwargs["language"] == "en" and kwargs["num_speakers"] == 2
        assert kwargs["timeout"] == 60.0
        assert kwargs["output_dir"] == tmp_path / "out" / "song"
        assert "--num-speakers only applies" in result.stderr
        assert "✔ song.wav" in result.stdout
        assert "task_1" in result.stdout  # the one-line form names the task in full

    def test_no_download_creates_no_output_folder(self, fake, tmp_path):
        out = tmp_path / "out"
        result = invoke(
            "--plain", "run", "https://x/a.wav", "-m", "vocals", "-o", str(out), "--no-download"
        )
        assert result.exit_code == 0, result.stderr
        assert fake.instances[0].runs[0][2]["download"] is False
        assert not out.exists()

    def test_an_asset_source_reuses_the_upload(self, fake, tmp_path):
        out = tmp_path / "out"
        result = invoke("--json", "run", "asset:abc", "-m", "vocals", "-o", str(out))
        assert result.exit_code == 0, result.stderr
        client = fake.instances[0]
        assert client.assets.lookups == ["abc"]
        source, _models, kwargs = client.runs[0]
        assert isinstance(source, Asset) and source.id == "abc"  # no upload
        assert kwargs["output_dir"] == out / "Live Take"  # filed by the uploaded file's name
        line = json.loads(result.stdout)
        assert line["source"] == "asset:abc" and line["asset_id"] == "abc"

    @pytest.mark.parametrize(
        ("source", "explanation"),
        [("asset:missing", "may have expired"), ("asset:", "asset:<id>")],
        ids=["expired", "empty"],
    )
    def test_an_unusable_asset_reference_is_a_usage_error_before_anything_runs(
        self, fake, source, explanation
    ):
        result = invoke("run", source, "-m", "vocals")
        assert result.exit_code == 2
        assert explanation in result.stderr
        assert not any(client.runs for client in fake.instances)  # rejected before any run

    def test_api_key_flag_beats_env(self, fake, tmp_path):
        invoke("--api-key", "flag-key", "run", "https://x/a.wav", "-m", "vocals")
        assert fake.instances[0].api_key == "flag-key"

    def test_json_output_is_ndjson_on_stdout_only(self, fake):
        result = invoke("--json", "run", "https://x/a.wav", "https://x/b.wav", "-m", "vocals")
        assert result.exit_code == 0, result.stderr
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        assert len(lines) == 2
        # The source is reported as given, so two files with one name stay apart.
        assert {line["source"] for line in lines} == {"https://x/a.wav", "https://x/b.wav"}
        assert lines[0]["status"] == "completed" and lines[0]["task_id"] == "task_1"
        assert lines[0]["files"] == ["out/a_vocals.wav"]
        assert "summary" not in result.stdout.lower()

    def test_a_directory_yields_its_media_files_and_recurses_only_when_asked(self, fake, tmp_path):
        (tmp_path / "a.wav").write_bytes(b"")
        (tmp_path / "notes.txt").write_bytes(b"")  # not media: never a source
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.MP3").write_bytes(b"")  # extensions match in any case
        invoke("--plain", "run", str(tmp_path), "-m", "vocals")
        assert [Path(r[0]).name for r in fake.instances[0].runs] == ["a.wav"]
        invoke("--plain", "run", str(tmp_path), "-m", "vocals", "-r")
        assert sorted(Path(r[0]).name for r in fake.instances[1].runs) == ["a.wav", "b.MP3"]

    def test_missing_source_is_a_usage_error(self, fake, tmp_path):
        result = invoke("run", str(tmp_path / "nope.wav"), "-m", "vocals")
        assert result.exit_code == 2
        assert "does not exist" in result.stderr

    @pytest.mark.parametrize(
        ("catalog", "args", "explanation"),
        [
            ({"vocals": ["wav", "mp3"]}, ["-m", "vocals", "-f", "ogg"], "cannot output 'ogg'"),
            ({"multi_voice": ["wav"]}, ["-m", "multi-voice"], "Did you mean 'multi_voice'"),
            ({"vocals_lead": ["wav"]}, ["-m", "lead-vocals"], "Did you mean 'vocals_lead'"),
            ({}, ["-m", "vocals,vocals"], "more than once"),
        ],
        ids=["unsupported format", "mistyped model", "reordered words", "duplicate model"],
    )
    def test_validation_is_a_usage_error_before_any_run(
        self, fake, monkeypatch, catalog, args, explanation
    ):
        """The library owns each rule; the CLI maps a rejection to exit 2 with no upload."""
        monkeypatch.setattr(FakeClient, "models", _Catalog(catalog), raising=False)
        result = invoke("run", "https://x/a.wav", "https://x/b.wav", *args)
        assert result.exit_code == 2
        assert explanation in result.stderr
        assert not any(client.runs for client in fake.instances)  # rejected before any run

    def test_a_model_keeping_its_default_format_is_logged(self, fake, monkeypatch):
        catalog = _Catalog({"vocals": ["wav", "mp3"], "alignment": ["json", "txt", "srt"]})
        monkeypatch.setattr(FakeClient, "models", catalog, raising=False)
        result = invoke(
            "--plain", "-v", "run", "https://x/a.wav", "-m", "vocals,alignment", "-f", "mp3"
        )
        assert result.exit_code == 0, result.stderr
        # The model that kept its default is named, with the format it kept and the one
        # it did not offer; the model that took the requested format is not mentioned.
        log = " ".join(result.stderr.split())  # the handler wraps long lines
        assert "alignment" in log and "json" in log and "mp3" in log
        assert "vocals:" not in log

    def test_failed_item_exits_1_and_summarises(self, fake, tmp_path, monkeypatch):
        from audioshake import AudioShakeError

        def failing(self, source, models, **kwargs):
            if "bad" in str(source):
                raise AudioShakeError("Failed to upload asset: HTTP 415")
            return RunResult(task=_task(), files=())

        monkeypatch.setattr(FakeClient, "run", failing)
        result = invoke("--plain", "run", "https://x/good.wav", "https://x/bad.wav", "-m", "vocals")
        assert result.exit_code == 1
        assert "✘ bad.wav" in result.stdout and "HTTP 415" in result.stdout
        assert "2 files" in result.stdout and "1 failed" in result.stdout

    def test_verbose_logs_go_to_stderr_and_leave_stdout_alone(self, fake):
        quiet = invoke("run", "https://x/a.wav", "-m", "vocals")
        verbose = invoke("-v", "run", "https://x/a.wav", "-m", "vocals")
        assert verbose.stderr.strip() and not quiet.stderr.strip()
        assert verbose.stdout == quiet.stdout


class TestExitCodes:
    def test_network_failure_is_4(self, monkeypatch):
        monkeypatch.setenv("AUDIOSHAKE_API_KEY", "k")
        monkeypatch.setenv("AUDIOSHAKE_BASE_URL", BASE)
        monkeypatch.setattr("audioshake._retry.sleep", lambda s: None)
        with respx.mock(base_url=BASE) as api:
            api.get("/models").mock(side_effect=httpx.ConnectError("down"))
            result = invoke("models")
        assert result.exit_code == 4
        assert "Failed to list models" in result.stderr

    def test_a_bug_is_5(self, monkeypatch):
        monkeypatch.setenv("AUDIOSHAKE_API_KEY", "k")
        monkeypatch.setenv("AUDIOSHAKE_BASE_URL", BASE)

        def broken(self, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("audioshake.client.Models.list", broken)
        result = invoke("models")
        # Handled, so: our message, a code of its own, no stack trace.
        assert result.exit_code == 5
        assert "Unexpected error (RuntimeError)" in result.stderr
        assert "Traceback" not in result.stderr

    def test_an_answer_that_is_not_json_is_5(self, monkeypatch):
        monkeypatch.setenv("AUDIOSHAKE_API_KEY", "k")
        monkeypatch.setenv("AUDIOSHAKE_BASE_URL", BASE)
        with respx.mock(base_url=BASE) as api:
            api.get("/models").respond(200, text="<html>upstream host:8080</html>")
            result = invoke("models")
        assert result.exit_code == 5
        assert "not JSON" in result.stderr and "host:8080" not in result.stderr

    def test_an_answer_of_the_wrong_shape_is_5(self, monkeypatch):
        monkeypatch.setenv("AUDIOSHAKE_API_KEY", "k")
        monkeypatch.setenv("AUDIOSHAKE_BASE_URL", BASE)
        with respx.mock(base_url=BASE) as api:
            api.get("/models").respond(200, json={"foo": "bar"})
            result = invoke("--json", "models")
        assert result.exit_code == 5, result.stderr
        assert result.stdout == ""

    def test_a_non_json_error_body_is_not_echoed(self, monkeypatch):
        monkeypatch.setenv("AUDIOSHAKE_API_KEY", "k")
        monkeypatch.setenv("AUDIOSHAKE_BASE_URL", BASE)
        monkeypatch.setattr("audioshake._retry.sleep", lambda s: None)
        with respx.mock(base_url=BASE) as api:
            api.get("/models").respond(502, html="<html>nginx/1.24.0 upstream host:8080</html>")
            result = invoke("models")
        assert result.exit_code == 4
        assert "HTTP 502" in result.stderr
        assert "nginx" not in result.stderr and "host:8080" not in result.stderr


class TestReadCommands:
    @pytest.fixture
    def api(self, monkeypatch):
        monkeypatch.setenv("AUDIOSHAKE_API_KEY", "k")
        monkeypatch.setenv("AUDIOSHAKE_BASE_URL", BASE)
        with respx.mock(base_url=BASE, assert_all_called=False) as mock:
            yield mock

    def test_models_table_and_json(self, api):
        models = json.loads((Path(__file__).parent.parent / "fixtures" / "models.json").read_text())
        api.get("/models").respond(200, json=models)
        table = invoke("models")
        assert table.exit_code == 0, table.stderr
        assert "multi_voice" in table.stdout and "≤ 90 min" in table.stdout
        assert "smart_mute" not in table.stdout  # requestAccess hidden by default
        everything = invoke("--json", "models", "--all")
        assert len(json.loads(everything.stdout)) == 28

    def test_tasks_list_get_download(self, api, tmp_path):
        raw = _task().raw
        api.get("/tasks").respond(200, json=[raw])
        api.get("/tasks/task_1").respond(200, json=raw)
        assert "task_1" in invoke("tasks", "list").stdout
        assert json.loads(invoke("--json", "tasks", "get", "task_1").stdout)["id"] == "task_1"
        api.get("/assets/asset_9").respond(200, json={"id": "asset_9", "name": "Live Take.m4a"})
        api.get("/tasks/task_1").respond(200, json={**raw, "assetId": "asset_9"})
        with respx.mock(base_url="https://s") as storage:
            storage.get("/x").respond(200, content=b"pcm")
            result = invoke("--json", "tasks", "download", "task_1", "-o", str(tmp_path))
        assert result.exit_code == 0, result.stderr
        files = json.loads(result.stdout)["files"]
        assert files == [str(tmp_path / "Live Take" / "vocals.wav")]

    @pytest.mark.parametrize(
        ("asset_id", "asset_status", "shown"),
        [
            ("asset_9", 200, "asset asset_9  (Live Take [final].m4a)"),  # brackets literal
            ("asset_9", 404, "asset asset_9\n"),  # the id alone when it cannot be read
            (None, None, None),  # made from a URL: no asset line at all
        ],
        ids=["readable", "unreadable", "none"],
    )
    def test_tasks_get_names_the_asset_it_was_made_from(self, api, asset_id, asset_status, shown):
        raw = {**_task().raw, "assetId": asset_id} if asset_id else _task().raw
        api.get("/tasks/task_1").respond(200, json=raw)
        found = {"id": "asset_9", "name": "Live Take [final].m4a"}
        assets = api.get("/assets/asset_9").respond(
            asset_status or 404, json=found if asset_status == 200 else {"message": "not found"}
        )
        result = invoke("tasks", "get", "task_1")
        assert result.exit_code == 0, result.stderr
        if shown is None:
            assert "asset " not in result.stdout
        else:
            assert shown in result.stdout
            assert result.stdout.index("task_1") < result.stdout.index("asset ")
            assert result.stdout.index("asset ") < result.stdout.index("Model")
        # --json is the raw object, which carries assetId itself: no lookup.
        lookups = assets.call_count
        raw_json = json.loads(invoke("--json", "tasks", "get", "task_1").stdout)
        assert raw_json.get("assetId") == asset_id
        assert assets.call_count == lookups

    def test_tasks_download_falls_back_to_task_id_without_asset(self, api, tmp_path):
        raw = _task().raw
        api.get("/tasks/task_1").respond(200, json=raw)
        with respx.mock(base_url="https://s") as storage:
            storage.get("/x").respond(200, content=b"pcm")
            result = invoke("--json", "tasks", "download", "task_1", "-o", str(tmp_path))
        assert json.loads(result.stdout)["files"] == [str(tmp_path / "task_1" / "vocals.wav")]

    def test_tasks_list_limit_and_since(self, api):
        def task(task_id, created):
            return {**_task(task_id).raw, "createdAt": created}

        # The API lists newest first.
        route = api.get("/tasks").respond(
            200,
            json=[
                task("new", "2099-01-01T00:00:00Z"),
                task("mid", "2026-06-01T12:00:00Z"),
                task("old", "2026-01-01T00:00:00Z"),
            ],
        )
        table = invoke("tasks", "list")
        assert table.stdout.index("new") < table.stdout.index("mid") < table.stdout.index("old")
        assert "2026-06-01 12:00" in table.stdout
        assert route.calls.last.request.url.params["take"] == "20"  # the default limit
        limited = invoke("--json", "tasks", "list", "--limit", "1")
        assert [json.loads(line)["id"] for line in limited.stdout.splitlines()] == ["new"]
        assert route.calls.last.request.url.params["take"] == "1"
        since = invoke("--json", "tasks", "list", "--since", "2026-03-01")
        assert [json.loads(line)["id"] for line in since.stdout.splitlines()] == ["new", "mid"]
        recent = invoke("--json", "tasks", "list", "--since", "3650d")
        assert len(recent.stdout.splitlines()) == 3
        bad = invoke("tasks", "list", "--since", "yesterday")
        assert bad.exit_code == 2 and "not a duration" in bad.stderr
        assert invoke("tasks", "list", "--since", "100000000d").exit_code == 2

    def test_a_long_target_error_is_capped_in_the_table(self, api):
        api.get("/tasks/task_1").respond(
            200,
            json={
                "id": "task_1",
                "targets": [{"model": "vocals", "status": "failed", "error": "boom " * 200}],
            },
        )
        detail = invoke("tasks", "get", "task_1")
        assert detail.exit_code == 0, detail.stderr
        assert "boom" in detail.stdout and "…" in detail.stdout
        assert detail.stdout.count("boom") < 100

    def test_version_and_help(self, api):
        assert "audioshake, version" in invoke("--version").stdout
        assert "run" in invoke("-h").stdout


class TestPartlyCompletedRun:
    """End to end through the real library: a failed model must not cost the others."""

    def test_completed_models_are_downloaded_and_the_run_exits_1(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUDIOSHAKE_API_KEY", "k")
        monkeypatch.setenv("AUDIOSHAKE_BASE_URL", BASE)
        task = {
            "id": "task_9",
            "targets": [
                {
                    "model": "vocals",
                    "status": "completed",
                    "cost": 2.0,
                    "output": [{"name": "vocals", "format": "wav", "link": "https://s/v"}],
                },
                {"model": "drums", "status": "failed", "cost": 0, "error": "worker died"},
            ],
        }
        with respx.mock(base_url=BASE, assert_all_called=False) as api:
            api.get("/models").respond(200, json=[])
            api.post("/tasks").respond(200, json={"id": "task_9", "targets": []})
            api.get("/tasks/task_9").respond(200, json=task)
            with respx.mock(base_url="https://s") as storage:
                storage.get("/v").respond(200, content=b"pcm")
                result = invoke(
                    "--json",
                    "run",
                    "https://cdn.test/song.wav",
                    "-m",
                    "vocals,drums",
                    "-o",
                    str(tmp_path),
                )

        assert result.exit_code == 1, result.stderr
        line = json.loads(result.stdout)
        assert line["status"] == "partial" and line["task_id"] == "task_9"
        assert line["cost"] == 2.0 and "drums" in line["error"]
        assert [Path(f).read_bytes() for f in line["files"]] == [b"pcm"]
