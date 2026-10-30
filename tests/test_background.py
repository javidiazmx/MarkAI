"""`mark export`: the company's own material, as background for another model."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from markai.cli import app
from markai.models import Document, SourceKind
from markai.sources.background import (
    background_pack,
    boilerplate_lines,
    is_own,
    sections,
)
from markai.sources.manifest import BusinessProfile

runner = CliRunner()
TODAY = "2026-10-30"

NAV = "Home About Services Contact"
FOOTER = "© GC Realty & Development. All rights reserved."


def _doc(title, url, text, kind=SourceKind.WEBSITE, **over):
    doc = Document(
        id=Document.make_id(kind, url),
        kind=kind,
        title=title,
        locator=url,
        text=text,
        link=url,
        **over,
    )
    doc.ensure_hash()
    return doc


def _site(n, *, blog=False, body=None):
    """A crawl of n pages that all carry the same nav and footer, as a real one does."""
    slug = "blog/" if blog else ""
    return [
        _doc(
            f"Page {i}",
            f"https://www.gcrealtyinc.com/{slug}page-{i}",
            f"{NAV}\nSomething only page {i} says about {body or 'managing rentals'}.\n{FOOTER}",
            published_at=f"2026-0{(i % 9) + 1}-01",
        )
        for i in range(n)
    ]


# --- scope: whose material is this -----------------------------------------------------


@pytest.mark.parametrize(
    ("url", "own"),
    [
        ("https://www.gcrealtyinc.com/blog/late-fees", True),
        ("https://gcrealtyinc.com/leasing-faq", True),
        ("https://www.straightupchicagoinvestor.com/podcast/311", True),
        ("https://www.cookcountyil.gov/service/permits", False),
        ("https://www.hud.gov/vawa", False),
    ],
)
def test_only_the_companys_own_pages_count_as_background(url, own):
    """A county permits page is something Jay reads, not something GC Realty is."""
    assert is_own(_doc("x", url, "text")) is own


def test_the_command_leaves_the_government_sites_out_by_default(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("business:\n  name: GC Realty\nwebsites: []\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data))

    from markai.knowledge.store import KnowledgeStore

    store = KnowledgeStore(data / "markai.db")
    store.upsert_document(
        _doc("Our services", "https://www.gcrealtyinc.com/services", "We manage rentals."), []
    )
    store.upsert_document(
        _doc("Permits", "https://www.cookcountyil.gov/permits", "Demolition permitting."), []
    )
    store.close()

    assert runner.invoke(app, ["export"]).exit_code == 0
    body = (data / "gc-background.md").read_text(encoding="utf-8")
    assert "We manage rentals." in body
    assert "Demolition permitting." not in body

    assert runner.invoke(app, ["export", "--everything"]).exit_code == 0
    assert "Demolition permitting." in (data / "gc-background.md").read_text(encoding="utf-8")


# --- boilerplate, which is most of a crawl ---------------------------------------------


def test_the_site_template_is_stripped_once_not_printed_on_every_page():
    docs = _site(40)
    drop = boilerplate_lines(docs)
    assert NAV in drop and FOOTER in drop

    body = background_pack(docs, BusinessProfile(name="GC Realty"), TODAY)[0]
    assert body.count(NAV) == 0
    assert body.count("Something only page 7 says") == 1


def test_a_small_crawl_keeps_everything_rather_than_eating_its_own_content():
    """With six pages, one shared sentence clears any share threshold. Strip nothing."""
    docs = _site(6)
    assert boilerplate_lines(docs) == set()
    assert NAV in background_pack(docs, BusinessProfile(), TODAY)[0]


def test_a_long_repeated_paragraph_is_prose_not_a_footer():
    """A boilerplate line is a nav item. A sentence somebody wrote stays in."""
    long_line = "We have managed Chicagoland rentals since 2005 and " + "we believe " * 25
    docs = [
        _doc(f"Page {i}", f"https://www.gcrealtyinc.com/p{i}", f"{NAV}\n{long_line}\nUnique {i}.")
        for i in range(30)
    ]
    drop = boilerplate_lines(docs)
    assert NAV in drop
    assert long_line not in drop


# --- grouping ---------------------------------------------------------------------------


def test_blog_posts_and_site_pages_are_separate_sections():
    docs = _site(14) + _site(14, blog=True)
    found = {s.key for s in sections(docs, episodes=False)}
    assert found == {"pages", "posts"}


def test_transcripts_are_left_out_unless_asked_for_and_carry_their_caveat():
    docs = _site(14) + [
        _doc(
            "Episode 311",
            "https://www.straightupchicagoinvestor.com/311",
            "So the thing about boilers is you want to get ahead of it.",
            kind=SourceKind.PODCAST,
            episode="311",
        )
    ]
    assert {s.key for s in sections(docs, episodes=False)} == {"pages"}

    with_shows = background_pack(docs, BusinessProfile(), TODAY, episodes=True)[0]
    assert "boilers" in with_shows
    assert "does not record who is speaking" in with_shows


def test_a_page_keeps_its_title_date_and_link():
    docs = [
        _doc(
            "Late fees in Chicago",
            "https://www.gcrealtyinc.com/blog/late-fees",
            "Ten dollars on the first five hundred.",
            published_at="2025-03-04",
        )
    ]
    body = background_pack(docs, BusinessProfile(), TODAY)[0]
    assert "### Late fees in Chicago" in body
    assert "2025-03-04" in body
    assert "<https://www.gcrealtyinc.com/blog/late-fees>" in body


# --- splitting ---------------------------------------------------------------------------


def test_one_part_when_it_fits():
    assert len(background_pack(_site(14), BusinessProfile(), TODAY)) == 1


def test_it_splits_on_a_post_boundary_never_through_a_post():
    docs = [
        _doc(f"Post {i}", f"https://www.gcrealtyinc.com/blog/p{i}", f"{NAV}\n" + f"body{i} " * 400)
        for i in range(30)
    ]
    parts = background_pack(docs, BusinessProfile(), TODAY, part_chars=60_000)
    assert len(parts) > 1
    whole = "\n".join(parts)
    for i in range(30):
        heading = f"### Post {i}\n"
        assert whole.count(heading) == 1, "every post appears exactly once"
        # The body of a post never straddles two parts. (Match on the heading with its
        # newline: "### Post 1" is a substring of "### Post 19".)
        holding = [p for p in parts if heading in p]
        assert len(holding) == 1
        assert f"body{i} " in holding[0]


def test_a_part_may_overrun_rather_than_cut_a_post_in_half():
    """The size is a target, not a cap - the alternative is half a post in each file."""
    docs = [
        _doc(f"Post {i}", f"https://www.gcrealtyinc.com/blog/p{i}", f"{NAV}\n" + f"body{i} " * 400)
        for i in range(30)
    ]
    limit = 60_000
    parts = background_pack(docs, BusinessProfile(), TODAY, part_chars=limit)
    longest = max(len(p) for p in parts)
    one_post = max(len(d.text) for d in docs)
    assert limit < longest <= limit + one_post + 1_000


def test_every_part_says_which_part_it_is_and_where_the_front_matter_went():
    docs = [
        _doc(f"Post {i}", f"https://www.gcrealtyinc.com/blog/p{i}", f"{NAV}\n" + f"body{i} " * 400)
        for i in range(30)
    ]
    parts = background_pack(docs, BusinessProfile(), TODAY, part_chars=60_000)
    for index, body in enumerate(parts, start=1):
        assert f"part {index} of {len(parts)}" in body
    assert "Part 1 has the front matter" in parts[1]


# --- the front matter ---------------------------------------------------------------------


def test_the_pack_says_it_is_a_point_of_view_not_a_neutral_source():
    """It is a management company's own marketing. A reader should know that up front."""
    body = background_pack(_site(14), BusinessProfile(name="GC Realty"), TODAY)[0]
    assert "point of view" in body.lower()
    assert "not as verified fact" in body


