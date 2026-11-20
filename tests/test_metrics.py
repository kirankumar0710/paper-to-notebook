"""Tests for the dashboard metrics: cost, pages, notebook facts, what
served the request, and the session totals."""

import io
import json
from types import SimpleNamespace as Namespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

import api.main as api_main
from notebook_builder import pdf_to_notebook
from notebook_builder.builder import split_packages
from providers import ProviderSettings
from providers.claude import ClaudeProvider
from providers.gemini import GeminiProvider
from util.config import load_config
from util.pdf import count_pages
from util.pricing import estimate_cost, load_prices

SCHEMA = {"type": "object"}
USAGE = {"input_tokens": 20_000, "output_tokens": 10_000, "total_tokens": 0}


def _pdf(pages):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestPricing:
    def test_standard_price(self):
        cost = estimate_cost("claude-opus-5-5", USAGE)
        # 20k * $4/M + 10k * $20/M
        assert cost["total_usd"] == pytest.approx(0.28)
        assert (cost["input_rate"], cost["output_rate"]) == (4.0, 20.0)

    def test_fast_mode_doubles_price(self):
        cost = estimate_cost("claude-opus-5-5", USAGE, speed="fast")
        assert cost["total_usd"] == pytest.approx(0.56)

    def test_unknown_model_or_no_usage_has_no_cost(self):
        assert estimate_cost("gemini-3.8-flash", USAGE) is None
        assert estimate_cost("claude-opus-5-5", None) is None

    def test_invalid_lines_are_skipped(self, tmp_path):
        path = tmp_path / "p.ini"
        path.write_text(
            "[pricing]\nfast_multiplier = x\ngood = 1, 2\nbad = 1\n"
        )
        prices, multiplier = load_prices(load_config(path))
        assert prices["good"] == (1.0, 2.0)
        assert "bad" not in prices
        assert multiplier == 1.0


class TestPageCount:
    def test_counts_pages(self):
        assert count_pages(_pdf(3)) == 3

    def test_unreadable_pdf_is_none(self):
        assert count_pages(b"%PDF-1.7 not really") is None


class TestNotebookFacts:
    def test_split_packages(self):
        valid, dropped = split_packages(["a", "b>=1", "a", "x; rm -rf /"])
        assert valid == ["a", "b>=1"]
        assert dropped == ["x; rm -rf /"]

    def test_converter_reports_contents(self):
        from test_notebook import PDF, FakeProvider

        result = pdf_to_notebook(PDF, FakeProvider())
        assert result.pip_packages == ("einops", "torch>=2.0")
        assert result.dropped_packages == ("bad; rm -rf /",)
        assert result.requires_gpu is False
        assert result.code_lines == 2


def _claude_client(response):
    client = MagicMock()
    stream = client.beta.messages.stream.return_value.__enter__.return_value
    stream.__iter__.return_value = iter(())
    stream.get_final_message.return_value = response
    client.with_options.return_value = client
    return client


def _claude_response(model="claude-opus-5-5", usage=None, content=None):
    return Namespace(
        stop_reason="end_turn",
        model=model,
        usage=usage,
        content=content or [Namespace(type="text", text='{"ok": true}')],
    )


def _served(provider, response):
    updates = []
    provider(response).generate_json(
        "x", schema=SCHEMA, on_progress=updates.append
    )
    return updates[-1].served


