"""Website ingestion: fetch pages over httpx, extract the main text, optionally crawl.

Every request goes through an injected ``httpx.Client`` so tests can intercept traffic with
``respx``. robots.txt is fetched through the same client (never ``RobotFileParser.read()``).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from bs4 import BeautifulSoup

from markai.config import Settings
from markai.models import Document, IngestError, IngestFailure, SourceKind
from markai.sources.manifest import WebsiteSource

logger = logging.getLogger(__name__)

USER_AGENT = "MarkAI/0.1 (+https://github.com/javidiazmx/MarkAI; landlord knowledge assistant)"

_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid"}
_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "blockquote", "pre"]
_STRIP_TAGS = ["script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form"]
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\r\f\v]+")

# Links a crawler should never follow. Fetching them costs a request, spends a page from
# max_pages, and lands in the failure table as "Not an HTML page" - noise that hides the
# failures worth reading. A URL listed by hand in the manifest is still fetched: this only
# filters links found while crawling.
_NOT_A_PAGE = frozenset(
    """
    .jpg .jpeg .png .gif .webp .svg .ico .bmp .tif .tiff .avif .heic
    .mp3 .mp4 .m4a .mov .avi .wmv .webm .wav .ogg .flac
    .zip .gz .tgz .bz2 .7z .rar .dmg .exe .msi .pkg .apk
    .doc .docx .xls .xlsx .ppt .pptx .odt .ods .rtf
    .css .js .json .xml .rss .atom .txt .csv
    .woff .woff2 .ttf .otf .eot
    """.split()
)

# Indirection so tests can neutralise politeness delays without patching ``time``.
_sleep: Callable[[float], None] = time.sleep


def canonical_url(url: str) -> str:
    """Normalise a URL for identity: lowercase scheme/host, drop fragment and tracking params,
    sort the remaining query params, strip a trailing slash (unless the path is ``/``)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _host_key(netloc: str) -> str:
    host = netloc.lower()
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if host.startswith("www."):
        host = host[4:]
    return host


def make_client(timeout: float = 30.0) -> httpx.Client:
    """An httpx client that follows redirects and identifies itself honestly."""
    return httpx.Client(
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        },
        timeout=timeout,
    )


def _decode_body(body: bytes, response: httpx.Response) -> str:
    charset = response.charset_encoding
    if not charset:
        match = _META_CHARSET_RE.search(body[:4096])
        if match:
            charset = match.group(1).decode("ascii", errors="ignore")
    for encoding in (charset, "utf-8"):
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


class PageFetch(NamedTuple):
    """One fetched page. ``truncated`` means it hit ``max_bytes`` and we kept the head.

    ``pdf`` holds the raw bytes when the response was a PDF, and ``html`` is empty in that
    case: the two are read by different extractors.
    """

    final_url: str
    html: str
    truncated: bool = False
    pdf: bytes | None = None


