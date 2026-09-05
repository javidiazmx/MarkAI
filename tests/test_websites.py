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
    extract_pdf_text,
    fetch_page,
    ingest_websites,
)
from markai.models import Document, IngestError, IngestFailure, SourceKind
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
def test_fetch_page_rejects_what_it_cannot_read(respx_mock):
    respx_mock.get("https://example.com/a.jpg").mock(
        return_value=httpx.Response(
            200, content=b"\xff\xd8\xff", headers={"content-type": "image/jpeg"}
        )
    )
    with httpx.Client() as client, pytest.raises(IngestError):
        fetch_page("https://example.com/a.jpg", client)


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


# --- oversized pages ----------------------------------------------------------------------

BIG_PAGE = (
    "<html><head><title>Blog</title></head><body>"
    "<h1>Chicago landlord blog</h1>"
    "<p>Security deposits must earn interest every year.</p>"
    "<p>Hold the money in a separate account in Illinois.</p>"
    "<!--" + "padding" * 200000 + "-->"
    "</body></html>"
)


@respx.mock(assert_all_called=False)
def test_an_oversized_page_is_truncated_not_discarded(respx_mock):
    """A page builder inlines megabytes of CSS; the article is still at the top."""
    respx_mock.get("https://example.com/blog").mock(
        return_value=httpx.Response(200, text=BIG_PAGE, headers={"content-type": "text/html"})
    )
    with httpx.Client() as client:
        fetched = fetch_page("https://example.com/blog", client, max_bytes=2000)

    assert fetched.truncated is True
    title, text = extract_main_text(fetched.html, "https://example.com/blog")
    assert "Security deposits must earn interest" in text


@respx.mock(assert_all_called=False)
def test_a_page_under_the_limit_is_not_marked_truncated(respx_mock):
    respx_mock.get("https://example.com/deposits").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    )
    with httpx.Client() as client:
        fetched = fetch_page("https://example.com/deposits", client)
    assert fetched.truncated is False


@respx.mock(assert_all_called=False)
def test_an_empty_response_is_still_a_failure(respx_mock):
    respx_mock.get("https://example.com/blank").mock(
        return_value=httpx.Response(200, content=b"", headers={"content-type": "text/html"})
    )
    with httpx.Client() as client, pytest.raises(IngestError):
        fetch_page("https://example.com/blank", client)


@respx.mock(assert_all_called=False)
def test_a_huge_blog_page_now_reaches_the_knowledge_base(respx_mock, tmp_path, settings):
    settings = settings.model_copy(update={"crawl_delay_seconds": 0.0, "max_page_bytes": 2000})
    respx_mock.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://example.com/blog").mock(
        return_value=httpx.Response(200, text=BIG_PAGE, headers={"content-type": "text/html"})
    )
    source = WebsiteSource(url="https://example.com/blog")
    with httpx.Client() as client:
        results = list(ingest_websites([source], tmp_path, client, settings))

    documents = [r for r in results if isinstance(r, Document)]
    assert len(documents) == 1
    assert "Security deposits" in documents[0].text
    assert documents[0].metadata["truncated"] is True


@respx.mock(assert_all_called=False)
def test_broken_pages_cannot_run_past_max_pages(respx_mock, tmp_path, settings):
    """Failures used to be free, so a site full of them crawled without a ceiling."""
    settings = settings.model_copy(update={"crawl_delay_seconds": 0.0})
    respx_mock.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    links = "".join(f'<a href="/p{i}">p{i}</a>' for i in range(50))
    respx_mock.get("https://example.com/start").mock(
        return_value=httpx.Response(
            200,
            text=f"<html><body><p>Start</p>{links}</body></html>",
            headers={"content-type": "text/html"},
        )
    )
    broken = respx_mock.get(url__regex=r"https://example\.com/p\d+").mock(
        return_value=httpx.Response(500)
    )

    source = WebsiteSource(url="https://example.com/start", crawl=True, max_pages=10)
    with httpx.Client() as client:
        list(ingest_websites([source], tmp_path, client, settings))

    assert broken.call_count <= 9, "the budget must cover attempts, not just successes"


# --- links that are downloads, not pages ---------------------------------------------------


def test_discover_links_skips_downloads_not_pages():
    """48 image fetches per run spent 48 pages of the budget and 48 rows of the failure table."""
    html = (
        "".join(
            f'<a href="/x{i}{suffix}">x</a>'
            for i, suffix in enumerate([".jpg", ".png", ".zip", ".docx", ".css", ".woff2"])
        )
        + '<a href="/real-article">a</a><a href="/blog/2024/deposits">b</a>'
    )
    assert discover_links(html, "https://example.com/", [], []) == [
        "https://example.com/real-article",
        "https://example.com/blog/2024/deposits",
    ]


