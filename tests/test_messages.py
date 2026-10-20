"""The message modules are the single home for the words we show on a failure.

These guard the properties that make that home worth having: a raise site never
carries its own literal, nothing in the modules goes unused, and they stay
importable from anywhere in the package.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "audioshake"

MODULES = [PACKAGE / "_messages.py", PACKAGE / "cli" / "_messages.py"]

# Every module that may have to report a failure, so the sweep below covers new
# files without anyone remembering to add them.
SOURCES = sorted(p for p in PACKAGE.rglob("*.py") if p not in MODULES)

# Exceptions allowed to carry a literal: they either carry no message at all
# (an exit status) or never reach a user. Anything else raised with a literal
# string is a message that escaped the message modules, including exception
# types added after this test was written.
CARRY_NO_MESSAGE = frozenset(
    {
        "SystemExit",
        "AssertionError",
        "NotImplementedError",
        "StopIteration",
        "StopAsyncIteration",
    }
)

# Helpers that fold what they are handed into a user-facing message, so the text
# they receive is message text too: `error_from_response(response, "get task")`
# ends up as "Failed to get task: HTTP 404".
MESSAGE_BUILDERS = frozenset({"error_from_response", "classify_transport_error"})

_NAME = re.compile(r"^(?:def ([a-z_][a-z_0-9]*)\(|([A-Z][A-Z_0-9]*) = )", re.MULTILINE)


def _defined_names(module: Path) -> list[str]:
    """Public message builders and constants, in definition order."""
    return [func or const for func, const in _NAME.findall(module.read_text())]


def _is_literal_text(node: ast.expr) -> bool:
    """A string spelled out at the call site: ``"..."`` or an f-string."""
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _called_name(func: ast.expr) -> str:
    """``ValidationError`` for a bare name, ``BadParameter`` for ``click.BadParameter``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _literal_messages(module: Path) -> list[str]:
    """Every place in ``module`` that spells failure text out in line.

    Four shapes carry it: raising an exception, logging at warning or error,
    handing a fragment to a helper that folds it into a message, and the
    ``action`` that names the failed call. Other keyword arguments are ignored,
    so ``param_hint="SOURCE"`` and the like do not count.
    """
    found: list[str] = []
    tree = ast.parse(module.read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            name = _called_name(node.exc.func)
            # A raised message builder is reported by the Call branch below.
            if (
                name not in CARRY_NO_MESSAGE
                and name not in MESSAGE_BUILDERS
                and any(_is_literal_text(arg) for arg in node.exc.args)
            ):
                found.append(f"line {node.lineno}: raise {name}(...)")

        if not isinstance(node, ast.Call):
            continue

        name = _called_name(node.func)
        if name in MESSAGE_BUILDERS and any(_is_literal_text(arg) for arg in node.args):
            found.append(f"line {node.lineno}: {name}(...)")
        for keyword in node.keywords:
            if keyword.arg == "action" and _is_literal_text(keyword.value):
                found.append(f"line {node.lineno}: {name}(action=...)")
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            is_log = node.func.attr in ("error", "warning") and node.func.value.id in (
                "log",
                "_log",
                "logger",
            )
            if is_log and node.args and _is_literal_text(node.args[0]):
                found.append(f"line {node.lineno}: {node.func.value.id}.{node.func.attr}(...)")
    return found


@pytest.mark.parametrize("module", SOURCES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_failure_messages_come_from_the_message_modules(module: Path) -> None:
    """A message shown on a failure belongs in `_messages`, never at the raise site.

    One condition then reads the same way whichever call path reached it, and the
    sync and async halves of the client cannot drift apart.
    """
    inline = _literal_messages(module)
    assert not inline, (
        f"{module.relative_to(ROOT)} spells out failure text in line:\n  "
        + "\n  ".join(inline)
        + "\nMove the wording into src/audioshake/_messages.py (or cli/_messages.py "
        "for CLI-only wording) and pass the built message instead."
    )


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_message_module_imports_no_package_modules(module: Path) -> None:
    """A message module may only reach for the standard library.

    Importing from the package would put it in a cycle with the modules that
    raise, since they all import it. Values that vary, environment variable
    names included, are passed in as arguments instead.
    """
    tree = ast.parse(module.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{module.name} imports relatively from {node.module}"
            assert not (node.module or "").startswith("audioshake")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("audioshake")


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_every_message_is_used(module: Path) -> None:
    """A message nobody raises is a message that has drifted out of the product."""
    # CLI messages are only for the CLI; library ones may be used anywhere.
    scope = module.parent
    others = [p for p in scope.rglob("*.py") if p != module]
    body = "\n".join(p.read_text() for p in others)
    unused = [name for name in _defined_names(module) if f"_messages.{name}" not in body]
    assert not unused, f"{module.name} defines unused messages: {unused}"


SAMPLE = """
def spells_them_out():
    raise ValidationError("in line")
    raise ValidationError(f"in {line}")
    raise click.BadParameter("in line", param_hint="SOURCE")
    logger.warning("in line")
    log.error("in line %s", detail)
    raise error_from_response(response, "get task")
    session.get_json("/tasks", action="list tasks")


def does_not():
    raise SystemExit(3)
    raise ValidationError(_messages.NO_MODELS)
    raise click.BadParameter(_messages.since_too_far_back(v), param_hint="SOURCE")
    logger.warning(_messages.INTERRUPTED)
    log.error(_messages.SOURCE_FAILED, label, reason)
    raise error_from_response(response, _messages.ACTION_GET_TASK)
    session.get_json("/tasks", action=_messages.ACTION_LIST_TASKS)
    logger.info("progress is not a failure, so it may stay here")
"""


def test_the_sweep_is_not_vacuous(tmp_path: Path) -> None:
    """Guard the detector itself: a typo above would quietly pass everything.

    Runs the real `_literal_messages` over a module written for the purpose, so
    this cannot drift from what the sweep above actually does.
    """
    for module in MODULES:
        assert len(_defined_names(module)) > 5
    assert len(SOURCES) > 15

    sample = tmp_path / "sample.py"
    sample.write_text(SAMPLE)
    found = _literal_messages(sample)

    # Seven in the first function; the second is how each should be written, and
    # a path argument and an INFO log are not failure text.
    assert len(found) == 7, found
    boundary = SAMPLE.splitlines().index("def does_not():") + 1
    assert all(int(hit.split()[1].rstrip(":")) < boundary for hit in found), found
