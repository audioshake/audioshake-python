"""``audioshake login`` / ``logout`` / ``whoami``: browser sign-in for the CLI."""

from __future__ import annotations

import os
import time

import click

from audioshake import AuthError, auth
from audioshake._http import API_KEY_ENV

from . import _messages
from ._logging import logger, stderr
from ._output import emit_json, emit_line
from ._settings import Settings


@click.command()
@click.option(
    "--no-browser", is_flag=True, help="Print the sign-in URL instead of opening a browser"
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=10),
    default=auth.LOGIN_TIMEOUT_SECONDS,
    show_default=True,
    help="Seconds to wait for the browser sign-in",
)
@click.pass_obj
def login(settings: Settings, no_browser: bool, timeout: float) -> None:
    """Sign in through the AudioShake sign-in page and cache the session.

    Later commands use the cached session when no API key is given. The
    session is refreshed silently until its refresh token expires.
    """
    if os.environ.get(API_KEY_ENV) and not settings.quiet:
        logger.warning(_messages.API_KEY_PRECEDENCE, API_KEY_ENV)

    def show_url(url: str) -> None:
        if no_browser:
            stderr.print("Open this URL in a browser to sign in:\n")
            stderr.print(url, soft_wrap=True)
            return
        stderr.print("Opening the AudioShake sign-in page in your browser...")
        # The URL carries this flow's `state`, the guard on the loopback
        # callback, so keep it out of scrollback and CI logs unless asked.
        if settings.verbosity > 0:
            stderr.print(f"[dim]If it does not open, visit: {url}[/dim]", soft_wrap=True)
        else:
            stderr.print("[dim]If it does not open, re-run with -v for the URL.[/dim]")

    tokens = auth.login(open_browser=not no_browser, timeout=timeout, on_url=show_url)
    if settings.json:
        emit_json({"signed_in_as": tokens.describe(), "expires_at": tokens.expires_at})
    else:
        emit_line(f"Signed in as [bold]{tokens.describe()}[/bold]")


@click.command()
@click.option("--all", "everything", is_flag=True, help="Forget sessions for every API host")
@click.pass_obj
def logout(settings: Settings, everything: bool) -> None:
    """Discard the cached sign-in session."""
    auth.logout(everything=everything)
    if not settings.json:
        emit_line("Signed out" + (" of all hosts" if everything else ""))
    else:
        emit_json({"signed_out": True})


@click.command()
@click.pass_obj
def whoami(settings: Settings) -> None:
    """Show which credential the CLI would use and who the session belongs to."""
    api_key_set = bool(settings.api_key or os.environ.get(API_KEY_ENV))
    try:
        tokens = auth.get_valid_token()
    except AuthError as e:
        if not api_key_set:
            raise  # the session was the only credential: exit 3, as any command would
        # The API key is what every command would use, so an expired session
        # beside it is worth a warning, not a failure.
        logger.warning(_messages.SESSION_NOT_REFRESHED, e)
        tokens = None

    if settings.json:
        emit_json(
            {
                "credential": "api_key" if api_key_set else ("session" if tokens else None),
                "signed_in_as": tokens.describe() if tokens else None,
                "expires_at": tokens.expires_at if tokens else None,
            }
        )
        return

    if tokens is not None:
        expires = time.strftime("%Y-%m-%d %H:%M", time.localtime(tokens.expires_at))
        emit_line(f"Signed in as [bold]{tokens.describe()}[/bold] (session valid until {expires})")
    if api_key_set:
        source = "--api-key" if settings.api_key else API_KEY_ENV
        emit_line(
            f"Using the API key from {source}" + (", which takes precedence" if tokens else "")
        )
    if tokens is None and not api_key_set:
        emit_line(_messages.not_signed_in(API_KEY_ENV))
        raise SystemExit(3)
