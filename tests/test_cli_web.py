"""The command line and the web API."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from markai.cli import app
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
    assert "Chicagoland landlord advisor" in response.text


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
