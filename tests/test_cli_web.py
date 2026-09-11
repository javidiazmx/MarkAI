"""The command line and the web API."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from markai.cli import app
from markai.config import Settings
from markai.web.app import create_app
from tests.fakes import FakeAdvisor

runner = CliRunner()


# --- CLI --------------------------------------------------------------------------------


def test_help_lists_every_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "doctor", "ingest", "status", "gaps", "search", "ask", "chat", "serve"):
        assert command in result.stdout


@pytest.mark.parametrize("group", ["sources", "calc"])
def test_sub_apps_have_help(group):
    result = runner.invoke(app, [group, "--help"])
    assert result.exit_code == 0


def test_calc_mortgage_prints_the_payment():
    result = runner.invoke(
        app,
        [
            "calc",
            "mortgage",
            "--price",
            "300000",
            "--down-pct",
            "25",
            "--rate",
            "6.5",
            "--years",
            "30",
        ],
    )
    assert result.exit_code == 0
    assert "1,422.15" in result.stdout
    assert "225,000" in result.stdout


def test_calc_deal_reports_the_ratios():
    result = runner.invoke(
        app,
        ["calc", "deal", "--price", "400000", "--rent", "4000", "--down-pct", "25", "--rate", "7"],
    )
    assert result.exit_code == 0
    for label in ("Cash flow", "Cap rate", "DSCR", "1% rule"):
        assert label in result.stdout


def test_sources_validate_accepts_a_temporary_manifest(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites:\n  - url: https://example.com/a\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))

    result = runner.invoke(app, ["sources", "validate"])
    assert result.exit_code == 0
    assert "valid" in result.stdout
    assert "websites: 1" in result.stdout


def test_sources_validate_reports_a_broken_manifest(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites:\n  - url: ftp://bad\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))

    result = runner.invoke(app, ["sources", "validate"])
    assert result.exit_code == 1


def test_ask_without_a_key_explains_what_to_do(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("markai.config.Settings.anthropic_key", lambda self: None)

    result = runner.invoke(app, ["ask", "What about deposits?"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.stdout + str(result.stderr)


def test_status_reports_an_empty_knowledge_base(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "not Mark Ainley" in result.stdout


def test_status_never_prints_the_key(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecretvalue")

    result = runner.invoke(app, ["status"])
    assert "supersecretvalue" not in result.stdout


# --- web --------------------------------------------------------------------------------


def _client(settings, store, advisor=None) -> TestClient:
    return TestClient(create_app(settings, advisor=advisor or FakeAdvisor(), store=store))


def test_health_and_status(settings, store):
    client = _client(settings, store)
    assert client.get("/api/health").json() == {"status": "ok"}

    status = client.get("/api/status").json()
    assert status["model"] == settings.model
    assert status["chunks"] > 0
    assert status["api_key_set"] is False
    assert "not Mark Ainley" in status["identity_notice"]


def test_status_exposes_no_secrets(settings, store):
    settings = settings.model_copy(update={"anthropic_api_key": "sk-ant-secret"})
    payload = json.dumps(_client(settings, store).get("/api/status").json())
    assert "sk-ant" not in payload
    assert "secret" not in payload


def test_sources_listing(settings, store):
    sources = _client(settings, store).get("/api/sources").json()["sources"]
    assert len(sources) == 3
    assert {s["kind"] for s in sources} == {"website", "youtube", "podcast"}


def test_chat_streams_text_and_a_final_event(settings, store):
    client = _client(settings, store)
    with client.stream(
        "POST", "/api/chat", json={"session_id": "s1", "message": "How do I screen tenants?"}
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: text" in body
    assert "event: done" in body
    assert "Here is the answer" in body


def test_an_empty_question_is_rejected(settings, store):
    response = _client(settings, store).post("/api/chat", json={"session_id": "s", "message": "  "})
    assert response.status_code == 400


def test_an_oversized_question_is_rejected(settings, store):
    settings = settings.model_copy(update={"max_question_chars": 50})
    response = _client(settings, store).post(
        "/api/chat", json={"session_id": "s", "message": "x" * 200}
    )
    assert response.status_code == 413


def test_the_daily_limit_stops_runaway_spend(settings, store):
    settings = settings.model_copy(update={"daily_question_limit": 1})
    client = _client(settings, store)
    with client.stream("POST", "/api/chat", json={"session_id": "s", "message": "one"}) as first:
        first.read()
    second = client.post("/api/chat", json={"session_id": "s", "message": "two"})
    assert second.status_code == 429


def test_the_per_session_limit_applies(settings, store):
    settings = settings.model_copy(update={"per_session_question_limit": 1})
    client = _client(settings, store)
    with client.stream("POST", "/api/chat", json={"session_id": "s", "message": "one"}) as first:
        first.read()
    assert client.post("/api/chat", json={"session_id": "s", "message": "two"}).status_code == 429


def test_an_access_code_gates_every_api_route(settings, store):
    settings = settings.model_copy(update={"web_access_code": "letmein"})
    client = _client(settings, store)
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/chat", json={"session_id": "s", "message": "hi"}).status_code == 401

    ok = client.get("/api/status", headers={"X-Access-Code": "letmein"})
    assert ok.status_code == 200
    assert ok.json()["access_code_required"] is True
    assert client.get("/api/status", headers={"X-Access-Code": "wrong"}).status_code == 401


def test_reset_starts_a_new_conversation(settings, store):
    advisor = FakeAdvisor()
    client = _client(settings, store, advisor)
    with client.stream("POST", "/api/chat", json={"session_id": "s", "message": "one"}) as first:
        first.read()
    assert client.post("/api/reset", json={"session_id": "s"}).json() == {"status": "reset"}


def test_the_index_page_is_served(settings, store):
    response = _client(settings, store).get("/")
    assert response.status_code == 200
    assert "Your Chicagoland AI Advisor" in response.text


def test_importing_the_module_needs_no_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import importlib

    import markai.web.app as web_app

    importlib.reload(web_app)
    assert web_app.app is not None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("", "no VOYAGE_API_KEY line"),
        ("# VOYAGE_API_KEY=pa-x", "still starts with #"),
        ("VOYAGE_API_KEY=", "no value after the ="),
        ('VOYAGE_API_KEY="pa-x"', "quotes around it"),
    ],
)
def test_doctor_explains_why_a_key_is_missing(tmp_path, line, expected):
    from markai.cli import _env_line_state

    env = tmp_path / ".env"
    env.write_text(f"ANTHROPIC_API_KEY=sk-ant-x\n{line}\n", encoding="utf-8")
    assert expected in _env_line_state(env, "VOYAGE_API_KEY")


def test_the_diagnosis_never_reveals_the_value(tmp_path):
    from markai.cli import _env_line_state

    env = tmp_path / ".env"
    env.write_text('VOYAGE_API_KEY="pa-supersecretvalue"\n', encoding="utf-8")
    assert "supersecretvalue" not in _env_line_state(env, "VOYAGE_API_KEY")


def test_a_missing_env_file_is_reported_not_raised(tmp_path):
    from markai.cli import _env_line_state

    assert "no .env file" in _env_line_state(tmp_path / "nope", "VOYAGE_API_KEY")


def test_check_urls_flags_a_domain_that_does_not_exist(tmp_path, monkeypatch, capsys):
    """A typo'd domain costs an hour of ingest; it should cost five seconds here."""
    import socket

    from markai.cli import _check_reachable

    real = socket.getaddrinfo

    def fake(host, *args, **kwargs):
        if host == "www.typodomain.com":
            raise OSError("Name or service not known")
        return real("localhost", None)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    _check_reachable(
        [("websites[0]", "https://www.good.com/a"), ("websites[1]", "https://www.typodomain.com")]
    )
    out = capsys.readouterr().out
    assert "does not resolve" in out
    assert "1 host(s) do not resolve" in out


def test_check_urls_is_quiet_when_everything_resolves(tmp_path, monkeypatch, capsys):
    import socket

    from markai.cli import _check_reachable

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [("ok",)])
    _check_reachable([("websites[0]", "https://www.good.com/a")])
    out = capsys.readouterr().out
    assert "resolves" in out
    assert "do not resolve" not in out


def test_validate_stays_offline_unless_asked(tmp_path, monkeypatch):
    """`sources validate` must not touch the network by default."""
    import socket

    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites:\n  - url: https://example.com/a\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))

    def boom(*args, **kwargs):
        raise AssertionError("validate must not resolve hostnames without --check-urls")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert runner.invoke(app, ["sources", "validate"]).exit_code == 0


# --- sources probe ----------------------------------------------------------------------


@respx.mock(assert_all_called=False)
def test_probe_reports_a_page_that_reads_fine(respx_mock, tmp_path, monkeypatch):
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))
    respx_mock.get("https://site.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://site.test/blog").mock(
        return_value=httpx.Response(
            200,
            text="<html><head><title>Blog</title></head><body><h1>Deposits</h1>"
            "<p>Interest is owed every year on a held deposit.</p>"
            "<p>Keep it in a separate Illinois account.</p>"
            '<a href="/next">next</a></body></html>',
            headers={"content-type": "text/html"},
        )
    )
    result = runner.invoke(app, ["sources", "probe", "https://site.test/blog"])
    assert result.exit_code == 0
    assert "words" in result.stdout
    assert "1 on the same host" in result.stdout


