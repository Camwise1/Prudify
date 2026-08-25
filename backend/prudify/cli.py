"""Command line interface.

The server is the main event, but everything the server does is reachable from
the CLI too -- handy for cron jobs, for testing settings on one book before
turning the queue loose on 400 of them, and for people who do not want a UI.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

from . import __version__
from .config import config_path, load_config, save_config
from .core import matcher as matcher_mod
from .core import scanner
from .core.audio import FFmpegError, probe
from .core.pipeline import clean_part
from .logging_setup import configure_logging

app = typer.Typer(
    name="prudify",
    help="Self-hosted profanity filtering for audiobook libraries.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def serve(
    host: str = typer.Option(None, help="Override the configured bind address."),
    port: int = typer.Option(None, help="Override the configured port."),
) -> None:
    """Start the web UI and background queue."""
    import uvicorn

    from .main import create_app

    config = load_config()
    if host:
        config.server.host = host
    if port:
        config.server.port = port

    configure_logging(config.log_level, config.log_path())
    console.print(
        f"[bold cyan]Prudify {__version__}[/] -> "
        f"http://{'localhost' if config.server.host in ('0.0.0.0', '::') else config.server.host}"
        f":{config.server.port}{config.server.url_base}"
    )
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        log_config=None,
        access_log=False,
    )


@app.command()
def clean(
    source: Path = typer.Argument(..., exists=True, help="Audio file to clean."),
    output: Path = typer.Option(None, "--output", "-o", help="Destination file."),
    wordlist: str = typer.Option(None, help="Wordlist name (strict, moderate, ...)."),
    model: str = typer.Option(None, help="Whisper model, e.g. base.en or small.en."),
    mode: str = typer.Option(None, help="mute, beep, or cut."),
    pad_before: int = typer.Option(None, help="Milliseconds of padding before each hit."),
    pad_after: int = typer.Option(None, help="Milliseconds of padding after each hit."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report matches without writing."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Clean a single audio file, using the saved configuration as defaults."""
    config = load_config()
    configure_logging("DEBUG" if verbose else "WARNING", None)
    logging.getLogger().handlers = [logging.StreamHandler()] if verbose else []

    if wordlist:
        config.filtering.wordlist = wordlist
    if model:
        config.transcription.model = model
    if mode:
        config.output.mode = mode  # type: ignore[assignment]
    if pad_before is not None:
        config.filtering.pad_before_ms = pad_before
    if pad_after is not None:
        config.filtering.pad_after_ms = pad_after
    config.processing.dry_run = dry_run

    destination = output or source.with_name(f"{source.stem}-clean{source.suffix}")
    work_dir = config.resolved_work_dir() / "cli"

    info = probe(source)
    console.print(
        f"[bold]{source.name}[/]  "
        f"{_fmt_duration(info.duration)}  {info.codec}  "
        f"{info.chapter_count} chapters  {'cover' if info.has_cover else 'no cover'}"
    )

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
    ) as progress_ui:
        task = progress_ui.add_task("starting", total=1000)

        def on_progress(stage: str, fraction: float, message: str) -> None:
            # Fall back to the stage name so the bar never shows a stale or
            # empty label -- a long transcription with no message would
            # otherwise still read "probing".
            progress_ui.update(
                task,
                completed=int(fraction * 1000),
                description=(message or stage)[:40],
            )

        try:
            result = clean_part(
                source=source,
                destination=destination,
                config=config,
                work_dir=work_dir,
                progress=on_progress,
            )
        except FFmpegError as exc:
            # A wall of Python frames helps nobody diagnose an ffmpeg failure;
            # what matters is ffmpeg's own message.
            progress_ui.stop()
            console.print(f"[bold red]ffmpeg failed[/] (exit code {exc.returncode})")
            if exc.stderr:
                console.print(exc.stderr)
            else:
                console.print(
                    "[yellow]ffmpeg produced no output before exiting.[/] "
                    "Re-run with -v to see the exact command."
                )
            raise typer.Exit(code=1) from None

    if not result.ok:
        console.print(f"[bold red]Failed:[/] {result.reason}")
        raise typer.Exit(code=1)

    console.print(f"[bold green]{result.reason}[/]")
    if result.counts_by_word:
        table = Table("word", "count", title="Detected", show_edge=False)
        for word, count in result.counts_by_word.items():
            table.add_row(word, str(count))
        console.print(table)
    console.print(
        f"Muted {result.muted_seconds:.1f}s across {result.match_count} instances "
        f"in {_fmt_duration(result.elapsed_seconds)}"
    )
    if not dry_run:
        console.print(f"Wrote [cyan]{destination}[/]")


