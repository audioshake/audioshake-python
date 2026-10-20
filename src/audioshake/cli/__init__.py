"""Command-line interface for AudioShake.

audioshake run song.wav -m vocals,drums -o out/
audioshake models
audioshake tasks list | get ID | download ID
audioshake login | logout | whoami
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import click

from audioshake import (
    APIConnectionError,
    AudioShakeError,
    AuthenticationError,
    AuthError,
    ServerError,
    UnexpectedResponseError,
    ValidationError,
    __version__,
)

from . import _messages
from ._format import format_error
from ._logging import configure_logging, level_for, logger
from ._settings import (
    EXIT_AUTH,
    EXIT_FAILED_ITEMS,
    EXIT_NETWORK,
    EXIT_UNEXPECTED,
    EXIT_USAGE,
    Settings,
)
from .auth import login, logout, whoami
from .models import models
from .run import run
from .tasks import tasks

__all__ = ["main"]

F = TypeVar("F", bound=Callable[..., Any])


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--api-key", help="API key; otherwise AUDIOSHAKE_API_KEY, then the `login` session")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output on stdout")
@click.option("--plain", is_flag=True, help="Log lines instead of the live progress view")
@click.option("-v", "--verbose", count=True, help="More detail on stderr (-vv for debug)")
@click.option("-q", "--quiet", is_flag=True, help="Errors only on stderr")
@click.version_option(__version__, prog_name="audioshake")
@click.pass_context
def main(
    ctx: click.Context,
    api_key: str | None,
    as_json: bool,
    plain: bool,
    verbose: int,
    quiet: bool,
) -> None:
    """AudioShake: separate stems, isolate dialogue, transcribe and align lyrics.

    Set AUDIOSHAKE_API_KEY or run `audioshake login`, then `audioshake run song.wav -m vocals`.
    Logs go to stderr; results (and --json) go to stdout.
    """
    configure_logging(level_for(verbose, quiet))
    ctx.obj = Settings(api_key=api_key, json=as_json, plain=plain, verbosity=verbose, quiet=quiet)


def handle_errors(func: F) -> F:
    """Turn library exceptions into messages and exit codes; shared by every subcommand."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (AuthenticationError, AuthError) as e:
            logger.error(str(e))
            sys.exit(EXIT_AUTH)
        except (APIConnectionError, ServerError) as e:
            logger.error(str(e))
            sys.exit(EXIT_NETWORK)
        except ValidationError as e:
            logger.error(str(e))
            sys.exit(EXIT_USAGE)
        except UnexpectedResponseError as e:
            # An answer the library could not read is not the user's to fix.
            logger.error(str(e))
            sys.exit(EXIT_UNEXPECTED)
        except AudioShakeError as e:
            logger.error(str(e))
            sys.exit(EXIT_FAILED_ITEMS)
        except (click.ClickException, click.exceptions.Exit, click.Abort):
            # Click prints its own usage errors and chooses its own exit code.
            raise
        except KeyboardInterrupt:
            logger.warning(_messages.INTERRUPTED)
            sys.exit(130)
        except Exception as e:  # noqa: BLE001 - a bug must not print a traceback
            # A stack trace names local paths and the library's internals and
            # tells the user nothing they can act on; -vv still has it.
            detail = format_error(str(e)) or _messages.NO_ERROR_DETAILS
            logger.error(_messages.UNEXPECTED_ERROR, type(e).__name__, detail)
            logger.debug("unexpected error", exc_info=True)
            sys.exit(EXIT_UNEXPECTED)

    return wrapper  # type: ignore[return-value]


def _wrap(command: click.Command) -> None:
    """Route a command's callback (or each of a group's) through handle_errors."""
    if isinstance(command, click.Group):
        for sub in command.commands.values():
            _wrap(sub)
        return
    assert command.callback is not None
    command.callback = handle_errors(command.callback)


for _command in (run, models, tasks, login, logout, whoami):
    _wrap(_command)
    main.add_command(_command)