def test_a_dot_in_a_directory_name_does_not_look_like_a_file():
    html = '<a href="/v1.2/guide">a</a><a href="/deposits">b</a>'
    links = discover_links(html, "https://example.com/", [], [])
    assert links == ["https://example.com/v1.2/guide", "https://example.com/deposits"]


@respx.mock(assert_all_called=False)
def test_a_pdf_listed_by_hand_is_still_attempted(respx_mock, tmp_path, settings):
    """Filtering is for links we discover. What the owner typed, we try."""
    respx_mock.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://example.com/rlto.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF", headers={"content-type": "text/html"})
    )
    source = WebsiteSource(url="https://example.com/rlto.pdf")
    with httpx.Client() as client:
        results = list(ingest_websites([source], tmp_path, client, settings))
    assert len(results) == 1


# --- PDFs ----------------------------------------------------------------------------------


def _tiny_pdf(line: str, title: str | None = "RLTO Summary") -> bytes:
    """A real, minimal PDF, so these tests exercise pypdf rather than a stand-in."""
    content = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if title:
        objects.append(f"<< /Title ({title}) >>".encode())
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start_xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    info_entry = b" /Info %d 0 R" % len(objects) if title else b""
    out += b"trailer\n<< /Size %d /Root 1 0 R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        info_entry,
        start_xref,
    )
    return bytes(out)


def test_extract_pdf_text_reads_the_words_and_the_title():
    title, text = extract_pdf_text(_tiny_pdf("Deposits earn interest."), "https://x.test/a.pdf")
    assert "Deposits earn interest." in text
    assert title == "RLTO Summary"


def test_a_pdf_without_a_title_is_named_from_its_filename():
    pdf = _tiny_pdf("Lead paint disclosure rules.", title=None)
    title, _ = extract_pdf_text(pdf, "https://epa.gov/lead-paint_pamphlet.pdf")
    assert title == "lead paint pamphlet"


def test_a_scanned_pdf_says_it_needs_ocr():
    """No selectable text means page images. Saying "empty" would send the owner hunting."""
    blank = _tiny_pdf(" ")
    with pytest.raises(IngestError) as excinfo:
        extract_pdf_text(blank, "https://x.test/scan.pdf")
    assert "OCR" in (excinfo.value.hint or "")


def test_a_damaged_pdf_fails_without_taking_the_run_down():
    with pytest.raises(IngestError):
        extract_pdf_text(b"%PDF-1.4\nnot really a pdf", "https://x.test/broken.pdf")


@respx.mock(assert_all_called=False)
def test_a_pdf_is_ingested_like_any_other_page(respx_mock, tmp_path, settings):
    respx_mock.get("https://chicago.gov/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://chicago.gov/rlto.pdf").mock(
        return_value=httpx.Response(
            200,
            content=_tiny_pdf("A tenant may withhold rent under the ordinance."),
            headers={"content-type": "application/pdf"},
        )
    )
    source = WebsiteSource(url="https://chicago.gov/rlto.pdf")
    with httpx.Client() as client:
        results = list(ingest_websites([source], tmp_path, client, settings))

    documents = [r for r in results if isinstance(r, Document)]
    assert len(documents) == 1
    assert "withhold rent under the ordinance" in documents[0].text
    assert documents[0].metadata["format"] == "pdf"
    assert documents[0].content_hash


@respx.mock(assert_all_called=False)
def test_a_pdf_mislabelled_by_the_server_is_still_read(respx_mock):
    """Plenty of county servers send application/octet-stream. %PDF- is the real signal."""
    respx_mock.get("https://county.test/form.pdf").mock(
        return_value=httpx.Response(
            200,
            content=_tiny_pdf("Eviction filing fee schedule."),
            headers={"content-type": "text/plain"},
        )
    )
    with httpx.Client() as client:
        fetched = fetch_page("https://county.test/form.pdf", client)
    assert fetched.pdf is not None
    assert "Eviction filing fee schedule." in extract_pdf_text(fetched.pdf, "u")[1]


@respx.mock(assert_all_called=False)
def test_a_crawl_now_follows_pdf_links(respx_mock, tmp_path, settings):
    settings = settings.model_copy(update={"crawl_delay_seconds": 0.0})
    respx_mock.get("https://chicago.gov/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://chicago.gov/housing").mock(
        return_value=httpx.Response(
            200,
            text="<html><body><h1>Housing</h1><p>Read the summary.</p>"
            '<a href="/rlto.pdf">RLTO</a><a href="/logo.png">logo</a></body></html>',
            headers={"content-type": "text/html"},
        )
    )
    respx_mock.get("https://chicago.gov/rlto.pdf").mock(
        return_value=httpx.Response(
            200,
            content=_tiny_pdf("Security deposit interest is set each year."),
            headers={"content-type": "application/pdf"},
        )
    )
    logo = respx_mock.get("https://chicago.gov/logo.png").mock(return_value=httpx.Response(200))

    source = WebsiteSource(url="https://chicago.gov/housing", crawl=True, max_pages=10)
    with httpx.Client() as client:
        documents = [
            r
            for r in ingest_websites([source], tmp_path, client, settings)
            if isinstance(r, Document)
        ]

    assert {d.locator for d in documents} == {
        "https://chicago.gov/housing",
        "https://chicago.gov/rlto.pdf",
    }
    assert logo.call_count == 0, "images are still skipped"


