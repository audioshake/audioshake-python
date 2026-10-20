"""Every message the library uses to report a problem.

One home for the exact words, so the same condition is never described two
different ways depending on which call path reached it. Anything that reaches a
user as an exception message or a warning log lives here; progress and debug
logging stays with the code that emits it.

Functions build the messages that carry values, and constants hold the fixed
ones. Constants written with ``%s`` placeholders are logging format strings:
pass them to the logger with their arguments so formatting stays lazy.

This module imports nothing from the package. Values that vary arrive as
arguments, environment variable names included, which keeps it free of import
cycles with the modules that raise.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

# -- models and targets ------------------------------------------------------

NO_MODELS = "At least one model is required"
NO_TARGETS = "A task needs at least one target"


def duplicate_model(model: str) -> str:
    """A model was named twice, directly or in a comma list."""
    return f"Model '{model}' is listed more than once; each model runs once per source"


def empty_model_name(value: str) -> str:
    """A comma-separated model list had an empty entry."""
    return f"Empty model name in {value!r}"


def invalid_task_limit(limit: int) -> str:
    """tasks.list() was asked for fewer than one task."""
    return f"limit must be at least 1, got {limit}"


def unknown_model(model_id: str) -> str:
    """No model with this id is visible to the account."""
    return f"Unknown model '{model_id}'. Use models.list() to see what is available."


def model_suggestion(model_id: str, suggestion: str) -> str:
    """The id is not in the catalog, but differs from a listed one only in spelling."""
    return f"Unknown model '{model_id}'. Did you mean '{suggestion}'?"


def unsupported_format(model_id: str, requested: str, supported: Iterable[str]) -> str:
    """The catalog says this model cannot produce the requested output format."""
    return f"Model '{model_id}' cannot output '{requested}'. Choose one of: {', '.join(supported)}"


def no_model_outputs_format(requested: str, offers: Iterable[tuple[str, Iterable[str]]]) -> str:
    """Nothing in a multi-model run offers the format, so name what each does offer.

    Takes ``(model id, formats)`` pairs rather than models, to stay clear of the
    package's own types.
    """
    choices = "; ".join(f"{model_id}: {', '.join(formats)}" for model_id, formats in offers)
    return f"No selected model can output '{requested}'. Formats: {choices}"


def unsupported_language(model_id: str, requested: str, supported: Iterable[str]) -> str:
    """The catalog says this model does not accept the requested language."""
    return (
        f"Model '{model_id}' does not support language '{requested}'. "
        f"Supported: {', '.join(supported)}"
    )


# -- tasks -------------------------------------------------------------------

# Stands in for a failed target's own error when the API sent none.
NO_TARGET_ERROR = "no error details provided"


def task_failed(task_id: str, reasons: Iterable[str]) -> str:
    """A task finished with at least one failed target."""
    return f"Task {task_id} completed with failures ({'; '.join(reasons)})"


# Logged when run() keeps a task some of whose targets failed. A failed target's
# credit hold is released; the completed ones are charged, so their outputs are
# downloaded rather than dropped with the error.
PARTIAL_TASK = "task %s: %s failed; keeping the %d target(s) that completed"


def task_timeout(task_id: str, timeout: float) -> str:
    """Waiting for a task ran past the caller's timeout."""
    return f"Task {task_id} did not finish within {timeout:.0f}s"


# -- credentials -------------------------------------------------------------


def no_credentials(api_key_env: str) -> str:
    """Nothing to authenticate with.

    Names the library's own knobs, not the CLI's; the CLI has its own wording
    in :mod:`audioshake.cli._messages`.
    """
    return f"No credentials. Pass api_key= or bearer_token=, or set {api_key_env}."


# -- arguments the caller got wrong ------------------------------------------

NEGATIVE_RETRIES = "retries cannot be negative"
NON_POSITIVE_CONCURRENCY = "download_concurrency must be at least 1"


def file_not_found(path: object) -> str:
    """A path handed to the library is not a file."""
    return f"No such file: {path}"


# -- HTTP --------------------------------------------------------------------

# The ``action`` half of an API error: "Failed to <action>: HTTP 500". These are
# message text, so they live here rather than at the call sites, where the sync
# and async clients would each spell them out and could drift apart.
ACTION_UPLOAD_ASSET = "upload asset"
ACTION_GET_ASSET = "get asset"
ACTION_CREATE_TASK = "create task"
ACTION_GET_TASK = "get task"
ACTION_LIST_TASKS = "list tasks"
ACTION_LIST_MODELS = "list models"
ACTION_DOWNLOAD_OUTPUT = "download output"
SHAPE_TASK = "a task"
SHAPE_TASK_LIST = "a list of tasks"
SHAPE_ASSET = "an asset"
SHAPE_MODEL_LIST = "a list of models"


def api_error(action: str, status: int, detail: str = "") -> str:
    """A non-2xx response. ``action`` says what was being attempted."""
    message = f"Failed to {action}: HTTP {status}"
    return f"{message} {detail}" if detail else message


