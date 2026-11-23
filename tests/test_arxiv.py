"""Tests for arXiv input: ID parsing, the MCP lookup, the MCP event log and
the endpoints.

The MCP server and arxiv.org are replaced with fakes, so no network or
subprocess is used.
"""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import api.routes.convert as convert_route
import sources.arxiv as arxiv
from api.main import get_llm_provider
from sources.arxiv import (
    ArxivError,
    ArxivPaper,
    ArxivSettings,
    fetch_arxiv_paper,
    load_arxiv_settings,
    parse_arxiv_ref,
)
from sources.mcp_log import MCP_LOG, McpLog
from tests.test_notebook import FakeProvider
from util.config import ConfigError

PDF = b"%PDF-1.7 arxiv paper"
ABSTRACT = {
    "status": "success",
    "paper_id": "1706.03762",
    "title": "Attention Is All You Need",
    "authors": ["A. Vaswani", "N. Shazeer"],
    "abstract": "The dominant sequence transduction models…",
    "published": "2017-06-12T17:57:34Z",
    "pdf_url": "http://arxiv.org/pdf/1706.03762v7",
}


# ---- fakes -----------------------------------------------------------------


class FakeSession:
    """Stands in for mcp.ClientSession."""

    def __init__(
        self,
        reply=None,
        *,
        tools=("get_abstract",),
        error=False,
        raw=None,
        delay=0.0,
    ):
        self.reply = ABSTRACT if reply is None else reply
        self.tools = tools
        self.error = error
        self.raw = raw
        self.delay = delay
        self.calls = []

    async def initialize(self):
        return SimpleNamespace(
            serverInfo=SimpleNamespace(
                name="arxiv-mcp-server", version="0.8.2"
            )
        )

    async def list_tools(self):
        return SimpleNamespace(
            tools=[SimpleNamespace(name=name) for name in self.tools]
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        text = self.raw if self.raw is not None else json.dumps(self.reply)
        return SimpleNamespace(
            isError=self.error,
            content=[SimpleNamespace(type="text", text=text)],
        )


def factory(session):
    @asynccontextmanager
    async def open_session():
        yield session

    return open_session


def http(status=200, body=PDF, error=None):
    def handler(request):
        if error is not None:
            raise error
        return httpx.Response(status, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def fetch(ref="1706.03762", session=None, client=None, **kwargs):
    log = kwargs.pop("log", McpLog())
    return log, asyncio.run(
        fetch_arxiv_paper(
            ref,
            settings=kwargs.pop("settings", ArxivSettings()),
            log=log,
            session_factory=factory(session or FakeSession()),
            http_client=client or http(),
            **kwargs,
        )
    )


def fetch_error(**kwargs):
    log = kwargs.setdefault("log", McpLog())
    with pytest.raises(ArxivError) as info:
        fetch(**kwargs)
    return info.value, log


# ---- parsing ----------------------------------------------------------------


class TestParseRef:
    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            ("1706.03762", "1706.03762"),
            ("  1706.03762v7 ", "1706.03762v7"),
            ("arXiv:2401.12345", "2401.12345"),
            ("https://arxiv.org/abs/1706.03762", "1706.03762"),
            ("https://arxiv.org/pdf/1706.03762v7.pdf", "1706.03762v7"),
            ("arxiv.org/pdf/1706.03762", "1706.03762"),
            ("http://export.arxiv.org/abs/1706.03762/", "1706.03762"),
            ("https://www.arxiv.org/html/2401.12345v2", "2401.12345v2"),
            ("hep-th/9901001", "hep-th/9901001"),
            ("https://arxiv.org/abs/math.GT/0309136", "math.GT/0309136"),
        ],
    )
    def test_accepts(self, ref, expected):
        assert parse_arxiv_ref(ref) == expected

    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "attention is all you need",
            "1706.037",
            "https://example.com/abs/1706.03762",
            "https://arxiv.org/list/cs.LG/recent",
            "https://evil.example/arxiv.org/abs/1706.03762",
        ],
    )
    def test_rejects(self, ref):
        with pytest.raises(ArxivError) as info:
            parse_arxiv_ref(ref)
        assert info.value.status_code == 400


# ---- settings ---------------------------------------------------------------


class TestSettings:
    def test_defaults_from_config_toml(self):
        settings = load_arxiv_settings()
        assert settings.command == ("{python}", "-m", "arxiv_mcp_server")
        assert settings.timeout == 60
        assert settings.argv()[0] == sys.executable

    def test_missing_section_uses_defaults(self):
        assert load_arxiv_settings({}) == ArxivSettings()

    @pytest.mark.parametrize(
        "section",
        [
            {"command": "python -m arxiv_mcp_server"},
            {"command": []},
            {"command": ["python", ""]},
            {"timeout": "60"},
            {"timeout": True},
            {"timeout": 0},
        ],
    )
    def test_invalid(self, section):
        with pytest.raises(ConfigError):
            load_arxiv_settings({"arxiv": section})


