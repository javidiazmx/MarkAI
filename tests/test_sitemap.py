"""Diffing a site's own page list against the knowledge base.

"Are all the blog posts in there?" cannot be answered by counting what was stored: a crawl
that missed twenty posts looks exactly like one that found everything.
"""

from __future__ import annotations

import httpx
import respx

from markai.sitemap import diff_against_store, discover_sitemaps, read_sitemap

INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://gc.test/sitemap-posts.xml</loc></sitemap>
  <sitemap><loc>https://gc.test/sitemap-pages.xml</loc></sitemap>
</sitemapindex>"""

POSTS = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://gc.test/blog/eviction-hurdles</loc></url>
  <url><loc>https://gc.test/blog/five-day-notice</loc></url>
  <url><loc>https://gc.test/blog/cash-for-keys</loc></url>
</urlset>"""

PAGES = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://gc.test/about</loc></url>
</urlset>"""


def _xml(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "application/xml"})


def _mock(respx_mock) -> None:
    respx_mock.get("https://gc.test/robots.txt").mock(
        return_value=httpx.Response(200, text="Sitemap: https://gc.test/sitemap.xml")
    )
    respx_mock.get("https://gc.test/sitemap.xml").mock(return_value=_xml(INDEX))
    respx_mock.get("https://gc.test/sitemap-posts.xml").mock(return_value=_xml(POSTS))
    respx_mock.get("https://gc.test/sitemap-pages.xml").mock(return_value=_xml(PAGES))


@respx.mock(assert_all_called=False)
def test_robots_names_the_sitemap(respx_mock):
    _mock(respx_mock)
    with httpx.Client() as client:
        assert discover_sitemaps("https://gc.test/blog", client) == ["https://gc.test/sitemap.xml"]


@respx.mock(assert_all_called=False)
def test_a_sitemap_index_is_followed_to_the_pages(respx_mock):
    _mock(respx_mock)
    with httpx.Client() as client:
        pages = read_sitemap("https://gc.test/sitemap.xml", client)
    assert len(pages) == 4
    assert "https://gc.test/blog/cash-for-keys" in pages


@respx.mock(assert_all_called=False)
def test_it_names_the_posts_that_are_not_stored(respx_mock):
    _mock(respx_mock)
    stored = {"d1": "https://gc.test/blog/eviction-hurdles", "d2": "https://gc.test/about"}
    with httpx.Client() as client:
        diff = diff_against_store("https://gc.test", stored, client)

    assert len(diff.listed) == 4
    assert sorted(diff.missing) == [
        "https://gc.test/blog/cash-for-keys",
        "https://gc.test/blog/five-day-notice",
    ]
    assert diff.coverage == 0.5


@respx.mock(assert_all_called=False)
def test_a_section_narrows_it_to_the_blog(respx_mock):
    """The real question is about the blog, not the whole site."""
    _mock(respx_mock)
    stored = {"d1": "https://gc.test/blog/eviction-hurdles"}
    with httpx.Client() as client:
        diff = diff_against_store("https://gc.test", stored, client, path_contains="/blog")

    assert len(diff.listed) == 3, "the /about page is not a blog post"
    assert len(diff.missing) == 2


@respx.mock(assert_all_called=False)
def test_a_stored_url_matches_whatever_form_the_sitemap_uses(respx_mock):
    """Trailing slashes and tracking parameters must not read as a missing page."""
    respx_mock.get("https://gc.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://gc.test/sitemap.xml").mock(
        return_value=_xml(
            '<?xml version="1.0"?><urlset><url><loc>https://gc.test/blog/a/</loc></url></urlset>'
        )
    )
    with httpx.Client() as client:
        diff = diff_against_store("https://gc.test", {"d1": "https://gc.test/blog/a"}, client)
    assert diff.missing == []


@respx.mock(assert_all_called=False)
def test_a_section_matching_nothing_is_told_apart_from_an_empty_sitemap(respx_mock):
    """The CLI needs to tell "this site has no sitemap" from "the sitemap has pages, just
    none in that section" - the second one is a wrong --section value, not a dead site."""
    _mock(respx_mock)
    with httpx.Client() as client:
        diff = diff_against_store("https://gc.test", {}, client, path_contains="/nonexistent")
    assert diff.listed == []
    assert diff.total_on_site == 4, "the sitemap had pages - none matched the section"