@respx.mock(assert_all_called=False)
def test_probe_names_a_javascript_page_instead_of_saying_nothing(respx_mock, tmp_path, monkeypatch):
    """The whole reason the command exists: a site that ingests to zero and never says why."""
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))
    respx_mock.get("https://spa.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://spa.test/").mock(
        return_value=httpx.Response(
            200,
            text='<html><head><title>App</title></head><body><div id="root"></div>'
            '<script src="/bundle.js"></script></body></html>',
            headers={"content-type": "text/html"},
        )
    )
    result = runner.invoke(app, ["sources", "probe", "https://spa.test/"])
    assert result.exit_code == 0
    assert "JavaScript" in result.stdout


@respx.mock(assert_all_called=False)
def test_probe_exits_non_zero_when_the_fetch_fails(respx_mock, tmp_path, monkeypatch):
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))
    respx_mock.get("https://gone.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://gone.test/x").mock(return_value=httpx.Response(404))
    result = runner.invoke(app, ["sources", "probe", "https://gone.test/x"])
    assert result.exit_code == 1
    assert "404" in result.stdout


FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<title>Straight Up Chicago Investor</title>
<link>https://www.realshow.test/</link>
<item><title>Ep 212</title><link>https://www.realshow.test/212</link></item>
<item><title>Ep 211</title><link>https://www.realshow.test/211</link></item>
</channel></rss>"""


@respx.mock(assert_all_called=False)
def test_probe_reads_a_feed_and_names_the_show_website(respx_mock, tmp_path, monkeypatch):
    """The feed is the authority on where the show's site lives - better than guessing."""
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))
    respx_mock.get("https://feeds.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://feeds.test/rss").mock(
        return_value=httpx.Response(200, text=FEED, headers={"content-type": "application/rss+xml"})
    )
    result = runner.invoke(app, ["sources", "probe", "https://feeds.test/rss"])
    assert result.exit_code == 0
    assert "RSS feed" in result.stdout
    assert "realshow.test" in result.stdout
    assert "Straight Up Chicago Investor" in result.stdout


def test_the_usage_line_shows_writes_beside_reads():
    """Reads alone hide the failure mode: paying 1.25x for a cache nothing collects."""
    from types import SimpleNamespace

    from markai.cli import _usage_line

    cold = SimpleNamespace(
        model="claude-opus-5",
        coverage="covered",
        usage={
            "input_tokens": 2,
            "output_tokens": 741,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 4200,
        },
    )
    assert "4,200 written" in _usage_line(cold)
    assert "no hit yet" in _usage_line(cold)

    warm = SimpleNamespace(
        model="claude-opus-5",
        coverage="covered",
        usage={
            "input_tokens": 2,
            "output_tokens": 741,
            "cache_read_input_tokens": 4200,
            "cache_creation_input_tokens": 0,
        },
    )
    assert "4,200 read" in _usage_line(warm)
    assert "no hit yet" not in _usage_line(warm)


# --- the stream the page has to parse ------------------------------------------------------


def test_the_server_separates_frames_with_crlf_and_the_page_expects_it():
    """Every answer arrived, was billed, and rendered as an empty bubble.

    sse-starlette ends every line with CRLF, so frames are separated by "\\r\\n\\r\\n". The
    page split on "\\n\\n", which never occurs in that stream: nothing was ever parsed. This
    pins both halves, because a library default is exactly the kind of thing that changes
    underneath you.
    """
    import re
    from pathlib import Path

    settings = Settings(_env_file=None, data_dir="/tmp/markai-sse-check")
    settings.ensure_dirs()
    advisor = FakeAdvisor("Chicago gives you 45 days.")
    client = TestClient(create_app(settings=settings, advisor=advisor))

    with client.stream(
        "POST", "/api/chat", json={"session_id": "s1", "message": "How long?"}
    ) as response:
        raw = b"".join(response.iter_bytes()).decode("utf-8")

    assert "\r\n\r\n" in raw, "this is the separator the page has to handle"
    assert "event: done" in raw

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    match = re.search(r"var FRAME_SEP = /([^/]+)/;", page)
    assert match, "the page must declare how it splits frames"
    separator = re.compile(match.group(1).replace("\\\\", "\\"))
    frames = [f for f in separator.split(raw) if f.strip()]
    assert len(frames) >= 2, "the page's own regex has to split this stream"
    assert any("Chicago gives you 45 days." in f for f in frames)


def test_a_handler_error_is_not_swallowed_as_a_keep_alive():
    """The catch around JSON.parse also caught render errors, hiding them as empty bubbles."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    body = page[page.index("function parseSse") : page.index("async function send")]
    handler = body.index("onEvent(name, payload)")
    catch = body.index("} catch (e) {")
    assert handler > body.index("payload = JSON.parse")
    assert handler > catch, "onEvent must sit outside the try that swallows parse failures"


# --- Jay, and a page that does not repeat itself -------------------------------------------


def test_the_page_carries_no_disclaimer_box_at_all():
    """Neither at the top of the session nor under each answer - the owner asked for both gone."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert '<div class="banner" id="banner">' not in page, "no permanent banner"
    assert "IDENTITY_NOTICE" not in page, "and no notice box under answers"
    # The standing notice moved to a card shown once on a first visit, which is where the
    # owner wanted it: read once, rather than furniture under every reply.
    assert 'id="welcome"' in page and "STANDING_NOTICE" in page
    assert "does not constitute legal advice" in page
    assert "jay_notice" in page, "and remembered, so it is not shown again"


def test_the_page_and_the_notice_call_the_assistant_jay():
    from pathlib import Path

    from markai.advisor.guardrails import IDENTITY_NOTICE

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert IDENTITY_NOTICE.startswith("Jay is an AI assistant")
    assert "Mark Ainley" in IDENTITY_NOTICE, "whose style it borrows is still named"
    assert "<h1>Jay</h1>" in page
    assert 'addMessage("mark", "Jay")' in page


def test_answers_render_as_markdown_not_asterisks():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert "function renderRich" in page and "function inline" in page
    # The word appears in a comment explaining why it is not used; the assignment must not.
    assert ".innerHTML" not in page.replace("never innerHTML", ""), (
        "answers are built from text nodes, so one can never inject markup into the page"
    )
    assert "insertAdjacentHTML" not in page and "document.write" not in page


def test_the_status_call_does_not_touch_the_removed_banner():
    """Referencing it threw, and the header said "Could not reach the server" instead."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    status = page[page.index("async function loadStatus") :]
    assert 'getElementById("banner")' not in status


def test_a_url_in_an_answer_becomes_a_link():
    """A Calendly link rendered as text made the landlord select and copy it by hand."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert "function linkify" in page
    assert 'link.rel = "noopener noreferrer"' in page
    assert "link.href = href" in page, "built from the matched text, never from markup"


