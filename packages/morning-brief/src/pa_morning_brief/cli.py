from __future__ import annotations

from datetime import date

import typer
from pa_core.harness.run import run_agent

from .agent import AGENT, build

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Morning brief agent.")


@app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute and print; touch nothing."),
    force: bool = typer.Option(False, "--force", help="Regenerate even if today already ran."),
    for_date: str = typer.Option(None, "--date", help="logical date, YYYY-MM-DD."),
    mail_limit: int = typer.Option(60, help="Max messages to consider."),
    timeout: int = typer.Option(900, help="Hard wall-clock limit, seconds."),
    log_level: str = typer.Option("INFO"),
) -> None:
    """Generate today's brief."""
    rc = run_agent(
        AGENT,
        lambda ctx: build(ctx, mail_limit=mail_limit),
        logical_date=date.fromisoformat(for_date) if for_date else None,
        dry_run=dry_run,
        force=force,
        timeout_s=timeout,
        log_level=log_level,
    )
    raise typer.Exit(rc)


@app.command()
def show(for_date: str = typer.Option(None, "--date")) -> None:
    """Print the most recent brief that was written to disk."""
    from pa_core.config import load

    cfg = load()
    d = cfg.outputs / AGENT
    files = sorted(d.rglob("*.md"))
    if for_date:
        files = [f for f in files if f.name.startswith(for_date)]
    if not files:
        typer.echo(f"no briefs found under {d}")
        raise typer.Exit(1)
    typer.echo(files[-1].read_text())
