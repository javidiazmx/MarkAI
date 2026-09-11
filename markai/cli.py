"""The ``mark`` command line: set up, ingest, ask, serve.

Heavy modules are imported inside the command functions so ``mark --help`` stays instant and
still works before the rest of the project is configured.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    name="mark",
    help="Mark: a Chicagoland landlord advisor grounded only in your curated sources.",
    no_args_is_help=True,
    add_completion=False,
)
sources_app = typer.Typer(help="Inspect and validate sources/sources.yaml.", no_args_is_help=True)
calc_app = typer.Typer(help="Run the deal calculators from the terminal.", no_args_is_help=True)
facts_app = typer.Typer(
    help="The ordinances and cost ranges you maintain by hand.", no_args_is_help=True
)
accounts_app = typer.Typer(
    help="The landlords who created an account on the page.", no_args_is_help=True
)
leads_app = typer.Typer(help="Leads on their way to your CRM.", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(calc_app, name="calc")
app.add_typer(facts_app, name="facts")
app.add_typer(accounts_app, name="accounts")
app.add_typer(leads_app, name="leads")


def _printable(stream: Any) -> Any:
    """Make a stream that cannot kill a command over one character.

    The owner runs this in a Windows console, where the code page is often cp1252 and a
    "✓" is not in it. Every command here ends with one, so `mark report` wrote its file,
    printed the confirmation, and died on the tick - a crash after the work was done, which
    reads as total failure and is not.

    UTF-8 first, because Windows Terminal renders it properly. ``errors="replace"`` is the
    part that matters: on a console that cannot be switched, a character it has no glyph for
    becomes a "?" instead of a traceback.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return stream
    for encoding in ("utf-8", None):
        try:
            reconfigure(encoding=encoding, errors="replace")
            return stream
        except Exception:
            continue
    return stream


# Fixed up in place rather than handed to the Console, which resolves ``sys.stdout`` at
# write time: binding it here would cut out anything that swaps the stream afterwards.
_printable(sys.stdout)
console = Console()
err = Console(stderr=True)

PLACEHOLDERS = {"", "sk-ant-...", "sk-ant-xxx", "your-key-here", "changeme"}
LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0.localhost"}


def _fail(message: str, hint: str | None = None) -> None:
    err.print(f"[bold red]✗[/bold red] {message}")
    if hint:
        err.print(f"  [dim]{hint}[/dim]")
    raise typer.Exit(code=1)


def _settings() -> Any:
    from markai.config import get_settings

    settings = get_settings()
    settings.ensure_dirs()
    return settings


def _manifest(settings: Any) -> Any:
    from markai.sources.manifest import load_manifest

    try:
        return load_manifest(settings.sources_file)
    except FileNotFoundError:
        _fail(
            f"No sources file at {settings.sources_file}.",
            "Run `mark init` to create one, then list your websites, videos and podcast in it.",
        )
    except Exception as exc:
        _fail(f"sources.yaml is not valid: {exc}", _yaml_hint(exc))


def _env_line_state(env_path: Any, name: str) -> str:
    """Why a key is missing, without ever revealing its value."""
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return f"no .env file, so {name} is not set"

    commented = False
    for raw in lines:
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.lstrip("#").strip() != name:
            continue
        if line.startswith("#"):
            commented = True
            continue
        value = value.strip()
        if not value:
            return f"{name} is in .env but has no value after the ="
        if value[0] in "\"'" or value[-1] in "\"'":
            return f"{name} has quotes around it; remove them"
        return f"{name} looks set but was not loaded; check for stray spaces"
    if commented:
        return f"the {name} line in .env still starts with #; delete the #"
    return f"there is no {name} line in .env"


def _yaml_hint(exc: Exception) -> str:
    """Turn a YAML parser complaint into something an owner can act on."""
    message = str(exc)
    if "\\t" in message or "tab" in message.lower():
        return (
            "There is a Tab character in the file. YAML only accepts spaces. Replace every Tab "
            "with spaces, then run `mark sources validate` again."
        )
    if "mapping values are not allowed" in message:
        return (
            "A line is probably missing its `- url:` prefix, or a value with a colon in it needs "
            "quotes. Compare against sources/sources.example.yaml."
        )
    return "Fix the file and run `mark sources validate`."


def _store(settings: Any) -> Any:
    from markai.knowledge.store import KnowledgeStore

    return KnowledgeStore(settings.db_path)


def _advisor(settings: Any, manifest: Any, store: Any) -> Any:
    """Build the advisor for a terminal session.

    The terminal has no welcome screen to carry a standing notice, so a legal answer here
    always ends with the disclaimer regardless of the setting the browser page relies on.
    """
    settings = settings.model_copy(update={"legal_disclaimer_in_answers": True})
    from markai.advisor.mark import MarkAdvisor, MissingApiKeyError
    from markai.advisor.prompt_builder import load_system_prompt
    from markai.knowledge.embeddings import build_embedder
    from markai.knowledge.retriever import Retriever
    from markai.sources.facts import facts_path, load_facts

    retriever = Retriever(store, build_embedder(settings), settings)
    try:
        prompt = load_system_prompt(settings.system_prompt_path, settings.show_citations)
    except FileNotFoundError as exc:
        _fail(str(exc))
    try:
        return MarkAdvisor(
            settings,
            retriever,
            manifest.tools,
            prompt,
            business=manifest.business,
            store=store,
            facts=load_facts(facts_path(settings.sources_file)),
        )
    except MissingApiKeyError as exc:
        _fail(str(exc), "Get a key at https://console.anthropic.com/ and run `mark init`.")


# --------------------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------------------


@app.command()
def init(
    api_key: str = typer.Option(None, "--api-key", help="Anthropic API key (skips the prompt)."),
    no_input: bool = typer.Option(False, "--no-input", help="Never prompt; write what you have."),
) -> None:
    """Create .env and sources/sources.yaml, and make the data folders."""
    from markai.config import PROJECT_ROOT

    env_path = PROJECT_ROOT / ".env"
    example_path = PROJECT_ROOT / ".env.example"

    if env_path.exists():
        console.print(f"[dim]Keeping the existing {env_path.name}.[/dim]")
    else:
        key = api_key
        if not key and not no_input:
            key = typer.prompt("Anthropic API key (input hidden)", hide_input=True, default="")
        key = (key or "").strip()
        if key in PLACEHOLDERS and not no_input:
            err.print(
                "[yellow]No key entered. Add ANTHROPIC_API_KEY to .env before you "
                "ask Mark anything.[/yellow]"
            )
        body = example_path.read_text(encoding="utf-8") if example_path.exists() else ""
        body = body.replace("ANTHROPIC_API_KEY=", f"ANTHROPIC_API_KEY={key}", 1)
        env_path.write_text(body, encoding="utf-8")
        try:
            env_path.chmod(0o600)
        except OSError:
            pass
        console.print(f"[green]✓[/green] Wrote {env_path} (readable only by you).")

    settings = _settings()
    if settings.sources_file.exists():
        console.print(f"[dim]Keeping the existing {settings.sources_file.name}.[/dim]")
    else:
        from markai.sources.template import SOURCES_TEMPLATE

        settings.sources_file.parent.mkdir(parents=True, exist_ok=True)
        settings.sources_file.write_text(SOURCES_TEMPLATE, encoding="utf-8")
        console.print(f"[green]✓[/green] Wrote {settings.sources_file}.")

    console.print(f"[green]✓[/green] Data folders ready under {settings.data_dir}.")
    console.print(
        Panel.fit(
            "1. Open [bold]sources/sources.yaml[/bold] and list your websites, YouTube "
            "episodes and podcast.\n"
            "2. Run [bold]mark ingest[/bold] to build the knowledge base.\n"
            "3. Run [bold]mark chat[/bold] (terminal) or [bold]mark serve[/bold] (browser).",
            title="Next steps",
        )
    )


@app.command()
def doctor(
    online: bool = typer.Option(False, "--online", help="Also make one tiny live API call."),
) -> None:
    """Check that everything Mark needs is in place."""
    from markai.config import PROJECT_ROOT, get_settings

    settings = get_settings()
    table = Table(title="Mark checkup", show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Result")

    env_path = PROJECT_ROOT / ".env"
    table.add_row(".env file", "found" if env_path.exists() else "[yellow]missing[/yellow]")
    table.add_row(
        "Anthropic key", "set" if settings.anthropic_key() else "[red]not set[/red] (run mark init)"
    )
    if settings.voyage_key():
        table.add_row("Embeddings", f"on ({settings.embedding_model})")
    else:
        table.add_row(
            "Embeddings",
            f"off (keyword search only)\n[dim]{_env_line_state(env_path, 'VOYAGE_API_KEY')}[/dim]",
        )

    unblock = settings.youtube_unblock_method()
    table.add_row(
        "YouTube access",
        "direct (if it blocks you, set a proxy or cookies)"
        if unblock == "none"
        else f"via {unblock}",  # the method, never the secret
    )

    from markai.sources.manifest import load_manifest

    try:
        manifest = load_manifest(settings.sources_file)
        counts = manifest.counts()
        listed = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items() if v)
        table.add_row("sources.yaml", f"valid, {listed}" if listed else "valid, but empty")
        for warning in manifest.warnings():
            table.add_row("[yellow]Warning[/yellow]", warning)
    except FileNotFoundError:
        table.add_row("sources.yaml", "[red]missing[/red] (run mark init)")
    except Exception as exc:
        # Report it here rather than aborting: the rest of the checkup is still useful.
        table.add_row("sources.yaml", f"[red]not valid[/red]\n{exc}\n\n{_yaml_hint(exc)}")

    try:
        settings.ensure_dirs()
        probe = settings.data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        table.add_row("Data folder", f"writable ({settings.data_dir})")
    except Exception as exc:
        table.add_row("Data folder", f"[red]{exc}[/red]")

    store = _store(settings)
    stats = store.stats()
    table.add_row(
        "Knowledge base",
        f"{stats.chunks} chunks from {sum(stats.documents_by_kind.values())} sources",
    )
    store.close()

    # --- the browser page: who can reach it, who signs up, where the lead goes ---------
    if settings.access_code():
        table.add_row("Web access code", "set")
    else:
        table.add_row(
            "Web access code",
            "[yellow]not set[/yellow] - anyone who reaches the page can ask\n"
            "[dim]Set MARKAI_WEB_ACCESS_CODE before sharing the address.[/dim]",
        )

    if settings.account_required:
        table.add_row(
            "Lead form",
            f"{settings.free_questions_before_signup} free question(s), then the form\n"
            "[dim]name, email, phone. No password.[/dim]",
        )
    else:
        table.add_row("Lead form", "off, nobody is ever asked for their details")

    accounts_path = settings.data_dir / "accounts.db"
    if accounts_path.exists():
        from markai.web.accounts import Accounts

        signups = Accounts(accounts_path, free_questions=settings.free_questions_before_signup)
        total = len(signups.all())
        legacy = signups.legacy_signups()
        signups.close()
        note = f"{total} signup(s)"
        if legacy:
            note += f"\n[dim]{legacy} from an earlier version, kept in signups_v1[/dim]"
        table.add_row("Signups so far", note)

    from markai.web.crm import Crm, sender_from_settings

    sender, describe = sender_from_settings(settings)
    queue = Crm(settings.data_dir / "leads.db")
    counts = queue.counts()
    queue.close()
    tally = (
        f"[dim]{counts['delivered']} delivered · {counts['waiting']} waiting · "
        f"{counts['gave_up']} gave up[/dim]"
    )
    if sender is not None:
        note = f"{escape(describe)}\n{tally}"
        if counts["waiting"] or counts["gave_up"]:
            note += "\n[dim]`mark leads send` pushes them.[/dim]"
        table.add_row("Leads go to", note)
    else:
        why = escape(describe) if describe else "nowhere set"
        table.add_row(
            "Leads go to",
            f"[yellow]{why}[/yellow] - they queue and wait\n{tally}\n"
            "[dim]MARKAI_LEAD_EMAIL_TO plus MARKAI_SMTP_*, then `mark leads test`.[/dim]",
        )

    host = settings.web_host
    if settings.cookie_secure:
        table.add_row("Sign-in cookie", "https only")
    elif host in LOOPBACK:
        table.add_row("Sign-in cookie", f"plain http, fine on {host}")
    else:
        table.add_row(
            "Sign-in cookie",
            "[red]plain http on a public host[/red]\n"
            "[dim]Set MARKAI_COOKIE_SECURE=true once this is behind https.[/dim]",
        )

    try:
        import faster_whisper  # noqa: F401

        table.add_row("Local transcription", "installed")
    except ImportError:
        table.add_row(
            "Local transcription", escape('not installed (pip install "markai[transcribe]")')
        )

    if online:
        table.add_row("Live API call", _probe_api(settings))

    console.print(table)


