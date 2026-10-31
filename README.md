# AudioShake

[![PyPI](https://img.shields.io/pypi/v/audioshake)](https://pypi.org/project/audioshake/)
[![Python](https://img.shields.io/pypi/pyversions/audioshake)](https://pypi.org/project/audioshake/)
[![CI](https://github.com/audioshake/audioshake-python/actions/workflows/ci.yml/badge.svg)](https://github.com/audioshake/audioshake-python/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/audioshake/audioshake-python/blob/main/LICENSE)

Python client and command-line tool for the [AudioShake API](https://developer.audioshake.ai):
stem separation, dialogue isolation, speech clean-up, multi-speaker separation,
music detection and removal, lyric transcription and alignment.

```bash
pip install audioshake          # library + CLI
uvx audioshake run song.wav -m vocals,drums   # or run the CLI with no install
```

- **Library**: `AudioShake` and `AsyncAudioShake` clients, typed results, precise
  exceptions, automatic retries, one-call `run()` or the individual API steps.
- **CLI**: `audioshake run` for files, folders and URLs with a live progress
  tree, `--json` for scripts, `audioshake models` and `audioshake tasks` to
  explore your account.
- Python 3.10+, typed (`py.typed`), tested on 3.10 through 3.15.
- Version 0.x: the surface is settling; breaking changes are called out in the
  [changelog](https://github.com/audioshake/audioshake-python/blob/main/CHANGELOG.md)
  and bump the minor version.

Authenticate either with an API key from the
[AudioShake dashboard](https://dashboard.audioshake.ai), exported as
`AUDIOSHAKE_API_KEY`, or by signing in from the terminal with `audioshake login`.

## Library

```python
from audioshake import AudioShake

with AudioShake() as client:  # reads AUDIOSHAKE_API_KEY
    result = client.run("song.wav", ["vocals", "drums"], output_dir="stems/song")

print(result.task.cost)  # credits charged
print([str(f) for f in result.files])  # stems/song/vocals.wav, stems/song/drums.wav
```

`run()` uploads the file (or passes a URL through), creates the task, waits for
it and downloads the outputs into `output_dir` as `<output>.<format>`, for
example `vocals.wav`. Give each source its own directory; the CLI does that for
you with one sub-directory per source. Every step is also available on its own:

```python
from audioshake import AudioShake, Target

with AudioShake() as client:
    for model in client.models.list():
        print(model.id, model.credits_per_minute, model.output_formats)

    asset = client.assets.upload("interview.mp3")
    task = client.tasks.create(
        asset,
        [
            Target("multi_voice", num_speakers=3),
            Target("transcription", formats=("json",), language="en"),
        ],
    )
    task = client.tasks.wait(task.id, on_poll=lambda t: print(t.done))
    client.tasks.download(task, "out/")
```

The async client has the same surface:

```python
import asyncio
from audioshake import AsyncAudioShake


async def main():
    async with AsyncAudioShake() as client:
        results = await asyncio.gather(
            client.run("a.wav", ["vocals"], output_dir="out/a"),
            client.run("b.wav", ["vocals"], output_dir="out/b"),
        )


asyncio.run(main())
```

Errors are typed, so you can catch what you can handle:

```python
from audioshake import AudioShake, AuthenticationError, TaskFailedError

try:
    with AudioShake() as client:
        result = client.run("song.wav", ["vocals", "drums"], output_dir="stems/song")
    for target in result.failed:  # some models failed; the rest were downloaded
        print(target.model, target.error)
except TaskFailedError as e:  # every model failed
    print(e.task.failed[0].error)  # the API's reason
except AuthenticationError:
    ...
```

Connection errors, timeouts, rate limits and 5xx responses are retried with
backoff before an exception reaches you. Task creation is billed, so it is only
retried when the API declined to act (408, 425, 429, 503). The library logs to
the `audioshake` logger and never prints.

Full reference:
[docs/library.md](https://github.com/audioshake/audioshake-python/blob/main/docs/library.md).

## CLI

```bash
audioshake run song.wav -m vocals,drums -o stems/
audioshake run ./album -m effects,dialogue,music_fx -f mp3 --residual
audioshake run https://example.com/talk.mp3 -m transcription --language en
audioshake run panel.wav -m multi_voice --num-speakers 3

audioshake models                 # what your account can run, with cost and formats
audioshake tasks list             # recent tasks
audioshake tasks get TASK_ID
audioshake tasks download TASK_ID -o out/
```

`run` takes any number of files, directories (`-r` to recurse) and public URLs,
processes them concurrently (`-c`, default 10), and writes
`OUT/<source>/<output>.<format>`, the layout Demucs and Spleeter use. In a
terminal it paints a live tree with one row per source and per model;
elsewhere, or with `--plain`, it logs lines instead.

Logs go to **stderr**; results go to **stdout**. `--json` makes the results
machine-readable (`run` prints one JSON object per source):

```bash
audioshake --json run song.wav -m vocals | jq .files
audioshake --json models | jq '.[].id'
```

Exit codes: `0` success, `1` a source failed or partly completed, `2` usage
error, `3` authentication, `4` network failure after retries, `5` unexpected
error, `130` interrupted.

Full reference, every option and the output layout:
[docs/cli.md](https://github.com/audioshake/audioshake-python/blob/main/docs/cli.md).

## Install

| | |
| --- | --- |
| Library | `pip install audioshake` or `uv add audioshake` |
| CLI, no install | `uvx audioshake ...` |
| CLI, on your PATH | `uv tool install audioshake` or `pipx install audioshake` |
| From source | `git clone https://github.com/audioshake/audioshake-python && cd audioshake-python && uv sync` |

## Authentication

Two credentials work everywhere; the first one found wins:

1. An API key: `--api-key`, or `AUDIOSHAKE_API_KEY` in the environment (what CI
   and scripts should use).
2. A browser sign-in session: `audioshake login` opens the AudioShake sign-in
   page, caches the session in `~/.audioshake/credentials.json` (mode 600) and
   refreshes it silently until its refresh token expires.

```bash
audioshake login                # opens the browser
audioshake login --no-browser   # prints the URL (on stderr) instead
audioshake whoami               # which credential is in use, and who you are
audioshake logout
```

Under the hood `login` is the OAuth 2.0 authorization-code flow with PKCE: the
CLI listens on `127.0.0.1` for the redirect, exchanges the code for tokens, and
sends them to the API as `Authorization: Bearer`. From Python, use
`audioshake.auth.login()` and pass the token to `AudioShake(bearer_token=...)`.

`AUDIOSHAKE_BASE_URL` points the library at another API host.

## Models

Model ids are passed through to the API, which is the source of truth for what
exists; `audioshake models` lists the current catalogue with cost per minute,
output formats, supported languages and input limits. Text models
(`transcription`, `alignment`, `music_detection`, `music_identification`)
produce JSON by default; audio models produce WAV unless you ask for another
format. Several models run from one upload: `-m effects,dialogue,music_fx` gives
the dialogue, music and effects stems, `-m vocals_backing,vocals_lead` the two
vocal stems.

## Claude Code plugin

The `plugin/` folder is a plugin for Claude Code and Codex that teaches the
agent to use the CLI and library. Install it from the AudioShake marketplace.

Inside Claude Code:

```
/plugin marketplace add audioshake/claude-plugins
/plugin install audioshake-cli@audioshake
```

From your shell, for Codex:

```bash
codex plugin marketplace add audioshake/claude-plugins
codex plugin add audioshake-cli@audioshake
```

Then ask for stems, an instrumental, isolated dialogue or lyrics from a file and
Claude will run `audioshake` for you. The skill is kept in step with the CLI by
`tests/test_skill_docs.py`.

## Development

```bash
uv sync                          # environment with dev dependencies
uv run pytest                    # tests (HTTP is mocked; no key needed)
uv run ruff check . && uv run ruff format --check .
uv run mypy                      # strict on the library
```

CI runs the same on Python 3.10 to 3.15. Releases are published to PyPI from
`v*` tags. See
[CONTRIBUTING.md](https://github.com/audioshake/audioshake-python/blob/main/CONTRIBUTING.md)
before opening an issue or a pull request. Design notes are in
[docs/design.md](https://github.com/audioshake/audioshake-python/blob/main/docs/design.md).

## Links

- [API documentation](https://developer.audioshake.ai)
- [Issues](https://github.com/audioshake/audioshake-python/issues)
- MIT License, © AudioShake, Inc.