class TestSafePdfUrl:
    def test_arxiv_url_upgraded_to_https(self):
        assert (
            arxiv._safe_pdf_url("http://arxiv.org/pdf/1706.03762v7", "x")
            == "https://arxiv.org/pdf/1706.03762v7"
        )

    @pytest.mark.parametrize(
        "url", [None, 42, "https://evil.example/x.pdf", "file:///etc/passwd"]
    )
    def test_other_urls_fall_back_to_arxiv(self, url):
        assert (
            arxiv._safe_pdf_url(url, "1706.03762")
            == "https://arxiv.org/pdf/1706.03762"
        )


# ---- event log --------------------------------------------------------------


class TestMcpLog:
    def test_since_returns_newer_events(self):
        log = McpLog()
        first = log.add("server", "start", run="r")
        log.add(
            "call",
            "get_abstract(...)",
            tool="get_abstract",
            arguments={"paper_id": "1"},
            duration_ms=12.345,
        )
        page = log.since(first["id"])
        assert page["last_id"] == 2
        assert [e["kind"] for e in page["events"]] == ["call"]
        assert page["events"][0]["duration_ms"] == 12.3

    def test_bounded_and_clipped(self):
        log = McpLog(max_events=3)
        for i in range(5):
            log.add("server", f"event {i}")
        log.add("result", "x" * 1000, detail="y" * 1000)
        events = log.since()["events"]
        assert len(events) == 3
        assert len(events[-1]["message"]) == 400
        assert events[-1]["detail"].endswith("…")

    def test_unknown_kind(self):
        with pytest.raises(ValueError):
            McpLog().add("other", "x")

    def test_clear_keeps_ids_increasing(self):
        log = McpLog()
        log.add("server", "a")
        log.clear()
        assert log.since()["events"] == []
        assert log.add("server", "b")["id"] == 2


# ---- lookup -----------------------------------------------------------------


class TestFetch:
    def test_success(self):
        notes = []
        session = FakeSession()
        log, paper = fetch(
            "https://arxiv.org/abs/1706.03762",
            session=session,
            notify=notes.append,
        )
        assert paper.pdf == PDF
        assert paper.title == "Attention Is All You Need"
        assert paper.pdf_url == "https://arxiv.org/pdf/1706.03762v7"
        assert paper.source_name == (
            "arXiv 1706.03762 Attention Is All You Need.pdf"
        )
        assert paper.describe()["abs_url"] == (
            "https://arxiv.org/abs/1706.03762"
        )
        assert session.calls == [("get_abstract", {"paper_id": "1706.03762"})]
        kinds = [e["kind"] for e in log.since()["events"]]
        assert kinds == [
            "server",
            "server",
            "server",
            "call",
            "result",
            "server",
            "http",
        ]
        assert all(
            e["run"] == "arXiv 1706.03762" for e in log.since()["events"]
        )
        assert len(notes) == 2

    @pytest.mark.parametrize(
        ("session", "status", "text"),
        [
            (
                FakeSession(
                    {
                        "status": "error",
                        "message": "Paper 1 not found on arXiv",
                    }
                ),
                404,
                "not found",
            ),
            (
                FakeSession({"status": "error", "message": "rate limited"}),
                502,
                "rate limited",
            ),
            (FakeSession(error=True, raw="boom"), 502, "reported an error"),
            (FakeSession(raw="not json"), 502, "not JSON"),
            (FakeSession(raw="[1, 2]"), 502, "unexpected reply"),
            (FakeSession({**ABSTRACT, "title": " "}), 502, "no title"),
            (FakeSession(tools=("search_papers",)), 502, "no get_abstract"),
        ],
    )
    def test_lookup_errors(self, session, status, text):
        error, log = fetch_error(session=session)
        assert error.status_code == status
        assert text in str(error)
        assert log.since()["events"][-1]["kind"] == "error"

    def test_server_fails_to_start(self):
        @asynccontextmanager
        async def broken():
            raise OSError("no such file: arxiv_mcp_server")
            yield  # pragma: no cover

        log = McpLog()
        with pytest.raises(ArxivError, match="Could not use") as info:
            asyncio.run(
                fetch_arxiv_paper(
                    "1706.03762",
                    settings=ArxivSettings(),
                    log=log,
                    session_factory=broken,
                )
            )
        assert isinstance(info.value.__cause__, OSError)

    def test_error_inside_exception_group_is_unwrapped(self):
        @asynccontextmanager
        async def grouped():
            raise ExceptionGroup("tasks", [ArxivError("inner", 404)])
            yield  # pragma: no cover

        with pytest.raises(ArxivError, match="inner") as info:
            asyncio.run(
                fetch_arxiv_paper(
                    "1706.03762",
                    settings=ArxivSettings(),
                    log=McpLog(),
                    session_factory=grouped,
                )
            )
        assert info.value.status_code == 404

    def test_timeout(self):
        error, _ = fetch_error(
            session=FakeSession(delay=1),
            settings=ArxivSettings(timeout=0.05),
        )
        assert error.status_code == 504

    @pytest.mark.parametrize(
        ("client", "status", "text"),
        [
            (http(status=404), 404, "HTTP 404"),
            (http(status=500), 502, "HTTP 500"),
            (http(body=b"<html>"), 502, "did not return a PDF"),
            (
                http(error=httpx.ConnectError("down")),
                502,
                "Could not download",
            ),
        ],
    )
    def test_download_errors(self, client, status, text):
        error, log = fetch_error(client=client)
        assert error.status_code == status
        assert text in str(error)
        assert log.since()["events"][-1]["kind"] == "error"

    def test_pdf_too_large(self):
        error, _ = fetch_error(client=http(body=PDF * 10), max_bytes=50)
        assert error.status_code == 400

    def test_default_http_client(self, monkeypatch):
        real = httpx.AsyncClient

        def client(**kwargs):
            assert kwargs["headers"]["User-Agent"] == arxiv.USER_AGENT
            return real(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=PDF)
                )
            )

        monkeypatch.setattr(arxiv.httpx, "AsyncClient", client)
        paper = asyncio.run(
            fetch_arxiv_paper(
                "1706.03762",
                settings=ArxivSettings(),
                log=McpLog(),
                session_factory=factory(FakeSession()),
            )
        )
        assert paper.pdf == PDF


