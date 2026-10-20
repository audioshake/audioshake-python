"""AudioShake Python client.

Separate stems, isolate dialogue, transcribe and align lyrics, and more, through
the AudioShake API::

    from audioshake import AudioShake, Target

    client = AudioShake()  # reads AUDIOSHAKE_API_KEY
    asset = client.assets.upload("song.wav")
    task = client.tasks.wait(client.tasks.create(asset, [Target("vocals")]).id)
    client.tasks.download(task, "out/")

The library logs to the ``audioshake`` logger and never prints. Attach a
handler to see request and task activity.
"""

from __future__ import annotations

import logging

from ._download import ProgressCallback
from ._run import SPEAKER_COUNT_MODELS, ProgressEvent, ProgressHandler, build_targets, expand_models
from ._run import Stage as ProgressStage
from ._version import __version__
from .aio import AsyncAssets, AsyncAudioShake, AsyncModels, AsyncTasks
from .client import Assets, AudioShake, Models, Tasks
from .exceptions import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AudioShakeError,
    AuthenticationError,
    AuthError,
    NotFoundError,
    RateLimitError,
    ServerError,
    TaskFailedError,
    TaskTimeoutError,
    UnexpectedResponseError,
    ValidationError,
)
from .types import (
    Asset,
    Model,
    Output,
    RunResult,
    StrPath,
    Target,
    TargetResult,
    TargetStatus,
    Task,
)

logging.getLogger("audioshake").addHandler(logging.NullHandler())

__all__ = [
    "SPEAKER_COUNT_MODELS",
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "Asset",
    "Assets",
    "AsyncAssets",
    "AsyncAudioShake",
    "AsyncModels",
    "AsyncTasks",
    "AudioShake",
    "AudioShakeError",
    "AuthError",
    "AuthenticationError",
    "Model",
    "Models",
    "NotFoundError",
    "Output",
    "ProgressCallback",
    "ProgressEvent",
    "ProgressHandler",
    "ProgressStage",
    "RateLimitError",
    "RunResult",
    "ServerError",
    "StrPath",
    "Target",
    "TargetResult",
    "TargetStatus",
    "Task",
    "TaskFailedError",
    "TaskTimeoutError",
    "Tasks",
    "UnexpectedResponseError",
    "ValidationError",
    "__version__",
    "build_targets",
    "expand_models",
]
