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
    assert "Your Chicagoland Landlord Advisor" in response.text


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


def test_the_server_separates_frames_with_crlf_and_the_page_expects_it(tmp_path):
    """Every answer arrived, was billed, and rendered as an empty bubble.

    sse-starlette ends every line with CRLF, so frames are separated by "\\r\\n\\r\\n". The
    page split on "\\n\\n", which never occurs in that stream: nothing was ever parsed. This
    pins both halves, because a library default is exactly the kind of thing that changes
    underneath you.
    """
    import re
    from pathlib import Path

    settings = Settings(_env_file=None, data_dir=tmp_path / "markai-sse-check")
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


class _FakeLock:
    """`_events` only ever calls `.release()` on the real per-session lock."""

    def release(self) -> None:
        pass


def test_a_missing_api_key_does_not_leak_setup_instructions_to_the_browser():
    """The message is meant for whoever runs the server, not whichever landlord's browser
    happens to be open when the key falls out of .env."""
    from markai.advisor.mark import MissingApiKeyError
    from markai.web.app import _events

    def broken_get_advisor():
        raise MissingApiKeyError(
            "ANTHROPIC_API_KEY is not set. Run `mark init`, or copy .env.example to .env."
        )

    events = list(_events(broken_get_advisor, conversation=None, message="hi", lock=_FakeLock()))
    assert len(events) == 1 and events[0]["event"] == "error"
    message = json.loads(events[0]["data"])["message"]
    assert "ANTHROPIC_API_KEY" not in message and "mark init" not in message


def test_a_missing_file_does_not_leak_the_server_s_path_layout():
    """The raw OSError text is a filesystem path on the server (e.g. /mnt/data/...) - not
    something a browser on the other end of the internet needs to see."""
    from markai.web.app import _events

    def broken_get_advisor():
        raise FileNotFoundError(2, "No such file or directory", "/mnt/data/markai.db")

    events = list(_events(broken_get_advisor, conversation=None, message="hi", lock=_FakeLock()))
    assert len(events) == 1 and events[0]["event"] == "error"
    message = json.loads(events[0]["data"])["message"]
    assert "/mnt/data" not in message


def test_an_unexpected_crash_mid_stream_does_not_echo_the_exception_text():
    """The catch-all used to hand str(exc) straight to the browser - whatever a DB, SDK, or
    filesystem error happened to say, verbatim."""
    from markai.web.app import _events

    class _CrashingAdvisor:
        def stream(self, *args, **kwargs):
            raise RuntimeError("connection to internal-db-host-04.prod failed: auth denied")
            yield  # pragma: no cover - makes this a generator without ever running

    events = list(
        _events(lambda: _CrashingAdvisor(), conversation=None, message="hi", lock=_FakeLock())
    )
    assert len(events) == 1 and events[0]["event"] == "error"
    message = json.loads(events[0]["data"])["message"]
    assert "internal-db-host-04" not in message and "auth denied" not in message


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
    assert IDENTITY_NOTICE.startswith("Jay is an agent built by GC Realty")
    assert "Mark Ainley" in IDENTITY_NOTICE, "still named, to say plainly it isn't him"
    assert "<h1>Ask Jay</h1>" in page
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
    assert "link.href = target;" in page, "built from the matched text, never from markup"