def test_the_page_no_longer_shows_the_corpus_counts():
    """Sources and passages are operator diagnostics, not something a landlord reads."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert "passages · " not in page
    assert "keyword + semantic search" not in page
    assert "Your Chicagoland AI Advisor" in page


# --- attaching files to a question ---------------------------------------------------------


def _tiny_png_b64() -> str:
    import base64

    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode("ascii")


def _attach_client(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    settings.ensure_dirs()
    advisor = FakeAdvisor("The staining pattern suggests a supply line, not the roof.")
    return TestClient(create_app(settings=settings, advisor=advisor)), advisor


def test_a_photo_reaches_the_advisor(tmp_path):
    client, advisor = _attach_client(tmp_path)
    response = client.post(
        "/api/chat",
        json={
            "session_id": "s1",
            "message": "What is causing this?",
            "attachments": [
                {"name": "ceiling.png", "media_type": "image/png", "data": _tiny_png_b64()}
            ],
        },
    )
    assert response.status_code == 200
    body = response.text
    assert "supply line" in body
    assert len(advisor.attachments) == 1
    assert advisor.attachments[0].name == "ceiling.png"


def test_a_photo_with_no_question_still_asks_something(tmp_path):
    """Dragging in a photo and hitting send is a real thing people do."""
    client, advisor = _attach_client(tmp_path)
    response = client.post(
        "/api/chat",
        json={
            "session_id": "s1",
            "attachments": [
                {"name": "ceiling.png", "media_type": "image/png", "data": _tiny_png_b64()}
            ],
        },
    )
    assert response.status_code == 200
    assert advisor.questions[0], "a question was supplied on their behalf"


def test_a_file_type_that_cannot_be_read_is_rejected_before_any_api_call(tmp_path):
    client, advisor = _attach_client(tmp_path)
    import base64

    response = client.post(
        "/api/chat",
        json={
            "session_id": "s1",
            "message": "Have a look",
            "attachments": [
                {
                    "name": "setup.exe",
                    "media_type": "application/x-msdownload",
                    "data": base64.b64encode(b"MZ\x90\x00").decode("ascii"),
                }
            ],
        },
    )
    assert response.status_code == 400
    assert "setup.exe" in response.json()["detail"]
    assert advisor.questions == [], "nothing was billed"


def test_no_message_and_no_file_is_still_refused(tmp_path):
    client, _ = _attach_client(tmp_path)
    assert client.post("/api/chat", json={"session_id": "s1"}).status_code == 400


def test_the_page_has_an_attach_control_that_keeps_nothing():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="attach"' in page and 'id="file"' in page
    assert "readAsDataURL" in page
    assert "attachments: files" in page
    assert "MAX_PER_FILE" in page, "the browser refuses an oversized file before uploading it"


# --- saved conversations ----------------------------------------------------------------


def _ask(client, session_id, message, browser="b1"):
    headers = {"X-Browser-Id": browser} if browser else {}
    with client.stream(
        "POST", "/api/chat", json={"session_id": session_id, "message": message}, headers=headers
    ) as response:
        response.read()
    return response


def test_an_answer_is_saved_and_can_be_reopened(settings, store):
    client = _client(settings, store, FakeAdvisor("45 days in Chicago."))
    _ask(client, "t1", "How long for a deposit?")

    listed = client.get("/api/threads", headers={"X-Browser-Id": "b1"}).json()["threads"]
    assert [t["title"] for t in listed] == ["How long for a deposit"]
    assert "messages" not in listed[0], "the sidebar does not download every transcript"

    thread = client.get("/api/threads/t1", headers={"X-Browser-Id": "b1"}).json()
    assert [m["role"] for m in thread["messages"]] == ["user", "assistant"]
    assert thread["messages"][1]["content"] == "45 days in Chicago."


def test_another_browser_gets_its_own_list(settings, store):
    client = _client(settings, store)
    _ask(client, "t1", "Mine", browser="b1")

    assert client.get("/api/threads", headers={"X-Browser-Id": "b2"}).json() == {"threads": []}
    assert client.get("/api/threads/t1", headers={"X-Browser-Id": "b2"}).status_code == 404
    assert client.get("/api/threads").json() == {"threads": []}, "and no header sees nothing"


def test_without_a_browser_id_nothing_is_saved(settings, store):
    client = _client(settings, store)
    _ask(client, "t1", "How long for a deposit?", browser=None)
    assert client.get("/api/threads", headers={"X-Browser-Id": "b1"}).json() == {"threads": []}


def test_a_deleted_conversation_is_gone(settings, store):
    client = _client(settings, store)
    _ask(client, "t1", "First question")
    _ask(client, "t2", "Second question")

    gone = client.delete("/api/threads/t1", headers={"X-Browser-Id": "b1"})
    assert gone.json() == {"deleted": True}
    assert [
        t["id"]
        for t in client.get("/api/threads", headers={"X-Browser-Id": "b1"}).json()["threads"]
    ] == ["t2"]
    assert client.delete("/api/threads/t1", headers={"X-Browser-Id": "b1"}).json() == {
        "deleted": False
    }


def test_the_conversation_list_is_gated_by_the_access_code(settings, store):
    settings = settings.model_copy(update={"web_access_code": "letmein"})
    client = _client(settings, store)
    assert client.get("/api/threads", headers={"X-Browser-Id": "b1"}).status_code == 401
    assert client.delete("/api/threads/t1", headers={"X-Browser-Id": "b1"}).status_code == 401
    assert (
        client.get(
            "/api/threads", headers={"X-Browser-Id": "b1", "X-Access-Code": "letmein"}
        ).status_code
        == 200
    )


def test_a_failed_answer_is_not_saved(settings, store):
    advisor = FakeAdvisor()
    advisor.error = "Overloaded"
    client = _client(settings, store, advisor)
    _ask(client, "t1", "How long for a deposit?")
    assert client.get("/api/threads", headers={"X-Browser-Id": "b1"}).json() == {"threads": []}


def test_the_page_lists_saved_conversations_on_the_side():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="side"' in page and 'id="threads"' in page
    assert "X-Browser-Id" in page, "the list is keyed by a browser id the page sends"
    assert "/api/threads" in page
    assert "startNew" in page, "a new conversation gets a new id instead of reusing the old one"
    assert ".innerHTML" not in page, "titles are landlord text; they go in as text nodes"


# --- the episode index ------------------------------------------------------------------


def test_episodes_lists_the_catalog(tmp_path, monkeypatch, settings, store):
    monkeypatch.setenv("MARKAI_DATA_DIR", str(settings.data_dir))
    result = runner.invoke(app, ["episodes"])
    assert result.exit_code == 0
    assert "212" in result.stdout and "198" in result.stdout
    assert "Security deposit rules" not in result.stdout, "a web page is not an episode"


def test_episodes_searches_for_a_topic(tmp_path, monkeypatch, settings, store):
    monkeypatch.setenv("MARKAI_DATA_DIR", str(settings.data_dir))
    result = runner.invoke(app, ["episodes", "heat ordinance temperature"])
    assert result.exit_code == 0
    assert "Winter heat rules" in result.stdout
    assert "Ep. 198" in result.stdout


def test_episodes_says_so_when_nothing_matches(tmp_path, monkeypatch, settings, store):
    monkeypatch.setenv("MARKAI_DATA_DIR", str(settings.data_dir))
    result = runner.invoke(app, ["episodes", "zzzzq nonexistent topic"])
    assert result.exit_code == 0
    assert "Nothing in the episodes" in result.stdout


def test_episodes_rejects_an_unknown_kind(tmp_path, monkeypatch, settings, store):
    monkeypatch.setenv("MARKAI_DATA_DIR", str(settings.data_dir))
    result = runner.invoke(app, ["episodes", "--kind", "blogs"])
    assert result.exit_code == 1


def test_the_episodes_endpoint_searches_and_lists(settings, store):
    client = _client(settings, store)

    found = client.get("/api/episodes", params={"q": "heat ordinance", "limit": 3}).json()
    assert found["episodes"][0]["number"] == "198"
    assert found["episodes"][0]["timestamp"] is not None

    listed = client.get("/api/episodes").json()
    assert [e["number"] for e in listed["episodes"]] == ["212", "198"]


def test_the_episodes_endpoint_is_gated_by_the_access_code(settings, store):
    settings = settings.model_copy(update={"web_access_code": "letmein"})
    client = _client(settings, store)
    assert client.get("/api/episodes").status_code == 401


# --- mark facts -------------------------------------------------------------------------


def _facts_dir(tmp_path, monkeypatch, body: str | None = None):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    if body is not None:
        (tmp_path / "facts.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))


GOOD_FACTS = """
ordinances:
  - id: deposit
    jurisdiction: Chicago
    topic: security deposit return
    rule: Return the deposit within 45 days.
    citation: RLTO 5-12-080(d)
    effective_from: 2010-01-01
  - id: deposit-old
    jurisdiction: Chicago
    topic: security deposit return
    rule: The older window.
    citation: RLTO 5-12-080 (pre-amendment)
    effective_from: 2000-01-01
    effective_to: 2009-12-31
costs:
  - id: boiler
    item: Boiler replacement
    low: 9000
    high: 16000
