# Design notes

How the `audioshake` package is put together and why. For what each function
and option does, read [library.md](library.md) and [cli.md](cli.md).

## Goals

- One distribution, `audioshake`, that is both a typed Python library and a CLI
  built on that library. Nothing in the CLI talks to the API except through
  the library.
- The API is the source of truth for models: ids, output formats, supported
  languages and limits come from `GET /models`, not from lists in the code.
- The library raises precise exceptions and never logs a warning and returns a
  default. The CLI turns those into messages and exit codes.
- Logs on stderr, results on stdout, so the CLI composes with pipes and `jq`.
- Public API only: no private hosts or endpoints in the code.
  `AUDIOSHAKE_BASE_URL` covers other deployments.

## Non-goals (for now)

- Chaining model outputs into further models ("pipelines") and mixing outputs
  locally. These belong in an explicit layer built on the library, if at all.
- A config file. Configuration is environment variables and command-line
  options.

## Layout

```
src/audioshake/                 # the package; `src/` keeps tests on the installed copy
  __init__.py                   # public names; see __all__
  client.py                     # AudioShake + Assets / Tasks / Models resources (sync)
  aio.py                        # AsyncAudioShake + async resources (same surface)
  types.py                      # frozen dataclasses + from_api()/to_api()
  exceptions.py
  auth.py                       # browser sign-in: PKCE flow, token cache, refresh, revoke
  _messages.py                  # every message shown on a failure, in one place
  _text.py                      # one-line, capped rendering of error text
  _http.py                      # httpx client factory, timeouts, retry loop (sync + async)
  _retry.py                     # policy: retryable statuses, backoff with jitter
  _download.py                  # streamed downloads and the shared transfer budget
  _run.py                       # run(): upload -> create -> wait -> download, ProgressEvent
  py.typed
  cli/
    __init__.py                 # click group `main`, exception -> exit code mapping
    _messages.py                # the failure wording that is CLI-only
    _settings.py                # global options carried on the click context
    _logging.py                 # RichHandler on stderr, -v/-vv/-q
    _output.py, _format.py      # --json, tables and display helpers
    run.py                      # `run`: batch driver on AudioShake.run()
    progress.py                 # rich live tree
    models.py, tasks.py, auth.py
tests/
  lib/  cli/  fixtures/*.json
```

## Library

`AudioShake` and `AsyncAudioShake` expose the same resources (`assets`, `tasks`,
`models`) and the same `run()`. They share request building and response
parsing in `_http.py` and `types.py`; only the I/O calls differ, so a change to
one client is a change to both.

Types are frozen dataclasses with a tolerant `from_api` that keeps the full
object in `.raw`, so a new API field never breaks parsing. `to_api()` emits
camelCase (`numSpeakers`); everything else is snake_case.

Model-aware defaults come from `/models`: with no format given, a model gets
its first audio output format, else its first output format, so
`transcription` gets `json` without a hardcoded list. `--format` and
`--language` are validated against `outputFormats` and `supportedLanguages`
before anything is uploaded. The API does not declare which models take a
speaker count, so `SPEAKER_COUNT_MODELS = ("multi_voice",)` is the one
constant.

## Messages

Every string a failure shows a user lives in `_messages.py`, and the CLI-only
wording in `cli/_messages.py`. A raise site passes a built message, never a
literal: `raise ValidationError(_messages.duplicate_model(name))`. This covers
exception messages, WARNING and ERROR logs, and the fragments folded into a
message, `action` among them (`"Failed to get task: HTTP 404"`). Progress and
debug logging stays with the code that emits it.

One condition then reads the same way whichever call path reached it, the sync
and async halves cannot drift, and the wording can be reviewed in one file. The
message modules import nothing from the package, so `exceptions.py` and
`_http.py` can import them without a cycle. Functions build the messages that
carry values, constants hold the fixed ones, and `%s` constants stay logging
format strings so formatting is lazy. `tests/test_messages.py` walks the AST of
every module and fails on a literal left at a raise site.

## Retries and billing

A single transport-level loop in `_http.py` retries connection errors,
timeouts and 408/425/429/5xx with exponential backoff and jitter, honouring
`Retry-After`. Streamed downloads are re-run whole under the same policy.

Creating a task is billed, and a lost response or a gateway error can follow a
task the backend already created. `tasks.create()` is therefore retried only
on statuses where the API declined to act (408, 425, 429, 503), and any other
failure warns that the task may exist. A model that fails on its input is
reported once and never resubmitted.

## Downloads

Outputs are presigned links, fetched with a separate client that carries no
credentials. Each file streams to a temporary file next to its destination
and is renamed into place on success, so a failure never
leaves a partial output. A task's outputs download concurrently under one
budget per client, shared by every download it makes, so running several
sources in parallel does not multiply the number of open connections.

Output names come from the API and are reduced to safe path components before
anything is written. Names that would collide after that get the model id
prefixed, then a numeric suffix, rather than overwriting each other.

## Logging

- Library: `logging.getLogger("audioshake")` with a `NullHandler`; children
  `audioshake.http`, `audioshake.tasks`. DEBUG is requests and poll ticks,
  INFO state changes, WARNING retries. No `print`, no ANSI.
- CLI: `rich.logging.RichHandler` on **stderr**. Default WARNING; `-v` INFO;
  `-vv` DEBUG, including `httpx`; `-q` ERROR. stdout carries only results and
  `--json`.
- Live tree: while it runs, WARNING and above print above the tree and
  INFO/DEBUG are dropped. `--plain`, a non-terminal stdout or `-v` disables it.

## CLI

The CLI is a thin layer: `run` drives `AudioShake.run()` once per source on a
thread pool, and every other command maps to one library call. Exceptions are
mapped to exit codes in `cli/__init__.py`; the codes are a contract, listed in
[cli.md](cli.md#exit-codes). When a batch fails several ways, the most
actionable code wins: authentication, then network, then a bug, then a failed
source.

Ctrl-C cancels queued sources and stops an upload before its task is created.
A task that already exists is billed whatever happens locally, so the CLI waits
for it to finish and download unless interrupted a second time.

## Standards

- Naming: kebab-case flags, snake_case Python, camelCase only inside
  `to_api`/`from_api`.
- Tooling: `ruff` (lint and format), `mypy --strict` on `src/audioshake/`,
  `pytest` with `pytest-asyncio` and `respx`, an 85% coverage gate, a GitHub
  Actions matrix on Python 3.10 to 3.15, `uv lock --check`, and PyPI trusted
  publishing from `v*` tags.
- Google-style docstrings, `__all__` in `src/audioshake/__init__.py`, and a
  Keep a Changelog `CHANGELOG.md`.
- `tests/test_skill_docs.py` fails when a CLI command or option is missing from
  the agent skill or the CLI reference.
