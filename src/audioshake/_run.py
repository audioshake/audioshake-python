"""The one-call ``run()`` flow: upload, create, wait, download."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import urlparse

from . import _messages
from .exceptions import AudioShakeError, ValidationError
from .types import Asset, Model, StrPath, Target, Task

__all__ = [
    "SPEAKER_COUNT_MODELS",
    "ProgressEvent",
    "ProgressHandler",
    "build_targets",
    "requested_formats",
]

log = logging.getLogger("audioshake.tasks")

# The API does not yet describe which models take a speaker count.
SPEAKER_COUNT_MODELS: tuple[str, ...] = ("multi_voice",)

# Used only when GET /models is unavailable, so defaults still make sense offline.
_FALLBACK_LANGUAGE_MODELS = ("transcription", "alignment")
_FALLBACK_TEXT_MODELS = ("transcription", "alignment", "music_detection", "music_identification")
_FALLBACK_TEXT_FORMATS = ("json", "txt", "srt")

Stage: TypeAlias = Literal["upload", "queued", "processing", "download", "done"]


@dataclass(frozen=True)
class ProgressEvent:
    """A step in a :meth:`AudioShake.run` call, for progress display.

    Attributes:
        stage: What is happening.
        detail: Human-readable specifics, such as the file being downloaded.
        done: Units completed so far (bytes for transfers, targets for processing).
        total: Units in total, or 0 when unknown.
        task: The task once it exists, so callers can read per-target status.
        asset: The uploaded source, on the ``upload`` event that completes the
            upload. Assets outlive the run, so a caller can run more models on
            one without uploading it again. ``None`` for a URL source.
    """

    stage: Stage
    detail: str = ""
    done: int = 0
    total: int = 0
    task: Task | None = None
    asset: Asset | None = None


# ``object``, not ``None``: the return value is ignored, so any handler may be passed.
ProgressHandler: TypeAlias = Callable[[ProgressEvent], object]


def expand_models(values: str | Iterable[str]) -> list[str]:
    """Normalise a user-supplied model list.

    ``values`` may be one string or any number of them. Each value may itself
    be comma-separated (``"vocals,drums"``), surrounding whitespace is dropped,
    and the result must not name the same model twice (it would be billed
    twice). Model ids pass through as given: the API is the source of truth for
    what exists.

    Raises:
        ValidationError: Empty input, an empty entry, or a duplicated model.
    """
    models: list[str] = []
    if isinstance(values, str):
        # A str iterates as letters; "vocals" means the one model.
        values = [values]
    for value in values:
        for raw in value.split(","):
            name = raw.strip()
            if not name:
                raise ValidationError(_messages.empty_model_name(value))
            if name in models:
                raise ValidationError(_messages.duplicate_model(name))
            models.append(name)
    if not models:
        raise ValidationError(_messages.NO_MODELS)
    return models


def build_targets(
    models: str | Target | Iterable[str | Target],
    *,
    catalog: Mapping[str, Model] | Sequence[Model] | None = None,
    format: str | Iterable[str] | None = None,
    residual: bool = False,
    language: str | None = None,
    num_speakers: int | None = None,
) -> list[Target]:
    """Turn model names into fully specified targets.

    Strings are expanded (comma lists) and given per-model defaults;
    :class:`Target` instances are passed through untouched so callers keep full
    control when they need it.

    Args:
        models: Model ids or ready-made targets; one of them or a
            sequence. A string may be a comma list (``"vocals,drums"``).
        catalog: The models from ``GET /models``: the list ``models.list()``
            returns, or a ``{model_id: Model}`` mapping. Supplies the default
            output format and which models accept a language. When ``None`` a
            small built-in fallback is used.
        format: Output formats: one, a comma list, or a sequence. Each model
            produces the requested formats it lists (so ``"srt,mp3"`` gives
            ``alignment`` its SRT and the stems their MP3, and ``"json,srt"``
            gives ``alignment`` both files from one billed target). A model that
            offers none of them keeps its own default (WAV for audio, JSON for
            text); a format no selected model offers is an error. ``None`` means
            every model's default.
        residual: Also return everything the models did not capture.
        language: Applied to models that accept one.
        num_speakers: Applied to models that accept one (``multi_voice``).
    """
    catalog = _catalog_by_id(catalog)
    if isinstance(models, str | Target):
        # A str is itself a Sequence[str], so without this a bare "vocals"
        # would be read as one model per letter.
        models = [models]
    # Read twice below, so a generator must be drained first or the second pass
    # would silently see nothing.
    models = list(models)
    names = [m for m in models if isinstance(m, str)]
    ready = [m for m in models if isinstance(m, Target)]
    requested = requested_formats(format)
    targets: list[Target] = []
    expanded = expand_models(names) if names else []
    honoured: set[str] = set()
    if catalog is not None:
        _reject_misspelled_models([*expanded, *(t.model for t in ready)], catalog)

    for name in expanded:
        model = catalog.get(name) if catalog is not None else None
        if model is not None:
            _validate_language(model, language)
        chosen = _formats_for(name, model, requested)
        honoured.update(f for f in chosen if f in requested)
        targets.append(
            Target(
                model=name,
                formats=chosen,
                residual=residual,
                language=language if _accepts_language(name, model) else None,
                num_speakers=num_speakers if name in SPEAKER_COUNT_MODELS else None,
            )
        )
    if catalog is not None:
        for unused in (f for f in requested if f not in honoured):
            _reject_format(unused, expanded, catalog)
    targets.extend(ready)
    if not targets:
        raise ValidationError(_messages.NO_MODELS)
    _reject_duplicate_models(targets)
    return targets


def _reject_misspelled_models(names: Sequence[str], catalog: Mapping[str, Model]) -> None:
    """Stop a typo of a listed model before the upload it would waste.

    The catalog cannot be the last word on which ids exist: pre-release models
    are callable without being listed, so an unknown id is passed through for
    the API to judge. Only a certain misspelling is stopped (see
    :func:`_listed_spelling_of`). The id is never rewritten silently; the
    caller is told what was probably meant.
    """
    for name in names:
        if name in catalog:
            continue
        suggestion = _listed_spelling_of(name, catalog)
        if suggestion is not None:
            raise ValidationError(_messages.model_suggestion(name, suggestion))


def _listed_spelling_of(name: str, catalog: Mapping[str, Model]) -> str | None:
    """The listed id that ``name`` is a certain misspelling of, or None.

    Three transformations are certain. Lowercase with hyphens and spaces as
    underscores: every listed id is spelled that way, while CLI flags are
    hyphenated, so ``multi-voice`` for ``multi_voice`` is the typo the command
    line invites. The same words in another order, ``lead_vocals`` for
    ``vocals_lead``. And a word in the singular, ``lead_vocal`` or ``drum``:
    no listed id differs from another only by a trailing s. The last two are
    accepted only when exactly one listed id matches. Anything looser, such as
    edit distance, would stop a pre-release id like ``music_removal_beta``
    beside ``music_removal``, so that is left to the API.
    """
    normalized = re.sub(r"[-\s]+", "_", name.strip().lower())
    if normalized in catalog:
        return normalized
    words = _word_key(normalized)
    matches = [listed for listed in catalog if _word_key(listed) == words]
    return matches[0] if len(matches) == 1 else None


def _word_key(model_id: str) -> list[str]:
    """An id's words, sorted and with a trailing s dropped, for comparing spellings."""
    return sorted(w[:-1] if len(w) > 2 and w.endswith("s") else w for w in model_id.split("_"))