@respx.mock(assert_all_called=False)
def test_a_leading_or_missing_slash_matches_the_same_section(respx_mock):
    """A shell that mangles a leading slash still has a sane value to try: no slash at all."""
    _mock(respx_mock)
    with httpx.Client() as client:
        with_slash = diff_against_store("https://gc.test", {}, client, path_contains="/blog")
    with httpx.Client() as client:
        without_slash = diff_against_store("https://gc.test", {}, client, path_contains="blog")
    assert with_slash.listed == without_slash.listed and len(with_slash.listed) == 3


@respx.mock(assert_all_called=False)
def test_a_sitemap_named_twice_in_robots_txt_is_only_tried_once(respx_mock):
    """Some sites repeat "Sitemap:" once per user-agent block in robots.txt."""
    respx_mock.get("https://gc.test/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(
                "User-agent: *\nSitemap: https://gc.test/sitemap.xml\n\n"
                "User-agent: GPTBot\nSitemap: https://gc.test/sitemap.xml\n"
            ),
        )
    )
    respx_mock.get("https://gc.test/sitemap.xml").mock(return_value=_xml(INDEX))
    respx_mock.get("https://gc.test/sitemap-posts.xml").mock(return_value=_xml(POSTS))
    respx_mock.get("https://gc.test/sitemap-pages.xml").mock(return_value=_xml(PAGES))
    with httpx.Client() as client:
        diff = diff_against_store("https://gc.test", {}, client)
    assert diff.sitemaps == ["https://gc.test/sitemap.xml"]


@respx.mock(assert_all_called=False)
def test_a_site_with_no_sitemap_reports_nothing_listed(respx_mock):
    respx_mock.get("https://bare.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get(url__regex=r"https://bare\.test/.*").mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        diff = diff_against_store("https://bare.test", {}, client)
    assert diff.listed == [] and diff.missing == []


# --- saying why, not just that -------------------------------------------------------------


def test_the_reason_a_page_is_absent_comes_from_the_last_run(tmp_path):
    """ "Missing" alone sends the owner grepping a log; most of the time it was on purpose."""
    from markai.sitemap import reasons_from_last_run

    details = tmp_path / "last-ingest.txt"
    details.write_text(
        "Ingest run t0 -> t1\n"
        "added 675, updated 0, unchanged 0, removed 0, failed 4\n"
        "\n"
        "FAILURES\n"
        "       4 x Nothing but the site's own menus and footer\n"
        "\n"
        "[website] https://gc.test/blog/operation-keep-me-warm\n"
        "    Nothing but the site's own menus and footer\n"
        "    -> Listing and gallery pages usually look like this.\n"
        "[website] https://gc.test/blog/charitable-events\n"
        "    HTTP 404 for https://gc.test/blog/charitable-events\n"
        "[youtube] https://www.youtube.com/watch?v=x\n"
        "    No captions available.\n",
        encoding="utf-8",
    )

    reasons = reasons_from_last_run(
        details,
        [
            "https://gc.test/blog/operation-keep-me-warm",
            "https://gc.test/blog/charitable-events",
            "https://gc.test/blog/never-attempted",
        ],
    )
    assert reasons["https://gc.test/blog/operation-keep-me-warm"].startswith("Nothing but")
    assert "404" in reasons["https://gc.test/blog/charitable-events"]
    assert "https://gc.test/blog/never-attempted" not in reasons, "silence is its own answer"
    assert not any("youtube" in k for k in reasons), "only the URLs asked about"


def test_a_missing_details_file_is_not_an_error(tmp_path):
    from markai.sitemap import reasons_from_last_run

    assert reasons_from_last_run(tmp_path / "nope.txt", ["https://x.test/a"]) == {}