def fetch_page(url: str, client: httpx.Client, max_bytes: int = 25_000_000) -> PageFetch:
    """GET an HTML page.

    Raises ``IngestError`` on a non-2xx status (401/403 carry a login hint) or a non-HTML
    content type. A page bigger than ``max_bytes`` is **truncated, not rejected**: page
    builders inline megabytes of CSS and JavaScript, and the article text is near the top,
    so keeping the head beats discarding the page.
    """
    try:
        with client.stream("GET", url) as response:
            status = response.status_code
            final_url = str(response.url)
            if status in (401, 403):
                raise IngestError(
                    f"HTTP {status} for {url}",
                    hint=(
                        "This page requires login or blocks automated readers. Mark can only "
                        "read pages that open without signing in; save the content as a "
                        "transcript-style .txt file instead, or drop the entry."
                    ),
                )
            if status == 404:
                raise IngestError(
                    f"HTTP 404 for {url}", hint="The page does not exist. Check the URL."
                )
            if status == 429:
                raise IngestError(
                    f"HTTP 429 for {url}",
                    hint="The site is rate-limiting us. Wait a while and re-run `mark ingest`.",
                )
            if not (200 <= status < 300):
                raise IngestError(f"HTTP {status} for {url}")
            content_type = response.headers.get("content-type", "").lower()
            is_pdf = "pdf" in content_type
            if (
                content_type
                and not is_pdf
                and not any(marker in content_type for marker in ("html", "xml", "text/plain"))
            ):
                raise IngestError(
                    f"Not a readable page ({content_type.split(';')[0].strip()}): {url}",
                    hint="Mark reads web pages and PDFs. Anything else has to be pasted "
                    "into a .txt file.",
                )
            chunks: list[bytes] = []
            total = 0
            truncated = False
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    truncated = True
                    logger.info("truncating %s at %s bytes", url, f"{total:,}")
                    break
            body = b"".join(chunks)
    except httpx.HTTPError as exc:
        raise IngestError(
            f"Could not fetch {url}: {type(exc).__name__}: {exc}",
            hint="Check the URL and your internet connection, then re-run `mark ingest`.",
        ) from exc
    if not body.strip():
        raise IngestError(f"Empty response from {url}", hint="The page returned no content.")
    # Some servers label a PDF text/plain or application/octet-stream; the magic number is
    # the only thing that never lies.
    if is_pdf or body[:5] == b"%PDF-":
        return PageFetch(final_url, "", truncated, body)
    return PageFetch(final_url, _decode_body(body, response), truncated)