def _reject_duplicate_models(targets: Sequence[Target]) -> None:
    """Each model runs once per source, however it was named.

    The API refuses a task that lists a model twice, but only after the source
    has been uploaded. Checking the final list catches every combination
    first: two names, a name and a ``Target``, or two ``Target`` objects.
    """
    seen: set[str] = set()
    for target in targets:
        if target.model in seen:
            raise ValidationError(_messages.duplicate_model(target.model))
        seen.add(target.model)


def _catalog_by_id(
    catalog: Mapping[str, Model] | Sequence[Model] | None,
) -> Mapping[str, Model] | None:
    """The catalog keyed by model id, whichever form the caller passed.

    ``models.list()`` returns a list, and that is the natural thing to hand
    over; a mapping is what the lookups below want.
    """
    if catalog is None or isinstance(catalog, Mapping):
        return catalog
    return {model.id: model for model in catalog}


def _validate_language(model: Model, language: str | None) -> None:
    """Reject a language the catalog says this model cannot honour, before anything is billed."""
    if language and model.supported_languages and language not in model.supported_languages:
        raise ValidationError(
            _messages.unsupported_language(model.id, language, model.supported_languages)
        )


def _reject_format(requested: str, names: Sequence[str], catalog: Mapping[str, Model]) -> None:
    """Fail when nothing in the run can produce the requested format."""
    known = [catalog[name] for name in names if name in catalog]
    if not known:
        return
    if len(known) == 1:
        raise ValidationError(
            _messages.unsupported_format(known[0].id, requested, known[0].output_formats)
        )
    raise ValidationError(
        _messages.no_model_outputs_format(
            requested, [(model.id, model.output_formats) for model in known]
        )
    )


