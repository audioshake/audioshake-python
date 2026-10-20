"""The async client shares request building and parsing with the sync one. What it
owns is its request loop, wait loop, upload body, download loop and model cache."""

import json

import httpx
import pytest

from audioshake import AsyncAudioShake, ServerError, Target, Task, TaskFailedError, ValidationError

from .conftest import BASE_URL


@pytest.fixture
async def aclient(api):
    async with AsyncAudioShake(api_key="test-key", base_url=BASE_URL, retries=2) as c:
        yield c


@pytest.fixture(autouse=True)
def no_async_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("audioshake._retry.async_sleep", fake_sleep)
    return slept


async def test_create_and_wait(aclient, api, task_json, no_async_sleep):
    create = api.post("/tasks").respond(200, json=task_json)
    pending = {"id": task_json["id"], "targets": [{"model": "vocals", "status": "processing"}]}
    poll = api.get(f"/tasks/{task_json['id']}")
    poll.side_effect = [httpx.Response(200, json=pending), httpx.Response(200, json=task_json)]

    task = await aclient.tasks.create(
        "https://example.com/a.wav", [Target("vocals", residual=True)]
    )
    sent = json.loads(create.calls.last.request.content)
    assert sent["url"] == "https://example.com/a.wav"
    assert sent["targets"] == [{"model": "vocals", "formats": ["wav"], "residual": True}]

    task = await aclient.tasks.wait(task.id, poll_interval=4)
    assert task.succeeded and task.cost == 3.0
    assert no_async_sleep == [4]


async def test_failed_task_raises(aclient, api):
    api.get("/tasks/t1").respond(
        200, json={"id": "t1", "targets": [{"model": "vocals", "status": "failed", "error": "bad"}]}
    )
    with pytest.raises(TaskFailedError):
        await aclient.tasks.wait("t1")


async def test_retries_then_raises(aclient, api, no_async_sleep):
    route = api.get("/tasks/t1").respond(503)
    with pytest.raises(ServerError):
        await aclient.tasks.get("t1")
    assert route.call_count == 3
    assert len(no_async_sleep) == 2


async def test_upload_multipart(aclient, api, tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"z" * 2048)
    route = api.post("/assets").respond(200, json={"id": "asset_9", "name": "clip.wav"})
    seen = []
    asset = await aclient.assets.upload(source, on_progress=lambda s, t: seen.append((s, t)))
    assert asset.id == "asset_9"
    assert b"Content-Type: audio/wav" in route.calls.last.request.content
    assert seen[-1] == (2048, 2048)


async def test_download_retries_whole_transfer(aclient, storage, tmp_path, no_async_sleep):
    route = storage.get("/x.wav")
    route.side_effect = [httpx.Response(503), httpx.Response(200, content=b"ok")]
    task = Task.from_api(
        {
            "id": "t",
            "targets": [
                {
                    "model": "vocals",
                    "status": "completed",
                    "output": [
                        {"name": "vocals", "format": "wav", "link": "https://storage.test/x.wav"}
                    ],
                }
            ],
        }
    )
    (path,) = await aclient.tasks.download(task, tmp_path)
    assert path.read_bytes() == b"ok" and len(no_async_sleep) == 1
    # Presigned links must not receive the API key.
    assert "x-api-key" not in route.calls.last.request.headers


async def test_models_cached(aclient, api, models_json):
    route = api.get("/models").respond(200, json=models_json)
    assert (await aclient.models.get("multi_voice")).max_input_seconds == 5400
    assert len(await aclient.models.list()) == 26
    assert route.call_count == 1
    with pytest.raises(ValidationError):
        await aclient.models.get("nope")


async def test_list_tasks_pages_until_the_limit(aclient, api):
    def page(start, stop):
        return [{"id": f"t{i}", "targets": []} for i in range(start, stop)]

    route = api.get("/tasks")
    route.side_effect = [
        httpx.Response(200, json=page(0, 100)),
        httpx.Response(200, json=page(100, 120)),
    ]
    assert len(await aclient.tasks.list(limit=120)) == 120
    assert [call.request.url.params["take"] for call in route.calls] == ["100", "20"]


@pytest.mark.parametrize(
    ("statuses", "attempts"),
    [([500], 1), ([429, 200], 2)],
    ids=["ambiguous: never retried", "declined: retried"],
)
async def test_create_retries_only_declined_statuses(
    aclient, api, task_json, no_async_sleep, statuses, attempts
):
    """A 5xx after POST /tasks may follow a billed task; only a decline is safe to retry."""
    route = api.post("/tasks")
    route.side_effect = [httpx.Response(s, json=task_json if s == 200 else None) for s in statuses]
    if statuses[-1] == 200:
        await aclient.tasks.create("https://x/a.wav", [Target("vocals")])
    else:
        with pytest.raises(ServerError):
            await aclient.tasks.create("https://x/a.wav", [Target("vocals")])
    assert route.call_count == attempts