def _fallback_extract(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    blocks: list[str] = []
    for element in soup.find_all(_BLOCK_TAGS):
        if element.find_parent(_BLOCK_TAGS) is not None and element.name not in ("li", "td", "th"):
            # Nested block inside another block we already captured; avoid duplicate text.
            continue
        text = _WS_RE.sub(" ", element.get_text(" ", strip=True)).strip()
        if text:
            blocks.append(text)
    # Drop exact duplicates while keeping order (menus repeated in the body, etc.).
    seen: set[str] = set()
    unique = []
    for block in blocks:
        if block not in seen:
            seen.add(block)
            unique.append(block)
    return "\n\n".join(unique)


def extract_main_text(html: str, url: str) -> tuple[str, str]:
    """Return ``(title, text)`` for an HTML document.

    trafilatura's output is used when it is non-empty and contains a newline; otherwise a
    BeautifulSoup block-element fallback runs (short pages collapse into one paragraph in
    trafilatura, which would defeat paragraph-based chunking).
    """
    text = ""
    try:
        extracted = trafilatura.extract(html, url=url, include_comments=False, include_tables=True)
    except Exception as exc:  # trafilatura is defensive but not infallible
        logger.debug("trafilatura failed for %s: %s", url, exc)
        extracted = None
    if extracted and "\n" in extracted.strip():
        text = extracted.strip()

    soup: BeautifulSoup | None = None
    if not text:
        soup = BeautifulSoup(html, "lxml")
        text = _fallback_extract(soup)

    title = ""
    try:
        metadata = trafilatura.extract_metadata(html, default_url=url)
        if metadata is not None and metadata.title:
            title = str(metadata.title).strip()
    except Exception as exc:
        logger.debug("trafilatura metadata failed for %s: %s", url, exc)
    if not title:
        if soup is None:
            soup = BeautifulSoup(html, "lxml")
        if soup.title and soup.title.string:
            title = _WS_RE.sub(" ", soup.title.string).strip()
    if not title:
        title = url
    return title, text


def extract_pdf_text(data: bytes, url: str) -> tuple[str, str]:
    """Return ``(title, text)`` for a PDF. Pages are separated by blank lines so the
    chunker sees paragraph boundaries the same way it does for HTML.

    Raises ``IngestError`` for a PDF that holds no extractable text, which in practice
    means a scan: the bytes are page images and reading them would need OCR.
    """
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # An empty user password is common for print-restricted documents.
            try:
                reader.decrypt("")
            except Exception as exc:
                raise IngestError(
                    f"PDF is password protected: {url}",
                    hint="Save an unlocked copy, or paste the text into a .txt file.",
                ) from exc
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        raw_title = (reader.metadata or {}).get("/Title") if reader.metadata else None
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(
            f"Could not read the PDF at {url}: {type(exc).__name__}: {exc}",
            hint="The file may be damaged. Open it yourself to check.",
        ) from exc

    text = "\n\n".join(page for page in pages if page)
    if not text.strip():
        raise IngestError(
            f"PDF has no selectable text: {url}",
            hint=(
                "It is almost certainly a scan. Run it through OCR, or paste the text "
                "into a .txt file."
            ),
        )
    title = _WS_RE.sub(" ", str(raw_title)).strip() if raw_title else ""
    if not title:
        # Fall back to the filename: "RLTO-Summary-2024.pdf" reads better than a bare URL.
        stem = urlsplit(url).path.rsplit("/", 1)[-1]
        title = stem.removesuffix(".pdf").replace("-", " ").replace("_", " ").strip() or url
    return title, _WS_RE.sub(" ", text).strip()


def _is_a_file_not_a_page(path: str) -> bool:
    """True for a link that is plainly a download rather than a page to read."""
    dot = path.rfind(".")
    if dot == -1 or "/" in path[dot:]:
        return False
    return path[dot:].lower() in _NOT_A_PAGE


def discover_links(
    html: str, base_url: str, include_patterns: list[str], exclude_patterns: list[str]
) -> list[str]:
    """Same-host http(s) links in document order, canonicalised and deduplicated.

    ``include_patterns`` (when non-empty) must match at least once and ``exclude_patterns``
    must not match; both are regexes searched against the canonical URL.
    """
    base_host = _host_key(urlsplit(base_url).netloc)
    includes = [re.compile(p) for p in include_patterns]
    excludes = [re.compile(p) for p in exclude_patterns]
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if _host_key(parts.netloc) != base_host:
            continue
        if _is_a_file_not_a_page(parts.path):
            continue
        canon = canonical_url(absolute)
        if canon in seen:
            continue
        if includes and not any(p.search(canon) for p in includes):
            continue
        if any(p.search(canon) for p in excludes):
            continue
        seen.add(canon)
        links.append(canon)
    return links


class RobotsCache:
    """One ``RobotFileParser`` per netloc, fetched through the injected httpx client.

    A robots.txt that cannot be fetched (network error or non-2xx) allows everything.
    """

    def __init__(self, client: httpx.Client, user_agent: str = USER_AGENT) -> None:
        self._client = client
        self._user_agent = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}

    def _parser_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        key = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        if key in self._parsers:
            return self._parsers[key]
        parser: RobotFileParser | None = None
        robots_url = f"{key}/robots.txt"
        try:
            response = self._client.get(robots_url)
            if 200 <= response.status_code < 300:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
            else:
                logger.debug(
                    "robots.txt %s returned %s; allowing", robots_url, response.status_code
                )
        except Exception as exc:
            logger.debug("robots.txt fetch failed for %s: %s; allowing", robots_url, exc)
            parser = None
        self._parsers[key] = parser
        return parser

    def can_fetch(self, url: str) -> bool:
        parser = self._parser_for(url)
        if parser is None:
            return True
        try:
            return bool(parser.can_fetch(self._user_agent, url))
        except Exception:
            return True

    def crawl_delay(self, url: str) -> float | None:
        parser = self._parser_for(url)
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(self._user_agent)
        except Exception:
            return None
        if delay is None:
            return None
        try:
            return float(delay)
        except (TypeError, ValueError):
            return None