def requested_formats(format: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalise the requested formats: comma lists split, lowercased, deduplicated."""
    if format is None:
        return ()
    values = [format] if isinstance(format, str) else list(format)
    wanted: list[str] = []
    for value in values:
        for part in value.split(","):
            cleaned = part.strip().lower()
            if cleaned and cleaned not in wanted:
                wanted.append(cleaned)
    return tuple(wanted)


def _formats_for(name: str, model: Model | None, requested: Sequence[str]) -> tuple[str, ...]:
    """The formats this model will produce: those it offers, else its default."""
    if model is not None:
        return tuple(f for f in requested if f in model.output_formats) or (model.default_format,)
    # Offline fallback: text models take the text formats, anything else the request
    # as given, so a model or format newer than this SDK still reaches the API.
    if name in _FALLBACK_TEXT_MODELS:
        return tuple(f for f in requested if f in _FALLBACK_TEXT_FORMATS) or ("json",)
    return tuple(requested) or ("wav",)


def _accepts_language(name: str, model: Model | None) -> bool:
    if model is not None:
        return bool(model.supported_languages)
    return name in _FALLBACK_LANGUAGE_MODELS


def is_url(source: StrPath) -> bool:
    return isinstance(source, str) and source.startswith(("http://", "https://"))


ASSET_PREFIX = "asset:"
"""Marks a source that names an asset uploaded earlier, as ``asset:<id>``."""


def is_asset_ref(source: StrPath) -> bool:
    """True for an ``asset:<id>`` source."""
    return isinstance(source, str) and source.startswith(ASSET_PREFIX)


def asset_id_from_ref(source: str) -> str:
    """The id in an ``asset:<id>`` source; empty when none was given."""
    return source[len(ASSET_PREFIX) :].strip()


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_name(value: str, fallback: str) -> str:
    """Reduce a name from outside (the API, a URL) to a single safe path component.

    Directory separators and parent references are removed, characters outside
    a conservative set become ``_``, and an empty or dot-only result falls back.
    """
    base = Path(value.replace("\\", "/")).name if value else ""
    base = _SAFE_CHARS.sub("_", base).strip(" .")
    return base or fallback


def source_stem(source: StrPath) -> str:
    """A file-system friendly stem for a source, used to name its output directory."""
    name = Path(urlparse(str(source)).path).name if is_url(source) else Path(source).name
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return safe_name(stem, "url" if is_url(source) else "source")


def catalog_or_none(fetch: Callable[[], Sequence[Model]]) -> dict[str, Model] | None:
    """Fetch the model catalog, or return None if the API cannot supply it right now."""
    try:
        return {model.id: model for model in fetch()}
    except AudioShakeError as e:
        log.warning(_messages.CATALOG_UNAVAILABLE, e)
        return None