# ---- endpoints --------------------------------------------------------------


def _paper():
    return ArxivPaper(
        paper_id="1706.03762",
        title="Attention Is All You Need",
        authors=("A. Vaswani",),
        abstract="",
        published=None,
        pdf_url="https://arxiv.org/pdf/1706.03762",
        pdf=PDF,
    )


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def fake_fetch(ref, *, max_bytes, notify=None):
        calls.append(ref)
        if notify:
            notify("Looking up…")
        if ref == "9999.99999":
            raise ArxivError("arXiv lookup failed: not found", 404)
        return _paper()

    monkeypatch.setattr(convert_route, "fetch_arxiv_paper", fake_fetch)
    api_main.app.dependency_overrides[get_llm_provider] = FakeProvider
    try:
        test_client = TestClient(api_main.app)
        test_client.fetch_calls = calls
        yield test_client
    finally:
        api_main.app.dependency_overrides.clear()


class TestEndpoints:
    def test_convert_from_arxiv_link(self, client):
        response = client.post(
            "/convert", data={"arxiv": "https://arxiv.org/abs/1706.03762"}
        )
        assert response.status_code == 200
        assert client.fetch_calls == ["1706.03762"]
        assert (
            "Attention Is All You Need"
            in (response.headers["content-disposition"])
        )

    def test_convert_arxiv_not_found(self, client):
        response = client.post("/convert", data={"arxiv": "9999.99999"})
        assert response.status_code == 404

    def test_file_and_link_together_is_400(self, client):
        response = client.post(
            "/convert",
            data={"arxiv": "1706.03762"},
            files={"file": ("p.pdf", PDF, "application/pdf")},
        )
        assert response.status_code == 400
        assert "not both" in response.json()["detail"]

    def test_bad_link_is_400_before_any_lookup(self, client):
        response = client.post("/convert/stream", data={"arxiv": "hello"})
        assert response.status_code == 400
        assert client.fetch_calls == []

    def _events(self, response):
        events = []
        for block in response.text.strip().split("\n\n"):
            lines = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append((lines["event"], json.loads(lines["data"])))
        return events

    def test_stream_from_arxiv(self, client):
        response = client.post(
            "/convert/stream", data={"arxiv": "arXiv:1706.03762"}
        )
        events = self._events(response)
        assert events[0][0] == "progress"
        assert events[0][1]["stage"] == "fetching"
        name, result = events[-1]
        assert name == "result"
        assert result["source"]["type"] == "arxiv"
        assert result["source"]["paper_id"] == "1706.03762"
        assert result["upload_bytes"] == len(PDF)

    def test_stream_upload_source(self, client):
        response = client.post(
            "/convert/stream",
            files={"file": ("p.pdf", PDF, "application/pdf")},
        )
        result = self._events(response)[-1][1]
        assert result["source"] == {"type": "upload", "filename": "p.pdf"}

    def test_stream_arxiv_failure_is_error_event(self, client):
        response = client.post("/convert/stream", data={"arxiv": "9999.99999"})
        name, data = self._events(response)[-1]
        assert name == "error"
        assert data["status"] == 404

    def test_mcp_events_endpoint(self, client):
        MCP_LOG.clear()
        event = MCP_LOG.add("call", "get_abstract({})", tool="get_abstract")
        body = client.get(f"/api/mcp/events?after={event['id'] - 1}").json()
        assert [e["id"] for e in body["events"]] == [event["id"]]
        assert body["last_id"] == event["id"]
        assert client.get("/api/mcp/events?after=-1").status_code == 422

    def test_static_files_are_revalidated(self, client):
        # So an updated dashboard never mixes new HTML with cached CSS.
        response = client.get("/static/base.css")
        assert response.headers["cache-control"] == "no-cache"
        assert '"output output mcp"' in response.text
        again = client.get(
            "/static/base.css",
            headers={"If-None-Match": response.headers["etag"]},
        )
        assert again.status_code == 304

    def test_page_has_mcp_panel_and_link_field(self, client):
        page = client.get("/").text
        assert 'id="mcp-panel"' in page
        assert 'id="arxiv"' in page
        assert "Upload paper / specify link" in page
