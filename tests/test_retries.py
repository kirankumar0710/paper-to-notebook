"""Tests for retrying temporary provider failures (overload, rate limits)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest
from fastapi import HTTPException
from google.genai import errors as genai_errors

import providers.gemini as gemini_module
from providers import ProviderError, ProviderSettings
from providers.base import MAX_ATTEMPTS
from providers.claude import ClaudeProvider
from providers.gemini import GeminiProvider

SCHEMA = {"type": "object"}
_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _gemini_settings(base_url=None):
    return ProviderSettings(
        name="gemini",
        model="test-model",
        base_url=base_url,
        api_key="test-key-0000000000",
        api_key_source="GEMINI_API_KEY",
    )


class TestRetryConfiguration:
    def test_gemini_client_retries(self):
        provider = GeminiProvider(_gemini_settings())
        options = provider._client._api_client._http_options.retry_options
        assert options.attempts == MAX_ATTEMPTS
        assert 503 in options.http_status_codes
        assert 429 in options.http_status_codes

    def test_gemini_keeps_base_url_and_version(self):
        provider = GeminiProvider(
            _gemini_settings(
                "https://generativelanguage.googleapis.com/v1beta"
            )
        )
        options = provider._client._api_client._http_options
        assert options.api_version == "v1beta"
        assert options.retry_options.attempts == MAX_ATTEMPTS

    def test_claude_client_retries(self):
        settings = ProviderSettings(
            name="anthropic", model="m", api_key="sk-test-000000000000"
        )
        provider = ClaudeProvider(settings)
        assert provider._client.max_retries == MAX_ATTEMPTS - 1


class TestRetryableFlag:
    @pytest.mark.parametrize(
        ("code", "retryable"), [(503, True), (400, False)]
    )
    def test_gemini(self, code, retryable):
        client = MagicMock()
        client.models.generate_content.side_effect = genai_errors.APIError(
            code, {"error": {"code": code, "message": "busy"}}
        )
        provider = GeminiProvider(_gemini_settings(), client=client)
        with pytest.raises(ProviderError) as info:
            provider.generate_json("x", schema=SCHEMA)
        assert info.value.retryable is retryable
        if retryable:
            assert f"after {MAX_ATTEMPTS} attempts" in str(info.value)

    @pytest.mark.parametrize(
        ("error", "retryable"),
        [
            (
                anthropic.RateLimitError(
                    "slow",
                    response=httpx2.Response(429, request=_REQUEST),
                    body=None,
                ),
                True,
            ),
            (
                anthropic.APIStatusError(
                    "overloaded",
                    response=httpx2.Response(529, request=_REQUEST),
                    body=None,
                ),
                True,
            ),
            (anthropic.APIConnectionError(request=_REQUEST), True),
            (
                anthropic.BadRequestError(
                    "bad",
                    response=httpx2.Response(400, request=_REQUEST),
                    body=None,
                ),
                False,
            ),
            (
                anthropic.AuthenticationError(
                    "key",
                    response=httpx2.Response(401, request=_REQUEST),
                    body=None,
                ),
                False,
            ),
        ],
    )
    def test_claude(self, error, retryable):
        client = MagicMock()
        client.beta.messages.stream.side_effect = error
        settings = ProviderSettings(name="anthropic", model="m")
        provider = ClaudeProvider(settings, client=client)
        with pytest.raises(ProviderError) as info:
            provider.generate_json("x", schema=SCHEMA)
        assert info.value.retryable is retryable


class _FlakyGemini(BaseHTTPRequestHandler):
    """Answers 503 a set number of times, then a valid reply."""

    failures_left = 0
    requests = 0

    def do_POST(self):  # noqa: N802 (http.server naming)
        type(self).requests += 1
        self.rfile.read(int(self.headers["Content-Length"]))
        if type(self).failures_left > 0:
            type(self).failures_left -= 1
            body = {"error": {"code": 503, "message": "high demand"}}
            self._send(503, body)
            return
        reply = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": '{"ok": true}'}],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
        self._send(200, reply)

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def flaky_server(monkeypatch):
    # Keep the test fast: retry almost immediately.
    monkeypatch.setattr(gemini_module, "RETRY_INITIAL_DELAY", 0.01)
    monkeypatch.setattr(gemini_module, "RETRY_MAX_DELAY", 0.02)
    server = HTTPServer(("127.0.0.1", 0), _FlakyGemini)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _FlakyGemini.requests = 0
    yield server
    server.shutdown()


class TestGeminiRetriesForReal:
    def _provider(self, server):
        host, port = server.server_address
        return GeminiProvider(_gemini_settings(f"http://{host}:{port}/v1beta"))

    def test_recovers_after_two_503s(self, flaky_server):
        _FlakyGemini.failures_left = 2
        result = self._provider(flaky_server).generate_json("x", schema=SCHEMA)
        assert result == {"ok": True}
        assert _FlakyGemini.requests == 3

    def test_gives_up_after_max_attempts(self, flaky_server):
        _FlakyGemini.failures_left = 100
        with pytest.raises(ProviderError) as info:
            self._provider(flaky_server).generate_json("x", schema=SCHEMA)
        assert info.value.retryable
        assert _FlakyGemini.requests == MAX_ATTEMPTS


class TestApiResponse:
    def test_temporary_failure_is_503_with_retry_after(self):
        from test_notebook import FakeProvider, _convert

        provider = FakeProvider(
            error=ProviderError("Gemini API error 503", retryable=True)
        )
        with pytest.raises(HTTPException) as info:
            _convert(provider)
        assert info.value.status_code == 503
        assert info.value.headers == {"Retry-After": "60"}
        assert "try again" in info.value.detail
