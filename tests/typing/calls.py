"""Calls the library must accept, checked by mypy and never run.

mypy checks the package, but every type in a public signature is only ever
exercised from calling code, so these calls stand in for a user's script. Each
block pins one public signature: the values it must accept, and the forms a
caller would naturally pass. ``pytest`` skips the file (its name is not
``test_*``); ``uv run mypy`` includes it.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

import httpx

from audioshake import (
    AsyncAudioShake,
    AudioShake,
    ProgressCallback,
    ProgressEvent,
    ProgressHandler,
    ProgressStage,
    Target,
    Task,
    auth,
    build_targets,
)


def _handler_with_a_return_value(event: ProgressEvent) -> ProgressStage:
    return event.stage


def _bytes_handler(sent: int, total: int) -> float:
    return sent / (total or 1)


def _poll_handler(task: Task) -> bool:
    return task.done


def _url_handler(url: str) -> int:
    return len(url)


def _one_model_as_a_string(client: AudioShake) -> None:
    client.run("song.wav", "vocals")
    client.run("song.wav", "vocals,drums")
    client.run("song.wav", Target("vocals"))
    build_targets("vocals")


def _callbacks_may_return_a_value(client: AudioShake, task: Task) -> None:
    handler: ProgressHandler = _handler_with_a_return_value
    client.run("song.wav", ["vocals"], on_progress=handler)
    client.run("song.wav", ["vocals"], on_progress=_handler_with_a_return_value)
    bytes_handler: ProgressCallback = _bytes_handler
    client.assets.upload("song.wav", on_progress=bytes_handler)
    client.tasks.wait(task.id, on_poll=_poll_handler)
    client.tasks.download(task, "out", on_progress=lambda output, done, total: output.name)
    auth.login(on_url=_url_handler)


def _any_path_like(client: AudioShake) -> None:
    client.run(PurePosixPath("song.wav"), ["vocals"], output_dir=PurePosixPath("out"))
    client.assets.upload(PurePosixPath("song.wav"))
    auth.TokenStore(PurePosixPath("/tmp/audioshake"))


def _token_sets(payload: types.MappingProxyType[str, Any]) -> None:
    tokens = auth.TokenSet.from_dict(payload)
    auth.TokenSet.from_token_response(payload, tokens.refresh_token)
    tokens.is_expired(skew=0.5)


def _timeouts() -> None:
    AudioShake("key", timeout=None)
    AudioShake("key", timeout=httpx.Timeout(10.0))
    AsyncAudioShake("key", timeout=None)


def _formats_as_any_iterable(client: AudioShake) -> None:
    client.run("song.wav", ["vocals"], format={"mp3", "wav"})
    client.run("song.wav", ["alignment"], format=(f for f in ("json", "srt")))
    build_targets(["vocals"], format={"mp3"})


def _models_and_targets_as_any_iterable(client: AudioShake) -> None:
    models: Iterable[str] = (m for m in ("vocals", "drums"))
    client.run("song.wav", models)
    targets: Iterable[Target] = (Target(m) for m in ("vocals", "drums"))
    client.tasks.create("asset_1", targets)


def _a_task_where_an_id_is_expected(client: AudioShake, task: Task) -> None:
    client.tasks.get(task)
    client.tasks.wait(task, timeout=60)
    client.tasks.get(task_id=task.id)


async def _async_twins(client: AsyncAudioShake, task: Task) -> None:
    await client.run(PurePosixPath("song.wav"), "vocals", on_progress=_handler_with_a_return_value)
    await client.tasks.wait(task, on_poll=_poll_handler)
    await client.tasks.create("asset_1", (Target(m) for m in ("vocals",)))
    await client.assets.upload("song.wav", on_progress=_bytes_handler)


def _run_everything() -> None:  # never called; keeps every helper referenced
    asyncio.run(_async_twins(AsyncAudioShake("key"), Task(id="t")))
