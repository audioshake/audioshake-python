"""``audioshake tasks``: inspect and download existing tasks."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.markup import escape

from audioshake import AudioShake, AudioShakeError, Task
from audioshake._run import source_stem

from . import _messages
from ._format import credits_or_none, format_error
from ._logging import logger
from ._output import emit_json, emit_line, emit_table, task_to_json
from ._settings import Settings

DEFAULT_LIST_LIMIT = 20


@click.group()
def tasks() -> None:
    """List, inspect and download tasks."""


@tasks.command("list")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=DEFAULT_LIST_LIMIT,
    show_default=True,
    help="Show at most N tasks, newest first",
)
@click.option(
    "--since",
    metavar="WHEN",
    help="Only tasks created after WHEN: an ISO date/time, or a duration like 24h, 7d, 30m",
)
@click.pass_obj
def list_tasks(settings: Settings, limit: int, since: str | None) -> None:
    """List this account's tasks, newest first."""
    cutoff = parse_since(since) if since else None
    with settings.client() as client:
        items = client.tasks.list(limit=limit, since=cutoff)
    if settings.json:
        for task in items:
            emit_json(task_to_json(task))
        return
    emit_table(
        ["Task", "Created", "Models", "Status", "Cost"],
        [
            (
                task.id,
                _created_str(task),
                ", ".join(t.model for t in task.targets),
                _status(task),
                credits_or_none(task.cost),
            )
            for task in items
        ],
    )


@tasks.command("get")
@click.argument("task_id")
@click.pass_obj
def get_task(settings: Settings, task_id: str) -> None:
    """Show one task, with per-model status and outputs."""
    with settings.client() as client:
        task = client.tasks.get(task_id)
        # The raw object under --json already carries assetId; only the human
        # view spends a request on the file's name.
        asset_id, asset_name = ("", "") if settings.json else _asset_of(client, task)
    if settings.json:
        emit_json(task_to_json(task))
        return
    for target in task.targets:
        if target.error:
            logger.debug("task %s target %s error: %s", task.id, target.model, target.error)
    header = f"[bold]{task.id}[/bold]  {_status(task)}"
    credits = credits_or_none(task.cost)
    emit_line(f"{header}  {credits}" if credits is not None else header)
    if asset_id:
        # The id is what `run asset:ID` takes to run more models on this upload.
        shown = f"asset {asset_id}"
        emit_line(f"{shown}  ({escape(asset_name)})" if asset_name else shown)
    emit_table(
        ["Model", "Status", "Cost", "Outputs", "Error"],
        [
            (
                t.model,
                t.status,
                credits_or_none(t.cost),
                ", ".join(f"{o.name}.{o.format}" for o in t.outputs),
                format_error(t.error) if t.error else "",
            )
            for t in task.targets
        ],
    )


@tasks.command("download")
@click.argument("task_id")
@click.option(
    "-o",
    "--output",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("audioshake-output"),
    show_default=True,
    help="Directory to write into",
)
@click.pass_obj
def download_task(settings: Settings, task_id: str, output_dir: Path) -> None:
    """Download the outputs of a finished task."""
    with settings.client() as client:
        task = client.tasks.get(task_id)
        folder = _source_folder(client, task)
        paths = client.tasks.download(task, output_dir / folder)
    if settings.json:
        emit_json({"task_id": task.id, "files": [str(p) for p in paths]})
        return
    for path in paths:
        emit_line(str(path))
    if not paths:
        emit_line(_messages.NO_DOWNLOADABLE_OUTPUTS)


_DURATION = re.compile(r"^(\d+)\s*([smhdw])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_since(value: str) -> datetime:
    """``24h`` / ``7d`` style durations, or an ISO date or date-time (UTC if naive)."""
    match = _DURATION.match(value.strip().lower())
    if match:
        amount, unit = match.groups()
        try:
            return datetime.now(timezone.utc) - timedelta(**{_UNITS[unit]: int(amount)})
        except OverflowError:
            raise click.BadParameter(
                _messages.since_too_far_back(value), param_hint="--since"
            ) from None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise click.BadParameter(
            _messages.since_not_a_duration(value), param_hint="--since"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _asset_of(client: AudioShake, task: Task) -> tuple[str, str]:
    """The task's asset id and the name of the file it was uploaded from.

    Either may be empty: a task made from a URL has no asset, and an asset that
    has expired or cannot be read gives the id with no name.
    """
    asset_id = task.raw.get("assetId")
    if not isinstance(asset_id, str) or not asset_id:
        return "", ""
    try:
        return asset_id, client.assets.get(asset_id).name
    except AudioShakeError as e:
        logger.debug("asset %s of task %s could not be looked up: %s", asset_id, task.id, e)
        return asset_id, ""


def _source_folder(client: AudioShake, task: Task) -> str:
    """Name the per-source folder like `run` does: after the original file.

    Falls back to the task id when the asset cannot be looked up.
    """
    _, name = _asset_of(client, task)
    return source_stem(name) if name else task.id


def _created_str(task: Task) -> str:
    created = task.created_at
    return created.strftime("%Y-%m-%d %H:%M") if created else ""


def _status(task: Task) -> str:
    if not task.done:
        return "processing"
    return "failed" if task.failed else "completed"
