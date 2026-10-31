# Working in audioshake-python

`audioshake` is the public Python library and CLI for the AudioShake API. The
library (`src/audioshake/`) is the product; the CLI (`src/audioshake/cli/`) is a thin
layer on it and never calls the API directly. Design notes: `docs/design.md`.

## Commands

```bash
uv sync                                   # dev environment
uv run pytest                             # HTTP is mocked; no API key needed
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict on src/audioshake/
uv tool install -e . --reinstall          # make `audioshake` on PATH follow this checkout
```

CI runs all of these on Python 3.10–3.15 with an 85% coverage gate. Ruff also
formats Python code blocks inside Markdown, so keep doc examples under 100 columns.

## Rules

- The API is the source of truth for models: read `GET /models` (cached on the
  client) instead of hardcoding ids, formats or languages. The one constant is
  `SPEAKER_COUNT_MODELS`, because the API does not declare it.
- The library raises typed exceptions (`src/audioshake/exceptions.py`) and logs to
  the `audioshake` logger. It never prints and never swallows an error to return
  a default. The CLI maps exceptions to exit codes in `src/audioshake/cli/__init__.py`.
- Every message a failure shows a user lives in `src/audioshake/_messages.py`, or
  `src/audioshake/cli/_messages.py` when the wording only makes sense at the command
  line. Raise sites, WARNING/ERROR logs and the `action` fragments pass a built
  message, never a literal. `tests/test_messages.py` enforces this by AST, so a
  new inline string fails CI; add the wording to a message module instead.
- CLI: logs on stderr, results on stdout, `--json` must stay machine-readable.
  Exit codes 0/1/2/3/4/5/130 are a contract; see `docs/cli.md`.
- No staging hosts, internal service names or credentials in the code. The
  public API only; `AUDIOSHAKE_BASE_URL` covers other deployments.
- Sync and async clients share request building and parsing; a change to one
  is a change to both (`client.py` / `aio.py`).

## When you change the CLI, update the skill and the docs

`plugin/skills/audioshake-cli/SKILL.md` teaches agents to use the CLI. `plugin/` is
a Claude Code and Codex plugin, listed in the `audioshake/claude-plugins` marketplace,
which fetches only that folder. The two manifests, `.claude-plugin/plugin.json`
and `.codex-plugin/plugin.json`, must carry the same `name` and `version`.
It must match the CLI exactly. Whenever
a change touches any of these:

- a command, option or argument (added, renamed, removed, new default)
- the `--json` output shape or the output file layout
- exit codes or the meaning of an error
- authentication (credential order, `login` / `whoami` / `logout`)
- model limits the skill mentions

then in the same PR:

1. Update `plugin/skills/audioshake-cli/SKILL.md` (tables, examples, gotchas).
2. Update `docs/cli.md` and, for library changes, `docs/library.md`.
3. Add a line to the top section of `CHANGELOG.md`. Until 0.1.0 ships, that is
   the 0.1.0 entry itself, written as what the release does, not how it changed.
4. Run `uv run pytest tests/test_skill_docs.py`: it fails when a CLI command or
   option is missing from the skill or the CLI reference.

Bump `version` in `plugin/.claude-plugin/plugin.json` when the skill's
guidance changes. Try it locally with `claude --plugin-dir ./plugin`.

## Releasing

Nothing publishes on merge. Before tagging, replace `Unreleased` in the top
`CHANGELOG.md` heading with the release date and open a new `[Unreleased]`
section above it. A `v*` tag matching `src/audioshake/_version.py` runs
`.github/workflows/publish.yml` (PyPI trusted publishing, `pypi` environment).
Push `main` before the tag: publish fails on a tag whose commit is not yet on main.
If the publish run fails, use "Re-run failed jobs" rather than re-running all jobs.
The marketplace pins the plugin to a tag, so after a release move its `ref`
to the new tag.
Stacked PRs: check the base branch is `main` before merging, or the merge lands
on the parent branch instead.
