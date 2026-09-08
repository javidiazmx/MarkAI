"""Ask a site for its own index of pages, and say which of them Jay does not have.

"Are all the blog posts in there?" cannot be answered by counting what was stored - a crawl
that missed twenty posts looks exactly like one that found everything. The site publishes
the list itself in sitemap.xml, so the honest answer is a diff against that.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from markai.ingest.websites import canonical_url, fetch_page
from markai.models import IngestError

logger = logging.getLogger(__name__)

# Where sites keep it when robots.txt does not say.
COMMON_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap-index.xml")
MAX_SITEMAPS = 50

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_SITEMAP_TAG = re.compile(r"<sitemap>", re.IGNORECASE)


def reasons_from_last_run(details_path: Path, urls: list[str]) -> dict[str, str]:
    """Why each URL is absent, read out of the last run's own record.

    "Missing" without a reason sends the owner grepping a log by hand every time, and most
    of the time the answer is that the page was deliberately dropped - nothing but the
    site's footer, or a copy of another page.
    """
    try:
        lines = details_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    wanted = {url.rstrip("/") for url in urls}
    found: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        if line.startswith("[website] "):
            locator = stripped.removeprefix("[website] ").rstrip("/")
            current = locator if locator in wanted else None
        elif current and stripped and not stripped.startswith("->"):
            found.setdefault(current, stripped)
            current = None
    return found


@dataclass
class SitemapDiff:
    """What the site says it has, against what is stored."""

    sitemaps: list[str] = field(default_factory=list)
    listed: list[str] = field(default_factory=list)
    stored: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return len(self.stored) / len(self.listed) if self.listed else 0.0


def discover_sitemaps(base_url: str, client: httpx.Client) -> list[str]:
    """robots.txt names them when it can be read; otherwise try the usual places."""
    parts = urlsplit(base_url)
    root = f"{parts.scheme}://{parts.netloc}"
    found: list[str] = []
    try:
        response = client.get(f"{root}/robots.txt")
        if response.status_code == 200:
            for line in response.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    url = line.split(":", 1)[1].strip()
                    if url:
                        found.append(url)
    except httpx.HTTPError as exc:
        logger.debug("no robots.txt for %s: %s", root, exc)
    return found or [f"{root}{path}" for path in COMMON_PATHS]


def read_sitemap(url: str, client: httpx.Client, seen: set[str] | None = None) -> list[str]:
    """Every page URL in a sitemap, following index files one level down."""
    seen = seen if seen is not None else set()
    if url in seen or len(seen) > MAX_SITEMAPS:
        return []
    seen.add(url)
    try:
        fetched = fetch_page(url, client, max_bytes=30_000_000)
    except IngestError as exc:
        logger.debug("could not read sitemap %s: %s", url, exc)
        return []

    body = fetched.html
    locations = _LOC_RE.findall(body)
    if _SITEMAP_TAG.search(body):
        # An index of sitemaps, not of pages.
        pages: list[str] = []
        for child in locations:
            pages.extend(read_sitemap(child, client, seen))
        return pages
    return locations


def diff_against_store(
    base_url: str,
    store_locators: dict[str, str],
    client: httpx.Client,
    path_contains: str | None = None,
) -> SitemapDiff:
    """Compare the site's own page list against what is stored.

    ``path_contains`` narrows it to one section, which is how you ask "are all the blog
    posts in there?" rather than "is the whole site in there?".
    """
    diff = SitemapDiff()
    diff.sitemaps = discover_sitemaps(base_url, client)

    listed: list[str] = []
    for sitemap in diff.sitemaps:
        listed.extend(read_sitemap(sitemap, client))
        if listed:
            break  # the first one that answers is the site's real index

    known = {canonical_url(locator) for locator in store_locators.values()}
    for url in listed:
        if path_contains and path_contains.lower() not in urlsplit(url).path.lower():
            continue
        canon = canonical_url(url)
        if canon in diff.listed:
            continue
        diff.listed.append(canon)
        (diff.stored if canon in known else diff.missing).append(canon)
    return diff