def test_the_page_no_longer_shows_the_corpus_counts():
    """Sources and passages are operator diagnostics, not something a landlord reads."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert "passages · " not in page
    assert "keyword + semantic search" not in page
    assert "Your Chicagoland Landlord Advisor" in page


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


def test_facts_stale_says_so_when_nothing_is_flagged(tmp_path, monkeypatch):
    _facts_dir(tmp_path, monkeypatch, GOOD_FACTS)
    result = runner.invoke(app, ["facts", "stale"])
    assert result.exit_code == 0
    assert "No rate/fee figure" in result.stdout


def test_facts_stale_flags_an_old_dollar_figure_but_not_a_notice_period(tmp_path, monkeypatch):
    _facts_dir(
        tmp_path,
        monkeypatch,
        "ordinances:\n"
        "  - id: fee-2021\n"
        "    jurisdiction: Cook County, IL\n"
        "    topic: Late fees\n"
        "    rule: Late fees cannot exceed $10 for the first $1,000 of rent plus 5%.\n"
        "    citation: What Is the Cook County RTLO That Passed January 2021\n"
        "  - id: notice-1\n"
        "    jurisdiction: Chicago\n"
        "    topic: Non-renewal notice\n"
        "    rule: 60 days notice, or 120 if the tenant has lived there three years or longer.\n"
        "    citation: An old blog post\n",
    )
    result = runner.invoke(app, ["facts", "stale"])
    assert result.exit_code == 0
    assert "fee-2021" not in result.stdout, "the table shows the topic, not the raw id"
    assert "Late fees" in result.stdout
    assert "2021" in result.stdout
    assert "Non-renewal notice" not in result.stdout, "a fixed notice period is never flagged"
    assert "1 rule(s) flagged" in result.stdout


@respx.mock(assert_all_called=False)
def test_facts_stale_check_live_confirms_a_figure_against_its_government_source(
    tmp_path, monkeypatch, respx_mock
):
    from markai.sources.live_check import AUTHORITATIVE_SOURCES

    _facts_dir(
        tmp_path,
        monkeypatch,
        "ordinances:\n"
        "  - id: fee-2021\n"
        "    jurisdiction: Cook County, IL\n"
        "    topic: Late fees\n"
        "    rule: Late fees cannot exceed $10 for the first $1,000 of rent plus 5%.\n"
        "    citation: What Is the Cook County RTLO That Passed January 2021\n",
    )
    respx_mock.get(AUTHORITATIVE_SOURCES["Suburban Cook County"]).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><p>The late fee is $10 if the rent is $1000 or less, "
            "plus 5% of any amount over $1000.</p></body></html>",
        )
    )
    result = runner.invoke(app, ["facts", "stale", "--check-live"])
    assert result.exit_code == 0
    assert "confirmed" in result.stdout
    assert "1 confirmed against an official source" in result.stdout


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


def test_business_info_shares_the_same_escalation_contact_as_the_handoff(tmp_path, settings, store):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(
        "websites: []\nbusiness:\n  name: GC Realty\n  escalation_name: Russell\n"
        "  escalation_url: https://calendly.com/example/20min\n",
        encoding="utf-8",
    )
    settings = settings.model_copy(update={"sources_file": manifest})
    client = _client(settings, store)
    info = client.get("/api/business", headers={"X-Browser-Id": "b1"}).json()
    assert info == {
        "escalation_name": "Russell",
        "escalation_url": "https://calendly.com/example/20min",
    }


def test_business_info_is_gated_by_the_access_code(settings, store):
    settings = settings.model_copy(update={"web_access_code": "letmein"})
    client = _client(settings, store)
    assert client.get("/api/business", headers={"X-Browser-Id": "b1"}).status_code == 401


def test_the_handoff_is_rate_limited_against_a_flood_from_one_address(settings, store):
    """Signup and handoff are both reachable with no access code configured at all in a
    real launch, so a script hammering either one has nothing else standing in its way -
    a basic per-address cap must kick in."""
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    for _ in range(10):
        resp = client.post("/api/handoff", json={"session_id": "t1"}, headers=headers)
        assert resp.status_code == 200
    flooded = client.post("/api/handoff", json={"session_id": "t1"}, headers=headers)
    assert flooded.status_code == 429


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


def test_four_questions_are_answered_then_the_wall(settings, store):
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}

    assert client.get("/api/account", headers=headers).json()["free_left"] == 4
    assert _ask(client, "t1", "First question").status_code == 200
    assert client.get("/api/account", headers=headers).json()["free_left"] == 3
    assert _ask(client, "t2", "Second question").status_code == 200
    assert _ask(client, "t3", "Third question").status_code == 200
    assert _ask(client, "t4", "Fourth question").status_code == 200

    walled = client.post(
        "/api/chat", json={"session_id": "t5", "message": "Fifth"}, headers=headers
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
    assert client.get("/api/account", headers=headers).json()["free_left"] == 4


def test_another_browser_still_has_its_own_free_questions(settings, store):
    client = _client(settings, store, FakeAdvisor())
    _ask(client, "t1", "One", browser="b1")
    _ask(client, "t2", "Two", browser="b1")
    assert client.get("/api/account", headers={"X-Browser-Id": "b2"}).json()["free_left"] == 4
    assert _ask(client, "t3", "One", browser="b2").status_code == 200


def test_deleting_the_conversations_does_not_hand_back_a_free_question(settings, store):
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}
    _ask(client, "t1", "One")
    _ask(client, "t2", "Two")
    client.delete("/api/threads/t1", headers=headers)
    client.delete("/api/threads/t2", headers=headers)
    assert client.get("/api/account", headers=headers).json()["free_left"] == 2


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


# --- signals worth a call about a property manager --------------------------------------


def _signals(settings):
    """Just the candidate-alert leads, not the signup lead signing up always queues."""
    from markai.web.crm import Crm

    crm = Crm(settings.data_dir / "leads.db")
    leads = crm.all()
    crm.close()
    return [lead.payload["signal"] for lead in leads if "signal" in lead.payload]


def test_asking_about_a_pm_queues_a_candidate_lead(settings, store):
    from markai.advisor.guardrails import FLAG_PM_INTEREST

    client = _client(settings, store, FakeAdvisor(flags=[FLAG_PM_INTEREST]))
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/account", json=SIGNUP, headers=headers)

    _ask(client, "t1", "How much does a property manager cost?")

    assert _signals(settings) == ["pm_interest"]


def test_asking_twice_only_queues_the_lead_once(settings, store):
    from markai.advisor.guardrails import FLAG_PM_INTEREST

    client = _client(settings, store, FakeAdvisor(flags=[FLAG_PM_INTEREST]))
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/account", json=SIGNUP, headers=headers)

    _ask(client, "t1", "Should I hire a property manager?")
    _ask(client, "t2", "How much would a property manager cost?")

    assert _signals(settings) == ["pm_interest"], "one fact, told twice, is still one lead"


def test_an_anonymous_visitor_asking_about_a_pm_queues_nothing(settings, store):
    from markai.advisor.guardrails import FLAG_PM_INTEREST

    client = _client(settings, store, FakeAdvisor(flags=[FLAG_PM_INTEREST]))
    _ask(client, "t1", "How much does a property manager cost?")

    assert _signals(settings) == [], "there is nobody to call yet"


def test_a_question_with_no_pm_interest_queues_nothing(settings, store):
    client = _client(settings, store, FakeAdvisor())
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/account", json=SIGNUP, headers=headers)

    _ask(client, "t1", "How long do I have to return a deposit?")

    assert _signals(settings) == []


def test_crossing_four_units_queues_a_candidate_lead_once(settings, store):
    """4 units, not a 2nd property, is the documented self-management burnout point - three
    single-families add up to the same load a "2nd property" count would miss entirely."""
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/account", json=SIGNUP, headers=headers)

    client.post("/api/properties", json={"label": "2145 W Division", "units": "1"}, headers=headers)
    assert _signals(settings) == [], "one unit is nowhere near the line"

    client.post("/api/properties", json={"label": "Berwyn two-flat", "units": "2"}, headers=headers)
    assert _signals(settings) == [], "3 units total - not there yet"

    client.post(
        "/api/properties", json={"label": "Oak Park bungalow", "units": "1"}, headers=headers
    )
    assert _signals(settings) == ["portfolio_growth"], "4 units total crosses the line"

    client.post(
        "/api/properties", json={"label": "A fifth unit later", "units": "1"}, headers=headers
    )
    assert _signals(settings) == ["portfolio_growth"], "a fifth unit is not new information"


def test_a_single_large_property_crosses_the_threshold_on_its_own(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/account", json=SIGNUP, headers=headers)

    client.post("/api/properties", json={"label": "Berwyn six-flat", "units": "6"}, headers=headers)
    assert _signals(settings) == ["portfolio_growth"], "one 6-unit building is past the line"


def test_a_property_with_no_unit_count_counts_as_one(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/account", json=SIGNUP, headers=headers)

    for label in ("A", "B", "C", "D"):
        client.post("/api/properties", json={"label": label}, headers=headers)
    assert _signals(settings) == ["portfolio_growth"], (
        "4 properties, no units given, still counts as 4"
    )


def test_an_anonymous_visitors_properties_queue_nothing(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/properties", json={"label": "2145 W Division", "units": "6"}, headers=headers)

    assert _signals(settings) == [], "there is nobody to call yet"


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


def test_mark_leads_candidates_shows_only_the_behavioral_alerts(tmp_path, monkeypatch):
    from markai.web.accounts import Account
    from markai.web.crm import Crm, build_payload

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    account = Account(id="a1", email="javier@example.com", name="Javier Diaz", phone="312-555-0134")
    queue = Crm(data_dir / "leads.db")
    queue.enqueue("a1", build_payload(account))  # a plain signup, no signal
    queue.enqueue(
        "a1",
        build_payload(account, {"signal": "pm_interest", "reason": "Asked about hiring a PM"}),
    )
    queue.close()

    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data_dir))

    everything = runner.invoke(app, ["leads", "list"])
    assert everything.stdout.count("javier@example.com") == 2
    assert "Signed up" in everything.stdout
    assert "Asked about hiring a PM" in everything.stdout

    only_signals = runner.invoke(app, ["leads", "list", "--candidates"])
    assert only_signals.exit_code == 0
    assert only_signals.stdout.count("javier@example.com") == 1
    assert "Asked about hiring a PM" in only_signals.stdout
    assert "Signed up" not in only_signals.stdout


def test_doctor_reports_on_the_page_the_accounts_and_the_crm(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(tmp_path / "data"))

    bare = runner.invoke(app, ["doctor"])
    assert bare.exit_code == 0
    assert "Web access code" in bare.stdout and "not set" in bare.stdout
    assert "4 free question" in bare.stdout and "No password" in bare.stdout
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
    old.execute("INSERT INTO usage VALUES ('b-javid', 4)")
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

    # The four questions already spent still count, so the wall stays where it was.
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


def test_facts_review_skipping_one_card_leaves_it_for_later(tmp_path, monkeypatch):
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
    result = runner.invoke(app, ["facts", "review"], input="s\ns\n")
    assert result.exit_code == 0, result.stdout
    assert not (manifest.parent / "facts.yaml").exists(), "nothing accepted, nothing written"
    left = json.loads((data / "facts-proposals.json").read_text(encoding="utf-8"))["proposals"]
    assert len(left) == 2, "skipping a card leaves it for later, same as skipping its topic"


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


def test_costless_maintenance_and_notes_are_left_out_of_the_money_log(settings, store):
    """Live feedback: the Income & expenses panel was showing every tracking entry - a
    heater complaint, a note about a tenant lockout - with no dollar amount at all. That
    view is about money moving; a costless maintenance/note row belongs to the Maintenance
    Tracker instead. A row that DOES carry a cost, even a still-open maintenance one, still
    belongs here - it just isn't counted in the totals until it's actually done."""
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    client.post("/api/log", json={"kind": "maintenance", "what": "Heater is out"}, headers=headers)
    client.post("/api/log", json={"kind": "note", "what": "Tenant locked out"}, headers=headers)
    client.post(
        "/api/log",
        json={"kind": "maintenance", "what": "Replace kitchen filter", "amount": "200"},
        headers=headers,
    )
    client.post(
        "/api/log",
        json={"kind": "expense", "what": "New boiler", "amount": "8000"},
        headers=headers,
    )

    read = client.get("/api/log", headers=headers).json()
    whats = {e["what"] for e in read["entries"]}
    assert whats == {"Replace kitchen filter", "New boiler"}
    open_whats = {e["what"] for e in read["open"]}
    assert open_whats == {"Replace kitchen filter"}


def test_monthly_route_returns_a_full_window_oldest_first(settings, store):
    from datetime import date

    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    this_month = date.today().isoformat()[:7]
    client.post(
        "/api/log",
        json={"kind": "income", "what": "Rent", "amount": "1000", "date": this_month + "-05"},
        headers=headers,
    )
    months = client.get("/api/log/monthly?months=3", headers=headers).json()["months"]
    assert len(months) == 3
    assert months[-1]["month"] == this_month, "the current month is last, oldest first"
    assert months[-1]["in"] == 1000


def test_by_property_route_covers_every_building_plus_unassigned(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    p1 = client.post("/api/properties", json={"label": "2145 W Division"}, headers=headers).json()[
        "property"
    ]["id"]
    client.post(
        "/api/log",
        json={"kind": "expense", "what": "Boiler", "amount": "8000", "property_id": p1},
        headers=headers,
    )
    client.post(
        "/api/log", json={"kind": "income", "what": "Cash gig, no building"}, headers=headers
    )
    client.post(
        "/api/log",
        json={"kind": "income", "what": "Odd job", "amount": "500"},
        headers=headers,
    )

    rows = client.get("/api/log/by-property", headers=headers).json()["properties"]
    by_label = {r["label"]: r for r in rows}
    assert by_label["2145 W Division"]["out"] == 8000
    assert by_label["Unassigned"]["in"] == 500


def test_by_property_route_leaves_out_unassigned_when_everything_has_a_building(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    p1 = client.post("/api/properties", json={"label": "2145 W Division"}, headers=headers).json()[
        "property"
    ]["id"]
    client.post(
        "/api/log",
        json={"kind": "expense", "what": "Boiler", "amount": "8000", "property_id": p1},
        headers=headers,
    )
    rows = client.get("/api/log/by-property", headers=headers).json()["properties"]
    assert [r["label"] for r in rows] == ["2145 W Division"]


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


def test_maintenance_route_returns_only_open_maintenance_worst_first(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    client.post(
        "/api/log", json={"kind": "expense", "what": "New sign", "amount": 200}, headers=headers
    )
    client.post(
        "/api/log",
        json={"kind": "maintenance", "what": "Loose cabinet handle", "urgency": "routine"},
        headers=headers,
    )
    client.post(
        "/api/log",
        json={"kind": "maintenance", "what": "Gas smell in unit 3", "urgency": "emergency"},
        headers=headers,
    )
    open_items = client.get("/api/maintenance", headers=headers).json()["open"]
    assert [item["what"] for item in open_items] == [
        "Gas smell in unit 3",
        "Loose cabinet handle",
    ]


def test_maintenance_route_is_gated_by_the_access_code(settings, store):
    settings = settings.model_copy(update={"web_access_code": "letmein"})
    client = _client(settings, store)
    assert client.get("/api/maintenance", headers={"X-Browser-Id": "b1"}).status_code == 401


def test_maintenance_route_also_returns_completed_history(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log", json={"kind": "maintenance", "what": "Loose cabinet handle"}, headers=headers
    ).json()["entry"]

    client.post(f"/api/maintenance/{entry['id']}/complete", headers=headers)

    data = client.get("/api/maintenance", headers=headers).json()
    assert data["open"] == []
    assert [item["what"] for item in data["history"]] == ["Loose cabinet handle"]


def test_completing_a_priced_maintenance_issue_logs_a_matching_expense(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log",
        json={
            "kind": "maintenance",
            "what": "New water heater",
            "vendor": "ABC Plumbing",
            "amount": "$900",
        },
        headers=headers,
    ).json()["entry"]

    done = client.post(f"/api/maintenance/{entry['id']}/complete", headers=headers)
    assert done.status_code == 200
    assert done.json()["entry"]["status"] == "done"

    totals = client.get("/api/log", headers=headers).json()["totals"]
    assert totals["out"] == 900, "the completed job's cost should now count as money out"


def test_completing_a_missing_maintenance_entry_is_a_404(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    assert client.post("/api/maintenance/nope/complete", headers=headers).status_code == 404


def test_marking_a_priced_maintenance_entry_done_from_the_generic_route_also_logs_the_expense(
    settings, store
):
    """Live bug: a priced maintenance issue now shows in the Income & expenses panel too
    (it has a cost), which has its own "mark done" control that hit the generic close route
    - and that route used to just flip status, with none of the auto-expense logic the
    Maintenance Tracker's own "Mark done" button has. Wherever "done" is clicked, a priced
    maintenance issue must log its companion expense exactly the same way."""
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log",
        json={
            "kind": "maintenance",
            "what": "Replace kitchen filter",
            "vendor": "mE",
            "amount": "200",
        },
        headers=headers,
    ).json()["entry"]

    done = client.post(f"/api/log/{entry['id']}/done", headers=headers)
    assert done.status_code == 200
    assert done.json() == {"closed": True}

    totals = client.get("/api/log", headers=headers).json()["totals"]
    assert totals["out"] == 200, "the generic done route must log the expense too"


def test_an_entry_can_be_edited(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log",
        json={"kind": "expense", "what": "New boiler", "amount": "8000", "vendor": "ABC Heating"},
        headers=headers,
    ).json()["entry"]

    edited = client.post(
        f"/api/log/{entry['id']}/edit",
        json={
            "what": "New boiler (corrected)",
            "amount": "8500",
            "vendor": "ABC Heating Co",
            "date": "2026-09-10",
        },
        headers=headers,
    )
    assert edited.status_code == 200
    saved = edited.json()["entry"]
    assert saved["what"] == "New boiler (corrected)"
    assert saved["amount"] == 8500
    assert saved["vendor"] == "ABC Heating Co"
    assert saved["happened_on"] == "2026-09-10"

    totals = client.get("/api/log", headers=headers).json()["totals"]
    assert totals["out"] == 8500, "the edited amount, not the original, should count"


def test_editing_does_not_touch_status_property_or_photo(settings, store):
    """A dedicated UPDATE, not the add() upsert - so completing an item, attaching a
    photo, and then only fixing its cost must leave the completion and the photo alone."""
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log",
        json={"kind": "maintenance", "what": "New water heater", "amount": "900"},
        headers=headers,
    ).json()["entry"]
    client.post(f"/api/maintenance/{entry['id']}/complete", headers=headers)
    client.post(
        f"/api/log/{entry['id']}/photo",
        headers=headers,
        files={"file": ("job.jpg", _jpeg_bytes(), "image/jpeg")},
    )

    edited = client.post(
        f"/api/log/{entry['id']}/edit",
        json={"what": "New water heater", "amount": "950", "vendor": "", "date": "2026-09-16"},
        headers=headers,
    )
    assert edited.status_code == 200
    saved = edited.json()["entry"]
    assert saved["status"] == "done", "editing must not silently reopen a completed item"
    assert saved["has_photo"] is True, "editing must not wipe the attached photo"


def test_an_entry_with_nothing_in_it_cannot_be_edited_to_blank(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log", json={"kind": "note", "what": "Tenant locked out"}, headers=headers
    ).json()["entry"]
    bad = client.post(
        f"/api/log/{entry['id']}/edit",
        json={"what": "", "amount": "", "vendor": "", "date": ""},
        headers=headers,
    )
    assert bad.status_code == 400


def test_editing_someone_elses_entry_is_a_404(settings, store):
    client = _client(settings, store)
    entry = client.post(
        "/api/log",
        json={"kind": "note", "what": "Mine"},
        headers={"X-Browser-Id": "b1"},
    ).json()["entry"]
    other = client.post(
        f"/api/log/{entry['id']}/edit",
        json={"what": "Not yours", "amount": "", "vendor": "", "date": ""},
        headers={"X-Browser-Id": "b2"},
    )
    assert other.status_code == 404


def test_vendor_cost_can_be_filled_in_after_the_fact(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log", json={"kind": "maintenance", "what": "Leaky faucet"}, headers=headers
    ).json()["entry"]

    saved = client.post(
        f"/api/log/{entry['id']}/vendor-cost",
        json={"vendor": "Joe's Plumbing", "amount": "$150"},
        headers=headers,
    )
    assert saved.json() == {"saved": True}

    open_items = client.get("/api/maintenance", headers=headers).json()["open"]
    assert open_items[0]["vendor"] == "Joe's Plumbing"
    assert open_items[0]["amount"] == 150


def test_vendor_cost_rejects_a_negative_amount(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log", json={"kind": "maintenance", "what": "Leaky faucet"}, headers=headers
    ).json()["entry"]
    bad = client.post(
        f"/api/log/{entry['id']}/vendor-cost",
        json={"vendor": "Joe's Plumbing", "amount": "-50"},
        headers=headers,
    )
    assert bad.status_code == 400


def _jpeg_bytes() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def test_a_photo_can_be_uploaded_and_read_back(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log", json={"kind": "maintenance", "what": "New water heater"}, headers=headers
    ).json()["entry"]

    uploaded = client.post(
        f"/api/log/{entry['id']}/photo",
        headers=headers,
        files={"file": ("job.jpg", _jpeg_bytes(), "image/jpeg")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json() == {"saved": True}

    listed = client.get("/api/maintenance", headers=headers).json()["open"]
    assert listed[0]["has_photo"] is True

    photo = client.get(f"/api/log/{entry['id']}/photo", headers=headers)
    assert photo.status_code == 200
    assert photo.headers["content-type"] == "image/jpeg"


def test_a_photo_upload_rejects_a_non_image(settings, store):
    client = _client(settings, store)
    headers = {"X-Browser-Id": "b1"}
    entry = client.post(
        "/api/log", json={"kind": "maintenance", "what": "New water heater"}, headers=headers
    ).json()["entry"]
    bad = client.post(
        f"/api/log/{entry['id']}/photo",
        headers=headers,
        files={"file": ("job.txt", b"not a photo", "text/plain")},
    )
    assert bad.status_code == 400


def test_one_owners_photo_is_invisible_to_another(settings, store):
    client = _client(settings, store)
    entry = client.post(
        "/api/log",
        json={"kind": "maintenance", "what": "New water heater"},
        headers={"X-Browser-Id": "b1"},
    ).json()["entry"]
    client.post(
        f"/api/log/{entry['id']}/photo",
        headers={"X-Browser-Id": "b1"},
        files={"file": ("job.jpg", _jpeg_bytes(), "image/jpeg")},
    )

    other = client.get(f"/api/log/{entry['id']}/photo", headers={"X-Browser-Id": "b2"})
    assert other.status_code == 404


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


def test_the_page_links_a_bare_domain_a_phone_and_an_address():
    """Jay writes "gcrealtyinc.com/free-rental-analysis", not a full URL. It has to be a link.

    Checked against the page source rather than a browser, so the patterns stay honest: the
    endings list is what stops "facts.yaml" and an ordinance number becoming links.
    """
    import re
    from pathlib import Path as _Path

    page = (_Path(__file__).parent.parent / "markai/web/static/index.html").read_text(
        encoding="utf-8"
    )
    tlds = re.search(r'var TLDS = "([^"]+)"', page)
    assert tlds, "the endings list is what keeps this from linking everything"
    assert "com" in tlds.group(1).split("|")

    # The four shapes the answer path actually produces, and the ones it must leave alone.
    source = page[page.index("var URL_RE") : page.index("function inline")]
    for shape in ("https?", "@", "TLDS", r"\\d{3}"):
        assert shape in source, f"{shape} is not matched any more"
    assert 'anchor("https://" + href)' in page, "a bare domain gets the scheme added here"
    assert 'anchor("tel:+"' in page, "a phone number is one tap on the device reading it"
    assert 'anchor("mailto:" + href)' in page
    assert "link.href = target;" in page, "the href is built, never taken from the answer"


# --- the admin panel ---------------------------------------------------------------------


def test_the_admin_page_is_served(settings, store):
    response = _client(settings, store).get("/admin")
    assert response.status_code == 200
    assert "Jay · Admin" in response.text


def test_admin_routes_refuse_with_no_code_configured(settings, store):
    """Unlike the landlord gate, an unset admin code refuses rather than allows."""
    client = _client(settings, store)
    assert client.get("/api/admin/users").status_code == 401
    assert client.post("/api/admin/users/a1/override", json={"good_fit": True}).status_code == 401


def test_admin_routes_are_gated_by_their_own_code(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    assert client.get("/api/admin/users").status_code == 401
    assert client.get("/api/admin/users", headers={"X-Admin-Code": "wrong"}).status_code == 401

    ok = client.get("/api/admin/users", headers={"X-Admin-Code": "staffonly"})
    assert ok.status_code == 200
    assert ok.json() == {"users": []}


def test_the_landlord_access_code_does_not_open_the_admin_gate(settings, store):
    """The two codes are independent - knowing one must grant nothing on the other."""
    settings = settings.model_copy(
        update={"web_access_code": "letmein", "admin_access_code": "staffonly"}
    )
    client = _client(settings, store)
    assert client.get("/api/admin/users", headers={"X-Admin-Code": "letmein"}).status_code == 401


def test_repeated_wrong_admin_codes_from_one_address_are_locked_out(settings, store):
    """Five wrong guesses lock that address out for a cooldown - a script guessing the
    admin code one attempt at a time must not be free to keep trying forever."""
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)

    for _ in range(5):
        resp = client.get("/api/admin/users", headers={"X-Admin-Code": "wrong"})
        assert resp.status_code == 401

    locked = client.get("/api/admin/users", headers={"X-Admin-Code": "staffonly"})
    assert locked.status_code == 429, (
        "the correct code must still be refused while this address is locked out"
    )


def test_a_correct_admin_code_resets_the_failure_count(settings, store):
    """Getting it right clears the slate - a landlord mistyping the code a couple of
    times before getting it right must not be creeping toward a lockout."""
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)

    for _ in range(3):
        assert client.get("/api/admin/users", headers={"X-Admin-Code": "wrong"}).status_code == 401
    ok = client.get("/api/admin/users", headers={"X-Admin-Code": "staffonly"})
    assert ok.status_code == 200

    for _ in range(3):
        assert client.get("/api/admin/users", headers={"X-Admin-Code": "wrong"}).status_code == 401
    still_ok = client.get("/api/admin/users", headers={"X-Admin-Code": "staffonly"})
    assert still_ok.status_code == 200, "the earlier failures must not have carried over"


def test_admin_users_lists_signups_with_a_summary_and_signals(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor(flags=[]))
    admin_headers = {"X-Admin-Code": "staffonly"}

    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    _ask(client, "t1", "How long for a deposit?")

    users = client.get("/api/admin/users", headers=admin_headers).json()["users"]
    assert len(users) == 1
    user = users[0]
    assert user["email"] == "javier@example.com"
    assert user["name"] == "Javier Diaz"
    assert user["summary"] == ["How long for a deposit"]
    assert user["signals"] == []
    assert user["good_fit"] is False


def test_a_pain_point_flag_marks_the_landlord_a_good_fit_and_alerts_staff(
    settings, store, monkeypatch
):
    """A signal marks the checkbox and fires the alert - synchronously here so the test
    needs no thread join; `deliver_soon`'s own background-thread discipline is `crm.py`'s,
    mirrored by `send_pm_fit_alert_soon` and covered directly in test_admin_store.py."""
    from markai.advisor.guardrails import FLAG_TENANT_TROUBLE

    sent = []
    monkeypatch.setattr(
        "markai.web.admin_store.send_pm_fit_alert_soon",
        lambda sender, account, flag, reason: sent.append((account.email, flag)),
    )

    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor(flags=[FLAG_TENANT_TROUBLE]))
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    assert _ask(client, "t1", "My tenant hasn't paid in two months.").status_code == 200

    users = client.get("/api/admin/users", headers={"X-Admin-Code": "staffonly"}).json()["users"]
    assert users[0]["signals"] == ["tenant_trouble"]
    assert users[0]["good_fit"] is True
    assert sent == [("javier@example.com", "tenant_trouble")]


def test_the_same_signal_twice_alerts_staff_only_once(settings, store):
    from markai.advisor.guardrails import FLAG_TENANT_TROUBLE

    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor(flags=[FLAG_TENANT_TROUBLE]))
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})

    _ask(client, "t1", "My tenant hasn't paid.")
    _ask(client, "t2", "Still no rent from my tenant.")

    users = client.get("/api/admin/users", headers={"X-Admin-Code": "staffonly"}).json()["users"]
    assert users[0]["signals"] == ["tenant_trouble"], "one fact, told twice, is still one signal"


def test_an_anonymous_visitor_shows_up_without_being_identified(settings, store):
    """No name or email exists yet, but staff should still see the activity and the signal."""
    from markai.advisor.guardrails import FLAG_TENANT_TROUBLE

    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor(flags=[FLAG_TENANT_TROUBLE]))
    _ask(client, "t1", "My tenant hasn't paid.")

    users = client.get("/api/admin/users", headers={"X-Admin-Code": "staffonly"}).json()["users"]
    assert len(users) == 1
    visitor = users[0]
    assert visitor["anonymous"] is True
    assert visitor["name"] == "" and visitor["email"] == "" and visitor["phone"] == ""
    assert visitor["summary"] == ["My tenant hasn't paid"]
    assert visitor["signals"] == ["tenant_trouble"]
    assert visitor["good_fit"] is True


def test_an_anonymous_visitors_signal_never_alerts_or_queues_a_lead(settings, store, monkeypatch):
    """There is no contact to reach yet, so no email and no CRM lead - only for staff to see."""
    from markai.advisor.guardrails import FLAG_PM_INTEREST

    alerted = []
    monkeypatch.setattr(
        "markai.web.admin_store.send_pm_fit_alert_soon",
        lambda sender, account, flag, reason: alerted.append(flag),
    )
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor(flags=[FLAG_PM_INTEREST]))
    _ask(client, "t1", "How much does a property manager cost?")

    assert alerted == []
    assert _signals(settings) == []
    users = client.get("/api/admin/users", headers={"X-Admin-Code": "staffonly"}).json()["users"]
    assert users[0]["signals"] == ["pm_interest"]


def test_an_anonymous_visitors_signals_carry_over_when_they_sign_up(settings, store):
    """What they showed before the form is theirs, the same as their conversations already are."""
    from markai.advisor.guardrails import FLAG_TENANT_TROUBLE

    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor(flags=[FLAG_TENANT_TROUBLE]))
    admin_headers = {"X-Admin-Code": "staffonly"}
    headers = {"X-Browser-Id": "b1"}
    _ask(client, "t1", "My tenant hasn't paid.", browser="b1")

    client.post("/api/account", json=SIGNUP, headers=headers)

    users = client.get("/api/admin/users", headers=admin_headers).json()["users"]
    assert len(users) == 1, "the anonymous row is gone, merged into the new account"
    merged = users[0]
    assert merged["anonymous"] is False
    assert merged["email"] == "javier@example.com"
    assert merged["signals"] == ["tenant_trouble"]


def test_the_pm_interest_flag_still_reaches_the_crm_unchanged(settings, store):
    """The existing pm_interest to LeadSimple path must not regress with the new alert."""
    from markai.advisor.guardrails import FLAG_PM_INTEREST

    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor(flags=[FLAG_PM_INTEREST]))
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    _ask(client, "t1", "How much does a property manager cost?")

    assert _signals(settings) == ["pm_interest"]
    users = client.get("/api/admin/users", headers={"X-Admin-Code": "staffonly"}).json()["users"]
    assert users[0]["signals"] == ["pm_interest"]
    assert users[0]["good_fit"] is True


def test_staff_can_override_the_ai_and_clear_it_again(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    admin_headers = {"X-Admin-Code": "staffonly"}
    made = client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    email = made.json()["email"]

    users = client.get("/api/admin/users", headers=admin_headers).json()["users"]
    account_id = next(u["id"] for u in users if u["email"] == email)

    override = client.post(
        f"/api/admin/users/{account_id}/override",
        json={"good_fit": True},
        headers=admin_headers,
    )
    assert override.json() == {"saved": True}
    users = client.get("/api/admin/users", headers=admin_headers).json()["users"]
    flagged = next(u for u in users if u["id"] == account_id)
    assert flagged["good_fit"] is True
    assert flagged["manual_override"] is True

    client.post(
        f"/api/admin/users/{account_id}/override",
        json={"good_fit": None},
        headers=admin_headers,
    )
    users = client.get("/api/admin/users", headers=admin_headers).json()["users"]
    cleared = next(u for u in users if u["id"] == account_id)
    assert cleared["good_fit"] is False
    assert cleared["manual_override"] is None


# --- the per-visitor detail view: full info, the full transcript, and their IP ------------


def test_the_detail_view_has_full_contact_info_and_the_full_transcript(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor("45 days in Chicago."))
    admin_headers = {"X-Admin-Code": "staffonly"}
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    _ask(client, "t1", "How long for a deposit?")

    owner_id = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]["id"]
    detail = client.get(f"/api/admin/users/{owner_id}", headers=admin_headers).json()

    assert detail["anonymous"] is False
    assert detail["email"] == "javier@example.com"
    assert detail["phone"] == "312-555-0134"
    assert len(detail["threads"]) == 1
    assert [m["content"] for m in detail["threads"][0]["messages"]] == [
        "How long for a deposit?",
        "45 days in Chicago.",
    ]


def test_the_detail_view_works_for_an_anonymous_visitor_too(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor())
    admin_headers = {"X-Admin-Code": "staffonly"}
    _ask(client, "t1", "How long for a deposit?")

    owner_id = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]["id"]
    detail = client.get(f"/api/admin/users/{owner_id}", headers=admin_headers).json()

    assert detail["anonymous"] is True
    assert detail["email"] == "" and detail["name"] == ""
    assert len(detail["threads"]) == 1


def test_the_detail_view_is_gated_and_404s_for_an_unknown_visitor(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    assert client.get("/api/admin/users/browser:nobody").status_code == 401
    assert (
        client.get(
            "/api/admin/users/browser:nobody", headers={"X-Admin-Code": "staffonly"}
        ).status_code
        == 404
    )


def test_a_chat_question_records_the_visitors_ip_for_the_detail_view(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor())
    admin_headers = {"X-Admin-Code": "staffonly"}
    with client.stream(
        "POST",
        "/api/chat",
        json={"session_id": "t1", "message": "How long for a deposit?"},
        headers={"X-Browser-Id": "b1", "X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    ) as response:
        response.read()

    owner_id = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]["id"]
    detail = client.get(f"/api/admin/users/{owner_id}", headers=admin_headers).json()
    assert detail["last_ip"] == "203.0.113.7", "the first hop, not the proxy's own address"


def test_cf_connecting_ip_wins_over_x_forwarded_for(settings, store):
    """Cloudflare's own header names the real visitor; a generic forwarded-for can be spoofed
    further upstream of it, so Cloudflare's is trusted first."""
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor())
    admin_headers = {"X-Admin-Code": "staffonly"}
    with client.stream(
        "POST",
        "/api/chat",
        json={"session_id": "t1", "message": "Hi"},
        headers={
            "X-Browser-Id": "b1",
            "Cf-Connecting-Ip": "198.51.100.9",
            "X-Forwarded-For": "203.0.113.7",
        },
    ) as response:
        response.read()

    owner_id = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]["id"]
    detail = client.get(f"/api/admin/users/{owner_id}", headers=admin_headers).json()
    assert detail["last_ip"] == "198.51.100.9"


