"""Typed views of the objects the API sends and receives.

Response types are frozen dataclasses built with ``from_api``. Parsing is
tolerant: fields the API adds later are kept in ``raw`` rather than rejected,
so an SDK upgrade is never required just to keep reading responses.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeAlias

StrPath: TypeAlias = str | os.PathLike[str]
"""Anything ``pathlib.Path`` accepts: a string or any path-like object."""

__all__ = [
    "Asset",
    "Model",
    "Output",
    "RunResult",
    "Target",
    "TargetResult",
    "TargetStatus",
    "Task",
]

TargetStatus = Literal["pending", "processing", "completed", "failed"]

# The API's own vocabulary, mapped onto ours. Anything unknown is treated as
# still running (and logged once) so a new API state cannot be mistaken for
# success; the caller's timeout bounds the wait.
_STATUS_ALIASES: dict[str, TargetStatus] = {
    "pending": "pending",
    "created": "pending",
    "queued": "pending",
    "processing": "processing",
    "running": "processing",
    "completed": "completed",
    "complete": "completed",
    "succeeded": "completed",
    "failed": "failed",
    "error": "failed",
    "errored": "failed",
    "cancelled": "failed",
    "canceled": "failed",
}
_unknown_statuses_seen: set[str] = set()


def normalize_status(value: Any) -> TargetStatus:
    raw = str(value or "pending").lower()
    status = _STATUS_ALIASES.get(raw)
    if status is None:
        if raw not in _unknown_statuses_seen:
            _unknown_statuses_seen.add(raw)
            logging.getLogger("audioshake.tasks").warning(
                "unknown target status %r from the API; treating it as still processing", raw
            )
        return "processing"
    return status


# Formats the API can produce as audio or video, used to pick sensible defaults.
MEDIA_FORMATS = frozenset({"wav", "aiff", "aif", "flac", "mp3", "mp4", "aac", "m4a", "mov"})


@dataclass(frozen=True)
class Target:
    """One model to run on a source, with its options.

    Attributes:
        model: Model id, for example ``"vocals"``. Passed through to the API,
            which is the source of truth for which models exist.
        formats: Output formats to produce. Defaults to WAV.
        residual: Also return everything the model did not capture.
        language: Language code for models that accept one (transcription, alignment).
        num_speakers: Speaker count for models that accept one (multi_voice).
    """

    model: str
    formats: tuple[str, ...] = ("wav",)
    residual: bool = False
    language: str | None = None
    num_speakers: int | None = None

    def to_api(self) -> dict[str, Any]:
        """Serialize to the API's target shape (camelCase keys)."""
        target: dict[str, Any] = {"model": self.model, "formats": list(self.formats)}
        if self.residual:
            target["residual"] = True
        if self.language:
            target["language"] = self.language
        if self.num_speakers is not None:
            target["numSpeakers"] = self.num_speakers
        return target


@dataclass(frozen=True)
class Output:
    """A single downloadable result file produced by a target."""

    name: str
    format: str
    link: str

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Output:
        """Parse one output from an API response."""
        return cls(
            name=str(data.get("name", "")),
            format=str(data.get("format", "")),
            link=str(data.get("link", "")),
        )


@dataclass(frozen=True)
class TargetResult:
    """The state of one target within a task."""

    model: str
    status: TargetStatus
    cost: float = 0.0
    duration: float | None = None
    error: str | None = None
    outputs: tuple[Output, ...] = ()

    @property
    def done(self) -> bool:
        """True when the target has completed or failed."""
        return self.status in ("completed", "failed")

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> TargetResult:
        """Parse one target from an API response."""
        status = normalize_status(data.get("status"))
        outputs = tuple(
            Output.from_api(item)
            for item in data.get("output") or ()
            if isinstance(item, Mapping) and item.get("link")
        )
        return cls(
            model=str(data.get("model", "")),
            status=status,
            cost=_as_float(data.get("cost")) or 0.0,
            duration=_as_float(data.get("duration")),
            error=_error_text(data.get("error")) or _error_text(data.get("message")),
            outputs=outputs,
        )


