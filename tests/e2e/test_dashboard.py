"""End-to-end tests: the real dashboard in headless Chrome.

Run them with ``./e2e.sh`` (or ``uv run pytest -m e2e``); they are left out
of ``./check.sh`` and CI.

- The dashboard tests use a scripted model: no key, no cost.
- The two conversion tests use the real provider named by ``PROVIDER``,
  with its key from the environment (``./e2e.sh`` asks for both), and the
  example paper ``example/non-linear activation functions.pdf``. Without a
  provider they are skipped. Each prints its token count and cost.
- For the arXiv link, the MCP server and the PDF download are faked: the
  lookup returns the same example paper, so arXiv is never contacted.

Uses the installed Google Chrome, or Playwright's Chromium (``uv run
playwright install chromium``); without either the tests are skipped.
"""

import json
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

import api.main as api_main
import api.routes.convert as convert_route
import sources.arxiv as arxiv
from providers import LLMProvider, ProviderError, ProviderSettings
from providers.base import Progress
from providers.factory import get_provider
from tests.test_arxiv import FakeSession
from util.config import ConfigError

pytestmark = pytest.mark.e2e

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

EXAMPLE_PDF = (
    Path(__file__).resolve().parents[2]
    / "example"
    / "non-linear activation functions.pdf"
)
# The arXiv ID typed into the link field; the fake lookup answers it with
# the example paper.
FAKE_ARXIV_ID = "2401.00001"

# A real model takes a minute or two.
CONVERSION_TIMEOUT_MS = 5 * 60 * 1000

NOTEBOOK = {
    "title": "Demo Paper",
    "pip_packages": [],
    "requires_gpu": False,
    "cells": [
        {"cell_type": "markdown", "source": "# Demo Paper"},
        {"cell_type": "code", "source": "print(1)"},
    ],
}

# Collects Content-Security-Policy violations on every page.
CSP_PROBE = """
window.__csp = [];
document.addEventListener("securitypolicyviolation",
  (e) => window.__csp.push(e.violatedDirective + " " + e.blockedURI));
"""


class ScriptedModel(LLMProvider):
    """Returns a fixed notebook: the dashboard tests need no real model."""

    def generate_json(
        self, prompt, *, schema, system=None, pdf=None, on_progress=None
    ):
        if on_progress:
            on_progress(
                Progress(
                    "done",
                    "Done.",
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                )
            )
        return NOTEBOOK


# ---- the live server -------------------------------------------------------


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def start_server():
    """Start the real app on a free 127.0.0.1 port; returns its URL."""
    servers = []

    def start():
        port = _free_port()
        live = uvicorn.Server(
            uvicorn.Config(
                api_main.app, host="127.0.0.1", port=port, log_level="warning"
            )
        )
        thread = threading.Thread(target=live.run, daemon=True)
        thread.start()
        servers.append((live, thread))
        deadline = time.monotonic() + 10
        while not live.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)
        return f"http://127.0.0.1:{port}"

    yield start
    for live, thread in servers:
        live.should_exit = True
        thread.join(timeout=10)
    api_main.app.dependency_overrides.clear()


@pytest.fixture
def scripted_server(start_server, monkeypatch):
    """The app with the scripted model.

    A placeholder key makes the configuration complete, so the dashboard
    opens on the Upload tab instead of flagging a missing key.
    """
    monkeypatch.setenv("PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-e2e-not-a-real-key")
    api_main.app.dependency_overrides[api_main.get_llm_provider] = lambda: (
        ScriptedModel(ProviderSettings(name="demo", model="demo-model"))
    )
    return start_server()


@pytest.fixture
def real_server(start_server):
    """The app with the real provider from the environment.

    Skips the test when no provider or key is set (``./e2e.sh`` asks).
    """
    try:
        get_provider()
    except (ConfigError, ProviderError) as exc:
        pytest.skip(f"no real provider; run ./e2e.sh to choose one ({exc})")
    return start_server()


@pytest.fixture
def fake_arxiv(monkeypatch):
    """Answer arXiv lookups with the example paper, without the network.

    Only the MCP server and the download are replaced; the real lookup
    code runs, so the MCP log shows each step.
    """
    reply = {
        "status": "success",
        "paper_id": FAKE_ARXIV_ID,
        "title": "Non-linear activation functions (example paper)",
        "authors": ["Example Author"],
        "abstract": "The example paper from example/, served as arXiv.",
        "published": "2024-01-01T00:00:00Z",
        "pdf_url": f"https://arxiv.org/pdf/{FAKE_ARXIV_ID}",
    }
    pdf = EXAMPLE_PDF.read_bytes()
    real_fetch = arxiv.fetch_arxiv_paper

    @asynccontextmanager
    async def session():
        yield FakeSession(reply)

    async def fetch(ref, **kwargs):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=pdf)
        )
        return await real_fetch(
            ref,
            session_factory=session,
            http_client=httpx.AsyncClient(transport=transport),
            **kwargs,
        )

    monkeypatch.setattr(convert_route, "fetch_arxiv_paper", fetch)