def response_not_json(action: str, status: int) -> str:
    """A success response whose body is not JSON."""
    return f"Failed to {action}: the API answered HTTP {status} with a body that is not JSON"


def response_wrong_shape(action: str, expected: str) -> str:
    """A JSON success response that is not the shape the API documents."""
    return f"Failed to {action}: the API's answer was not {expected}"


def transport_timeout(action: str, error: object) -> str:
    """The request ran out of time connecting, sending, or reading."""
    return f"Failed to {action}: timed out ({error})"


def transport_failure(action: str, error: object) -> str:
    """The request never got an answer: DNS, connection, or protocol failure."""
    return f"Failed to {action}: {error}"


def download_retries_exhausted(retries: int, url: str) -> str:
    """Every attempt at a whole-file download failed."""
    return f"Failed to download output after {retries} retries: {url}"


# Logged before each retry. Both halves of the pair are shared by the sync and
# async transports, so the two report a retry identically.
REQUEST_RETRY = "%s %s failed (%s); retry %d/%d in %.1fs"
DOWNLOAD_RETRY = "download of %s failed; retry %d/%d in %.1fs"

# Logged when POST /tasks failed in a way that does not prove the server did
# nothing, so re-running blindly could pay for the same work twice.
TASK_MAY_EXIST = (
    "the task may have been created and billed despite this error; "
    "check the account's task list before retrying"
)

# Logged when GET /models is unavailable and the built-in defaults take over.
CATALOG_UNAVAILABLE = "could not fetch model catalog, using built-in defaults: %s"


def plain_http_warning(url: str) -> str:
    """The base URL is plain http to a host off this machine: credentials go in the clear."""
    return (
        f"{url} is plain http, so credentials are sent in the clear; "
        "use https unless this is a local proxy"
    )


# -- sign-in -----------------------------------------------------------------

# Shared prefix for the ways the browser flow can come back wrong.
_SIGNIN_FAILED = "Sign-in failed"

NO_REFRESH_TOKEN = "No refresh token is cached; sign in again"

# Logged when the credentials file exists but cannot be parsed.
CREDENTIALS_UNREADABLE = "could not read cached credentials at %s: %s"


def signin_not_configured(domain_env: str, client_id_env: str, api_key_env: str) -> str:
    """Neither a sign-in domain nor a client id is available."""
    return (
        "Browser sign-in is not configured. Set "
        f"{domain_env} and {client_id_env}, or use an API key ({api_key_env})."
    )


def callback_server_unavailable(ports: Sequence[int], error: object) -> str:
    """No loopback port was free to receive the browser redirect."""
    return (
        f"Could not bind a local callback server on any of {list(ports)}: {error}. "
        "Close whatever is using those ports and try again."
    )


def signin_timeout(timeout: float) -> str:
    """The browser never came back."""
    return f"Timed out after {timeout:.0f}s waiting for the browser sign-in"


def signin_unreachable(error: object) -> str:
    """The token endpoint could not be reached at all."""
    return f"Could not reach the sign-in service: {error}"


def token_request_rejected(detail: str) -> str:
    """The token endpoint answered with an error.

    ``detail`` is its own text, or ``HTTP <status>`` when it did not give one.
    """
    return f"The sign-in service rejected the token request: {detail}"


# Stands in for the content type when the response did not declare one.
NO_CONTENT_TYPE = "no content type"


def token_endpoint_not_json(url: str, content_type: str, token_domain_env: str) -> str:
    """The token endpoint answered with something that is not a token."""
    return (
        f"The token endpoint at {url} did not return a token "
        f"({content_type}). Set {token_domain_env} if it has moved."
    )


def signin_provider_error(detail: str) -> str:
    """The sign-in page itself reported an error."""
    return f"{_SIGNIN_FAILED}: {detail}"


def signin_wrong_audience() -> str:
    """The posted tokens did not claim to be for this application."""
    return f"{_SIGNIN_FAILED}: the tokens were not issued for this application"


def signin_state_mismatch() -> str:
    """The callback's state value did not match the one the flow sent."""
    return f"{_SIGNIN_FAILED}: the redirect state did not match the request"


def signin_no_code() -> str:
    """The callback carried neither a code nor a token."""
    return f"{_SIGNIN_FAILED}: no authorization code was returned"


def refresh_failed(error: object) -> str:
    """The cached session is expired and the refresh token did not work."""
    return f"The cached session could not be refreshed ({error}); sign in again"


# -- sign-in callback page ---------------------------------------------------
#
# Shown in the browser, not the terminal, so these are headings and sentences
# rather than log lines.

CALLBACK_FAILED_HEADING = _SIGNIN_FAILED
CALLBACK_SIGNED_IN_HEADING = "Signed in"
CALLBACK_SIGNED_IN = "You can close this tab and return to the terminal."
CALLBACK_UNEXPECTED_ORIGIN = "Unexpected origin."
CALLBACK_NO_CODE_OR_TOKEN = "The sign-in page returned neither a code nor a token."
CALLBACK_TOKENS_IN_URL = "Tokens are only accepted from the sign-in page, not in a link."
