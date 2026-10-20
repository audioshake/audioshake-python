"""Result output helpers: JSON for scripts, tables for people. Always stdout."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table

# Pipes and files get a generous width; a terminal gets its real size.
stdout = Console(highlight=False, width=None if sys.stdout.isatty() else 120)


def emit_json(data: Any) -> None:
    """Print one JSON document per call, newline-terminated (NDJSON when called repeatedly)."""
    sys.stdout.write(json.dumps(data, default=str) + "\n")
    sys.stdout.flush()


def emit_table(columns: Iterable[str], rows: Iterable[Iterable[Any]]) -> None:
    """A table the way most CLIs draw one: a rule under the header and no box around it."""
    table = Table(
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        padding=(0, 2),
        header_style="bold",
        border_style="dim",
    )
    for index, column in enumerate(columns):
        # The first column is the identifier; never let it wrap.
        table.add_column(column, overflow="fold", no_wrap=index == 0)
    for row in rows:
        table.add_row(*(str(cell) if cell is not None else "" for cell in row))
    stdout.print(table)


def emit_line(text: str) -> None:
    # soft_wrap: never break a path or id across lines when stdout is a pipe.
    stdout.print(text, soft_wrap=True)


def task_to_json(task: Any) -> Mapping[str, Any]:
    """The full API object, untouched, so scripts see everything the API sent."""
    return dict(task.raw)