@dataclass(frozen=True)
class Task:
    """A processing task: one source, one or more targets.

    Attributes:
        id: Task id.
        targets: Per-model state and outputs.
        raw: The full API response, for fields this SDK does not model.
    """

    id: str
    targets: tuple[TargetResult, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def done(self) -> bool:
        """True when every target has completed or failed."""
        return all(target.done for target in self.targets)

    @property
    def failed(self) -> tuple[TargetResult, ...]:
        """The targets that failed."""
        return tuple(target for target in self.targets if target.status == "failed")

    @property
    def succeeded(self) -> bool:
        """True when the task is done and nothing failed."""
        return self.done and not self.failed

    @property
    def cost(self) -> float:
        """Credits charged so far, summed over the targets."""
        return sum(target.cost for target in self.targets)

    @property
    def outputs(self) -> tuple[Output, ...]:
        """Every target's outputs, in API order."""
        return tuple(output for target in self.targets for output in target.outputs)

    @property
    def created_at(self) -> datetime | None:
        """When the task was created (UTC), or None if the API did not say."""
        raw = self.raw.get("createdAt")
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Task:
        """Parse a task from an API response."""
        targets = tuple(
            TargetResult.from_api(item)
            for item in data.get("targets") or ()
            if isinstance(item, Mapping)
        )
        return cls(id=str(data.get("id", "")), targets=targets, raw=dict(data))


@dataclass(frozen=True)
class Asset:
    """An uploaded source file."""

    id: str
    name: str = ""
    format: str = ""
    link: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Asset:
        """Parse an asset from an API response."""
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            format=str(data.get("format", "")),
            link=_as_optional_str(data.get("link")),
            raw=dict(data),
        )


@dataclass(frozen=True)
class Model:
    """A model this account can run, as described by ``GET /models``."""

    id: str
    name: str = ""
    category: str = ""
    description: str = ""
    credits_per_minute: float | None = None
    output_formats: tuple[str, ...] = ()
    input_formats: tuple[str, ...] = ()
    supported_languages: tuple[str, ...] = ()
    max_input_seconds: int | None = None
    access: str = "enabled"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def enabled(self) -> bool:
        """True if this account can run the model without requesting access."""
        return self.access == "enabled"

    @property
    def default_format(self) -> str:
        """The format to use when the caller did not choose one.

        Audio models default to WAV when they offer it; text models (whose output
        formats are things like ``json``) default to their first listed format.
        """
        if "wav" in self.output_formats:
            return "wav"
        return self.output_formats[0] if self.output_formats else "wav"

    @property
    def produces_media(self) -> bool:
        """True if the model outputs audio or video rather than only text."""
        return any(fmt in MEDIA_FORMATS for fmt in self.output_formats)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Model:
        """Parse a model from ``GET /models``."""
        limits = data.get("limits") or {}
        max_seconds = limits.get("maxInputDurationSeconds") if isinstance(limits, Mapping) else None
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            category=str(data.get("category", "")),
            description=str(data.get("description", "")),
            credits_per_minute=_as_float(data.get("creditsPerMinute")),
            output_formats=_as_str_tuple(data.get("outputFormats")),
            input_formats=_media_input_formats(data.get("inputs")),
            supported_languages=_as_str_tuple(data.get("supportedLanguages")),
            max_input_seconds=int(max_seconds) if isinstance(max_seconds, int | float) else None,
            access=str(data.get("access", "enabled")),
            raw=dict(data),
        )


@dataclass(frozen=True)
class RunResult:
    """What :meth:`AudioShake.run` returns: the finished task and any downloaded files.

    A task where some targets failed still returns, with the completed targets'
    files; check :attr:`failed` before assuming every model ran.

    Attributes:
        task: The finished task.
        files: The outputs written, in API order.
        asset: The uploaded source, or ``None`` for a URL source. An asset
            outlives the run, so more models can be run on it later without
            uploading the file again.
    """

    task: Task
    files: tuple[Path, ...] = ()
    asset: Asset | None = None

    @property
    def failed(self) -> tuple[TargetResult, ...]:
        """Targets that failed. Empty when every requested model completed."""
        return self.task.failed


def _error_text(value: Any) -> str | None:
    """The API stores a target's error as JSON; pull out something readable."""
    if value is None or value == "":
        return None
    if isinstance(value, Mapping):
        for key in ("message", "error", "detail", "reason", "code"):
            inner = value.get(key)
            if isinstance(inner, str) and inner:
                return inner
        return str(dict(value))
    return str(value)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(str(item) for item in value)
    return ()


def _media_input_formats(inputs: Any) -> tuple[str, ...]:
    """Flatten the ``inputs[].formats`` map of the media input into one tuple."""
    if not isinstance(inputs, Sequence):
        return ()
    for item in inputs:
        if not isinstance(item, Mapping) or item.get("id") not in (None, "media"):
            continue
        formats = item.get("formats")
        if isinstance(formats, Mapping):
            flat: list[str] = []
            for group in formats.values():
                flat.extend(_as_str_tuple(group))
            return tuple(flat)
    return ()
