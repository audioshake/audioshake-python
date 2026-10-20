"""Every module must import on every supported interpreter."""

import importlib

import pytest

MODULES = [
    "audioshake",
    "audioshake.aio",
    "audioshake.cli",
    "audioshake.cli.progress",
    "audioshake.cli.run",
    "audioshake.cli.models",
    "audioshake.cli.tasks",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    assert importlib.import_module(name)