def _probe_api(settings: Any) -> str:
    import anthropic

    key = settings.anthropic_key()
    if not key:
        return "[red]skipped: no API key[/red]"
    try:
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(
            model=settings.model, max_tokens=16, messages=[{"role": "user", "content": "hi"}]
        )
        return f"ok ({settings.model} responded)"
    except Exception as exc:
        return f"[red]{type(exc).__name__}: {exc}[/red]"


# --------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------


def _check_reachable(urls: list[tuple[str, str]]) -> None:
    """Say which listed sites actually answer. A typo'd domain is cheap to find here."""
    import socket
    from urllib.parse import urlsplit

    table = Table(title="Reachability", show_header=True, header_style="bold")
    table.add_column("Where")
    table.add_column("Host")
    table.add_column("Result")
    problems = 0
    for label, url in urls:
        host = urlsplit(url).hostname or ""
        try:
            socket.getaddrinfo(host, None)
        except OSError:
            problems += 1
            table.add_row(label, escape(host), "[red]does not resolve[/red] - check for a typo")
            continue
        table.add_row(label, escape(host), "resolves")
    console.print(table)
    if problems:
        console.print(
            f"[yellow]{problems} host(s) do not resolve. Those entries will fetch nothing.[/yellow]"
        )


@sources_app.command("validate")
def sources_validate(
    check_urls: bool = typer.Option(
        False, "--check-urls", help="Also confirm every listed domain resolves."
    ),
) -> None:
    """Check that sources.yaml parses and report what it contains."""
    settings = _settings()
    manifest = _manifest(settings)
    counts = manifest.counts()
    console.print(f"[green]✓[/green] {settings.sources_file} is valid.")
    for key, value in counts.items():
        console.print(f"  {key.replace('_', ' ')}: {value}")
    if manifest.is_empty():
        console.print("[yellow]No sources listed yet. Mark has nothing to learn from.[/yellow]")
    for warning in manifest.warnings():
        console.print(f"[yellow]![/yellow] {escape(warning)}")

    if check_urls:
        targets = [(f"websites[{i}]", w.url) for i, w in enumerate(manifest.websites)]
        targets += [(f"youtube.channels[{i}]", c) for i, c in enumerate(manifest.youtube.channels)]
        if manifest.podcast.rss:
            targets.append(("podcast.rss", manifest.podcast.rss))
        console.print()
        _check_reachable(targets)


@sources_app.command("probe")
def sources_probe(
    url: str = typer.Argument(..., help="One URL to fetch and report on."),
    show: bool = typer.Option(False, "--show", help="Print the first 40 lines of the text."),
) -> None:
    """Fetch one URL and say exactly what came back.

    Answers "why did this site bring nothing?" in one request instead of a whole crawl.
    """
    import httpx

    from markai.ingest.websites import (
        USER_AGENT,
        RobotsCache,
        canonical_url,
        discover_links,
        extract_main_text,
        extract_pdf_text,
        fetch_page,
    )
    from markai.models import IngestError

    settings = _settings()
    rows: list[tuple[str, str]] = []
    with httpx.Client(
        follow_redirects=True, timeout=30.0, headers={"User-Agent": USER_AGENT}
    ) as client:
        allowed = RobotsCache(client).can_fetch(url)
        rows.append(("robots.txt", "allows it" if allowed else "[red]disallows it[/red]"))
        try:
            fetched = fetch_page(url, client, max_bytes=settings.max_page_bytes)
        except IngestError as exc:
            rows.append(("fetch", f"[red]{escape(str(exc))}[/red]"))
            if exc.hint:
                rows.append(("hint", escape(exc.hint)))
            _print_probe(url, rows)
            raise typer.Exit(1) from None

        is_pdf = fetched.pdf is not None
        is_feed = not is_pdf and fetched.html.lstrip()[:600].lower().find("<rss") != -1
        rows.append(("final url", escape(fetched.final_url)))
        rows.append(("format", "PDF" if is_pdf else "RSS feed" if is_feed else "HTML"))
        rows.append(("size", f"{len(fetched.pdf or fetched.html.encode()):,} bytes"))
        if fetched.truncated:
            rows.append(("truncated", "yes - hit MARKAI_MAX_PAGE_BYTES"))
        if is_feed:
            _describe_feed(fetched.html, rows)
            _print_probe(url, rows)
            return
        try:
            if is_pdf:
                title, text = extract_pdf_text(fetched.pdf or b"", fetched.final_url)
            else:
                title, text = extract_main_text(fetched.html, fetched.final_url)
        except IngestError as exc:
            rows.append(("extract", f"[red]{escape(str(exc))}[/red]"))
            _print_probe(url, rows)
            raise typer.Exit(1) from None

        rows.append(("title", escape(title[:70])))
        words = len(text.split())
        rows.append(
            ("text", f"{words:,} words" if words else "[red]none - probably JavaScript[/red]")
        )
        links = discover_links(fetched.html, fetched.final_url, [], []) if not is_pdf else []
        rows.append(("links to crawl", f"{len(links)} on the same host"))
        rows.append(("stored as", escape(canonical_url(fetched.final_url))))

    _print_probe(url, rows)
    if show and text:
        console.print()
        console.print(escape("\n".join(text.splitlines()[:40])))


def _describe_feed(xml: str, rows: list[tuple[str, str]]) -> None:
    """A show's own feed is the authority on where its website lives."""
    import feedparser

    parsed = feedparser.parse(xml)
    feed = parsed.feed
    rows.append(("show", escape(str(feed.get("title", "") or "?"))))
    site = str(feed.get("link", "") or "")
    rows.append(("website", escape(site) if site else "[yellow]the feed names none[/yellow]"))
    rows.append(("episodes", str(len(parsed.entries))))
    for entry in parsed.entries[:3]:
        rows.append(("  episode page", escape(str(entry.get("link", "") or "?"))))


def _print_probe(url: str, rows: list[tuple[str, str]]) -> None:
    table = Table(title=f"Probe: {escape(url)}", show_header=False)
    table.add_column("", style="bold")
    table.add_column("")
    for name, value in rows:
        table.add_row(name, value)
    console.print(table)


@sources_app.command("missing")
def sources_missing(
    url: str = typer.Argument(..., help="The site to check, e.g. https://www.gcrealtyinc.com"),
    section: str = typer.Option(
        None, "--section", help='Only this part of the site, e.g. "/blog".'
    ),
    write: str = typer.Option(
        None, "--write", help="Write the missing URLs to this file, one per line."
    ),
) -> None:
    """List pages the site publishes that are not in the knowledge base.

    Counting what was stored cannot tell a complete crawl from one that missed twenty posts.
    The site's own sitemap can.
    """
    import httpx

    from markai.ingest.websites import USER_AGENT
    from markai.sitemap import diff_against_store

    settings = _settings()
    store = _store(settings)
    locators = store.list_locators()
    store.close()

    console.print(f"[dim]Reading the sitemap for {escape(url)}…[/dim]")
    with httpx.Client(
        follow_redirects=True, timeout=60.0, headers={"User-Agent": USER_AGENT}
    ) as client:
        diff = diff_against_store(url, locators, client, section)

    if not diff.listed:
        _fail(
            "The sitemap listed nothing for that site or section.",
            f"Tried: {', '.join(diff.sitemaps[:3])}. Some sites publish none, in which case "
            "this check cannot be made.",
        )

    console.print(
        f"[bold]{len(diff.listed):,}[/bold] pages listed"
        + (f" under {escape(section)}" if section else "")
        + f" · [green]{len(diff.stored):,} stored[/green]"
        + (f" · [red]{len(diff.missing):,} missing[/red]" if diff.missing else "")
        + f" ({diff.coverage:.0%})"
    )

    if not diff.missing:
        console.print("[green]✓[/green] Everything the site lists is in the knowledge base.")
        return

    from markai.sitemap import reasons_from_last_run

    reasons = reasons_from_last_run(settings.data_dir / "last-ingest.txt", diff.missing)
    for missing in diff.missing[:25]:
        console.print(f"  [red]-[/red] {escape(missing)}")
        reason = reasons.get(missing.rstrip("/"))
        if reason:
            console.print(f"      [dim]{escape(reason)}[/dim]")
    if len(diff.missing) > 25:
        console.print(f"  [dim]… and {len(diff.missing) - 25:,} more[/dim]")
    unexplained = len(diff.missing) - sum(1 for m in diff.missing if m.rstrip("/") in reasons)
    if reasons and unexplained:
        console.print(
            f"  [dim]{unexplained:,} of them were never attempted - the last run did not "
            f"reach them at all.[/dim]"
        )

    if write:
        path = Path(write)
        path.write_text("\n".join(diff.missing) + "\n", encoding="utf-8")
        console.print(f"\nWrote {len(diff.missing):,} URLs to {escape(str(path))}.")
        console.print(
            "[dim]Add them to sources.yaml as entries with crawl: false, then "
            "`mark ingest --only website`.[/dim]"
        )
    raise typer.Exit(1)


@sources_app.command("list")
def sources_list() -> None:
    """List what is currently in the knowledge base."""
    settings = _settings()
    store = _store(settings)
    documents = store.list_documents()
    if not documents:
        console.print("Nothing ingested yet. Run [bold]mark ingest[/bold].")
        store.close()
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Kind")
    table.add_column("Title")
    table.add_column("Episode")
    table.add_column("Date")
    for doc in documents:
        table.add_row(
            doc.kind.value,
            escape(doc.title[:60]),
            escape(doc.episode or ""),
            doc.published_at or "",
        )
    console.print(table)
    store.close()


@sources_app.command("match")
def sources_match() -> None:
    """Show which transcript file (if any) each podcast episode resolved to."""
    from markai.ingest.podcast import match_transcript_file, resolve_transcript_plan

    settings = _settings()
    manifest = _manifest(settings)
    if not (manifest.podcast.rss or manifest.podcast.episodes):
        console.print("No podcast configured in sources.yaml.")
        return
    from markai.models import IngestError

    try:
        plan = resolve_transcript_plan(manifest.podcast, settings)
    except IngestError as exc:
        _fail(f"Could not read the podcast feed: {exc}", exc.hint)
    table = Table(title="Podcast transcript matching", show_header=True, header_style="bold")
    table.add_column("Episode")
    table.add_column("Title")
    table.add_column("Source of transcript")
    for episode, method in plan:
        detail = method.replace("_", " ")
        if method == "transcript_file":
            path = match_transcript_file(
                episode, settings.podcast_transcripts_dir, settings.project_root
            )
            detail = path.name if path else detail
        table.add_row(escape(episode.episode or ""), escape((episode.title or "")[:50]), detail)
    console.print(table)


# --------------------------------------------------------------------------------------
# Ingest and status
# --------------------------------------------------------------------------------------


