"""`skc` command-line interface.

Stage subcommands mirror the pipeline: ingest → enrich → classify → taxonomy →
graph → query. Each reads config from .env (override with flags). `ingest` is
implemented; later stages print their milestone until built.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from .config import load_settings
from .enrich import enrich_channel
from .ingest import ingest_channel
from .store import load_cursors, read_items

app = typer.Typer(
    name="skc",
    help="Slack Knowledge Curator — export, enrich, classify, and graph a Slack channel.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_NOT_YET = "[yellow]Not implemented yet[/] — see PRD.md milestone {m}."


def _resolve_channels(channel: str | None) -> list[str]:
    settings = load_settings()
    channels = [channel] if channel else settings.channel_ids
    if not channels:
        raise typer.BadParameter(
            "No channel given. Pass --channel C..., or set SLACK_CHANNELS in .env."
        )
    return channels


@app.command()
def ingest(
    channel: str = typer.Option(None, "--channel", "-c", help="Slack channel ID (C...)."),
    since: str = typer.Option(None, "--since", help="Only messages newer than this Slack ts."),
    incremental: bool = typer.Option(
        False, "--incremental", help="Resume from the stored cursor and merge."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch one page; write nothing."),
) -> None:
    """Export channel messages + threads to data/raw/<channel>.jsonl."""
    settings = load_settings()
    try:
        for ch in _resolve_channels(channel):
            result = ingest_channel(
                settings, ch, since=since, incremental=incremental, dry_run=dry_run
            )
            console.print(
                f"[bold green]✓[/] {result.channel}: {result.messages} messages, "
                f"{result.threads_expanded} threads, latest ts={result.latest_ts}"
            )
    except RuntimeError as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command()
def enrich(
    channel: str = typer.Option(None, "--channel", "-c"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve links; write nothing."),
) -> None:
    """Resolve X.com links (oEmbed) and page titles → data/enriched/."""
    settings = load_settings()
    try:
        for ch in _resolve_channels(channel):
            console.print(f"[bold]Enriching[/] {ch}")
            enrich_channel(settings, ch, dry_run=dry_run)
    except RuntimeError as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command()
def classify(
    channel: str = typer.Option(None, "--channel", "-c"),
    model: str = typer.Option(None, "--model", help="Override ANTHROPIC_MODEL."),
) -> None:
    """Summarize + classify each item with Claude → data/classified/."""
    console.print(_NOT_YET.format(m="M3"))


@app.command()
def taxonomy(
    discover: bool = typer.Option(False, "--discover", help="Run the discovery pass."),
    assign: bool = typer.Option(False, "--assign", help="Assign items to the taxonomy."),
) -> None:
    """Build/curate the emergent category taxonomy → data/taxonomy.json."""
    console.print(_NOT_YET.format(m="M4"))


@app.command()
def graph(action: str = typer.Argument("build", help="build | reset")) -> None:
    """Load the classified corpus into Cognee (pgGraph adapter)."""
    console.print(_NOT_YET.format(m="M5"))


@app.command()
def query(text: str = typer.Argument(..., help="Natural-language question.")) -> None:
    """Query the knowledge graph."""
    console.print(_NOT_YET.format(m="M5"))


@app.command()
def run(channel: str = typer.Option(None, "--channel", "-c")) -> None:
    """Run the full pipeline end-to-end."""
    console.print(_NOT_YET.format(m="M6"))


@app.command()
def status() -> None:
    """Show per-stage item counts and the incremental cursor."""
    settings = load_settings()
    cursors = load_cursors(settings.cursor_path)
    table = Table(title="skc status")
    table.add_column("channel")
    table.add_column("raw", justify="right")
    table.add_column("enriched", justify="right")
    table.add_column("classified", justify="right")
    table.add_column("cursor ts")

    channels = set(settings.channel_ids) | set(cursors)
    if not channels and settings.raw_dir.exists():
        channels = {p.stem for p in settings.raw_dir.glob("*.jsonl")}

    for ch in sorted(channels):
        raw = sum(1 for _ in read_items(settings.raw_dir / f"{ch}.jsonl"))
        enr = sum(1 for _ in read_items(settings.enriched_dir / f"{ch}.jsonl"))
        cls = sum(1 for _ in read_items(settings.classified_dir / f"{ch}.jsonl"))
        table.add_row(ch, str(raw), str(enr), str(cls), cursors.get(ch, "—"))

    console.print(table)


if __name__ == "__main__":
    app()
