"""``mark audit``: prove the knowledge base is actually usable, without calling Claude.

Ingest counts say what was stored. They do not say whether a landlord's question finds
anything, whether a listed site contributed a single page, or whether half the passages
are the same footer repeated. This looks for those.

Nothing here calls the Messages API. The only paid call is one Voyage embedding per probe
question when semantic search is on, which is fractions of a cent for the whole run.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from markai.models import SourceKind

# Real questions a Chicagoland landlord would ask, in both languages the owner's audience
# uses. Deliberately worded the way someone types, not the way the sources are written -
# that is what makes them a test of retrieval rather than of string matching.
PROBE_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("deposits", "How long do I have to return a security deposit in Chicago?"),
    ("deposits", "cuanto le regreso al inquilino cuando se muda"),
    ("deposit interest", "Do I owe interest on a deposit in Cook County?"),
    ("eviction", "My tenant stopped paying in January, what are my steps?"),
    ("eviction", "como desalojo a un inquilino que no paga"),
    ("notices", "How many days notice before I can file?"),
    ("screening", "What should I look at on a rental application?"),
    ("screening", "que documentos le pido a un solicitante"),
    ("fair housing", "Do I have to accept a housing voucher?"),
    ("heat", "When does the heat ordinance season start?"),
    ("repairs", "Tenant says the furnace is out in January, what now?"),
    ("lead paint", "What do I have to disclose about lead paint?"),
    ("late fees", "How much can I charge as a late fee?"),
    ("leases", "Can I use a month to month lease in Chicago?"),
    ("RLTO", "Which buildings are exempt from the RLTO?"),
    ("registration", "Do I need to register my rental property?"),
    ("insurance", "What insurance should a small landlord carry?"),
    ("numbers", "How do I work out cash flow on a two flat?"),
    ("market", "What are rents doing on the north side?"),
    ("management", "Should I self manage or hire a property manager?"),
)

MIN_WORDS = 40
NEAR_DUPLICATE_SHARE = 0.25


@dataclass
class Finding:
    """One problem worth acting on."""

    severity: str  # "problem" or "warning"
    area: str
    detail: str
    fix: str = ""


@dataclass
class AuditReport:
    documents: int = 0
    chunks: int = 0
    embedded: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    source_coverage: list[tuple[str, int]] = field(default_factory=list)
    probes: list[tuple[str, str, str, str]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "problem"]

    def ok(self) -> bool:
        return not self.problems


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or url).lower()
    return host[4:] if host.startswith("www.") else host


def audit(store: Any, manifest: Any, retriever: Any, log: Any = None) -> AuditReport:
    """Walk the knowledge base and report what would make Mark answer badly."""
    say = log or (lambda _message: None)
    report = AuditReport()
    documents = store.list_documents()
    stats = store.stats()

    report.documents = len(documents)
    report.chunks = stats.chunks
    report.embedded = stats.embedded_chunks
    report.by_kind = dict(stats.documents_by_kind)

    if not documents:
        report.findings.append(
            Finding("problem", "empty", "Nothing is stored at all.", "Run `mark ingest`.")
        )
        return report

    _check_embeddings(report, stats)
    _check_sources(report, manifest, documents, say)
    _check_content(report, store, documents, say)
    _check_retrieval(report, retriever, say)
    return report


def _check_embeddings(report: AuditReport, stats: Any) -> None:
    if stats.embedded_chunks == 0:
        report.findings.append(
            Finding(
                "warning",
                "search",
                "No passage has an embedding, so Mark only matches exact words.",
                "Add VOYAGE_API_KEY to .env and run `mark embed`.",
            )
        )
        return
    missing = stats.chunks - stats.embedded_chunks
    if missing > 0:
        share = missing / stats.chunks
        report.findings.append(
            Finding(
                "problem" if share > 0.05 else "warning",
                "search",
                f"{missing:,} of {stats.chunks:,} passages have no embedding "
                f"({share:.0%}), so semantic search cannot see them.",
                "Run `mark embed` again; it resumes where it stopped.",
            )
        )


def _check_sources(report: AuditReport, manifest: Any, documents: list, say: Any) -> None:
    """Every listed source should have produced something. A zero is a silent failure."""
    say("Checking each listed source contributed something...")
    stored_hosts = Counter(
        _host(d.link or d.locator) for d in documents if d.kind == SourceKind.WEBSITE
    )

    for site in manifest.websites:
        host = _host(site.url)
        count = stored_hosts.get(host, 0)
        report.source_coverage.append((host, count))
        if count == 0:
            report.findings.append(
                Finding(
                    "problem",
                    "source",
                    f"{host} is listed in sources.yaml but contributed no pages.",
                    f"Run `mark sources probe {site.url}` to see what it returns.",
                )
            )

    for kind, label, listed in (
        (SourceKind.YOUTUBE, "YouTube", bool(manifest.youtube.has_sources())),
        (SourceKind.PODCAST, "Podcast", bool(manifest.podcast.rss or manifest.podcast.episodes)),
    ):
        count = sum(1 for d in documents if d.kind == kind)
        report.source_coverage.append((label, count))
        if listed and count == 0:
            report.findings.append(
                Finding(
                    "problem",
                    "source",
                    f"{label} is configured but nothing from it is stored.",
                    f"Run `mark ingest --only {kind.value}`.",
                )
            )


def _check_content(report: AuditReport, store: Any, documents: list, say: Any) -> None:
    """Thin pages, pages with no passages, and text repeated across the whole store."""
    say(f"Checking the text of {len(documents):,} documents...")

    thin = [d for d in documents if len(d.text.split()) < MIN_WORDS]
    if thin:
        report.findings.append(
            Finding(
                "warning",
                "content",
                f"{len(thin):,} documents hold under {MIN_WORDS} words, e.g. {thin[0].locator}",
                "Usually listing or gallery pages. Add their URL pattern to exclude_patterns.",
            )
        )

    chunkless = [d for d in documents if not store.chunks_for_document(d.id)]
    if chunkless:
        report.findings.append(
            Finding(
                "problem",
                "content",
                f"{len(chunkless):,} documents produced no searchable passages, "
                f"e.g. {chunkless[0].locator}",
                "Re-ingest with --force; if it persists the text is unusable.",
            )
        )

    identical = Counter(d.content_hash for d in documents if d.content_hash)
    repeated = [(h, n) for h, n in identical.items() if n > 1]
    if repeated:
        worst = max(n for _, n in repeated)
        report.findings.append(
            Finding(
                "problem",
                "content",
                f"{len(repeated):,} texts are stored more than once "
                f"(one appears {worst} times), so a search returns the same passage twice.",
                "Run `mark ingest --only website --force --prune`.",
            )
        )

    # Text repeated across a quarter of the store is site furniture that survived.
    paragraphs: Counter[str] = Counter()
    for document in documents:
        paragraphs.update({p.strip() for p in document.text.split("\n") if len(p.strip()) > 60})
    threshold = max(3, int(len(documents) * NEAR_DUPLICATE_SHARE))
    furniture = [(p, n) for p, n in paragraphs.items() if n >= threshold]
    if furniture:
        worst_text, worst_count = max(furniture, key=lambda item: item[1])
        report.findings.append(
            Finding(
                "problem",
                "content",
                f"{len(furniture)} blocks of text appear on {threshold:,}+ documents "
                f'(worst: {worst_count:,} copies of "{worst_text[:60]}...").',
                "That is a menu or footer diluting every passage. "
                "Run `mark ingest --only website --force --prune`.",
            )
        )


def _check_retrieval(report: AuditReport, retriever: Any, say: Any) -> None:
    """The real test: can a landlord's question find anything?"""
    say(f"Asking {len(PROBE_QUESTIONS)} real questions of the index...")
    uncovered: list[str] = []

    for topic, question in PROBE_QUESTIONS:
        try:
            result = retriever.retrieve(question)
        except Exception as exc:  # a broken retriever is the finding
            report.findings.append(Finding("problem", "retrieval", f"Retrieval failed: {exc}", ""))
            return
        top = result.chunks[0].document.title[:48] if result.chunks else "-"
        report.probes.append((topic, question, result.coverage, top))
        if result.coverage == "none":
            uncovered.append(topic)

    if uncovered:
        report.findings.append(
            Finding(
                "warning" if len(uncovered) < 5 else "problem",
                "retrieval",
                f"{len(uncovered)} of {len(PROBE_QUESTIONS)} topics find nothing: "
                f"{', '.join(sorted(set(uncovered)))}.",
                "Mark will answer 'not covered' for these. Add sources on those topics.",
            )
        )
