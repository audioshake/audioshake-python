"""Console logging for the CLI. The library itself never installs handlers.

All log output goes to **stderr**, so stdout carries only results (paths,
tables, ``--json``) and stays safe to pipe.
"""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

LOGGER_NAME = "audioshake"

logger = logging.getLogger(LOGGER_NAME)
stderr = Console(stderr=True, highlight=False)


def level_for(verbosity: int, quiet: bool) -> int:
    """Map ``-v`` counts and ``-q`` onto a logging level."""
    if quiet:
        return logging.ERROR
    if verbosity >= 2:
        return logging.DEBUG
    if verbosity == 1:
        return logging.INFO
    return logging.WARNING


def configure_logging(level: int = logging.WARNING) -> None:
    """Send the ``audioshake`` logger to stderr through rich."""
    handler = RichHandler(
        console=stderr,
        show_time=False,
        show_path=False,
        markup=False,
        rich_tracebacks=False,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    # Replace, don't append: repeated invocations in one process must not double up.
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False
    # httpx logs every request at INFO; only show that when the user asks for debug.
    logging.getLogger("httpx").setLevel(
        logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    )
    if level <= logging.DEBUG:
        logging.getLogger("httpx").handlers = [handler]
        logging.getLogger("httpx").propagate = False