@app.command()
def ingest(
    only: list[str] = typer.Option(
        None, "--only", help="Limit to website, youtube or podcast (repeatable)."
    ),
    force: bool = typer.Option(False, "--force", help="Re-ingest sources even if unchanged."),
    prune: bool = typer.Option(
        False, "--prune", help="Delete stored sources no longer listed in sources.yaml."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan and stop."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the transcription confirmation."),
    no_transcribe: bool = typer.Option(
        False, "--no-transcribe", help="Never transcribe audio; use transcripts only."
    ),
) -> None:
    """Fetch every source in sources.yaml and build the knowledge base."""
    from markai.ingest.pipeline import plan_ingest, run_ingest
    from markai.knowledge.embeddings import build_embedder
    from markai.models import SourceKind

    settings = _settings()
    manifest = _manifest(settings)
    if manifest.is_empty():
        _fail(
            "sources.yaml has no sources yet.",
            "Add at least one website, YouTube episode or podcast feed, then run this again.",
        )

    kinds: set[Any] | None = None
    if only:
        try:
            kinds = {SourceKind(value.lower()) for value in only}
        except ValueError:
            _fail("--only takes website, youtube or podcast.")

    plan = plan_ingest(manifest, settings, kinds)
    console.print(plan.summary_table())

    if dry_run:
        return
    if no_transcribe:
        allow = False
        if plan.needs_transcription():
            console.print(
                f"[dim]Skipping transcription for "
                f"{plan.podcast_by_method.get('audio_transcribe', 0)} episodes (--no-transcribe)."
                "[/dim]"
            )
    elif plan.needs_transcription() and not yes:
        hours = plan.transcription_minutes / 60.0
        console.print(
            f"[yellow]{plan.podcast_by_method.get('audio_transcribe', 0)} episodes have no "
            f"transcript. Transcribing them locally takes roughly {hours:.1f} hours.[/yellow]"
        )
        if not typer.confirm("Transcribe them now?", default=False):
            console.print("Skipping transcription. Everything else will still be ingested.")
            allow = False
        else:
            allow = True
    else:
        allow = True

    store = _store(settings)
    report = run_ingest(
        manifest,
        store,
        build_embedder(settings),
        settings,
        only=kinds,
        force=force,
        prune=prune,
        allow_transcription=allow,
        log=lambda message: console.print(f"[dim]{escape(message)}[/dim]"),
    )
    console.print(report.summary_table())
    details = report.write_details(settings.data_dir / "last-ingest.txt")
    console.print(f"[dim]Full details, page by page: {details}[/dim]")
    store.close()


def _looks_like_a_rate_limit_message(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in ("rate limit", "429", "too many requests", " rpm", " tpm"))


@app.command()
def audit(
    show_probes: bool = typer.Option(
        False, "--probes", help="Print every probe question and what it found."
    ),
) -> None:
    """Check the knowledge base is actually usable, without asking Claude anything.

    Ingest counts say what was stored, not whether a landlord's question finds it.
    """
    from markai.audit import audit as run_audit
    from markai.knowledge.embeddings import build_embedder
    from markai.knowledge.retriever import Retriever

    settings = _settings()
    manifest = _manifest(settings)
    store = _store(settings)
    retriever = Retriever(store, build_embedder(settings), settings)

    console.print("[bold]Auditing the knowledge base[/bold]")
    report = run_audit(
        store, manifest, retriever, lambda message: console.print(f"[dim]  {message}[/dim]")
    )
    store.close()
    console.print()

    shape = Table(title="What is stored", show_header=True, header_style="bold")
    shape.add_column("Item")
    shape.add_column("Value", justify="right")
    shape.add_row("Documents", f"{report.documents:,}")
    for kind, count in sorted(report.by_kind.items()):
        shape.add_row(f"  {kind}", f"{count:,}")
    shape.add_row("Passages", f"{report.chunks:,}")
    embedded = (
        f"{report.embedded:,} ({report.embedded / report.chunks:.0%})" if report.chunks else "0"
    )
    shape.add_row("  with embeddings", embedded)
    console.print(shape)
    console.print()

    if report.source_coverage:
        sources = Table(title="Pages per listed source", show_header=True, header_style="bold")
        sources.add_column("Source")
        sources.add_column("Stored", justify="right")
        for name, count in report.source_coverage:
            marker = "[red]0 - nothing[/red]" if count == 0 else f"{count:,}"
            sources.add_row(escape(name), marker)
        console.print(sources)
        console.print()

    if report.probes:
        answered = sum(1 for _, _, coverage, _ in report.probes if coverage != "none")
        console.print(
            f"[bold]Probe questions[/bold]: {answered} of {len(report.probes)} find material."
        )
        if show_probes:
            probes = Table(show_header=True, header_style="bold")
            probes.add_column("Topic")
            probes.add_column("Coverage")
            probes.add_column("Best match")
            for topic, _question, coverage, top in report.probes:
                colour = {"none": "red", "weak": "yellow"}.get(coverage, "green")
                probes.add_row(escape(topic), f"[{colour}]{coverage}[/{colour}]", escape(top))
            console.print(probes)
        else:
            missing = [t for t, _q, c, _top in report.probes if c == "none"]
            if missing:
                topics = escape(", ".join(sorted(set(missing))))
                console.print(f"  [red]No material for:[/red] {topics}")
            console.print("  [dim]Use --probes to see all of them.[/dim]")
        console.print()

    if not report.findings:
        console.print("[green]✓ No problems found.[/green]")
        return

    for finding in report.findings:
        colour = "red" if finding.severity == "problem" else "yellow"
        mark = "✗" if finding.severity == "problem" else "!"
        console.print(
            f"[{colour}]{mark}[/{colour}] [bold]{finding.area}[/bold] {escape(finding.detail)}"
        )
        if finding.fix:
            console.print(f"    [dim]{escape(finding.fix)}[/dim]")

    console.print()
    if report.problems:
        console.print(f"[red]{len(report.problems)} problem(s) to fix.[/red]")
        raise typer.Exit(1)
    console.print("[yellow]Warnings only - nothing is broken.[/yellow]")


@app.command()
def embed() -> None:
    """Add semantic search to material already ingested, without re-downloading it."""
    from markai.ingest.pipeline import _backfill_embeddings
    from markai.knowledge.embeddings import build_embedder

    settings = _settings()
    embedder = build_embedder(settings)
    if embedder is None:
        _fail(
            "No Voyage API key, so there is nothing to embed.",
            "Add VOYAGE_API_KEY to .env, then run this again.",
        )
    store = _store(settings)
    result = _backfill_embeddings(
        store, embedder, settings, lambda message: console.print(f"[dim]{escape(message)}[/dim]")
    )
    store.close()

    if result.done:
        console.print(f"[green]✓[/green] Embedded {result.done:,} passages with {embedder.name}.")
    if result.error:
        console.print(f"[red]Stopped:[/red] {escape(result.error)}")
        console.print(
            f"[yellow]{result.remaining:,} passages still have no embedding.[/yellow] "
            "What is embedded is saved, so running this again resumes rather than restarts."
        )
        if "payment" in result.error.lower() or _looks_like_a_rate_limit_message(result.error):
            console.print(
                "\nVoyage caps an account with no payment method at 3 requests a minute. "
                "Adding a card at https://dashboard.voyageai.com/ lifts that; the 200M free "
                "tokens still apply, so this stays free."
            )
        raise typer.Exit(1)
    if not result.done:
        console.print("Everything already has embeddings. Nothing to do.")


@app.command()
def status() -> None:
    """Show what Mark knows and how he is configured."""
    from markai.advisor.guardrails import IDENTITY_NOTICE

    settings = _settings()
    store = _store(settings)
    stats = store.stats()

    table = Table(title="Mark status", show_header=True, header_style="bold")
    table.add_column("Item")
    table.add_column("Value")
    table.add_row("Model", f"{settings.model} (effort: {settings.effort})")
    table.add_row("Anthropic key", "set" if settings.anthropic_key() else "[red]not set[/red]")
    table.add_row(
        "Search",
        f"keyword + embeddings ({stats.embedding_model})"
        if stats.embedded_chunks
        else "keyword only (BM25)",
    )
    for kind, count in stats.documents_by_kind.items():
        table.add_row(f"Sources: {kind}", str(count))
    table.add_row("Chunks", str(stats.chunks))
    table.add_row("Last ingest", stats.last_ingest_at or "never")
    table.add_row("Questions asked", str(stats.questions_total))
    table.add_row("Not covered", str(stats.questions_not_covered))
    console.print(table)
    console.print(f"[dim]{IDENTITY_NOTICE}[/dim]")
    store.close()


@app.command()
def report(
    questions: bool = typer.Option(
        False, "--questions", help="Include the text of questions the sources missed."
    ),
) -> None:
    """Write one file describing the state of everything, to send to whoever is helping you.

    Screenshots are a bad way to debug a program. This is every number somebody would ask
    for - what is indexed, what is in facts.yaml, what the review queue holds and why, what
    landlords have asked that the sources missed - in one text file you can paste or attach.

    It never includes a key, an email, a phone number, or anything from a landlord's
    property log. Question text is off unless you ask for it with `--questions`, because
    those are other people's words.
    """
    import platform
    import sys
    from datetime import datetime

    from markai.facts_miner import group_proposals, is_market_rent, why_topical
    from markai.sources.facts import facts_path, load_facts

    settings = _settings()
    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)

    head = ""
    git_head = Path(".git") / "HEAD"
    if git_head.exists():
        ref = git_head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            head = ref[5:]
            pointer = Path(".git") / head
            if pointer.exists():
                head = f"{head} @ {pointer.read_text(encoding='utf-8').strip()[:8]}"
        else:
            head = ref[:8]

    say(f"mark report - {datetime.now().isoformat(timespec='seconds')}")
    say(f"python {sys.version.split()[0]} on {platform.system()} {platform.release()}")
    say(f"branch {head or 'unknown'}")
    say(f"model {settings.model}, effort {settings.effort}")
    say(f"anthropic key {'set' if settings.anthropic_key() else 'NOT SET'}")
    say()

    say("--- what is indexed ---")
    try:
        store = _store(settings)
        stats = store.stats()
        for kind, count in stats.documents_by_kind.items():
            say(f"{kind}: {count} documents")
        say(f"chunks: {stats.chunks}, embedded: {stats.embedded_chunks}")
        say(f"last ingest: {stats.last_ingest_at or 'never'}")
        say(f"questions asked: {stats.questions_total}, not covered: {stats.questions_not_covered}")
        rows = store.list_gaps(200)
        missed = [str(row.get("question", "")) for row in rows]
        say(f"gap rows logged: {len(rows)}")
        if questions and missed:
            say("questions the sources missed:")
            for question in missed[:40]:
                say(f"  - {question[:120]}")
        store.close()
    except Exception as exc:
        say(f"could not read the store: {type(exc).__name__}: {exc}")
    say()

    say("--- facts.yaml ---")
    path = facts_path(settings.sources_file)
    say(f"path: {path}")
    try:
        book = load_facts(path)
        say(f"valid. ordinances: {len(book.ordinances)}, costs: {len(book.costs)}")
        for rule in book.ordinances:
            say(f"  [{rule.id}] {rule.jurisdiction} | {rule.topic} | from {rule.effective_from}")
        for cost in book.costs:
            say(f"  [{cost.id}] {cost.item} | {cost.money()} {cost.unit} | as of {cost.as_of}")
    except Exception as exc:
        say(f"INVALID: {type(exc).__name__}: {exc}")
    say()

    say("--- the review queue ---")
    saved = _load_proposals(settings)
    waiting = list(saved.get("proposals", []))
    put_away = list(saved.get("dropped", []))
    say(f"waiting: {len(waiting)} proposals, set aside: {len(put_away)}")
    if waiting:
        known, _, _ = _known_terms(path)
        groups = group_proposals(waiting, known_terms=known, asked=_questions_that_missed(settings))
        say(f"distinct rules: {len(groups)}")
        say(f"  off topic: {sum(1 for g in groups if not g.on_topic)}")
        say(f"  market rents: {sum(1 for g in groups if is_market_rent(g.lead))}")
        say(f"  cite a section: {sum(1 for g in groups if g.cited)}")
        say(f"  asked about: {sum(1 for g in groups if g.wanted)}")
        say(f"  subjects already covered: {sum(1 for g in groups if g.known)}")
        say("first 40, in the order review would show them:")
        for group in groups[:40]:
            marks = [str(group.support)]
            if group.cited:
                marks.append("cited")
            if group.wanted:
                marks.append("asked")
            if group.known:
                marks.append("covered")
            if not group.on_topic:
                marks.append(f"off topic ({why_topical(group.lead)})")
            say(f"  {group.kind:9} {str(group.lead.get('topic', ''))[:56]:58} {', '.join(marks)}")
    say()

    settings.ensure_dirs()
    out = settings.data_dir / "mark-report.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]✓[/green] Wrote {out}")
    console.print(
        "[dim]Nothing in it is a key, an email, a phone number, or anything from a "
        "landlord's log. Send that file instead of a screenshot.[/dim]"
    )


