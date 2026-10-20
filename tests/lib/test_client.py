import json
import logging
from datetime import datetime

import httpx
import pytest

from audioshake import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AudioShake,
    AuthenticationError,
    NotFoundError,
    ServerError,
    Target,
    Task,
    TaskFailedError,
    TaskTimeoutError,
    UnexpectedResponseError,
    ValidationError,
    _messages,
)
from audioshake._http import resolve_base_url

from .conftest import BASE_URL


class TestConstruction:
    def test_reads_key_from_environment(self, monkeypatch, api):
        monkeypatch.setenv("AUDIOSHAKE_API_KEY", "env-key")
        monkeypatch.setenv("AUDIOSHAKE_BASE_URL", BASE_URL + "/")
        route = api.get("/tasks").respond(200, json=[])
        with AudioShake() as client:
            assert client.base_url == BASE_URL
            client.tasks.list()
        assert route.calls.last.request.headers["x-api-key"] == "env-key"

    def test_missing_key_is_an_authentication_error(self, monkeypatch):
        monkeypatch.delenv("AUDIOSHAKE_API_KEY", raising=False)
        with pytest.raises(AuthenticationError):
            AudioShake()

    @pytest.mark.parametrize(
        ("env_key", "kwargs", "expected"),
        [
            ("env-key", {"api_key": "arg-key"}, {"x-api-key": "arg-key"}),
            (None, {"bearer_token": "tok"}, {"authorization": "Bearer tok"}),
            (None, {"api_key": "k", "bearer_token": "tok"}, {"x-api-key": "k"}),
        ],
        ids=["argument beats environment", "bearer when no key", "key beats bearer"],
    )
    def test_credential_precedence(self, monkeypatch, api, env_key, kwargs, expected):
        """One credential is sent: the explicit key, else the bearer token, else the env key."""
        if env_key is None:
            monkeypatch.delenv("AUDIOSHAKE_API_KEY", raising=False)
        else:
            monkeypatch.setenv("AUDIOSHAKE_API_KEY", env_key)
        route = api.get("/tasks").respond(200, json=[])
        AudioShake(base_url=BASE_URL, **kwargs).tasks.list()
        headers = route.calls.last.request.headers
        for name, value in expected.items():
            assert headers[name] == value
        other = "authorization" if "x-api-key" in expected else "x-api-key"
        assert other not in headers

    def test_user_agent_identifies_the_sdk(self, client, api):
        route = api.get("/tasks").respond(200, json=[])
        client.tasks.list()
        assert route.calls.last.request.headers["user-agent"].startswith("audioshake-python/")

    def test_bring_your_own_http_client(self, api):
        own = httpx.Client()
        route = api.get("/tasks").respond(200, json=[])
        AudioShake(api_key="k", base_url=BASE_URL, http_client=own).tasks.list()
        assert route.called
        assert own.headers["x-api-key"] == "k"


class TestBaseUrl:
    @pytest.mark.parametrize(
        ("url", "warns"),
        [
            ("http://api.example.test/", True),
            ("https://api.example.test", False),
            ("http://localhost:8080", False),
            ("http://127.0.0.1:8080", False),
        ],
        ids=["plain http to a remote host", "https", "localhost", "loopback ip"],
    )
    def test_plain_http_warns_except_to_loopback(self, url, warns, monkeypatch, caplog):
        """A credential would travel in the clear; only a loopback host is exempt."""
        monkeypatch.delenv("AUDIOSHAKE_BASE_URL", raising=False)
        with caplog.at_level(logging.WARNING, logger="audioshake.http"):
            assert resolve_base_url(url) == url.rstrip("/")
        assert ("plain http" in caplog.text) is warns


