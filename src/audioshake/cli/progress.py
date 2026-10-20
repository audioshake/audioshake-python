"""Live, graph-style progress display for the AudioShake CLI.

The batch runner reports what it is doing by mutating a tree of :class:`Node`
objects, which :class:`ProgressTracker` repaints in place in an interactive
terminal. Outside a terminal (or under ``--plain``) the tree is never drawn and
the library's log lines carry the progress instead, so piped output and CI
logs stay plain.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

    from rich.console import Console, ConsoleOptions, RenderResult
    from rich.live import Live
    from typing_extensions import Self

from ._format import credits_or_none, format_duration
from ._logging import logger

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

REFRESH_PER_SECOND = 10

# Rows reserved for the header panel, the footer bar and breathing room.
CHROME_ROWS = 8

# Narrowest the trailing detail column ever gets, so short details such as
# "processing" stay readable even on a cramped terminal.
MIN_DETAIL_WIDTH = 16


class Status(Enum):
    """Lifecycle state of a node in the progress tree."""

    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    # Some of a source's models completed and were downloaded, others failed.
    PARTIAL = "partial"

    @property
    def finished(self) -> bool:
        return self in (Status.DONE, Status.FAILED, Status.PARTIAL)


# One accent for the chrome (title, border, spinner, bar), the terminal's own
# cyan so it follows the user's theme. Colour otherwise means state: green done,
# red failed, yellow partly done, dim not started. Nothing else is coloured, so
# a coloured thing is always a status.
ACCENT = "cyan"

# symbol, rich style
_STATUS_LOOK = {
    Status.PENDING: ("·", "dim"),
    Status.ACTIVE: (None, ACCENT),
    Status.DONE: ("✔", "green"),
    Status.FAILED: ("✘", "red bold"),
    Status.PARTIAL: ("◐", "yellow bold"),
}


def truncate_middle(text: str, width: int) -> str:
    """Shorten `text` to `width` characters, eliding the middle.

    Used for filenames, where both ends carry identifying information.
    """
    if width <= 1 or len(text) <= width:
        return text
    keep = width - 1
    head = (keep + 1) // 2
    tail = keep - head
    return f"{text[:head]}…{text[len(text) - tail :]}" if tail else f"{text[:head]}…"


def truncate_end(text: str, width: int) -> str:
    """Shorten `text` to `width` characters, keeping the front."""
    if width <= 1 or len(text) <= width:
        return text
    return f"{text[: width - 1]}…"


# A guide string and the node it leads to; None stands for a trimmed-rows marker.
_Row = tuple[str, "Node | None"]


class Node:
    """One row in the progress tree (a file or one of its models)."""

    label: str
    parent: Node | None
    children: list[Node]
    status: Status
    detail: str
    started_at: float | None
    ended_at: float | None

    def __init__(self, tracker: ProgressTracker, label: str, parent: Node | None = None) -> None:
        self._tracker = tracker
        self.label = label
        self.parent = parent
        self.children: list[Node] = []
        self.status = Status.PENDING
        self.detail = ""
        self.started_at: float | None = None
        self.ended_at: float | None = None

    # -- structure ---------------------------------------------------------

    def child(self, label: str) -> Node:
        """Create a nested node under this one."""
        node = Node(self._tracker, label, parent=self)
        with self._tracker.lock:
            self.children.append(node)
        return node

    @property
    def path(self) -> str:
        parts: list[str] = []
        node: Node | None = self
        while node is not None:
            parts.append(node.label)
            node = node.parent
        return " › ".join(reversed(parts))  # noqa: RUF001 - a breadcrumb glyph, on purpose

    # -- state transitions -------------------------------------------------

    def start(self, detail: str = "") -> Node:
        return self._transition(Status.ACTIVE, detail)

    def update(self, detail: str) -> Node:
        """Refresh the trailing detail without changing status."""
        if self.status.finished:
            return self  # a settled node keeps the reason it settled with
        return self._transition(Status.ACTIVE, detail)

    def succeed(self, detail: str = "") -> Node:
        return self._transition(Status.DONE, detail)

    def fail(self, detail: str = "") -> Node:
        return self._transition(Status.FAILED, detail)

    def partial(self, detail: str = "") -> Node:
        return self._transition(Status.PARTIAL, detail)

    def _transition(self, status: Status, detail: str) -> Node:
        with self._tracker.lock:
            self.status = status
            self.detail = detail
            if status is Status.ACTIVE and self.started_at is None:
                self.started_at = time.monotonic()
            if status.finished and self.ended_at is None:
                self.ended_at = time.monotonic()
        return self

    # -- rendering helpers -------------------------------------------------

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.ended_at or time.monotonic()) - self.started_at

    def display_detail(self) -> str:
        if self.status is Status.ACTIVE and self.started_at is not None:
            elapsed = format_duration(self.elapsed)
            return f"{self.detail} · {elapsed}" if self.detail else elapsed
        return self.detail

    def has_failure(self) -> bool:
        return self.status is Status.FAILED or any(c.has_failure() for c in self.children)


class ProgressTracker:
    """Owns the progress tree and paints it, live or as plain log lines."""

    title: str
    subtitle: str
    lock: threading.RLock
    roots: list[Node]
    credits: float
    total: int | None
    start_time: float

    def __init__(self, title: str, subtitle: str = "", live: bool = True) -> None:
        self.title = title
        self.subtitle = subtitle
        self.lock = threading.RLock()
        self.roots: list[Node] = []
        self.credits = 0.0
        self.total: int | None = None
        self.start_time = time.monotonic()
        self._live_requested = live
        self._detail_cells = MIN_DETAIL_WIDTH
        self._live: Live | None = None
        self._console: Console | None = None
        self._log_handlers: list[logging.Handler] = []

    # -- public API --------------------------------------------------------

    def add_item(self, label: str) -> Node:
        node = Node(self, label)
        with self.lock:
            self.roots.append(node)
        return node

    def set_total(self, total: int) -> None:
        with self.lock:
            self.total = total

    def add_credits(self, amount: float) -> None:
        with self.lock:
            self.credits += amount

    @property
    def live_active(self) -> bool:
        return self._live is not None

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> Self:
        if not self._live_requested:
            return self
        try:
            from rich.console import Console
            from rich.live import Live
        except ImportError:  # pragma: no cover - rich is a hard dependency
            return self

        console = Console()
        if not console.is_terminal:
            return self

        self._console = console
        self._live = Live(
            _View(self),
            console=console,
            refresh_per_second=REFRESH_PER_SECOND,
            vertical_overflow="crop",
        )
        self._live.start()
        self._quiet_logger()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._live is not None:
            self._live.refresh()
            self._live.stop()
            self._live = None
        self._restore_logger()
        self._console = None

    def _quiet_logger(self) -> None:
        """Route logging through the live console and mute routine INFO chatter."""
        self._log_handlers = list(logger.handlers)
        handler = _ConsoleHandler(self._console)
        handler.setLevel(logging.WARNING)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.handlers = [handler]

    def _restore_logger(self) -> None:
        if self._log_handlers:
            logger.handlers = self._log_handlers
            self._log_handlers = []

    # -- rendering ---------------------------------------------------------

    def render(self, console: Console, options: ConsoleOptions) -> RenderResult:
        from rich.console import Group
        from rich.panel import Panel
        from rich.progress_bar import ProgressBar
        from rich.table import Table
        from rich.text import Text

        width = max(options.max_width, 40)
        height = console.size.height or 24
        frame = SPINNER_FRAMES[int(time.monotonic() * REFRESH_PER_SECOND) % len(SPINNER_FRAMES)]

        with self.lock:
            rows = self._collect_rows(max(4, height - CHROME_ROWS))
            detail_width = self._detail_column(rows, width)
            done = sum(1 for root in self.roots if root.status.finished)
            total = self.total if self.total is not None else len(self.roots)
            credits = self.credits

        label_width = max(10, width - detail_width - 4)

        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(width=2, no_wrap=True)
        table.add_column(ratio=1, no_wrap=True, overflow="crop")
        table.add_column(width=detail_width, justify="right", no_wrap=True, overflow="crop")

        for guide, node in rows:
            if node is None:  # placeholder row standing in for hidden items
                table.add_row("", Text(guide, style="dim italic"), "")
                continue

            symbol, style = _STATUS_LOOK[node.status]
            is_root = node.parent is None
            label = truncate_middle(node.label, max(8, label_width - len(guide)))
            label_text = Text(guide, style="dim")
            label_text.append(
                label, style="bold" if is_root else "red" if node.status is Status.FAILED else ""
            )

            table.add_row(
                Text(symbol if symbol is not None else frame, style=style),
                label_text,
                Text(
                    truncate_end(node.display_detail(), detail_width),
                    style="red" if node.status is Status.FAILED else "dim",
                ),
            )

        header = Panel(
            # One line: a long output path is clipped, not wrapped into extra
            # header rows that push the file rows off a small screen.
            Text(self.subtitle, style="dim", no_wrap=True, overflow="ellipsis"),
            title=Text(self.title, style=f"bold {ACCENT}"),
            title_align="left",
            border_style=ACCENT,
            padding=(0, 1),
        )

        elapsed = format_duration(time.monotonic() - self.start_time)
        parts: list[tuple[str, str]] = [(f"{done}/{total} files", "bold")]
        charge = credits_or_none(credits)
        if charge is not None:
            parts += [("  ·  ", "dim"), (charge, "")]
        parts += [("  ·  ", "dim"), (elapsed, "dim")]
        summary = Text.assemble(*parts)
        bar = ProgressBar(
            total=max(total, 1),
            completed=done,
            width=max(10, width - summary.cell_len - 4),
            style="dim",
            complete_style=ACCENT,
            finished_style="green",
        )
        footer = Table.grid(padding=(0, 2), expand=True)
        footer.add_column(ratio=1)
        footer.add_column(justify="right", no_wrap=True)
        # One file has nothing to count up: its row already shows the stage.
        footer.add_row(bar if total > 1 else Text(""), summary)

        yield Group(header, table, Text(""), footer)

    def _detail_column(self, rows: list[_Row], width: int) -> int:
        """Width for the trailing detail column: what the text needs, up to half
        the screen.

        A roomy terminal shows details such as ``task 01J… · 2 running`` in full
        instead of clipping them to a fixed guess. The column only ever grows
        during a run: resizing it every time the longest detail changes would
        shuffle the labels sideways between frames.
        """
        natural = max(
            (len(node.display_detail()) for _, node in rows if node is not None), default=0
        )
        self._detail_cells = max(self._detail_cells, natural)
        return min(self._detail_cells, max(MIN_DETAIL_WIDTH, width // 2))

    def _collect_rows(self, max_rows: int) -> list[_Row]:
        """Flatten the tree into (guide, node) rows, trimming to fit the screen."""
        # With one file the model rows are the result, so they stay once it
        # settles; in a batch they fold away to make room for the next file.
        groups = [_flatten(root, collapse=len(self.roots) > 1) for root in self.roots]
        if sum(len(rows) for rows in groups) <= max_rows:
            return [row for rows in groups for row in rows]

        # Screen is too small for everything: fill it by priority (problems and
        # in-flight work first) but render what survives in the original order.
        budget = max_rows - 1  # one row goes to the "hidden" note
        by_priority = sorted(range(len(groups)), key=lambda i: (_priority(self.roots[i]), i))

        visible, used = set(), 0
        for index in by_priority:
            cost = len(groups[index])
            if used + cost > budget:
                continue
            visible.add(index)
            used += cost

        if not visible:  # a single item taller than the screen; let it crop
            visible.add(by_priority[0])

        rows = [row for index in sorted(visible) for row in groups[index]]
        hidden = len(groups) - len(visible)
        if hidden:
            rows.append((f"… {hidden} more item(s) not shown", None))
        return rows


def _priority(root: Node) -> int:
    """Lower sorts first when the screen cannot hold every item."""
    if root.has_failure():
        return 0
    if root.status is Status.ACTIVE:
        return 1
    if root.status is Status.PENDING:
        return 2
    return 3


def _flatten(
    node: Node, guide: str = "", continuation: str = "", *, collapse: bool = True
) -> list[_Row]:
    """Rows for `node`; with `collapse`, children are shown only while the parent is in flight."""
    rows: list[_Row] = [(guide, node)]
    if not node.children or (collapse and node.status.finished and not node.has_failure()):
        return rows

    last_index = len(node.children) - 1
    for index, child in enumerate(node.children):
        is_last = index == last_index
        rows.extend(
            _flatten(
                child,
                continuation + ("└─ " if is_last else "├─ "),
                continuation + ("   " if is_last else "│  "),
                collapse=collapse,
            )
        )
    return rows


class _View:
    """Renderable wrapper so `Live` repaints from current tracker state."""

    def __init__(self, tracker: ProgressTracker) -> None:
        self._tracker = tracker

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield from self._tracker.render(console, options)


class _ConsoleHandler(logging.Handler):
    """Logging handler that prints above the live region."""

    def __init__(self, console: Console | None) -> None:
        super().__init__()
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._console is not None:
                self._console.print(self.format(record), highlight=False, markup=False)
        except Exception:  # noqa: BLE001  # pragma: no cover - never let logging break a run
            self.handleError(record)
