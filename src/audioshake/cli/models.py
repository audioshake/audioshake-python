"""``audioshake models``: what this account can run."""

from __future__ import annotations

import click

from ._output import emit_json, emit_table
from ._settings import Settings


@click.command()
@click.option("--all", "show_all", is_flag=True, help="Include models that need access requests")
@click.pass_obj
def models(settings: Settings, show_all: bool) -> None:
    """List available models with cost, output formats and limits."""
    with settings.client() as client:
        catalog = client.models.list(enabled_only=not show_all)

    if settings.json:
        emit_json([dict(model.raw) for model in catalog])
        return

    rows = []
    for model in catalog:
        limits = []
        if model.supported_languages:
            limits.append("language")
        if model.max_input_seconds:
            limits.append(f"≤ {model.max_input_seconds // 60} min")
        cost = f"{model.credits_per_minute:g}" if model.credits_per_minute is not None else "-"
        rows.append(
            (
                model.id,
                model.category,
                cost,
                " ".join(model.output_formats),
                ", ".join(limits),
                "" if model.enabled else model.access,
            )
        )
    emit_table(
        ["Model", "Category", "Credits/min", "Formats", "Options", "Access"],
        rows,
    )