# --- staff notes and soft-hide on the admin panel -----------------------------------------


def test_staff_notes_save_and_appear_in_the_detail_view(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    admin_headers = {"X-Admin-Code": "staffonly"}
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})

    owner_id = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]["id"]
    saved = client.post(
        f"/api/admin/users/{owner_id}/notes",
        json={"notes": "Called, interested in a 6-unit"},
        headers=admin_headers,
    )
    assert saved.json() == {"saved": True}

    detail = client.get(f"/api/admin/users/{owner_id}", headers=admin_headers).json()
    assert detail["notes"] == "Called, interested in a 6-unit"
    listed = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]
    assert listed["notes"] == "Called, interested in a 6-unit"


def test_notes_route_is_gated_by_the_admin_code(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    assert client.post("/api/admin/users/a1/notes", json={"notes": "x"}).status_code == 401


def test_hiding_a_visitor_removes_them_from_the_default_list(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    admin_headers = {"X-Admin-Code": "staffonly"}
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    owner_id = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]["id"]

    hidden = client.post(
        f"/api/admin/users/{owner_id}/hidden", json={"hidden": True}, headers=admin_headers
    )
    assert hidden.json() == {"saved": True}

    default_view = client.get("/api/admin/users", headers=admin_headers).json()["users"]
    assert default_view == [], "hidden by default, but still there - not deleted"

    with_hidden = client.get(
        "/api/admin/users", params={"include_hidden": True}, headers=admin_headers
    ).json()["users"]
    assert len(with_hidden) == 1
    assert with_hidden[0]["hidden"] is True

    unhidden = client.post(
        f"/api/admin/users/{owner_id}/hidden", json={"hidden": False}, headers=admin_headers
    )
    assert unhidden.json() == {"saved": True}
    assert len(client.get("/api/admin/users", headers=admin_headers).json()["users"]) == 1


def test_hidden_route_is_gated_by_the_admin_code(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    assert client.post("/api/admin/users/a1/hidden", json={"hidden": True}).status_code == 401


# --- CSV export -----------------------------------------------------------------------------


def test_the_csv_export_has_a_header_row_and_the_visitors_data(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    admin_headers = {"X-Admin-Code": "staffonly"}
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})

    response = client.get("/api/admin/users/export.csv", headers=admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    body = response.text
    assert body.startswith("name,email,phone,neighborhood")
    assert "Javier Diaz" in body
    assert "javier@example.com" in body


def test_the_csv_export_excludes_hidden_visitors_by_default(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    admin_headers = {"X-Admin-Code": "staffonly"}
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    owner_id = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]["id"]
    client.post(f"/api/admin/users/{owner_id}/hidden", json={"hidden": True}, headers=admin_headers)

    default_export = client.get("/api/admin/users/export.csv", headers=admin_headers).text
    assert "Javier Diaz" not in default_export

    full_export = client.get(
        "/api/admin/users/export.csv", params={"include_hidden": True}, headers=admin_headers
    ).text
    assert "Javier Diaz" in full_export


def test_the_export_route_is_gated_by_the_admin_code(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    assert client.get("/api/admin/users/export.csv").status_code == 401


# --- the shared stylesheet --------------------------------------------------------------


def test_theme_css_is_served(settings, store):
    response = _client(settings, store).get("/theme.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "--accent" in response.text


def test_both_pages_link_the_shared_stylesheet():
    from pathlib import Path

    for name in ("index.html", "admin.html"):
        page = Path(f"markai/web/static/{name}").read_text(encoding="utf-8")
        assert 'href="/theme.css"' in page, f"{name} does not link the shared stylesheet"


# --- the Deal Analyzer calculator endpoint ------------------------------------------------


def test_calc_deal_matches_the_cli_calculator(settings, store):
    from markai.advisor.calculators import analyze_deal

    client = _client(settings, store)
    payload = {
        "price": 400000,
        "down_payment_pct": 0.25,
        "annual_rate": 0.065,
        "years": 30,
        "monthly_rent": 4000,
    }
    response = client.post("/api/calc/deal", json=payload)
    assert response.status_code == 200
    body = response.json()
    expected = analyze_deal(**payload)
    assert body == expected
    assert body["cap_rate"] > 0
    assert "dscr" in body


def test_calc_deal_rejects_a_missing_required_field(settings, store):
    client = _client(settings, store)
    response = client.post("/api/calc/deal", json={"price": 300000})
    assert response.status_code == 422, "monthly_rent is required"


def test_calc_deal_rejects_a_non_positive_price(settings, store):
    client = _client(settings, store)
    response = client.post("/api/calc/deal", json={"price": 0, "monthly_rent": 2000})
    assert response.status_code == 422


def test_calc_deal_is_gated_by_the_access_code(settings, store):
    settings = settings.model_copy(update={"web_access_code": "letmein"})
    client = _client(settings, store)
    response = client.post("/api/calc/deal", json={"price": 300000, "monthly_rent": 2500})
    assert response.status_code == 401


# --- the Notice Wizard endpoint -----------------------------------------------------------


_NOTICE_FACTS = """
ordinances:
  - id: chi-notice
    jurisdiction: Chicago
    topic: Five day notice for nonpayment
    rule: A five day notice may be served for nonpayment of rent.
    citation: RLTO 5-12-130
  - id: chi-renewal
    jurisdiction: Chicago
    topic: Lease renewal notice
    rule: 60 days notice for non-renewal, 120 if the tenant has lived there three years or longer.
    citation: Chicago Fair Notice Ordinance
  - id: il-general
    jurisdiction: Illinois
    topic: Notice to terminate month-to-month tenancy
    rule: Month-to-month rental agreements require thirty days notice before eviction can begin.
    citation: 735 ILCS 5/9-207
"""


def _with_notice_facts(settings, tmp_path):
    (tmp_path / "facts.yaml").write_text(_NOTICE_FACTS, encoding="utf-8")
    return settings


def test_notice_wizard_finds_a_chicago_rule(settings, store, tmp_path):
    settings = _with_notice_facts(settings, tmp_path)
    client = _client(settings, store)
    response = client.post(
        "/api/notice-wizard",
        json={"jurisdiction": "Chicago", "reason": "nonpayment", "tenure_years": None},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) > 0
    assert all(r["jurisdiction"].lower().find("chicago") != -1 for r in results)
    assert all(r["citation"] for r in results), "every result must still name its source"


def test_notice_wizard_never_answers_a_suburb_with_a_chicago_only_rule(settings, store, tmp_path):
    settings = _with_notice_facts(settings, tmp_path)
    client = _client(settings, store)
    response = client.post(
        "/api/notice-wizard",
        json={
            "jurisdiction": "Illinois (no local ordinance)",
            "reason": "no_cause_nonrenewal",
            "tenure_years": 5.0,
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results, "the DuPage/Illinois-general fact must surface"
    for r in results:
        jurisdiction = r["jurisdiction"].lower()
        assert "chicago" not in jurisdiction and "cook" not in jurisdiction


def test_notice_wizard_rejects_an_unknown_jurisdiction(settings, store):
    client = _client(settings, store)
    response = client.post(
        "/api/notice-wizard",
        json={"jurisdiction": "Narnia", "reason": "nonpayment"},
    )
    assert response.status_code == 400


def test_notice_wizard_rejects_an_unknown_reason(settings, store):
    client = _client(settings, store)
    response = client.post(
        "/api/notice-wizard",
        json={"jurisdiction": "Chicago", "reason": "because I feel like it"},
    )
    assert response.status_code == 400


def test_notice_wizard_is_gated_by_the_access_code(settings, store):
    settings = settings.model_copy(update={"web_access_code": "letmein"})
    client = _client(settings, store)
    response = client.post(
        "/api/notice-wizard", json={"jurisdiction": "Chicago", "reason": "nonpayment"}
    )
    assert response.status_code == 401


# --- the Deal Analyzer panel and conversation search (index.html) -------------------------


def test_closing_or_adding_a_log_entry_anywhere_refreshes_the_maintenance_tracker_too():
    """Marking an item done from the sidebar's generic log panel, or Jay logging one in
    chat, must not leave an already-open Maintenance Tracker showing a stale list - both
    panels read the same log_entries rows."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    start = page.index("function refreshLog()")
    end = page.index("\n  }", start)
    body = page[start:end]
    assert "loadMaintenance()" in body


def test_the_page_has_a_maintenance_tracker_panel():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="open-maintenance"' in page
    assert 'id="maintenance-card"' in page
    assert 'id="maintenance-what"' in page and 'id="maintenance-urgency"' in page
    assert "/api/maintenance" in page
    assert "/api/log" in page, "adding must write to the same log the sidebar panel uses"
    assert ".innerHTML" not in page.replace("never innerHTML", "")


def test_the_page_has_a_deal_analyzer_panel():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="open-deal"' in page
    assert 'id="deal-price"' in page and 'id="deal-rent"' in page
    assert "/api/calc/deal" in page
    assert "Estimates only. Confirm taxes, insurance and rents before you buy." in page
    assert ".innerHTML" not in page.replace("never innerHTML", "")


def test_every_deal_analyzer_field_has_a_visible_label():
    """A field pre-filled with a default (down %, rate, years, vacancy %...) shows a bare
    number once its placeholder is masked by that value - a screen-reader-only label
    doesn't help a sighted user tell what "25" or "7" means, so none of these can be
    `class="vh"` the way the rest of the app hides a label."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    start = page.index('<form id="deal-form">')
    end = page.index("</form>", start)
    form = page[start:end]
    assert form.count("<label") == form.count("<label for=")
    assert '<label class="vh"' not in form, "a pre-filled deal field needs an on-screen label"


def test_the_page_has_a_conversation_search_box():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="thread-search"' in page


def test_the_thread_delete_button_is_not_hover_only_on_touch():
    """:hover never fires on a touch device - a bare `opacity: 0` on the per-conversation
    delete button would leave it permanently invisible (not just tidied away) on a phone.
    It must only hide behind a (hover: hover) media query, not unconditionally."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    start = page.index(".thread .drop {")
    end = page.index("}", start)
    bare_rule = page[start:end]
    assert "opacity: 0" not in bare_rule, "the unconditional rule must not itself hide the button"
    assert "@media (hover: hover)" in page


def test_the_page_has_a_notice_wizard_panel():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="notice-card"' in page
    assert "/api/notice-wizard" in page
    assert "Chicago" in page and "Suburban Cook County" in page and "Evanston" in page
    assert ".innerHTML" not in page.replace("never innerHTML", "")


def test_the_notice_wizard_hides_citations_behind_a_click():
    """Straight from the owner: showing a source line on every result by default reads as
    a footnoted report, not a confident answer - it must be a click away, not on by default.

    Scoped to just the result-rendering function, not the whole inline script (which also
    contains the words "citation" and "source" throughout unrelated code) - otherwise this
    would pass even if a future change stopped hiding the citation by default.
    """
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    start = page.index("function noticeResultRow")
    end = page.index("\n  }", start)
    result_row_fn = page[start:end]
    assert "source.hidden = true" in result_row_fn, (
        "the citation/source element must start hidden, not be shown by default"
    )
    assert "source.hidden = !source.hidden" in result_row_fn, (
        "a toggle must be the only way to reveal it"
    )


def test_the_notice_wizard_footer_has_the_not_a_lawyer_disclaimer_and_an_escalation_link():
    """The owner caught the old footer ("Based on your own reviewed sources...") reading
    like a source citation was itself the legal cover - it needs a real not-a-lawyer
    disclaimer, and a path to a human (Russell) when a case is past what the tool answers.
    """
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    start = page.index('id="notice-card"')
    end = page.index("</script>")
    wizard_section = page[start:end]
    assert "not a lawyer" in wizard_section.lower()
    assert "legal advice" in wizard_section.lower()
    assert 'id="notice-escalation"' in wizard_section and "hidden" in wizard_section
    assert "/api/business" in wizard_section


def test_the_page_has_a_visible_tools_button_that_opens_the_command_palette():
    """The command palette existed with no visible entry point before this - a hidden
    Cmd/Ctrl+K shortcut is undiscoverable by definition."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert "⌘K" in page or "&#8984;K" in page or "Tools" in page
    assert 'id="palette-card"' in page


def test_the_sidebar_groups_its_tools_under_a_visible_label():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert ">Tools<" in page


def test_the_empty_state_lists_capabilities_without_reintroducing_clickable_chips():
    """The starter-question chips were removed for looking basic - these are informational
    labels, not buttons, and must not bring back a clickable chip."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    start_section = page[page.index('id="start"') : page.index("</section>")]
    assert 'class="chip"' not in start_section
    assert len(start_section) > 200, "should list more than just the original two lines"


# --- the admin panel toolbar and per-visitor actions (admin.html) -------------------------


def test_the_admin_page_has_search_filter_sort_and_hidden_toggle():
    from pathlib import Path

    page = Path("markai/web/static/admin.html").read_text(encoding="utf-8")
    assert 'id="search"' in page
    assert 'id="filter-signal"' in page
    assert 'id="filter-good-fit"' in page
    assert 'id="sort-by"' in page
    assert 'id="show-hidden"' in page
    assert 'id="export-csv"' in page
    assert ".innerHTML" not in page.replace("never innerHTML", "")


def test_the_admin_page_has_a_notes_textarea_in_the_detail_view():
    from pathlib import Path

    page = Path("markai/web/static/admin.html").read_text(encoding="utf-8")
    assert "notes-area" in page
    assert "/notes" in page


# --- AI classification and bulk hide (admin panel) -----------------------------------------


def test_analyze_stores_the_verdict_and_it_appears_in_the_listing(settings, store, monkeypatch):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store, FakeAdvisor("45 days in Chicago."))
    admin_headers = {"X-Admin-Code": "staffonly"}
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    _ask(client, "t1", "How long for a deposit?")

    monkeypatch.setattr(
        "markai.web.admin_store.classify_fit",
        lambda settings, threads, client=None: (True, "Sounds like a real prospect."),
    )
    owner_id = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]["id"]
    response = client.post(f"/api/admin/users/{owner_id}/analyze", headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == {"ai_good_fit": True, "ai_reasoning": "Sounds like a real prospect."}

    listed = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]
    assert listed["ai_good_fit"] is True
    assert listed["ai_reasoning"] == "Sounds like a real prospect."
    assert listed["good_fit"] is True

    detail = client.get(f"/api/admin/users/{owner_id}", headers=admin_headers).json()
    assert detail["ai_good_fit"] is True
    assert detail["ai_analyzed_at"] is not None


def test_analyze_reports_a_classification_failure_as_422(settings, store, monkeypatch):
    from markai.web.admin_store import ClassificationError

    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    admin_headers = {"X-Admin-Code": "staffonly"}
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})
    owner_id = client.get("/api/admin/users", headers=admin_headers).json()["users"][0]["id"]

    def boom(settings, threads, client=None):
        raise ClassificationError("There is no conversation to analyze yet.")

    monkeypatch.setattr("markai.web.admin_store.classify_fit", boom)
    response = client.post(f"/api/admin/users/{owner_id}/analyze", headers=admin_headers)
    assert response.status_code == 422
    assert "no conversation" in response.json()["detail"].lower()


def test_analyze_route_is_gated_by_the_admin_code(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    assert client.post("/api/admin/users/a1/analyze").status_code == 401


def test_bulk_hidden_hides_several_visitors_at_once(settings, store):
    """Two genuinely separate visitors - a signed-in TestClient carries its cookie on every
    request regardless of X-Browser-Id, so the second one needs its own client instance,
    the same way a second physical device would have its own cookie jar."""
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    app_under_test = create_app(settings, advisor=FakeAdvisor(), store=store)
    client = TestClient(app_under_test)
    admin_headers = {"X-Admin-Code": "staffonly"}
    client.post("/api/account", json=SIGNUP, headers={"X-Browser-Id": "b1"})

    other_device = TestClient(app_under_test)
    _ask(other_device, "t1", "A question", browser="b2")

    users = client.get("/api/admin/users", headers=admin_headers).json()["users"]
    ids = [u["id"] for u in users]
    assert len(ids) == 2

    response = client.post(
        "/api/admin/users/bulk-hidden",
        json={"owner_ids": ids, "hidden": True},
        headers=admin_headers,
    )
    assert response.json() == {"saved": True, "count": 2}
    assert client.get("/api/admin/users", headers=admin_headers).json()["users"] == []
    restored = client.get(
        "/api/admin/users", params={"include_hidden": True}, headers=admin_headers
    ).json()["users"]
    assert len(restored) == 2
    assert all(u["hidden"] for u in restored)


def test_bulk_hidden_route_is_gated_by_the_admin_code(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    response = client.post(
        "/api/admin/users/bulk-hidden", json={"owner_ids": ["a1"], "hidden": True}
    )
    assert response.status_code == 401


# --- proactive nudges -----------------------------------------------------------------------


def test_nudges_shows_the_heat_season_reminder_in_season(settings, store, monkeypatch):
    import datetime as datetime_module

    class _FixedDate(datetime_module.date):
        @classmethod
        def today(cls):
            return cls(2026, 1, 15)

    monkeypatch.setattr("markai.web.nudges.date", _FixedDate)
    client = _client(settings, store)
    nudges = client.get("/api/nudges").json()["nudges"]
    assert any(n["id"] == "heat-season" for n in nudges)


def test_nudges_is_empty_outside_heat_season(settings, store, monkeypatch):
    import datetime as datetime_module

    class _FixedDate(datetime_module.date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 4)

    monkeypatch.setattr("markai.web.nudges.date", _FixedDate)
    client = _client(settings, store)
    assert client.get("/api/nudges").json()["nudges"] == []


def test_nudges_is_gated_by_the_access_code(settings, store):
    settings = settings.model_copy(update={"web_access_code": "letmein"})
    client = _client(settings, store)
    assert client.get("/api/nudges").status_code == 401


# --- mark digest send ---------------------------------------------------------------------


def _digest_dirs(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("websites: []\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARKAI_SOURCES_FILE", str(manifest))
    monkeypatch.setenv("MARKAI_DATA_DIR", str(data_dir))
    return data_dir


def test_digest_send_says_so_when_nobody_has_anything_to_report(tmp_path, monkeypatch):
    data_dir = _digest_dirs(tmp_path, monkeypatch)
    data_dir.mkdir(parents=True)
    from markai.web.accounts import Accounts

    accounts = Accounts(data_dir / "accounts.db", free_questions=2)
    accounts.create(SIGNUP)
    accounts.close()

    result = runner.invoke(app, ["digest", "send", "--dry-run"])
    assert result.exit_code == 0
    assert "Nothing to send" in result.stdout


def test_digest_send_dry_run_lists_who_would_get_one(tmp_path, monkeypatch):
    data_dir = _digest_dirs(tmp_path, monkeypatch)
    data_dir.mkdir(parents=True)
    from markai.web.accounts import Accounts
    from markai.web.ledger import Ledger

    accounts = Accounts(data_dir / "accounts.db", free_questions=2)
    account, _token = accounts.create(SIGNUP)
    accounts.close()
    ledger = Ledger(data_dir / "ledger.db")
    ledger.add(account.owner_id, {"kind": "income", "what": "September rent", "amount": 4200})
    ledger.close_db()

    result = runner.invoke(app, ["digest", "send", "--dry-run"])
    assert result.exit_code == 0
    assert "javier@example.com" in result.stdout
    assert "Dry run" in result.stdout


def test_digest_send_refuses_with_no_smtp_host(tmp_path, monkeypatch):
    data_dir = _digest_dirs(tmp_path, monkeypatch)
    data_dir.mkdir(parents=True)
    from markai.web.accounts import Accounts
    from markai.web.ledger import Ledger

    accounts = Accounts(data_dir / "accounts.db", free_questions=2)
    account, _token = accounts.create(SIGNUP)
    accounts.close()
    ledger = Ledger(data_dir / "ledger.db")
    ledger.add(account.owner_id, {"kind": "income", "what": "Rent", "amount": 100})
    ledger.close_db()

    result = runner.invoke(app, ["digest", "send", "--yes"])
    assert result.exit_code == 1
    assert "MARKAI_SMTP_HOST" in result.stdout + str(result.stderr)


def test_digest_send_emails_everyone_who_has_something_to_report(tmp_path, monkeypatch):
    data_dir = _digest_dirs(tmp_path, monkeypatch)
    data_dir.mkdir(parents=True)
    from markai.web.accounts import Accounts
    from markai.web.ledger import Ledger

    accounts = Accounts(data_dir / "accounts.db", free_questions=2)
    account, _token = accounts.create(SIGNUP)
    accounts.close()
    ledger = Ledger(data_dir / "ledger.db")
    ledger.add(account.owner_id, {"kind": "income", "what": "September rent", "amount": 4200})
    ledger.close_db()

    monkeypatch.setenv("MARKAI_SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("MARKAI_SMTP_USERNAME", "jay@gcrealtyinc.com")

    sent = {"messages": []}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"], sent["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent["starttls"] = True

        def login(self, username, password):
            sent["login"] = username

        def send_message(self, message):
            sent["messages"].append(message)

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    result = runner.invoke(app, ["digest", "send", "--yes"])
    assert result.exit_code == 0
    assert "Sent 1, 0 failed" in result.stdout
    assert len(sent["messages"]) == 1
    assert sent["messages"][0]["To"] == "javier@example.com"


def test_digest_send_needs_confirmation_without_yes_or_dry_run(tmp_path, monkeypatch):
    data_dir = _digest_dirs(tmp_path, monkeypatch)
    data_dir.mkdir(parents=True)
    from markai.web.accounts import Accounts
    from markai.web.ledger import Ledger

    accounts = Accounts(data_dir / "accounts.db", free_questions=2)
    account, _token = accounts.create(SIGNUP)
    accounts.close()
    ledger = Ledger(data_dir / "ledger.db")
    ledger.add(account.owner_id, {"kind": "income", "what": "Rent", "amount": 100})
    ledger.close_db()

    monkeypatch.setenv("MARKAI_SMTP_HOST", "smtp.gmail.com")
    result = runner.invoke(app, ["digest", "send"], input="n\n")
    assert result.exit_code == 0
    assert "Nothing sent" in result.stdout


# --- Phase 2/3: nudges, save-to-property, property switcher, palette, voice, regenerate,
# onboarding (index.html) ------------------------------------------------------------------


def test_the_page_has_a_nudge_container():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="nudges"' in page
    assert "/api/nudges" in page


def test_the_page_can_save_a_deal_result_to_a_property():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert "dealSaveControl" in page
    assert "/api/log" in page


def test_the_page_has_a_property_context_switcher():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="property-context"' in page
    assert "Regarding " in page


def test_the_page_has_a_command_palette():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="palette-card"' in page
    assert 'id="palette-input"' in page


def test_the_page_has_voice_input_that_feature_detects():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="mic"' in page
    assert "SpeechRecognition" in page


def test_the_page_has_a_regenerate_action_and_an_onboarding_tour():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert "Regenerate" in page
    assert 'id="tour-card"' in page
    assert "jay_onboarded" in page


def test_the_page_still_has_no_innerhtml_after_the_big_rewrite():
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert ".innerHTML" not in page.replace("never innerHTML", "")
    assert "insertAdjacentHTML" not in page


# --- admin.html Phase 2: AI classify + bulk select --------------------------------------


def test_the_admin_page_has_an_ai_analyze_control():
    from pathlib import Path

    page = Path("markai/web/static/admin.html").read_text(encoding="utf-8")
    assert "/analyze" in page
    assert "analyzeUser" in page


def test_the_admin_page_has_bulk_select_and_bulk_hide():
    from pathlib import Path

    page = Path("markai/web/static/admin.html").read_text(encoding="utf-8")
    assert 'id="bulk-bar"' in page
    assert "bulk-hidden" in page
    assert ".innerHTML" not in page.replace("never innerHTML", "")


def test_the_admin_detail_card_scrolls_instead_of_running_off_the_screen():
    """A visitor with a long conversation used to overflow the modal past the bottom of
    the screen with no way to reach the rest of it, or the close button."""
    from pathlib import Path

    theme = Path("markai/web/static/theme.css").read_text(encoding="utf-8")
    card_rule = theme[theme.index(".card {") : theme.index("}", theme.index(".card {"))]
    assert "overflow-y: auto" in card_rule
    assert "max-height" in card_rule


def test_each_conversation_in_the_detail_card_is_collapsed_until_clicked():
    """Every message from every thread used to render at once - now each conversation is
    a collapsed <details> the staff member opens by clicking its title."""
    from pathlib import Path

    page = Path("markai/web/static/admin.html").read_text(encoding="utf-8")
    assert 'el("details", "thread")' in page
    assert 'el("summary", "thread-title"' in page


# --- CSV export formula-injection guard ----------------------------------------------------


def test_csv_export_defuses_a_formula_looking_name(settings, store):
    settings = settings.model_copy(update={"admin_access_code": "staffonly"})
    client = _client(settings, store)
    admin_headers = {"X-Admin-Code": "staffonly"}
    client.post(
        "/api/account",
        json={
            "name": '=HYPERLINK("http://evil.test","x")',
            "email": "javier@example.com",
            "phone": "312-555-0134",
            "neighborhood": "Logan Square",
        },
        headers={"X-Browser-Id": "b1"},
    )
    body = client.get("/api/admin/users/export.csv", headers=admin_headers).text
    assert "'=HYPERLINK" in body, "a leading apostrophe defuses it as a formula in Excel/Sheets"
    assert ',"=HYPERLINK' not in body, "the raw formula must never reach a cell unescaped"


def test_csv_safe_leaves_ordinary_text_untouched():
    from markai.web.app import _csv_safe

    assert _csv_safe("Javier Diaz") == "Javier Diaz"
    assert _csv_safe("") == ""
    assert _csv_safe(False) == "False"


def test_csv_safe_defuses_every_dangerous_leading_character():
    from markai.web.app import _csv_safe

    for prefix in ("=", "+", "-", "@"):
        assert _csv_safe(prefix + "cmd|calc").startswith("'" + prefix)


# --- regenerate must resend the question that produced THAT answer, not the latest one -----


def test_regenerate_reads_the_question_off_its_own_slot_not_a_shared_variable():
    """With two answers on screen, clicking Regenerate on the older one used to silently
    replace it with the reply to the newer question instead - a global "last question"
    variable, read by every Regenerate button regardless of which slot it belongs to. The
    fix keeps the question on the slot object itself."""
    from pathlib import Path

    page = Path("markai/web/static/index.html").read_text(encoding="utf-8")
    assert "lastSentQuestion" not in page and "lastRawQuestion" not in page
    assert "slot.apiMessage = apiMessage" in page
    assert "slot.rawQuestion = question" in page
    regenerate_body = page[
        page.index("function regenerate(slot)") : page.index(
            "function ", page.index("function regenerate(slot)") + 10
        )
    ]
    assert "slot.apiMessage" in regenerate_body
    assert "slot.rawQuestion" in regenerate_body


# --- the weekly YouTube ingest webhook ----------------------------------------------------


def test_the_youtube_ingest_webhook_requires_the_token(settings, store):
    client = _client(settings, store)
    assert client.post("/internal/ingest-youtube").status_code == 401


def test_the_youtube_ingest_webhook_rejects_the_wrong_token(settings, store):
    settings = settings.model_copy(update={"ingest_webhook_token": "right-token"})
    client = _client(settings, store)
    resp = client.post("/internal/ingest-youtube", headers={"X-Ingest-Token": "wrong"})
    assert resp.status_code == 401


def test_the_youtube_ingest_webhook_starts_a_youtube_only_background_ingest(
    settings, store, monkeypatch
):
    import threading as threading_module

    import markai.ingest.pipeline as pipeline
    from markai.models import SourceKind

    calls = []

    def fake_run_ingest(manifest, store_arg, embedder, settings_arg, **kwargs):
        calls.append(kwargs)
        return pipeline.IngestReport()

    monkeypatch.setattr(pipeline, "run_ingest", fake_run_ingest)

    class ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading_module, "Thread", ImmediateThread)

    settings = settings.model_copy(update={"ingest_webhook_token": "right-token"})
    settings.sources_file.write_text("websites: []\n", encoding="utf-8")
    client = _client(settings, store)
    resp = client.post("/internal/ingest-youtube", headers={"X-Ingest-Token": "right-token"})
    assert resp.status_code == 200
    assert resp.json() == {"started": True}
    assert len(calls) == 1
    assert calls[0]["only"] == {SourceKind.YOUTUBE}
    assert calls[0]["allow_transcription"] is False


def test_the_youtube_ingest_webhook_refuses_a_second_run_while_one_is_in_progress(
    settings, store, monkeypatch
):
    """A cron job that fires twice (a retry, a manual trigger during the weekly run) must
    not start a second ingest stepping on the first one's store connection."""
    import threading as threading_module

    import markai.ingest.pipeline as pipeline

    started = threading_module.Event()
    finish = threading_module.Event()

    def slow_run_ingest(manifest, store_arg, embedder, settings_arg, **kwargs):
        started.set()
        finish.wait(timeout=5)
        return pipeline.IngestReport()

    monkeypatch.setattr(pipeline, "run_ingest", slow_run_ingest)

    settings = settings.model_copy(update={"ingest_webhook_token": "right-token"})
    settings.sources_file.write_text("websites: []\n", encoding="utf-8")
    client = _client(settings, store)
    headers = {"X-Ingest-Token": "right-token"}

    first = client.post("/internal/ingest-youtube", headers=headers)
    assert first.status_code == 200
    assert first.json() == {"started": True}
    assert started.wait(timeout=5), "the background thread never started"

    second = client.post("/internal/ingest-youtube", headers=headers)
    assert second.status_code == 200
    assert second.json()["started"] is False

    finish.set()
