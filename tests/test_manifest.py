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