class TestTasksCreate:
    def test_url_source_and_payload_shape(self, client, api, task_json):
        route = api.post("/tasks").respond(200, json=task_json)
        task = client.tasks.create(
            "https://example.com/a.wav",
            [Target("vocals"), Target("multi_voice", num_speakers=3)],
        )
        sent = json.loads(route.calls.last.request.content)
        assert sent == {
            "url": "https://example.com/a.wav",
            "targets": [
                {"model": "vocals", "formats": ["wav"]},
                {"model": "multi_voice", "formats": ["wav"], "numSpeakers": 3},
            ],
        }
        assert task.id == task_json["id"]

    def test_asset_source_uses_asset_id(self, client, api, task_json):
        route = api.post("/tasks").respond(200, json=task_json)
        client.tasks.create("asset_123", [Target("vocals")])
        sent = json.loads(route.calls.last.request.content)
        assert sent["assetId"] == "asset_123"
        assert "url" not in sent


class TestNoDuplicateTasks:
    """A lost response after POST /tasks may mean a billed task exists; never blind-retry."""

    def test_create_task_does_not_retry_transport_errors(self, client, api, no_sleep, caplog):
        route = api.post("/tasks").mock(side_effect=httpx.ReadTimeout("lost response"))
        with pytest.raises(APITimeoutError), caplog.at_level("WARNING", logger="audioshake"):
            client.tasks.create("https://x/a.wav", [Target("vocals")])
        assert route.call_count == 1 and no_sleep == []
        assert _messages.TASK_MAY_EXIST in caplog.messages

    def test_create_task_still_retries_server_declined(self, client, api, task_json, no_sleep):
        route = api.post("/tasks")
        route.side_effect = [httpx.Response(503), httpx.Response(200, json=task_json)]
        client.tasks.create("https://x/a.wav", [Target("vocals")])
        assert route.call_count == 2 and len(no_sleep) == 1

    @pytest.mark.parametrize(
        ("status", "attempts", "warns"),
        [(500, 1, True), (502, 1, True), (504, 1, True), (503, 3, False)],
        ids=["500", "502", "504", "503 is declined, so retried"],
    )
    def test_ambiguous_statuses_are_not_retried_and_warn(
        self, client, api, no_sleep, caplog, status, attempts, warns
    ):
        """A gateway can give up after the backend created (and billed) the task."""
        route = api.post("/tasks").respond(status)
        with pytest.raises(ServerError), caplog.at_level("WARNING", logger="audioshake"):
            client.tasks.create("https://x/a.wav", [Target("vocals")])
        assert route.call_count == attempts
        assert (_messages.TASK_MAY_EXIST in caplog.messages) is warns

    def test_other_requests_still_retry_transport_errors(self, client, api, task_json):
        route = api.get("/tasks/t1")
        route.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json=task_json)]
        assert client.tasks.get("t1").id == task_json["id"]
        assert route.call_count == 2


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "exception", "body"),
        [
            (401, AuthenticationError, {"message": "bad key"}),
            (404, NotFoundError, {"error": "not found"}),
            (400, APIError, {"message": "unknown model"}),
        ],
    )
    def test_status_maps_to_an_exception_without_retrying(
        self, client, api, no_sleep, status, exception, body
    ):
        route = api.get("/tasks/t1").respond(status, json=body, headers={"x-request-id": "req-9"})
        with pytest.raises(exception) as exc:
            client.tasks.get("t1")
        assert type(exc.value) is exception
        assert exc.value.status == status and exc.value.request_id == "req-9"
        assert next(iter(body.values())) in str(exc.value)
        assert route.call_count == 1 and no_sleep == []

    def test_a_non_json_error_body_is_not_put_in_the_message(self, client, api, no_sleep):
        page = "<html><body>nginx/1.24.0 upstream backend.test:8080 failed</body></html>"
        api.get("/tasks/t1").respond(502, html=page)
        with pytest.raises(ServerError) as exc:
            client.tasks.get("t1")
        # A proxy's error page is upstream detail: kept on the exception for
        # debugging, never in the message the CLI shows.
        assert str(exc.value) == "Failed to get task: HTTP 502 Bad Gateway"
        assert exc.value.body == page

    def test_a_json_error_message_is_collapsed_and_capped(self, client, api):
        detail = "line one\nline two " + "x" * 400
        api.get("/tasks/t1").respond(400, json={"message": detail})
        with pytest.raises(APIError) as exc:
            client.tasks.get("t1")
        message = str(exc.value)
        assert "\n" not in message and "line one line two" in message
        assert message.endswith("…") and len(message) < 400