# ---- the browser -----------------------------------------------------------


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        launched = None
        for options in ({"channel": "chrome"}, {}):
            try:
                launched = playwright.chromium.launch(**options)
                break
            except sync_api.Error:
                continue
        if launched is None:
            pytest.skip(
                "No Chrome or Chromium for Playwright; install Google Chrome"
                " or run `uv run playwright install chromium`."
            )
        yield launched
        launched.close()


def _open(browser, url):
    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, accept_downloads=True
    )
    context.add_init_script(CSP_PROBE)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url + "/")
    page.wait_for_load_state("networkidle")
    return context, page, errors


@pytest.fixture
def page(browser, scripted_server):
    context, page, errors = _open(browser, scripted_server)
    yield page
    assert errors == [], errors
    context.close()


@pytest.fixture
def real_page(browser, real_server):
    context, page, errors = _open(browser, real_server)
    yield page
    assert errors == [], errors
    context.close()


def _fits_one_screen(page):
    return page.evaluate(
        "document.documentElement.scrollHeight <= innerHeight"
    )


# ---- dashboard tests (scripted model, free) --------------------------------


def test_dashboard_loads_under_the_csp(page):
    assert page.title()
    expect(page.locator("#tab-upload")).to_have_attribute(
        "aria-selected", "true"
    )
    assert page.locator("#submit").is_disabled()
    assert page.evaluate("window.__csp") == []
    assert _fits_one_screen(page)


def test_tabs_switch_panels(page):
    page.click("#tab-config")
    expect(page.locator("#tab-config")).to_have_attribute(
        "aria-selected", "true"
    )
    page.click("#tab-download")
    assert page.locator("#download-panel").is_visible()
    assert page.locator("#files-empty").is_visible()
    page.click("#tab-metrics")
    expect(page.locator("#tab-metrics")).to_have_attribute(
        "aria-selected", "true"
    )
    assert _fits_one_screen(page)


@pytest.mark.security
def test_scripted_conversion_passes_the_guards(page):
    """The same-origin guard and the CSP let the dashboard itself through."""
    page.set_input_files("#file", str(EXAMPLE_PDF))
    page.click("#submit")
    expect(page.locator("#download-link")).to_be_visible(timeout=15_000)
    # The list keeps every notebook this server process saved.
    expect(page.locator("#files li").first).to_be_visible()
    assert page.evaluate("window.__csp") == []
    assert _fits_one_screen(page)


# ---- conversions with the real provider ------------------------------------


def _finish_and_report(page, tmp_path, run_note, label):
    """Wait for the notebook, download it, and note tokens and cost."""
    # Locator waits, not wait_for_function: the CSP forbids eval().
    expect(page.locator("#download-link")).to_be_visible(
        timeout=CONVERSION_TIMEOUT_MS
    )
    expect(page.locator("#tab-download")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.locator("#submit")).to_be_enabled()
    # The list keeps every notebook this server process saved.
    expect(page.locator("#files li").first).to_be_visible()

    with page.expect_download() as download:
        page.click("#download-link")
    path = tmp_path / "notebook.ipynb"
    download.value.save_as(path)
    notebook = json.loads(path.read_text())
    kinds = {cell["cell_type"] for cell in notebook["cells"]}
    assert {"markdown", "code"} <= kinds

    page.click("#tab-metrics")
    tokens = page.locator("#m-total").inner_text().strip()
    cost = page.locator("#m-cost").inner_text().strip()
    provider = os.environ.get("PROVIDER", "?")
    model = os.environ.get("PROVIDER_MODEL") or "model from config.toml"
    # Noted for ./e2e.sh's summary, never asserted: the cost is
    # information, not pass/fail.
    run_note(
        label=label,
        provider=provider,
        model=model,
        tokens=tokens,
        cost=cost,
        cells=len(notebook["cells"]),
    )
    assert page.evaluate("window.__csp") == []


def test_upload_example_pdf_with_real_model(real_page, tmp_path, run_note):
    real_page.set_input_files("#file", str(EXAMPLE_PDF))
    assert EXAMPLE_PDF.name in real_page.locator("#file-name").inner_text()
    real_page.click("#submit")
    _finish_and_report(real_page, tmp_path, run_note, "upload")


def test_arxiv_link_with_faked_mcp(real_page, fake_arxiv, tmp_path, run_note):
    real_page.fill("#arxiv", f"https://arxiv.org/abs/{FAKE_ARXIV_ID}")
    real_page.click("#submit")
    _finish_and_report(real_page, tmp_path, run_note, "arXiv, faked MCP")
    # The lookup went through the (faked) MCP server and was logged.
    real_page.click("#tab-mcp")
    expect(real_page.locator("#mcp-events li").first).to_be_visible()
    assert FAKE_ARXIV_ID in real_page.locator("#mcp-events").inner_text()
