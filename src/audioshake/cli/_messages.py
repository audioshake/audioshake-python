"""Every message the CLI adds on top of the library's.

The library's wording lives in :mod:`audioshake._messages`; this module holds
the text that only makes sense at the command line, where the advice is a flag
or a subcommand rather than a keyword argument.

Same conventions: functions for messages that carry values, constants for the
fixed ones, and ``%s`` constants are logging format strings to be passed to the
logger with their arguments.
"""

from __future__ import annotations

from collections.abc import Iterable

# -- credentials -------------------------------------------------------------


def not_signed_in(api_key_env: str) -> str:
    """No credential at all: used both when a command needs one and by `whoami`."""
    return f"Not signed in. Run `audioshake login`, pass --api-key, or set {api_key_env}."


# Logged by `login` when a key in the environment would win anyway.
API_KEY_PRECEDENCE = "%s is set and takes precedence over a signed-in session"

# Logged by `whoami` when an API key is set and the cached session beside it is dead.
SESSION_NOT_REFRESHED = "The cached sign-in session could not be refreshed (%s); using the API key"


# -- sources -----------------------------------------------------------------

NO_AUDIO_FILES = "No audio files to process"

# Logged per directory that contained nothing we recognise.
NO_SUPPORTED_AUDIO_FILES = "no supported audio files in %s"


def source_does_not_exist(value: str) -> str:
    """A SOURCE argument names no file, directory, or URL."""
    return f"{value} does not exist"


def empty_asset_ref(value: str) -> str:
    """An ``asset:`` SOURCE with nothing after the colon."""
    return f"{value!r} names no asset; write asset:<id>"


def asset_not_found(asset_id: str) -> str:
    """The API has no asset by this id: a typo, or an upload that has expired."""
    return f"Asset '{asset_id}' not found; it may have expired. Pass the file or URL to upload it again."


# -- run ---------------------------------------------------------------------

# Stands in for a failed target's own error in the progress tree, where there is
# only room for a word.
TARGET_FAILED = "failed"

# Logged once per source that did not finish.
SOURCE_FAILED = "failed %s: %s"


def uploaded_as_asset(asset_id: str) -> str:
    """Appended to a failure after the upload: the upload is not lost."""
    return f"uploaded as asset {asset_id}; re-run with asset:{asset_id} to skip the upload"


INTERRUPTED = "Interrupted"

# Logged once on the way out of an interrupted batch, so the user can see that
# the sources still queued were dropped rather than quietly run and billed.
INTERRUPT_SKIPPED = "interrupted: %d source(s) were not started"

# Carried by the worker that finds the batch already stopped. Nothing has been
# uploaded or created for it, so it is not billed and not reported as a failure.
NOT_STARTED = "not started: the batch was interrupted"

# Carried by a source whose upload was cut short by the interrupt. Its task was
# never created, so nothing will be charged for it.
STOPPED_BEFORE_TASK = "stopped before its task was created: the batch was interrupted"

# Logged when an interrupted batch still has sources whose tasks exist. There is
# no way to cancel a task, and whatever completes is charged, so they are left
# to finish and download; without this the CLI looks hung, and a second Ctrl-C
# would cut their downloads short.
INTERRUPT_FINISHING = (
    "waiting for %d task(s) already created to finish and download: %s. "
    "Press Ctrl-C again to leave them; `audioshake tasks download TASK_ID` fetches them later"
)

# Logged on the second Ctrl-C, just before the process exits without waiting.
INTERRUPT_ABANDONED = "left %d running task(s) unfinished; `audioshake tasks download` fetches them"

# Logged once per source whose task partly completed: the named models failed,
# the others were downloaded.
SOURCE_PARTIAL = "partly completed %s: %s"

# Logged once for a bug that reached the top of the CLI; the traceback is only
# at -vv, since it names local paths and the library's internals.
UNEXPECTED_ERROR = "Unexpected error (%s): %s"
NO_ERROR_DETAILS = "no details"


def num_speakers_ignored(models: Iterable[str]) -> str:
    """`--num-speakers` was given but no model selected can use it."""
    return f"--num-speakers only applies to {', '.join(models)}; none selected, so it is ignored"


# -- tasks -------------------------------------------------------------------

NO_DOWNLOADABLE_OUTPUTS = "No downloadable outputs (task not finished, or every model failed)."


def since_not_a_duration(value: str) -> str:
    """`--since` was neither a duration nor a date we could read."""
    return f"{value!r} is not a duration like 24h or 7d, nor an ISO date"


def since_too_far_back(value: str) -> str:
    """`--since` named a duration that does not fit in a date."""
    return f"{value!r} is too far back"