class TestRetries:
    def test_a_declined_request_is_retried_after_the_header_delay(
        self, client, api, task_json, no_sleep
    ):
        route = api.get("/tasks/t1")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json=task_json),
        ]
        assert client.tasks.get("t1").id == task_json["id"]
        assert route.call_count == 2 and no_sleep == [3.0]

    @pytest.mark.parametrize(
        ("failure", "exception"),
        [
            (httpx.Response(502), ServerError),
            (httpx.ConnectError("refused"), APIConnectionError),
            (httpx.ReadTimeout("slow"), APITimeoutError),
        ],
        ids=["5xx", "connection error", "timeout"],
    )
    def test_retries_are_exhausted_then_raise(self, client, api, no_sleep, failure, exception):
        route = api.get("/tasks/t1")
        if isinstance(failure, Exception):
            route.mock(side_effect=failure)
        else:
            route.mock(return_value=failure)
        with pytest.raises(exception):
            client.tasks.get("t1")
        assert route.call_count == 3  # first try + 2 retries
        assert len(no_sleep) == 2

    def test_zero_retries_means_one_attempt(self, api):
        route = api.get("/tasks/t1").respond(500)
        with (
            AudioShake(api_key="k", base_url=BASE_URL, retries=0) as client,
            pytest.raises(ServerError),
        ):
            client.tasks.get("t1")
        assert route.call_count == 1


class TestWait:
    def test_polls_until_done(self, client, api, task_json, no_sleep):
        pending = {"id": "t1", "targets": [{"model": "vocals", "status": "processing"}]}
        route = api.get("/tasks/t1")
        route.side_effect = [
            httpx.Response(200, json=pending),
            httpx.Response(200, json=pending),
            httpx.Response(200, json=task_json),
        ]
        seen = []
        task = client.tasks.wait("t1", poll_interval=5, on_poll=lambda t: seen.append(t.done))
        assert task.succeeded
        assert seen == [False, False, True]
        assert no_sleep == [5, 5]

    @pytest.mark.parametrize(
        "drums",
        [
            {"model": "drums", "status": "failed", "error": "no audio"},
            {"model": "drums", "status": "error", "error": {"message": "no audio"}},
        ],
        ids=["failed", "the API's error status and JSON error"],
    )
    def test_a_failed_target_stops_polling_with_the_task_attached(
        self, client, api, no_sleep, drums
    ):
        failed = {
            "id": "t1",
            "targets": [{"model": "vocals", "status": "completed", "cost": 1}, drums],
        }
        route = api.get("/tasks/t1").respond(200, json=failed)
        with pytest.raises(TaskFailedError) as exc:
            client.tasks.wait("t1", poll_interval=1, timeout=60)
        assert "drums: no audio" in str(exc.value)
        assert exc.value.task.cost == 1
        assert route.call_count == 1 and no_sleep == []

    def test_timeout(self, client, api, monkeypatch):
        pending = {"id": "t1", "targets": [{"model": "vocals", "status": "pending"}]}
        api.get("/tasks/t1").respond(200, json=pending)
        clock = iter([0.0, 0.0, 100.0, 100.0, 100.0])
        monkeypatch.setattr("audioshake.client.time.monotonic", lambda: next(clock))
        with pytest.raises(TaskTimeoutError):
            client.tasks.wait("t1", timeout=60)


class TestList:
    def test_accepts_bare_list_or_wrapped(self, client, api, task_json):
        api.get("/tasks").respond(200, json=[task_json])
        assert [t.id for t in client.tasks.list()] == [task_json["id"]]
        api.get("/tasks").respond(200, json={"tasks": [task_json, task_json]})
        assert len(client.tasks.list()) == 2


