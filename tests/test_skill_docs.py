"""The agent skill and the CLI reference must mention every command and option.

The CLI is the contract; these docs are how people and agents learn it. A new
option that is not documented fails here so it is not forgotten.
"""

import os
import re
from pathlib import Path

import click
import pytest

from audioshake.cli import main

ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = ROOT / "plugin" / "skills" / "audioshake" / "SKILL.md"
# Set to 1 in CI, which runs from the repository: there a missing skill must fail
# rather than skip, or the check that keeps it in step with the CLI would
# silently stop running.
REQUIRE_SKILL_ENV = "AUDIOSHAKE_REQUIRE_SKILL"
if not SKILL_PATH.exists():
    if os.environ.get(REQUIRE_SKILL_ENV) == "1":
        pytest.fail(
            f"{SKILL_PATH} is missing, and {REQUIRE_SKILL_ENV}=1, so the check that "
            "keeps the skill in step with the CLI cannot be skipped",
            pytrace=False,
        )
    # The source distribution ships the tests but not the agent skill.
    pytest.skip("the agent skill is only in the repository", allow_module_level=True)
SKILL = SKILL_PATH.read_text()
CLI_DOC = (ROOT / "docs" / "cli.md").read_text()


def _walk(command: click.Command, path: tuple[str, ...] = ()):
    """Yield (command path, command) for every command and subcommand."""
    yield path, command
    if isinstance(command, click.Group):
        for name, sub in command.commands.items():
            yield from _walk(sub, (*path, name))


def _option_forms(param: click.Parameter) -> list[str]:
    return [opt for opt in param.opts if opt.startswith("-")]


COMMANDS = [(" ".join(path), cmd) for path, cmd in _walk(main) if path]
OPTIONS = [
    (" ".join(path) or "audioshake", param)
    for path, cmd in _walk(main)
    for param in cmd.params
    if isinstance(param, click.Option) and not param.hidden and param.name not in ("help",)
]


@pytest.mark.parametrize("name,command", COMMANDS, ids=[c[0] for c in COMMANDS])
def test_every_command_is_documented(name, command):
    leaf = name.split()[-1]
    assert leaf in SKILL, f"`{name}` is missing from plugin/skills/audioshake/SKILL.md"
    assert leaf in CLI_DOC, f"`{name}` is missing from docs/cli.md"


@pytest.mark.parametrize("where,param", OPTIONS, ids=[f"{w}:{p.opts[0]}" for w, p in OPTIONS])
def test_every_option_is_documented(where, param):
    forms = _option_forms(param)
    if param.name == "version":
        return
    assert any(form in SKILL for form in forms), (
        f"{where} option {forms} is missing from plugin/skills/audioshake/SKILL.md"
    )
    assert any(form in CLI_DOC for form in forms), (
        f"{where} option {forms} is missing from docs/cli.md"
    )


# ---------------------------------------------------------------------------
# The reverse direction: everything the docs *say* is a command must exist.
# Catches a skill written ahead of (or after) the CLI. Only code is scanned
# (fenced blocks and inline spans), so prose about the tool is ignored.

_FENCED = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE = re.compile(r"`([^`\n]+)`")
_VALUE_OPTIONS = {
    "--api-key",
    "-o",
    "--output",
    "-m",
    "--model",
    "-f",
    "--format",
    "-c",
    "--language",
}


def _code_lines(text: str) -> list[str]:
    lines: list[str] = []
    for block in _FENCED.findall(text):
        lines.extend(block.splitlines())
    lines.extend(_INLINE.findall(text))
    return lines


def _documented_invocations(text: str) -> set[tuple[str, ...]]:
    """Command paths written as `audioshake [global opts] <cmd> [<sub>]` in code."""
    paths: set[tuple[str, ...]] = set()
    for line in _code_lines(text):
        tokens = line.split()
        if not tokens or tokens[0] != "audioshake":
            continue
        words: list[str] = []
        skip_next = False
        for token in tokens[1:]:
            if skip_next:
                skip_next = False
                continue
            if token.startswith("-"):
                skip_next = token in _VALUE_OPTIONS
                continue
            if not re.fullmatch(r"[a-z][\w-]*", token):
                break
            words.append(token)
            if len(words) == 2:
                break
        if words:
            paths.add(tuple(words))
    return paths


def _real_paths() -> set[tuple[str, ...]]:
    return {path for path, _ in _walk(main) if path}


@pytest.mark.parametrize(
    "doc,text", [("SKILL.md", SKILL), ("docs/cli.md", CLI_DOC)], ids=["SKILL.md", "docs/cli.md"]
)
def test_documented_commands_exist(doc, text):
    real = _real_paths()
    unknown = sorted(
        " ".join(path)
        for path in _documented_invocations(text)
        if path not in real and (len(path) == 1 or path[:1] not in real or path not in real)
    )
    assert not unknown, f"{doc} mentions commands the CLI does not have: {unknown}"
