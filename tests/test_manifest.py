"""The sources manifest: schema, shipped templates, warnings, and the private override."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from markai.config import PROJECT_ROOT
from markai.sources.manifest import (
    SourceManifest,
    load_manifest,
    resolve_manifest_path,
    save_manifest,
)
from markai.sources.template import SOURCES_TEMPLATE

SHIPPED_TEMPLATE = PROJECT_ROOT / "sources" / "sources.template.yaml"
SHIPPED_EXAMPLE = PROJECT_ROOT / "sources" / "sources.example.yaml"
LIVE_MANIFEST = PROJECT_ROOT / "sources" / "sources.yaml"


def test_shipped_template_is_valid_and_empty():
    manifest = load_manifest(SHIPPED_TEMPLATE)
    assert manifest.is_empty()
    assert manifest.business.is_empty()
    assert manifest.counts()["websites"] == 0


def test_shipped_example_validates_and_is_populated():
    manifest = load_manifest(SHIPPED_EXAMPLE)
    counts = manifest.counts()
    assert not manifest.is_empty()
    assert counts["websites"] == 2
    assert counts["youtube_episodes"] == 2
    assert counts["tools"] == 2
    assert manifest.podcast.rss
    assert manifest.business.name


def test_the_live_manifest_is_valid():
    """sources.yaml holds the owner's real sources; it must always parse."""
    manifest = load_manifest(LIVE_MANIFEST)
    assert manifest.warnings() == []
    for site in manifest.websites:
        assert site.url.startswith("https://"), site.url
    for episode in manifest.youtube.episodes:
        assert "/@" not in episode.url, (
            f"{episode.url} is a channel, not a video. Channels go in channel_url; "
            "individual video URLs go in episodes or urls_file."
        )


def test_template_module_matches_the_shipped_file():
    assert SOURCES_TEMPLATE == SHIPPED_TEMPLATE.read_text(encoding="utf-8")
    assert SourceManifest.model_validate(yaml.safe_load(SOURCES_TEMPLATE)).is_empty()


def test_website_urls_must_be_http():
    with pytest.raises(ValueError):
        SourceManifest.model_validate({"websites": [{"url": "ftp://example.com/x"}]})


def test_a_missing_manifest_says_how_to_create_one(tmp_path):
    with pytest.raises(FileNotFoundError) as excinfo:
        load_manifest(tmp_path / "nope.yaml")
    assert "mark init" in str(excinfo.value)


def test_a_non_mapping_manifest_is_rejected(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(path)


def test_an_empty_file_loads_as_an_empty_manifest(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("", encoding="utf-8")
    assert load_manifest(path).is_empty()


def test_local_manifest_takes_precedence(tmp_path):
    (tmp_path / "sources.yaml").write_text("websites: []\n", encoding="utf-8")
    (tmp_path / "sources.local.yaml").write_text(
        "websites:\n  - url: https://private.test/page\n", encoding="utf-8"
    )
    assert resolve_manifest_path(tmp_path / "sources.yaml").name == "sources.local.yaml"
    assert load_manifest(tmp_path / "sources.yaml").counts()["websites"] == 1


def test_tokens_in_urls_produce_a_warning():
    manifest = SourceManifest.model_validate(
        {"podcast": {"rss": "https://feeds.test/show?auth=secret123"}}
    )
    warnings = manifest.warnings()
    assert len(warnings) == 1
    assert "sources.local.yaml" in warnings[0]


def test_a_clean_manifest_has_no_warnings():
    assert load_manifest(SHIPPED_EXAMPLE).warnings() == []


def test_urls_file_counts_as_content():
    manifest = SourceManifest.model_validate({"youtube": {"urls_file": "sources/urls.txt"}})
    assert not manifest.is_empty()


def test_save_and_reload_roundtrip(tmp_path: Path):
    manifest = SourceManifest.model_validate(
        {
            "websites": [{"url": "https://example.com/a", "title": "A"}],
            "business": {"name": "GC Realty", "never_say": ["fee quotes"]},
        }
    )
    path = tmp_path / "out.yaml"
    save_manifest(manifest, path)
    reloaded = load_manifest(path)
    assert reloaded.websites[0].title == "A"
    assert reloaded.business.never_say == ["fee quotes"]


# --- the owner's own exclusions ------------------------------------------------------------


def test_the_live_manifest_keeps_property_listings_out():
    """Each listing is the same marketing template with a different address on it.

    They carry nothing a landlord's question needs, they rotate weekly, and with max_pages
    raised to fit 550 blog posts there is now plenty of budget for them to flood into.
    """
    from pathlib import Path

    from markai.ingest.websites import discover_links
    from markai.sources.manifest import load_manifest

    site = load_manifest(Path("sources/sources.yaml")).websites[0]
    assert "gcrealtyinc.com" in site.url

    listings = [
        "/_system/listings/1266/313-Ridge-Road-Kenilworth",
        "/chicago-homes-for-rent",
        "/naperville-houses-for-rent",
        "/available-rentals",
        "/property-search?beds=2",
        "/tenant-portal",
    ]
    keepers = [
        "/blog/what-can-i-do-if-my-tenant-doesnt-pay-rent",
        "/chicago-property-management",
        "/tenant-placement",
        "/case-study",
    ]
    html = "".join(f'<a href="{u}">x</a>' for u in listings + keepers)
    kept = set(discover_links(html, site.url + "/", site.include_patterns, site.exclude_patterns))

    for url in listings:
        assert not any(url.split("?")[0] in k for k in kept), f"{url} should be excluded"
    for url in keepers:
        assert any(url in k for k in kept), f"{url} should be kept"


def test_the_blog_seed_cannot_wander_off_the_blog():
    from pathlib import Path

    from markai.ingest.websites import discover_links
    from markai.sources.manifest import load_manifest

    manifest = load_manifest(Path("sources/sources.yaml"))
    blog = next(w for w in manifest.websites if w.url.endswith("/blog"))
    html = (
        '<a href="/blog/five-day-notice">a</a>'
        '<a href="/chicago-homes-for-rent">b</a>'
        '<a href="/blog/tag/evictions">c</a>'
    )
    kept = discover_links(html, blog.url, blog.include_patterns, blog.exclude_patterns)
    assert kept == ["https://www.gcrealtyinc.com/blog/five-day-notice"]


def test_a_pattern_that_is_not_a_valid_regex_is_caught_at_load():
    """It used to surface as a crash partway through a 2,000-page crawl."""
    import pytest

    from markai.sources.manifest import WebsiteSource

    with pytest.raises(ValueError) as excinfo:
        WebsiteSource(url="https://x.test", exclude_patterns=["?pg="])
    assert "not a valid pattern" in str(excinfo.value)
    assert "\\\\?pg=" in str(excinfo.value) or "\\?pg=" in str(excinfo.value), "shows the fix"

    WebsiteSource(url="https://x.test", exclude_patterns=["\\?pg=", "/tag/"])


def test_the_live_manifest_has_somewhere_to_hand_a_hard_case():
    """Jay only offers the call when a real link is configured, so this must not go missing."""
    from pathlib import Path

    from markai.advisor.prompt_builder import build_business_block
    from markai.sources.manifest import load_manifest

    business = load_manifest(Path("sources/sources.yaml")).business
    assert business.escalation_url and business.escalation_url.startswith("https://calendly.com/")
    assert business.escalation_name == "Russell"

    block = build_business_block(business)
    assert business.escalation_url in block
    assert "only when it is genuinely warranted" in block
