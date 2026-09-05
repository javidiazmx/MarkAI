"""Website ingestion: canonical URLs, extraction, robots, and crawling."""

from __future__ import annotations

import httpx
import pytest
import respx

from markai.ingest.websites import (
    RobotsCache,
    canonical_url,
    discover_links,
    extract_main_text,
    fetch_page,
    ingest_websites,
)
from markai.models import Document, IngestError, IngestFailure
from markai.sources.manifest import WebsiteSource

PAGE = """<html><head><title>Deposits</title></head><body>
<nav>skip me</nav>
<h1>Security deposits</h1>
<p>Chicago landlords must pay interest every year on held deposits.</p>
<p>Keep the money in a separate federally insured account in Illinois.</p>
<ul><li>Pay within thirty days</li><li>Document everything</li></ul>
<a href="/second">Second page</a>
<a href="https://other.test/x">Off site</a>
</body></html>"""

SECOND = """<html><head><title>Notices</title></head><body>
<h1>Notices</h1><p>A five day notice starts the clock on unpaid rent.</p>
<p>File in the right courtroom or you start over.</p></body></html>"""


def test_canonical_url_normalizes_the_noise():
    assert (
        canonical_url("HTTP://Example.COM/a/?utm_source=x&b=2#frag") == "http://example.com/a?b=2"
    )
    assert canonical_url("https://example.com/") == "https://example.com/"
    assert canonical_url("https://example.com/a/") == "https://example.com/a"


def test_canonical_url_sorts_query_parameters():
    assert canonical_url("https://x.test/a?b=2&a=1") == canonical_url("https://x.test/a?a=1&b=2")


def test_extract_main_text_keeps_block_structure():
    title, text = extract_main_text(PAGE, "https://example.com/deposits")
    assert "deposits" in title.lower()
    assert "interest every year" in text
    assert "skip me" not in text
    assert "\n" in text


def test_extract_main_text_survives_junk_html():
    title, text = extract_main_text("<html><body></body></html>", "https://x.test/a")
    assert isinstance(title, str) and isinstance(text, str)


def test_discover_links_stays_on_the_same_host():
    links = discover_links(PAGE, "https://example.com/deposits", [], [])
    assert links == ["https://example.com/second"]


def test_discover_links_honours_exclude_patterns():
    html = '<a href="/blog/tag/x">t</a><a href="/blog/post">p</a>'
    links = discover_links(html, "https://example.com/", [], ["/tag/"])
    assert links == ["https://example.com/blog/post"]


@respx.mock(assert_all_called=False)
def test_fetch_page_rejects_non_html(respx_mock):
    respx_mock.get("https://example.com/a.pdf").mock(
        return_value=httpx.Response(
            200, content=b"%PDF", headers={"content-type": "application/pdf"}
        )
    )
    with httpx.Client() as client, pytest.raises(IngestError):
        fetch_page("https://example.com/a.pdf", client)


@respx.mock(assert_all_called=False)
def test_fetch_page_reports_a_login_wall(respx_mock):
    respx_mock.get("https://example.com/private").mock(return_value=httpx.Response(403))
    with httpx.Client() as client, pytest.raises(IngestError) as excinfo:
        fetch_page("https://example.com/private", client)
    assert excinfo.value.hint


@respx.mock(assert_all_called=False)
def test_robots_cache_uses_the_injected_client(respx_mock):
    respx_mock.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\nCrawl-delay: 2")
    )
    with httpx.Client() as client:
        robots = RobotsCache(client)
        assert robots.can_fetch("https://example.com/public") is True
        assert robots.can_fetch("https://example.com/private/x") is False
        assert robots.crawl_delay("https://example.com/") == 2


@respx.mock(assert_all_called=False)
def test_robots_failure_allows_everything(respx_mock):
    respx_mock.get("https://example.com/robots.txt").mock(return_value=httpx.Response(500))
    with httpx.Client() as client:
        assert RobotsCache(client).can_fetch("https://example.com/anything") is True


@respx.mock(assert_all_called=False)
def test_ingest_single_page(respx_mock, tmp_path, settings):
    respx_mock.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://example.com/deposits").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    )
    source = WebsiteSource(url="https://example.com/deposits", title="Deposits")
    with httpx.Client() as client:
        results = list(ingest_websites([source], tmp_path, client, settings))
    documents = [r for r in results if isinstance(r, Document)]
    assert len(documents) == 1
    assert documents[0].locator == "https://example.com/deposits"
    assert documents[0].link == "https://example.com/deposits"
    assert documents[0].content_hash
    assert "interest every year" in documents[0].text


@respx.mock(assert_all_called=False)
def test_crawl_respects_max_pages(respx_mock, tmp_path, settings):
    settings = settings.model_copy(update={"crawl_delay_seconds": 0.0})
    respx_mock.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://example.com/deposits").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    )
    respx_mock.get("https://example.com/second").mock(
        return_value=httpx.Response(200, text=SECOND, headers={"content-type": "text/html"})
    )
    source = WebsiteSource(url="https://example.com/deposits", crawl=True, max_pages=1)
    with httpx.Client() as client:
        documents = [
            r
            for r in ingest_websites([source], tmp_path, client, settings)
            if isinstance(r, Document)
        ]
    assert len(documents) == 1


@respx.mock(assert_all_called=False)
def test_crawl_follows_links_up_to_the_cap(respx_mock, tmp_path, settings):
    settings = settings.model_copy(update={"crawl_delay_seconds": 0.0})
    respx_mock.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://example.com/deposits").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    )
    respx_mock.get("https://example.com/second").mock(
        return_value=httpx.Response(200, text=SECOND, headers={"content-type": "text/html"})
    )
    source = WebsiteSource(url="https://example.com/deposits", crawl=True, max_pages=5)
    with httpx.Client() as client:
        documents = [
            r
            for r in ingest_websites([source], tmp_path, client, settings)
            if isinstance(r, Document)
        ]
    assert {d.locator for d in documents} == {
        "https://example.com/deposits",
        "https://example.com/second",
    }


@respx.mock(assert_all_called=False)
def test_disallowed_seed_reports_a_fixable_failure(respx_mock, tmp_path, settings):
    respx_mock.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /")
    )
    source = WebsiteSource(url="https://example.com/deposits")
    with httpx.Client() as client:
        results = list(ingest_websites([source], tmp_path, client, settings))
    failures = [r for r in results if isinstance(r, IngestFailure)]
    assert len(failures) == 1
    assert "robots" in failures[0].reason.lower()
    assert "ignore_robots" in (failures[0].hint or "")


@respx.mock(assert_all_called=False)
def test_ignore_robots_overrides_the_block(respx_mock, tmp_path, settings):
    respx_mock.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /")
    )
    respx_mock.get("https://example.com/deposits").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    )
    source = WebsiteSource(url="https://example.com/deposits", ignore_robots=True)
    with httpx.Client() as client:
        documents = [
            r
            for r in ingest_websites([source], tmp_path, client, settings)
            if isinstance(r, Document)
        ]
    assert len(documents) == 1