@app.command()
def gaps(
    top: int = typer.Option(20, "--top", help="How many to show."),
    forget_declined: bool = typer.Option(
        False,
        "--forget-declined",
        help="Clear the 'Jay declined it' rows. They are history, not work.",
    ),
) -> None:
    """List questions the sources answered thinly or not at all.

    Jay does not tell a landlord when the material is thin any more, so this is where that
    shows up. "none" means nothing came back; "weak" means something did and it was a
    stretch, which is the more useful list: those are near misses worth one blog post.
    """
    settings = _settings()
    store = _store(settings)
    if forget_declined:
        cleared = store.forget_declined_gaps()
        console.print(
            f"[green]✓[/green] Cleared {cleared} row(s) Jay declined but the sources "
            f"covered.\n[dim]They stay in the question log; they just stop looking like "
            f"work.[/dim]"
        )
    rows = store.list_gaps(top)
    if not rows:
        console.print("No thin or unanswered questions logged yet.")
        store.close()
        return
    table = Table(title="Where the sources came up short", show_header=True, header_style="bold")
    table.add_column("When")
    table.add_column("Match")
    table.add_column("Question", overflow="fold")
    # A status code in this column reads as noise; what the row means is the point. And
    # "covered" here means something specific worth seeing: the sources had it and Jay said
    # they did not. Those are from before he stopped announcing gaps.
    meaning = {
        "none": "[red]nothing found[/red]",
        "weak": "[yellow]thin match[/yellow]",
        "covered": "[dim]Jay declined it[/dim]",
    }
    declined = 0
    for row in rows:
        coverage = str(row.get("coverage", ""))
        if coverage == "covered":
            declined += 1
        table.add_row(
            str(row.get("asked_at", ""))[:10],
            meaning.get(coverage, f"[dim]{escape(coverage)}[/dim]"),
            escape(str(row.get("question", ""))[:80]),
        )
    console.print(table)
    if declined:
        console.print(
            f"[dim]{declined} say 'Jay declined it': the sources covered the question and "
            f"he said they did not. He no longer does that, so those are history rather "
            f"than a to-do. `mark gaps --forget-declined` clears them.[/dim]"
        )
    console.print("[dim]`mark feedback` is the other half: answers landlords marked wrong.[/dim]")
    store.close()


@app.command()
def search(
    query: str = typer.Argument(..., help="What to look for."),
    k: int = typer.Option(5, "-k", help="How many chunks to show."),
    scores: bool = typer.Option(
        False, "--scores", help="Show the raw keyword and semantic scores behind the verdict."
    ),
) -> None:
    """Search the knowledge base directly, without asking Claude."""
    from markai.knowledge.embeddings import build_embedder
    from markai.knowledge.retriever import Retriever

    settings = _settings()
    store = _store(settings)
    retriever = Retriever(store, build_embedder(settings), settings)
    if retriever.is_empty():
        _fail("The knowledge base is empty.", "Run `mark ingest` first.")
    result = retriever.retrieve(query, k)
    console.print(f"[dim]coverage: {result.coverage}[/dim]")
    if scores:
        # The verdict comes from these two, and a question in Spanish scores no keyword
        # signal at all against English sources - which is worth being able to see.
        console.print(
            f"[dim]top keyword {result.top_lexical_score:.2f} "
            f"(covered needs {settings.weak_relevance:.2f}) · "
            f"top semantic {result.top_cosine:.3f} "
            f"(covered needs {settings.covered_cosine:.2f}) · "
            f"keyword arm {'used' if result.lexical_used else 'unused'} · "
            f"semantic arm {'used' if result.vector_used else 'unused'}[/dim]"
        )
    for index, rc in enumerate(result.chunks, start=1):
        console.print(
            f"[bold]{index}. {escape(rc.document.title)}[/bold] ({rc.document.kind.value})"
        )
        if scores:
            console.print(
                f"   [dim]fused {rc.score:.4f} · keyword rank {rc.lexical_rank} · "
                f"semantic rank {rc.vector_rank}[/dim]"
            )
        console.print(f"   {escape(rc.chunk.text[:220])}…")
    store.close()


def _accounts_store(settings: Any) -> Any:
    from markai.web.accounts import Accounts

    path = settings.data_dir / "accounts.db"
    if not path.exists():
        _fail(
            f"No accounts yet ({path} does not exist).",
            "Nobody has signed up on the page.",
        )
    return Accounts(path, free_questions=settings.free_questions_before_signup)


@accounts_app.command("list")
def accounts_list(
    csv_path: Path = typer.Option(
        None, "--csv", help="Write them to a CSV file instead of printing a table."
    ),
    limit: int = typer.Option(50, "-n", "--limit", help="How many to show."),
) -> None:
    """The landlords with an account: name, email, phone, neighborhood."""
    import csv as csv_module
    from datetime import UTC, datetime

    settings = _settings()
    store = _accounts_store(settings)
    rows = store.all(limit=None if csv_path else limit)
    legacy = store.legacy_signups()
    store.close()
    if legacy:
        console.print(
            f"[dim]{legacy} signup(s) from before passwords are kept in the signups_v1 "
            f"table. They have no password, so they are not accounts.[/dim]"
        )
    if not rows:
        console.print("[yellow]Nobody has an account yet.[/yellow]")
        return

    def when(stamp: float, with_time: bool = True) -> str:
        moment = datetime.fromtimestamp(stamp, UTC)
        return moment.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")

    if csv_path:
        with Path(csv_path).open("w", newline="", encoding="utf-8") as handle:
            writer = csv_module.writer(handle)
            writer.writerow(["signed_up_utc", "name", "email", "phone", "neighborhood"])
            for account, stamp in rows:
                writer.writerow(
                    [when(stamp), account.name, account.email, account.phone, account.neighborhood]
                )
        console.print(f"[green]✓[/green] Wrote {len(rows)} to {csv_path}.")
        console.print("[dim]That file holds personal data. Treat it like your rent roll.[/dim]")
        return

    table = Table(show_header=True, header_style="bold", title="Accounts")
    table.add_column("When")
    # An email you have to guess the end of is no use, so these wrap instead of truncating.
    table.add_column("Name", overflow="fold")
    table.add_column("Email", overflow="fold")
    table.add_column("Phone", overflow="fold")
    table.add_column("Neighborhood", overflow="fold")
    for account, stamp in rows:
        table.add_row(
            # Date only here so the email fits on one line in an 80 column terminal. The
            # exact minute is in the CSV, which is where anyone doing anything with it works.
            when(stamp, with_time=False),
            escape(account.name),
            escape(account.email),
            escape(account.phone),
            escape(account.neighborhood),
        )
    console.print(table)
    console.print(f"[dim]{len(rows)} shown · `mark accounts list --csv leads.csv` to export[/dim]")


def _update_env(values: dict[str, str]) -> Path:
    """Set these keys in .env, leaving every other line exactly as it was.

    Written here rather than by hand because the alternative is a person editing a file
    with a password in it in a text editor, which is how a password ends up pasted into
    the wrong window. Nothing is echoed and the file stays readable only by its owner.
    """
    from markai.config import PROJECT_ROOT

    env_path = PROJECT_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(values)

    updated: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        key = stripped.lstrip("#").strip().partition("=")[0].strip()
        if key in remaining:
            # Replaces the live line and revives a commented-out one.
            updated.append(f"{key}={remaining.pop(key)}")
            continue
        updated.append(raw)
    if remaining:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append("# Added by `mark leads setup`.")
        updated.extend(f"{key}={value}" for key, value in remaining.items())

    env_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    return env_path


@leads_app.command("setup")
def leads_setup() -> None:
    """Ask for the mailbox that sends leads, check it works, and write it to .env.

    The password is typed here and goes straight into .env. It is never echoed, never
    logged, and never has to be pasted anywhere else.
    """
    import smtplib

    console.print(
        "[bold]Where the lead goes[/bold]\n"
        "[dim]The CRM's inbound address, then a mailbox that can send to it. On Google "
        "Workspace or Gmail the password below is an app password, not the account "
        "password.[/dim]"
    )
    to_address = typer.prompt("CRM inbound address").strip()
    host = typer.prompt("SMTP host", default="smtp.gmail.com").strip()
    port = int(typer.prompt("SMTP port", default="587"))
    username = typer.prompt("Mailbox (the account that signs in)").strip()
    password = typer.prompt("App password (hidden)", hide_input=True)
    from_address = typer.prompt("Send from", default=username).strip()

    console.print("[dim]Signing in to check it before saving…[/dim]")
    try:
        opener = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with opener(host, port, timeout=20) as smtp:
            if port != 465:
                smtp.starttls()
            smtp.login(username, password)
    except smtplib.SMTPAuthenticationError:
        _fail(
            "The mail server refused that username and password.",
            "On Google this has to be an app password (Account > Security > 2-Step "
            "Verification > App passwords), not the password you log in with.",
        )
    except Exception as exc:
        _fail(f"Could not reach {host}:{port} - {exc}", "Check the host and the port.")

    env_path = _update_env(
        {
            "MARKAI_LEAD_EMAIL_TO": to_address,
            "MARKAI_SMTP_HOST": host,
            "MARKAI_SMTP_PORT": str(port),
            "MARKAI_SMTP_USERNAME": username,
            "MARKAI_SMTP_PASSWORD": password,
            "MARKAI_SMTP_FROM": from_address,
            "MARKAI_SMTP_STARTTLS": "false" if port == 465 else "true",
        }
    )
    console.print(f"[green]✓[/green] It signed in. Saved to {env_path}.")
    console.print(
        "[dim]That file is git-ignored and readable only by you. Now run "
        "`mark leads test` to put one fake lead in your CRM.[/dim]"
    )


def _crm(settings: Any) -> Any:
    from markai.web.crm import Crm, sender_from_settings

    sender, describe = sender_from_settings(settings)
    return Crm(settings.data_dir / "leads.db", sender=sender, describe=describe)


@leads_app.command("list")
def leads_list(
    limit: int = typer.Option(25, "-n", "--limit", help="How many to show."),
) -> None:
    """Every lead and whether the CRM has it yet."""
    from datetime import UTC, datetime

    settings = _settings()
    crm = _crm(settings)
    counts = crm.counts()
    rows = crm.all(limit=limit)
    configured, describe = crm.configured, crm.describe
    crm.close()

    if not configured:
        console.print(
            "[yellow]Nowhere to send leads yet, so they are queued and waiting.[/yellow]\n"
            "[dim]Set MARKAI_LEAD_EMAIL_TO plus the MARKAI_SMTP_* lines in .env, or "
            "MARKAI_CRM_WEBHOOK_URL for a webhook. Then `mark leads send`.[/dim]"
        )
    else:
        console.print(f"[dim]Sending: {escape(describe)}[/dim]")
    console.print(
        f"[dim]{counts['total']} total · {counts['delivered']} delivered · "
        f"{counts['waiting']} waiting · {counts['gave_up']} gave up[/dim]"
    )
    if not rows:
        return
    table = Table(show_header=True, header_style="bold", title="Leads")
    table.add_column("When")
    table.add_column("Name", overflow="fold")
    table.add_column("Email", overflow="fold")
    table.add_column("State")
    table.add_column("Why not", overflow="fold")
    for lead in rows:
        if lead.delivered:
            state = "[green]delivered[/green]"
        elif lead.gave_up:
            state = "[red]gave up[/red]"
        else:
            state = f"waiting ({lead.attempts})"
        table.add_row(
            datetime.fromtimestamp(lead.created_at, UTC).strftime("%Y-%m-%d"),
            escape(str(lead.payload.get("name", ""))),
            escape(str(lead.payload.get("email", ""))),
            state,
            escape(lead.last_error[:60]),
        )
    console.print(table)