"""


def test_facts_validate_without_a_file_says_what_to_copy(tmp_path, monkeypatch):
    _facts_dir(tmp_path, monkeypatch)
    result = runner.invoke(app, ["facts", "validate"])
    assert result.exit_code == 0
    assert "facts.example.yaml" in result.stdout


def test_facts_validate_counts_what_applies_today(tmp_path, monkeypatch):
    _facts_dir(tmp_path, monkeypatch, GOOD_FACTS)
    result = runner.invoke(app, ["facts", "validate"])
    assert result.exit_code == 0
    assert "1 in force today" in result.stdout
    assert "1 superseded" in result.stdout


def test_facts_validate_refuses_a_rule_with_no_citation(tmp_path, monkeypatch):
    _facts_dir(
        tmp_path,
        monkeypatch,
        "ordinances:\n  - id: x\n    jurisdiction: Chicago\n    topic: t\n"
        "    rule: r\n    citation: ''\n",
    )
    result = runner.invoke(app, ["facts", "validate"])
    assert result.exit_code == 1
    assert "citation" in result.stdout + str(result.stderr)


def test_facts_list_marks_the_superseded_rule(tmp_path, monkeypatch):
    _facts_dir(tmp_path, monkeypatch, GOOD_FACTS)
    result = runner.invoke(app, ["facts", "list"])
    assert result.exit_code == 0
    assert "in force" in result.stdout and "superseded" in result.stdout
    assert "$9,000 to $16,000" in result.stdout


def test_facts_probe_shows_what_a_question_would_pull_in(tmp_path, monkeypatch):
    _facts_dir(tmp_path, monkeypatch, GOOD_FACTS)
    result = runner.invoke(app, ["facts", "probe", "how long to return a deposit"])
    assert result.exit_code == 0
    assert "RLTO 5-12-080(d)" in result.stdout
    assert "pre-amendment" not in result.stdout, "a superseded rule is never offered"


def test_facts_probe_says_when_nothing_matches(tmp_path, monkeypatch):
    _facts_dir(tmp_path, monkeypatch, GOOD_FACTS)
    result = runner.invoke(app, ["facts", "probe", "what about parking permits"])
    assert result.exit_code == 0
    assert "keywords" in result.stdout


# --- properties and the handoff ---------------------------------------------------------


def test_a_property_is_saved_and_reaches_the_advisor(settings, store):
    advisor = FakeAdvisor()
    client = _client(settings, store, advisor)
    headers = {"X-Browser-Id": "b1"}

    saved = client.post(
        "/api/properties",
        json={"label": "2145 W Division", "units": "6", "city": "Chicago", "notes": "boiler"},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["property"]["units"] == 6
    assert [
        p["label"] for p in client.get("/api/properties", headers=headers).json()["properties"]
    ] == ["2145 W Division"]

    _ask(client, "t1", "Is the boiler worth fixing?")
    assert [p.label for p in advisor.portfolio] == ["2145 W Division"]


def test_a_property_with_no_name_is_refused(settings, store):
    client = _client(settings, store)
    bad = client.post("/api/properties", json={"units": "6"}, headers={"X-Browser-Id": "b1"})
    assert bad.status_code == 400
    assert "address" in bad.json()["detail"]


def test_another_browser_sees_no_properties(settings, store):
    client = _client(settings, store)
    client.post("/api/properties", json={"label": "Mine"}, headers={"X-Browser-Id": "b1"})
    assert client.get("/api/properties", headers={"X-Browser-Id": "b2"}).json() == {
        "properties": []
    }


def test_a_property_can_be_deleted(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    saved = client.post("/api/properties", json={"label": "Mine"}, headers=headers).json()
    gone = client.delete(f"/api/properties/{saved['property']['id']}", headers=headers)
    assert gone.json() == {"deleted": True}
    assert client.get("/api/properties", headers=headers).json() == {"properties": []}


def test_the_handoff_uses_the_conversation_and_the_owners_contact(tmp_path, settings, store):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(
        "websites: []\nbusiness:\n  name: GC Realty\n  escalation_name: Russell\n"
        "  escalation_url: https://calendly.com/example/20min\n",
        encoding="utf-8",
    )
    settings = settings.model_copy(update={"sources_file": manifest})
    client = _client(settings, store, FakeAdvisor("Start with a five day notice."))
    headers = {"X-Browser-Id": "b1"}

    client.post("/api/properties", json={"label": "2145 W Division"}, headers=headers)
    _ask(client, "t1", "My tenant stopped paying.")

    notes = client.post("/api/handoff", json={"session_id": "t1"}, headers=headers).json()
    assert notes["name"] == "Russell"
    assert notes["url"] == "https://calendly.com/example/20min"
    assert "2145 W Division" in notes["text"]
    assert "My tenant stopped paying." in notes["text"]
    assert "five day notice" in notes["text"]


def test_the_handoff_still_works_with_no_manifest(settings, store, tmp_path):
    settings = settings.model_copy(update={"sources_file": tmp_path / "missing.yaml"})
    client = _client(settings, store)
    notes = client.post(
        "/api/handoff", json={"session_id": "t1"}, headers={"X-Browser-Id": "b1"}
    ).json()
    assert notes["url"] is None
    assert "property manager" in notes["text"]


def test_properties_and_the_handoff_are_gated_by_the_access_code(settings, store):
    settings = settings.model_copy(update={"web_access_code": "letmein"})
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    assert client.get("/api/properties", headers=headers).status_code == 401
    assert client.post("/api/properties", json={"label": "x"}, headers=headers).status_code == 401
    assert client.delete("/api/properties/x", headers=headers).status_code == 401
    assert client.post("/api/handoff", json={"session_id": "t"}, headers=headers).status_code == 401


def test_the_page_has_a_properties_panel_and_a_handoff():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="props"' in page and 'id="prop-form"' in page
    assert "/api/properties" in page and "/api/handoff" in page
    assert 'id="handoff-text"' in page
    assert ".innerHTML" not in page, "an address is landlord text; it goes in as a text node"


# --- the free account -------------------------------------------------------------------


SIGNUP = {
    "name": "Javier Diaz",
    "email": "javier@example.com",
    "phone": "312-555-0134",
    "neighborhood": "Logan Square",
}


def test_two_questions_are_answered_then_the_wall(settings, store):
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}

    assert client.get("/api/account", headers=headers).json()["free_left"] == 2
    assert _ask(client, "t1", "First question").status_code == 200
    assert client.get("/api/account", headers=headers).json()["free_left"] == 1
    assert _ask(client, "t2", "Second question").status_code == 200

    walled = client.post(
        "/api/chat", json={"session_id": "t3", "message": "Third"}, headers=headers
    )
    assert walled.status_code == 403
    assert walled.json()["detail"]["signup_required"] is True


def test_signing_up_lets_the_third_question_through(settings, store):
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}
    _ask(client, "t1", "First question")
    _ask(client, "t2", "Second question")

    made = client.post("/api/account", json=SIGNUP, headers=headers)
    assert made.json()["signed_in"] is True
    assert made.json()["email"] == "javier@example.com"
    assert "mark_auth" in made.cookies, "signing up signs you in"

    state = client.get("/api/account", headers=headers).json()
    assert state["signed_in"] is True and state["name"] == "Javier Diaz"
    assert _ask(client, "t3", "Third question").status_code == 200


def test_a_second_device_fills_the_form_again_and_starts_clean(settings, store):
    """No password, so nothing can prove who anyone is, and nothing is handed over."""
    app_under_test = create_app(settings, advisor=FakeAdvisor(), store=store)
    phone = TestClient(app_under_test)
    phone.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    _ask(phone, "t1", "Something about my building")

    laptop = TestClient(app_under_test)  # its own cookie jar, like a different machine
    for index in range(2):
        _ask(laptop, f"o{index}", "A free question", browser="b2")
    again = laptop.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b2"})
    assert again.status_code == 200, "the same email fills the form again, and that is fine"

    titles = [t["title"] for t in laptop.get("/api/threads").json()["threads"]]
    assert "Something about my building" not in titles
    assert titles == ["A free question", "A free question"]
    assert _ask(laptop, "o9", "And a third").status_code == 200, "no wall on this device now"


def test_the_session_cookie_cannot_be_read_by_a_script(settings, store):
    client = _client(settings, store)
    made = client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    header = made.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header, "so no other site can post with it"


def test_the_neighborhood_reaches_the_advisor(settings, store):
    advisor = FakeAdvisor()
    client = _client(settings, store, advisor)
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/account", json=SIGNUP, headers=headers)
    _ask(client, "t1", "Do I need a heat certificate?")
    assert advisor.neighborhood == "Logan Square"


def test_a_bad_signup_says_which_field(settings, store):
    client = _client(settings, store)
    bad = client.post(
        "/api/account",
        json={"name": "J", "email": "nope", "phone": "1", "neighborhood": ""},
        headers={"X-Browser-Id": "b1"},
    )
    assert bad.status_code == 400
    assert "name" in bad.json()["detail"].lower()


def test_a_failed_answer_does_not_spend_a_free_question(settings, store):
    advisor = FakeAdvisor()
    advisor.error = "Overloaded"
    client = _client(settings, store, advisor)
    headers = {"X-Browser-Id": "b1"}
    _ask(client, "t1", "First question")
    assert client.get("/api/account", headers=headers).json()["free_left"] == 2


def test_another_browser_still_has_its_own_free_questions(settings, store):
    client = _client(settings, store, FakeAdvisor())
    _ask(client, "t1", "One", browser="b1")
    _ask(client, "t2", "Two", browser="b1")
    assert client.get("/api/account", headers={"X-Browser-Id": "b2"}).json()["free_left"] == 2
    assert _ask(client, "t3", "One", browser="b2").status_code == 200


def test_deleting_the_conversations_does_not_hand_back_a_free_question(settings, store):
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}
    _ask(client, "t1", "One")
    _ask(client, "t2", "Two")
    client.delete("/api/threads/t1", headers=headers)
    client.delete("/api/threads/t2", headers=headers)
    assert client.get("/api/account", headers=headers).json()["free_left"] == 0


def test_the_gate_can_be_turned_off(settings, store):
    settings = settings.model_copy(update={"account_required": False})
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}
    assert client.get("/api/account", headers=headers).json()["required"] is False
    for index in range(4):
        assert _ask(client, f"t{index}", "Another question").status_code == 200


def test_the_terminal_is_never_gated(settings, store):
    """No browser id, no wall: `mark ask` and `mark chat` are the owner's own machine."""
    client = _client(settings, store, FakeAdvisor())
    for index in range(4):
        assert _ask(client, f"t{index}", "Another question", browser=None).status_code == 200


def test_the_account_routes_are_gated_by_the_access_code(settings, store):
    settings = settings.model_copy(update={"web_access_code": "letmein"})
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    assert client.get("/api/account", headers=headers).status_code == 401
    assert client.post("/api/account", json={}, headers=headers).status_code == 401