def _cache_html(cache_dir: Path, canonical: str, html: str) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
        (cache_dir / f"{digest}.html").write_text(html, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not cache HTML for %s: %s", canonical, exc)


def ingest_websites(
    sources: list[WebsiteSource],
    cache_dir: Path,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[Document | IngestFailure]:
    """Yield a ``Document`` per fetched page (or an ``IngestFailure``) for each manifest entry.

    With ``crawl: true`` the seed's same-host links are followed breadth-first up to
    ``max_pages``. Every URL is checked against robots.txt unless ``ignore_robots`` is set.
    """
    emit = log or logger.info
    base_delay = settings.crawl_delay_seconds if settings else 0.5
    max_bytes = settings.max_page_bytes if settings else 5_000_000
    timeout = settings.http_timeout_seconds if settings else 30.0
    own_client = client is None
    client = client or make_client(timeout)
    robots = RobotsCache(client)
    last_request: float | None = None

    def pace(url: str) -> None:
        nonlocal last_request
        delay = base_delay
        robots_delay = robots.crawl_delay(url) if not_ignoring else None
        if robots_delay:
            delay = max(delay, robots_delay)
        if last_request is not None and delay > 0:
            elapsed = time.monotonic() - last_request
            if elapsed < delay:
                _sleep(delay - elapsed)
        last_request = time.monotonic()

    try:
        for source in sources:
            not_ignoring = not source.ignore_robots
            seed_canon = canonical_url(source.url)
            queue: deque[str] = deque([source.url])
            visited: set[str] = {seed_canon}
            max_pages = source.max_pages if source.crawl else 1
            fetched_pages = 0
            while queue and fetched_pages < max_pages:
                url = queue.popleft()
                canon = canonical_url(url)
                is_seed = canon == seed_canon
                if not_ignoring and not robots.can_fetch(url):
                    if is_seed:
                        yield IngestFailure(
                            kind=SourceKind.WEBSITE,
                            locator=canon,
                            reason="robots.txt disallows this URL",
                            hint=(
                                "If you own the site or have permission, set ignore_robots: "
                                "true on this entry"
                            ),
                        )
                    else:
                        emit(f"Skipping {url} (robots.txt disallows)")
                    continue
                pace(url)
                # Count the attempt, not the success: a site full of broken pages must not
                # be able to run past max_pages.
                fetched_pages += 1
                try:
                    fetched = fetch_page(url, client, max_bytes=max_bytes)
                except IngestError as exc:
                    yield IngestFailure(
                        kind=SourceKind.WEBSITE, locator=canon, reason=str(exc), hint=exc.hint
                    )
                    continue
                final_url, html = fetched.final_url, fetched.html
                final_canon = canonical_url(final_url)
                visited.add(final_canon)
                is_pdf = fetched.pdf is not None
                _cache_html(cache_dir, final_canon, html)
                try:
                    if is_pdf:
                        title, text = extract_pdf_text(fetched.pdf or b"", final_url)
                    else:
                        title, text = extract_main_text(html, final_url)
                except IngestError as exc:
                    yield IngestFailure(
                        kind=SourceKind.WEBSITE,
                        locator=final_canon,
                        reason=str(exc),
                        hint=exc.hint,
                    )
                    continue
                except Exception as exc:
                    yield IngestFailure(
                        kind=SourceKind.WEBSITE,
                        locator=final_canon,
                        reason=f"Could not extract text: {type(exc).__name__}: {exc}",
                    )
                    continue
                if is_seed and source.title:
                    title = source.title
                if not text.strip():
                    yield IngestFailure(
                        kind=SourceKind.WEBSITE,
                        locator=final_canon,
                        reason="No readable text found on the page",
                        hint=(
                            "The page may be rendered by JavaScript. Paste its text into a "
                            ".txt transcript file or try a different URL."
                        ),
                    )
                else:
                    emit(f"Fetched {final_url} ({len(text.split()):,} words)")
                    document = Document(
                        id=Document.make_id(SourceKind.WEBSITE, final_canon),
                        kind=SourceKind.WEBSITE,
                        title=title,
                        locator=final_canon,
                        text=text,
                        link=final_url,
                        channel=source.title if not is_seed else None,
                        metadata={
                            "requested_url": url,
                            "domain": urlsplit(final_url).netloc.lower(),
                            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                            "notes": source.notes,
                            "truncated": fetched.truncated,
                            "format": "pdf" if is_pdf else "html",
                        },
                    )
                    document.ensure_hash()
                    yield document
                if source.crawl and fetched_pages < max_pages:
                    for link in discover_links(
                        html, final_url, source.include_patterns, source.exclude_patterns
                    ):
                        if link not in visited:
                            visited.add(link)
                            queue.append(link)
    finally:
        if own_client:
            client.close()