@leads_app.command("send")
def leads_send(
    retry_all: bool = typer.Option(
        False, "--retry-all", help="Also retry the ones that gave up, after fixing the URL."
    ),
) -> None:
    """Push whatever is waiting to the CRM now."""
    settings = _settings()
    crm = _crm(settings)
    if not crm.configured:
        crm.close()
        _fail(
            "There is nowhere to send leads.",
            "Set MARKAI_LEAD_EMAIL_TO and the MARKAI_SMTP_* lines in .env to email them to "
            "your CRM, or MARKAI_CRM_WEBHOOK_URL to POST them.",
        )
    if retry_all:
        console.print(f"[dim]Requeued {crm.reset_attempts()} undelivered lead(s).[/dim]")
    delivered, failed = crm.deliver_pending()
    counts = crm.counts()
    crm.close()
    console.print(f"[green]✓[/green] Sent {delivered}, {failed} to retry.")
    console.print(
        f"[dim]{counts['waiting']} waiting · {counts['gave_up']} gave up · "
        f"{counts['delivered']} delivered in total[/dim]"
    )


@leads_app.command("test")
def leads_test() -> None:
    """Send one obviously fake lead, to prove the wiring before a real one arrives."""
    from markai.web.crm import build_payload

    settings = _settings()
    crm = _crm(settings)
    if not crm.configured:
        crm.close()
        _fail(
            "There is nowhere to send leads.",
            "Set MARKAI_LEAD_EMAIL_TO and the MARKAI_SMTP_* lines in .env first.",
        )
    console.print(f"[dim]Sending: {escape(crm.describe)}[/dim]")

    class _Fake:
        name = "TEST LEAD - please ignore"
        email = "test@example.com"
        phone = "312-555-0100"
        neighborhood = "Logan Square"

    crm.enqueue("test", build_payload(_Fake(), {"asked_about": "A test from mark leads test"}))
    delivered, failed = crm.deliver_pending()
    crm.close()
    if delivered:
        console.print("[green]✓[/green] It went out. Go look for the test lead in your CRM.")
        return
    _fail(
        f"It did not go out ({failed} failed).",
        "Run `mark leads list` for the reason it gave.",
    )


# --------------------------------------------------------------------------------------
# The owner's own facts
# --------------------------------------------------------------------------------------


def _facts() -> tuple[Any, Any]:
    from markai.sources.facts import facts_path, load_facts

    settings = _settings()
    path = facts_path(settings.sources_file)
    try:
        return load_facts(path), path
    except Exception as exc:
        _fail(f"{path} is not valid: {exc}", "Compare against sources/facts.example.yaml.")


@facts_app.command("validate")
def facts_validate() -> None:
    """Check facts.yaml parses, every rule names a citation, and the dates make sense."""
    book, path = _facts()
    if not path.exists():
        console.print(
            f"[dim]No {path.name} yet, so Jay answers from the sources alone. Copy "
            f"sources/facts.example.yaml to {path} when you want an ordinance layer.[/dim]"
        )
        return
    from datetime import date

    today = date.today()
    console.print(f"[green]✓[/green] {path} is valid.")
    console.print(
        f"  ordinances: {len(book.ordinances)} "
        f"({len(book.in_force(today))} in force today, {len(book.superseded(today))} "
        f"superseded) · cost ranges: {len(book.costs)}"
    )
    undated = [o.id for o in book.ordinances if not o.effective_from]
    if undated:
        console.print(
            "[yellow]No effective_from on: "
            + escape(", ".join(undated[:8]))
            + ". They will be treated as always in force.[/yellow]"
        )


@facts_app.command("list")
def facts_list() -> None:
    """Show every rule and price, and whether each one applies today."""
    from datetime import date

    book, path = _facts()
    if book.is_empty():
        console.print(f"[yellow]Nothing in {path}.[/yellow]")
        return
    today = date.today()
    if book.ordinances:
        table = Table(show_header=True, header_style="bold", title="Ordinances")
        table.add_column("Where")
        table.add_column("Topic")
        table.add_column("From")
        table.add_column("Until")
        table.add_column("Citation")
        table.add_column("Now")
        for rule in book.ordinances:
            live = rule.in_force(today)
            table.add_row(
                escape(rule.jurisdiction),
                escape(rule.topic[:40]),
                str(rule.effective_from or "-"),
                str(rule.effective_to or "-"),
                escape(rule.citation[:40]),
                "[green]in force[/green]" if live else "[dim]superseded[/dim]",
            )
        console.print(table)
    if book.costs:
        table = Table(show_header=True, header_style="bold", title="Cost ranges")
        table.add_column("Item")
        table.add_column("Range")
        table.add_column("Unit")
        table.add_column("Priced")
        for cost in book.costs:
            table.add_row(
                escape(cost.item[:44]), cost.money(), escape(cost.unit), cost.as_of or "-"
            )
        console.print(table)


PROPOSALS_FILE = "facts-proposals.json"


def _proposals_path(settings: Any) -> Path:
    return settings.data_dir / PROPOSALS_FILE


