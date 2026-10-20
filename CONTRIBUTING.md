# Contributing

Thanks for helping improve `audioshake`. Bug reports, fixes and documentation
improvements are all welcome.

## Before you start

- **Security issues:** don't open an issue or PR. Follow
  [SECURITY.md](SECURITY.md) and email security@audioshake.ai.
- **Bugs:** open an [issue](https://github.com/audioshake/audioshake-python/issues)
  with the version (`audioshake --version`), your OS and Python version, the
  command or code you ran, and what happened. Redact API keys, tokens and
  presigned links. `-vv` output and raw `--json` output both contain presigned
  links.
- **New features or behavior changes:** open an issue first so we can agree on
  the shape before you write code. Anything that changes a CLI option, the
  `--json` output or an exit code is part of a public contract, and we are
  careful about those.
- **API behavior:** this repository is the client. A model's output, pricing,
  or a server error belongs with [AudioShake support](mailto:support@audioshake.ai),
  not here.

## Setup

You need Python 3.10 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/audioshake/audioshake-python
cd audioshake-python
uv sync                              # environment with dev dependencies
uv tool install -e . --reinstall     # optional: put this checkout's `audioshake` on PATH
```

The package lives under `src/`, so Python only sees it through the editable
install that `uv sync` makes. After pulling a change that moves files, run
`uv sync` again, and `uv tool install -e . --reinstall` if you use the global
command.

## Checks

Run these before opening a PR. CI runs the same on Python 3.10 to 3.15.

```bash
uv run pytest                        # HTTP is mocked; no API key needed
uv run ruff check . && uv run ruff format --check .
uv run mypy                          # strict on src/audioshake/, plus tests/typing/
uv run --with pyright==1.1.414 pyright --verifytypes audioshake --ignoreexternal
```

CI also enforces 85% coverage:

```bash
uv run pytest --cov=audioshake --cov-fail-under=85
```

| Tests | What they cover |
| --- | --- |
| `tests/lib/` | The library: clients, retries, downloads, `run()`, types, sign-in. HTTP is mocked with `respx`. |
| `tests/cli/` | The CLI through click's test runner, with mocked HTTP or a fake client. |
| `tests/test_messages.py` | Fails on a user-facing message written inline instead of in a message module. |
| `tests/test_skill_docs.py` | Fails when a CLI command or option is missing from `docs/cli.md` or the agent skill. |
| `tests/test_import.py` | Every module imports on every supported Python version. |

The test suite never calls the real API. If you try a change against it, use
your own API key and remember that every task is billed. Never commit a key,
a token or a presigned link, including in test fixtures; fixtures use
placeholder hosts such as `storage.test`.

## How the code is organized

The library in `src/audioshake/` is the product. The CLI in `src/audioshake/cli/` is
a thin layer on the library and never calls the API directly.
[docs/design.md](docs/design.md) explains the structure and the reasons
behind it. The rules that most often come up in review:

- **The API is the source of truth for models.** Read `GET /models` rather
  than hardcoding model ids, formats or languages.
- **The library raises; it never prints.** It raises typed exceptions from
  `src/audioshake/exceptions.py`, logs to the `audioshake` logger, and never
  swallows an error to return a default.
- **User-facing messages live in one place.** Wording goes in
  `src/audioshake/_messages.py`, or `src/audioshake/cli/_messages.py` when it only
  makes sense at the command line. A raise site passes a built message, never
  a string literal, and `tests/test_messages.py` checks this.
- **Sync and async stay in step.** `client.py` and `aio.py` share request
  building and parsing. A change to one is a change to both, with tests for
  both.
- **The CLI's output is a contract.** Logs go to stderr and results to stdout,
  `--json` stays machine-readable, and the exit codes in
  [docs/cli.md](docs/cli.md#exit-codes) do not change meaning.
- **Public API only.** No private hosts, internal service names or
  credentials in the code. `AUDIOSHAKE_BASE_URL` covers other deployments.

## Docs travel with the change

`plugin/skills/audioshake/SKILL.md` teaches AI agents to use the CLI and ships
as a Claude Code plugin from `plugin/`, so it must match the CLI exactly.
When your change touches a command, an option, a default, the `--json` shape,
the output layout, an exit code, authentication or model aliases, update in
the same PR:

1. `plugin/skills/audioshake/SKILL.md`, and bump `version` in
   `plugin/.claude-plugin/plugin.json` when its guidance changes.
2. `docs/cli.md`, and `docs/library.md` for library changes.
3. `CHANGELOG.md`: add a line to the top section, written as what the release
   does for a user rather than how the code changed.

To try your changes to the skill, start Claude Code from the repository root
with the plugin loaded from your checkout:

```bash
claude --plugin-dir ./plugin
```

Ruff formats the Python code blocks inside Markdown, so keep doc examples
under 100 columns.

## Style

- `ruff` handles formatting and linting; `mypy --strict` covers `src/audioshake/`.
- Google-style docstrings on public functions and classes.
- A new public name goes in `__all__` in `src/audioshake/__init__.py`, and in
  `docs/library.md`.
- Comments explain why the code is the way it is, not the history of how it
  got there.

## Pull requests

- Branch from `main` and target `main`.
- Keep a PR to one change. Separate refactors from behavior changes.
- Say what changed and why, and how you tested it. Link the issue if there is
  one.
- Add a test that fails without your change.
- CI must pass before review.

Releases are cut by maintainers from `v*` tags; merging a PR does not publish
anything.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