class TestAssets:
    def test_upload_is_multipart_with_progress(self, client, api, tmp_path):
        source = tmp_path / "clip.m4a"
        source.write_bytes(b"x" * 3000)
        route = api.post("/assets").respond(
            200, json={"id": "asset_1", "name": "clip.m4a", "format": "m4a", "link": None}
        )
        seen = []
        asset = client.assets.upload(
            source, on_progress=lambda sent, total: seen.append((sent, total))
        )
        assert asset.id == "asset_1"
        request = route.calls.last.request
        assert request.headers["content-type"].startswith("multipart/form-data")
        assert b'filename="clip.m4a"' in request.content
        assert b"Content-Type: audio/mp4" in request.content
        assert seen and seen[-1] == (3000, 3000)

    def test_upload_retry_rewinds_the_file(self, client, api, tmp_path):
        source = tmp_path / "clip.wav"
        source.write_bytes(b"y" * 100)
        route = api.post("/assets")
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(200, json={"id": "asset_2"}),
        ]
        client.assets.upload(source)
        first, second = route.calls
        # Boundaries differ per request; the file bytes must be present both times.
        assert b"y" * 100 in first.request.content
        assert b"y" * 100 in second.request.content

    def test_get_asset(self, client, api):
        api.get("/assets/asset_1").respond(
            200, json={"id": "asset_1", "name": "song.m4a", "format": "mp4"}
        )
        asset = client.assets.get("asset_1")
        assert asset.name == "song.m4a" and asset.format == "mp4"


class TestDownload:
    def test_writes_files_with_the_name_prefix(self, client, api, storage, task_json, tmp_path):
        for name in ("vocals.wav", "vocals_residual.wav", "drums.wav", "drums_residual.wav"):
            storage.get(f"/{name}").respond(200, content=name.encode())
        paths = client.tasks.download(Task.from_api(task_json), tmp_path, name_prefix="song_")
        assert [p.name for p in paths] == [
            "song_vocals.wav",
            "song_vocals_residual.wav",
            "song_drums.wav",
            "song_drums_residual.wav",
        ]
        assert (tmp_path / "song_vocals.wav").read_bytes() == b"vocals.wav"
        # Presigned links must not receive the API key.
        assert "x-api-key" not in storage.calls.last.request.headers

    def test_api_names_cannot_escape_the_output_directory(self, client, storage, tmp_path):
        storage.get("/x").respond(200, content=b"pcm")
        task = Task.from_api(
            {
                "id": "t",
                "targets": [
                    {
                        "model": "vocals",
                        "status": "completed",
                        "output": [
                            {
                                "name": "../../evil",
                                "format": "wav/../../x",
                                "link": "https://storage.test/x",
                            },
                            {"name": "", "format": "", "link": "https://storage.test/x"},
                        ],
                    }
                ],
            }
        )
        out = tmp_path / "out"
        paths = client.tasks.download(task, out)
        assert all(p.parent == out for p in paths)
        assert [p.name for p in paths] == ["evil.x", "output.bin"]

    def test_skips_targets_that_did_not_complete(self, client, storage, tmp_path):
        task = Task.from_api(
            {
                "id": "t",
                "targets": [
                    {
                        "model": "vocals",
                        "status": "failed",
                        "output": [
                            {"name": "vocals", "format": "wav", "link": "https://storage.test/x"}
                        ],
                    }
                ],
            }
        )
        assert client.tasks.download(task, tmp_path) == []
        assert not storage.calls


class TestModels:
    def test_list_is_cached_and_filters_access(self, client, api, models_json):
        route = api.get("/models").respond(200, json=models_json)
        enabled = client.models.list()
        everything = client.models.list(enabled_only=False)
        assert route.call_count == 1
        assert len(everything) == 28
        assert len(enabled) < len(everything)
        assert all(m.enabled for m in enabled)

    def test_get_and_refresh(self, client, api, models_json):
        route = api.get("/models").respond(200, json=models_json)
        assert client.models.get("multi_voice").max_input_seconds == 5400
        with pytest.raises(ValidationError):
            client.models.get("not_a_model")
        client.models.refresh()
        client.models.list()
        assert route.call_count == 2