def test_the_page_has_the_signup_form_with_the_four_fields():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    for field in ("su-name", "su-email", "su-phone", "su-hood"):
        assert f'id="{field}"' in page
    assert "su-pass" not in page and "/api/login" not in page, "no password anywhere"
    assert "signup_required" in page, "the page reacts to the server's wall, not its own count"
    assert "heldQuestion" in page, "the question they were typing is asked after signup"


def test_mark_accounts_lists_and_exports_the_signups(tmp_path, monkeypatch):
    from markai.web.accounts import Accounts

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    store = Accounts(data_dir / "accounts.db", free_questions=2)
    store.create(
        {
            "name": "Javier Diaz",
            "email": "javier@example.com",
            "phone": "312-555-0134",
            "neighborhood": "Logan Square",
            "password": "six flats and a boiler",
        }
    )
    store.close()

    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data_dir))

    listed = runner.invoke(app, ["accounts", "list"])
    assert listed.exit_code == 0
    assert "javier@example.com" in listed.stdout
    assert "Logan Square" in listed.stdout

    out = tmp_path / "leads.csv"
    exported = runner.invoke(app, ["accounts", "list", "--csv", str(out)])
    assert exported.exit_code == 0
    body = out.read_text(encoding="utf-8")
    assert "signed_up_utc,name,email,phone,neighborhood" in body
    assert "javier@example.com" in body


def test_mark_accounts_says_so_when_nobody_signed_up(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))
    result = runner.invoke(app, ["accounts", "list"])
    assert result.exit_code == 1
    assert "No accounts yet" in result.stdout + str(result.stderr)


# --- the lead reaching the CRM ----------------------------------------------------------


def test_a_signup_queues_a_lead_with_what_they_asked_about(settings, store):
    from markai.web.crm import Crm

    sent = []
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}
    _ask(client, "t1", "How long do I have to return a deposit?")

    # Swap the queue's sender before signing up, so nothing leaves the process.
    queue = Crm(settings.data_dir / "leads.db", url="https://crm.test/hook", sender=sent.append)
    client.post("/api/account", json=SIGNUP, headers=headers)
    queue.deliver_pending()
    queue.close()

    assert len(sent) == 1
    lead = sent[0]
    assert lead["email"] == "javier@example.com"
    assert lead["neighborhood"] == "Logan Square"
    assert lead["asked_about"] == "How long do I have to return a deposit"


def test_mark_leads_lists_and_sends(tmp_path, monkeypatch):
    from markai.web.accounts import Account
    from markai.web.crm import Crm, build_payload

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    queue = Crm(data_dir / "leads.db")
    queue.enqueue(
        "a1",
        build_payload(
            Account(
                id="a1",
                email="javier@example.com",
                name="Javier Diaz",
                phone="312-555-0134",
                neighborhood="Logan Square",
            )
        ),
    )
    queue.close()

    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data_dir))

    listed = runner.invoke(app, ["leads", "list"])
    assert listed.exit_code == 0
    assert "javier@example.com" in listed.stdout
    assert "Nowhere to send leads yet" in listed.stdout
    assert "1 waiting" in listed.stdout

    blocked = runner.invoke(app, ["leads", "send"])
    assert blocked.exit_code == 1
    assert "MARKAI_LEAD_EMAIL_TO" in blocked.stdout + str(blocked.stderr)


def test_doctor_reports_on_the_page_the_accounts_and_the_crm(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))

    bare = runner.invoke(app, ["doctor"])
    assert bare.exit_code == 0
    assert "Web access code" in bare.stdout and "not set" in bare.stdout
    assert "2 free question" in bare.stdout and "No password" in bare.stdout
    assert "MARKAI_LEAD_EMAIL_TO" in bare.stdout, "it says how to wire the leads up"
    assert "fine on 127.0.0.1" in bare.stdout

    monkeypatch.setenv("MARKAI_WEB_ACCESS_CODE", "letmein")
    monkeypatch.setenv("MARKAI_LEAD_EMAIL_TO", "new-deal@newlead.leadsimple.com")
    monkeypatch.setenv("MARKAI_SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("MARKAI_WEB_HOST", "0.0.0.0")
    wired = runner.invoke(app, ["doctor"])
    assert wired.exit_code == 0
    assert "email to new-deal@newlead.leadsimple.com" in wired.stdout
    assert "plain http on a public host" in wired.stdout, "the cookie warning has to be loud"

    # Half-configured is the dangerous one: it looks set up and sends nothing.
    monkeypatch.delenv("MARKAI_SMTP_HOST")
    half = runner.invoke(app, ["doctor"])
    assert "no SMTP host" in half.stdout


def test_doctor_never_prints_a_secret(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MARKAI_CRM_WEBHOOK_TOKEN", "crm-secret-value")
    monkeypatch.setenv("MARKAI_WEB_ACCESS_CODE", "access-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")

    result = runner.invoke(app, ["doctor"])
    for secret in ("crm-secret-value", "access-secret-value", "sk-ant-secret-value"):
        assert secret not in result.stdout


def test_old_databases_load_and_keep_what_they_hold(tmp_path):
    """The owner's machine has databases from before accounts existed.

    They are migrated in place on first open. If this ever breaks, `mark serve` dies on
    the first request and a landlord's saved conversations go with it.
    """
    import json
    import sqlite3
    import time

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)

    old = sqlite3.connect(data_dir / "conversations.db")
    old.executescript(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, browser_id TEXT NOT NULL,"
        " title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " turns INTEGER NOT NULL DEFAULT 0, messages TEXT NOT NULL DEFAULT '[]');"
        "CREATE INDEX threads_by_browser ON threads(browser_id, updated_at DESC);"
    )
    old.execute(
        "INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
        (
            "t-old",
            "b-javid",
            "Cuanto tiempo para el deposito",
            time.time(),
            time.time(),
            2,
            json.dumps([{"role": "user", "content": "Cuanto?"}]),
        ),
    )
    old.commit()
    old.close()

    old = sqlite3.connect(data_dir / "portfolio.db")
    old.executescript(
        "CREATE TABLE properties (id TEXT PRIMARY KEY, browser_id TEXT NOT NULL,"
        " label TEXT NOT NULL, units INTEGER, city TEXT NOT NULL DEFAULT '',"
        " notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);"
    )
    old.execute(
        "INSERT INTO properties VALUES ('p1','b-javid','2145 W Division',6,'Chicago','',?)",
        (time.time(),),
    )
    old.commit()
    old.close()

    old = sqlite3.connect(data_dir / "accounts.db")
    old.executescript(
        "CREATE TABLE accounts (browser_id TEXT PRIMARY KEY, name TEXT, email TEXT,"
        " phone TEXT, neighborhood TEXT, created_at REAL);"
        "CREATE TABLE usage (browser_id TEXT PRIMARY KEY, questions INTEGER);"
    )
    old.execute(
        "INSERT INTO accounts VALUES ('b-javid','Javier','javier@example.com','312','Logan',1.0)"
    )
    old.execute("INSERT INTO usage VALUES ('b-javid', 2)")
    old.commit()
    old.close()

    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    settings = Settings(_env_file=None, data_dir=data_dir, sources_file=manifest)
    client = TestClient(create_app(settings=settings, advisor=FakeAdvisor()))
    headers = {"X-Browser-Id": "b-javid"}

    threads = client.get("/api/threads", headers=headers).json()["threads"]
    assert [t["title"] for t in threads] == ["Cuanto tiempo para el deposito"]
    properties = client.get("/api/properties", headers=headers).json()["properties"]
    assert [p["label"] for p in properties] == ["2145 W Division"]

    # The two questions already spent still count, so the wall stays where it was.
    assert client.get("/api/account", headers=headers).json()["free_left"] == 0
    walled = client.post("/api/chat", json={"session_id": "x", "message": "third"}, headers=headers)
    assert walled.status_code == 403

    # And the old email is free to become a real account, which claims the old rows.
    made = client.post("/api/account", json={**SIGNUP, "password": ""}, headers=headers)
    assert made.status_code == 200
    assert [t["title"] for t in client.get("/api/threads", headers=headers).json()["threads"]] == [
        "Cuanto tiempo para el deposito"
    ]


# --- wiring the mailbox up ----------------------------------------------------------------