# --- site furniture ------------------------------------------------------------------------
#
# A property manager's site repeats the same marketing block on every listing page. Stored
# as-is, a search for "how much do I return to the tenant" matched five identical listings,
# because the only text those pages carried was the footer.

FOOTER = (
    "<p>The GC Realty Experience is providing the right solutions, being easy to do business "
    "with, and leaving you with a remarkable customer experience.</p>"
    "<p>Anything less than that is not acceptable!</p>"
    "<p>Only speak Spanish? Not a problem, we have you covered.</p>"
)


def _listing(address: str) -> str:
    return (
        f"<html><head><title>{address}</title></head><body><h1>{address}</h1>{FOOTER}</body></html>"
    )


ARTICLE = (
    "<html><head><title>Deposits</title></head><body><h1>Security deposits</h1>"
    "<p>Chicago landlords owe interest on a held deposit every single year, at the rate the "
    "City Comptroller publishes each January without fail.</p>"
    "<p>The deposit itself must go back within forty five days of the tenant moving out, with "
    "an itemised statement of anything you deducted from it.</p>"
    "<p>Keep the money in a separate federally insured account inside Illinois, never mixed "
    "with your own operating funds, or the damages dwarf the deposit.</p>"
    f"{FOOTER}</body></html>"
)


@respx.mock(assert_all_called=False)
def test_repeated_furniture_is_stripped_and_hollow_pages_are_dropped(
    respx_mock, tmp_path, settings
):
    settings = settings.model_copy(update={"crawl_delay_seconds": 0.0})
    respx_mock.get("https://gc.test/robots.txt").mock(return_value=httpx.Response(404))
    links = "".join(f'<a href="/listing-{i}">l{i}</a>' for i in range(4))
    respx_mock.get("https://gc.test/deposits").mock(
        return_value=httpx.Response(
            200,
            text=ARTICLE.replace("</body>", f"{links}</body>"),
            headers={"content-type": "text/html"},
        )
    )
    for i in range(4):
        respx_mock.get(f"https://gc.test/listing-{i}").mock(
            return_value=httpx.Response(
                200, text=_listing(f"{100 + i} West Street"), headers={"content-type": "text/html"}
            )
        )

    source = WebsiteSource(url="https://gc.test/deposits", crawl=True, max_pages=10)
    with httpx.Client() as client:
        results = list(ingest_websites([source], tmp_path, client, settings))

    documents = [r for r in results if isinstance(r, Document)]
    failures = [r for r in results if isinstance(r, IngestFailure)]

    assert len(documents) == 1, "only the article survives; the listings were all footer"
    assert "forty five days" in documents[0].text
    assert "remarkable customer experience" not in documents[0].text, "the footer is gone"
    assert documents[0].metadata["boilerplate_removed"] is True
    assert len(failures) == 4
    assert "menus and footer" in failures[0].reason
    assert "exclude_patterns" in (failures[0].hint or "")


@respx.mock(assert_all_called=False)
def test_a_small_site_is_left_alone(respx_mock, tmp_path, settings):
    """Two pages are not evidence of a pattern; stripping there would just lose content."""
    settings = settings.model_copy(update={"crawl_delay_seconds": 0.0})
    respx_mock.get("https://small.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://small.test/a").mock(
        return_value=httpx.Response(
            200,
            text=ARTICLE.replace("</body>", '<a href="/b">b</a></body>'),
            headers={"content-type": "text/html"},
        )
    )
    respx_mock.get("https://small.test/b").mock(
        return_value=httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
    )
    source = WebsiteSource(url="https://small.test/a", crawl=True, max_pages=5)
    with httpx.Client() as client:
        documents = [
            r
            for r in ingest_websites([source], tmp_path, client, settings)
            if isinstance(r, Document)
        ]
    assert len(documents) == 2
    assert all("remarkable customer experience" in d.text for d in documents)


def test_stripping_leaves_a_site_with_no_repetition_untouched():
    from markai.ingest.websites import _strip_shared_boilerplate

    docs = []
    for i in range(6):
        doc = Document(
            id=f"d{i}",
            kind=SourceKind.WEBSITE,
            title=f"Page {i}",
            locator=f"https://x.test/{i}",
            text=f"Paragraph about topic {i}.\n\nAnother distinct thought about {i}. " * 12,
        )
        doc.ensure_hash()
        docs.append(doc)

    kept, hollow = _strip_shared_boilerplate(docs, lambda _m: None)
    assert len(kept) == 6 and hollow == []