@app.command("scan")
def scan_cmd(
    path: Path = typer.Argument(..., exists=True, help="Library root to scan."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """List the books Prudify would find under a directory."""
    from .config import LibrarySettings

    library = LibrarySettings(
        name="cli", source_path=str(path), output_path=str(path.parent / "clean")
    )
    books = scanner.scan_library(library)

    if json_out:
        typer.echo(json.dumps([b.to_dict() for b in books], indent=2))
        return

    table = Table("Author", "Title", "Parts", "Formats", "Size")
    for book in books:
        table.add_row(
            book.author or "-",
            book.title,
            str(book.part_count),
            ", ".join(book.formats),
            _fmt_size(book.total_bytes),
        )
    console.print(table)
    console.print(f"[bold]{len(books)}[/] book(s) found")


@app.command("test-words")
def test_words(
    text: str = typer.Argument(..., help="Sentence to run through the matcher."),
    wordlist: str = typer.Option(None, help="Wordlist to test against."),
    mode: str = typer.Option(None, help="exact, prefix, or fuzzy."),
) -> None:
    """Show which words in a sentence would be silenced."""
    from .core.transcribe import Word

    config = load_config()
    if wordlist:
        config.filtering.wordlist = wordlist
    if mode:
        config.filtering.match_mode = mode  # type: ignore[assignment]

    matcher = matcher_mod.build_matcher_from_settings(
        config.filtering, user_dir=config.resolved_data_dir() / "wordlists"
    )
    tokens = text.split()
    words = [Word(start=i, end=i + 0.5, text=t) for i, t in enumerate(tokens)]
    matches = matcher.find(words)
    hits = {m.word_index + o for m in matches for o in range(len(m.text.split()))}

    rendered = " ".join(
        f"[bold red on white]{token}[/]" if i in hits else token
        for i, token in enumerate(tokens)
    )
    console.print(rendered)
    console.print(f"[bold]{len(matches)}[/] match(es)")


@app.command("wordlists")
def list_wordlists() -> None:
    """List bundled wordlists and their rule counts."""
    table = Table("Name", "Rules", "Path")
    for name in matcher_mod.available_wordlists():
        path = matcher_mod.bundled_wordlist_dir() / f"{name}.txt"
        rules = matcher_mod.load_wordlist_file(path)
        table.add_row(name, str(len(rules)), str(path))
    console.print(table)


@app.command("config")
def show_config(
    reveal_key: bool = typer.Option(False, "--reveal-key", help="Print the API key."),
) -> None:
    """Show where configuration lives and the key settings in effect."""
    config = load_config()
    console.print(f"Config file: [cyan]{config_path(config.resolved_data_dir())}[/]")
    console.print(f"Data dir:    [cyan]{config.resolved_data_dir()}[/]")
    console.print(f"Work dir:    [cyan]{config.resolved_work_dir()}[/]")
    console.print(f"Database:    [cyan]{config.database_path()}[/]")
    console.print(
        f"Engine:      {config.transcription.engine} / {config.transcription.model}"
    )
    console.print(f"Wordlist:    {config.filtering.wordlist} ({config.filtering.match_mode})")
    console.print(f"Output mode: {config.output.mode}")
    console.print(f"Libraries:   {len(config.libraries)}")
    if reveal_key:
        console.print(f"API key:     [yellow]{config.server.api_key}[/]")


@app.command("add-library")
def add_library(
    source: Path = typer.Argument(..., exists=True, help="Source library root."),
    output: Path = typer.Argument(..., help="Where cleaned copies are written."),
    name: str = typer.Option("Audiobooks", help="Display name."),
    auto: bool = typer.Option(True, help="Automatically process new books."),
) -> None:
    """Register a library without opening the UI."""
    from .config import LibrarySettings

    config = load_config()
    if source.resolve() == output.resolve():
        console.print("[red]Source and output must be different directories.[/]")
        raise typer.Exit(code=1)
    output.mkdir(parents=True, exist_ok=True)
    library = LibrarySettings(
        name=name, source_path=str(source), output_path=str(output), auto_process=auto
    )
    config.libraries.append(library)
    save_config(config)
    console.print(f"Added library [bold]{name}[/] ({library.id})")


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"Prudify {__version__}  (python {sys.version.split()[0]})")


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s" if hours else f"{minutes}m{secs:02d}s"


def _fmt_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


if __name__ == "__main__":
    app()


auth_app = typer.Typer(help="Manage the login account.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")


@auth_app.command("set-password")
def auth_set_password(
    username: str = typer.Option(None, help="Username to set (keeps the current one if omitted)."),
    password: str = typer.Option(
        None,
        prompt="New password",
        hide_input=True,
        confirmation_prompt=True,
        help="Prompted for if not supplied. Avoid passing it on the "
        "command line -- it lands in your shell history.",
    ),
    sign_out: bool = typer.Option(
        True, "--sign-out/--keep-sessions", help="Invalidate existing sessions."
    ),
) -> None:
    """Create or reset the login account.

    This is the way back in when you are locked out. Prudify runs on your own
    hardware and has no email, so there is no self-service reset -- physical
    or shell access to the server *is* the recovery mechanism.
    """
    from .security import hash_password

    if len(password) < 8:
        console.print("[bold red]Password must be at least 8 characters.[/]")
        raise typer.Exit(code=1)

    config = load_config()
    if username:
        config.auth.username = username.strip()
    elif not config.auth.username:
        console.print("[bold red]No username set. Pass --username.[/]")
        raise typer.Exit(code=1)

    config.auth.password_hash = hash_password(password)
    if sign_out:
        config.auth.session_epoch += 1
    if config.auth.method in ("none", "apikey"):
        config.auth.method = "forms"
        console.print("Authentication method switched to [cyan]forms[/] (login page).")

    save_config(config)
    console.print(f"[bold green]Password set for[/] [cyan]{config.auth.username}[/]")
    if sign_out:
        console.print("Existing sessions were signed out.")


@auth_app.command("method")
def auth_method(
    value: str = typer.Argument(
        ..., help="none, apikey, basic, forms, or external."
    ),
) -> None:
    """Change how browsers authenticate."""
    allowed = {"none", "apikey", "basic", "forms", "external"}
    if value not in allowed:
        console.print(f"[bold red]Must be one of:[/] {', '.join(sorted(allowed))}")
        raise typer.Exit(code=1)

    config = load_config()
    config.auth.method = value  # type: ignore[assignment]
    save_config(config)
    console.print(f"Authentication method is now [cyan]{value}[/]")
    if value == "none":
        console.print(
            "[bold yellow]Warning:[/] anyone who can reach this port now has full access."
        )
    elif value in ("basic", "forms") and not config.auth.configured:
        console.print("No account exists yet. Run [cyan]prudify auth set-password[/] next.")


@auth_app.command("status")
def auth_status_cmd() -> None:
    """Show the current authentication configuration."""
    config = load_config()
    table = Table("Setting", "Value", show_edge=False)
    table.add_row("Method", config.auth.method)
    table.add_row("Required", config.auth.required)
    table.add_row("Account", config.auth.username or "[dim]none[/]")
    table.add_row("Password set", "yes" if config.auth.password_hash else "[dim]no[/]")
    table.add_row("Session lifetime", f"{config.auth.session_lifetime_hours}h")
    table.add_row("API key set", "yes" if config.server.api_key else "[dim]no[/]")
    if config.auth.method == "external":
        table.add_row("Trusted proxies", ", ".join(config.auth.trusted_proxies) or "[red]none[/]")
        table.add_row("User header", config.auth.proxy_user_header)
    console.print(table)
    if config.auth.needs_setup:
        console.print("\n[yellow]No account yet.[/] Open the web UI, or run "
                      "[cyan]prudify auth set-password[/].")


@auth_app.command("sign-out-everywhere")
def auth_sign_out() -> None:
    """Invalidate every existing session without changing the password."""
    config = load_config()
    config.auth.session_epoch += 1
    save_config(config)
    console.print("[bold green]All sessions signed out.[/]")