def test_leads_setup_checks_the_login_then_writes_env(tmp_path, monkeypatch):
    """The password is typed into the terminal and goes straight to .env, nowhere else."""
    import smtplib

    from markai import cli

    env_path = tmp_path / ".env"
    env_path.write_text(
        "ANTHROPIC_API_KEY=sk-ant-keep-me\n# MARKAI_SMTP_HOST=\nMARKAI_EFFORT=medium\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr("markai.config.PROJECT_ROOT", tmp_path)

    signed_in = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            signed_in["host"], signed_in["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            signed_in["starttls"] = True

        def login(self, username, password):
            signed_in["as"] = username

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    result = runner.invoke(
        app,
        ["leads", "setup"],
        input="new-deal@newlead.leadsimple.com\nsmtp.gmail.com\n587\n"
        "javier@gcrealtyinc.com\nan-app-password\njay@gcrealtyinc.com\n",
    )
    assert result.exit_code == 0, result.stdout
    assert signed_in["as"] == "javier@gcrealtyinc.com", "it signs in before saving anything"

    body = env_path.read_text(encoding="utf-8")
    assert "MARKAI_LEAD_EMAIL_TO=new-deal@newlead.leadsimple.com" in body
    assert "MARKAI_SMTP_HOST=smtp.gmail.com" in body, "the commented-out line was revived"
    assert "MARKAI_SMTP_PASSWORD=an-app-password" in body
    assert "MARKAI_SMTP_FROM=jay@gcrealtyinc.com" in body
    assert "ANTHROPIC_API_KEY=sk-ant-keep-me" in body, "nothing else in the file was touched"
    assert "MARKAI_EFFORT=medium" in body
    assert "an-app-password" not in result.stdout, "the password is never echoed back"


def test_leads_setup_refuses_a_bad_password_and_writes_nothing(tmp_path, monkeypatch):
    import smtplib

    env_path = tmp_path / ".env"
    env_path.write_text("ANTHROPIC_API_KEY=sk-ant-keep-me\n", encoding="utf-8")
    monkeypatch.setattr("markai.config.PROJECT_ROOT", tmp_path)

    class Refusing:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, *a):
            raise smtplib.SMTPAuthenticationError(535, b"denied")

    monkeypatch.setattr(smtplib, "SMTP", Refusing)

    result = runner.invoke(
        app,
        ["leads", "setup"],
        input="new-deal@newlead.test\nsmtp.gmail.com\n587\nme@test\nwrong\nme@test\n",
    )
    assert result.exit_code == 1
    assert "app password" in result.stdout + str(result.stderr), "it says what to fix"
    assert env_path.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-ant-keep-me\n"


# --- what a landlord thought of the answer ------------------------------------------------


def test_a_thumbs_down_is_recorded_against_the_question(settings, store):
    client = _client(settings, store, FakeAdvisor("45 days."))
    headers = {"X-Browser-Id": "b1"}
    _ask(client, "t1", "How long do I have to return a deposit?")

    saved = client.post(
        "/api/feedback",
        json={"session_id": "t1", "rating": "down", "note": "It never mentions the interest"},
        headers=headers,
    )
    assert saved.json() == {"saved": True}

    from markai.web.history import History

    store_ = History(settings.data_dir / "conversations.db")
    rows = store_.ratings()
    store_.close()
    assert rows[0]["rating"] == "down"
    assert rows[0]["question"] == "How long do I have to return a deposit?"
    assert rows[0]["note"] == "It never mentions the interest"


def test_a_rating_on_a_thread_that_is_not_theirs_saves_nothing(settings, store):
    client = _client(settings, store, FakeAdvisor())
    _ask(client, "t1", "Mine", browser="b1")
    other = client.post(
        "/api/feedback",
        json={"session_id": "t1", "rating": "down"},
        headers={"X-Browser-Id": "b2"},
    )
    assert other.json() == {"saved": False}


def test_a_rating_that_is_not_a_thumb_is_refused(settings, store):
    client = _client(settings, store, FakeAdvisor())
    _ask(client, "t1", "A question")
    assert client.post(
        "/api/feedback",
        json={"session_id": "t1", "rating": "sideways"},
        headers={"X-Browser-Id": "b1"},
    ).json() == {"saved": False}


def test_mark_feedback_lists_what_they_said(tmp_path, monkeypatch):
    from markai.web.history import History

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    history = History(data_dir / "conversations.db")
    history.record("account:a1", "t1", "How long for a deposit?", "45 days.")
    history.rate("account:a1", "t1", "down", "It never mentions the interest")
    history.close()

    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data_dir))

    result = runner.invoke(app, ["feedback"])
    assert result.exit_code == 0
    assert "0 up · 1 down" in result.stdout
    assert "How long for a deposit" in result.stdout
    assert "mentions the interest" in result.stdout


def test_the_page_shows_the_reasoning_the_stop_and_the_thumbs():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert '"thinking"' in page, "the reasoning is streamed while Jay works"
    assert "foldReasoning" in page, "and folds away once the answer starts"
    assert 'id="stop"' in page and "AbortController" in page
    assert "/api/feedback" in page
    assert "Hand this to a property manager" in page
    assert ".innerHTML" not in page


