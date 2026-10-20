"""How the live tree lays out its columns at different terminal widths."""

from rich.console import Console
from rich.text import Text

from audioshake.cli.progress import ProgressTracker, _View

LONG_DETAIL = "task 01JABCDEFGHJKMNPQRSTVWXYZ · 2 running"


def _lines(tracker, width):
    console = Console(width=width, height=24, force_terminal=True)
    with console.capture() as capture:
        console.print(_View(tracker))
    return [Text.from_ansi(line).plain for line in capture.get().splitlines()]


def _tree():
    tracker = ProgressTracker("audioshake run", "1 file · 2 models", live=False)
    node = tracker.add_item("some-fairly-long-song-name-here.wav")
    node.start(LONG_DETAIL)
    node.child("vocals").update("processing")
    node.child("drums").update("processing")
    tracker.set_total(1)
    return tracker


def test_a_wide_terminal_shows_the_whole_detail_flush_right():
    """With room to spare, a detail is shown in full and ends at the right edge."""
    lines = _lines(_tree(), 200)

    assert any(LONG_DETAIL in line for line in lines)
    model_rows = [line for line in lines if "vocals" in line or "drums" in line]
    assert all("processing ·" in line for line in model_rows)
    assert all(line.rstrip().endswith("processing · 0s") for line in model_rows)


def test_no_row_is_wider_than_the_terminal():
    """A row that overruns the width wraps, and the live region then scrolls."""
    for width in (200, 120, 80, 60):
        assert {len(line) for line in _lines(_tree(), width)} <= {0, width}


def test_a_narrow_terminal_splits_the_row_rather_than_starving_the_label():
    """The detail never takes more than half the row: a long task id cannot squeeze the
    file name out of the tree."""
    row = next(line for line in _lines(_tree(), 60) if "some-fairly" in line)

    label, detail = row[3:].split(" task ")
    assert label.startswith("some-fairly") and label.endswith("here.wav")
    assert len(label) >= 60 // 2 - 4 and detail.endswith("…")