class TestServed:
    @staticmethod
    def _claude(speed="fast"):
        settings = ProviderSettings(
            name="anthropic", model="claude-opus-5-5", options={"speed": speed}
        )
        return lambda response: ClaudeProvider(
            settings, client=_claude_client(response)
        )

    def test_fast_mode_used(self):
        usage = Namespace(input_tokens=1, output_tokens=1, speed="fast")
        served = _served(self._claude(), _claude_response(usage=usage))
        assert served == {
            "model": "claude-opus-5-5",
            "speed": "fast",
            "model_fallback": False,
            "speed_fallback": False,
        }

    def test_speed_fallback_is_reported(self):
        usage = Namespace(input_tokens=1, output_tokens=1, speed="standard")
        served = _served(self._claude(), _claude_response(usage=usage))
        assert served["speed"] == "standard"
        assert served["speed_fallback"] is True

    def test_model_fallback_is_reported(self):
        usage = Namespace(
            input_tokens=1,
            output_tokens=1,
            iterations=[Namespace(type="fallback_message")],
        )
        response = _claude_response(model="claude-opus-4-8", usage=usage)
        served = _served(self._claude("standard"), response)
        assert served["model"] == "claude-opus-4-8"
        assert served["model_fallback"] is True

    def test_claude_budget_and_speed(self):
        provider = ClaudeProvider(
            ProviderSettings(
                name="anthropic",
                model="m",
                options={"speed": "fast", "max_tokens": "64000"},
            ),
            client=MagicMock(),
        )
        assert provider.requested_speed == "fast"
        assert provider.max_output_tokens == 64000

    def test_gemini_reports_model_version(self):
        client = MagicMock()
        client.models.generate_content.return_value = Namespace(
            text='{"ok": true}',
            candidates=[Namespace(finish_reason="STOP")],
            model_version="gemini-3.8-flash-001",
        )
        provider = GeminiProvider(
            ProviderSettings(name="gemini", model="gemini-3.8-flash"),
            client=client,
        )
        updates = []
        provider.generate_json("x", schema=SCHEMA, on_progress=updates.append)
        assert updates[-1].served["model"] == "gemini-3.8-flash-001"
        assert updates[-1].served["speed"] is None
        assert provider.max_output_tokens is None


def _parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


class TestStreamMetrics:
    @pytest.fixture
    def client(self):
        def use(provider):
            api_main.app.dependency_overrides[api_main.get_llm_provider] = (
                lambda: provider
            )
            return TestClient(api_main.app)

        yield use
        api_main.app.dependency_overrides.clear()

    @staticmethod
    def _provider(**kwargs):
        from test_notebook import FakeProvider

        from providers.base import Progress

        class Priced(FakeProvider):
            def __init__(self):
                super().__init__(**kwargs)
                self.settings = ProviderSettings(
                    name="anthropic", model="claude-opus-5-5"
                )

            def generate_json(self, *args, on_progress=None, **kw):
                reply = super().generate_json(
                    *args, on_progress=on_progress, **kw
                )
                on_progress(
                    Progress(
                        "done",
                        "Done",
                        usage={
                            "input_tokens": 20_000,
                            "output_tokens": 10_000,
                            "total_tokens": 30_000,
                        },
                        served={
                            "model": "claude-opus-5-5",
                            "speed": "fast",
                            "model_fallback": False,
                            "speed_fallback": False,
                        },
                    )
                )
                return reply

        return Priced()

    def _post(self, client, data):
        return client.post(
            "/convert/stream",
            files={"file": ("paper.pdf", data, "application/pdf")},
        )

    def test_result_carries_metrics(self, client):
        test_client = client(self._provider())
        before = test_client.get("/api/metrics/session").json()
        kind, result = _parse_sse(self._post(test_client, _pdf(4)).text)[-1]
        assert kind == "result"
        assert result["pages"] == 4
        assert result["cost"]["total_usd"] == pytest.approx(0.56)
        assert result["served"]["speed"] == "fast"
        assert result["code_lines"] == 2
        assert result["pip_packages"] == ["einops", "torch>=2.0"]
        assert result["dropped_packages"] == ["bad; rm -rf /"]
        assert result["requires_gpu"] is False

        session = result["session"]
        assert session["conversions"] == before["conversions"] + 1
        assert session["total_tokens"] == before["total_tokens"] + 30_000
        assert session["cost_usd"] == pytest.approx(before["cost_usd"] + 0.56)
        assert test_client.get("/api/metrics/session").json() == session

    def test_failure_is_counted(self, client):
        from providers import ProviderError

        test_client = client(self._provider(error=ProviderError("boom")))
        before = test_client.get("/api/metrics/session").json()["failed"]
        kind, _ = _parse_sse(self._post(test_client, _pdf(1)).text)[-1]
        assert kind == "error"
        after = test_client.get("/api/metrics/session").json()["failed"]
        assert after == before + 1