def test_no_reading_list_is_handed_back_under_an_answer():
    """The podcast is what Jay learned from, not a list of links to check his homework."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert "Mark on this" not in page
    assert 'class="related"' not in page


def test_the_corpus_is_loaded_before_anyone_asks(settings, store, caplog):
    """The first question is the worst moment to pay for building the BM25 index."""
    import time

    settings = settings.model_copy(update={"anthropic_api_key": "sk-ant-test"})
    manifest = settings.data_dir / "sources.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("websites: []\n", encoding="utf-8")
    settings = settings.model_copy(update={"sources_file": manifest})

    with caplog.at_level("INFO"):
        with TestClient(create_app(settings, store=store)) as client:
            assert client.get("/api/health").json() == {"status": "ok"}
            for _ in range(50):
                if "warm:" in caplog.text:
                    break
                time.sleep(0.05)
    assert "warm:" in caplog.text, "the warm-up ran on startup, not on the first question"


def test_a_returning_landlord_is_remembered_across_conversations(settings, store):
    """The point of an account: Jay knows they asked about the deposit last week."""
    advisor = FakeAdvisor()
    # The wall is not what this is about, and three questions would hit it.
    client = _client(settings.model_copy(update={"account_required": False}), store, advisor)

    _ask(client, "t1", "How long do I have to return a deposit?")
    _ask(client, "t2", "The tenant left the unit trashed")

    assert [title for title, _ in advisor.remembered] == ["How long do I have to return a deposit"]
    _ask(client, "t3", "And the carpet?")
    assert [title for title, _ in advisor.remembered] == [
        "The tenant left the unit trashed",
        "How long do I have to return a deposit",
    ]


def test_the_conversation_they_are_in_is_not_handed_back_as_memory(settings, store):
    advisor = FakeAdvisor()
    client = _client(settings, store, advisor)
    _ask(client, "t1", "First question")
    _ask(client, "t1", "A follow-up in the same thread")
    assert advisor.remembered == [], "that is history, not memory"


def test_one_landlord_never_gets_anothers_memory(settings, store):
    advisor = FakeAdvisor()
    client = _client(settings, store, advisor)
    _ask(client, "t1", "Something about my building", browser="b1")
    _ask(client, "t2", "A different question", browser="b2")
    assert advisor.remembered == []


# --- reviewing a big pile of mined proposals ---------------------------------------------


def _waiting(tmp_path, monkeypatch, proposals):
    """A manifest, a data dir, and a proposals file, as `mark facts mine` would leave it."""
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(
        "business:\n  name: GC Realty\nsources: []\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "facts-proposals.json").write_text(
        json.dumps({"read_chunk_ids": [], "proposals": proposals}), encoding="utf-8"
    )
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return manifest, data


def _proposal(topic, rule, quote, source, **extra):
    return {
        "kind": "ordinance",
        "topic": topic,
        "rule": rule,
        "quote": quote,
        "source": source,
        "url": None,
        "verified_quote": True,
        "jurisdiction": "Chicago",
        "citation": "",
        **extra,
    }


def test_facts_proposals_collapses_the_repeats(tmp_path, monkeypatch):
    quote = "The landlord must return the deposit within 45 days of the tenant vacating."
    _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal("security deposit", "Return it in 45 days.", quote, f"Post {n}")
            for n in range(4)
        ]
        + [
            _proposal(
                "heat ordinance",
                "68 degrees by day.",
                "Heat must reach 68 degrees during the day.",
                "A post",
            )
        ],
    )
    result = runner.invoke(app, ["facts", "proposals"])
    assert result.exit_code == 0
    assert "5 proposals" in result.stdout
    assert "2 distinct rules" in result.stdout, "four posts quoting one sentence is one decision"


def test_facts_review_accepts_a_filtered_set_in_one_go(tmp_path, monkeypatch):
    quote = "The landlord must return the deposit within 45 days of the tenant vacating."
    manifest, data = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal("security deposit", "Return it in 45 days.", quote, f"Post {n}")
            for n in range(3)
        ]
        + [
            _proposal(
                "snow removal",
                "Clear it in 24 hours.",
                "Snow must be cleared within 24 hours of a storm.",
                "A post",
            )
        ],
    )
    result = runner.invoke(
        app, ["facts", "review", "--min-sources", "3", "--accept-all"], input="y\n"
    )
    assert result.exit_code == 0, result.stdout

    body = (manifest.parent / "facts.yaml").read_text(encoding="utf-8")
    assert "security deposit" in body
    assert "snow" not in body, "the filter is the point of --accept-all"
    assert "stated in 3 of your sources" in body

    left = json.loads((data / "facts-proposals.json").read_text(encoding="utf-8"))["proposals"]
    assert len(left) == 1, "the three that quoted one sentence are all answered"
    assert left[0]["topic"] == "snow removal"


def test_facts_review_can_skip_a_whole_topic(tmp_path, monkeypatch):
    manifest, data = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "deposit interest", "Pay it.", "Interest is due on deposits over 6 months.", "A"
            ),
            _proposal("deposit return", "45 days.", "Return the deposit within 45 days.", "B"),
        ],
    )
    result = runner.invoke(app, ["facts", "review"], input="t\n")
    assert result.exit_code == 0, result.stdout
    assert not (manifest.parent / "facts.yaml").exists(), "nothing accepted, nothing written"
    left = json.loads((data / "facts-proposals.json").read_text(encoding="utf-8"))["proposals"]
    assert len(left) == 2, "skipping a topic leaves it for later, it does not throw it away"


def test_facts_review_refuses_a_price_with_no_number(tmp_path, monkeypatch):
    _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "boiler replacement",
                "Boilers are expensive.",
                "A boiler replacement is one of the most expensive jobs on a 6 flat.",
                "A post",
                kind="cost",
                low=None,
                high=None,
                unit="per job",
            )
        ],
    )
    result = runner.invoke(app, ["facts", "review"], input="a\n")
    assert result.exit_code == 0
    assert "Nothing matches" in result.stdout, (
        "a $0 to $0 price is a wrong answer, not a missing one"
    )


# --- the property log ---------------------------------------------------------------------


def test_the_log_is_saved_and_reaches_the_advisor(settings, store):
    advisor = FakeAdvisor()
    client = _client(settings, store, advisor)
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/properties", json={"label": "2145 W Division"}, headers=headers)

    saved = client.post(
        "/api/log",
        json={"kind": "expense", "what": "New boiler", "amount": "$8,000", "vendor": "ABC Heating"},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["entry"]["amount"] == 8000

    read = client.get("/api/log", headers=headers).json()
    assert [e["what"] for e in read["entries"]] == ["New boiler"]
    assert read["totals"]["out"] == 8000

    _ask(client, "t1", "Is the boiler under warranty?")
    assert advisor.log is not None, "Jay can read it back and add to it while answering"
    assert [e.what for e in advisor.log.recent()] == ["New boiler"]


def test_a_log_entry_with_nothing_in_it_is_refused(settings, store):
    client = _client(settings, store)
    bad = client.post("/api/log", json={"kind": "expense"}, headers={"X-Browser-Id": "b1"})
    assert bad.status_code == 400
    assert "what happened" in bad.json()["detail"]


def test_another_browser_sees_no_log(settings, store):
    client = _client(settings, store)
    client.post("/api/log", json={"kind": "note", "what": "Mine"}, headers={"X-Browser-Id": "b1"})
    other = client.get("/api/log", headers={"X-Browser-Id": "b2"}).json()
    assert other["entries"] == [] and other["count"] == 0


def test_an_open_item_can_be_closed_and_deleted(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log", json={"kind": "maintenance", "what": "No heat in unit 2"}, headers=headers
    ).json()["entry"]
    assert entry["status"] == "open"

    assert client.post(f"/api/log/{entry['id']}/done", headers=headers).json() == {"closed": True}
    assert client.get("/api/log", headers=headers).json()["open"] == []

    assert client.delete(f"/api/log/{entry['id']}", headers=headers).json() == {"deleted": True}
    assert client.get("/api/log", headers=headers).json()["entries"] == []


def test_the_handoff_carries_what_is_still_open(settings, store):
    client = _client(settings, store, FakeAdvisor("Call a plumber."))
    headers = {"X-Browser-Id": "b1"}
    client.post(
        "/api/log",
        json={"kind": "maintenance", "what": "Kitchen stack backing up", "date": "2026-08-01"},
        headers=headers,
    )
    _ask(client, "t1", "The stack is backing up again.")

    notes = client.post("/api/handoff", json={"session_id": "t1"}, headers=headers).json()["text"]
    assert "Still open:" in notes
    assert "Kitchen stack backing up" in notes


def test_facts_mine_free_spends_nothing_and_needs_no_key(tmp_path, monkeypatch):
    """The free path has to work on a machine with no API key at all."""
    from markai.knowledge.chunking import chunk_document
    from markai.knowledge.store import KnowledgeStore
    from markai.models import Document, SourceKind

    manifest = tmp_path / "sources.yaml"
    manifest.write_text("business:\n  name: GC Realty\nsources: []\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    rule = "Heat must reach 68 degrees during the day under the Chicago ordinance."
    doc = Document(
        id="d1",
        kind=SourceKind.WEBSITE,
        title="A post about heat",
        locator="https://example.com/heat",
        text=(rule + " ") * 4,
    )
    doc.ensure_hash()
    store = KnowledgeStore(data / "markai.db")
    store.upsert_document(doc, chunk_document(doc, target_words=60, overlap_words=10))
    store.close()

    result = runner.invoke(app, ["facts", "mine", "--free"])
    assert result.exit_code == 0, result.stdout
    assert "Cost: nothing" in result.stdout

    waiting = json.loads((data / "facts-proposals.json").read_text(encoding="utf-8"))["proposals"]
    assert waiting, "it found the sentence"
    assert waiting[0]["quote"] == rule, "proposed verbatim, so it cannot misquote the source"
    assert waiting[0]["jurisdiction"] == "Chicago"

    # And a second run has nothing left to do rather than proposing it all again.
    again = runner.invoke(app, ["facts", "mine", "--free"])
    assert "Nothing left to read" in again.stdout


def test_facts_review_dates_a_price_from_the_store(tmp_path, monkeypatch):
    """Proposals mined before the date was carried still get one: the store knows."""
    from markai.knowledge.chunking import chunk_document
    from markai.knowledge.store import KnowledgeStore
    from markai.models import Document, SourceKind

    manifest, data = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "Boiler replacement",
                "$8,000 to $16,000.",
                "A boiler replacement must run $8,000 to $16,000 on a six flat.",
                "A post about boilers",
                kind="cost",
                low=8000,
                high=16000,
                unit="per job",
            )
        ],
    )
    doc = Document(
        id="d1",
        kind=SourceKind.WEBSITE,
        title="A post about boilers",
        locator="https://example.com/boilers",
        text="A boiler replacement must run $8,000 to $16,000 on a six flat.",
        published_at="2024-11-01",
    )
    doc.ensure_hash()
    store = KnowledgeStore(data / "markai.db")
    store.upsert_document(doc, chunk_document(doc, target_words=60, overlap_words=10))
    store.close()

    result = runner.invoke(app, ["facts", "review", "--accept-all"], input="y\n")
    assert result.exit_code == 0, result.stdout
    body = (manifest.parent / "facts.yaml").read_text(encoding="utf-8")
    assert 'as_of: "2024-11-01"' in body, "a price with no date is a number, not a fact"


def test_facts_proposals_can_leave_the_market_rents_out(tmp_path, monkeypatch):
    _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "Average apartment rent",
                "$2,100.",
                "The average apartment rent must be around $2,100 a month.",
                "Post A",
                kind="cost",
                low=2100,
                high=2100,
            ),
            _proposal(
                "Boiler replacement",
                "$8,000.",
                "A boiler replacement must run about $8,000 on a six flat.",
                "Post B",
                kind="cost",
                low=8000,
                high=8000,
            ),
        ],
    )
    both = runner.invoke(app, ["facts", "proposals"])
    assert "market rent" in both.stdout

    without = runner.invoke(app, ["facts", "proposals", "--no-rents"])
    assert "Average apartment rent" not in without.stdout
    assert "Boiler replacement" in without.stdout


def test_facts_drop_clears_a_slice_and_never_touches_the_file(tmp_path, monkeypatch):
    manifest, data = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "Average apartment rent",
                "$2,100.",
                "The average apartment rent must be around $2,100 a month.",
                "Post A",
                kind="cost",
                low=2100,
                high=2100,
            ),
            _proposal(
                "security deposit interest",
                "Interest each year.",
                "Under RLTO 5-12-080 the landlord must pay interest every 12 months.",
                "Post B",
                citation="RLTO 5-12-080",
            ),
        ],
    )
    bare = runner.invoke(app, ["facts", "drop", "--yes"])
    assert bare.exit_code != 0, "dropping everything has to be asked for by name"

    result = runner.invoke(app, ["facts", "drop", "--rents", "--yes"])
    assert result.exit_code == 0, result.stdout
    left = json.loads((data / "facts-proposals.json").read_text(encoding="utf-8"))["proposals"]
    assert [raw["topic"] for raw in left] == ["security deposit interest"]
    assert not (manifest.parent / "facts.yaml").exists(), "dropping writes nothing"


def test_facts_proposals_puts_what_landlords_asked_about_first(tmp_path, monkeypatch):
    from markai.knowledge.chunking import chunk_document
    from markai.knowledge.store import KnowledgeStore
    from markai.models import Document, SourceKind

    _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "snow removal",
                "24 hours.",
                "Snow must be cleared within 24 hours of a storm.",
                "Post A",
            ),
            _proposal(
                "security deposit interest",
                "Interest is due every year.",
                "The landlord must pay interest on the deposit every 12 months.",
                "Post B",
            ),
        ],
    )
    doc = Document(
        id="d1",
        kind=SourceKind.WEBSITE,
        title="A post",
        locator="https://example.com/1",
        text="Something about deposits and interest that must be paid within 12 months.",
    )
    doc.ensure_hash()
    store = KnowledgeStore(tmp_path / "data" / "markai.db")
    store.upsert_document(doc, chunk_document(doc, target_words=60, overlap_words=10))
    store.log_question(
        "s1", "how much interest do I owe on a security deposit", "weak", [], True, {}
    )
    store.close()

    result = runner.invoke(app, ["facts", "proposals"])
    assert result.exit_code == 0, result.stdout
    assert "asked about" in result.stdout
    rows = [line for line in result.stdout.splitlines() if "│" in line and "ordinance" in line]
    assert "deposit" in rows[0], "the one somebody asked for goes first"


def test_facts_drop_clears_what_is_not_about_renting(tmp_path, monkeypatch):
    manifest, data = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "Restricted cannabis zone petition deadline",
                "30 days.",
                "A petition must be filed within 30 days of the notice.",
                "City page",
            ),
            _proposal(
                "Security deposit return deadline",
                "45 days.",
                "The landlord must return the deposit within 45 days.",
                "A post",
            ),
        ],
    )
    listing = runner.invoke(app, ["facts", "proposals"])
    assert "off topic" in listing.stdout

    only_stray = runner.invoke(app, ["facts", "proposals", "--off-topic"])
    assert "cannabis" in only_stray.stdout
    assert "Security deposit" not in only_stray.stdout, "look before you drop"

    dropped = runner.invoke(app, ["facts", "drop", "--off-topic", "--yes"])
    assert dropped.exit_code == 0, dropped.stdout
    left = json.loads((data / "facts-proposals.json").read_text(encoding="utf-8"))["proposals"]
    assert [raw["topic"] for raw in left] == ["Security deposit return deadline"]
    assert not (manifest.parent / "facts.yaml").exists()


def test_a_dropped_proposal_can_be_put_back(tmp_path, monkeypatch):
    """Deciding what a landlord will never ask is a judgement call, so it has to be undoable."""
    manifest, data = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "Dog registration", "30 days.", "A dog must be registered within 30 days.", "City"
            ),
            _proposal(
                "Security deposit return",
                "45 days.",
                "The landlord must return the deposit within 45 days.",
                "A post",
            ),
        ],
    )
    runner.invoke(app, ["facts", "drop", "--off-topic", "--yes"])
    saved = json.loads((data / "facts-proposals.json").read_text(encoding="utf-8"))
    assert [raw["topic"] for raw in saved["proposals"]] == ["Security deposit return"]
    assert [raw["topic"] for raw in saved["dropped"]] == ["Dog registration"], "kept, not deleted"

    back = runner.invoke(app, ["facts", "undrop"])
    assert back.exit_code == 0, back.stdout
    saved = json.loads((data / "facts-proposals.json").read_text(encoding="utf-8"))
    assert len(saved["proposals"]) == 2 and saved["dropped"] == []
    assert not (manifest.parent / "facts.yaml").exists()


def test_facts_undrop_can_take_back_one_subject(tmp_path, monkeypatch):
    _, data = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal("Dog registration", "30 days.", "A dog must be registered in 30 days.", "C"),
            _proposal("FOIA copy fees", "$0.15.", "A copy must cost no more than $0.15.", "C"),
        ],
    )
    runner.invoke(app, ["facts", "drop", "--off-topic", "--yes"])
    runner.invoke(app, ["facts", "undrop", "--topic", "dog"])
    saved = json.loads((data / "facts-proposals.json").read_text(encoding="utf-8"))
    assert [raw["topic"] for raw in saved["proposals"]] == ["Dog registration"]
    assert [raw["topic"] for raw in saved["dropped"]] == ["FOIA copy fees"]


def test_facts_why_explains_the_verdict(tmp_path, monkeypatch):
    _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "Section 8 change of ownership",
                "10 days.",
                "A change of ownership must be reported to the CHA within 10 days.",
                "A page",
            ),
            _proposal(
                "Dog registration", "30 days.", "A dog must be registered within 30 days.", "City"
            ),
        ],
    )
    kept = runner.invoke(app, ["facts", "why", "section 8"])
    assert kept.exit_code == 0, kept.stdout
    assert "about renting property" in kept.stdout
    assert "Section 8" in kept.stdout, "it names the reason, not just the verdict"

    stray = runner.invoke(app, ["facts", "why", "dog"])
    assert "off topic" in stray.stdout
    assert "nothing about renting property" in stray.stdout


def test_facts_review_refuses_a_rule_that_names_no_city(tmp_path, monkeypatch):
    """The card and the file both have to say where a rule is from."""
    manifest, _ = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "Non-renewal notice",
                "90 days' notice not to renew.",
                "Landlords must give tenants 90 days' notice not to renew a lease.",
                "A post",
                jurisdiction="",
            )
        ],
    )
    refused = runner.invoke(app, ["facts", "review", "--accept-all"], input="y\n")
    assert refused.exit_code == 0, refused.stdout
    assert "which city or county" in refused.stdout
    assert not (manifest.parent / "facts.yaml").exists(), "nothing written on a guess"


def test_facts_review_files_an_evanston_rule_under_evanston(tmp_path, monkeypatch):
    manifest, _ = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "Non-renewal notice",
                "90 days' notice not to renew.",
                "Under the updated RLTO, Evanston property managers must give tenants 90 days'"
                " notice if they intend not to renew a lease.",
                "Evanston RLTO changes landlords must know",
                jurisdiction="",
            )
        ],
    )
    result = runner.invoke(app, ["facts", "review", "--accept-all"], input="y\n")
    assert result.exit_code == 0, result.stdout
    body = (manifest.parent / "facts.yaml").read_text(encoding="utf-8")
    assert 'jurisdiction: "Evanston"' in body
    assert "Chicago" not in body


def test_mark_report_says_everything_without_saying_anything_private(tmp_path, monkeypatch):
    """One file to send instead of a screenshot - and never a key, an email or a question."""
    manifest, data = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "Security deposit return",
                "45 days.",
                "The landlord must return the deposit within 45 days.",
                "A post",
            ),
            _proposal(
                "Dog registration", "30 days.", "A dog must be registered within 30 days.", "City"
            ),
        ],
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")

    from markai.knowledge.store import KnowledgeStore

    store = KnowledgeStore(data / "markai.db")
    store.log_question("s1", "how long do I have to return a deposit", "none", [], True, {})
    store.close()

    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0, result.stdout
    body = (data / "mark-report.txt").read_text(encoding="utf-8")

    assert "sk-ant" not in body and "secret" not in body
    assert "how long do I have to return" not in body, "other people's words stay out by default"
    assert "anthropic key set" in body
    assert "distinct rules: 2" in body
    assert "off topic" in body and "Dog registration" in body
    assert str(manifest.parent) in body, "it says which facts.yaml it read"

    asked = runner.invoke(app, ["report", "--questions"])
    assert asked.exit_code == 0
    assert "how long do I have to return" in (data / "mark-report.txt").read_text(encoding="utf-8")


def test_a_windows_codepage_never_kills_a_command():
    """`mark report` wrote its file, then died printing the tick. The work was done."""
    import io

    from rich.console import Console

    from markai.cli import _printable

    raw = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", line_buffering=True)
    with pytest.raises(UnicodeEncodeError):
        Console(file=raw, force_terminal=False).print("[green]✓[/green] Wrote the report")

    safe = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", line_buffering=True)
    Console(file=_printable(safe), force_terminal=False).print("[green]✓[/green] Wrote the report")
    assert "Wrote the report" in safe.buffer.getvalue().decode("utf-8", "replace")


def test_a_stream_that_cannot_be_reconfigured_is_left_alone():
    from markai.cli import _printable

    class Plain:
        pass

    stream = Plain()
    assert _printable(stream) is stream


def test_facts_review_can_print_the_cards_without_deciding(tmp_path, monkeypatch):
    """So the queue can be read somewhere other than a prompt."""
    manifest, data = _waiting(
        tmp_path,
        monkeypatch,
        [
            _proposal(
                "Non-renewal notice",
                "90 days.",
                "Under the updated RLTO, Evanston landlords must give 90 days' notice.",
                "A post",
                jurisdiction="",
            )
        ],
    )
    result = runner.invoke(app, ["facts", "review", "--list"])
    assert result.exit_code == 0, result.stdout
    assert "Non-renewal notice" in result.stdout
    assert "Evanston" in result.stdout, "the card has to say which city before anyone accepts it"
    assert "Nothing was decided" in result.stdout

    assert not (manifest.parent / "facts.yaml").exists()
    left = json.loads((data / "facts-proposals.json").read_text(encoding="utf-8"))["proposals"]
    assert len(left) == 1, "listing decides nothing"
