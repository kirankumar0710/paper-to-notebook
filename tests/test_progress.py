"""Tests for Claude options (effort, speed, max_tokens) and progress."""

import json
from types import SimpleNamespace as Namespace
from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import providers.claude as claude_module
from providers import ProviderError, ProviderSettings, resolve_settings
from providers.base import Progress
from providers.claude import ClaudeProvider
from providers.gemini import GeminiProvider

SCHEMA = {"type": "object"}
_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _settings(**options):
    return ProviderSettings(name="anthropic", model="m", options=options)


def _response(text='{"ok": true}', stop_reason="end_turn", usage=None):
    return Namespace(
        stop_reason=stop_reason,
        content=[Namespace(type="text", text=text)],
        usage=usage,
    )


def _client(events=(), response=None):
    client = MagicMock()
    stream = client.beta.messages.stream.return_value.__enter__.return_value
    stream.__iter__.return_value = iter(events)
    stream.get_final_message.return_value = response or _response()
    # with_options() is used for the fast-mode attempt; share the mock.
    client.with_options.return_value = client
    return client


class TestOptions:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-0000000000000")
        for var in (
            "PROVIDER_EFFORT",
            "PROVIDER_SPEED",
            "PROVIDER_MAX_TOKENS",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_read_from_config(self):
        assert resolve_settings().options == {
            "effort": "medium",
            "speed": "fast",
            "max_tokens": "128000",
        }

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_EFFORT", "low")
        monkeypatch.setenv("PROVIDER_SPEED", "standard")
        monkeypatch.setenv("PROVIDER_MAX_TOKENS", "32000")
        assert resolve_settings().options == {
            "effort": "low",
            "speed": "standard",
            "max_tokens": "32000",
        }

    def test_gemini_has_no_options(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g-test")
        assert resolve_settings("gemini").options == {}

    def test_provider_uses_options(self):
        provider = ClaudeProvider(
            _settings(effort="HIGH", speed="fast", max_tokens="5000"),
            client=MagicMock(),
        )
        assert (provider.effort, provider.speed, provider.max_tokens) == (
            "high",
            "fast",
            5000,
        )

    def test_keyword_arguments_win(self):
        provider = ClaudeProvider(
            _settings(effort="high"), effort="low", client=MagicMock()
        )
        assert provider.effort == "low"

    def test_defaults(self):
        provider = ClaudeProvider(_settings(), client=MagicMock())
        assert (provider.effort, provider.speed, provider.max_tokens) == (
            "medium",
            "standard",
            128000,
        )

    @pytest.mark.parametrize(
        ("options", "match"),
        [
            ({"effort": "extreme"}, "PROVIDER_EFFORT"),
            ({"speed": "ludicrous"}, "PROVIDER_SPEED"),
            ({"max_tokens": "lots"}, "PROVIDER_MAX_TOKENS"),
            ({"max_tokens": "-5"}, "PROVIDER_MAX_TOKENS"),
        ],
    )
    def test_invalid_options(self, options, match):
        with pytest.raises(ProviderError, match=match):
            ClaudeProvider(_settings(**options), client=MagicMock())


class TestFastMode:
    def test_fast_request(self):
        client = _client()
        ClaudeProvider(_settings(speed="fast"), client=client).generate_json(
            "x", schema=SCHEMA
        )
        client.with_options.assert_called_once_with(max_retries=0)
        assert client.beta.messages.stream.call_args.kwargs["speed"] == "fast"

    @pytest.mark.parametrize(
        "error",
        [
            anthropic.RateLimitError(
                "busy",
                response=httpx2.Response(429, request=_REQUEST),
                body=None,
            ),
            anthropic.BadRequestError(
                "speed 'fast' is not supported for this model",
                response=httpx2.Response(400, request=_REQUEST),
                body=None,
            ),
        ],
    )
    def test_falls_back_to_standard(self, error, caplog):
        client = _client()
        good = client.beta.messages.stream.return_value
        client.beta.messages.stream.side_effect = [error, good]
        updates = []
        result = ClaudeProvider(
            _settings(speed="fast"), client=client
        ).generate_json("x", schema=SCHEMA, on_progress=updates.append)

        assert result == {"ok": True}
        first, second = client.beta.messages.stream.call_args_list
        assert first.kwargs["speed"] == "fast"
        assert "speed" not in second.kwargs
        assert "Retrying at standard speed" in caplog.text
        assert "retrying" in [u.stage for u in updates]

    def test_other_bad_request_is_not_retried(self):
        client = _client()
        client.beta.messages.stream.side_effect = anthropic.BadRequestError(
            "invalid schema",
            response=httpx2.Response(400, request=_REQUEST),
            body=None,
        )
        with pytest.raises(ProviderError, match="400"):
            ClaudeProvider(
                _settings(speed="fast"), client=client
            ).generate_json("x", schema=SCHEMA)
        assert client.beta.messages.stream.call_count == 1


class TestProgressCallbacks:
    def test_claude_reports_thinking_and_writing(self, monkeypatch):
        monkeypatch.setattr(claude_module, "PROGRESS_EMIT_INTERVAL", 0)
        events = [
            Namespace(
                type="content_block_start",
                content_block=Namespace(type="thinking"),
            ),
            Namespace(
                type="content_block_delta",
                delta=Namespace(
                    type="thinking_delta",
                    thinking="Identifying the paper's core method.",
                ),
            ),
            Namespace(
                type="content_block_start",
                content_block=Namespace(type="text"),
            ),
            Namespace(
                type="content_block_delta",
                delta=Namespace(type="text_delta", text='{"ok": true}'),
            ),
        ]
        updates: list[Progress] = []
        ClaudeProvider(_settings(), client=_client(events)).generate_json(
            "x", schema=SCHEMA, on_progress=updates.append
        )

        stages = [u.stage for u in updates]
        assert stages[0] == "waiting"
        assert "thinking" in stages
        assert "writing" in stages
        assert stages[-1] == "done"
        thinking = [u for u in updates if u.thinking]
        assert "core method" in thinking[0].thinking
        # Deltas are sent once each, so joining them gives the full summary.
        assert "".join(u.thinking_delta or "" for u in updates) == (
            "Identifying the paper's core method."
        )
        assert max(u.output_chars for u in updates) == 12

    def test_updates_are_throttled(self, monkeypatch):
        monkeypatch.setattr(claude_module, "PROGRESS_EMIT_INTERVAL", 3600)
        deltas = [
            Namespace(
                type="content_block_delta",
                delta=Namespace(type="text_delta", text="x"),
            )
        ] * 50
        updates = []
        ClaudeProvider(_settings(), client=_client(deltas)).generate_json(
            "x", schema=SCHEMA, on_progress=updates.append
        )
        # Only the forced "waiting" and "done" updates get through.
        assert [u.stage for u in updates] == ["waiting", "done"]

    def test_max_tokens_message_explains_budget(self):
        client = _client(response=_response(stop_reason="max_tokens"))
        with pytest.raises(ProviderError, match="PROVIDER_EFFORT"):
            ClaudeProvider(_settings(), client=client).generate_json(
                "x", schema=SCHEMA
            )

    def test_gemini_reports_waiting_and_done(self):
        client = MagicMock()
        client.models.generate_content.return_value = Namespace(
            text='{"ok": true}',
            candidates=[Namespace(finish_reason="STOP")],
        )
        updates = []
        GeminiProvider(
            ProviderSettings(name="gemini", model="g"), client=client
        ).generate_json("x", schema=SCHEMA, on_progress=updates.append)
        assert [u.stage for u in updates] == ["waiting", "done"]
        assert updates[-1].output_chars == 12

    def test_progress_to_dict(self):
        assert Progress("writing", "W", 1.26, 5, None).to_dict() == {
            "stage": "writing",
            "message": "W",
            "elapsed": 1.3,
            "output_chars": 5,
            "thinking": None,
            "thinking_delta": None,
            "usage": None,
            "served": None,
            "check": None,
        }

    def test_claude_reports_usage_when_done(self):
        response = _response(
            usage=Namespace(input_tokens=1200, output_tokens=345)
        )
        updates = []
        ClaudeProvider(
            _settings(), client=_client(response=response)
        ).generate_json("x", schema=SCHEMA, on_progress=updates.append)
        assert updates[-1].stage == "done"
        assert updates[-1].usage == {
            "input_tokens": 1200,
            "output_tokens": 345,
            "total_tokens": 1545,
        }

    def test_claude_without_usage(self):
        updates = []
        ClaudeProvider(_settings(), client=_client()).generate_json(
            "x", schema=SCHEMA, on_progress=updates.append
        )
        assert updates[-1].usage is None

    def test_gemini_reports_usage_with_thinking_as_output(self):
        client = MagicMock()
        client.models.generate_content.return_value = Namespace(
            text='{"ok": true}',
            candidates=[Namespace(finish_reason="STOP")],
            usage_metadata=Namespace(
                prompt_token_count=900,
                candidates_token_count=100,
                thoughts_token_count=50,
                total_token_count=1050,
            ),
        )
        updates = []
        GeminiProvider(
            ProviderSettings(name="gemini", model="g"), client=client
        ).generate_json("x", schema=SCHEMA, on_progress=updates.append)
        assert updates[-1].usage == {
            "input_tokens": 900,
            "output_tokens": 150,
            "total_tokens": 1050,
        }


def _parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


class TestStreamEndpoint:
    @pytest.fixture
    def client(self):
        from test_notebook import FakeProvider

        def use(provider=None):
            api_main.app.dependency_overrides[api_main.get_llm_provider] = (
                lambda: provider or FakeProvider()
            )
            return TestClient(api_main.app)

        yield use
        api_main.app.dependency_overrides.clear()

    def _post(self, client, name="paper.pdf", data=b"%PDF-1.7 fake"):
        return client.post(
            "/convert/stream",
            files={"file": (name, data, "application/pdf")},
        )

    def test_streams_progress_then_result(self, client, notebook_output_dir):
        response = self._post(client())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(response.text)
        stages = [d["stage"] for e, d in events if e == "progress"]
        assert stages == ["uploaded", "thinking", "writing", "building"]
        assert events[1][1]["thinking"] == "Hmm"

        kind, result = events[-1]
        assert kind == "result"
        assert result["filename"] == "paper.ipynb"
        assert result["saved_path"] == str(notebook_output_dir / "paper.ipynb")
        assert json.loads(result["notebook"])["cells"]
        assert (notebook_output_dir / "paper.ipynb").exists()
        assert result["upload_bytes"] == len(b"%PDF-1.7 fake")
        assert result["notebook_bytes"] == len(result["notebook"].encode())
        assert result["cells"] == (
            result["markdown_cells"] + result["code_cells"]
        )
        assert (result["provider"], result["model"]) == ("fake", "fake-model")
        assert result["usage"] is None

    def test_result_includes_usage_from_done(self, client):
        from test_notebook import FakeProvider

        class UsageProvider(FakeProvider):
            def generate_json(self, *args, on_progress=None, **kwargs):
                reply = super().generate_json(
                    *args, on_progress=on_progress, **kwargs
                )
                on_progress(
                    Progress(
                        "done",
                        "Done",
                        usage={
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "total_tokens": 15,
                        },
                    )
                )
                return reply

        events = _parse_sse(self._post(client(UsageProvider())).text)
        kind, result = events[-1]
        assert kind == "result"
        assert result["usage"]["total_tokens"] == 15

    def test_saved_notebook_can_be_listed_and_downloaded(self, client):
        test_client = client()
        result = _parse_sse(self._post(test_client).text)[-1][1]
        url = result["download_url"]
        assert url.startswith("/api/notebooks/")

        listed = test_client.get("/api/notebooks").json()
        assert listed[0]["download_url"] == url
        assert listed[0]["exists"] is True
        assert listed[0]["size"] == len(result["notebook"].encode())

        download = test_client.get(url)
        assert download.status_code == 200
        assert download.text == result["notebook"]
        assert "paper.ipynb" in download.headers["content-disposition"]

    def test_unknown_notebook_is_404(self, client):
        response = client().get("/api/notebooks/../../etc/passwd")
        assert response.status_code == 404
        assert client().get("/api/notebooks/nope").status_code == 404

    def test_streams_error(self, client):
        from test_notebook import FakeProvider

        provider = FakeProvider(error=ProviderError("busy", retryable=True))
        events = _parse_sse(self._post(client(provider)).text)
        kind, error = events[-1]
        assert kind == "error"
        assert error["status"] == 503
        assert "try again" in error["detail"]

    def test_bad_upload_is_plain_400(self, client):
        response = self._post(client(), name="paper.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Only PDF files are accepted."

    def test_unusable_reply_streams_502(self, client):
        from test_notebook import FakeProvider

        provider = FakeProvider(reply={"title": "x", "cells": []})
        kind, error = _parse_sse(self._post(client(provider)).text)[-1]
        assert (kind, error["status"]) == ("error", 502)


class TestConfigEndpoint:
    @pytest.fixture
    def client(self, monkeypatch):
        for var in ("PROVIDER", "PROVIDER_MODEL", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        return TestClient(api_main.app)

    def test_configured(self, client, monkeypatch, notebook_output_dir):
        monkeypatch.setenv("PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-0000000000000")
        info = client.get("/api/config").json()
        assert info["provider"] == "anthropic"
        assert info["model"]
        assert info["api_key"] == {
            "found": True,
            "source": "ANTHROPIC_API_KEY",
            "env_vars": ["ANTHROPIC_API_KEY"],
        }
        assert info["output_dir"]["path"] == str(notebook_output_dir)
        assert info["output_dir"]["source"] == "$NOTEBOOK_OUTPUT_DIR"
        assert info["output_dir"]["writable"] is True
        assert info["problems"] == []

    def test_key_is_never_returned(self, client, monkeypatch):
        monkeypatch.setenv("PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-qzxjvwkqzxjv")
        text = client.get("/api/config").text
        assert "sk-s" not in text
        assert "zxjv" not in text

    def test_missing_key(self, client, monkeypatch):
        monkeypatch.setenv("PROVIDER", "anthropic")
        info = client.get("/api/config").json()
        assert info["provider"] == "anthropic"
        assert info["api_key"]["found"] is False
        assert any("ANTHROPIC_API_KEY" in p for p in info["problems"])

    def test_nothing_configured(self, client, monkeypatch, tmp_path):
        monkeypatch.delenv("NOTEBOOK_OUTPUT_DIR")
        monkeypatch.setenv("HOME", str(tmp_path))
        info = client.get("/api/config").json()
        assert info["provider"] is None
        assert info["output_dir"] == {
            "path": str(tmp_path / "Downloads"),
            "source": "default",
            "exists": False,
            "writable": True,
        }
        assert len(info["problems"]) == 1