def _load_proposals(settings: Any) -> dict:
    import json

    path = _proposals_path(settings)
    if not path.exists():
        return {"read_chunk_ids": [], "proposals": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        _fail(f"{path} is not readable JSON.", "Delete it and mine again.")


def _save_proposals(settings: Any, data: dict) -> None:
    import json

    settings.ensure_dirs()
    _proposals_path(settings).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _record_proposals(settings: Any, saved: dict, already: set, report: Any) -> None:
    """File what a run found, whichever path found it. Never loses what was there before."""
    saved["read_chunk_ids"] = sorted(already | set(report.read_chunk_ids))
    saved["proposals"] = list(saved.get("proposals", [])) + [p.to_dict() for p in report.proposals]
    _save_proposals(settings, saved)


@facts_app.command("mine")
def facts_mine(
    limit: int = typer.Option(0, "--limit", help="Passages to read. 0 means all of them."),
    kind: str = typer.Option("all", "--kind", help="all, website, youtube or podcast."),
    yes: bool = typer.Option(False, "--yes", help="Skip the cost confirmation."),
    restart: bool = typer.Option(False, "--restart", help="Read everything again from scratch."),
    free: bool = typer.Option(
        False, "--free", help="No API call, no cost: propose the sentences themselves."
    ),
) -> None:
    """Read the indexed sources and propose ordinance and cost entries out of them.

    Jay never learns a local fact on his own. This is different: the facts are already in
    the sources, said out loud in a blog post or an episode, and this reads them out. Every
    proposal quotes the sentence it came from, the quote is checked against the passage
    before you ever see it, and nothing lands in facts.yaml until you accept it in
    `mark facts review`.

    This calls Claude and costs money, so it tells you how much before it starts. It picks
    up where it left off, so a run you stop is not a run you lose.

    `--free` spends nothing. It pulls out the sentences that state a rule outright and
    proposes each one as itself, no API key and no call. It finds less, and everything it
    finds is a sentence your source actually wrote. Worth running first on new material:
    what it catches costs nothing, and what it misses is still there for a paid run.
    """
    from markai.facts_miner import candidates_in, estimate, mine, mine_locally
    from markai.models import SourceKind

    wanted = {
        "all": None,
        "website": (SourceKind.WEBSITE,),
        "youtube": (SourceKind.YOUTUBE,),
        "podcast": (SourceKind.PODCAST,),
    }
    if kind.lower() not in wanted:
        _fail(f"Unknown kind {kind!r}.", "Use all, website, youtube or podcast.")
    kinds = wanted[kind.lower()]

    settings = _settings()
    store = _store(settings)
    saved = {"read_chunk_ids": [], "proposals": []} if restart else _load_proposals(settings)
    already = set(saved.get("read_chunk_ids", []))

    found, seen = candidates_in(store, kinds=kinds, already_read=already)
    if limit:
        found = found[:limit]
    if not found:
        store.close()
        console.print(
            f"[green]✓[/green] Nothing left to read: {seen} passages, all of them either "
            f"already mined or with no rule in them."
        )
        return

    if free:
        with console.status("Reading the sources…") as status:

            def free_progress(done: int, total: int) -> None:
                status.update(f"Read {done} of {total} passages…")

            report = mine_locally(
                store, limit=limit, kinds=kinds, already_read=already, on_progress=free_progress
            )
        store.close()
        _record_proposals(settings, saved, already, report)
        console.print(
            f"[green]✓[/green] Read {report.passages_read} passages and found "
            f"{len(report.proposals)} sentences that state a rule. [bold]Cost: nothing.[/bold]"
        )
        console.print(
            f"Now run [bold]mark facts proposals[/bold] to see what is there "
            f"({len(saved['proposals'])} waiting)."
        )
        return

    plan = estimate(found)
    console.print(
        f"[bold]{plan['passages']}[/bold] passages worth reading out of {seen} "
        f"({len(already)} already done)\n"
        f"[dim]{plan['batches']} requests, roughly {plan['input_tokens']:,} in and "
        f"{plan['output_tokens']:,} out[/dim]\n"
        f"Estimated cost: [bold]${plan['usd']:.2f}[/bold], "
        f"[dim]up to ${plan['usd_max']:.2f} if the sources are dense with rules[/dim]"
    )
    if not yes and not typer.confirm("Run it?", default=False):
        store.close()
        console.print("[dim]Nothing spent.[/dim]")
        return

    import anthropic

    key = settings.anthropic_key()
    if not key:
        store.close()
        _fail("ANTHROPIC_API_KEY is not set.", "Run `mark init`, or put your key in .env.")
    client = anthropic.Anthropic(api_key=key)

    with console.status("Reading the sources…") as status:

        def progress(done: int, total: int) -> None:
            status.update(f"Read {done} of {total} passages…")

        report = mine(
            store,
            client,
            settings.model,
            limit=limit,
            kinds=kinds,
            on_progress=progress,
            already_read=already,
        )
    store.close()

    _record_proposals(settings, saved, already, report)

    console.print(
        f"[green]✓[/green] Read {report.passages_read} passages, "
        f"found {len(report.proposals)} to propose."
    )
    if report.dropped_unquoted:
        console.print(
            f"[dim]{report.dropped_unquoted} were dropped: the quote was not in the "
            f"passage, so the sources never actually said it.[/dim]"
        )
    if report.batches_failed:
        console.print(
            f"[yellow]{report.batches_failed} request(s) failed. Run it again to pick "
            f"those up.[/yellow]"
        )
    console.print(f"[dim]Cost about ${report.cost_usd:.2f}.[/dim]")
    console.print(f"Now run [bold]mark facts review[/bold] ({len(saved['proposals'])} waiting).")


def _known_terms(path: Path) -> tuple[frozenset[str], set[str], str]:
    """The vocabulary of facts.yaml, the ids in it, and the file as text.

    Read once per sitting so the review can say "you already have a rule about this"
    without parsing the file for every proposal.
    """
    from markai.sources.facts import load_facts, terms_in

    body = path.read_text(encoding="utf-8") if path.exists() else ""
    if not body:
        return frozenset(), set(), ""
    try:
        book = load_facts(path)
    except Exception as exc:
        _fail(f"{path} is not valid, so nothing can be added to it: {exc}")
    words: set[str] = set()
    ids: set[str] = set()
    for item in [*book.ordinances, *book.costs]:
        ids.add(item.id)
        words |= terms_in(getattr(item, "topic", "") or getattr(item, "item", ""))
    return frozenset(words), ids, body


def _dates_by_title(settings: Any) -> dict[str, str]:
    """When each page was published, keyed by its title.

    The proposals mined before the date was carried through do not have one, and a price
    with no date is the mistake the whole fact layer exists to prevent. The store still
    knows, so it is looked up here rather than lost.
    """
    try:
        store = _store(settings)
    except Exception:
        return {}
    try:
        return {
            doc.title: str(doc.published_at)[:32]
            for doc in store.list_documents()
            if doc.published_at
        }
    except Exception:
        return {}
    finally:
        store.close()


def _questions_that_missed(settings: Any, limit: int = 200) -> list[str]:
    """Questions the sources answered thinly or not at all, plus the answers marked wrong.

    Both are already recorded - `mark gaps` and `mark feedback` read them - and together
    they are the only demand signal this product has. A fact layer is worth what it answers,
    so proposals that would have answered one of these are reviewed first.
    """
    asked: list[str] = []
    try:
        store = _store(settings)
    except Exception:
        return asked
    try:
        asked.extend(str(row.get("question", "")) for row in store.list_gaps(limit))
    except Exception:
        pass
    finally:
        store.close()
    try:
        from markai.web.history import History

        history = History(settings.data_dir / "conversations.db")
        try:
            asked.extend(str(row.get("question", "")) for row in history.ratings(limit, "down"))
        finally:
            history.close()
    except Exception:
        pass
    return [question for question in asked if question.strip()]


def _pick_groups(
    settings: Any,
    kind: str,
    topic: str,
    min_sources: int,
    new_only: bool,
    no_rents: bool = False,
    cited_only: bool = False,
    wanted_only: bool = False,
    max_sources: int = 0,
    on_topic: bool = False,
    off_topic: bool = False,
) -> tuple[list[Any], list[dict], Path, set[str], str, int]:
    """The proposals worth showing this sitting, grouped, filtered and ordered."""
    from markai.facts_miner import group_proposals, has_a_price
    from markai.sources.facts import facts_path

    saved = _load_proposals(settings)
    waiting = list(saved.get("proposals", []))
    path = facts_path(settings.sources_file)
    known, existing_ids, body = _known_terms(path)

    priced = [raw for raw in waiting if has_a_price(raw)]
    groups = group_proposals(waiting, known_terms=known, asked=_questions_that_missed(settings))
    wanted = kind.lower()
    kept = []
    for group in groups:
        if wanted in ("ordinance", "cost") and group.kind != wanted:
            continue
        if topic and topic.lower() not in str(group.lead.get("topic", "")).lower():
            continue
        if group.support < max(1, min_sources):
            continue
        if new_only and group.known:
            continue
        if no_rents and group.rent:
            continue
        if on_topic and not group.on_topic:
            continue
        if off_topic and group.on_topic:
            continue
        if cited_only and not group.cited:
            continue
        if wanted_only and not group.wanted:
            continue
        if max_sources and group.support > max_sources:
            continue
        if not has_a_price(group.lead):
            continue  # a price with no number in it would read as "$0"
        kept.append(group)
    return kept, waiting, path, existing_ids, body, len(waiting) - len(priced)


@facts_app.command("proposals")
def facts_proposals(
    kind: str = typer.Option("all", "--kind", help="all, ordinance or cost."),
    topic: str = typer.Option("", "--topic", help="Only topics containing this word."),
    limit: int = typer.Option(25, "-n", "--limit", help="Rows to show. 0 shows all of them."),
    no_rents: bool = typer.Option(
        False, "--no-rents", help="Leave out market rent numbers, which go stale in a season."
    ),
    cited: bool = typer.Option(
        False, "--cited", help="Only rules that name a section number, not a summary of one."
    ),
    wanted: bool = typer.Option(
        False, "--wanted", help="Only subjects a landlord actually asked about and missed."
    ),
    on_topic: bool = typer.Option(False, "--on-topic", help="Only what is about renting property."),
    off_topic: bool = typer.Option(
        False, "--off-topic", help="Only what is not, so you can see it before dropping it."
    ),
) -> None:
    """What is waiting in the review queue, by subject, before you sit down to it.

    Mining a corpus this size proposes more than anyone reviews in one evening, and most of
    it is the same handful of rules quoted in different posts. This is the map: which
    subjects came up, how many of your own sources state each rule, and which ones you
    already have a rule about.
    """
    settings = _settings()
    groups, waiting, path, _, _, priceless = _pick_groups(
        settings, kind, topic, 1, False, no_rents, cited, wanted, 0, on_topic, off_topic
    )
    if not waiting:
        console.print("[yellow]Nothing waiting. Run `mark facts mine` first.[/yellow]")
        return
    if not groups:
        console.print("[yellow]Nothing matches that filter.[/yellow]")
        return

    corroborated = [g for g in groups if g.support >= 3]
    console.print(
        f"[bold]{len(waiting)}[/bold] proposals, [bold]{len(groups)}[/bold] distinct rules "
        f"after the repeats collapse"
    )
    counts = {"ordinance": 0, "cost": 0}
    for group in groups:
        counts[group.kind] = counts.get(group.kind, 0) + 1
    console.print(
        f"[dim]{counts.get('ordinance', 0)} rules, {counts.get('cost', 0)} prices; "
        f"{sum(1 for g in groups if g.known)} on subjects you already cover"
        + (
            f"; {priceless} priced proposals with no number in them are held back"
            if priceless
            else ""
        )
        + "[/dim]"
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Topic", overflow="fold")
    table.add_column("Kind")
    table.add_column("Sources", justify="right")
    table.add_column("Note")
    for group in groups[: limit or len(groups)]:
        notes = []
        if not group.on_topic:
            notes.append("[yellow]off topic[/yellow]")
        if group.wanted:
            notes.append("[bold]asked about[/bold]")
        if group.cited:
            notes.append("cites a section")
        if group.known:
            notes.append("you cover this")
        if group.rent:
            notes.append("market rent")
        table.add_row(
            escape(str(group.lead.get("topic", ""))[:52]),
            group.kind,
            str(group.support),
            ", ".join(notes),
        )
    console.print(table)
    if limit and len(groups) > limit:
        console.print(f"[dim]{len(groups) - limit} more. Use -n 0 to see all of them.[/dim]")
    rents = [g for g in groups if g.rent]
    if rents:
        console.print(
            f"\n[dim]{len(rents)} of these are market rent numbers. A tuckpointing quote "
            f"from 2023 is roughly still true; last season's asking rent is not, and in the "
            f"facts block Jay would state it as current. Add [bold]--no-rents[/bold] to "
            f"leave them out.[/dim]"
        )
    asked_about = [g for g in groups if g.wanted]
    cited_ones = [g for g in groups if g.cited]
    stray = [g for g in groups if not g.on_topic]
    console.print(
        f"\n[dim]A fact layer is worth what it answers. You do not want {len(groups)} "
        f"entries in it, you want the ones somebody asked for.[/dim]"
    )
    if stray:
        console.print(
            f"  [bold]mark facts proposals --off-topic[/bold]  [dim]{len(stray)} that mention "
            f"nothing about renting property - a cannabis petition, a vehicle sticker, the "
            f"state budget. Look, then `mark facts drop --off-topic`.[/dim]"
        )
    if asked_about:
        console.print(
            f"  [bold]mark facts review --wanted[/bold]  [dim]{len(asked_about)} that would "
            f"have answered a question your sources missed[/dim]"
        )
    if cited_ones:
        console.print(
            f"  [bold]mark facts review --cited[/bold]  [dim]{len(cited_ones)} that name a "
            f"section number, so they are the rule and not a summary[/dim]"
        )
    if corroborated:
        console.print(
            f"  [bold]mark facts review --min-sources 3 --no-rents[/bold]  [dim]"
            f"{len(corroborated)} that three or more of your sources state[/dim]"
        )
    console.print(
        "[dim]`mark facts drop` clears a slice you are never going to want, so the queue "
        "stops looking like a thousand decisions.[/dim]"
    )


@facts_app.command("review")
def facts_review(
    limit: int = typer.Option(0, "-n", "--limit", help="How many to go through this sitting."),
    kind: str = typer.Option("all", "--kind", help="all, ordinance or cost."),
    topic: str = typer.Option("", "--topic", help="Only topics containing this word."),
    min_sources: int = typer.Option(
        1, "--min-sources", help="Only rules this many of your sources state."
    ),
    new_only: bool = typer.Option(
        False, "--new-only", help="Skip subjects facts.yaml already covers."
    ),
    no_rents: bool = typer.Option(
        False, "--no-rents", help="Leave out market rent numbers, which go stale in a season."
    ),
    cited: bool = typer.Option(
        False, "--cited", help="Only rules that name a section number, not a summary of one."
    ),
    wanted: bool = typer.Option(
        False, "--wanted", help="Only subjects a landlord actually asked about and missed."
    ),
    on_topic: bool = typer.Option(False, "--on-topic", help="Only what is about renting property."),
    accept_all: bool = typer.Option(
        False, "--accept-all", help="Accept everything that matches, without asking one by one."
    ),
    list_only: bool = typer.Option(
        False, "--list", help="Print the cards and decide nothing, for reading them elsewhere."
    ),
) -> None:
    """Walk the mined proposals and accept the ones you want into facts.yaml.

    Nothing was written while mining. This is where you decide, with the sentence from your
    own source in front of you.

    Repeats are collapsed first, so a rule four of your posts state is one decision, not
    four, and it says how many said it. Then it goes by subject, so every proposal about
    security deposits arrives together. Filter it down (`--kind cost`, `--topic heat`,
    `--min-sources 3`) and the pile becomes an evening instead of a week; `--accept-all`
    takes a filtered set in one go, which is the point of the filters.
    """
    from markai.facts_miner import (
        CannotInsert,
        Proposal,
        as_yaml_entry,
        insert_into_facts,
        jurisdiction_of,
    )

    settings = _settings()
    groups, waiting, path, existing_ids, body, _ = _pick_groups(
        settings, kind, topic, min_sources, new_only, no_rents, cited, wanted, 0, on_topic
    )
    if not waiting:
        console.print("[yellow]Nothing to review. Run `mark facts mine` first.[/yellow]")
        return
    if not groups:
        console.print("[yellow]Nothing matches that filter.[/yellow]")
        return
    if limit:
        groups = groups[:limit]

    dates = _dates_by_title(settings)

    if accept_all:
        console.print(
            f"About to add [bold]{len(groups)}[/bold] entries to {path.name} "
            f"({sum(g.support for g in groups)} proposals, repeats collapsed)."
        )
        if not typer.confirm("Go ahead?", default=False):
            console.print("[dim]Nothing written.[/dim]")
            return

    def take(group: Any) -> tuple[bool, str]:
        """Write one group's lead into the file body. Returns (written, why not)."""
        nonlocal body
        raw = group.lead
        where = jurisdiction_of(raw)
        if raw.get("kind") != "cost" and not where:
            return False, (
                "the passage never says which city or county this rule is from. Filing it "
                "as Chicago would be a guess, and a landlord in Evanston would be handed it "
                "as their own"
            )
        number = 1
        while f"mined-{number}" in existing_ids:
            number += 1
        entry_id = f"mined-{number}"
        proposal = Proposal(
            kind=str(raw.get("kind", "ordinance")),
            topic=str(raw.get("topic", "")),
            rule=str(raw.get("rule", "")),
            quote=str(raw.get("quote", "")),
            source_title=str(raw.get("source", "")),
            source_url=raw.get("url"),
            jurisdiction=where,
            citation=str(raw.get("citation", "")),
            low=raw.get("low"),
            high=raw.get("high"),
            unit=str(raw.get("unit", "")),
            # Whatever the miner carried, or the page's date looked up from the store.
            source_date=str(raw.get("source_date") or dates.get(str(raw.get("source", "")), ""))[
                :32
            ],
            support=group.support,
        )
        section = "ordinances" if proposal.kind == "ordinance" else "costs"
        try:
            candidate = insert_into_facts(body, section, as_yaml_entry(proposal, entry_id))
        except CannotInsert as exc:
            return False, str(exc)
        # Written only once it parses. A file that does not load takes Jay's whole fact
        # layer with it, and that is not a trade worth making for one entry.
        try:
            import yaml

            yaml.safe_load(candidate)
        except Exception as exc:
            return False, f"that entry would break the file ({exc})"
        body = candidate
        existing_ids.add(entry_id)
        return True, ""

    accepted = declined = 0
    decided: set[int] = set()
    skip_topics: set[str] = set()

    def show(group: Any, number: int = 0) -> None:
        """One card: the rule, where it is from, and everything known about it."""
        console.print()
        label = f"[bold]{escape(str(group.lead.get('topic', '')))}[/bold]"
        console.print(
            (f"[dim]{number}.[/dim] " if number else "")
            + label
            + f" [dim]({group.kind}"
            + (
                f", {escape(jurisdiction_of(group.lead))}"
                if jurisdiction_of(group.lead)
                else ", nowhere named"
            )
            + (f", {group.support} sources say it" if group.support > 1 else "")
            + (", you already cover this subject" if group.known else "")
            + (", a market rent that will go stale" if group.rent else "")
            + (", somebody asked about this" if group.wanted else "")
            + (", nothing to do with renting" if not group.on_topic else "")
            + (
                f", page dated {escape(str(group.lead.get('source_date', '')))[:10]}"
                if group.lead.get("source_date")
                else ""
            )
            + f", from {escape(group.sources()[0][:44] if group.sources() else '?')})[/dim]"
        )
        console.print(f"  {escape(str(group.lead.get('rule', '')))}")
        console.print(f'  [dim]source says: "{escape(str(group.lead.get("quote", "")))}"[/dim]')

    if list_only:
        # For reading somewhere other than a prompt. Decides nothing and writes nothing.
        for number, group in enumerate(groups, start=1):
            show(group, number)
        console.print()
        console.print(
            f"[dim]{len(groups)} cards. Nothing was decided; add --accept-all, or drop "
            f"--list to go through them one at a time.[/dim]"
        )
        return

    for group in groups:
        if not accept_all and group.topic in skip_topics:
            continue
        if accept_all:
            choice = "a"
        else:
            show(group)
            choice = (
                typer.prompt("  [a]ccept, [s]kip, skip this [t]opic, [q]uit", default="s")
                .strip()
                .lower()[:1]
            )
        if choice == "q":
            break
        if choice == "t":
            skip_topics.add(group.topic)
            continue
        if choice != "a":
            declined += 1
            decided.update(group.indices)
            continue

        written, why = take(group)
        if not written:
            console.print(f"[red]Skipped: {escape(why)}[/red]")
            continue
        accepted += 1
        # Every passage that quoted this sentence is answered by the one entry.
        decided.update(group.indices)

    if accepted:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    remaining = [raw for index, raw in enumerate(waiting) if index not in decided]
    saved = _load_proposals(settings)
    saved["proposals"] = remaining
    _save_proposals(settings, saved)

    console.print()
    console.print(
        f"[green]✓[/green] Accepted {accepted} into {path.name}, turned down {declined}, "
        f"{len(remaining)} proposals left."
    )
    if accepted:
        console.print("[dim]Run `mark facts validate`, then restart `mark serve`.[/dim]")


@facts_app.command("drop")
def facts_drop(
    kind: str = typer.Option("all", "--kind", help="all, ordinance or cost."),
    topic: str = typer.Option("", "--topic", help="Only topics containing this word."),
    rents: bool = typer.Option(False, "--rents", help="Drop the market rent numbers."),
    max_sources: int = typer.Option(
        0, "--max-sources", help="Only what this many or fewer of your sources state."
    ),
    uncited: bool = typer.Option(
        False, "--uncited", help="Drop the ones that name no section number."
    ),
    off_topic: bool = typer.Option(
        False, "--off-topic", help="Drop what mentions nothing about renting property."
    ),
    covered: bool = typer.Option(
        False, "--covered", help="Drop subjects facts.yaml already has a rule about."
    ),
    yes: bool = typer.Option(False, "--yes", help="Do not ask."),
) -> None:
    """Clear a slice of the queue you are never going to want.

    Nine hundred subjects waiting is not a to-do list, it is a reason to stop opening the
    command. This sets a filtered slice aside - `--off-topic`, `--rents`, `--max-sources 1`,
    `--uncited` - so what is left is a list you might actually finish.

    Nothing is lost. What it drops is kept in the same file and `mark facts undrop` puts it
    back, so being wrong about a slice costs a command rather than a re-mine. It never
    touches facts.yaml.
    """
    from markai.facts_miner import (
        cites_a_section,
        group_proposals,
        is_landlord_business,
        is_market_rent,
    )
    from markai.sources.facts import facts_path

    settings = _settings()
    saved = _load_proposals(settings)
    waiting = list(saved.get("proposals", []))
    if not waiting:
        console.print("[yellow]Nothing waiting.[/yellow]")
        return
    if not (
        rents or max_sources or uncited or covered or off_topic or topic or kind.lower() != "all"
    ):
        _fail(
            "That would drop everything.",
            "Name a slice: --off-topic, --rents, --max-sources 1, --uncited, --covered, "
            "--kind cost, --topic <word>.",
        )

    known, _, _ = _known_terms(facts_path(settings.sources_file))
    groups = group_proposals(waiting, known_terms=known)
    wanted_kind = kind.lower()
    doomed: set[int] = set()
    for group in groups:
        if wanted_kind in ("ordinance", "cost") and group.kind != wanted_kind:
            continue
        if topic and topic.lower() not in str(group.lead.get("topic", "")).lower():
            continue
        if rents and not is_market_rent(group.lead):
            continue
        if uncited and cites_a_section(group.lead):
            continue
        if off_topic and is_landlord_business(group.lead):
            continue
        if covered and not group.known:
            continue
        if max_sources and group.support > max_sources:
            continue
        doomed.update(group.indices)

    if not doomed:
        console.print("[yellow]Nothing matches that.[/yellow]")
        return
    console.print(
        f"About to set aside [bold]{len(doomed)}[/bold] proposals.\n"
        f"[dim]facts.yaml is untouched, and these are kept rather than deleted: "
        f"`mark facts undrop` puts them all back. Nothing here needs mining again.[/dim]"
    )
    if not yes and not typer.confirm("Go ahead?", default=False):
        console.print("[dim]Nothing dropped.[/dim]")
        return

    # Kept, not deleted. A classifier deciding what a landlord will never ask is a
    # judgement call, and the owner should be able to disagree with it afterwards.
    saved["dropped"] = list(saved.get("dropped", [])) + [waiting[index] for index in sorted(doomed)]
    saved["proposals"] = [raw for index, raw in enumerate(waiting) if index not in doomed]
    _save_proposals(settings, saved)
    console.print(
        f"[green]✓[/green] Set aside {len(doomed)}. {len(saved['proposals'])} proposals left, "
        f"{len(saved['dropped'])} put away.\n"
        f"[dim]`mark facts undrop` brings them back.[/dim]"
    )


@facts_app.command("undrop")
def facts_undrop(
    topic: str = typer.Option("", "--topic", help="Only the ones whose topic contains this."),
) -> None:
    """Put back what `mark facts drop` set aside.

    Deciding what a landlord will never ask is a judgement call, and mine was wrong about
    Section 8 once already.
    """
    settings = _settings()
    saved = _load_proposals(settings)
    put_away = list(saved.get("dropped", []))
    if not put_away:
        console.print("[yellow]Nothing has been set aside.[/yellow]")
        return
    wanted = topic.lower()
    back = [raw for raw in put_away if not wanted or wanted in str(raw.get("topic", "")).lower()]
    if not back:
        console.print(f"[yellow]None of the {len(put_away)} set aside mention {topic!r}.[/yellow]")
        return
    keep = [raw for raw in put_away if raw not in back]
    saved["proposals"] = list(saved.get("proposals", [])) + back
    saved["dropped"] = keep
    _save_proposals(settings, saved)
    console.print(
        f"[green]✓[/green] Put {len(back)} back. {len(saved['proposals'])} waiting, "
        f"{len(keep)} still set aside."
    )


@facts_app.command("why")
def facts_why(
    text: str = typer.Argument(..., help="A word from the topic you want explained."),
    limit: int = typer.Option(10, "-n", "--limit", help="How many to explain."),
) -> None:
    """Explain how a proposal was judged: on topic or not, cited, corroborated, asked about.

    For when the queue says something you disagree with. It runs the same code the listing
    does, so what it prints is the actual reason and not a story about it.
    """
    from markai.facts_miner import group_proposals, is_market_rent, jurisdiction_of, why_topical
    from markai.sources.facts import facts_path

    settings = _settings()
    saved = _load_proposals(settings)
    put_away = list(saved.get("dropped", []))
    pool = list(saved.get("proposals", [])) + put_away
    if not pool:
        console.print("[yellow]Nothing to explain. Run `mark facts mine` first.[/yellow]")
        return

    known, _, _ = _known_terms(facts_path(settings.sources_file))
    groups = group_proposals(pool, known_terms=known, asked=_questions_that_missed(settings))
    wanted = text.lower()
    found = [g for g in groups if wanted in str(g.lead.get("topic", "")).lower()]
    if not found:
        console.print(f"[yellow]No proposal's topic contains {text!r}.[/yellow]")
        return

    set_aside = {id(raw) for raw in put_away}
    for group in found[: limit or len(found)]:
        console.print()
        console.print(f"[bold]{escape(str(group.lead.get('topic', '')))}[/bold] ({group.kind})")
        console.print(f'  [dim]source says: "{escape(str(group.lead.get("quote", "")))}"[/dim]')
        verdict = "about renting property" if group.on_topic else "[yellow]off topic[/yellow]"
        console.print(f"  {verdict}, on {escape(why_topical(group.lead))}")
        where = jurisdiction_of(group.lead)
        marks = [f"{group.support} source(s) state it"]
        marks.append(f"about {where}" if where else "[yellow]names no city or county[/yellow]")
        if group.cited:
            marks.append("names a section number")
        if group.wanted:
            marks.append("answers a question somebody asked")
        if group.known:
            marks.append("a subject facts.yaml already covers")
        if is_market_rent(group.lead):
            marks.append("a market rent, so it goes stale")
        if any(id(raw) in set_aside for raw in group.raws):
            marks.append("[yellow]set aside by `facts drop`[/yellow]")
        console.print("  " + "; ".join(marks))
    if limit and len(found) > limit:
        console.print(f"\n[dim]{len(found) - limit} more match. Use -n 0 for all of them.[/dim]")


@facts_app.command("probe")
def facts_probe(
    question: str = typer.Argument(..., help="A question, to see which facts it would pull in."),
) -> None:
    """Show exactly which of your facts a question puts in front of Jay."""
    from markai.sources.facts import select

    book, path = _facts()
    if book.is_empty():
        console.print(f"[yellow]Nothing in {path}.[/yellow]")
        return
    rules, costs = select(book, question)
    if not rules and not costs:
        console.print(
            "[yellow]None of your facts share a word with that question, so Jay would "
            "answer from the sources alone.[/yellow]"
        )
        console.print("[dim]Add the words an owner would actually use to `keywords`.[/dim]")
        return
    for rule in rules:
        console.print(f"[bold]{escape(rule.jurisdiction)}[/bold] · {escape(rule.topic)}")
        console.print(f"   {escape(rule.rule.strip()[:200])}")
        console.print(f"   [dim]{escape(rule.citation)} · from {rule.effective_from or '-'}[/dim]")
    for cost in costs:
        console.print(f"[bold]{escape(cost.item)}[/bold] {cost.money()} {escape(cost.unit)}")


@app.command()
def feedback(
    limit: int = typer.Option(30, "-n", "--limit", help="How many to show."),
    only: str = typer.Option("", "--only", help="down or up. Down is the list worth reading."),
) -> None:
    """What landlords thought of Jay's answers on the page.

    A thumbs down names a question the sources answered badly, which is a content decision:
    a blog post to write, an episode to point at, a rule to add to facts.yaml.
    """
    from datetime import UTC, datetime

    from markai.web.history import History

    settings = _settings()
    path = settings.data_dir / "conversations.db"
    if not path.exists():
        console.print("[yellow]No conversations yet, so nothing has been rated.[/yellow]")
        return
    store = History(path)
    counts = store.rating_counts()
    rows = store.ratings(limit=limit, only=only.strip().lower())
    store.close()

    console.print(f"[dim]{counts['up']} up · {counts['down']} down[/dim]")
    if not rows:
        console.print("[yellow]Nothing rated yet.[/yellow]")
        return
    table = Table(show_header=True, header_style="bold", title="What landlords said")
    table.add_column("When")
    table.add_column("")
    table.add_column("Question", overflow="fold")
    table.add_column("What they said", overflow="fold")
    for row in rows:
        table.add_row(
            datetime.fromtimestamp(row["created_at"], UTC).strftime("%Y-%m-%d"),
            "[green]up[/green]" if row["rating"] == "up" else "[red]down[/red]",
            escape(row["question"][:70]),
            escape(row["note"][:70]),
        )
    console.print(table)
    if counts["down"]:
        console.print(
            "[dim]`mark gaps` is the other half: questions the sources never covered.[/dim]"
        )


@app.command()
def episodes(
    query: str = typer.Argument("", help="A topic to look for. Omit to list the catalog."),
    guest: str = typer.Option("", "--guest", help="Only episodes whose title names this person."),
    limit: int = typer.Option(10, "-n", "--limit", help="How many to show."),
    kind: str = typer.Option(
        "all", "--kind", help="all, podcast or youtube.", case_sensitive=False
    ),
) -> None:
    """Search the podcast and video index: episode, guest, topics and the minute."""
    from markai.knowledge.episodes import AV_KINDS, catalog, find_moments
    from markai.models import SourceKind

    wanted = {"all": AV_KINDS, "podcast": (SourceKind.PODCAST,), "youtube": (SourceKind.YOUTUBE,)}
    kinds = wanted.get(kind.lower())
    if kinds is None:
        _fail(f"Unknown kind {kind!r}.", "Use all, podcast or youtube.")

    settings = _settings()
    store = _store(settings)

    if not query.strip():
        entries = catalog(store, guest=guest or None, kinds=kinds, limit=limit)
        if not entries:
            console.print("[yellow]No episodes match.[/yellow]")
            store.close()
            return
        table = Table(show_header=True, header_style="bold", title="Episodes")
        table.add_column("Ep.")
        table.add_column("Title")
        table.add_column("Guest")
        table.add_column("Date")
        for entry in entries:
            title = escape(entry.title[:70])
            if not entry.transcribed:
                title += " [dim](show notes only)[/dim]"
            table.add_row(
                entry.number or "-", title, escape(entry.guest or "-"), entry.published_at or "-"
            )
        console.print(table)
        console.print(f"[dim]{len(entries)} shown[/dim]")
        store.close()
        return

    from markai.knowledge.embeddings import build_embedder
    from markai.knowledge.retriever import Retriever

    retriever = Retriever(store, build_embedder(settings), settings)
    if retriever.is_empty():
        _fail("The knowledge base is empty.", "Run `mark ingest` first.")
    moments = find_moments(retriever, query, limit=limit, kinds=kinds)
    if not moments:
        console.print(f"[yellow]Nothing in the episodes covers {escape(query)}.[/yellow]")
        store.close()
        return
    for index, moment in enumerate(moments, start=1):
        label = moment.label()
        console.print(
            f"[bold]{index}. {escape(moment.title)}[/bold]"
            + (f" [dim]{label}[/dim]" if label else "")
        )
        if moment.guest:
            console.print(f"   guest: {escape(moment.guest)}")
        if moment.topics:
            console.print(f"   [dim]topics: {escape(', '.join(moment.topics))}[/dim]")
        console.print(f"   {escape(moment.quote)}…")
        if moment.url:
            console.print(f"   [blue]{escape(moment.url)}[/blue]")
    store.close()


# --------------------------------------------------------------------------------------
# Asking Mark
# --------------------------------------------------------------------------------------


def _render_answer(advisor: Any, question: str, conversation: Any) -> None:
    printed: list[str] = []
    response = None
    for event in advisor.stream(question, conversation):
        if event.type == "text":
            console.print(escape(event.text), end="")
            printed.append(event.text)
        elif event.type == "tool_call":
            console.print(f"\n[dim]running {event.text}…[/dim]")
        elif event.type == "error":
            console.print()
            err.print(f"[bold red]✗[/bold red] {event.text}")
            return
        elif event.type == "final":
            response = event.response
    console.print()

    if response is None:
        return
    if response.text.strip() != "".join(printed).strip():
        console.rule("[dim]final answer[/dim]")
        console.print(escape(response.text))

    if response.citations:
        console.print()
        console.print("[bold]Sources[/bold]")
        for citation in response.citations:
            bits = [f"[{citation.marker}]", citation.title]
            if citation.episode:
                bits.append(f"ep. {citation.episode}")
            if citation.published_at:
                bits.append(citation.published_at)
            if citation.timestamp:
                bits.append(citation.timestamp)
            if citation.url:
                bits.append(citation.url)
            console.print("  " + escape(" · ".join(bits)))

    console.print(f"[dim]{_usage_line(response)}[/dim]")


def _usage_line(response: Any) -> str:
    """One line of what the answer cost, in the units Anthropic bills.

    Writes are shown next to reads on purpose: a run with writes and no reads is paying
    1.25x for a cache nothing ever collects, which looks identical to a healthy run if you
    only print the read count.
    """
    usage = response.usage or {}
    read = usage.get("cache_read_input_tokens", 0)
    written = usage.get("cache_creation_input_tokens", 0)
    cache = f"cache {read:,} read"
    if written:
        cache += f" / {written:,} written"
    if written and not read:
        cache += " [yellow](no hit yet)[/yellow]"
    return (
        f"{response.model} · in {usage.get('input_tokens', 0):,} "
        f"out {usage.get('output_tokens', 0):,} · {cache} · coverage {response.coverage}"
    )


@app.command()
def ask(question: str = typer.Argument(..., help="Your question for Mark.")) -> None:
    """Ask Mark one question."""
    settings = _settings()
    manifest = _manifest(settings)
    store = _store(settings)
    advisor = _advisor(settings, manifest, store)
    _render_answer(advisor, question, None)
    store.close()


@app.command()
def chat() -> None:
    """Talk to Mark in the terminal. /reset clears the thread, /quit exits."""
    from markai.advisor.guardrails import IDENTITY_NOTICE
    from markai.advisor.mark import Conversation

    settings = _settings()
    manifest = _manifest(settings)
    store = _store(settings)
    advisor = _advisor(settings, manifest, store)

    console.print(Panel.fit(IDENTITY_NOTICE, title="Mark"))
    console.print(
        "[dim]Type your question. /reset starts over, /sources lists what I know, "
        "/quit exits.[/dim]\n"
    )
    conversation = Conversation(session_id="cli")

    while True:
        try:
            question = typer.prompt("you", prompt_suffix=" > ").strip()
        except (EOFError, KeyboardInterrupt, typer.Abort):
            console.print("\nTake care.")
            break
        if not question:
            continue
        low = question.lower()
        if low in ("/quit", "/exit", "/q"):
            console.print("Take care.")
            break
        if low == "/reset":
            conversation = Conversation(session_id="cli")
            console.print("[dim]Fresh thread.[/dim]")
            continue
        if low == "/sources":
            sources_list()
            continue
        console.print("[bold]mark[/bold] > ", end="")
        _render_answer(advisor, question, conversation)
        console.print()

    store.close()


@app.command()
def serve(
    host: str = typer.Option(None, "--host", help="Address to bind (default 127.0.0.1)."),
    port: int = typer.Option(None, "--port", help="Port to bind (default 8000)."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm binding to a public address."),
) -> None:
    """Run the browser chat UI."""
    import uvicorn

    settings = _settings()
    bind_host = host or settings.web_host
    bind_port = port or settings.web_port

    if bind_host not in LOOPBACK and not settings.access_code():
        err.print(
            f"[yellow]![/yellow] Binding to {bind_host} puts Mark on the network with no login. "
            "Anyone who can reach it can chat at your API expense."
        )
        err.print(
            "  [dim]Set MARKAI_WEB_ACCESS_CODE in .env, or put it behind a proxy that "
            "requires a login.[/dim]"
        )
        if not yes:
            _fail("Refusing to start.", "Re-run with --yes if you really want this.")

    console.print(f"Mark is at [bold]http://{bind_host}:{bind_port}[/bold] (ctrl-c to stop)")
    if reload:
        uvicorn.run("markai.web.app:app", host=bind_host, port=bind_port, reload=True)
    else:
        from markai.web.app import create_app

        uvicorn.run(create_app(settings), host=bind_host, port=bind_port)


# --------------------------------------------------------------------------------------
# Calculators
# --------------------------------------------------------------------------------------


@calc_app.command("mortgage")
def calc_mortgage(
    price: float = typer.Option(..., "--price", help="Purchase price."),
    down_pct: float = typer.Option(20.0, "--down-pct", help="Down payment, in percent."),
    rate: float = typer.Option(..., "--rate", help="Annual interest rate, in percent."),
    years: int = typer.Option(30, "--years", help="Loan term in years."),
) -> None:
    """Monthly principal and interest for a loan."""
    from markai.advisor.calculators import mortgage_payment

    principal = price * (1 - down_pct / 100.0)
    payment = mortgage_payment(principal, rate / 100.0, years)
    console.print(f"Loan amount: [bold]${principal:,.2f}[/bold]")
    console.print(f"Monthly principal + interest: [bold]${payment:,.2f}[/bold]")


@calc_app.command("deal")
def calc_deal(
    price: float = typer.Option(..., "--price", help="Purchase price."),
    rent: float = typer.Option(..., "--rent", help="Total monthly rent."),
    down_pct: float = typer.Option(25.0, "--down-pct", help="Down payment, in percent."),
    rate: float = typer.Option(7.0, "--rate", help="Annual interest rate, in percent."),
    years: int = typer.Option(30, "--years", help="Loan term in years."),
    expenses_monthly: float = typer.Option(
        0.0, "--expenses-monthly", help="Taxes, insurance and HOA per month."
    ),
    vacancy_pct: float = typer.Option(5.0, "--vacancy-pct", help="Vacancy allowance, in percent."),
) -> None:
    """Cash flow, cap rate, cash-on-cash and DSCR for a deal."""
    from markai.advisor.calculators import analyze_deal

    result = analyze_deal(
        price=price,
        down_payment_pct=down_pct / 100.0,
        annual_rate=rate / 100.0,
        years=years,
        monthly_rent=rent,
        vacancy_rate=vacancy_pct / 100.0,
        taxes_annual=expenses_monthly * 12.0,
    )
    table = Table(title="Deal analysis", show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Loan amount", f"${result['loan_amount']:,.2f}")
    table.add_row("Monthly payment", f"${result['monthly_payment']:,.2f}")
    table.add_row("Operating expenses / mo", f"${result['operating_expenses_monthly']:,.2f}")
    table.add_row("Cash flow / mo", f"${result['cash_flow_monthly']:,.2f}")
    table.add_row("Cash flow / yr", f"${result['cash_flow_annual']:,.2f}")
    table.add_row("Cap rate", f"{result['cap_rate'] * 100:.2f}%")
    table.add_row("Cash on cash", f"{result['cash_on_cash'] * 100:.2f}%")
    table.add_row("DSCR", f"{result['dscr']:.2f}")
    table.add_row("1% rule", "passes" if result["one_percent_rule"]["passes"] else "fails")
    console.print(table)
    console.print("[dim]Estimates only. Confirm taxes, insurance and rents before you buy.[/dim]")


def main() -> None:
    """Console-script entry point."""
    os.environ.setdefault("COLUMNS", "100")
    app()


if __name__ == "__main__":
    main()
