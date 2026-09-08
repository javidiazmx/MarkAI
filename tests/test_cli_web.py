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
    "password": "six flats and a boiler",
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


def test_signing_in_again_from_a_clean_browser(settings, store):
    client = _client(settings, store, FakeAdvisor())
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    client.post("/api/logout")
    assert client.get("/api/account").json()["signed_in"] is False

    # A different browser id entirely: the account is the identity, not the browser.
    back = client.post(
        "/api/login",
        json={"email": "JAVIER@example.com", "password": "six flats and a boiler"},
        headers={"X-Browser-Id": "b2"},
    )
    assert back.json()["name"] == "Javier Diaz"
    assert _ask(client, "t9", "A question from the other machine", browser="b2").status_code == 200


def test_a_wrong_password_is_refused_without_saying_which_half(settings, store):
    client = _client(settings, store)
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    client.post("/api/logout")

    wrong = client.post("/api/login", json={"email": "javier@example.com", "password": "not it"})
    unknown = client.post("/api/login", json={"email": "nobody@example.com", "password": "not it"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]
    assert client.get("/api/account").json()["signed_in"] is False


def test_a_signup_that_is_missing_a_password_is_refused(settings, store):
    client = _client(settings, store)
    bad = client.post(
        "/api/account",
        json={**SIGNUP, "password": "short"},
        headers={"X-Browser-Id": "b1"},
    )
    assert bad.status_code == 400
    assert "8 characters" in bad.json()["detail"]


def test_the_session_cookie_cannot_be_read_by_a_script(settings, store):
    client = _client(settings, store)
    made = client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    header = made.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header, "so no other site can post with it"


def test_conversations_follow_the_account_not_the_browser(settings, store):
    client = _client(settings, store, FakeAdvisor())
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    _ask(client, "t1", "How long for a deposit?")
    client.post("/api/properties", json={"label": "2145 W Division"})

    client.post("/api/logout")
    assert client.get("/api/threads", headers={"X-Browser-Id": "b2"}).json() == {"threads": []}

    client.post(
        "/api/login", json={"email": "javier@example.com", "password": "six flats and a boiler"}
    )
    threads = client.get("/api/threads", headers={"X-Browser-Id": "b2"}).json()["threads"]
    assert [t["title"] for t in threads] == ["How long for a deposit"]
    properties = client.get("/api/properties", headers={"X-Browser-Id": "b2"}).json()
    assert [p["label"] for p in properties["properties"]] == ["2145 W Division"]


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
    for field in ("su-name", "su-email", "su-phone", "su-hood", "su-pass"):
        assert f'id="{field}"' in page
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


def test_mark_accounts_resets_a_password(tmp_path, monkeypatch):
    from markai.web.accounts import Accounts

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    store = Accounts(data_dir / "accounts.db")
    _account, token = store.create(
        {
            "name": "Javier Diaz",
            "email": "javier@example.com",
            "phone": "312-555-0134",
            "neighborhood": "Logan Square",
            "password": "the old password",
        }
    )
    store.close()

    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data_dir))

    done = runner.invoke(
        app, ["accounts", "reset-password", "javier@example.com", "--password", "a new password"]
    )
    assert done.exit_code == 0

    store = Accounts(data_dir / "accounts.db")
    assert store.account_for_token(token) is None, "the reset signed that session out"
    assert store.sign_in("javier@example.com", "a new password")[0]
    store.close()

    missing = runner.invoke(
        app, ["accounts", "reset-password", "nobody@example.com", "--password", "a new password"]
    )
    assert missing.exit_code == 1


# --- the lead reaching the CRM ----------------------------------------------------------


def test_a_signup_queues_a_lead_with_what_they_asked_about(settings, store):
    from markai.web.crm import Crm

    sent = []
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}
    _ask(client, "t1", "How long do I have to return a deposit?")

    # Swap the queue's sender before signing up, so nothing leaves the process.
    queue = Crm(settings.data_dir / "leads.db", url="https://crm.test/hook", sender=sent.append)
    client.post("/api/account", json={**SIGNUP, "password": ""}, headers=headers)
    queue.deliver_pending()
    queue.close()

    assert len(sent) == 1
    lead = sent[0]
    assert lead["email"] == "javier@example.com"
    assert lead["neighborhood"] == "Logan Square"
    assert lead["asked_about"] == "How long do I have to return a deposit"
    assert lead["has_password"] is False


def test_a_signup_without_a_password_is_still_remembered(settings, store):
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}
    _ask(client, "t1", "One")
    _ask(client, "t2", "Two")

    made = client.post("/api/account", json={**SIGNUP, "password": ""}, headers=headers)
    assert made.status_code == 200
    assert made.json()["has_password"] is False
    assert _ask(client, "t3", "Third question").status_code == 200, "no wall on this device"

    state = client.get("/api/account", headers=headers).json()
    assert state["signed_in"] is True and state["has_password"] is False


def test_the_password_can_be_required_by_the_owner(settings, store):
    settings = settings.model_copy(update={"password_required": True})
    client = _client(settings, store)
    bad = client.post(
        "/api/account", json={**SIGNUP, "password": ""}, headers={"X-Browser-Id": "b1"}
    )
    assert bad.status_code == 400
    assert "8 characters" in bad.json()["detail"]


def test_a_password_added_later_keeps_this_device_signed_in(settings, store):
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/account", json={**SIGNUP, "password": ""}, headers=headers)

    added = client.post("/api/password", json={"password": "six flats and a boiler"})
    assert added.json() == {"has_password": True}
    assert client.get("/api/account", headers=headers).json()["signed_in"] is True

    client.post("/api/logout")
    back = client.post(
        "/api/login", json={"email": "javier@example.com", "password": "six flats and a boiler"}
    )
    assert back.json()["has_password"] is True


def test_adding_a_password_needs_to_be_signed_in(settings, store):
    client = _client(settings, store)
    refused = client.post("/api/password", json={"password": "six flats and a boiler"})
    assert refused.status_code == 401


def test_an_email_already_taken_without_a_password_is_not_handed_over(settings, store):
    client = _client(settings, store, FakeAdvisor())
    client.post("/api/account", json={**SIGNUP, "password": ""}, headers={"X-Browser-Id": "b1"})
    _ask(client, "t1", "Something private about my building")
    client.post("/api/logout")

    again = client.post(
        "/api/account",
        json={**SIGNUP, "name": "Someone Else", "password": ""},
        headers={"X-Browser-Id": "b2"},
    )
    assert again.status_code == 400
    assert "no password yet" in again.json()["detail"]
    assert client.get("/api/threads", headers={"X-Browser-Id": "b2"}).json() == {"threads": []}


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
    assert "No CRM webhook set" in listed.stdout
    assert "1 waiting" in listed.stdout

    blocked = runner.invoke(app, ["leads", "send"])
    assert blocked.exit_code == 1
    assert "MARKAI_CRM_WEBHOOK_URL" in blocked.stdout + str(blocked.stderr)