def _listed(task_id, created):
    return {"id": task_id, "createdAt": created, "targets": []}


class TestListTasks:
    def test_pages_until_the_limit(self, client, api):
        first = [_listed(f"t{i}", "2026-09-01T00:00:00Z") for i in range(100)]
        second = [_listed(f"t{i}", "2026-09-01T00:00:00Z") for i in range(100, 150)]
        route = api.get("/tasks")
        route.side_effect = [httpx.Response(200, json=first), httpx.Response(200, json=second)]
        tasks = client.tasks.list(limit=150)
        assert len(tasks) == 150
        pages = [dict(call.request.url.params) for call in route.calls]
        assert pages == [{"skip": "0", "take": "100"}, {"skip": "100", "take": "50"}]

    def test_without_a_limit_pages_until_a_short_page(self, client, api):
        full = [_listed(f"t{i}", "2026-09-01T00:00:00Z") for i in range(100)]
        route = api.get("/tasks")
        route.side_effect = [httpx.Response(200, json=full), httpx.Response(200, json=[])]
        assert len(client.tasks.list()) == 100
        assert route.call_count == 2

    def test_since_stops_at_the_first_older_task(self, client, api):
        route = api.get("/tasks").respond(
            200,
            json=[
                _listed("new", "2026-09-20T00:00:00Z"),
                _listed("old", "2026-09-01T00:00:00Z"),
                _listed("older", "2026-08-01T00:00:00Z"),
            ],
        )
        tasks = client.tasks.list(since=datetime(2026, 9, 10))  # noqa: DTZ001 - naive means UTC
        assert [t.id for t in tasks] == ["new"]
        assert route.call_count == 1

    def test_limit_must_be_positive(self, client, api):
        with pytest.raises(ValidationError):
            client.tasks.list(limit=0)


class TestUnreadableAnswers:
    def test_body_that_is_not_json(self, client, api):
        api.get("/tasks/t1").respond(200, text="<html>proxy</html>")
        with pytest.raises(UnexpectedResponseError) as caught:
            client.tasks.get("t1")
        assert caught.value.body == "<html>proxy</html>"
        assert "proxy" not in str(caught.value)

    @pytest.mark.parametrize("body", [{}, {"id": ""}, []], ids=["empty", "blank id", "a list"])
    def test_task_without_an_id(self, client, api, body):
        api.get("/tasks/t1").respond(200, json=body)
        with pytest.raises(UnexpectedResponseError):
            client.tasks.get("t1")

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/tasks", {"foo": "bar"}),
            ("/tasks", {"tasks": "nope"}),
            ("/tasks", [1, 2]),
            ("/models", {"foo": "bar"}),
        ],
        ids=["tasks: an object", "tasks: a bad wrapper", "tasks: not objects", "models: an object"],
    )
    def test_a_list_of_the_wrong_shape(self, client, api, path, body):
        api.get(path).respond(200, json=body)
        call = client.tasks.list if path == "/tasks" else client.models.list
        with pytest.raises(UnexpectedResponseError):
            call()


class TestArgumentForms:
    def test_get_and_wait_take_a_task_as_well_as_its_id(self, client, api, task_json):
        route = api.get(f"/tasks/{task_json['id']}").respond(200, json=task_json)
        task = Task.from_api(task_json)
        assert client.tasks.get(task).id == task.id
        assert client.tasks.wait(task).id == task.id
        assert route.call_count == 2

    def test_create_takes_any_iterable_of_targets(self, client, api, task_json):
        route = api.post("/tasks").respond(200, json=task_json)
        client.tasks.create("asset_1", (Target(m) for m in ("vocals", "drums")))
        sent = json.loads(route.calls.last.request.content)
        assert [t["model"] for t in sent["targets"]] == ["vocals", "drums"]