def test_the_business_details_lead_the_pack():
    business = BusinessProfile(
        name="GC Realty & Development",
        services="Residential property management",
        service_area="Cook, DuPage, Lake",
        contact_url="https://gcrealtyinc.com",
    )
    body = background_pack(_site(14), business, TODAY)[0]
    assert "GC Realty & Development" in body
    assert "Residential property management" in body
    assert "Cook, DuPage, Lake" in body


def test_an_empty_index_fails_instead_of_writing_an_empty_pack(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data))

    result = runner.invoke(app, ["export"])
    assert result.exit_code != 0
    assert not (data / "gc-background.md").exists()


def test_the_fact_layer_is_opt_in_and_lands_beside_the_background(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("business:\n  name: GC Realty\nwebsites: []\n", encoding="utf-8")
    (tmp_path / "facts.yaml").write_text(
        "ordinances:\n"
        "  - id: mined-1\n"
        "    jurisdiction: Chicago\n"
        "    topic: Late fees\n"
        "    rule: Ten dollars on the first five hundred.\n"
        "    citation: CRLTO 5-12-140(h)\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data))

    from markai.knowledge.store import KnowledgeStore

    store = KnowledgeStore(data / "markai.db")
    store.upsert_document(
        _doc("Our services", "https://www.gcrealtyinc.com/services", "We manage rentals."), []
    )
    store.close()

    assert runner.invoke(app, ["export"]).exit_code == 0
    assert not (data / "gc-context-pack.md").exists()

    assert runner.invoke(app, ["export", "--facts"]).exit_code == 0
    assert (data / "gc-context-pack.md").exists()
    assert (data / "gc-context-instructions.md").exists()
    assert (data / "gc-background.md").exists()
