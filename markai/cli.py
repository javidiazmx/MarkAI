"""The ``mark`` command line: set up, ingest, ask, serve.

Heavy modules are imported inside the command functions so ``mark --help`` stays instant and
still works before the rest of the project is configured.
"""

from __future__ import annotations

import os
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
        how = "required at signup" if settings.password_required else "offered, not required"
        table.add_row(
            "Accounts",
            f"{settings.free_questions_before_signup} free question(s), then a signup "
            f"· password {how}",
        )
    else:
        table.add_row("Accounts", "off, nobody is ever asked to sign up")

    accounts_path = settings.data_dir / "accounts.db"
    if accounts_path.exists():
        from markai.web.accounts import Accounts

        signups = Accounts(accounts_path, free_questions=settings.free_questions_before_signup)
        total = len(signups.all())
        with_password = sum(1 for account, _ in signups.all() if account.has_password)
        legacy = signups.legacy_signups()
        signups.close()
        note = f"{total} account(s), {with_password} with a password"
        if legacy:
            note += f"\n[dim]{legacy} pre-password signup(s) kept in signups_v1[/dim]"
        table.add_row("Signups so far", note)

    if settings.crm_webhook_url:
        from markai.web.crm import Crm

        queue = Crm(settings.data_dir / "leads.db")
        counts = queue.counts()
        queue.close()
        note = (
            f"{settings.crm_webhook_url}\n"
            f"[dim]{counts['delivered']} delivered · {counts['waiting']} waiting · "
            f"{counts['gave_up']} gave up[/dim]"
        )
        if counts["waiting"] or counts["gave_up"]:
            note += "\n[dim]`mark leads send` pushes them.[/dim]"
        table.add_row("CRM webhook", note)
    else:
        table.add_row(
            "CRM webhook",
            "[yellow]not set[/yellow] - leads are queued and go nowhere\n"
            "[dim]MARKAI_CRM_WEBHOOK_URL, then `mark leads test`.[/dim]",
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
def gaps(top: int = typer.Option(20, "--top", help="How many to show.")) -> None:
    """List questions Mark could not answer, so you know what material to add."""
    settings = _settings()
    store = _store(settings)
    rows = store.list_gaps(top)
    if not rows:
        console.print("No unanswered questions logged yet.")
        store.close()
        return
    table = Table(title="Questions Mark could not answer", show_header=True, header_style="bold")
    table.add_column("When")
    table.add_column("Question")
    for row in rows:
        table.add_row(str(row.get("asked_at", ""))[:16], escape(str(row.get("question", ""))[:90]))
    console.print(table)
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


@accounts_app.command("reset-password")
def accounts_reset_password(
    email: str = typer.Argument(..., help="The account's email, which is its username."),
    password: str = typer.Option(
        None, "--password", help="The new password. Prompted for, hidden, if omitted."
    ),
) -> None:
    """Set a landlord's password.

    There is no email delivery here, so there is no reset link a landlord can use. This is
    the whole recovery story: they email the owner and the owner runs this. Every session
    that account had is ended, which is also how you throw someone out.
    """
    from markai.web.accounts import LoginError, SignupError

    settings = _settings()
    store = _accounts_store(settings)
    if not password:
        password = typer.prompt("New password (hidden)", hide_input=True, confirmation_prompt=True)
    try:
        store.set_password(email, password)
    except (SignupError, LoginError) as exc:
        store.close()
        _fail(str(exc))
    store.close()
    console.print(f"[green]✓[/green] Password set for {escape(email)}.")
    console.print("[dim]Every session that account had is signed out.[/dim]")


def _crm(settings: Any) -> Any:
    from markai.web.crm import Crm

    return Crm(
        settings.data_dir / "leads.db",
        url=settings.crm_webhook_url,
        headers=settings.crm_headers(),
    )


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
    configured = crm.configured
    crm.close()

    if not configured:
        console.print(
            "[yellow]No CRM webhook set, so leads are queued and waiting.[/yellow]\n"
            "[dim]Put the URL in .env as MARKAI_CRM_WEBHOOK_URL, then run "
            "`mark leads send`.[/dim]"
        )
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
            "No CRM webhook is set.",
            "Add MARKAI_CRM_WEBHOOK_URL to .env. A Zapier or Make catch hook works, and so "
            "does any endpoint that accepts a JSON POST.",
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
        _fail("No CRM webhook is set.", "Add MARKAI_CRM_WEBHOOK_URL to .env first.")

    class _Fake:
        name = "TEST LEAD - please ignore"
        email = "test@example.com"
        phone = "312-555-0100"
        neighborhood = "Logan Square"
        has_password = False

    crm.enqueue("test", build_payload(_Fake(), {"asked_about": "A test from mark leads test"}))
    delivered, failed = crm.deliver_pending()
    crm.close()
    if delivered:
        console.print("[green]✓[/green] The CRM accepted it. Go look for the test lead.")
        return
    _fail(
        f"The CRM did not accept it ({failed} failed).",
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
